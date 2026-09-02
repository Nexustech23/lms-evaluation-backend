import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app.api.routers import (
    ai_tutor,
    answers,
    auth,
    contact,
    course_material,
    evaluation,
    exams,
    faculty_materials,
    grading,
    institute_hierarchy,
    marks_import,
    mock_tests,
    mycareerguru_auth,
    profile,
    question_paper,
    relative_grading,
    roadmap,
    roles,
    self_learner_analytics,
    self_learner_course_material,
    student_subjects,
    subject_results,
    transcripts,
)
from app.core.config import settings
from app.core.observability import RequestTimingMiddleware, install_slow_query_logging
from app.core.rate_limit import GlobalRateLimitMiddleware
from app.core.redis_client import close_redis_connection, connect_to_redis
from app.db.indexes import ensure_indexes
from app.db.mongodb import close_mongo_connection, connect_to_mongo, get_database, ping_mongo
from app.services.ai_usage import ensure_ai_usage_indexes

logging.basicConfig(level=logging.INFO)
logging.getLogger("pymongo").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # uvicorn forces WindowsSelectorEventLoopPolicy on Windows (its default
    # asyncio loop backend, since uvloop isn't available there), which has no
    # subprocess transport — any code that spawns a subprocess raises
    # NotImplementedError. Playwright (used for PDF rendering) needs one to
    # launch its browser. Overriding the policy back to Proactor here — after
    # uvicorn's own startup has already set Selector and started its main
    # loop — doesn't touch that already-running loop, but does mean any NEW
    # loop created later (which is exactly what Playwright's sync API spins
    # up internally, per-call, in its own thread) picks up Proactor instead.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    # Blocking SDK calls (Gemini/Claude/Playwright/requests) go through
    # asyncio.to_thread; the default executor is only cpu+4 threads, which
    # serialises them under any concurrency. Size it explicitly.
    from concurrent.futures import ThreadPoolExecutor

    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=settings.THREAD_POOL_WORKERS, thread_name_prefix="blk")
    )

    # Must register the command listener BEFORE the Mongo client is created.
    install_slow_query_logging()
    connect_to_mongo()
    await ping_mongo()
    await ensure_ai_usage_indexes(get_database())
    await ensure_indexes(get_database())
    connect_to_redis()
    yield
    await close_redis_connection()
    close_mongo_connection()


app = FastAPI(title="LMS Evaluation API", lifespan=lifespan)

# Compress large JSON responses (CO/PO matrices, transcript lists,
# filter-data, result exports). Registered innermost so it compresses the
# final body after every other middleware has run.
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Registered before CORSMiddleware so CORS ends up as the outer layer (FastAPI
# wraps middleware in reverse registration order) — otherwise a 429 response
# short-circuited by GlobalRateLimitMiddleware would go out with no CORS
# headers, and the browser would block it before the frontend ever saw it.
app.add_middleware(GlobalRateLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Registered last => outermost => measures total wall-clock per request,
# including every other middleware. Adds an X-Response-Time-ms header.
app.add_middleware(RequestTimingMiddleware)


# FastAPI's HTTPException(detail=...) natively produces {"detail": ...}, but
# the Flask backend (and most of the frontend's error-reading code, e.g.
# `err?.response?.data?.error`) expects {"error": ...}. Rather than touching
# every one of the ~340 raise HTTPException(...) call sites across the
# routers, add both keys here in one place so existing frontend error
# handling keeps showing the real backend message instead of falling back
# to generic hardcoded text. Endpoints that already build their own
# JSONResponse with an "error" key (e.g. ai_tutor.py) bypass this handler
# entirely and are unaffected.
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict):
        body = dict(exc.detail)
        # Preserve the real payload even when it uses none of the recognized
        # keys, instead of discarding it behind a hardcoded generic message.
        body.setdefault(
            "error",
            body.get("error") or body.get("detail") or body.get("message") or json.dumps(exc.detail),
        )
        body.setdefault("detail", exc.detail)
    else:
        body = {"detail": exc.detail, "error": exc.detail}

    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)

# All routers are mounted at root (no /api prefix) to match the Flask
# backend's URL layout, so the existing Next.js frontend could point at
# this backend later without changes.
app.include_router(auth.router)
app.include_router(roles.router)
app.include_router(profile.router)
app.include_router(contact.router)
app.include_router(institute_hierarchy.router)
app.include_router(exams.router)
app.include_router(evaluation.router)
app.include_router(answers.router)
app.include_router(ai_tutor.router)
app.include_router(question_paper.router)
app.include_router(grading.router)
app.include_router(marks_import.router)
app.include_router(mock_tests.router)
app.include_router(mycareerguru_auth.router)
app.include_router(relative_grading.router)
app.include_router(roadmap.router)
app.include_router(self_learner_analytics.router)
app.include_router(subject_results.router)
app.include_router(faculty_materials.router)
app.include_router(course_material.router)
app.include_router(self_learner_course_material.router)
app.include_router(student_subjects.router)
app.include_router(transcripts.router)


@app.get("/")
async def index():
    return "Server is running - Claude-Generated Transcripts + Choice-Based Marking"


@app.get("/health")
async def health():
    try:
        db = get_database()
        await db.command("ping")
        database_status = "connected"
    except Exception:
        database_status = "disconnected"

    return {
        "status": "healthy",
        "database": database_status,
        "imagekit": "configured" if settings.IMAGEKIT_PUBLIC_KEY and settings.IMAGEKIT_PRIVATE_KEY else "not configured",
        "ai_models": {"gemini": "gemini-2.5-flash", "claude": "claude-sonnet-4-6"},
        "collections": {
            "newsaved": "newsavedDocs",
            "answer": "answerDetails",
            "evaluation": "evaluationDetails",
        },
    }
