# ============================================================
# AI ANSWER-SCRIPT GRADING ROUTER
# Ported from the QuizGradingAssistant pipeline embedded in Flask's
# server.py (lines 104-1490) — the platform's core AI grading trigger.
#
# Deviation from Flask: adds a faculty-ownership check (exam.faculty_id ==
# caller) as another pre-flight validation step alongside the existing ones
# — Flask's version only checked document existence/consistency, not that
# the caller owns the exam. Also uses the Playwright renderer already built
# in Phase 3a instead of adding wkhtmltopdf/pdfkit as a second PDF engine
# (confirmed with the user).
# ============================================================

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity, get_current_user_and_faculty_details
from app.core.queue import enqueue
from app.core.rate_limit import bulk_grading_rate_limit
from app.db.mongodb import get_database
from app.utils.net import safe_get
from app.schemas.grading import EvaluateAnswerScriptRequest
from app.services.grading import (
    extract_answer_text_with_gemini,
    generate_evaluation_report_html,
    generate_transcript_html_with_claude,
    grade_with_claude,
    safe_json_parse,
)
from app.services.imagekit import upload_file_to_imagekit
from app.services.job_store import get_job, set_job, update_job
from app.services.pdf_render import render_html_to_pdf
from app.utils.token_usage import aggregate_grading_tokens, check_institute_token_budget, save_grading_tokens_to_institute
from app.utils.transcript_generation_helper import refresh_transcript_for_exam

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["grading"])

EVAL_JOB_PREFIX = "job:"


def _download_pdf(url: str) -> bytes:
    # SSRF-checked: rejects internal / metadata addresses and redirects.
    return safe_get(url, timeout=60)


async def _resolve_institute_id(db: AsyncIOMotorDatabase, exam: dict) -> ObjectId:
    school_id = exam.get("school_id")
    if not school_id:
        raise ValueError("school_id missing in folder")
    if isinstance(school_id, str) and ObjectId.is_valid(school_id):
        school_id = ObjectId(school_id)
    school = await db["schoolDetails"].find_one({"_id": school_id})
    if not school:
        raise ValueError("School not found")
    institute_id = school.get("institute_id")
    if not institute_id:
        raise ValueError("institute_id missing in school")
    return institute_id


async def _fail(job_id: str, message: str) -> None:
    await set_job(EVAL_JOB_PREFIX, job_id, {"status": "error", "message": message})


# ============================================================
# BACKGROUND JOB
# ============================================================

