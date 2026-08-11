# ============================================================
# MOCK TESTS ROUTER
# Ported from routes/institute/mock_test_routes.py +
# controllers/institute/mock_test_controller.py +
# controllers/institute/test_attempt_controller.py +
# controllers/institute/analytics_controller.py (GET /mock-tests/analytics)
#
# Deviation from Flask: GET /mock-tests/{id} (used to fetch a test for the
# student to take) redacts correct_answer/explanation from each question —
# Flask returned them as-is, inspectable via dev tools before submitting.
# Only the post-submission /review response includes them.
# ============================================================

import asyncio
import base64
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity
from app.db.mongodb import get_database
from app.models.mock_test import build_create_document, serialize_mock_test, serialize_question
from app.schemas.mock_test import MockTestCreateRequest, MockTestSubmitRequest
from app.services.attempt_insight import generate_attempt_insight
from app.services.mock_test_generation import build_mock_test_prompt, generate_mock_test_questions

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["mock-tests"])


def _quiz_attempt_id(roadmap_id: Any, week: Any, submitted_at: datetime) -> str:
    # base64url instead of a raw "quiz:<id>:<week>:<iso timestamp>" string —
    # the timestamp itself contains colons, and those get mangled somewhere
    # in the browser -> Next.js rewrite -> backend round trip (colons are
    # exactly the kind of "special but not always re-escaped" character that
    # breaks across that many encode/decode layers). base64url's alphabet
    # (A-Za-z0-9-_) needs zero percent-encoding anywhere in that chain.
    raw = f"{roadmap_id}:{week}:{submitted_at.isoformat()}"
    token = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    return f"q_{token}"


def _parse_quiz_attempt_id(attempt_id: str) -> Optional[Tuple[str, int, datetime]]:
    if not attempt_id.startswith("q_"):
        return None
    token = attempt_id[2:]
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
    except Exception:
        return None
    parts = raw.split(":", 2)
    if len(parts) != 3:
        return None
    roadmap_id, week_str, ts = parts
    try:
        week = int(week_str)
        submitted_at = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if not ObjectId.is_valid(roadmap_id):
        return None
    return roadmap_id, week, submitted_at


