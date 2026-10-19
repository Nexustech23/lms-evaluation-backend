"""Recoverable projections of the immutable v2 usage ledger.

Keep a one-time baseline for pre-v2 counters. Recompute v2 contributions instead
of replaying increments; monotonic writes prevent stale concurrent snapshots
from rolling counters back. No transaction or per-event array on users needed.
"""
import asyncio
import logging
from copy import deepcopy

logger = logging.getLogger(__name__)


def _get(doc, dotted):
    for key in dotted.split("."):
        doc = doc.get(key, {}) if isinstance(doc, dict) else {}
    return doc if isinstance(doc, (int, float)) else 0


async def _project(db, collection, owner_id, institute=False):
    target = db[collection]
    owner = await target.find_one({"_id": owner_id})
    if owner is None:
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
        return result.matched_count == 1
    return True


async def project_usage_event(db, event):
    success = True
    targets = [("users", event["user_id"], False)]
    if event.get("record_institute") and event.get("institute_id"):
        targets.append(("instituteDetails", event["institute_id"], True))
    for collection, owner_id, institute in targets:
        try:
            if not await _project(db, collection, owner_id, institute):
                success = False
        except Exception:
            success = False
            logger.exception("Usage projection pending: %s %s", collection, owner_id)
    await db["aiUsageEvents"].update_one({"_id": event["_id"]}, {"$set": {"rollup_pending": not success}})


async def reconcile_usage_rollups(db):
    async for event in db["aiUsageEvents"].find({"accounting_version": 2, "rollup_pending": True}):
        await project_usage_event(db, event)


async def usage_reconciliation_loop(db):
    while True:
        try:
            await reconcile_usage_rollups(db)
        except Exception:
            logger.exception("AI usage reconciliation will retry")
        await asyncio.sleep(60)
