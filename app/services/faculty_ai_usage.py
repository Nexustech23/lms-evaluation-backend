"""Track each LMS AI step before downstream parsing, rendering or persistence."""
import asyncio
import logging
from bson import ObjectId
from app.core.diagnostics import event as log_event, scope, stage
from app.core.diagnostic_details import external_error_details
from app.models.ai_usage_event import create_ai_usage_event_document
from app.services.ai_usage import record_ai_usage
from app.services.ai_attempt_context import retry_recorder
from app.utils.token_usage import resolve_institute_id_for_faculty

logger = logging.getLogger(__name__)
_in_flight = set()


async def tracked_faculty_call(db, function, *args, faculty_id, user_id, provider,
                               model, feature, job_id=None, prompt_version=None):
    institute_id = None
    event_id = ObjectId()
    try:
        institute_id = await resolve_institute_id_for_faculty(db, str(faculty_id))
    except Exception as exc:
        logger.exception("Could not resolve institute for AI attempt %s", event_id)
        raise RuntimeError("Unable to resolve the AI billing account. Please retry.") from exc
    try:
        if not user_id:
            faculty = await db["facultyDetails"].find_one({"_id": ObjectId(faculty_id)})
            user_id = str(faculty["user_id"]) if faculty and faculty.get("user_id") else None
        if not user_id:
            raise ValueError("AI attempt requires an attributable user")
        if user_id:
            event = create_ai_usage_event_document(
                user_id=str(user_id), provider=provider, model=model, feature=feature,
                input_tokens=0, output_tokens=0, cost_usd=0, job_id=job_id,
                institute_id=str(institute_id) if institute_id else None,
            )
            event.update(_id=event_id, input_tokens=None, output_tokens=None,
                         total_tokens=None, cost_usd=None, usage_status="unknown",
                         cost_status="unknown", call_status="started")
            if prompt_version:
                event["prompt_version"] = prompt_version
            await db["aiUsageEvents"].insert_one(event)
    except Exception as exc:
        logger.exception("Could not persist AI attempt %s", event_id)
        raise RuntimeError("Unable to record the AI attempt. Please retry before generating content.") from exc

    loop = asyncio.get_running_loop()

    def retry(error, retry_provider, retry_model):
        if not user_id:
            return
        log_event("ai.retry", provider=retry_provider, model=retry_model, feature=feature,
                  error_type=type(error).__name__, job_id=job_id, external_error=external_error_details(error))
        usage = getattr(error, "usage", None) or {"usage_status": "unknown"}
        future = asyncio.run_coroutine_threadsafe(record_ai_usage(
            db, user_id=str(user_id), provider=retry_provider, model=retry_model, feature=feature,
            usage={**usage, "call_status": "failed"}, job_id=job_id,
            institute_id=str(institute_id) if institute_id else None,
            event_id=ObjectId(), record_institute=True), loop)
        try:
            future.result(timeout=10)
        except Exception:
            logger.exception("Could not persist provider retry attempt")

    async def execute():
        token = retry_recorder.set(retry)
        try:
            return await _execute_call(db, function, args, user_id, provider, model,
                                       feature, job_id, institute_id, event_id)
        finally:
            retry_recorder.reset(token)

    task = asyncio.create_task(execute())
    _in_flight.add(task)
    def completed(done):
        _in_flight.discard(done)
        if not done.cancelled():
            done.exception()  # Consume errors if the caller has already cancelled.
    task.add_done_callback(completed)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # to_thread continues running: let its accounting finish independently.
        try:
            await db["aiUsageEvents"].update_one({"_id": event_id}, {"$set": {"caller_cancelled": True}})
        except Exception:
            logger.exception("Could not flag caller cancellation")
        raise


async def _execute_call(db, function, args, user_id, provider, model, feature, job_id, institute_id, event_id):
    try:
        with scope(ai_event_id=str(event_id), provider=provider, model=model, feature=feature,
                   user_id=str(user_id), job_id=job_id, institute_id=str(institute_id) if institute_id else None):
            with stage("ai.provider_call"):
                result, usage = await asyncio.to_thread(function, *args)
    except BaseException as exc:
        # Cancellation also leaves an honest unknown-cost attempt, never a fake zero.
        usage = getattr(exc, "usage", None)
        if usage and user_id:
            await record_ai_usage(
                db, user_id=str(user_id), provider=provider, model=model, feature=feature,
                usage={**usage, "call_status": "failed"}, job_id=job_id,
                institute_id=str(institute_id) if institute_id else None,
                event_id=event_id, record_institute=True,
            )
        else:
            await _mark_unknown(db, event_id, "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed")
        raise

    if not usage or (
        isinstance(usage, dict) and usage.get("usage_status") == "unknown"
    ):
        status = usage.get("call_status", "completed") if isinstance(usage, dict) else "completed"
        await _mark_unknown(db, event_id, status)
    elif user_id:
        await record_ai_usage(
            db, user_id=str(user_id), provider=provider, model=usage.get("model", model),
            feature=feature, usage=usage, job_id=job_id,
            institute_id=str(institute_id) if institute_id else None,
            event_id=event_id, record_institute=True,
        )
    log_event("ai.call_accounted", ai_event_id=str(event_id), provider=provider, model=model,
              feature=feature, job_id=job_id, usage_status=(usage or {}).get("usage_status", "known"))
    return result, usage


async def _mark_unknown(db, event_id, status):
    log_event("ai.usage_unknown", ai_event_id=str(event_id), call_status=status)
    try:
        await db["aiUsageEvents"].update_one(
            {"_id": event_id}, {"$set": {"usage_status": "unknown", "call_status": status}},
        )
    except Exception:
        logger.exception("Could not mark AI attempt %s as %s", event_id, status)
