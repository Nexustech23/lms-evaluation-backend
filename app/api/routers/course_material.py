# ============================================================
# COURSE MATERIAL ROUTER (Phase 9 — RAG ingestion)
#
# New in the FastAPI port — this is the write path for the hybrid RAG layer
# (app/services/rag/): faculty/institute upload a course document (syllabus,
# course profile, textbook), it gets classified STRUCTURED (tree-indexed) or
# UNSTRUCTURED (chunked + embedded into Qdrant), and the resulting doc_id can
# then be used to ground AI roadmap generation for a matching subject name
# (see app/api/routers/roadmap.py).
#
# Distinct from faculty_materials.py, which is plain file-metadata storage
# with no text extraction/indexing — that router's behavior is unchanged by
# this addition.
#
# Follows this codebase's established async-job convention: Redis-backed
# job_store.py + BackgroundTasks (same pattern as roadmap.py/ai_tutor.py),
# rather than the Flask prototype's in-memory dict.
# ============================================================

import asyncio
import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import FACULTY, INSTITUTE, get_current_identity
from app.core.rate_limit import ai_rate_limit
from app.db.mongodb import get_database
from app.models.ai_usage_event import Feature, Provider
from app.services.ai_usage import record_ai_usage
from app.services.gemini import extract_text_from_file
from app.services.job_store import get_job, set_job, update_job
from app.services.rag import mongo_store, singletons, structure_parser, tree_index
from app.services.rag.pdf_extract import extract_pdf_text
from app.services.rag.schemas import DocType, DocumentRecord, SourceFormat, new_id
from app.services.rag.vector_store import chunk_text

router = APIRouter(prefix="/api/course-material", dependencies=[Depends(get_current_identity)], tags=["course-material"])

logger = logging.getLogger(__name__)

CM_JOB_PREFIX = "course_material_job:"

_EXT_TO_FORMAT = {
    "pdf": SourceFormat.PDF, "docx": SourceFormat.DOCX, "doc": SourceFormat.DOCX,
    "md": SourceFormat.MD, "txt": SourceFormat.TXT,
}


