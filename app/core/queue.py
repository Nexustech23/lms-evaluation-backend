# ============================================================
# Task queue (Perf Phase 2).
#
# The AI / PDF jobs used to run via FastAPI BackgroundTasks — i.e. *inside
# the web process*: a web worker restart lost in-flight jobs, there was no
# retry, and a burst of jobs starved normal requests. They now go to a
# Redis queue (arq) processed by a separate `worker` container that scales
# independently.
#
# The job *bodies* are unchanged — only the dispatch mechanism moved. The
# client contract is identical (POST returns a job_id, poll job-status).
#
# QUEUE_MODE:
#   "inline" (default) — run the job body immediately in the web process,
#              awaited before the request returns. No worker container
#              needed; matches the old BackgroundTasks "job is done by the
#              time you poll" behaviour. Also what the test suite uses.
#   "redis"  — enqueue to arq; a running lms-worker container runs the job
#              off the request path. Opt in via .env + the compose "queue"
#              profile when you want jobs off the web tier.
# ============================================================
from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger("queue")

_pool: Any = None


async def connect_to_queue() -> None:
    """Called from the web app's lifespan. No-op in inline mode."""
    global _pool
    if settings.QUEUE_MODE != "redis":
        return
    from arq import create_pool
    from arq.connections import RedisSettings

    _pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    logger.info("arq queue pool connected")


async def close_queue() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
    _pool = None


async def enqueue(job_name: str, *args: Any) -> None:
    """Hand a job to the worker (or run it inline in tests)."""
    if settings.QUEUE_MODE == "inline":
        from app.worker import JOB_REGISTRY

        await JOB_REGISTRY[job_name](None, *args)  # ctx=None
        return

    if _pool is None:
        # Lifespan didn't run (shouldn't happen in the web process) — make a
        # one-shot pool rather than dropping the job.
        from arq import create_pool
        from arq.connections import RedisSettings

        pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
        try:
            await pool.enqueue_job(job_name, *args)
        finally:
            await pool.aclose()
        return

    await _pool.enqueue_job(job_name, *args)
