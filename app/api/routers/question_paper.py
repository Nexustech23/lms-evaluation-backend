# ============================================================
# QUESTION PAPER ROUTER — AI generation + CRUD + docx editor save flow
# Ported from controllers/institute/question_controller.py (3,218 lines,
# the largest file in the Flask backend) +
# routes/institute/questionPaperGenerate_routes.py.
#
# Scope: AI generation (extract -> Claude -> docx) + full CRUD. The
# matplotlib/schemdraw diagram-rendering engine (Phase 3c) lives in
# app/services/diagram_render.py — <<<DIAGRAM>>> blocks embed as real
# images/tables (see app/services/docx_from_text.py), and render-diagram
# renders a single spec for the editor's live preview.
#
# Deviation from Flask: get-by-id is now scoped to the calling faculty
# (Flask's version had no ownership check at all — any authenticated user
# could fetch any non-deleted question paper by ID).
# ============================================================

import asyncio
import base64
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from io import BytesIO
from math import ceil
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity, get_current_user_and_faculty_details
from app.core.rate_limit import ai_rate_limit
from app.db.mongodb import get_database
from app.utils.uploads import read_upload_capped
from app.models.question_paper import build_create_document, build_update_fields, serialize_question_paper
from app.schemas.question_paper import (
    QuestionPaperSaveRequest,
    QuestionPaperUpdateRequest,
    RenderDiagramRequest,
)
from app.services.claude import generate_text
from app.services.diagram_render import draw_diagram
from app.services.docx_from_html import generate_docx_from_html, process_and_upload_base64_images
from app.services.docx_from_text import build_docx
from app.services.gemini import extract_text_from_file, generate_content_from_file
from app.services.imagekit import delete_imagekit_file, upload_file_to_imagekit
from app.services.job_store import get_job, set_job, update_job
from app.utils.token_usage import (
    check_institute_token_budget,
    increment_institute_claude_tokens,
    increment_institute_gemini_tokens,
)

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["question-paper"])

QP_JOB_PREFIX = "qp_job:"

