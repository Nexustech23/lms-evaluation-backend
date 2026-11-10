"""Read-only readiness probes. Never call AI, upload files, or mutate data."""
import asyncio
from app.core.config import settings
from app.core.redis_client import get_redis

WORKER_HEALTH_KEY = "lms:worker:health"


async def readiness(db):
    checks = {"database": "disconnected", "transactions": "unavailable",
              "redis": "disconnected", "worker": "not_required"}
    async def mongo():
        await db.command("ping")
        checks["database"] = "connected"
        hello = await db.command("hello")
        if not (hello.get("setName") or hello.get("msg") == "isdbgrid"):
            return
        async with await db.client.start_session() as session:
            session.start_transaction()
            try:
                await db["users"].find_one({}, {"_id": 1}, session=session)
            finally:
                await session.abort_transaction()
            checks["transactions"] = "ready"
    async def redis():
        client = get_redis()
        await client.ping()
        checks["redis"] = "connected"
        if settings.QUEUE_MODE == "redis":
            checks["worker"] = "ready" if await client.exists(WORKER_HEALTH_KEY) else "unavailable"
    if settings.QUEUE_MODE == "redis":
        checks["worker"] = "unavailable"
    await asyncio.gather(*(asyncio.wait_for(fn(), timeout=5) for fn in (mongo, redis)), return_exceptions=True)
    checks["queue_mode"] = settings.QUEUE_MODE
    ready = (checks["database"] == "connected" and checks["transactions"] == "ready"
             and checks["redis"] == "connected" and checks["worker"] in {"ready", "not_required"})
    return {"status": "healthy" if ready else "unhealthy", **checks}
