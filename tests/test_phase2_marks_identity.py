"""Phase 2 regressions using in-memory answers and HTTP dependencies only."""

from copy import deepcopy

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.api.routers import subject_results
from app.api.routers.subject_results import _extract_student_id, _get_subject_results, _get_combined_results
from app.services.review_marks import validate_review_marks
from app.utils.transcript_generation_helper import generate_transcript_for_semester
from tests.test_answer_access import isolated


def question(**changes):
    return {"question_no": 1, "max_marks": 10, "ai_awarded_marks": 3,
            "grace_marks": 0, **changes}


INVALID_REVIEWS = [
    [], [question(ai_awarded_marks=999)], [question(ai_awarded_marks="five")],
    [question(ai_awarded_marks=-1)], [question(grace_marks=8)], [question(grace_marks=-4)],
    [question(ai_awarded_marks=True)], [question(ai_awarded_marks="NaN")],
    [question(grace_marks="Infinity")], [question(grace_marks=None)],
    [question(max_marks=1000)], [question(max_marks=0)], [question(max_marks="NaN")],
    [question(question_no=1.5)], [question(question_no=True)], [question(question_no=0)],
    [question(question_no=2)], [question(), question()], [{"ai_awarded_marks": 3}],
    [question(cos=[{"co_code": "CO1", "max_marks": 10, "ai_marks": 3}])],
]


@pytest.mark.parametrize("marking", INVALID_REVIEWS)
async def test_invalid_review_cannot_change_marks_or_enqueue(isolated, marking):
    before = deepcopy(isolated.db["answerDetails"].documents)
    response = await isolated.client.post("/save-self-evaluation", headers={"x-test-user": "owner"},
        json={"answer_id": isolated.answer, "questionwise_marking": marking})
    assert response.status_code == 400, response.text
    assert isolated.db["answerDetails"].documents == before
    assert not isolated.db["answerDetails"].writes
    isolated.enqueue.assert_not_awaited()


@pytest.mark.parametrize("ai,grace,final", [(3, 2, 5), (3, -3, 0), (10, 0, 10), ("3.5", "0.5", 4)])
async def test_valid_review_recomputes_totals_and_ignores_forged_final(isolated, ai, grace, final):
    response = await isolated.client.post("/save-self-evaluation", headers={"x-test-user": "owner"},
        json={"answer_id": isolated.answer, "questionwise_marking": [
            question(ai_awarded_marks=ai, grace_marks=grace, final_marks=999)]})
    assert response.status_code == 200, response.text
    answer = isolated.db["answerDetails"].documents[0]
    assert response.json()["total_final_marks"] == answer["total_final_marks"] == final
    assert answer["questionwise_marking"][0]["final_marks"] == final
    assert answer["reviewed_by_professor"] is True
    isolated.enqueue.assert_awaited_once()


@pytest.mark.parametrize("broken", [None, [], [question(max_marks="NaN")]])
async def test_missing_or_corrupt_saved_limits_block_review(isolated, broken):
    isolated.db["answerDetails"].documents[0]["questionwise_marking"] = broken
    response = await isolated.client.post("/save-self-evaluation", headers={"x-test-user": "owner"},
        json={"answer_id": isolated.answer, "questionwise_marking": [question()]})
    assert response.status_code == 400
    assert not isolated.db["answerDetails"].writes
    isolated.enqueue.assert_not_awaited()


@pytest.mark.parametrize("cos", [
    [], [{"co_code": "CO2", "max_marks": 10, "ai_marks": 3}],
    [{"co_code": "CO1", "max_marks": 20, "ai_marks": 3}],
    [{"co_code": "CO1", "max_marks": 10, "ai_marks": "NaN"}],
    [{"co_code": "CO1", "max_marks": 10, "ai_marks": 11}],
    [{"co_code": "CO1", "max_marks": 10, "ai_marks": 3, "grace_marks": -4}],
    [{"co_code": "CO1", "max_marks": 10, "ai_marks": 2}],
    [{"co_code": "CO1", "max_marks": 10, "ai_marks": 3}] * 2,
])
async def test_invalid_co_review_rejected(isolated, cos):
    isolated.db["answerDetails"].documents[0]["questionwise_marking"][0]["cos"] = [
        {"co_code": "CO1", "max_marks": 10, "ai_marks": 3}]
    response = await isolated.client.post("/save-self-evaluation", headers={"x-test-user": "owner"},
        json={"answer_id": isolated.answer, "questionwise_marking": [question(cos=cos)]})
    assert response.status_code == 400, response.text
    assert not isolated.db["answerDetails"].writes
    isolated.enqueue.assert_not_awaited()


