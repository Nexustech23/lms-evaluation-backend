# ============================================================
# Shared finalization path for AI usage tracking.
# Writes:
#   1. one row in aiUsageEvents (the full ledger — per-feature dashboards,
#      RAG-savings measurement, cost drill-down)
#   2. a recoverable projection on users.token_usage.<provider> (the fast rollup —
#      quota checks read this single document instead of aggregating a
#      growing event collection on every generation request)
#
# Finalization is best-effort / non-fatal, matching other token helpers in
# this codebase (app/utils/token_usage.py, the old
# roadmap_ai.increment_student_*_tokens this replaces) — a tracking failure
# must never hide a completed provider result. The LMS wrapper requires a
# durable attempt BEFORE allowing spend; later failures remain visible as unknown.
#
# Accepts the raw usage object/dict exactly as returned by whichever SDK
# helper produced it (four different shapes are in live use across this
# codebase — see _extract_tokens) so call sites don't need their own
# extraction logic, only to say which provider/model/feature they are.
# ============================================================
import logging
import asyncio
from typing import Any, Optional
from bson import ObjectId

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.models.ai_usage_event import create_ai_usage_event_document
from app.utils.ai_pricing import estimate_cost_usd
from app.services.ai_usage_rollups import project_usage_event

logger = logging.getLogger("ai_usage")


def _token_count(value):
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("Boolean token count")
    count = int(value)
    if count < 0 or float(value) != count:
        raise ValueError("Invalid token count")
    return count


def _gemini_output_tokens(prompt_tokens: int, candidate_tokens: int, total_tokens: int) -> int:
    """Gemini's candidates_token_count is only the VISIBLE answer — a thinking model
    (any Gemini call that doesn't explicitly disable thinking_config, which today is
    every call site except generate_html_from_prompt) also spends thoughts_token_count,
    which Google bills as output but which candidates_token_count alone excludes.
    total_token_count is prompt + candidates + thoughts, so whenever it's bigger than
    prompt+candidates, the gap is thinking tokens — fold it into the output figure used
    for cost estimation instead of silently dropping it. Falls back to candidate_tokens
    when total_tokens is 0/missing (matches _parse_token_usage's own zeroed fallback),
    and never returns less than candidate_tokens even if total looks stale/inconsistent.
    """
    if not total_tokens:
        return candidate_tokens
    return max(candidate_tokens, total_tokens - prompt_tokens)


def _extract_tokens(usage: Any) -> tuple[int, int]:
    """Normalizes the four usage shapes already in use across this codebase:
    - dict from app.services.claude (generate_text/generate_html): input_tokens/output_tokens
    - dict from app.services.gemini (_parse_token_usage): prompt_tokens/candidate_tokens/total_tokens
    - anthropic SDK Usage object (roadmap_ai.py's generate_claude_json/generate_curriculum/generate_claude_text)
    - google-genai SDK usage_metadata object (roadmap_ai.py's generate_gemini_json)
    """
    if usage is None:
        return 0, 0

    if isinstance(usage, dict):
        if "input_tokens" in usage:
            return _token_count(usage.get("input_tokens")), _token_count(usage.get("output_tokens"))
        prompt = _token_count(usage.get("prompt_tokens"))
        candidate = _token_count(usage.get("candidate_tokens"))
        total = _token_count(usage.get("total_tokens"))
        return prompt, _gemini_output_tokens(prompt, candidate, total)

    input_tokens = getattr(usage, "input_tokens", None)
    if input_tokens is not None:
        return _token_count(input_tokens), _token_count(getattr(usage, "output_tokens", 0))

    # Raw google-genai SDK usage_metadata object (roadmap_ai.py's generate_gemini_json
    # passes response.usage_metadata straight through) — same thinking-token gap as
    # the dict branch above, fixed the same way.
    prompt = _token_count(getattr(usage, "prompt_token_count", 0))
    candidate = _token_count(getattr(usage, "candidates_token_count", 0))
    total = _token_count(getattr(usage, "total_token_count", 0))
    return prompt, _gemini_output_tokens(prompt, candidate, total)