async def _run_evaluation_job(
    job_id: str, exam_id: str, answer_id: str, generate_transcript_pdf: bool, faculty_id: str
) -> None:
    db = get_database()

    try:
        if not ObjectId.is_valid(exam_id) or not ObjectId.is_valid(answer_id):
            await _fail(job_id, "Invalid folderId or answerId")
            return

        exam_object_id = ObjectId(exam_id)
        answer_object_id = ObjectId(answer_id)

        await set_job(EVAL_JOB_PREFIX, job_id, {"status": "processing", "progress": 5, "step": "Fetching exam data"})

        exam = await db["newsavedDocs"].find_one({"_id": exam_object_id})
        if not exam:
            await _fail(job_id, "Folder not found")
            return

        if exam.get("faculty_id") != ObjectId(faculty_id):
            await _fail(job_id, "You are not authorized to evaluate this exam")
            return

        answer = await db["answerDetails"].find_one({"_id": answer_object_id})
        if not answer:
            await _fail(job_id, "Answer script not found")
            return

        if answer.get("exam_id") != exam_object_id:
            await _fail(job_id, "Answer script does not belong to this folder")
            return

        student_pdf_url = answer.get("answer_script_url")
        if not student_pdf_url:
            await _fail(job_id, "Missing student PDF URL")
            return

        question_text = (exam.get("question_paper") or {}).get("text")
        if not question_text:
            await _fail(
                job_id,
                "Question paper text has not been extracted yet. Please wait for background "
                "extraction to complete or re-upload the question paper.",
            )
            return

        evaluation_doc = await db["evaluationDetails"].find_one({"exam_id": exam_object_id})
        evaluation_rules = (evaluation_doc or {}).get("questionEvaluationDetails", [])
        if not evaluation_rules:
            await _fail(job_id, "No evaluation rules found for this exam")
            return

        institute_id = await _resolve_institute_id(db, exam)

        # Step 2 — download
        await set_job(EVAL_JOB_PREFIX, job_id, {
            "status": "processing", "progress": 15, "step": "Downloading student answer PDF",
        })
        student_pdf_bytes = await asyncio.to_thread(_download_pdf, student_pdf_url)

        # Step 3 — OCR
        await update_job(EVAL_JOB_PREFIX, job_id, {"progress": 30, "step": "Extracting student answer text"})
        answer_text, ans_gemini_tokens = await asyncio.to_thread(extract_answer_text_with_gemini, student_pdf_bytes)
        gemini_calls = [ans_gemini_tokens]

        # Step 4 — grading (+ transcript, in parallel when requested)
        await update_job(EVAL_JOB_PREFIX, job_id, {"progress": 50, "step": "AI grading in progress"})
        student_name = answer.get("student_name") or "Student"

        if generate_transcript_pdf:
            (grading_result_raw, grade_claude_tokens), (transcript_html, transcript_claude_tokens) = await asyncio.gather(
                asyncio.to_thread(grade_with_claude, question_text, answer_text, evaluation_rules),
                asyncio.to_thread(generate_transcript_html_with_claude, answer_text, student_name),
            )
            claude_calls = [grade_claude_tokens, transcript_claude_tokens]
        else:
            grading_result_raw, grade_claude_tokens = await asyncio.to_thread(
                grade_with_claude, question_text, answer_text, evaluation_rules
            )
            transcript_html = None
            claude_calls = [grade_claude_tokens]

        grading_json = safe_json_parse(grading_result_raw)

        # Step 5 — recompute marks server-side (never trust Claude's self-reported totals)
        await update_job(EVAL_JOB_PREFIX, job_id, {"progress": 65, "step": "Computing marks"})
        questionwise_marking = grading_json.get("questionwise_marking", [])
        total_ai_marks = round(sum(float(q.get("ai_awarded_marks", 0)) for q in questionwise_marking), 2)
        grading_json.setdefault("summary", {})
        grading_json["summary"]["total_ai_marks"] = total_ai_marks

        eval_total_marks = (evaluation_doc or {}).get("totalMarks")
        total_max_marks = (
            eval_total_marks if eval_total_marks
            else round(sum(r.get("maxMarks", 0) for r in evaluation_rules), 2)
        )

        # Step 6 — evaluation report HTML (pure Python, no LLM)
        await update_job(EVAL_JOB_PREFIX, job_id, {"progress": 70, "step": "Generating evaluation report"})
        evaluation_html = generate_evaluation_report_html(grading_json, student_name, total_max_marks=total_max_marks)

        # Step 7 — render PDFs (Playwright, in parallel when both are needed)
        await update_job(EVAL_JOB_PREFIX, job_id, {"progress": 78, "step": "Rendering PDFs"})
        if generate_transcript_pdf:
            transcript_pdf_binary, evaluation_pdf_binary = await asyncio.gather(
                asyncio.to_thread(render_html_to_pdf, transcript_html),
                asyncio.to_thread(render_html_to_pdf, evaluation_html),
            )
        else:
            evaluation_pdf_binary = await asyncio.to_thread(render_html_to_pdf, evaluation_html)
            transcript_pdf_binary = None

        # Step 8 — upload to ImageKit (in parallel when both are needed)
        await update_job(EVAL_JOB_PREFIX, job_id, {"progress": 88, "step": "Uploading reports"})
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        if generate_transcript_pdf:
            transcript_upload, evaluation_upload = await asyncio.gather(
                asyncio.to_thread(
                    upload_file_to_imagekit, transcript_pdf_binary, f"transcript_{answer_id}_{ts}.pdf",
                    "/transcripts", ["transcript", str(answer_id)],
                ),
                asyncio.to_thread(
                    upload_file_to_imagekit, evaluation_pdf_binary, f"evaluated_{answer_id}_{ts}.pdf",
                    "/evaluated-reports", ["evaluated", "report", str(answer_id)],
                ),
            )
        else:
            evaluation_upload = await asyncio.to_thread(
                upload_file_to_imagekit, evaluation_pdf_binary, f"evaluated_{answer_id}_{ts}.pdf",
                "/evaluated-reports", ["evaluated", "report", str(answer_id)],
            )
            transcript_upload = {"url": None, "file_id": None}

        # Step 9 — CO results per question (positional rubric lookup, 1-indexed)
        await update_job(EVAL_JOB_PREFIX, job_id, {"progress": 93, "step": "Saving results"})
        co_results_per_question = []
        for q in questionwise_marking:
            cos = q.get("cos")
            if not cos:
                continue
            unanswered = bool((q.get("flags") or {}).get("unanswered"))
            co_entries = [
                {
                    "co_code": c.get("co_code"),
                    "ai_marks": 0 if unanswered else c.get("ai_marks", 0),
                    "max_marks": 0 if unanswered else c.get("max_marks", 0),
                }
                for c in cos
            ]
            co_results_per_question.append({"question_no": q.get("question_no"), "cos": co_entries})

        # Step 10 — token aggregation
        token_usage = aggregate_grading_tokens(gemini_calls, claude_calls)

        # Step 11 — persist to answerDetails
        now = datetime.now(timezone.utc)
        await db["answerDetails"].update_one(
            {"_id": answer_object_id},
            {"$set": {
                "evaluated_report_url": evaluation_upload["url"],
                "evaluated_report_fileId": evaluation_upload["file_id"],
                "html_content": transcript_upload["url"],
                "transcript_pdf_fileId": transcript_upload["file_id"],
                "questionwise_marking": questionwise_marking,
                "total_ai_marks": total_ai_marks,
                "total_max_marks": total_max_marks,
                "total_final_marks": total_ai_marks,
                "co": co_results_per_question,
                "reviewed_by_professor": False,
                "evaluated_at": now,
                "updated_at": now,
                "token_usage": {**token_usage, "transcript_generated": generate_transcript_pdf},
            }},
        )

        # Already running as a background job, so just await this directly —
        # if this subject's batch/semester already has a generated academic
        # transcript, keep it in sync with the marks that were just saved.
        await refresh_transcript_for_exam(db, exam_object_id)

        # Step 12 — institute token counters (best-effort)
        await save_grading_tokens_to_institute(db, institute_id, token_usage)

        # Step 13 — final job payload
        await set_job(EVAL_JOB_PREFIX, job_id, {
            "status": "done",
            "progress": 100,
            "step": "Completed",
            "result": {
                "success": True,
                "message": "Answer script evaluated successfully",
                "answer_id": answer_id,
                "filename": answer.get("filename", "Result.pdf"),
                "evaluated_report_url": evaluation_upload["url"],
                "evaluated_report_fileId": evaluation_upload["file_id"],
                "html_content": transcript_upload["url"],
                "transcript_pdf_fileId": transcript_upload["file_id"],
                "transcript_generated": generate_transcript_pdf,
                "total_ai_marks": total_ai_marks,
                "total_max_marks": total_max_marks,
                "questionwise_marking": questionwise_marking,
                "co": co_results_per_question,
                "evaluated_at": now.isoformat(),
                "token_usage": {
                    "gemini_total_tokens": token_usage["gemini"]["total_tokens"],
                    "claude_total_tokens": token_usage["claude"]["total_tokens"],
                    "grand_total_tokens": token_usage["grand_total_tokens"],
                    "transcript_generated": generate_transcript_pdf,
                },
            },
        })
        logging.info("[%s] Evaluation job completed.", job_id)

    except RuntimeError as e:
        logging.error("[%s] Job stopped (runtime): %s", job_id, e)
        await set_job(EVAL_JOB_PREFIX, job_id, {"status": "error", "message": str(e), "user_message": str(e)})

    except Exception as e:
        logging.error("[%s] Job failed: %s", job_id, e, exc_info=True)
        await set_job(EVAL_JOB_PREFIX, job_id, {
            "status": "error",
            "message": str(e),
            "user_message": "Something went wrong during evaluation. Please try again later.",
        })