async def test_zero_question_and_co_marks_are_saved_consistently(isolated):
    cos = [{"co_code": "CO1", "max_marks": 10, "ai_marks": 3, "grace_marks": -3,
            "final_co_marks": 999}]
    isolated.db["answerDetails"].documents[0]["questionwise_marking"][0]["cos"] = deepcopy(cos)
    response = await isolated.client.post("/save-self-evaluation", headers={"x-test-user": "owner"},
        json={"answer_id": isolated.answer, "questionwise_marking": [question(grace_marks=-3, cos=cos)]})
    assert response.status_code == 200, response.text
    saved = isolated.db["answerDetails"].documents[0]
    assert saved["total_final_marks"] == 0
    assert saved["questionwise_marking"][0]["cos"][0]["final_co_marks"] == 0


def test_total_cannot_exceed_saved_answer_maximum():
    with pytest.raises(ValueError, match="Final total"):
        validate_review_marks([question(ai_awarded_marks=8)],
            {"questionwise_marking": [question()], "total_max_marks": 5})


@pytest.mark.parametrize("filename,expected", [
    ("MANUAL-DS-001", "DS-001"), ("MANUAL-CS-001", "CS-001"),
    ("MANUAL-DS.001", "DS.001"), ("DS-001.pdf", "DS-001"),
    ("CS-001.PDF", "CS-001"), ("S001.pdf", "S001"), (" S001 ", "S001"),
])
def test_complete_filename_identity(filename, expected):
    assert _extract_student_id(filename) == expected


class Cursor:
    def __init__(self, documents):
        self.documents = documents

    def sort(self, *args):
        return self

    def __aiter__(self):
        async def rows():
            for doc in self.documents:
                yield deepcopy(doc)
        return rows()


class Collection:
    def __init__(self, documents=()):
        self.documents = list(documents)

    def find(self, query, *args):
        def matches(doc):
            for key, value in query.items():
                actual = doc.get(key)
                if isinstance(value, dict):
                    if "$ne" in value and actual == value["$ne"]:
                        return False
                    if "$in" in value and actual not in value["$in"]:
                        return False
                elif actual != value:
                    return False
            return True
        return Cursor([doc for doc in self.documents if matches(doc)])

    async def find_one(self, query):
        async for doc in self.find(query):
            return doc
        return None


@pytest.fixture
def results_db():
    institute, subject, exam, batch = [ObjectId() for _ in range(4)]
    db = {
        "subjectDetails": Collection([{"_id": subject, "institute_id": institute,
            "subject_name": "Data Science", "credits": 4, "semester": 5,
            "batch_id": batch, "is_deleted": False}]),
        "newsavedDocs": Collection([{"_id": exam, "subject_id": subject, "weightage": 100}]),
        "answerDetails": Collection(), "relativeGradings": Collection(),
        "importedMarks": Collection(),
        "academic_transcripts": Collection(),
    }
    return db, institute, str(subject), exam, batch


def answer(exam, filename, final=4, **changes):
    return {"_id": ObjectId(), "exam_id": exam, "filename": filename,
        "total_final_marks": final, "total_ai_marks": 8, "total_max_marks": 10,
        "questionwise_marking": [], **changes}


@pytest.mark.parametrize("prefix,suffix", [("MANUAL-", ""), ("", ".pdf")])
async def test_hyphenated_students_keep_separate_marks(results_db, prefix, suffix):
    db, institute, subject, exam, _ = results_db
    db["answerDetails"].documents = [answer(exam, f"{prefix}DS-001{suffix}", 4),
                                     answer(exam, f"{prefix}CS-001{suffix}", 8)]
    result, status = await _get_subject_results(db, subject, institute)
    assert status == 200
    students = {s["student_id"]: s for s in result["students"]}
    assert set(students) == {"DS-001", "CS-001"}
    assert students["DS-001"]["results"][str(exam)]["final_marks"] == 4
    assert students["CS-001"]["results"][str(exam)]["final_marks"] == 8


async def test_explicit_id_links_same_student_across_exams(results_db):
    db, institute, subject, exam, _ = results_db
    other_exam = ObjectId()
    db["newsavedDocs"].documents[0]["weightage"] = 50
    db["newsavedDocs"].documents.append({"_id": other_exam, "subject_id": ObjectId(subject), "weightage": 50})
    db["answerDetails"].documents = [answer(exam, "different-file.pdf", 4, student_id="DS-001"),
                                     answer(other_exam, "MANUAL-DS-001", 8)]
    result, status = await _get_subject_results(db, subject, institute)
    assert status == 200
    assert result["total_students"] == 1
    student = result["students"][0]
    assert student["student_id"] == "DS-001"
    assert student["composite_percentage"] == 60
    assert all(student["results"].values())


