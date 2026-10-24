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
import time
from typing import Any

from app.core.config import settings
from app.core.diagnostics import context, event

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


async def _run_inline(job_name: str, args: tuple, trace=None) -> None:
    from app.worker import JOB_REGISTRY

    try:
        await JOB_REGISTRY[job_name](None, *args, _lms_trace=trace)  # ctx=None
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
    trace = context()
    trace["dispatch_epoch"] = time.time()
    event("job.dispatch", job_kind=job_name, queue_mode=mode, job_id=str(args[0]) if args else None)

    if mode in _INLINE_MODES:
        if mode == "inline_sync":
            event("job.enqueue_accepted",job_kind=job_name,job_id=str(args[0]) if args else None,queue_mode=mode,acceptance="inline_execution")
            await _run_inline(job_name, args, trace)
        elif background_tasks is not None:
            background_tasks.add_task(_run_inline, job_name, args, trace)
            event("job.enqueue_accepted",job_kind=job_name,job_id=str(args[0]) if args else None,queue_mode=mode,acceptance="background_task_registered")
        else:
            task = asyncio.create_task(_run_inline(job_name, args, trace))
            _pending.add(task)
            task.add_done_callback(_pending.discard)
            event("job.enqueue_accepted",job_kind=job_name,job_id=str(args[0]) if args else None,queue_mode=mode,acceptance="async_task_created")
        return

    if _pool is None:
        # Lifespan didn't run (shouldn't happen in the web process) — make a
        # one-shot pool rather than dropping the job.
        from arq import create_pool
        from arq.connections import RedisSettings

        try:
            pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
        except Exception as exc:
            event('job.enqueue_not_accepted',job_kind=job_name,job_id=str(args[0]) if args else None,
                  queue_mode='redis',reason='pool_unavailable',error_type=type(exc).__name__)
            raise
        try:
            await _enqueue_redis(pool, job_name, args, trace)
        finally:
            await pool.aclose()
        return

    await _enqueue_redis(_pool, job_name, args, trace)


async def _enqueue_redis(pool,job_name,args,trace):
    identity=str(args[0]) if args else None
    try:
        queued=await pool.enqueue_job(job_name,*args,_lms_trace=trace)
    except BaseException as exc:
        event("job.enqueue_unconfirmed",job_kind=job_name,job_id=identity,queue_mode="redis",
              error_type=type(exc).__name__,exc_info=True)
        raise
    arq_id=getattr(queued,'job_id',None)
    event("job.enqueue_accepted" if queued is not None else "job.enqueue_not_accepted",
          job_kind=job_name,job_id=identity,queue_mode="redis",
          queue_job_id=arq_id if isinstance(arq_id,str) else None,
          acceptance="redis_acknowledged" if queued is not None else "arq_job_id_already_exists")
