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
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity, get_current_user_and_faculty_details
from app.api.answer_access import require_answer_access
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
from app.services.imagekit import upload_file_to_imagekit, delete_imagekit_file
from app.services.mcq_grading import (
    apply_deterministic_mcq_overrides,
    ensure_mcq_answer_key,
    parse_mcq_answers_from_ocr_text,
)
from app.services.job_store import get_owned_job, set_job, update_job, claim_job_execution
from app.services.job_submission import claim_submission, release_submission
from app.services.paper_version import paper_snapshot_filter
from app.services.exam_paper_lock import lock_exam_for_marks
from app.services.pdf_render import render_html_to_pdf
from app.models.ai_usage_event import Feature, Provider
from app.services.faculty_ai_usage import tracked_faculty_call
from app.services.grading import CLAUDE_GRADING_MODEL, GEMINI_OCR_MODEL
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
    await update_job(EVAL_JOB_PREFIX, job_id, {"status": "error", "message": message})


# ============================================================
# BACKGROUND JOB
# ============================================================

async def _run_evaluation_job(
    job_id: str, exam_id: str, answer_id: str, generate_transcript_pdf: bool, faculty_id: str,
    user_id: str = None,
) -> None:
    db = get_database()
    if not await claim_job_execution(EVAL_JOB_PREFIX, job_id):
        return
    evaluation_upload = transcript_upload = None
    saved = False
    interrupted = False

    try:
        if not ObjectId.is_valid(exam_id) or not ObjectId.is_valid(answer_id):
            await _fail(job_id, "Invalid folderId or answerId")
            return

        exam_object_id = ObjectId(exam_id)
        answer_object_id = ObjectId(answer_id)

        await update_job(EVAL_JOB_PREFIX, job_id, {"status": "processing", "progress": 5, "step": "Fetching exam data"})

        exam = await db["newsavedDocs"].find_one({"_id": exam_object_id})
        if not exam:
            await _fail(job_id, "Folder not found")
            return

        if exam.get("faculty_id") != ObjectId(faculty_id):
            await _fail(job_id, "You are not authorized to evaluate this exam")
            return

        await lock_exam_for_marks(db, exam_object_id)
        # Upload could have won just before the permanent barrier was set.
        # Grade only the snapshot read AFTER acquiring that barrier.
        exam = await db["newsavedDocs"].find_one({"_id": exam_object_id, "faculty_id": ObjectId(faculty_id)})
        if not exam:
            await _fail(job_id, "Exam changed or was deleted")
            return

        answer = await db["answerDetails"].find_one({"_id": answer_object_id})
        if not answer:
            await _fail(job_id, "Answer script not found")
            return

        if answer.get("exam_id") != exam_object_id:
            await _fail(job_id, "Answer script does not belong to this folder")
            return
        if answer.get("reviewed_by_professor"):
            await _fail(job_id, "Reviewed marks are protected; evaluation was not started.")
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
        await update_job(EVAL_JOB_PREFIX, job_id, {
            "status": "processing", "progress": 15, "step": "Downloading student answer PDF",
        })
        student_pdf_bytes = await asyncio.to_thread(_download_pdf, student_pdf_url)

        # Step 2b — lazily resolve the MCQ answer key if this exam doesn't have one
        # yet (its question paper predates the deterministic-MCQ feature — a fresh
        # upload resolves it automatically, but papers are locked from re-upload
        # once set, so an already-existing exam would otherwise never get one). A
        # no-op (no AI call, no DB write) once the key is already saved — every
        # evaluation after the first on a given exam just reads it back instantly.
        mcq_answer_key: dict = await ensure_mcq_answer_key(db, exam, faculty_id, user_id)
        paper_guard = paper_snapshot_filter(exam_object_id, exam.get("question_paper") or {})
        if not await db["newsavedDocs"].find_one(paper_guard):
            raise RuntimeError("Question paper changed. Start evaluation again for the current paper.")

        # Step 3 — OCR
        await update_job(EVAL_JOB_PREFIX, job_id, {"progress": 30, "step": "Extracting student answer text"})
        # Only questions with a *confidently*-resolved answer key are asked for —
        # see app.services.mcq_grading — so OCR is never told to structure-extract a
        # question this exam has no reliable key for. Each question's option texts
        # (saved alongside the answer at key-resolution time) are passed through too,
        # so OCR can match a student who wrote out the answer's wording instead of
        # its letter — see extract_answer_text_with_gemini's docstring.
        confident_mcq_questions_with_options = {
            int(q_no): entry["options"]
            for q_no, entry in mcq_answer_key.items()
            if entry.get("confident") and entry.get("options")
        }
        answer_text, ans_gemini_tokens = await tracked_faculty_call(
            db, extract_answer_text_with_gemini, student_pdf_bytes, confident_mcq_questions_with_options or None,
            faculty_id=faculty_id, user_id=user_id, provider=Provider.GEMINI,
            model=GEMINI_OCR_MODEL, feature=Feature.GRADING_ANSWER_OCR, job_id=job_id,
        )
        gemini_calls = [ans_gemini_tokens]

        async def claude_step(function, *args, feature):
            return await tracked_faculty_call(
                db, function, *args, faculty_id=faculty_id, user_id=user_id,
                provider=Provider.CLAUDE, model=CLAUDE_GRADING_MODEL,
                feature=feature, job_id=job_id,
            )

        # Step 4 — grading (+ transcript, in parallel when requested)
        await update_job(EVAL_JOB_PREFIX, job_id, {"progress": 50, "step": "AI grading in progress"})
        student_name = answer.get("student_name") or "Student"

        if generate_transcript_pdf:
            (grading_result_raw, grade_claude_tokens), (transcript_html, transcript_claude_tokens) = await asyncio.gather(
                claude_step(grade_with_claude, question_text, answer_text, evaluation_rules,
                            feature=Feature.GRADING_ANSWER_EVALUATION),
                claude_step(generate_transcript_html_with_claude, answer_text, student_name,
                            feature=Feature.GRADING_TRANSCRIPT_GENERATION),
            )
            claude_calls = [grade_claude_tokens, transcript_claude_tokens]
        else:
            grading_result_raw, grade_claude_tokens = await claude_step(
                grade_with_claude, question_text, answer_text, evaluation_rules,
                feature=Feature.GRADING_ANSWER_EVALUATION,
            )
            transcript_html = None
            claude_calls = [grade_claude_tokens]

        grading_json = safe_json_parse(grading_result_raw)

        # Step 4b — deterministic MCQ override. Claude graded every question above
        # exactly as before (unchanged, zero risk to that path) — this replaces only
        # the entries for questions with a confidently-resolved answer key with a
        # plain letter-match result, so no AI leniency/partial-credit judgment can
        # reach the final saved marks for multiple-choice questions. See
        # app.services.mcq_grading module docstring for the full design rationale.
        student_mcq_answers = parse_mcq_answers_from_ocr_text(answer_text)
        grading_json["questionwise_marking"], mcq_override_count = apply_deterministic_mcq_overrides(
            grading_json.get("questionwise_marking", []), mcq_answer_key, student_mcq_answers,
        )
        if mcq_override_count:
            logging.info(
                "[eval-job %s] %d MCQ question(s) scored deterministically (answer key match)",
                job_id, mcq_override_count,
            )

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
        if not await db["newsavedDocs"].find_one(paper_guard):
            raise RuntimeError("Question paper changed. Start evaluation again for the current paper.")
        now = datetime.now(timezone.utc)
        save_result = await db["answerDetails"].update_one(
            {"_id": answer_object_id, "exam_id": exam_object_id,
             "reviewed_by_professor": {"$ne": True}},
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
        if save_result.matched_count != 1:
            raise RuntimeError("Answer was deleted or reviewed while AI ran. Existing marks were preserved.")
        saved = True

        # Already running as a background job, so just await this directly —
        # if this subject's batch/semester already has a generated academic
        # transcript, keep it in sync with the marks that were just saved.
        await refresh_transcript_for_exam(db, exam_object_id)

        # Step 12 — institute token counters (best-effort)
        await save_grading_tokens_to_institute(db, institute_id, token_usage, tokens_already_recorded=True)

        # Step 13 — final job payload
        await update_job(EVAL_JOB_PREFIX, job_id, {
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

    except asyncio.CancelledError:
        interrupted = True
        await update_job(EVAL_JOB_PREFIX, job_id, {"status": "interrupted",
            "message": "Evaluation was interrupted. Verify this job before starting another paid evaluation."})
        raise
    except RuntimeError as e:
        logging.error("[%s] Job stopped (runtime): %s", job_id, e)
        await update_job(EVAL_JOB_PREFIX, job_id, {"status": "error", "message": str(e), "user_message": str(e)})

    except Exception as e:
        logging.error("[%s] Job failed: %s", job_id, e, exc_info=True)
        await update_job(EVAL_JOB_PREFIX, job_id, {
            "status": "error",
            "message": str(e),
            "user_message": "Something went wrong during evaluation. Please try again later.",
        })
    finally:
        if not saved:
            for uploaded in (evaluation_upload, transcript_upload):
                if uploaded and uploaded.get("file_id"):
                    try:
                        await asyncio.to_thread(delete_imagekit_file, uploaded["file_id"])
                    except Exception:
                        logging.exception("Could not remove unused evaluation artifact")
        if not interrupted and ObjectId.is_valid(answer_id):
            try:
                await release_submission(db, "answerDetails", {"_id": ObjectId(answer_id)}, "active_evaluation", job_id)
            except Exception:
                logging.exception("Could not release evaluation submission %s", job_id)


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
    answer = await require_answer_access(identity, db, answer_id)
    if str(answer.get("exam_id")) != exam_id:
        raise HTTPException(400, "Answer script does not belong to this folder")
    if answer.get("reviewed_by_professor"):
        raise HTTPException(409, "Reviewed marks are protected; evaluation was not started.")
    job_id, is_new, options = await claim_submission(
        db, "answerDetails", {"_id": answer["_id"], "exam_id": answer["exam_id"]},
        "active_evaluation", EVAL_JOB_PREFIX, str(uuid.uuid4()), identity,
        {"generate_transcript_pdf": generate_transcript_pdf},
    )
    if not is_new:
        return JSONResponse(status_code=202, content={"job_id": job_id, "status": "processing",
            "reused": True, **options, "message": "Reconnected to the existing evaluation."})

    # Grading uses both Gemini (OCR) and Claude (grading + optional
    # transcript) calls inside the background job — cost is only known once
    # the job finishes, so this is a pre-flight check only. If the institute
    # is already out of tokens, don't start a job that can never complete
    # cleanly; once started, a job is allowed to finish even if it pushes
    # usage slightly past the limit, rather than abandoning a half-graded
    # answer sheet.
    budget = await check_institute_token_budget(db, str(faculty_id), ["gemini", "claude"])
    if not budget["allowed"]:
        await update_job(EVAL_JOB_PREFIX, job_id, {"status": "error", "message": budget["message"]})
        await release_submission(db, "answerDetails", {"_id": answer["_id"]}, "active_evaluation", job_id)
        return JSONResponse(status_code=402, content={"error": budget["message"]})

    try:
        await lock_exam_for_marks(db, answer["exam_id"])
        await enqueue(
            "run_evaluation_job", job_id, exam_id, answer_id, generate_transcript_pdf, str(faculty_id),
            str(user["_id"]), background_tasks=background_tasks,
        )
    except Exception:
        # Dispatch may have reached the queue. Retain the guard: retrying POST
        # must reconnect rather than bill a second job after an ambiguous write.
        await update_job(EVAL_JOB_PREFIX, job_id, {"dispatch_uncertain": True})
        return JSONResponse(status_code=503, content={"error": "Queue dispatch could not be confirmed. Retry to reconnect.", "job_id": job_id})

    return JSONResponse(status_code=202, content={
        "job_id": job_id,
        "status": "processing",
        "generate_transcript_pdf": generate_transcript_pdf,
        "message": "Evaluation started. Poll /evaluate-answer-script/status/<job_id>.",
        "token_warnings": budget["warnings"],
    })


@router.get("/evaluate-answer-script/status/{job_id}")
async def get_evaluation_status(
    job_id: str, identity: dict = Depends(get_current_identity),
):
    job = await get_owned_job(EVAL_JOB_PREFIX, job_id, identity)
    if job is None:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    return job