# ============================================================
# ROUTES
# ============================================================

@router.post("/evaluate-answer-script", dependencies=[Depends(bulk_grading_rate_limit)])
async def evaluate_answer_script(
    background_tasks: BackgroundTasks,
    payload: EvaluateAnswerScriptRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, faculty_id, error = await get_current_user_and_faculty_details(identity, db)
    if error:
        message, code = error
        return JSONResponse(status_code=code, content={"error": message})

    exam_id = payload.folderId
    answer_id = payload.answerId
    generate_transcript_pdf = payload.generateTranscriptPdf

    # Grading uses both Gemini (OCR) and Claude (grading + optional
    # transcript) calls inside the background job — cost is only known once
    # the job finishes, so this is a pre-flight check only. If the institute
    # is already out of tokens, don't start a job that can never complete
    # cleanly; once started, a job is allowed to finish even if it pushes
    # usage slightly past the limit, rather than abandoning a half-graded
    # answer sheet.
    budget = await check_institute_token_budget(db, str(faculty_id), ["gemini", "claude"])
    if not budget["allowed"]:
        return JSONResponse(status_code=402, content={"error": budget["message"]})

    job_id = str(uuid.uuid4())
    await set_job(EVAL_JOB_PREFIX, job_id, {"status": "processing", "progress": 0, "step": "Starting evaluation"})

    await enqueue(
        "run_evaluation_job", job_id, exam_id, answer_id, generate_transcript_pdf, str(faculty_id),
        background_tasks=background_tasks,
    )

    return JSONResponse(status_code=202, content={
        "job_id": job_id,
        "status": "processing",
        "generate_transcript_pdf": generate_transcript_pdf,
        "message": "Evaluation started. Poll /evaluate-answer-script/status/<job_id>.",
        "token_warnings": budget["warnings"],
    })


@router.get("/evaluate-answer-script/status/{job_id}")
async def get_evaluation_status(job_id: str):
    job = await get_job(EVAL_JOB_PREFIX, job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    return job
