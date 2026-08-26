# ============================================================
# SELF-LEARNER COURSE MATERIAL ROUTER
#
# The self-learner equivalent of app/api/routers/course_material.py's
# /upload — same ingestion pipeline (hash-dedup -> extract -> classify
# STRUCTURED/UNSTRUCTURED -> tree-index or chunk+embed), reused directly via
# _run_ingest_job rather than duplicated, but WITHOUT that router's
# institute/faculty role gate: any authenticated self-learner can ground
# their own roadmap in a syllabus/textbook they upload at creation time
# (see roadmap/create's "Ground it in your own material" step).
#
# Mounted at /api/self-learner/course-material — one of the two
# prefix-preserving rewrite rules in next.config.mjs (the other being
# /api/self-learner/roadmap), since this router's own url_prefix already
# includes "/api".
# ============================================================

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from app.api.deps import get_current_identity, require_mycareerguru_access
from app.api.routers.course_material import _run_ingest_job
from app.core.rate_limit import ai_rate_limit
from app.services.job_store import get_job, set_job

router = APIRouter(
    prefix="/api/self-learner/course-material",
    dependencies=[Depends(get_current_identity), Depends(require_mycareerguru_access)],
    tags=["self-learner-course-material"],
)

logger = logging.getLogger(__name__)

# Separate job-id prefix from the institute router's CM_JOB_PREFIX — same
# Redis-backed job_store, just a distinct namespace so status polling can't
# ever cross-resolve a self-learner's job against an institute upload's id.
SL_CM_JOB_PREFIX = "self_learner_course_material_job:"


@router.get("/status/{job_id}")
async def get_upload_status(job_id: str, identity: dict = Depends(get_current_identity)):
    job = await get_job(SL_CM_JOB_PREFIX, job_id)
    if job is None or job.get("user_id") != identity["user_id"]:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("", dependencies=[Depends(ai_rate_limit)])
async def upload_course_material(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    course_title: Optional[str] = Form(None),
    course_code: Optional[str] = Form(None),
    identity: dict = Depends(get_current_identity),
):
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Empty file")

    job_id = str(uuid.uuid4())
    await set_job(SL_CM_JOB_PREFIX, job_id, {
        "status": "processing", "step": "Starting…", "user_id": identity["user_id"],
    })

    background_tasks.add_task(
        _run_ingest_job, job_id, file_bytes, file.filename or "upload",
        file.content_type or "", course_title, course_code, identity["user_id"],
        SL_CM_JOB_PREFIX,
    )

    logger.info(
        "self-learner course material upload queued: job_id=%s filename=%r user_id=%s",
        job_id, file.filename, identity["user_id"],
    )
    return JSONResponse(status_code=202, content={"job_id": job_id, "status": "processing"})
