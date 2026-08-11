from unittest.mock import patch

from bson import ObjectId

from tests.conftest import login, register
from tests.test_security_fixes import PASSWORD, _register_institute_admin

_EXTRACT_PATCH = "app.api.routers.exams.extract_and_patch_question_paper_text"


async def _faculty_client(superadmin_client, client_factory, test_db):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Exams Institute")
    faculty_email = "faculty-exams@test.local"
    await register(
        institute, role="faculty", fullName="Exams Faculty", email=faculty_email,
        password=PASSWORD, school_id=str(ObjectId()),
    )
    faculty_doc = await test_db["facultyDetails"].find_one({})
    fc = await client_factory()
    await login(fc, faculty_email, PASSWORD)
    return fc, faculty_doc["_id"]


async def test_create_folder_requires_auth(client):
    resp = await client.post("/createSaveFolder", data={"folderName": "Midterm"})
    assert resp.status_code == 401


async def test_create_folder_success(superadmin_client, client_factory, test_db):
    fc, faculty_id = await _faculty_client(superadmin_client, client_factory, test_db)
    resp = await fc.post("/createSaveFolder", data={
        "folderName": "Midterm", "school": str(ObjectId()), "programme": str(ObjectId()),
        "subject_id": str(ObjectId()), "semester": "1", "examtype": "Midterm Exam",
        "covered_cos": "[]", "weightage": "10",  # real clients always send both
    })
    assert resp.status_code == 200
    assert "folder_id" in resp.json()


async def test_upload_question_paper_rejects_missing_field(superadmin_client, client_factory, test_db):
    fc, faculty_id = await _faculty_client(superadmin_client, client_factory, test_db)
    folder = await test_db["newsavedDocs"].insert_one({"faculty_id": faculty_id, "folder_name": "F1"})
    resp = await fc.post(f"/upload-question-paper/{folder.inserted_id}", json={
        "questionpaper_url": "https://cdn.test/q.pdf", "fileId": "f1",
    })
    assert resp.status_code == 422


async def test_upload_question_paper_rejects_unauthorized_folder(superadmin_client, client_factory, test_db):
    fc, faculty_id = await _faculty_client(superadmin_client, client_factory, test_db)
    folder = await test_db["newsavedDocs"].insert_one({"faculty_id": ObjectId(), "folder_name": "F1"})
    resp = await fc.post(f"/upload-question-paper/{folder.inserted_id}", json={
        "questionpaper_url": "https://cdn.test/q.pdf", "fileId": "f1", "filename": "q.pdf", "no_of_question": 5,
    })
    assert resp.status_code == 404


async def test_upload_question_paper_success(superadmin_client, client_factory, test_db):
    fc, faculty_id = await _faculty_client(superadmin_client, client_factory, test_db)
    folder = await test_db["newsavedDocs"].insert_one({"faculty_id": faculty_id, "folder_name": "F1"})

    with patch(_EXTRACT_PATCH, return_value=None):
        resp = await fc.post(f"/upload-question-paper/{folder.inserted_id}", json={
            "questionpaper_url": "https://cdn.test/q.pdf", "fileId": "f1", "filename": "q.pdf", "no_of_question": 5,
        })
    assert resp.status_code == 200
    assert resp.json()["exam"]["question_paper"]["no_of_questions"] == 5


async def test_rename_folder_requires_id_alias(superadmin_client):
    resp = await superadmin_client.put("/rename-folder", json={"newFoldername": "New Name"})
    assert resp.status_code == 422


async def test_rename_folder_success(superadmin_client, test_db):
    folder = await test_db["newsavedDocs"].insert_one({"folder_name": "Old Name"})
    resp = await superadmin_client.put("/rename-folder", json={
        "_id": str(folder.inserted_id), "newFoldername": "New Name",
    })
    assert resp.status_code == 200
    updated = await test_db["newsavedDocs"].find_one({"_id": folder.inserted_id})
    assert updated["folder_name"] == "New Name"


async def test_delete_folder_success(superadmin_client, test_db):
    folder = await test_db["newsavedDocs"].insert_one({"folder_name": "To Delete"})
    resp = await superadmin_client.request("DELETE", "/delete-folder", json={"_id": str(folder.inserted_id)})
    assert resp.status_code == 200
    remaining = await test_db["newsavedDocs"].find_one({"_id": folder.inserted_id})
    assert remaining is None


async def test_set_archive_status_rejects_non_bool(superadmin_client, test_db):
    folder = await test_db["newsavedDocs"].insert_one({"folder_name": "F1"})
    resp = await superadmin_client.post(
        f"/set-archive-status/{folder.inserted_id}", json={"is_archived": "maybe"}
    )
    assert resp.status_code == 422


async def test_set_archive_status_success(superadmin_client, test_db):
    folder = await test_db["newsavedDocs"].insert_one({"folder_name": "F1"})
    resp = await superadmin_client.post(f"/set-archive-status/{folder.inserted_id}", json={"is_archived": True})
    assert resp.status_code == 200
    updated = await test_db["newsavedDocs"].find_one({"_id": folder.inserted_id})
    assert updated["is_archived"] is True
