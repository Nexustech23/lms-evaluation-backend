# ============================================================
# Task-queue worker (Perf Phase 2).
#
# Runs as a SEPARATE process from the web server:
#     arq app.worker.WorkerSettings
# (docker-compose service `worker`; same image and env as the web
# container). The AI / PDF / transcript jobs that used to run inside the
# web process via FastAPI BackgroundTasks are now enqueued to Redis (see
# app/core/queue.py) and executed here.
#
# ── Accuracy guarantee ──────────────────────────────────────
# The job *bodies* are the exact same functions as before — imported
# unchanged from the routers. This module only supplies thin `run_*(ctx,
# ...)` wrappers that arq can call: they re-hydrate `db` from
# get_database() (the worker's own Mongo client, opened in _on_startup)
# and convert the few id args that cross Redis as strings back to
# ObjectId. No marking / grading / CGPA / CO-PO / transcript logic lives
# here or changes.
#
# max_tries = 1: these jobs bill external AI calls and write marks — a
# retry would double-count token usage and re-run a grading pass. This
# matches the old BackgroundTasks behaviour (no retry).
# ============================================================
from __future__ import annotations

import asyncio
import logging
import sys

from bson import ObjectId

from app.core.config import settings

logger = logging.getLogger("worker")


# ── Job wrappers ────────────────────────────────────────────
# One per enqueue() name. Each imports its body lazily to keep worker
# import cheap and avoid any import cycle (routers import app.core.queue).

async def run_homework_job(ctx, job_id, params, file_bytes, filename, user_id):
    from app.api.routers.ai_tutor import _run_homework_job

    await _run_homework_job(job_id, params, file_bytes, filename, user_id)


async def run_notes_job(ctx, job_id, params, file_bytes, filename, user_id):
    from app.api.routers.ai_tutor import _run_notes_job

    await _run_notes_job(job_id, params, file_bytes, filename, user_id)


async def run_ingest_job(ctx, job_id, file_bytes, filename, course_title, course_code, user_id, job_prefix):
    from app.api.routers.course_material import _run_ingest_job

    await _run_ingest_job(job_id, file_bytes, filename, course_title, course_code, user_id, job_prefix)


async def run_extract_question_paper_text(ctx, folder_id, question_paper_url, faculty_id, filename):
    from app.db.mongodb import get_database
    from app.services.gemini import extract_and_patch_question_paper_text

    await extract_and_patch_question_paper_text(
        get_database(), ObjectId(folder_id), question_paper_url, faculty_id, filename
    )


async def run_evaluation_job(ctx, job_id, exam_id, answer_id, generate_transcript_pdf, faculty_id):
    from app.api.routers.grading import _run_evaluation_job

    await _run_evaluation_job(job_id, exam_id, answer_id, generate_transcript_pdf, faculty_id)


async def run_mock_generation(ctx, test_id, prompt, user_id):
    from app.api.routers.mock_tests import _run_generation

    await _run_generation(ObjectId(test_id), prompt, user_id)


async def run_roadmap_mock_generation(ctx, test_id, prompt, user_id, grounded):
    from app.api.routers.mock_tests import _run_roadmap_generation

    await _run_roadmap_generation(ObjectId(test_id), prompt, user_id, grounded)


async def run_question_paper_generation(ctx, job_id, params, file_bytes):
    from app.api.routers.question_paper import _run_generation_job

    await _run_generation_job(job_id, params, file_bytes)


async def run_create_roadmap(
    ctx, job_id, user_id, subject, goal, skill_level,
    daily_study_time, revision_frequency, assessment_score, doc_id, custom_instruction,
):
    from app.api.routers.roadmap import _run_create_roadmap_job

    await _run_create_roadmap_job(
        job_id, user_id, subject, goal, skill_level,
        daily_study_time, revision_frequency, assessment_score, doc_id, custom_instruction,
    )


async def run_refresh_transcript(ctx, exam_id):
    from app.db.mongodb import get_database
    from app.utils.transcript_generation_helper import refresh_transcript_for_exam

    # refresh_transcript_for_exam already accepts a str or ObjectId and never raises.
    await refresh_transcript_for_exam(get_database(), exam_id)


_FUNCTIONS = [
    run_homework_job,
    run_notes_job,
    run_ingest_job,
    run_extract_question_paper_text,
    run_evaluation_job,
    run_mock_generation,
    run_roadmap_mock_generation,
    run_question_paper_generation,
    run_create_roadmap,
    run_refresh_transcript,
]

# Used by app/core/queue.py in QUEUE_MODE="inline" (the default, and the
# test suite) to run a job body directly in-process instead of shipping it
# to Redis. This module's WorkerSettings is only loaded when a lms-worker
# container actually runs (QUEUE_MODE="redis").
JOB_REGISTRY = {fn.__name__: fn for fn in _FUNCTIONS}


# ── arq worker lifecycle ────────────────────────────────────

async def _on_startup(ctx) -> None:
    # Mirror app/main.py: on Windows, flip the policy back to Proactor so
    # Playwright (PDF render) and libreoffice subprocesses can spawn.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    from concurrent.futures import ThreadPoolExecutor

    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=settings.THREAD_POOL_WORKERS, thread_name_prefix="blk")
    )

    from app.core.observability import install_slow_query_logging
    from app.core.redis_client import connect_to_redis
    from app.db.mongodb import connect_to_mongo, ping_mongo

    install_slow_query_logging()
    connect_to_mongo()
    await ping_mongo()
    connect_to_redis()
    logger.info("arq worker started — Mongo + Redis connected")


async def _on_shutdown(ctx) -> None:
    from app.core.redis_client import close_redis_connection
    from app.db.mongodb import close_mongo_connection

    await close_redis_connection()
    close_mongo_connection()


def _redis_settings():
    from arq.connections import RedisSettings

    return RedisSettings.from_dsn(settings.REDIS_URL)


class WorkerSettings:
    functions = _FUNCTIONS
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    redis_settings = _redis_settings()
    max_jobs = settings.ARQ_MAX_JOBS
    job_timeout = settings.ARQ_JOB_TIMEOUT_SECONDS
    keep_result = 3600
    # Jobs bill AI calls and write marks — never auto-retry (matches the old
    # BackgroundTasks behaviour and the zero-accuracy-risk constraint).
    max_tries = 1
