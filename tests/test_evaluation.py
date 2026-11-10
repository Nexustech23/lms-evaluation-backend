from bson import ObjectId

from tests.conftest import login, register
from tests.test_security_fixes import PASSWORD, _register_institute_admin

VALID_QUESTION = {
    "maxMarks": 10,
    "minMarks": 0,
    "guidelines": "Be thorough",
    "parameters": [{"name": "Clarity", "percentage": 100}],
    "cos": [{"co_code": "CO1", "marks": 10}],
}


async def _faculty_client(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Eval Institute")
    faculty_email = "faculty-eval@test.local"
    await register(
        institute, role="faculty", fullName="Eval Faculty", email=faculty_email,
        password=PASSWORD, school_id=str(ObjectId()),
    )
    fc = await client_factory()
    await login(fc, faculty_email, PASSWORD)
    return fc


async def test_get_evaluation_details_requires_auth(client):
    resp = await client.get(f"/evaluation-details/{ObjectId()}")
    assert resp.status_code == 401


async def test_get_evaluation_details_not_found(superadmin_client, client_factory):
    fc = await _faculty_client(superadmin_client, client_factory)
    resp = await fc.get(f"/evaluation-details/{ObjectId()}")
    assert resp.status_code == 404


async def test_save_evaluation_details_rejects_empty_questions(superadmin_client, client_factory):
    fc = await _faculty_client(superadmin_client, client_factory)
    resp = await fc.post(f"/evaluation-details/{ObjectId()}", json={
        "questionEvaluationDetails": [], "totalMarks": 10,
    })
    assert resp.status_code == 422


async def test_save_evaluation_details_rejects_zero_total_marks(superadmin_client, client_factory):
    fc = await _faculty_client(superadmin_client, client_factory)
    resp = await fc.post(f"/evaluation-details/{ObjectId()}", json={
        "questionEvaluationDetails": [VALID_QUESTION], "totalMarks": 0,
    })
    assert resp.status_code == 422


async def test_save_evaluation_details_rejects_min_greater_than_max(superadmin_client, client_factory, test_db):
    fc = await _faculty_client(superadmin_client, client_factory)
    faculty = await test_db["facultyDetails"].find_one({})
    exam = await test_db["newsavedDocs"].insert_one({"faculty_id": faculty["_id"], "covered_cos": ["CO1"]})
    bad_question = {**VALID_QUESTION, "minMarks": 20}
    resp = await fc.post(f"/evaluation-details/{exam.inserted_id}", json={
        "questionEvaluationDetails": [bad_question], "totalMarks": 10,
    })
    assert resp.status_code == 400


async def test_save_and_get_evaluation_details_roundtrip(superadmin_client, client_factory, test_db):
    fc = await _faculty_client(superadmin_client, client_factory)
    faculty = await test_db["facultyDetails"].find_one({})
    exam = await test_db["newsavedDocs"].insert_one({"faculty_id": faculty["_id"], "covered_cos": ["CO1"]})
    folder_id = str(exam.inserted_id)

    saved = await fc.post(f"/evaluation-details/{folder_id}", json={
        "questionEvaluationDetails": [VALID_QUESTION], "totalMarks": 10,
    })
    assert saved.status_code == 200
    assert saved.json()["totalMarks"] == 10

    fetched = await fc.get(f"/evaluation-details/{folder_id}")
    assert fetched.status_code == 200
    assert fetched.json()["evaluation"]["totalMarks"] == 10

    # saving again updates in place rather than duplicating
    resaved = await fc.post(f"/evaluation-details/{folder_id}", json={
        "questionEvaluationDetails": [{**VALID_QUESTION, "guidelines": "Updated"}], "totalMarks": 10,
    })
    assert resaved.status_code == 200
    fetched_again = await fc.get(f"/evaluation-details/{folder_id}")
    assert fetched_again.json()["evaluation"]["questionEvaluationDetails"][0]["guidelines"] == "Updated"
