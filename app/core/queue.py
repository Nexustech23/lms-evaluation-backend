# ============================================================
# Task queue (Perf Phase 2).
#
# The AI / PDF jobs used to run via FastAPI BackgroundTasks — i.e. *inside
# the web process*. Phase 2 added a Redis queue (arq) processed by a
# separate `worker` container. The job *bodies* are unchanged — only the
# dispatch mechanism moved. Client contract is identical (POST returns a
# job_id, poll job-status).
#
# QUEUE_MODE:
#   "inline" (default) — run the job in THIS process, after the response is
#              sent. When the caller passes its request `BackgroundTasks`,
#              the job is scheduled via Starlette's BackgroundTasks (the
#              exact pre-Phase-2 mechanism — uvicorn awaits it as part of
#              the response lifecycle). Otherwise it falls back to a bare
#              asyncio.create_task. enqueue() returns immediately either
#              way. No worker container needed. A web-process restart
#              mid-job loses that job (no retry) — acceptable here.
#   "inline_sync" — run the job inline and AWAIT it before returning. Only
#              for the test suite, which POSTs a job then immediately polls
#              its status and expects it finished.
#   "redis"  — enqueue to arq; a running lms-worker container runs the job
#              off the request path. Opt in via .env + the compose "queue"
#              profile when you want jobs off the web tier.
# ============================================================
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger("queue")

_pool: Any = None

# Hold strong refs to in-flight inline tasks so the event loop can't
# garbage-collect a task before it finishes (see asyncio.create_task docs).
_pending: set[asyncio.Task] = set()

_INLINE_MODES = {"inline", "inline_sync"}


async def connect_to_queue() -> None:
    """Called from the web app's lifespan. No-op unless QUEUE_MODE=redis."""
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


async def _run_inline(job_name: str, args: tuple) -> None:
    from app.worker import JOB_REGISTRY

    try:
        await JOB_REGISTRY[job_name](None, *args)  # ctx=None
    except Exception:
        # Job bodies catch their own errors and write a "failed" status; this
        # only fires on something truly unexpected. Log it — a bare
        # create_task would swallow it silently.
        logger.exception("inline job %r crashed", job_name)


async def enqueue(job_name: str, *args: Any, background_tasks: Any = None) -> None:
    """Dispatch a background job. Returns as soon as the job is scheduled —
    it does NOT wait for the job to finish (except in QUEUE_MODE=inline_sync,
    used by tests).

    Pass the request's `BackgroundTasks` as `background_tasks` in inline mode
    so the job runs through Starlette's proven response-lifecycle mechanism
    rather than a bare asyncio task.
    """
    mode = settings.QUEUE_MODE

    if mode in _INLINE_MODES:
        if mode == "inline_sync":
            await _run_inline(job_name, args)
        elif background_tasks is not None:
            background_tasks.add_task(_run_inline, job_name, args)
        else:
            task = asyncio.create_task(_run_inline(job_name, args))
            _pending.add(task)
            task.add_done_callback(_pending.discard)
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