@pytest.mark.parametrize("final,expected", [(0, 0), (None, 8), (4, 4)])
async def test_zero_final_marks_preserved(results_db, final, expected):
    db, institute, subject, exam, _ = results_db
    db["answerDetails"].documents = [answer(exam, "S001.pdf", final)]
    result, status = await _get_subject_results(db, subject, institute)
    assert status == 200
    assert result["students"][0]["results"][str(exam)]["final_marks"] == expected
    assert result["students"][0]["composite_percentage"] == expected * 10


@pytest.mark.parametrize("filename", ["", None])
async def test_missing_identity_blocks_report(results_db, filename):
    db, institute, subject, exam, _ = results_db
    db["answerDetails"].documents = [answer(exam, filename)]
    result, status = await _get_subject_results(db, subject, institute)
    assert status == 409
    assert "identity" in result["error"]


async def test_duplicate_student_exam_is_not_silently_overwritten(results_db):
    db, institute, subject, exam, _ = results_db
    db["answerDetails"].documents = [answer(exam, "S001.pdf", 4), answer(exam, "MANUAL-S001", 8)]
    result, status = await _get_subject_results(db, subject, institute)
    assert status == 409
    assert "duplicate" in result["error"]


async def test_manual_entry_stores_full_explicit_identity(isolated):
    response = await isolated.client.post(f"/manual-marks-entry/{isolated.exam}",
        headers={"x-test-user": "owner"}, json={"max_marks": 10,
            "entries": [{"student_id": "DS-001", "marks": 4}, {"student_id": "CS-001", "marks": 8}]})
    assert response.status_code == 200
    assert response.json()["saved_count"] == 2
    assert {doc["student_id"] for doc in isolated.db["answerDetails"].documents[1:]} == {"DS-001", "CS-001"}


async def test_manual_rubric_entry_also_stores_explicit_identity(isolated):
    isolated.db["evaluationDetails"].documents.append({
        "exam_id": ObjectId(isolated.exam), "totalMarks": 10,
        "questionEvaluationDetails": [{"maxMarks": 10, "cos": []}],
    })
    response = await isolated.client.post(f"/manual-marks-entry/{isolated.exam}",
        headers={"x-test-user": "owner"}, json={"entries": [
            {"student_id": "DS-001", "question_marks": [{"question_no": 1, "marks": 4}]}]})
    assert response.status_code == 200, response.text
    assert response.json()["saved_count"] == 1
    assert isolated.db["answerDetails"].documents[-1]["student_id"] == "DS-001"


async def test_review_cannot_drop_one_saved_question(isolated):
    isolated.db["answerDetails"].documents[0]["questionwise_marking"].append(question(question_no=2))
    response = await isolated.client.post("/save-self-evaluation", headers={"x-test-user": "owner"},
        json={"answer_id": isolated.answer, "questionwise_marking": [question()]})
    assert response.status_code == 400
    assert not isolated.db["answerDetails"].writes
    isolated.enqueue.assert_not_awaited()


async def test_multi_question_review_normalizes_numeric_strings(isolated):
    isolated.db["answerDetails"].documents[0]["questionwise_marking"] = [
        question(max_marks=5), question(question_no=2, max_marks=5)]
    response = await isolated.client.post("/save-self-evaluation", headers={"x-test-user": "owner"},
        json={"answer_id": isolated.answer, "questionwise_marking": [
            question(question_no="2", max_marks="5", ai_awarded_marks="5"),
            question(max_marks=5, ai_awarded_marks=3, grace_marks=-3)]})
    assert response.status_code == 200, response.text
    assert response.json()["total_final_marks"] == 5


async def test_combined_results_propagate_duplicate_conflict(results_db, monkeypatch):
    db, institute, _, exam, batch = results_db
    db["relativeGradings"].documents.append({"university_id": institute})
    monkeypatch.setattr(subject_results, "build_grading_config", lambda _: {})
    db["answerDetails"].documents = [answer(exam, "S001.pdf"), answer(exam, "MANUAL-S001")]
    result, status = await _get_combined_results(db, institute, str(batch), "5")
    assert status == 409
    assert "duplicate" in result["error"]


async def test_transcript_generation_blocks_identity_conflict(results_db):
    db, institute, _, exam, batch = results_db
    db["answerDetails"].documents = [answer(exam, "S001.pdf"), answer(exam, "MANUAL-S001")]
    with pytest.raises(HTTPException) as caught:
        await generate_transcript_for_semester(db, institute, str(batch), "5")
    assert caught.value.status_code == 409


async def test_zero_marks_survive_transcript_generation(results_db):
    db, institute, _, exam, batch = results_db
    db["answerDetails"].documents = [answer(exam, "DS-001.pdf", 0)]
    documents, summary = await generate_transcript_for_semester(db, institute, str(batch), "5")
    assert len(documents) == 1
    assert documents[0]["student_id"] == "DS-001"
    assert documents[0]["subjects"][0]["marks"] == 0
