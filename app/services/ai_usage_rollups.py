"""Recoverable projections of the immutable v2 usage ledger.

Keep a one-time baseline for pre-v2 counters. Recompute v2 contributions instead
of replaying increments; monotonic writes prevent stale concurrent snapshots
from rolling counters back. No transaction or per-event array on users needed.
"""
import asyncio
import logging
from copy import deepcopy
from app.core.diagnostics import event as diagnostic_event, scope

logger = logging.getLogger(__name__)


def _get(doc, dotted):
    for key in dotted.split("."):
        doc = doc.get(key, {}) if isinstance(doc, dict) else {}
    return doc if isinstance(doc, (int, float)) else 0


async def _project(db, collection, owner_id, institute=False):
    target = db[collection]
    owner = await target.find_one({"_id": owner_id})
    if owner is None:
        diagnostic_event('ai.rollup.target',collection=collection,owner_id=str(owner_id),outcome='pending',reason='owner_missing')
        return False
    # Compare-and-set captures the legacy baseline once, across all workers.
    await target.update_one(
        {"_id": owner_id, "ai_usage_v2_baseline": {"$exists": False}},
        {"$set": {"ai_usage_v2_baseline": deepcopy(owner.get("token_usage") or {})}},
    )
    owner = await target.find_one({"_id": owner_id})
    baseline = owner["ai_usage_v2_baseline"]
    query = {"accounting_version": 2, "usage_status": "known",
             "institute_id" if institute else "user_id": owner_id}
    if institute:
        query["record_institute"] = True
    totals = {}
    async for event in db["aiUsageEvents"].find(query):
        if event.get("call_status") == "skipped":
            continue
        values = totals.setdefault(event["provider"], [0, 0, 0])
        values[0] += event["input_tokens"]
        values[1] += event["output_tokens"]
        values[2] += 1
    fields = {}
    for provider, (inputs, outputs, calls) in totals.items():
        prefix = f"token_usage.{provider}"
        if not institute:
            fields[f"{prefix}.input_tokens"] = _get(baseline, f"{provider}.input_tokens") + inputs
            fields[f"{prefix}.output_tokens"] = _get(baseline, f"{provider}.output_tokens") + outputs
        else:
            input_name, output_name = (("total_prompt_tokens", "total_candidate_tokens") if provider == "gemini"
                                       else ("total_input_tokens", "total_output_tokens"))
            for name, value in ((input_name, inputs), (output_name, outputs),
                                ("total_tokens", inputs + outputs), ("call_count", calls)):
                fields[f"{prefix}.{name}"] = _get(baseline, f"{provider}.{name}") + value
    if institute:
        fields["token_usage.grand_total_tokens"] = _get(baseline, "grand_total_tokens") + sum(v[0] + v[1] for v in totals.values())
    if fields:
        result = await target.update_one({"_id": owner_id}, {"$max": fields})
        diagnostic_event('ai.rollup.target',collection=collection,owner_id=str(owner_id),
                         outcome='projected' if result.matched_count==1 else 'pending',
                         matched_count=result.matched_count,modified_count=getattr(result,'modified_count',None),
                         projected_field_count=len(fields))
        return result.matched_count == 1
    diagnostic_event('ai.rollup.target',collection=collection,owner_id=str(owner_id),outcome='no_v2_totals')
    return True


async def project_usage_event(db, event):
    diagnostic_event('ai.rollup.started',ai_event_id=str(event['_id']),previous_pending=event.get('rollup_pending'))
    success = True
    targets = [("users", event["user_id"], False)]
    if event.get("record_institute") and event.get("institute_id"):
        targets.append(("instituteDetails", event["institute_id"], True))
    for collection, owner_id, institute in targets:
        try:
            with scope(ai_event_id=str(event['_id'])):
                if not await _project(db, collection, owner_id, institute):
                    success = False
        except Exception as exc:
            success = False
            diagnostic_event('ai.rollup.target',ai_event_id=str(event['_id']),collection=collection,
                             owner_id=str(owner_id),outcome='pending',reason='projection_exception',error_type=type(exc).__name__)
            logger.exception("Usage projection pending: %s %s", collection, owner_id)
    try:
        result = await db["aiUsageEvents"].update_one({"_id": event["_id"]}, {"$set": {"rollup_pending": not success}})
    except Exception as exc:
        diagnostic_event('ai.rollup.outcome',ai_event_id=str(event['_id']),outcome='ledger_confirmation_unconfirmed',
                         targets_projected=success,error_type=type(exc).__name__)
        raise
    diagnostic_event('ai.rollup.outcome',ai_event_id=str(event['_id']),
                     outcome=('projected' if success else 'pending') if result.matched_count==1 else 'ledger_event_missing',
                     rollup_pending=(not success) if result.matched_count==1 else None,
                     requested_rollup_pending=not success,ledger_matched_count=result.matched_count)


async def reconcile_usage_rollups(db):
    processed=0
    async for event in db["aiUsageEvents"].find({"accounting_version": 2, "rollup_pending": True}):
        with scope(accounting_phase='reconciliation',ai_event_id=str(event['_id'])):
            diagnostic_event('ai.reconciliation.event',outcome='attempting_pending_event')
            await project_usage_event(db, event)
        processed+=1
    diagnostic_event('ai.reconciliation.scan_completed',processed_count=processed)


async def usage_reconciliation_loop(db):
    while True:
        try:
            await reconcile_usage_rollups(db)
        except Exception as exc:
            diagnostic_event('ai.reconciliation.scan_failed',error_type=type(exc).__name__,next_retry_seconds=60)
            logger.exception("AI usage reconciliation will retry")
        await asyncio.sleep(60)
