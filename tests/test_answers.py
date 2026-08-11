from bson import ObjectId

from tests.conftest import login, register
from tests.test_security_fixes import PASSWORD, _register_institute_admin


async def _faculty_with_folder(superadmin_client, client_factory, test_db):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Answers Institute")
    faculty_email = "faculty-answers@test.local"
    await register(
        institute, role="faculty", fullName="Answers Faculty", email=faculty_email,
        password=PASSWORD, school_id=str(ObjectId()),
    )
    faculty_doc = await test_db["facultyDetails"].find_one({})
    faculty_client = await client_factory()
    await login(faculty_client, faculty_email, PASSWORD)

    folder = await test_db["newsavedDocs"].insert_one({
        "faculty_id": faculty_doc["_id"], "folder_name": "Answers Folder",
    })
    return faculty_client, str(folder.inserted_id), faculty_doc["_id"]


async def test_upload_answer_script_requires_auth(client):
    resp = await client.post(f"/upload-answer-script/{ObjectId()}", json={
        "answer_script_url": "https://cdn.test/a.pdf", "fileId": "f1", "filename": "a.pdf",
    })
    assert resp.status_code == 401


async def test_upload_answer_script_rejects_missing_field(superadmin_client, client_factory, test_db):
    faculty_client, folder_id, _ = await _faculty_with_folder(superadmin_client, client_factory, test_db)
    resp = await faculty_client.post(f"/upload-answer-script/{folder_id}", json={
        "answer_script_url": "https://cdn.test/a.pdf", "fileId": "f1",
    })
    assert resp.status_code == 422


async def test_upload_answer_script_success(superadmin_client, client_factory, test_db):
    faculty_client, folder_id, _ = await _faculty_with_folder(superadmin_client, client_factory, test_db)
    resp = await faculty_client.post(f"/upload-answer-script/{folder_id}", json={
        "answer_script_url": "https://cdn.test/a.pdf", "fileId": "f1", "filename": "a.pdf",
    })
    assert resp.status_code == 200
    assert resp.json()["filename"] == "a.pdf"


async def test_rename_file_rejects_invalid_id(superadmin_client):
    resp = await superadmin_client.put("/rename-file", json={"answer_id": "not-valid", "newFilename": "x.pdf"})
    assert resp.status_code == 400


async def test_rename_file_requires_new_filename(superadmin_client):
    resp = await superadmin_client.put("/rename-file", json={"answer_id": str(ObjectId())})
    assert resp.status_code == 422


async def test_rename_file_success(superadmin_client, test_db):
    answer = await test_db["answerDetails"].insert_one({"filename": "old.pdf"})
    resp = await superadmin_client.put("/rename-file", json={
        "answer_id": str(answer.inserted_id), "newFilename": "new.pdf",
    })
    assert resp.status_code == 200
    updated = await test_db["answerDetails"].find_one({"_id": answer.inserted_id})
    assert updated["filename"] == "new.pdf"


async def test_delete_file_not_found(superadmin_client):
    resp = await superadmin_client.request("DELETE", "/delete-file", json={"answer_id": str(ObjectId())})
    assert resp.status_code == 404


async def test_delete_file_success(superadmin_client, test_db):
    answer = await test_db["answerDetails"].insert_one({"filename": "x.pdf"})
    resp = await superadmin_client.request("DELETE", "/delete-file", json={"answer_id": str(answer.inserted_id)})
    assert resp.status_code == 200
    remaining = await test_db["answerDetails"].find_one({"_id": answer.inserted_id})
    assert remaining is None


async def test_get_answer_scripts(superadmin_client, client_factory, test_db):
    faculty_client, folder_id, faculty_id = await _faculty_with_folder(superadmin_client, client_factory, test_db)
    await test_db["answerDetails"].insert_one({
        "exam_id": ObjectId(folder_id), "faculty_id": faculty_id, "filename": "s1.pdf",
    })
    resp = await faculty_client.get(f"/get-answer-scripts/{folder_id}")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


async def test_save_self_evaluation(superadmin_client, test_db):
    answer = await test_db["answerDetails"].insert_one({"filename": "x.pdf"})
    resp = await superadmin_client.post("/save-self-evaluation", json={
        "answer_id": str(answer.inserted_id),
        "questionwise_marking": [{"ai_awarded_marks": 5, "grace_marks": 1}, {"ai_awarded_marks": 3, "grace_marks": 0}],
    })
    assert resp.status_code == 200
    assert resp.json()["total_final_marks"] == 9


async def test_manual_marks_entry_rejects_non_positive_max_marks(superadmin_client, client_factory, test_db):
    faculty_client, folder_id, _ = await _faculty_with_folder(superadmin_client, client_factory, test_db)
    resp = await faculty_client.post(f"/manual-marks-entry/{folder_id}", json={"max_marks": 0, "entries": []})
    assert resp.status_code == 422


async def test_manual_marks_entry_success_skips_bad_rows(superadmin_client, client_factory, test_db):
    faculty_client, folder_id, _ = await _faculty_with_folder(superadmin_client, client_factory, test_db)
    resp = await faculty_client.post(f"/manual-marks-entry/{folder_id}", json={
        "max_marks": 10,
        "entries": [
            {"student_id": "S001", "marks": 8},
            {"student_id": "", "marks": 5},  # missing student_id -> skipped
            {"student_id": "S002", "marks": 999},  # out of range -> skipped
        ],
    })
    assert resp.status_code == 200
    assert resp.json()["saved_count"] == 1
