"""Short-lived process presence for conservative, explicitly requested recovery."""
import asyncio
from uuid import uuid4
from datetime import datetime, timezone
from fastapi import HTTPException
from app.core.redis_client import get_redis
from app.core.diagnostics import event

PROCESS_INSTANCE = uuid4().hex
LEASE_SECONDS = 90


async def process_lease():
    key = f"lms:process:{PROCESS_INSTANCE}"
    try:
        while True:
            try:
                await get_redis().setex(key, LEASE_SECONDS, "alive")
            except Exception:
                event("job.process_lease_unavailable")
            await asyncio.sleep(15)
    finally:
        # Leave the lease to expire: shielded provider threads may still be
        # finishing during graceful shutdown. Never advertise immediate death.
        pass


async def process_present(instance):
    if not instance:
        return True  # Legacy/unknown execution is not safe to abandon.
    return bool(await get_redis().exists(f"lms:process:{instance}"))


async def execution_is_orphaned(active, execution=None, presence=None):
    execution = execution or {}
    run = execution if execution.get("job_id") and execution.get("job_id") == active.get("job_id") else active
    since = run.get("started_at")
    if not isinstance(since, datetime):
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc)-since).total_seconds() < LEASE_SECONDS:
        return False
    try:
        return not await (presence or process_present)(run.get("process_instance"))
    except Exception as exc:
        raise HTTPException(503, "Execution health cannot be verified. Retry recovery later.") from exc