async def _record_ai_usage_once(
    db: AsyncIOMotorDatabase,
    *,
    user_id: str,
    provider: str,
    model: str,
    feature: str,
    usage: Any,
    grounded: Optional[bool] = None,
    context_id: Optional[str] = None,
    job_id: Optional[str] = None,
    institute_id: Optional[str] = None,
    school_id: Optional[str] = None,
    programme_id: Optional[str] = None,
    event_id: Optional[ObjectId] = None,
    record_institute: bool = False,
    skip_zero: bool = False,
) -> None:
    try:
        if isinstance(usage, dict):
            known = usage.get("usage_status") != "unknown" and (
                all(usage.get(key) is not None for key in ("input_tokens", "output_tokens")) or
                all(usage.get(key) is not None for key in ("prompt_tokens", "candidate_tokens", "total_tokens")))
        else:
            known = usage is not None and (
                all(getattr(usage, key, None) is not None for key in ("input_tokens", "output_tokens")) or
                (getattr(usage, "prompt_token_count", None) is not None and
                 (getattr(usage, "candidates_token_count", None) is not None or
                  getattr(usage, "total_token_count", None) is not None)))
        try:
            input_tokens, output_tokens = _extract_tokens(usage)
            cached = sum(_token_count(usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None))
                         for key in ("cache_creation_input_tokens", "cache_read_input_tokens"))
        except (TypeError, ValueError, OverflowError):
            input_tokens, output_tokens, known = 0, 0, False
            cached = 0
        input_tokens += cached
        if input_tokens < 0 or output_tokens < 0:
            known = False
        if known and input_tokens == 0 and output_tokens == 0 and (event_id is None or skip_zero):
            # No real API spend to record (e.g. a local-extraction path that
            # never called the model) — skip rather than pollute the ledger
            # with zero-cost rows.
            return

        cost_usd = estimate_cost_usd(model, input_tokens, output_tokens) if known else None
        # Cached Claude tokens need their own tariff; never price them as free.
        if cached:
            cost_usd = None

        event = create_ai_usage_event_document(
            user_id=user_id,
            provider=provider,
            model=model,
            feature=feature,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            grounded=grounded,
            context_id=context_id,
            job_id=job_id,
            institute_id=institute_id,
            school_id=school_id,
            programme_id=programme_id,
        )
        event_id = event_id or ObjectId()
        event.update(accounting_version=2, record_institute=record_institute,
                     usage_status="known" if known else "unknown",
                     cost_status="known" if cost_usd is not None else "unknown",
                     call_status=usage.get("call_status", "completed") if isinstance(usage, dict) else "completed",
                     rollup_pending=known, finalized=known)
        if not known:
            event.update(input_tokens=None, output_tokens=None, total_tokens=None, cost_usd=None)
        if cached:
            event["cache_tokens"] = cached
        # Create once, then finalize once. Replays cannot replace known usage.
        await db["aiUsageEvents"].update_one({"_id": event_id}, {"$setOnInsert": event}, upsert=True)
        existing = await db["aiUsageEvents"].find_one({"_id": event_id})
        if existing["user_id"] != event["user_id"] or existing["provider"] != provider:
            raise ValueError("AI event identity mismatch")
        if not existing.get("finalized"):
            # Preserve the original timestamp, so finalization doesn't move the spend date.
            event.pop("created_at", None)
            await db["aiUsageEvents"].update_one(
                {"_id": event_id, "finalized": {"$ne": True}}, {"$set": event})
        stored = await db["aiUsageEvents"].find_one({"_id": event_id})
        if stored.get("usage_status") == "known":
            await project_usage_event(db, stored)
        from app.core.diagnostics import event as log_event
        log_event("ai.usage_finalized", ai_event_id=str(event_id),
                  **{k:stored.get(k) for k in ("user_id","institute_id","job_id","provider","model",
                      "feature","input_tokens","output_tokens","total_tokens","cache_tokens",
                      "cost_usd","usage_status","cost_status","call_status")},
                  rollup_pending_before_projection=stored.get('rollup_pending'))
        return True
    except Exception as e:
        logger.warning(
            "record_ai_usage failed (non-fatal): user_id=%s provider=%s feature=%s error=%s",
            user_id, provider, feature, e,
        )
        return False


async def record_ai_usage(db, **kwargs):
    # Retain the same event ID when a ledger write has an ambiguous/transient
    # failure. Retrying must never create a second charge for the same call.
    kwargs["skip_zero"] = kwargs.get("event_id") is None
    kwargs["event_id"] = kwargs.get("event_id") or ObjectId()
    for attempt in range(3):
        if await _record_ai_usage_once(db, **kwargs) is not False:
            return
        if attempt < 2:
            await asyncio.sleep(0.05 * (2 ** attempt))


async def ensure_ai_usage_indexes(db: AsyncIOMotorDatabase) -> None:
    """Idempotent — safe to call on every app startup (see app/main.py's
    lifespan). No index-creation mechanism exists elsewhere in this
    codebase, so this is the only place aiUsageEvents' indexes are defined."""
    await db["aiUsageEvents"].create_index([("user_id", 1), ("created_at", -1)])
    await db["aiUsageEvents"].create_index([("institute_id", 1), ("school_id", 1), ("created_at", -1)])
    await db["aiUsageEvents"].create_index([("feature", 1), ("created_at", -1)])
    await db["aiUsageEvents"].create_index([("accounting_version", 1), ("rollup_pending", 1)])
    await db["aiUsageEvents"].create_index([("user_id", 1), ("accounting_version", 1), ("usage_status", 1)])
    await db["aiUsageEvents"].create_index([("institute_id", 1), ("accounting_version", 1), ("usage_status", 1), ("record_institute", 1)])
