import json
from typing import Any, Dict, Optional

from app.core.redis_client import get_redis
from app.core.diagnostics import event, job_progress
from redis.exceptions import WatchError

JOB_TTL = 3600  # 1 hour — matches Flask's job stores


async def set_job(prefix: str, job_id: str, payload: Dict[str, Any]) -> None:
    await get_redis().setex(f"{prefix}{job_id}", JOB_TTL, json.dumps(payload))
    event("job.created", job_id=job_id, status=payload.get("status"), job_prefix=prefix)


async def update_job(prefix: str, job_id: str, patch: Dict[str, Any]) -> None:
    if await _change_job(prefix, job_id, patch):
        job_progress(job_id, patch)



TERMINAL = {"done", "completed", "failed", "error"}


async def _change_job(prefix, job_id, patch, claim=False):
    key = f"{prefix}{job_id}"
    redis = get_redis()
    operation="claim_execution" if claim else "update"
    for attempt in range(10):
        async with redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                existing = await pipe.get(key)
                if not existing:
                    event("job.update_skipped",job_id=job_id,operation=operation,reason="missing_or_expired")
                    return False  # Never recreate an expired, ownerless result.
                data = json.loads(existing)
                if claim and not data.get("owner_user_id"):
                    event("job.update_skipped",job_id=job_id,operation=operation,reason="owner_missing")
                    return False
                if data.get("status") in TERMINAL:
                    event("job.update_skipped",job_id=job_id,operation=operation,reason="already_terminal",status=data.get("status"))
                    return False
                if claim and data.get("execution_started"):
                    event("job.update_skipped",job_id=job_id,operation=operation,reason="execution_already_claimed")
                    return False
                safe_patch = {k: v for k, v in patch.items() if k != "owner_user_id"}
                data.update(safe_patch)
                pipe.multi()
                pipe.setex(key, JOB_TTL, json.dumps(data))
                await pipe.execute()
                if claim:event("job.execution_claimed",job_id=job_id)
                return True
            except WatchError:
                event("job.state_contention",job_id=job_id,operation=operation,attempt=attempt+1)
                continue
    event("job.update_skipped",job_id=job_id,operation=operation,reason="contention_exhausted")
    raise RuntimeError("Job state changed repeatedly; retry the status request")


async def claim_job_execution(prefix, job_id):
    return await _change_job(prefix, job_id, {"execution_started": True}, claim=True)


async def get_job(prefix: str, job_id: str) -> Optional[Dict[str, Any]]:
    data = await get_redis().get(f"{prefix}{job_id}")
    return json.loads(data) if data else None


async def get_owned_job(prefix: str, job_id: str, identity: dict) -> Optional[Dict[str, Any]]:
    """Fail closed for foreign jobs and legacy jobs without an authenticated owner."""
    job = await get_job(prefix, job_id)
    user_id = identity.get("user_id")
    if not job or not user_id or job.get("owner_user_id") != str(user_id):
        return None
    return job
