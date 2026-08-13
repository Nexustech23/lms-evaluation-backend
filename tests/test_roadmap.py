from unittest.mock import patch

from bson import ObjectId

from app.api.routers.roadmap import (
    _build_roadmap_pdf_html,
    _ground_new_roadmap,
    _resolve_grounding,
    _week_status_for_pdf,
)
from app.models.roadmap import create_roadmap_document
from app.services.rag.mongo_store import find_document_by_id
from app.services.rag.schemas import DocType, DocumentRecord, SourceFormat
from app.services.roadmap_ai import (
    _dominant_vark_style,
    _is_valid_mermaid_diagram,
    _normalize_difficulty,
    _normalize_vark,
    _split_question_counts,
)
from tests.test_security_fixes import _seed_and_login_user

_GEMINI_JSON_PATCH = "app.api.routers.roadmap.generate_gemini_json"
_CURRICULUM_PATCH = "app.api.routers.roadmap.generate_curriculum"
_CLAUDE_JSON_PATCH = "app.api.routers.roadmap.generate_claude_json"
_CLAUDE_TEXT_PATCH = "app.services.roadmap_ai.generate_claude_text"
_PDF_RENDER_PATCH = "app.api.routers.roadmap.render_html_to_pdf"
_FIND_BY_ID_PATCH = "app.services.rag.mongo_store.find_document_by_id"
_FIND_BY_SUBJECT_PATCH = "app.services.rag.mongo_store.find_document_for_subject"
_RAG_RETRIEVE_PATCH = "app.services.rag.retrieval.router.retrieve"
_RAG_SHOULD_USE_PATCH = "app.services.rag.retrieval.router.should_use_rag"


async def _learner(client_factory, test_db):
    return await _seed_and_login_user(test_db, client_factory, role=7, name="Roadmap Learner")


async def _seed_roadmap(test_db, user_id: str) -> str:
    """Inserts a minimal one-week roadmap doc directly (bypassing the AI
    generation job) so notes/quiz endpoints have real data to operate on."""
    doc = create_roadmap_document(user_id, {
        "subject": "Python",
        "goal": "Interview Prep",
        "weeks": [{
            "week": 1,
            "title": "Basics",
            "introDescription": "Getting started.",
            "subtopics": [{
                "title": "Variables",
                "summary": "What variables are.",
                "keyPoints": ["Assignment", "Types"],
                "difficulty": "Beginner",
            }],
            "practiceQuestions": [],
        }],
        "unlockedWeeks": [1],
    })
    result = await test_db["selfLearnerRoadmaps"].insert_one(doc)
    return str(result.inserted_id)


async def test_assess_requires_auth(client):
    resp = await client.post("/api/self-learner/roadmap/assess", json={"subject": "Math"})
    assert resp.status_code == 401