def _file_ext(filename: str) -> str:
    return (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()


# ============================================================
# BACKGROUND JOB — INGEST (extract -> classify -> tree/vector index)
# ============================================================

async def _run_ingest_job(
    job_id: str, file_bytes: bytes, filename: str,
    course_title: Optional[str], course_code: Optional[str], user_id: str,
    job_prefix: str = CM_JOB_PREFIX,
) -> None:
    """job_prefix defaults to this router's own CM_JOB_PREFIX but is
    overridable — self_learner_course_material.py reuses this exact
    pipeline under its own SL_CM_JOB_PREFIX namespace so a self-learner's
    upload status poll can never cross-resolve against an institute job."""
    db = get_database()
    try:
        await update_job(job_prefix, job_id, {"step": "Checking for duplicates…"})

        content_hash = hashlib.sha256(file_bytes).hexdigest()
        existing = await mongo_store.find_document_by_hash(db, content_hash)
        if existing:
            logger.info("course_material ingest: doc_id=%s is a duplicate of existing doc_id=%s (hash match)",
                        job_id, existing.id)
            # Dedup is global (identity is the file's bytes), so the match may
            # well belong to someone else. Uploading the file is what grants
            # access — the uploader plainly holds it — and reusing the indexed
            # copy skips a whole parse/summarize/embed pass.
            await mongo_store.add_document_owner(db, existing.id, user_id)
            await update_job(job_prefix, job_id, {
                "status": "done", "doc_id": existing.id, "duplicate": True, "step": "Already indexed",
            })
            return

        await update_job(job_prefix, job_id, {"step": "Extracting text…"})

        ext = _file_ext(filename)
        source_format = _EXT_TO_FORMAT.get(ext, SourceFormat.TXT)

        if ext == "pdf":
            # Local-first: pdfplumber pulls the text layer straight out of
            # the PDF, no LLM call, no token limit — covers the large
            # majority of uploads (typed syllabi, exported docs, digital
            # textbooks). Gemini OCR is only invoked for pages that come
            # back empty (scanned/image-only), and even then batched a few
            # pages at a time so no single call risks Gemini's 65,536-token
            # output cap regardless of total document length.
            text, extract_usage, extract_truncated = await asyncio.to_thread(extract_pdf_text, file_bytes)
            await record_ai_usage(
                db, user_id=user_id, provider=Provider.GEMINI, model="gemini-2.5-flash",
                feature=Feature.RAG_INGEST_EXTRACTION, usage=extract_usage, job_id=job_id,
            )
            if extract_truncated:
                logger.warning(
                    "course_material ingest: doc_id=%s OCR extraction hit Gemini's output-token cap on at "
                    "least one page batch — extracted text for those pages may be incomplete", job_id,
                )
        else:
            text = await asyncio.to_thread(extract_text_from_file, file_bytes, filename)

        if not text or not text.strip():
            await update_job(job_prefix, job_id, {
                "status": "error", "error": "No text could be extracted from this file.",
            })
            return

        # No true per-page-with-tables extraction utility exists on the
        # FastAPI side (this backend's Gemini/docx extractors return one
        # flat string, not paginated dicts) — treat the whole document as a
        # single page. Fine for the small structured course-outline
        # documents this feature targets; heading-density classification
        # still works at document granularity.
        pages = [{"text": text, "page_num": 1, "tables": []}]

        doc_id = new_id()
        doc_type = structure_parser.classify_doc_type(pages)
        await update_job(job_prefix, job_id, {"step": f"Indexing as {doc_type.value}…"})

        if doc_type == DocType.STRUCTURED:
            nodes = structure_parser.build_tree(pages, doc_id)
            await tree_index.summarize_nodes(nodes, db=db, user_id=user_id)
            await mongo_store.save_tree(db, doc_id, nodes)
        else:
            store = await asyncio.to_thread(singletons.get_vector_store)
            if store is None:
                await update_job(job_prefix, job_id, {
                    "status": "error",
                    "error": "Vector store (Qdrant) is unavailable — start it and try again, or upload a "
                             "more structured document (with numbered headings) to use the tree-index path instead.",
                })
                return
            chunks = chunk_text(doc_id, pages)
            embed_usage = await asyncio.to_thread(store.upsert, chunks)
            await record_ai_usage(
                db, user_id=user_id, provider=Provider.GEMINI, model="gemini-embedding-001",
                feature=Feature.RAG_EMBEDDING, usage=embed_usage, job_id=job_id,
            )

        record = DocumentRecord(
            id=doc_id, filename=filename, source_format=source_format, doc_type=doc_type,
            course_code=course_code, course_title=course_title, content_hash=content_hash,
            owner_user_ids=[user_id],
        )
        await mongo_store.save_document_record(db, record)

        logger.info("course_material ingest done: doc_id=%s doc_type=%s course_title=%r",
                    doc_id, doc_type.value, course_title)
        await update_job(job_prefix, job_id, {
            "status": "done", "doc_id": doc_id, "doc_type": doc_type.value, "duplicate": False, "step": "Done",
        })

    except Exception as e:
        logger.error("course_material ingest job %s failed: %s", job_id, e, exc_info=True)
        await update_job(job_prefix, job_id, {
            "status": "error", "error": "Internal server error during course material indexing.",
        })


# ============================================================
# ROUTES
# ============================================================

@router.get("/status/{job_id}")
async def get_ingest_status(job_id: str, identity: dict = Depends(get_current_identity)):
    job = await get_job(CM_JOB_PREFIX, job_id)
    if job is None or job.get("user_id") != identity["user_id"]:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/upload", dependencies=[Depends(ai_rate_limit)])
async def upload_course_material(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    course_title: Optional[str] = Form(None),
    course_code: Optional[str] = Form(None),
    identity: dict = Depends(get_current_identity),
):
    if identity.get("role") not in (INSTITUTE, FACULTY):
        raise HTTPException(status_code=403, detail="Only institute admin or faculty can upload course material")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Empty file")

    job_id = str(uuid.uuid4())
    await set_job(CM_JOB_PREFIX, job_id, {
        "status": "processing", "step": "Starting…", "user_id": identity["user_id"],
    })

    background_tasks.add_task(
        _run_ingest_job, job_id, file_bytes, file.filename or "upload",
        course_title, course_code, identity["user_id"],
    )

    return JSONResponse(status_code=202, content={"job_id": job_id, "status": "processing"})


@router.get("")
async def list_course_materials(
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    # Scoped to the caller. This previously listed every document ever
    # uploaded by anyone — filename, course title and code included — to any
    # authenticated user, across institutes and self-learners alike.
    # Documents predating ownership carry no owner_user_ids and so match
    # nobody; see scripts/backfill_course_material_owners.py.
    cursor = db.courseMaterials.find({"owner_user_ids": identity["user_id"]})
    docs = [d async for d in cursor.sort("created_at", -1).limit(200)]
    return [
        {
            "id": d["_id"],
            "filename": d.get("filename"),
            "course_title": d.get("course_title"),
            "course_code": d.get("course_code"),
            "doc_type": d.get("doc_type"),
            "source_format": d.get("source_format"),
            "created_at": d.get("created_at").isoformat() if d.get("created_at") else None,
        }
        for d in docs
    ]
