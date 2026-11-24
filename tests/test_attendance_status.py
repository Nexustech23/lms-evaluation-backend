"""Step A1: absent / malpractice status on manual marks entry."""
import pytest
from bson import ObjectId

from app.services.manual_marks import prepare_manual_marks
from tests.test_answers import _faculty_with_folder


async def _folder_with_subject(superadmin_client, client_factory, test_db):
    """The usual faculty + exam, plus a real (non-batch) subject so result-revision bookkeeping passes."""
    client, folder_id, faculty_id = await _faculty_with_folder(superadmin_client, client_factory, test_db)
    subject = await test_db["subjectDetails"].insert_one({"faculty_id": faculty_id, "semester": 1})
    await test_db["newsavedDocs"].update_one(
        {"_id": ObjectId(folder_id)}, {"$set": {"subject_id": subject.inserted_id}})
    return client, folder_id


def _rubric():
    cos = [{"co_code": "CO1", "marks": 4}, {"co_code": "CO2", "marks": 6}]
    return {"questionEvaluationDetails": [
        {"maxMarks": 10, "minMarks": 0, "guidelines": "", "parameters": [], "cos": cos},
        {"maxMarks": 5, "minMarks": 0, "guidelines": "", "parameters": [], "cos": [{"co_code": "CO1", "marks": 5}]},
    ], "totalMarks": 15}


def _payload(*entries, max_marks=None):
    class Payload:
        pass
    Payload.entries = list(entries)
    Payload.max_marks = max_marks
    return Payload


def _present_entry(sid="s1", **extra):
    return {"student_id": sid, "question_marks": [{"question_no": 1, "marks": 7}, {"question_no": 2, "marks": 3}], **extra}


def test_present_is_the_default_and_marks_are_unchanged():
    prepared = prepare_manual_marks(_payload(_present_entry()), _rubric())[0]
    assert prepared["attendance_status"] == "present"
    assert prepared["status_note"] is None
    assert prepared["total_final_marks"] == 10
    assert prepared["total_max_marks"] == 15


@pytest.mark.parametrize("status", ["absent", "malpractice"])
def test_absent_and_malpractice_store_zero_in_the_usual_shape(status):
    entry = {"student_id": "s1", "attendance_status": status, "status_note": "  seen copying  "}
    prepared = prepare_manual_marks(_payload(entry), _rubric())[0]
    assert prepared["attendance_status"] == status
    assert prepared["status_note"] == "seen copying"
    assert prepared["total_final_marks"] == 0 and prepared["total_ai_marks"] == 0
    assert prepared["total_max_marks"] == 15
    questions = prepared["questionwise_marking"]
    assert [q["final_marks"] for q in questions] == [0, 0]
    assert [q["max_marks"] for q in questions] == [10, 5]
    assert all(co["ai_marks"] == 0 for q in questions for co in q["cos"])


def test_absent_without_a_rubric_uses_the_flat_maximum():
    entry = {"student_id": "s1", "attendance_status": "absent"}
    prepared = prepare_manual_marks(_payload(entry, max_marks=20), None)[0]
    assert prepared["total_final_marks"] == 0 and prepared["total_max_marks"] == 20
    assert prepared["questionwise_marking"] == []


@pytest.mark.parametrize("status", ["absent", "malpractice"])
def test_marks_are_rejected_with_a_non_present_status(status):
    with pytest.raises(ValueError, match="marks cannot be entered"):
        prepare_manual_marks(_payload(_present_entry(attendance_status=status)), _rubric())
    with pytest.raises(ValueError, match="marks cannot be entered"):
        prepare_manual_marks(_payload({"student_id": "s1", "attendance_status": status, "marks": 5}, max_marks=10), None)


def test_unknown_status_and_bad_note_are_rejected():
    with pytest.raises(ValueError, match="attendance_status"):
        prepare_manual_marks(_payload(_present_entry(attendance_status="late")), _rubric())
    with pytest.raises(ValueError, match="status_note"):
        prepare_manual_marks(_payload(_present_entry(status_note="x" * 501)), _rubric())


def test_mixed_batch_keeps_each_students_status():
    prepared = prepare_manual_marks(_payload(
        _present_entry("a"), {"student_id": "b", "attendance_status": "absent"},
        {"student_id": "c", "attendance_status": "malpractice"},
    ), _rubric())
    assert [(p["student_id"], p["attendance_status"], p["total_final_marks"]) for p in prepared] == [
        ("a", "present", 10), ("b", "absent", 0), ("c", "malpractice", 0)]


async def test_endpoint_saves_status_and_present_resets_it(superadmin_client, client_factory, test_db):
    client, folder_id = await _folder_with_subject(superadmin_client, client_factory, test_db)
    url = f"/manual-marks-entry/{folder_id}"
    resp = await client.post(url, json={"max_marks": 10, "entries": [
        {"student_id": "S1", "marks": 8},
        {"student_id": "S2", "attendance_status": "absent"},
        {"student_id": "S3", "attendance_status": "malpractice", "status_note": "phone"},
    ]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["saved_count"] == 3
    docs = {d["student_name"]: d async for d in test_db["answerDetails"].find({})}
    assert docs["S1"]["attendance_status"] == "present" and docs["S1"]["total_final_marks"] == 8
    assert docs["S2"]["attendance_status"] == "absent" and docs["S2"]["total_final_marks"] == 0
    assert docs["S3"]["attendance_status"] == "malpractice" and docs["S3"]["status_note"] == "phone"

    # The teacher corrects S2: they did sit the exam after all.
    resp = await client.post(url, json={"max_marks": 10, "entries": [{"student_id": "S2", "marks": 6}]})
    assert resp.status_code == 200, resp.text
    s2 = await test_db["answerDetails"].find_one({"student_name": "S2"})
    assert s2["attendance_status"] == "present" and s2["status_note"] is None and s2["total_final_marks"] == 6


async def test_endpoint_rejects_marks_for_an_absent_student(superadmin_client, client_factory, test_db):
    client, folder_id, _ = await _faculty_with_folder(superadmin_client, client_factory, test_db)
    resp = await client.post(f"/manual-marks-entry/{folder_id}", json={"max_marks": 10, "entries": [
        {"student_id": "S1", "attendance_status": "absent", "marks": 5}]})
    assert resp.status_code == 400
    assert await test_db["answerDetails"].count_documents({}) == 0
