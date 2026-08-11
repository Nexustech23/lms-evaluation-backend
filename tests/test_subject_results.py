"""
subject_results.py has no request bodies (every endpoint is GET) — nothing
to retrofit with Pydantic. co-detailed-excel and download-detailed-excel's
ownership checks already have dedicated regression coverage in
test_security_fixes.py; this file covers the remaining GET endpoints.
"""

from bson import ObjectId

from tests.test_security_fixes import _get_institute_id, _register_institute_admin


async def test_subject_result_requires_auth(client):
    resp = await client.get(f"/subject/result/{ObjectId()}")
    assert resp.status_code == 401


async def test_subject_result_invalid_subject_id(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "SR Institute")
    resp = await institute.get("/subject/result/not-an-object-id")
    assert resp.status_code == 400


async def test_subject_result_not_found(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "SR Institute")
    resp = await institute.get(f"/subject/result/{ObjectId()}")
    assert resp.status_code == 404


async def test_faculty_subject_result_requires_faculty_assignment(superadmin_client, client_factory, test_db):
    institute = await _register_institute_admin(superadmin_client, client_factory, "SR Institute 2")
    institute_id = await _get_institute_id(institute)

    from tests.conftest import login, register
    from tests.test_security_fixes import PASSWORD

    faculty_email = "faculty-sr@test.local"
    await register(
        institute, role="faculty", fullName="SR Faculty", email=faculty_email,
        password=PASSWORD, school_id=str(ObjectId()),
    )
    faculty_client = await client_factory()
    await login(faculty_client, faculty_email, PASSWORD)

    subject = await test_db["subjectDetails"].insert_one({
        "institute_id": ObjectId(institute_id), "school_id": ObjectId(), "programme_id": ObjectId(),
        "faculty_id": ObjectId(),  # a *different* faculty
        "subject_name": "Bio", "subject_code": "BIO101", "semester": 1, "is_deleted": False,
    })

    resp = await faculty_client.get(f"/faculty/subject/result/{subject.inserted_id}")
    assert resp.status_code == 403


async def test_combined_result_requires_auth(client):
    resp = await client.get("/combined-result")
    assert resp.status_code == 401


async def test_combined_result_requires_batch_and_semester(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "SR Institute 3")
    resp = await institute.get("/combined-result")
    assert resp.status_code == 422


async def test_combined_result_requires_relative_grading_setup(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "SR Institute 3")
    resp = await institute.get("/combined-result", params={"batch_id": str(ObjectId()), "semester": "1"})
    assert resp.status_code == 404
    assert "grading configuration" in resp.json()["error"].lower()


async def test_combined_result_empty_after_grading_setup(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "SR Institute 3")
    grading_payload = {
        "a_plus_percentage": 10, "a_percentage": 10, "a_minus_percentage": 10,
        "b_plus_percentage": 10, "b_percentage": 10, "b_minus_percentage": 10,
        "c_plus_percentage": 10, "c_percentage": 10, "c_minus_percentage": 10,
        "d_percentage": 10, "u_percentage": 0,
    }
    await institute.post("/relative-grading", json=grading_payload)

    resp = await institute.get("/combined-result", params={"batch_id": str(ObjectId()), "semester": "1"})
    assert resp.status_code == 200
    assert resp.json()["data"] == []