async def test_assess_rejects_blank_subject(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post("/api/self-learner/roadmap/assess", json={"subject": "  "})
    assert resp.status_code == 422


async def test_assess_rejects_too_long_subject(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post("/api/self-learner/roadmap/assess", json={"subject": "x" * 201})
    assert resp.status_code == 422


async def test_assess_success(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    with patch(_GEMINI_JSON_PATCH, return_value=([{"question": "2+2?"}], {}, False)):
        resp = await learner.post("/api/self-learner/roadmap/assess", json={"subject": "Math"})
    assert resp.status_code == 200
    assert resp.json()["questions"] == [{"question": "2+2?"}]


async def test_create_roadmap_requires_subject(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post("/api/self-learner/roadmap", json={})
    assert resp.status_code == 422


async def test_create_roadmap_rejects_too_long_goal(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post(
        "/api/self-learner/roadmap", json={"subject": "Math", "goal": "x" * 501}
    )
    assert resp.status_code == 422


async def test_create_roadmap_queues_job(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    with patch(_CURRICULUM_PATCH, return_value=({"weeks": []}, {}, False)):
        resp = await learner.post("/api/self-learner/roadmap", json={"subject": "Math"})
    assert resp.status_code == 202
    assert resp.json()["status"] == "processing"


async def test_update_subtopic_requires_key(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.patch(f"/api/self-learner/roadmap/{ObjectId()}/subtopic", json={})
    assert resp.status_code == 422


async def test_update_subtopic_invalid_roadmap_id(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.patch(
        "/api/self-learner/roadmap/not-valid/subtopic", json={"subtopic_key": "1-0"}
    )
    assert resp.status_code == 400


async def test_update_subtopic_not_found(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.patch(
        f"/api/self-learner/roadmap/{ObjectId()}/subtopic", json={"subtopic_key": "1-0"}
    )
    assert resp.status_code == 404


async def test_submit_quiz_invalid_roadmap_id(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post(
        "/api/self-learner/roadmap/not-valid/quiz/submit", json={"week": 1, "answers": {}}
    )
    assert resp.status_code == 400


async def test_submit_quiz_rejects_non_int_week(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post(
        f"/api/self-learner/roadmap/{ObjectId()}/quiz/submit", json={"week": "not-a-number", "answers": {}}
    )
    assert resp.status_code == 422


async def test_submit_quiz_not_found(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post(
        f"/api/self-learner/roadmap/{ObjectId()}/quiz/submit", json={"week": 1, "answers": {}}
    )
    assert resp.status_code == 404


async def test_get_roadmaps_empty(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.get("/api/self-learner/roadmap")
    assert resp.status_code == 200
    assert resp.json() == []


# ============================================================
# VARK HELPERS (pure functions — no DB/AI needed)
# ============================================================

def test_normalize_vark_defaults_missing_to_25():
    assert _normalize_vark(None, None, None, None) == {
        "visual": 25, "auditory": 25, "reading": 25, "kinesthetic": 25,
    }


def test_normalize_vark_clamps_negative_to_zero():
    assert _normalize_vark(-10, 50, 0, 60)["visual"] == 0


def test_dominant_vark_style_picks_highest():
    assert _dominant_vark_style({"visual": 10, "auditory": 70, "reading": 10, "kinesthetic": 10}) == "auditory"


def test_dominant_vark_style_breaks_ties_by_style_order():
    # visual comes first in VARK_STYLES, so a tie should resolve to it.
    assert _dominant_vark_style({"visual": 25, "auditory": 25, "reading": 25, "kinesthetic": 25}) == "visual"


def test_normalize_difficulty_defaults_and_rejects_unknown():
    assert _normalize_difficulty(None) == "Moderate"
    assert _normalize_difficulty("easy") == "Easy"
    assert _normalize_difficulty("Nonsense") == "Moderate"


# ============================================================
# VARK NOTES ENDPOINT
# ============================================================

async def test_notes_requires_auth(client):
    resp = await client.get(f"/api/self-learner/roadmap/{ObjectId()}/notes")
    assert resp.status_code == 401


async def test_notes_generates_and_caches_per_style_and_difficulty(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    roadmap_id = await _seed_roadmap(test_db, str((await test_db["users"].find_one({"role": 7}))["_id"]))

    fake_notes = {"summary": "Variables hold values.", "detailedExplanation": [], "keyPoints": []}
    fake_usage = type("Usage", (), {"input_tokens": 10, "output_tokens": 20})()

    with patch(_CLAUDE_JSON_PATCH, return_value=(fake_notes, fake_usage, False)) as mock_gen:
        # Auditory-dominant, Difficult
        resp = await learner.get(
            f"/api/self-learner/roadmap/{roadmap_id}/notes",
            params={"week": 1, "subtopic_idx": 0, "visual": 10, "auditory": 70, "reading": 10, "kinesthetic": 10, "difficulty": "difficult"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cached"] is False
        assert body["style"] == "Auditory"
        assert body["difficulty"] == "Difficult"
        assert body["notes"] == fake_notes
        assert mock_gen.call_count == 1

        # Same blend/difficulty again -> served from cache, AI not called again
        resp2 = await learner.get(
            f"/api/self-learner/roadmap/{roadmap_id}/notes",
            params={"week": 1, "subtopic_idx": 0, "visual": 10, "auditory": 70, "reading": 10, "kinesthetic": 10, "difficulty": "difficult"},
        )
        assert resp2.status_code == 200
        assert resp2.json()["cached"] is True
        assert mock_gen.call_count == 1

        # Different dominant style -> a fresh generation, separate cache slot
        resp3 = await learner.get(
            f"/api/self-learner/roadmap/{roadmap_id}/notes",
            params={"week": 1, "subtopic_idx": 0, "visual": 80, "auditory": 10, "reading": 5, "kinesthetic": 5, "difficulty": "difficult"},
        )
        assert resp3.status_code == 200
        assert resp3.json()["cached"] is False
        assert resp3.json()["style"] == "Visual"
        assert mock_gen.call_count == 2


async def test_notes_week_locked(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    roadmap_id = await _seed_roadmap(test_db, str((await test_db["users"].find_one({"role": 7}))["_id"]))

    resp = await learner.get(f"/api/self-learner/roadmap/{roadmap_id}/notes", params={"week": 2})
    assert resp.status_code == 403


# ============================================================
# MERMAID CONCEPT DIAGRAM VALIDATION
# ============================================================

_VALID_DIAGRAM = (
    'graph TD\n'
    '    A["Start: Define the Problem"] --> B["Gather Requirements"]\n'
    '    B --> C["Design Solution"]\n'
    '    C -->|"Approved"| D["Implement"]'
)

# The actual documented production failure case (see CLAUDE.md's roadmap_ai_todo.md
# §21 Post-Pivot notes): an unquoted round-bracket node whose label contains its
# own parentheses. Confirming the validator correctly rejects it is the whole
# point of this test — a looser regex could easily let this back through.
_BROKEN_DIAGRAM = 'graph TD\n    A(International Political Economy (IPE)) --> B[Trade]'


def test_mermaid_validator_accepts_valid_diagram():
    assert _is_valid_mermaid_diagram(_VALID_DIAGRAM) is True


def test_mermaid_validator_rejects_unquoted_parens_label():
    assert _is_valid_mermaid_diagram(_BROKEN_DIAGRAM) is False


def test_mermaid_validator_rejects_wrong_diagram_type():
    assert _is_valid_mermaid_diagram('mindmap\n  root((Topic))') is False


def test_mermaid_validator_rejects_empty_or_none():
    assert _is_valid_mermaid_diagram("") is False
    assert _is_valid_mermaid_diagram(None) is False


async def test_notes_visual_dominant_repairs_invalid_diagram(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    roadmap_id = await _seed_roadmap(test_db, str((await test_db["users"].find_one({"role": 7}))["_id"]))

    fake_notes = {"summary": "...", "conceptDiagram": _BROKEN_DIAGRAM}
    fake_usage = type("Usage", (), {"input_tokens": 10, "output_tokens": 20})()

    with patch(_CLAUDE_JSON_PATCH, return_value=(fake_notes, fake_usage, False)), \
         patch(_CLAUDE_TEXT_PATCH, return_value=(_VALID_DIAGRAM, fake_usage)) as mock_repair:
        resp = await learner.get(
            f"/api/self-learner/roadmap/{roadmap_id}/notes",
            params={"week": 1, "subtopic_idx": 0, "visual": 80, "auditory": 10, "reading": 5, "kinesthetic": 5},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["style"] == "Visual"
        assert body["notes"]["conceptDiagram"] == _VALID_DIAGRAM
        assert mock_repair.call_count == 1


async def test_notes_visual_dominant_drops_diagram_when_repair_also_fails(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    roadmap_id = await _seed_roadmap(test_db, str((await test_db["users"].find_one({"role": 7}))["_id"]))

    fake_notes = {"summary": "...", "conceptDiagram": _BROKEN_DIAGRAM}
    fake_usage = type("Usage", (), {"input_tokens": 10, "output_tokens": 20})()

    with patch(_CLAUDE_JSON_PATCH, return_value=(fake_notes, fake_usage, False)), \
         patch(_CLAUDE_TEXT_PATCH, return_value=(_BROKEN_DIAGRAM, fake_usage)):
        resp = await learner.get(
            f"/api/self-learner/roadmap/{roadmap_id}/notes",
            params={"week": 1, "subtopic_idx": 0, "visual": 80, "auditory": 10, "reading": 5, "kinesthetic": 5},
        )
        assert resp.status_code == 200
        assert resp.json()["notes"]["conceptDiagram"] == ""


# ============================================================
# AUTO TEST — question-count splitting (pure function)
# ============================================================

def test_split_question_counts_sums_to_total_even_split():
    counts = _split_question_counts(34, 33, 33, 10)
    assert sum(counts.values()) == 10


def test_split_question_counts_sums_to_total_various_percentages():
    # A representative sweep, not exhaustive — the invariant (sums to total)
    # is what actually matters, checked across several awkward splits that
    # plain truncation would under-count.
    for mcq, subj, prac, total in [
        (100, 0, 0, 7), (0, 100, 0, 13), (33, 33, 34, 1), (60, 20, 20, 9),
        (70, 15, 15, 20), (1, 1, 98, 3), (25, 25, 50, 6),
    ]:
        counts = _split_question_counts(mcq, subj, prac, total)
        assert sum(counts.values()) == total, f"mcq={mcq} subj={subj} prac={prac} total={total} -> {counts}"


def test_split_question_counts_100_percent_mcq_puts_everything_in_mcq():
    counts = _split_question_counts(100, 0, 0, 10)
    assert counts == {"mcq": 10, "subjective": 0, "practical": 0}


# ============================================================
# AUTO TEST — generate / resume / submit
# ============================================================

_MCQ_Q = {
    "type": "mcq", "question": "2+2?", "options": ["3", "4", "5", "6"], "answer": 1,
    "explanation": "Basic arithmetic.", "difficulty": "Easy", "topic": "Arithmetic",
}
_SUBJ_Q = {
    "type": "subjective", "question": "Explain variables.",
    "modelAnswer": "A variable is a named storage location.",
    "explanation": "Look for: naming, storage, mutability.", "difficulty": "Easy", "topic": "Variables",
}


async def test_generate_auto_test_rejects_percentages_not_summing_to_100(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    roadmap_id = await _seed_roadmap(test_db, str((await test_db["users"].find_one({"role": 7}))["_id"]))

    resp = await learner.post(
        f"/api/self-learner/roadmap/{roadmap_id}/quiz/generate",
        json={"week": 1, "mcq_percent": 50, "subjective_percent": 20, "practical_percent": 20, "question_count": 10},
    )
    assert resp.status_code == 422


async def test_generate_auto_test_strips_answer_keys_and_caches_on_week(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    user_id = str((await test_db["users"].find_one({"role": 7}))["_id"])
    roadmap_id = await _seed_roadmap(test_db, user_id)

    questions = [_MCQ_Q, _SUBJ_Q]
    fake_usage = type("Usage", (), {"prompt_token_count": 10, "candidates_token_count": 20})()

    with patch(_GEMINI_JSON_PATCH, return_value=(questions, fake_usage, False)):
        resp = await learner.post(
            f"/api/self-learner/roadmap/{roadmap_id}/quiz/generate",
            json={"week": 1, "mcq_percent": 50, "subjective_percent": 50, "practical_percent": 0, "question_count": 2},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["questions"]) == 2
    for q in body["questions"]:
        assert "answer" not in q
        assert "modelAnswer" not in q
        assert "explanation" not in q
    assert body["config"]["mcqPercent"] == 50

    # It's now stored on the week doc — GET /quiz (resume) should return it.
    resume = await learner.get(f"/api/self-learner/roadmap/{roadmap_id}/quiz", params={"week": 1})
    assert resume.status_code == 200
    assert len(resume.json()["questions"]) == 2


async def test_resume_auto_test_returns_null_when_none_generated(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    roadmap_id = await _seed_roadmap(test_db, str((await test_db["users"].find_one({"role": 7}))["_id"]))

    resp = await learner.get(f"/api/self-learner/roadmap/{roadmap_id}/quiz", params={"week": 1})
    assert resp.status_code == 200
    assert resp.json() == {"questions": None, "config": None}


async def test_resume_auto_test_never_generates_on_its_own(client_factory, test_db):
    """GET /quiz must be resume-only — it should never call the AI itself."""
    learner = await _learner(client_factory, test_db)
    roadmap_id = await _seed_roadmap(test_db, str((await test_db["users"].find_one({"role": 7}))["_id"]))

    with patch(_GEMINI_JSON_PATCH) as mock_gen:
        resp = await learner.get(f"/api/self-learner/roadmap/{roadmap_id}/quiz", params={"week": 1})
    assert resp.status_code == 200
    mock_gen.assert_not_called()


async def _seed_roadmap_with_auto_test(test_db, user_id: str, questions) -> str:
    roadmap_id = await _seed_roadmap(test_db, user_id)
    await test_db["selfLearnerRoadmaps"].update_one(
        {"_id": ObjectId(roadmap_id)},
        {"$set": {"weeks.0.autoTest": {
            "config": {"mcqPercent": 50, "subjectivePercent": 50, "practicalPercent": 0, "questionCount": 2, "customPrompt": None},
            "questions": questions,
        }}},
    )
    return roadmap_id


async def test_submit_auto_test_mixed_mcq_and_subjective_grading(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    user_id = str((await test_db["users"].find_one({"role": 7}))["_id"])
    roadmap_id = await _seed_roadmap_with_auto_test(test_db, user_id, [_MCQ_Q, _SUBJ_Q])

    fake_usage = type("Usage", (), {"prompt_token_count": 10, "candidates_token_count": 20})()
    grading_response = [{"score": 80, "feedback": "Good, but missed mutability."}]

    with patch(_GEMINI_JSON_PATCH, return_value=(grading_response, fake_usage, False)) as mock_grade:
        resp = await learner.post(
            f"/api/self-learner/roadmap/{roadmap_id}/quiz/submit",
            json={"week": 1, "answers": {"0": 1, "1": "A variable stores a value under a name."}},
        )
    assert resp.status_code == 200
    body = resp.json()
    # MCQ scored 100 (correct), subjective scored 80 -> mean = 90
    assert body["score"] == 90
    assert body["passed"] is True
    assert mock_grade.call_count == 1
    results = {r["questionIdx"]: r for r in body["results"]}
    assert results[0]["score"] == 100
    assert results[1]["score"] == 80
    assert results[1]["feedback"] == "Good, but missed mutability."


async def test_submit_auto_test_unanswered_open_ended_scored_zero_without_ai_call(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    user_id = str((await test_db["users"].find_one({"role": 7}))["_id"])
    roadmap_id = await _seed_roadmap_with_auto_test(test_db, user_id, [_MCQ_Q, _SUBJ_Q])

    with patch(_GEMINI_JSON_PATCH) as mock_grade:
        resp = await learner.post(
            f"/api/self-learner/roadmap/{roadmap_id}/quiz/submit",
            # Q0 (MCQ) wrong, Q1 (subjective) left blank
            json={"week": 1, "answers": {"0": 0}},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["score"] == 0  # both questions score 0
    mock_grade.assert_not_called()  # no open-ended answers -> no AI call needed
    results = {r["questionIdx"]: r for r in body["results"]}
    assert results[1]["score"] == 0
    assert results[1]["feedback"] == "No answer provided."


async def test_submit_auto_test_not_generated_yet(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    roadmap_id = await _seed_roadmap(test_db, str((await test_db["users"].find_one({"role": 7}))["_id"]))

    resp = await learner.post(
        f"/api/self-learner/roadmap/{roadmap_id}/quiz/submit",
        json={"week": 1, "answers": {}},
    )
    assert resp.status_code == 400


# ============================================================
# PDF EXPORT
# ============================================================

def test_week_status_for_pdf_locked_completed_in_progress():
    doc = {
        "unlockedWeeks": [1, 2],
        "progress": {"passedQuizzes": {"1": 90}},
    }
    assert _week_status_for_pdf(doc, 1) == "Completed"
    assert _week_status_for_pdf(doc, 2) == "In Progress"
    assert _week_status_for_pdf(doc, 3) == "Locked"


def test_build_roadmap_pdf_html_escapes_xss_subject():
    doc = create_roadmap_document("000000000000000000000000", {
        "subject": '<script>alert("xss")</script>',
        "goal": "Test",
        "weeks": [{
            "week": 1, "title": "Intro", "introDescription": "<img src=x onerror=alert(1)>",
            "subtopics": [{"title": "A"}],
        }],
        "unlockedWeeks": [1],
    })
    html_out = _build_roadmap_pdf_html(doc)
    assert "<script>alert" not in html_out
    assert "<img src=x onerror" not in html_out
    assert "&lt;script&gt;" in html_out


async def test_download_pdf_requires_auth(client):
    resp = await client.get(f"/api/self-learner/roadmap/{ObjectId()}/pdf")
    assert resp.status_code == 401


async def test_download_pdf_invalid_roadmap_id(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.get("/api/self-learner/roadmap/not-valid/pdf")
    assert resp.status_code == 400


async def test_download_pdf_not_found(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.get(f"/api/self-learner/roadmap/{ObjectId()}/pdf")
    assert resp.status_code == 404


async def test_download_pdf_success(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    roadmap_id = await _seed_roadmap(test_db, str((await test_db["users"].find_one({"role": 7}))["_id"]))

    fake_pdf_bytes = b"%PDF-1.4 fake pdf content"
    with patch(_PDF_RENDER_PATCH, return_value=fake_pdf_bytes) as mock_render:
        resp = await learner.get(f"/api/self-learner/roadmap/{roadmap_id}/pdf")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.content == fake_pdf_bytes
    assert mock_render.call_count == 1


# ============================================================
# RAG GROUNDING RESOLUTION
# ============================================================

_FAKE_RECORD = DocumentRecord(
    id="doc-abc-123", filename="syllabus.pdf", source_format=SourceFormat.PDF,
    doc_type=DocType.STRUCTURED, course_title="Python Fundamentals",
)


def _fake_retrieval_result(context_text="grounded content"):
    result = type("RetrievalResult", (), {
        "context_text": context_text, "source_nodes": [], "confidence": 0.9, "doc_id": _FAKE_RECORD.id,
    })()
    return result


async def test_find_document_by_id_found_and_not_found(test_db):
    # Real stored docs always have both "_id" and "id" (save_document_record
    # does `doc = asdict(record)` — which already includes "id" as a
    # dataclass field — then separately sets doc["_id"] = record.id).
    await test_db.courseMaterials.insert_one({
        "_id": "doc-xyz", "id": "doc-xyz", "filename": "book.pdf", "source_format": "pdf",
        "doc_type": "structured", "course_code": None, "course_title": "Algebra",
        "content_hash": None, "created_at": None,
    })
    found = await find_document_by_id(test_db, "doc-xyz")
    assert found is not None
    assert found.id == "doc-xyz"
    assert found.course_title == "Algebra"

    missing = await find_document_by_id(test_db, "does-not-exist")
    assert missing is None

    empty = await find_document_by_id(test_db, "")
    assert empty is None


async def test_resolve_grounding_trusts_grounded_doc_id_when_present_and_valid(test_db):
    doc = {"grounded_doc_id": "doc-abc-123"}
    with patch(_FIND_BY_ID_PATCH, return_value=_FAKE_RECORD) as mock_by_id, \
         patch(_FIND_BY_SUBJECT_PATCH) as mock_by_subject, \
         patch(_RAG_RETRIEVE_PATCH, return_value=_fake_retrieval_result("trusted grounding")), \
         patch(_RAG_SHOULD_USE_PATCH, return_value=True):
        result = await _resolve_grounding(test_db, doc, "Python", "some query", "user-1")

    assert result == "trusted grounding"
    mock_by_id.assert_called_once_with(test_db, "doc-abc-123")
    mock_by_subject.assert_not_called()  # precise id hit -> no need for the fallback match


async def test_resolve_grounding_falls_back_when_no_grounded_doc_id(test_db):
    doc = {}  # no grounded_doc_id at all — legacy/ungrounded-at-creation roadmap
    with patch(_FIND_BY_ID_PATCH) as mock_by_id, \
         patch(_FIND_BY_SUBJECT_PATCH, return_value=_FAKE_RECORD) as mock_by_subject, \
         patch(_RAG_RETRIEVE_PATCH, return_value=_fake_retrieval_result("subject-matched grounding")), \
         patch(_RAG_SHOULD_USE_PATCH, return_value=True):
        result = await _resolve_grounding(test_db, doc, "Python", "some query", "user-1")

    assert result == "subject-matched grounding"
    mock_by_id.assert_not_called()
    mock_by_subject.assert_called_once()


async def test_resolve_grounding_falls_back_when_grounded_doc_id_is_stale(test_db):
    """The material behind grounded_doc_id was deleted since the roadmap was
    created — find_document_by_id correctly returns None, and that must
    trigger the subject-match fallback rather than giving up ungrounded."""
    doc = {"grounded_doc_id": "deleted-doc-id"}
    with patch(_FIND_BY_ID_PATCH, return_value=None) as mock_by_id, \
         patch(_FIND_BY_SUBJECT_PATCH, return_value=_FAKE_RECORD) as mock_by_subject, \
         patch(_RAG_RETRIEVE_PATCH, return_value=_fake_retrieval_result("fallback grounding")), \
         patch(_RAG_SHOULD_USE_PATCH, return_value=True):
        result = await _resolve_grounding(test_db, doc, "Python", "some query", "user-1")

    assert result == "fallback grounding"
    mock_by_id.assert_called_once()
    mock_by_subject.assert_called_once()


async def test_resolve_grounding_returns_none_when_nothing_matches(test_db):
    doc = {}
    with patch(_FIND_BY_SUBJECT_PATCH, return_value=None):
        result = await _resolve_grounding(test_db, doc, "Obscure Subject", "query", "user-1")
    assert result is None


async def test_ground_new_roadmap_returns_context_and_resolved_doc_id(test_db):
    with patch(_FIND_BY_SUBJECT_PATCH, return_value=_FAKE_RECORD), \
         patch(_RAG_RETRIEVE_PATCH, return_value=_fake_retrieval_result("new roadmap grounding")), \
         patch(_RAG_SHOULD_USE_PATCH, return_value=True):
        context_text, doc_id = await _ground_new_roadmap(test_db, "Python", "query", "user-1")

    assert context_text == "new roadmap grounding"
    assert doc_id == "doc-abc-123"


async def test_ground_new_roadmap_returns_none_none_when_no_match(test_db):
    with patch(_FIND_BY_SUBJECT_PATCH, return_value=None):
        context_text, doc_id = await _ground_new_roadmap(test_db, "Obscure Subject", "query", "user-1")
    assert context_text is None
    assert doc_id is None
