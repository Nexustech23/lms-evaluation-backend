from bson import ObjectId

from tests.conftest import login, register
from tests.test_security_fixes import PASSWORD, _get_institute_id, _register_institute_admin


async def _institute_and_student(superadmin_client, client_factory, test_db):
    institute = await _register_institute_admin(superadmin_client, client_factory, "SS Institute")
    institute_id = await _get_institute_id(institute)

    student_email = "student-ss@test.local"
    await register(
        institute, role="institute_student", fullName="Student SS", email="unused-ss@test.local",
        password=PASSWORD, school_id=str(ObjectId()), programme_id=str(ObjectId()), roll_no="SS001",
        contact_no="9999999999", enrollment_no="ENRSS001",
    )
    student_doc = await test_db["studentDetails"].find_one({"roll_no": "SS001"})
    student_client = await client_factory()
    await login(student_client, student_doc["college_email"], PASSWORD)
    return institute, institute_id, student_client, student_doc


async def test_link_student_subjects_requires_auth(client):
    resp = await client.post("/link-student-subjects", json={"subject_ids": [str(ObjectId())]})
    assert resp.status_code == 401


async def test_link_student_subjects_rejects_empty_list(superadmin_client, client_factory, test_db):
    _, _, student_client, _ = await _institute_and_student(superadmin_client, client_factory, test_db)
    resp = await student_client.post("/link-student-subjects", json={"subject_ids": []})
    assert resp.status_code == 422


async def test_link_student_subjects_success(superadmin_client, client_factory, test_db):
    institute, institute_id, student_client, student_doc = await _institute_and_student(
        superadmin_client, client_factory, test_db
    )

    subject = await test_db["subjectDetails"].insert_one({
        "institute_id": ObjectId(institute_id), "school_id": student_doc["school_id"],
        "programme_id": student_doc["programme_id"], "subject_name": "Physics",
        "subject_code": "PHY101", "semester": 1, "is_deleted": False,
    })

    resp = await student_client.post("/link-student-subjects", json={"subject_ids": [str(subject.inserted_id)]})
    assert resp.status_code == 200
    assert "1 subjects linked" in resp.json()["message"]

    relations = await test_db["StudentSubjectRelationModel"].find({}).to_list(None)
    assert len(relations) == 1
    assert relations[0]["subject_id"] == subject.inserted_id

    # linking the same subject again is a no-op, not a duplicate
    resp2 = await student_client.post("/link-student-subjects", json={"subject_ids": [str(subject.inserted_id)]})
    assert resp2.status_code == 200
    assert "0 subjects linked" in resp2.json()["message"]
    relations_after = await test_db["StudentSubjectRelationModel"].find({}).to_list(None)
    assert len(relations_after) == 1


async def test_student_academic_filters_requires_institute_student_role(superadmin_client):
    resp = await superadmin_client.get("/student-academic-filters")
    assert resp.status_code == 403


async def test_student_groups_requires_institute_or_faculty_role(superadmin_client, client_factory, test_db):
    _, _, student_client, _ = await _institute_and_student(superadmin_client, client_factory, test_db)
    resp = await student_client.get("/student-groups")
    assert resp.status_code == 403


async def test_enrolled_students_for_institute(superadmin_client, client_factory, test_db):
    institute, _, _, student_doc = await _institute_and_student(superadmin_client, client_factory, test_db)
    resp = await institute.get("/enrolled-students")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["students"]) == 1
    assert body["students"][0]["roll_no"] == "SS001"