_ACCEPTED_EXTENSIONS = {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx", "txt", "csv", "md", "odt"}

BLOOM_LABELS = {1: "Remember", 2: "Understand", 3: "Apply", 4: "Analyse", 5: "Evaluate", 6: "Create"}


def _is_accepted(filename: str) -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in _ACCEPTED_EXTENSIONS


async def _require_faculty(identity: dict, db: AsyncIOMotorDatabase):
    user, faculty_id, error = await get_current_user_and_faculty_details(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)
    return user, faculty_id


async def _get_institute_name(db: AsyncIOMotorDatabase, faculty_id: str) -> Optional[str]:
    faculty = await db["facultyDetails"].find_one({"_id": ObjectId(faculty_id)})
    if not faculty:
        return None
    institute_id = faculty.get("institute_id")
    if not institute_id:
        return None
    if isinstance(institute_id, str) and ObjectId.is_valid(institute_id):
        institute_id = ObjectId(institute_id)
    institute = await db["instituteDetails"].find_one({"_id": institute_id})
    if not institute:
        return None
    return institute.get("institute_name") or institute.get("name") or institute.get("instituteName")


# ============================================================
# SECTIONS HELPERS
# ============================================================

def _parse_sections(raw_sections_json: str) -> List[Dict[str, Any]]:
    default = [
        {"label": "Section A", "percent": 25, "bloomLevels": [1, 2]},
        {"label": "Section B", "percent": 40, "bloomLevels": [3, 4]},
        {"label": "Section C", "percent": 35, "bloomLevels": [5, 6]},
    ]
    if not raw_sections_json:
        return default
    try:
        sections = json.loads(raw_sections_json)
        if not isinstance(sections, list) or not sections:
            return default
        cleaned = []
        for s in sections:
            pct = max(0, min(100, int(s.get("percent", 0) or 0)))
            bloom = [int(b) for b in (s.get("bloomLevels") or []) if 1 <= int(b) <= 6]
            cleaned.append({
                "label": str(s.get("label") or f"Section {len(cleaned) + 1}"),
                "percent": pct,
                "bloomLevels": bloom,
            })
        total = sum(s["percent"] for s in cleaned)
        if total != 100:
            logging.warning("Sections total %d%% — using default.", total)
            return default
        return cleaned
    except Exception as e:
        logging.warning("_parse_sections failed: %s — using default.", e)
        return default


def _build_section_distribution_block(sections: List[Dict[str, Any]], total_marks: int) -> str:
    lines = ["SECTION MARKS DISTRIBUTION (MUST be followed exactly):"]
    for sec in sections:
        marks = round(total_marks * sec["percent"] / 100)
        pct = sec["percent"]
        label = sec["label"]
        bloom_levels = sec.get("bloomLevels") or []
        if bloom_levels:
            level_nums = ", ".join(str(l) for l in bloom_levels)
            level_names = ", ".join(BLOOM_LABELS.get(l, f"L{l}") for l in bloom_levels)
            bloom_str = f"Bloom's Level {level_nums} ({level_names})"
        else:
            bloom_str = "All Bloom's levels (no restriction)"
        lines.append(f"  - {label}: {marks} marks ({pct}% of {total_marks}) — {bloom_str}")
    return "\n".join(lines)


# ============================================================
# GEMINI EXTRACTION (reuses Phase 2's extract_text_from_file for office/
# plain-text formats — zero token cost; only PDFs go to Gemini)
# ============================================================

_EXTRACTION_PROMPT_TEMPLATE = (
    "Extract ALL text from this {hint} with high accuracy. "
    "Preserve the original structure including: headings, section numbers, question numbers, "
    "sub-questions, marks allocation, mathematical expressions, chemical formulas, tables, "
    "diagrams described in text.\nReturn plain text only. Do not use markdown."
)


def _extract_file_text(file_bytes: bytes, filename: str, extraction_hint: str) -> tuple:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf":
        prompt = _EXTRACTION_PROMPT_TEMPLATE.format(hint=extraction_hint)
        return generate_content_from_file(file_bytes, "application/pdf", prompt)
    text = extract_text_from_file(file_bytes, filename)
    return text, {"prompt_tokens": 0, "candidate_tokens": 0, "total_tokens": 0}


# ============================================================
# CLAUDE PROMPT
# ============================================================

_DIAGRAM_SPEC_BLOCK = """═══════════════════════════════════════════════════════
DIAGRAM / VISUAL ELEMENT RULES — READ CAREFULLY
═══════════════════════════════════════════════════════

Whenever a question requires or references a table, diagram, graph, formula,
chemical equation, or circuit, you MUST emit it as a machine-readable block
immediately AFTER the question text:

    <<<DIAGRAM>>>
    { ... valid JSON ... }
    <<<END_DIAGRAM>>>

One block per visual element. NEVER use ASCII art, plain-text tables, or raw
LaTeX strings outside these blocks.
IMPORTANT: The JSON inside <<<DIAGRAM>>> blocks must be valid JSON. Include a
"type" field (one of: data_table, math_expression, chemical_equation,
electrical_circuit, graph, plant_cell, binary_tree, network_graph) and a
"title" field describing the visual.

═══════════════════════════════════════════════════════
END OF DIAGRAM RULES
═══════════════════════════════════════════════════════"""


def _build_source_block(prompt: str, extracted_text: str, course_planner_text: str, sections, total_marks) -> str:
    section_block = _build_section_distribution_block(sections, total_marks)

    if extracted_text:
        block = (
            f"<question_bank>\n{extracted_text}\n</question_bank>\n\n"
            "Select and reword suitable questions from the question bank above to build the paper.\n\n"
            f"{section_block}"
        )
    else:
        block = f"<faculty_instructions>\n{prompt}\n</faculty_instructions>\n\n{section_block}"

    if course_planner_text:
        block += (
            f"\n\n<course_planner>\n{course_planner_text}\n</course_planner>\n\n"
            "CRITICAL: Restrict all questions to topics covered in the course planner above. "
            f"Distribute marks proportionally. Total = {total_marks}."
        )

    return block


def _build_co_block(co_list: list) -> str:
    if not co_list:
        return ""
    co_lines = "\n".join(f"* {c.get('co_code', '')}: {c.get('description', '')}" for c in co_list if c.get("co_code"))
    return f"""

COURSE OUTCOMES (tag every question with the CO code(s) it primarily assesses):
{co_lines}

Append a CO tag in square brackets immediately after each question's marks tag, e.g.
"[5 Marks] [CO1]" or "[8 Marks] [CO2, CO3]" if a question spans more than one outcome.
Distribute questions across the COs above — do not leave any CO with zero coverage unless
the faculty instructions or reference content explicitly restrict scope to a subset of COs."""


def _build_claude_prompt(
    prompt, extracted_text, course_planner_text, institute_name, subject_name,
    exam_type, semester, academic_year, total_marks, duration, sections, co_list=None,
) -> str:
    source_block = _build_source_block(prompt, extracted_text, course_planner_text, sections, total_marks)
    section_heading_list = "\n".join(f"   - {s['label']}" for s in sections)
    co_block = _build_co_block(co_list or [])

    instr_num = 3
    section_instr_lines = []
    for sec in sections:
        bloom_levels = sec.get("bloomLevels") or []
        level_desc = (
            f" ({'/'.join(BLOOM_LABELS.get(l, f'L{l}') for l in bloom_levels)} level questions)"
            if bloom_levels else ""
        )
        section_instr_lines.append(f"{instr_num}. {sec['label']} carries {sec['percent']}% of total marks{level_desc}.")
        instr_num += 1
    section_instrs_text = "\n".join(section_instr_lines)
    footer_instr_num = instr_num

    return f"""You are an expert academic question paper designer.

PAPER DETAILS:
* Institute:      {institute_name}
* Subject:        {subject_name}
* Exam Type:      {exam_type}
* Semester:       {semester}
* Academic Year:  {academic_year}
* Total Marks:    {total_marks}
* Duration:       {duration}
{co_block}

REFERENCE CONTENT:
{source_block}

{_DIAGRAM_SPEC_BLOCK}

QUESTION PAPER GENERATION GUIDELINES

1. Generate a professional university-style examination paper.
2. Follow faculty instructions as the highest priority.
3. Respect the section distribution above — both marks AND Bloom's levels.
4. The paper MUST contain exactly these sections (in order):
{section_heading_list}
5. Create questions appropriate to the subject, semester level, exam type, and marks.
6. Include a mix of question types where appropriate.
7. If numerical data, formulas, graphs, circuits, or tables are needed, use <<<DIAGRAM>>> blocks.
8. Ensure total marks exactly equal: {total_marks}
9. Do not generate answer keys unless explicitly requested.
10. IMPORTANT: Do NOT use markdown formatting (**bold**, *italic*, etc.) anywhere in the output.
11. IMPORTANT: Do NOT use bullet points (•, *, -, etc.) for instructions. Write instructions as plain numbered lines.

OUTPUT FORMAT

---BEGIN PAPER---

INSTRUCTIONS:
1. Attempt all questions.
2. Write your name and roll number clearly.
{section_instrs_text}
{footer_instr_num}. This paper is generated by Gradelytics | All rights reserved.

[Generate the complete question paper with all {len(sections)} sections]

---END PAPER---

Every question must include marks in square brackets e.g. [5 Marks].{" Every question must also include a CO tag as instructed above, e.g. [5 Marks] [CO1]." if co_block else ""}
Maintain professional academic language throughout.
Instructions must be written as plain numbered lines only — no bullet points or asterisks."""


# ============================================================
# BACKGROUND JOB
# ============================================================

async def _run_generation_job(job_id: str, params: dict, file_bytes: dict) -> None:
    db = get_database()
    faculty_id = params["faculty_id"]

    try:
        extracted_text = ""
        course_planner_text = ""

        if file_bytes.get("questionBank"):
            await update_job(QP_JOB_PREFIX, job_id, {"status": "processing", "step": "extracting_question_bank"})
            extracted_text, g_usage = await asyncio.to_thread(
                _extract_file_text, file_bytes["questionBank"], file_bytes["questionBankFilename"], "question bank"
            )
            await increment_institute_gemini_tokens(
                db, faculty_id, g_usage["prompt_tokens"], g_usage["candidate_tokens"]
            )

        if file_bytes.get("coursePlanner"):
            await update_job(QP_JOB_PREFIX, job_id, {"step": "extracting_course_planner"})
            course_planner_text, g_usage2 = await asyncio.to_thread(
                _extract_file_text, file_bytes["coursePlanner"], file_bytes["coursePlannerFilename"], "course planner"
            )
            await increment_institute_gemini_tokens(
                db, faculty_id, g_usage2["prompt_tokens"], g_usage2["candidate_tokens"]
            )

        await update_job(QP_JOB_PREFIX, job_id, {"step": "generating_paper"})
        full_prompt = _build_claude_prompt(
            params["prompt"], extracted_text, course_planner_text, params["institute_name"],
            params["subject_name"], params["exam_type"], params["semester"], params["academic_year"],
            params["total_marks"], params["duration"], params["sections"], params.get("co_list") or [],
        )
        paper_text_raw, c_usage = await asyncio.to_thread(generate_text, full_prompt, "claude-sonnet-4-6", 8000)
        paper_text = re.sub(r"^-{3}BEGIN PAPER-{3}\s*", "", paper_text_raw)
        paper_text = re.sub(r"\s*-{3}END PAPER-{3}$", "", paper_text).strip()
        await increment_institute_claude_tokens(db, faculty_id, c_usage["input_tokens"], c_usage["output_tokens"])

        await update_job(QP_JOB_PREFIX, job_id, {"step": "building_docx"})
        docx_bytes = await asyncio.to_thread(
            build_docx, paper_text, params["institute_name"], params["department_name"],
            params["subject_name"], params["exam_type"], params["semester"], params["academic_year"],
            params["total_marks"], params["duration"], params.get("co_list") or [],
        )

        await update_job(QP_JOB_PREFIX, job_id, {"step": "uploading"})
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"question_paper_{ts}.docx"
        upload = await asyncio.to_thread(
            upload_file_to_imagekit, docx_bytes, filename,
            f"/question_papers/{faculty_id}", ["question_paper", params["subject_name"]],
        )

        await set_job(QP_JOB_PREFIX, job_id, {
            "status": "completed",
            "step": "done",
            "paperText": paper_text,
            "docxBase64": base64.b64encode(docx_bytes).decode("utf-8"),
            "filename": filename,
            "file_url": upload["url"],
            "file_id": upload["file_id"],
            "instituteName": params["institute_name"],
            "departmentName": params["department_name"],
            "subjectName": params["subject_name"],
            "examType": params["exam_type"],
            "semester": params["semester"],
            "academicYear": params["academic_year"],
            "totalMarks": params["total_marks"],
            "duration": params["duration"],
            "generationSource": "pdf" if file_bytes.get("questionBank") else "prompt",
            "tokenUsage": c_usage,
        })
        logging.info("[qp:%s] Job completed.", job_id)

    except Exception as e:
        logging.error("[qp:%s] Job failed: %s", job_id, e, exc_info=True)
        await set_job(QP_JOB_PREFIX, job_id, {"status": "failed", "step": "error", "error": str(e)})


# ============================================================
# ROUTES — AI GENERATION
# ============================================================

@router.post("/question-paper/generate-ai", dependencies=[Depends(ai_rate_limit)])
async def generate_question_paper_ai(
    background_tasks: BackgroundTasks,
    prompt: str = Form(""),
    departmentName: str = Form(""),
    subjectName: str = Form("Subject"),
    examType: str = Form("End Semester Examination"),
    semester: str = Form(""),
    academicYear: str = Form(""),
    duration: str = Form("3 Hours"),
    # Previously str, coerced via a try/except that silently fell back to
    # 100 on bad input (e.g. totalMarks="abc") — typed as int so FastAPI
    # rejects invalid input with a 422 instead, matching pomodoro.py's
    # equivalent Form-field retrofit.
    totalMarks: int = Form(100),
    sections: str = Form(""),
    # Optional — the "Select subject" dropdown previously only supplied
    # subjectName as a display string, with no way for the backend to look
    # up the actual subject document. When provided, its Course Outcomes
    # are pulled in and passed to the AI so generated questions can be
    # tagged with the CO(s) they assess.
    subjectId: Optional[str] = Form(None),
    questionBank: Optional[UploadFile] = File(None),
    coursePlanner: Optional[UploadFile] = File(None),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    try:
        _, faculty_id = await _require_faculty(identity, db)
        prompt = prompt.strip()

        co_list: list = []
        if subjectId:
            if not ObjectId.is_valid(subjectId):
                return JSONResponse(status_code=400, content={"error": "Invalid subjectId"})
            subject = await db["subjectDetails"].find_one({"_id": ObjectId(subjectId), "is_deleted": {"$ne": True}})
            if not subject:
                return JSONResponse(status_code=404, content={"error": "Subject not found"})
            if subject.get("faculty_id") != faculty_id:
                return JSONResponse(status_code=403, content={"error": "You are not assigned to this subject"})
            co_list = subject.get("co") or []

        qb_bytes = None
        qb_filename = ""
        if questionBank:
            fname = questionBank.filename or "upload"
            if not _is_accepted(fname):
                ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else "unknown"
                return JSONResponse(status_code=400, content={"error": f"Unsupported question bank file type '.{ext}'."})
            qb_bytes = await read_upload_capped(questionBank)
            qb_filename = fname

        if not prompt and not qb_bytes:
            return JSONResponse(status_code=400, content={"error": "Provide either a 'prompt' or a 'questionBank' file."})

        # Generation uses both Gemini (file extraction) and Claude (the
        # paper itself), so if either pool is exhausted the whole job is
        # blocked upfront rather than burning the other provider's tokens
        # on a run that can't finish.
        budget = await check_institute_token_budget(db, str(faculty_id), ["gemini", "claude"])
        if not budget["allowed"]:
            return JSONResponse(status_code=402, content={"error": budget["message"]})

        cp_bytes = None
        cp_filename = ""
        if coursePlanner:
            fname = coursePlanner.filename or "upload"
            if _is_accepted(fname):
                cp_bytes = await read_upload_capped(coursePlanner)
                cp_filename = fname
            else:
                logging.warning("Unsupported course planner file type for '%s' — dropped.", fname)

        total_marks = totalMarks

        sections_list = _parse_sections(sections)
        institute_name = await _get_institute_name(db, faculty_id) or ""

        job_id = str(uuid.uuid4())
        await set_job(QP_JOB_PREFIX, job_id, {
            "status": "queued", "step": "starting", "job_id": job_id, "faculty_id": str(faculty_id),
        })

        params = {
            "institute_name": institute_name,
            "department_name": departmentName,
            "subject_name": subjectName,
            "exam_type": examType,
            "semester": semester,
            "academic_year": academicYear,
            "duration": duration,
            "total_marks": total_marks,
            "sections": sections_list,
            "prompt": prompt,
            "faculty_id": str(faculty_id),
            "co_list": co_list,
        }
        job_file_bytes = {
            "questionBank": qb_bytes, "questionBankFilename": qb_filename,
            "coursePlanner": cp_bytes, "coursePlannerFilename": cp_filename,
        }

        background_tasks.add_task(_run_generation_job, job_id, params, job_file_bytes)

        return JSONResponse(status_code=202, content={
            "success": True,
            "jobId": job_id,
            "message": "Generation started. Poll /question-paper/generate-ai/status/<jobId>.",
            "token_warnings": budget["warnings"],
        })

    except HTTPException:
        raise
    except Exception as e:
        logging.error("generate_question_paper_ai error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})


@router.get("/question-paper/generate-ai/status/{job_id}")
async def generate_question_paper_status(job_id: str):
    job = await get_job(QP_JOB_PREFIX, job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"error": "Job not found or expired."})

    status = job.get("status", "unknown")

    if status == "completed":
        return {
            "success": True,
            "status": "completed",
            "step": "done",
            "paperText": job.get("paperText"),
            "docxBase64": job.get("docxBase64"),
            "filename": job.get("filename"),
            "fileUrl": job.get("file_url"),
            "fileId": job.get("file_id"),
            "instituteName": job.get("instituteName"),
            "departmentName": job.get("departmentName"),
            "subjectName": job.get("subjectName"),
            "examType": job.get("examType"),
            "semester": job.get("semester"),
            "academicYear": job.get("academicYear"),
            "totalMarks": job.get("totalMarks"),
            "duration": job.get("duration"),
            "generationSource": job.get("generationSource"),
            "tokenUsage": job.get("tokenUsage"),
        }

    if status == "failed":
        return JSONResponse(status_code=500, content={
            "success": False, "status": "failed", "error": job.get("error", "Unknown error."),
        })

    return JSONResponse(status_code=202, content={
        "success": True, "status": status, "step": job.get("step", ""), "message": "Generation in progress...",
    })


@router.post("/question-paper/render-diagram")
async def render_diagram(payload: RenderDiagramRequest):
    spec = payload.spec
    if not spec:
        return JSONResponse(status_code=400, content={"error": "Missing diagram specification"})
    try:
        png_bytes = await asyncio.to_thread(draw_diagram, spec)
        b64_str = base64.b64encode(png_bytes).decode("utf-8")
        return {"image": f"data:image/png;base64,{b64_str}"}
    except Exception as e:
        logging.error("render_diagram error: %s", e, exc_info=True)
        return JSONResponse(status_code=500, content={"error": str(e)})


# ============================================================
# ROUTES — CRUD / EDITOR SAVE FLOW
# ============================================================

@router.post("/question-paper/save")
async def save_question_paper(
    payload: QuestionPaperSaveRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    _, faculty_id = await _require_faculty(identity, db)
    faculty_object_id = ObjectId(faculty_id)

    editor_content = payload.editorContent
    processed_html = await asyncio.to_thread(process_and_upload_base64_images, editor_content, str(faculty_id))
    data = {**payload.model_dump(), "editorContent": processed_html}

    try:
        doc = build_create_document(data, faculty_object_id, faculty_object_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    header_data = {
        "institute": data.get("instituteName", ""),
        "department": data.get("departmentName", ""),
        "examType": data.get("examType", ""),
        "subjectName": data.get("subjectName", ""),
        "semester": data.get("semester", ""),
        "academicYear": data.get("academicYear", ""),
        "duration": data.get("duration", ""),
        "totalMarks": data.get("totalMarks", ""),
    }
    docx_bytes = await asyncio.to_thread(generate_docx_from_html, processed_html, header_data)

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    upload = await asyncio.to_thread(
        upload_file_to_imagekit, docx_bytes, f"question-paper-{ts}.docx",
        "/question-papers", ["question-paper", str(faculty_id)],
    )
    doc["question_paper_url"] = upload["url"]
    doc["question_paper_file_id"] = upload["file_id"]

    result = await db["questionPaperDetails"].insert_one(doc)
    created = await db["questionPaperDetails"].find_one({"_id": result.inserted_id})

    return JSONResponse(status_code=201, content={"success": True, "questionPaper": serialize_question_paper(created)})


@router.get("/question-paper")
async def list_question_papers(
    page: int = Query(1),
    limit: int = Query(10),
    subjectId: Optional[str] = Query(None),
    examType: Optional[str] = Query(None),
    academicYear: Optional[str] = Query(None),
    semester: Optional[str] = Query(None),
    schoolId: Optional[str] = Query(None),
    programmeId: Optional[str] = Query(None),
    departmentId: Optional[str] = Query(None),
    batchId: Optional[str] = Query(None),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    _, faculty_id = await _require_faculty(identity, db)

    page = max(1, page)
    limit = max(1, limit)
    skip = (page - 1) * limit

    match_stage: Dict[str, Any] = {"faculty_id": ObjectId(faculty_id), "is_deleted": False}
    if subjectId and ObjectId.is_valid(subjectId):
        match_stage["subject_id"] = ObjectId(subjectId)
    if examType:
        match_stage["exam_type"] = examType
    if academicYear:
        match_stage["academic_year"] = academicYear
    if semester:
        match_stage["semester"] = semester
    for field, value in [("school_id", schoolId), ("programme_id", programmeId),
                          ("department_id", departmentId), ("batch_id", batchId)]:
        if value and ObjectId.is_valid(value):
            match_stage[field] = ObjectId(value)

    total_papers = await db["questionPaperDetails"].count_documents(match_stage)

    pipeline = [
        {"$match": match_stage},
        {"$sort": {"created_at": -1}},
        {"$skip": skip},
        {"$limit": limit},
        {"$lookup": {"from": "schoolDetails", "localField": "school_id", "foreignField": "_id", "as": "school_doc"}},
        {"$lookup": {"from": "programmeDetails", "localField": "programme_id", "foreignField": "_id", "as": "programme_doc"}},
        {"$lookup": {"from": "departmentDetails", "localField": "department_id", "foreignField": "_id", "as": "department_doc"}},
        {"$lookup": {"from": "batchDetails", "localField": "batch_id", "foreignField": "_id", "as": "batch_doc"}},
        {"$addFields": {
            "school_name": {"$ifNull": [{"$arrayElemAt": ["$school_doc.school_name", 0]}, "$school_name"]},
            "programme_name": {"$ifNull": [{"$arrayElemAt": ["$programme_doc.programme_name", 0]}, "$programme_name"]},
            "department_name": {"$ifNull": [{"$arrayElemAt": ["$department_doc.department_name", 0]}, "$department_name"]},
            "batch_name": {"$ifNull": [{"$arrayElemAt": ["$batch_doc.batch_name", 0]}, "$batch_name"]},
        }},
    ]

    papers = [serialize_question_paper(doc) async for doc in db["questionPaperDetails"].aggregate(pipeline)]

    return {
        "success": True,
        "questionPapers": papers,
        "totalPapers": total_papers,
        "totalPages": ceil(total_papers / limit) if limit else 0,
        "currentPage": page,
        "limit": limit,
    }


@router.get("/question-paper/{question_paper_id}")
async def get_question_paper(
    question_paper_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    # Deviation from Flask: scoped to the calling faculty (see module docstring).
    _, faculty_id = await _require_faculty(identity, db)

    if not ObjectId.is_valid(question_paper_id):
        raise HTTPException(status_code=400, detail="Invalid question paper id")

    doc = await db["questionPaperDetails"].find_one({
        "_id": ObjectId(question_paper_id), "faculty_id": ObjectId(faculty_id), "is_deleted": False,
    })
    if not doc:
        raise HTTPException(status_code=404, detail="Question paper not found")

    return {"success": True, "questionPaper": serialize_question_paper(doc)}


@router.put("/question-paper/{question_paper_id}")
async def update_question_paper(
    question_paper_id: str,
    payload: QuestionPaperUpdateRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    data = payload.model_dump(exclude_unset=True)
    _, faculty_id = await _require_faculty(identity, db)
    faculty_object_id = ObjectId(faculty_id)

    if not ObjectId.is_valid(question_paper_id):
        raise HTTPException(status_code=400, detail="Invalid question paper id")

    existing = await db["questionPaperDetails"].find_one({
        "_id": ObjectId(question_paper_id), "faculty_id": faculty_object_id, "is_deleted": False,
    })
    if not existing:
        raise HTTPException(status_code=404, detail="Question paper not found")

    try:
        update_fields = build_update_fields(data, faculty_object_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if "editorContent" in data:
        processed_html = await asyncio.to_thread(
            process_and_upload_base64_images, data["editorContent"], str(faculty_id)
        )
        update_fields["editor_content"] = processed_html

        header_data = {
            "institute": update_fields.get("institute_name", existing.get("institute_name", "")),
            "department": update_fields.get("department_name", existing.get("department_name", "")),
            "examType": update_fields.get("exam_type", existing.get("exam_type", "")),
            "subjectName": update_fields.get("subject_name", existing.get("subject_name", "")),
            "semester": update_fields.get("semester", existing.get("semester", "")),
            "academicYear": update_fields.get("academic_year", existing.get("academic_year", "")),
            "duration": update_fields.get("duration", existing.get("duration", "")),
            "totalMarks": update_fields.get("total_marks", existing.get("total_marks", "")),
        }
        docx_bytes = await asyncio.to_thread(generate_docx_from_html, processed_html, header_data)

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        upload = await asyncio.to_thread(
            upload_file_to_imagekit, docx_bytes, f"question-paper-{ts}.docx",
            "/question-papers", ["question-paper", str(faculty_id)],
        )

        old_file_id = existing.get("question_paper_file_id")
        if old_file_id:
            await asyncio.to_thread(delete_imagekit_file, old_file_id)

        update_fields["question_paper_url"] = upload["url"]
        update_fields["question_paper_file_id"] = upload["file_id"]

    await db["questionPaperDetails"].update_one(
        {"_id": ObjectId(question_paper_id), "faculty_id": faculty_object_id, "is_deleted": False},
        {"$set": update_fields},
    )
    updated = await db["questionPaperDetails"].find_one({"_id": ObjectId(question_paper_id)})

    return {"success": True, "questionPaper": serialize_question_paper(updated)}


@router.delete("/question-paper/{question_paper_id}")
async def delete_question_paper(
    question_paper_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    _, faculty_id = await _require_faculty(identity, db)

    if not ObjectId.is_valid(question_paper_id):
        raise HTTPException(status_code=400, detail="Invalid question paper id")

    faculty_object_id = ObjectId(faculty_id)
    result = await db["questionPaperDetails"].update_one(
        {"_id": ObjectId(question_paper_id), "faculty_id": faculty_object_id},
        {"$set": {
            "is_deleted": True, "is_active": False,
            "updated_at": datetime.now(timezone.utc), "updated_by": faculty_object_id,
        }},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Question paper not found")

    return {"success": True, "message": "Question paper deleted successfully"}


@router.post("/question-paper/upload-docx")
async def upload_question_paper_docx(
    docx: UploadFile = File(...),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    _, faculty_id = await _require_faculty(identity, db)

    filename = docx.filename or "upload.docx"
    if not filename.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="Only .docx files are accepted")

    file_bytes = await read_upload_capped(docx)

    import mammoth
    result = await asyncio.to_thread(mammoth.convert_to_html, BytesIO(file_bytes))
    editor_content = result.value

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    upload = await asyncio.to_thread(
        upload_file_to_imagekit, file_bytes, f"{ts}_{safe_name}",
        "/question-papers", ["question-paper", "docx", str(faculty_id)],
    )

    return {
        "success": True,
        "url": upload["url"],
        "fileId": upload["file_id"],
        "filename": filename,
        "editorContent": editor_content,
    }
