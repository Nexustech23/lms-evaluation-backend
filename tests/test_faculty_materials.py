from bson import ObjectId

from tests.conftest import login, register
from tests.test_security_fixes import PASSWORD, _get_institute_id, _register_institute_admin

VALID_MATERIAL = {
    "title": "Chapter 1 Notes", "type": "Notes", "file_url": "https://cdn.test/notes.pdf",
    "filename": "notes.pdf",
}


async def _faculty_with_subject(superadmin_client, client_factory, test_db):
    institute = await _register_institute_admin(superadmin_client, client_factory, "FM Institute")
    institute_id = await _get_institute_id(institute)

    faculty_email = "faculty-fm@test.local"
    school_id = str(ObjectId())
    await register(
        institute, role="faculty", fullName="FM Faculty", email=faculty_email,
        password=PASSWORD, school_id=school_id,
    )
    faculty_doc = await test_db["facultyDetails"].find_one({"institute_id": ObjectId(institute_id)})
    faculty_client = await client_factory()
    await login(faculty_client, faculty_email, PASSWORD)

    subject = await test_db["subjectDetails"].insert_one({
        "institute_id": ObjectId(institute_id), "school_id": ObjectId(school_id),
        "programme_id": ObjectId(), "faculty_id": faculty_doc["_id"],
        "subject_name": "Chemistry", "subject_code": "CHEM101", "semester": 1, "is_deleted": False,
    })
    return faculty_client, str(subject.inserted_id)


async def test_create_material_requires_auth(client):
    resp = await client.post("/faculty/materials", json={**VALID_MATERIAL, "subject_id": str(ObjectId())})
    assert resp.status_code == 401


async def test_create_material_rejects_invalid_type(superadmin_client, client_factory, test_db):
    faculty_client, subject_id = await _faculty_with_subject(superadmin_client, client_factory, test_db)
    resp = await faculty_client.post("/faculty/materials", json={
        **VALID_MATERIAL, "type": "NotAType", "subject_id": subject_id,
    })
    assert resp.status_code == 422


async def test_create_material_rejects_missing_field(superadmin_client, client_factory, test_db):
    faculty_client, subject_id = await _faculty_with_subject(superadmin_client, client_factory, test_db)
    payload = {**VALID_MATERIAL, "subject_id": subject_id}
    del payload["filename"]
    resp = await faculty_client.post("/faculty/materials", json=payload)
    assert resp.status_code == 422


async def test_create_material_rejects_subject_not_assigned_to_faculty(superadmin_client, client_factory, test_db):
    faculty_client, _ = await _faculty_with_subject(superadmin_client, client_factory, test_db)
    other_subject = await test_db["subjectDetails"].insert_one({
        "institute_id": ObjectId(), "school_id": ObjectId(), "programme_id": ObjectId(),
        "faculty_id": ObjectId(), "subject_name": "Other", "subject_code": "OTH101",
        "semester": 1, "is_deleted": False,
    })
    resp = await faculty_client.post("/faculty/materials", json={
        **VALID_MATERIAL, "subject_id": str(other_subject.inserted_id),
    })
    assert resp.status_code == 403


async def test_create_and_list_material_success(superadmin_client, client_factory, test_db):
    faculty_client, subject_id = await _faculty_with_subject(superadmin_client, client_factory, test_db)
    created = await faculty_client.post("/faculty/materials", json={**VALID_MATERIAL, "subject_id": subject_id})
    assert created.status_code == 201

    listed = await faculty_client.get("/faculty/materials")
    assert listed.status_code == 200
    assert len(listed.json()["materials"]) == 1
    assert listed.json()["materials"][0]["title"] == "Chapter 1 Notes"


async def test_student_interaction_rejects_invalid_status(superadmin_client):
    # FastAPI validates the request body (422) before the handler body even
    # runs, so this fires regardless of the caller's role.
    resp = await superadmin_client.post(
        f"/student/materials/{ObjectId()}/interaction", json={"status": "not-a-status"}
    )
    assert resp.status_code == 422