def _serialize_attempt(doc: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(doc)
    out["_id"] = str(out["_id"])
    out["student_id"] = str(out["student_id"])
    out["test_id"] = str(out["test_id"])
    if out.get("subject_id"):
        out["subject_id"] = str(out["subject_id"])
    for field in ("submitted_at", "created_at"):
        if out.get(field):
            out[field] = out[field].isoformat()
    return out


def _evaluate_answers(
    questions: List[Dict[str, Any]], answers: Dict[str, Any], marks_per_question: float,
    negative_marking: bool, negative_marks: float,
) -> Tuple[int, int, int, float, List[Dict[str, Any]]]:
    correct = wrong = skipped = 0
    scored = 0.0
    qwise = []

    for q in questions:
        qid = str(q.get("_id") or q.get("id") or "")
        student_answer = str(answers.get(qid, "")).strip().lower()
        correct_answer = str(q.get("correct_answer", "")).strip().lower()
        q_marks = q.get("marks", marks_per_question)

        if not student_answer:
            skipped += 1
            status, marks_awarded = "skipped", 0
        elif student_answer == correct_answer:
            correct += 1
            status, marks_awarded = "correct", q_marks
            scored += q_marks
        else:
            wrong += 1
            marks_awarded = -negative_marks if negative_marking else 0
            status = "wrong"
            scored += marks_awarded

        qwise.append({
            "question_id": qid,
            "student_answer": answers.get(qid, ""),
            "correct_answer": q.get("correct_answer"),
            "status": status,
            "marks_awarded": marks_awarded,
        })

    return correct, wrong, skipped, max(0, round(scored, 2)), qwise


def _date_filter(time_range: str) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    if time_range == "week":
        return {"submitted_at": {"$gte": now - timedelta(days=7)}}
    if time_range == "month":
        return {"submitted_at": {"$gte": now - timedelta(days=30)}}
    return {}


def _safe_pct(num: float, den: float) -> float:
    if not den:
        return 0
    return round((num / den) * 100, 1)


# ============================================================
# BACKGROUND GENERATION
# ============================================================

async def _run_generation(test_id: ObjectId, prompt: str) -> None:
    db = get_database()
    now = datetime.now(timezone.utc)
    try:
        questions = await asyncio.to_thread(generate_mock_test_questions, prompt)
        await db["mockTests"].update_one(
            {"_id": test_id},
            {"$set": {"questions": questions, "questionCount": len(questions), "updated_at": now}},
        )
        logging.info("[mock-test:%s] generation completed — %d questions.", test_id, len(questions))
    except Exception as e:
        logging.error("[mock-test:%s] generation failed: %s", test_id, e, exc_info=True)
        await db["mockTests"].update_one({"_id": test_id}, {"$set": {"generationError": str(e), "updated_at": now}})


# ============================================================
# ROUTES
# ============================================================

@router.post("/mock-tests")
async def create_mock_test(
    background_tasks: BackgroundTasks,
    payload: MockTestCreateRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    student_id = ObjectId(identity["user_id"])

    try:
        doc = build_create_document(payload.model_dump(exclude_unset=True), student_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db["mockTests"].insert_one(doc)
    test_id = result.inserted_id

    prompt = build_mock_test_prompt(
        doc.get("subjectName") or "General", doc.get("topic"), doc["difficulty"],
        doc["questionCount"], doc["questionTypes"], doc["marksPerQuestion"],
    )
    background_tasks.add_task(_run_generation, test_id, prompt)

    return {
        "success": True,
        "message": "Mock test created. Questions are being generated…",
        "testId": str(test_id),
        "mockTest": {"_id": str(test_id)},
        "test": {"_id": str(test_id)},
    }


@router.get("/mock-tests")
async def list_mock_tests(identity: dict = Depends(get_current_identity), db: AsyncIOMotorDatabase = Depends(get_database)):
    student_id = ObjectId(identity["user_id"])
    cursor = db["mockTests"].find({"student_id": student_id}, {"questions": 0}).sort("created_at", -1)
    tests = [serialize_mock_test(doc) async for doc in cursor]
    return {"success": True, "tests": tests}


@router.get("/mock-tests/analytics")
async def get_mock_test_analytics(
    range: str = "all",
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Declared before /mock-tests/{test_id} so FastAPI doesn't match
    "analytics" as a test_id path param (same ordering Flask relies on)."""
    student_id = ObjectId(identity["user_id"])
    date_filter = _date_filter(range)
    base_filter: Dict[str, Any] = {"student_id": student_id, **date_filter}

    # testAttempts (Practice Tests) — kept as raw DB docs since topic/
    # difficulty/question-type breakdowns below need the real test_id join;
    # weekly-quiz entries have no equivalent per-question tagging to offer
    # there, same limitation the Flask analytics module has.
    attempts = await db["testAttempts"].find(base_filter).sort("submitted_at", -1).to_list(length=None)

    # Weekly Quiz history — lives embedded per-roadmap
    # (selfLearnerRoadmaps.progress.quizHistory[]), not in its own
    # collection, and was never part of this endpoint until now.
    cutoff = date_filter.get("submitted_at", {}).get("$gte")
    roadmap_docs = await db["selfLearnerRoadmaps"].find(
        {"user_id": student_id}, {"subject": 1, "progress.quizHistory": 1},
    ).to_list(length=None)

    quiz_normalized: List[Dict[str, Any]] = []
    for rd in roadmap_docs:
        subject = rd.get("subject", "Roadmap")
        for entry in rd.get("progress", {}).get("quizHistory", []):
            submitted_at = entry.get("submittedAt")
            if not submitted_at or (cutoff and submitted_at < cutoff):
                continue
            week = entry.get("week")
            total_q = entry.get("totalQuestions", 0)
            correct_q = entry.get("correctCount", 0)
            quiz_normalized.append({
                "id": _quiz_attempt_id(rd["_id"], week, submitted_at),
                "test_id": "",
                "sourceType": "weekly_quiz",
                "testTitle": f"Week {week}: {subject}",
                "subjectName": subject,
                "scored": correct_q,
                "totalMarks": total_q,
                "percentage": entry.get("score", 0),
                "correct": correct_q,
                "wrong": max(0, total_q - correct_q),
                "skipped": 0,
                "accuracy": _safe_pct(correct_q, total_q),
                "submitted_at": submitted_at,
                "hasInsight": bool(entry.get("aiInsight")),
            })

    test_normalized: List[Dict[str, Any]] = [
        {
            "id": str(a["_id"]),
            "test_id": str(a.get("test_id", "")),
            "sourceType": "practice_test",
            "testTitle": a.get("testTitle", ""),
            "subjectName": a.get("subjectName", ""),
            "scored": a.get("scored", 0),
            "totalMarks": a.get("totalMarks", 0),
            "percentage": a.get("percentage", 0),
            "correct": a.get("correct", 0),
            "wrong": a.get("wrong", 0),
            "skipped": a.get("skipped", 0),
            "accuracy": a.get("accuracy", 0),
            "submitted_at": a.get("submitted_at"),
            "hasInsight": bool(a.get("aiInsight")),
        }
        for a in attempts
    ]

    all_attempts = sorted(
        test_normalized + quiz_normalized,
        key=lambda a: a["submitted_at"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True,
    )

    if not all_attempts:
        return {
            "success": True,
            "summary": {
                "testsAttempted": 0, "totalQuestions": 0, "avgScore": 0,
                "bestScore": 0, "avgAccuracy": 0, "totalTimeMins": 0,
            },
            "attempts": [],
            "subjectPerformance": [],
            "topicPerformance": [],
            "difficultyBreakdown": {
                "easy": {"correct": 0, "total": 0},
                "medium": {"correct": 0, "total": 0},
                "hard": {"correct": 0, "total": 0},
            },
            "scoreTrend": [],
            "questionTypeBreakdown": [],
            "strengths": [],
            "improvements": [],
            "improvementPct": None,
        }

    # ── Summary ──────────────────────────────────────────
    tests_attempted = len(all_attempts)
    total_questions = sum(a["correct"] + a["wrong"] + a["skipped"] for a in all_attempts)
    avg_score = round(sum(a["percentage"] for a in all_attempts) / tests_attempted, 1)
    best_score = round(max(a["percentage"] for a in all_attempts), 1)
    avg_accuracy = round(sum(a["accuracy"] for a in all_attempts) / tests_attempted, 1)

    summary = {
        "testsAttempted": tests_attempted,
        "totalQuestions": total_questions,
        "avgScore": avg_score,
        "bestScore": best_score,
        "avgAccuracy": avg_accuracy,
        "totalTimeMins": 0,
    }

    # ── Improvement ──────────────────────────────────────
    improvement_pct = None
    if tests_attempted >= 4:
        mid = tests_attempted // 2
        first = [a["percentage"] for a in all_attempts[mid:]]
        second = [a["percentage"] for a in all_attempts[:mid]]
        avg_first = sum(first) / len(first)
        avg_second = sum(second) / len(second)
        improvement_pct = round(avg_second - avg_first, 1)

    # ── Attempt list ─────────────────────────────────────
    attempt_list = [
        {
            "_id": a["id"],
            "sourceType": a["sourceType"],
            "test_id": a["test_id"],
            "testTitle": a["testTitle"],
            "subjectName": a["subjectName"],
            "scored": a["scored"],
            "totalMarks": a["totalMarks"],
            "percentage": a["percentage"],
            "date": a["submitted_at"].isoformat() if a["submitted_at"] else None,
            "hasInsight": a["hasInsight"],
        }
        for a in all_attempts
    ]

    # ── Subject performance ───────────────────────────────
    subject_map: Dict[str, Dict[str, float]] = {}
    for a in all_attempts:
        subj = a["subjectName"] or "Unknown"
        entry = subject_map.setdefault(subj, {"scored": 0.0, "total": 0.0})
        entry["scored"] += a["scored"]
        entry["total"] += a["totalMarks"]

    subject_perf = [{"subject": k, "scored": v["scored"], "total": v["total"]} for k, v in subject_map.items()]

    # ── Topic + difficulty + question type ────────────────
    test_ids = [a["test_id"] for a in attempts if a.get("test_id")]
    test_docs: Dict[str, Dict[str, Any]] = {}
    if test_ids:
        cursor = db["mockTests"].find({"_id": {"$in": test_ids}}, {"topic": 1, "difficulty": 1})
        async for t in cursor:
            test_docs[str(t["_id"])] = t

    topic_map: Dict[str, Dict[str, int]] = {}
    diff_map = {
        "easy": {"correct": 0, "total": 0},
        "medium": {"correct": 0, "total": 0},
        "hard": {"correct": 0, "total": 0},
    }
    qtype_map: Dict[str, Dict[str, int]] = {}

    for a in attempts:
        tid = str(a.get("test_id", ""))
        tdoc = test_docs.get(tid, {})
        topic = (tdoc.get("topic") or "").strip() or "General"
        diff = tdoc.get("difficulty", "mixed")

        topic_entry = topic_map.setdefault(topic, {"correct": 0, "total": 0})
        topic_entry["correct"] += a.get("correct", 0)
        topic_entry["total"] += a.get("correct", 0) + a.get("wrong", 0)

        if diff in diff_map:
            diff_map[diff]["correct"] += a.get("correct", 0)
            diff_map[diff]["total"] += a.get("correct", 0) + a.get("wrong", 0)

        for qw in a.get("questionwise", []):
            qtype = qw.get("type", "mcq")
            qtype_entry = qtype_map.setdefault(qtype, {"correct": 0, "total": 0})
            qtype_entry["total"] += 1
            if qw.get("status") == "correct":
                qtype_entry["correct"] += 1

    topic_perf = [
        {"topic": k, "correct": v["correct"], "total": v["total"]}
        for k, v in topic_map.items()
        if v["total"] > 0
    ]
    qtype_breakdown = [{"type": k, "correct": v["correct"], "total": v["total"]} for k, v in qtype_map.items()]

    # ── Score trend ───────────────────────────────────────
    trend_data = [
        {
            "label": a["submitted_at"].strftime("%d %b") if a["submitted_at"] else f"T{i + 1}",
            "score": a["percentage"],
        }
        for i, a in enumerate(reversed(all_attempts[:10]))
    ]

    # ── Strengths & Improvements ─────────────────────────
    strengths: List[Dict[str, str]] = []
    improvements: List[Dict[str, str]] = []

    for subj, v in subject_map.items():
        p = _safe_pct(v["scored"], v["total"])
        entry = {"label": subj, "detail": f"{p}% avg score"}
        if p >= 75:
            strengths.append(entry)
        elif p < 50:
            improvements.append(entry)

    for topic, v in topic_map.items():
        if not v["total"]:
            continue
        p = _safe_pct(v["correct"], v["total"])
        entry = {"label": topic, "detail": f"{v['correct']}/{v['total']} correct"}
        if p >= 75:
            strengths.append(entry)
        elif p < 50 and topic != "General":
            improvements.append(entry)

    return {
        "success": True,
        "summary": summary,
        "attempts": attempt_list,
        "subjectPerformance": subject_perf,
        "topicPerformance": topic_perf,
        "difficultyBreakdown": diff_map,
        "scoreTrend": trend_data,
        "questionTypeBreakdown": qtype_breakdown,
        "strengths": strengths[:5],
        "improvements": improvements[:5],
        "improvementPct": improvement_pct,
    }


# ============================================================
# GET DETAILED FEEDBACK — per-question reasoning/feedback/improvement
# ============================================================

def _items_from_test_attempt(attempt: Dict[str, Any], test_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    """questionwise only has question_id + answers; question text/options
    live on the mockTests doc — same join /mock-tests/{id}/review already does."""
    q_map = {str(q.get("_id")): q for q in test_doc.get("questions", []) if q.get("_id") is not None}
    items = []
    for qw in attempt.get("questionwise", []):
        q = q_map.get(str(qw.get("question_id", "")), {})
        items.append({
            "question": q.get("questionText") or q.get("text", ""),
            "options": q.get("options", []),
            "studentAnswer": qw.get("student_answer"),
            "correctAnswer": qw.get("correct_answer", ""),
            "isCorrect": qw.get("status") == "correct",
        })
    return items


def _items_from_quiz_history(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """entry["questions"] shape comes from roadmap.py's _sanitize_quiz_for_history — already self-contained."""
    return [
        {
            "question": q.get("question", ""),
            "options": q.get("options", []),
            "studentAnswer": q.get("yourAnswer"),
            "correctAnswer": q.get("correctAnswer", ""),
            "isCorrect": q.get("isCorrect", False),
        }
        for q in entry.get("questions", [])
    ]


@router.post("/mock-tests/attempts/{source_type}/{attempt_id}/insight")
async def get_attempt_insight(
    source_type: str,
    attempt_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Generates (or returns the cached) per-question reasoning/feedback/
    improvement for one attempt. source_type is "practice_test" or
    "weekly_quiz" (see _quiz_attempt_id for that id's synthetic format)."""
    student_id = ObjectId(identity["user_id"])

    if source_type == "practice_test":
        if not ObjectId.is_valid(attempt_id):
            raise HTTPException(status_code=400, detail="Invalid attempt id")
        attempt = await db["testAttempts"].find_one({"_id": ObjectId(attempt_id), "student_id": student_id})
        if not attempt:
            raise HTTPException(status_code=404, detail="Attempt not found")

        test_doc = await db["mockTests"].find_one({"_id": attempt.get("test_id")}) or {}
        items = _items_from_test_attempt(attempt, test_doc)
        if not items:
            raise HTTPException(status_code=400, detail="No questions found for this attempt")

        cached = attempt.get("aiInsight")
        if not cached:
            result = await generate_attempt_insight(items, db=db, user_id=identity["user_id"])
            cached = result.get("insights", [])
            await db["testAttempts"].update_one({"_id": attempt["_id"]}, {"$set": {"aiInsight": cached}})

        questions = [{**item, **(cached[i] if i < len(cached) else {})} for i, item in enumerate(items)]
        return {
            "success": True,
            "attempt": {
                "testTitle": attempt.get("testTitle", ""),
                "subjectName": attempt.get("subjectName", ""),
                "percentage": attempt.get("percentage", 0),
                "scored": attempt.get("scored", 0),
                "totalMarks": attempt.get("totalMarks", 0),
                "date": attempt["submitted_at"].isoformat() if attempt.get("submitted_at") else None,
            },
            "questions": questions,
        }

    if source_type == "weekly_quiz":
        parsed = _parse_quiz_attempt_id(attempt_id)
        if not parsed:
            raise HTTPException(status_code=400, detail="Invalid attempt id")
        roadmap_id, week, submitted_at = parsed

        roadmap_doc = await db["selfLearnerRoadmaps"].find_one({"_id": ObjectId(roadmap_id), "user_id": student_id})
        if not roadmap_doc:
            raise HTTPException(status_code=404, detail="Roadmap not found")

        history = roadmap_doc.get("progress", {}).get("quizHistory", [])
        entry = next((h for h in history if h.get("week") == week and h.get("submittedAt") == submitted_at), None)
        if not entry:
            raise HTTPException(status_code=404, detail="Quiz attempt not found")

        items = _items_from_quiz_history(entry)
        if not items:
            raise HTTPException(status_code=400, detail="No questions found for this attempt")

        cached = entry.get("aiInsight")
        if not cached:
            result = await generate_attempt_insight(items, db=db, user_id=identity["user_id"])
            cached = result.get("insights", [])
            await db["selfLearnerRoadmaps"].update_one(
                {"_id": roadmap_doc["_id"], "user_id": student_id},
                {"$set": {"progress.quizHistory.$[entry].aiInsight": cached}},
                array_filters=[{"entry.week": week, "entry.submittedAt": submitted_at}],
            )

        questions = [{**item, **(cached[i] if i < len(cached) else {})} for i, item in enumerate(items)]
        return {
            "success": True,
            "attempt": {
                "testTitle": f"Week {week}: {roadmap_doc.get('subject', 'Roadmap')}",
                "subjectName": roadmap_doc.get("subject", ""),
                "percentage": entry.get("score", 0),
                "scored": entry.get("correctCount", 0),
                "totalMarks": entry.get("totalQuestions", 0),
                "date": submitted_at.isoformat(),
            },
            "questions": questions,
        }

    raise HTTPException(status_code=400, detail="Invalid sourceType")


@router.get("/mock-tests/{test_id}")
async def get_mock_test(
    test_id: str, identity: dict = Depends(get_current_identity), db: AsyncIOMotorDatabase = Depends(get_database)
):
    if not ObjectId.is_valid(test_id):
        raise HTTPException(status_code=400, detail="Invalid test_id")

    doc = await db["mockTests"].find_one({"_id": ObjectId(test_id), "student_id": ObjectId(identity["user_id"])})
    if not doc:
        raise HTTPException(status_code=404, detail="Mock test not found")

    return {"success": True, "test": serialize_mock_test(doc, include_answers=False)}


@router.delete("/mock-tests/{test_id}")
async def delete_mock_test(
    test_id: str, identity: dict = Depends(get_current_identity), db: AsyncIOMotorDatabase = Depends(get_database)
):
    if not ObjectId.is_valid(test_id):
        raise HTTPException(status_code=400, detail="Invalid test_id")

    result = await db["mockTests"].delete_one({"_id": ObjectId(test_id), "student_id": ObjectId(identity["user_id"])})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Mock test not found")

    return {"success": True, "message": "Mock test deleted"}


@router.post("/mock-tests/{test_id}/submit")
async def submit_test(
    test_id: str,
    payload: MockTestSubmitRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(test_id):
        raise HTTPException(status_code=400, detail="Invalid test_id")

    student_id = ObjectId(identity["user_id"])
    doc = await db["mockTests"].find_one({"_id": ObjectId(test_id), "student_id": student_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Mock test not found")

    answers = payload.answers

    questions = doc.get("questions", [])
    correct, wrong, skipped, scored, qwise = _evaluate_answers(
        questions, answers, doc.get("marksPerQuestion", 1),
        doc.get("negativeMarking", False), doc.get("negativeMarks", 0),
    )

    total_marks = doc.get("totalMarks", 0) or 0
    accuracy = round(correct / (correct + wrong) * 100, 1) if (correct + wrong) else 0.0
    percentage = round(scored / total_marks * 100, 1) if total_marks else 0.0

    now = datetime.now(timezone.utc)
    attempt_doc = {
        "student_id": student_id,
        "test_id": ObjectId(test_id),
        "testTitle": doc.get("subjectName") or doc.get("topic") or "Mock Test",
        "subjectName": doc.get("subjectName"),
        "subject_id": doc.get("subject_id"),
        "answers": answers,
        "questionwise": qwise,
        "correct": correct, "wrong": wrong, "skipped": skipped,
        "scored": scored, "totalMarks": total_marks,
        "accuracy": accuracy, "percentage": percentage,
        "submitted_at": now, "created_at": now,
    }
    result = await db["testAttempts"].insert_one(attempt_doc)
    attempt_id = result.inserted_id

    await db["mockTests"].update_one(
        {"_id": ObjectId(test_id)},
        {"$set": {"status": "submitted", "last_attempt": attempt_id, "updated_at": now}, "$inc": {"attempts_count": 1}},
    )

    return {
        "success": True,
        "attemptId": str(attempt_id),
        "result": {
            "scored": scored, "totalMarks": total_marks,
            "correct": correct, "wrong": wrong, "skipped": skipped,
            "accuracy": accuracy, "percentage": percentage,
            "submittedAt": now.isoformat(),
        },
    }


@router.get("/mock-tests/{test_id}/review")
async def review_test(
    test_id: str, identity: dict = Depends(get_current_identity), db: AsyncIOMotorDatabase = Depends(get_database)
):
    if not ObjectId.is_valid(test_id):
        raise HTTPException(status_code=400, detail="Invalid test_id")

    student_id = ObjectId(identity["user_id"])

    attempt = await db["testAttempts"].find_one(
        {"test_id": ObjectId(test_id), "student_id": student_id}, sort=[("submitted_at", -1)]
    )
    if not attempt:
        raise HTTPException(status_code=404, detail="No attempt found for this test")

    doc = await db["mockTests"].find_one({"_id": ObjectId(test_id), "student_id": student_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Mock test not found")

    questions = doc.get("questions", [])
    q_map = {str(q.get("_id")): q for q in questions}

    enriched_qwise = []
    for qw in attempt.get("questionwise", []):
        q = q_map.get(qw.get("question_id"), {})
        enriched_qwise.append({
            **qw,
            "questionText": q.get("questionText"),
            "options": q.get("options"),
            "explanation": q.get("explanation"),
            "type": q.get("type"),
            "marks": q.get("marks"),
            "difficulty": q.get("difficulty"),
        })

    attempt_out = _serialize_attempt(attempt)
    attempt_out["questionwise"] = enriched_qwise

    return {
        "success": True,
        "attempt": attempt_out,
        "questions": [serialize_question(q, include_answers=True) for q in questions],
        "testInfo": {
            "testTitle": doc.get("subjectName") or doc.get("topic") or "Mock Test",
            "subjectName": doc.get("subjectName"),
            "totalMarks": doc.get("totalMarks"),
        },
    }
