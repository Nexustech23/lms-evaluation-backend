"""
Regression suite for Phase 1's security fixes. Each test proves a specific
gap identified in the FastAPI codebase analysis is actually closed — this
file is a hard gate: if it fails, Phase 1 regressed and Phase 2 (Pydantic
retrofit) should not proceed until it's green again.
"""

import json
import uuid

from bson import ObjectId

from app.core.security import hash_password
from app.models.subject import create_subject_document
from app.models.user import create_user_document
from app.services.job_store import set_job

from tests.conftest import login, register

PASSWORD = "TestPass123!"


async def _seed_and_login_user(test_db, client_factory, role: int, name: str = "Test User"):
    """Bypasses /register (which requires an already-authenticated caller
    for every role, and leaves tutor/self_learner pending approval) purely
    to get a second/third distinct logged-in user quickly. The registration
    flow itself is covered separately in test_auth.py."""
    email = f"user-{uuid.uuid4().hex[:10]}@test.local"
    user_doc = create_user_document(
        {"fullName": name, "email": email, "role": role, "is_active": True}, hash_password(PASSWORD)
    )
    await test_db["users"].insert_one(user_doc)
    client = await client_factory()
    await login(client, email, PASSWORD)
    return client


async def _register_institute_admin(superadmin_client, client_factory, name: str):
    email = f"institute-{uuid.uuid4().hex[:10]}@test.local"
    await register(
        superadmin_client,
        role="institute",
        fullName=f"{name} Admin",
        email=email,
        password=PASSWORD,
        institute={"institute_name": name},
    )
    client = await client_factory()
    await login(client, email, PASSWORD)
    return client


async def _get_institute_id(client) -> str:
    resp = await client.get("/me")
    assert resp.status_code == 200
    return resp.json()["user"]["institute_id"]


# ============================================================
# 1. co-detailed-excel / download-detailed-excel ownership scoping
# ============================================================

async def test_co_detailed_excel_denies_cross_institute_access(superadmin_client, client_factory, test_db):
    institute_a = await _register_institute_admin(superadmin_client, client_factory, "Institute A")
    institute_b = await _register_institute_admin(superadmin_client, client_factory, "Institute B")

    institute_a_id = await _get_institute_id(institute_a)

    subject_doc = create_subject_document(
        {
            "institute_id": institute_a_id,
            "school_id": str(ObjectId()),
            "programme_id": str(ObjectId()),
            "subject_name": "Test Subject",
            "subject_code": "TS101",
        },
        created_by=institute_a_id,
    )
    result = await test_db["subjectDetails"].insert_one(subject_doc)
    subject_id = str(result.inserted_id)

    # Institute B must not be able to pull institute A's CO report.
    denied = await institute_b.get(f"/co-detailed-excel/{subject_id}")
    assert denied.status_code == 403

    # Institute A (the owner) must clear the ownership check — whatever
    # happens next in report generation is out of scope for this test.
    allowed = await institute_a.get(f"/co-detailed-excel/{subject_id}")
    assert allowed.status_code != 403


async def test_download_detailed_excel_denies_cross_faculty_access(superadmin_client, client_factory, test_db):
    institute_a = await _register_institute_admin(superadmin_client, client_factory, "Institute A")
    institute_a_id = await _get_institute_id(institute_a)

    faculty_a_email = f"faculty-a-{uuid.uuid4().hex[:8]}@test.local"
    await register(
        institute_a,
        role="faculty",
        fullName="Faculty A",
        email=faculty_a_email,
        password=PASSWORD,
        school_id=str(ObjectId()),
    )
    faculty_a_doc = await test_db["facultyDetails"].find_one({"institute_id": ObjectId(institute_a_id)})
    faculty_a_id = faculty_a_doc["_id"]

    folder_result = await test_db["newsavedDocs"].insert_one({
        "faculty_id": faculty_a_id, "folder_name": "Test Folder",
    })
    folder_id = str(folder_result.inserted_id)
    await test_db["answerDetails"].insert_one({
        "exam_id": folder_result.inserted_id, "filename": "test.pdf",
        "student_name": "Student One", "questionwise_marking": [],
    })

    # A different faculty (institute B) must not reach institute A's folder.
    institute_b = await _register_institute_admin(superadmin_client, client_factory, "Institute B")
    faculty_b_email = f"faculty-b-{uuid.uuid4().hex[:8]}@test.local"
    await register(
        institute_b,
        role="faculty",
        fullName="Faculty B",
        email=faculty_b_email,
        password=PASSWORD,
        school_id=str(ObjectId()),
    )
    faculty_b = await _login_new(client_factory, faculty_b_email)

    denied = await faculty_b.get(f"/download-detailed-excel/{folder_id}")
    assert denied.status_code == 404
    assert "unauthorized" in denied.json().get("error", "").lower()

    # The owning faculty succeeds end-to-end.
    faculty_a = await _login_new(client_factory, faculty_a_email)
    allowed = await faculty_a.get(f"/download-detailed-excel/{folder_id}")
    assert allowed.status_code == 200


async def _login_new(client_factory, email: str):
    client = await client_factory()
    await login(client, email, PASSWORD)
    return client


# ============================================================
# 2. Job-status endpoints scoped to the requesting user
# ============================================================

async def test_roadmap_job_status_scoped_to_owner(client_factory, test_db):
    from app.api.routers.roadmap import ROADMAP_JOB_PREFIX

    owner = await _seed_and_login_user(test_db, client_factory, role=7, name="Learner A")
    other = await _seed_and_login_user(test_db, client_factory, role=7, name="Learner B")

    owner_user_doc = await test_db["users"].find_one({"fullName": "Learner A"})
    job_id = str(uuid.uuid4())
    await set_job(ROADMAP_JOB_PREFIX, job_id, {"status": "processing", "user_id": str(owner_user_doc["_id"])})

    denied = await other.get(f"/api/self-learner/roadmap/status/{job_id}")
    assert denied.status_code == 404

    allowed = await owner.get(f"/api/self-learner/roadmap/status/{job_id}")
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "processing"


async def test_pomodoro_job_status_scoped_to_owner(client_factory, test_db):
    from app.api.routers.pomodoro import POMODORO_JOB_PREFIX

    owner = await _seed_and_login_user(test_db, client_factory, role=7, name="Learner C")
    other = await _seed_and_login_user(test_db, client_factory, role=7, name="Learner D")

    owner_user_doc = await test_db["users"].find_one({"fullName": "Learner C"})
    job_id = str(uuid.uuid4())
    await set_job(POMODORO_JOB_PREFIX, job_id, {"status": "pending", "user_id": str(owner_user_doc["_id"])})

    denied = await other.get(f"/api/pomodoro/job/{job_id}")
    assert denied.status_code == 404

    allowed = await owner.get(f"/api/pomodoro/job/{job_id}")
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "pending"


# ============================================================
# 3. Exception handler preserves custom dict detail content
# ============================================================

async def test_exception_handler_preserves_unrecognized_dict_detail():
    from fastapi import HTTPException

    from app.main import http_exception_handler

    exc = HTTPException(status_code=400, detail={"foo": "bar"})
    response = await http_exception_handler(None, exc)

    assert response.status_code == 400
    body = json.loads(response.body)
    assert body["foo"] == "bar"
    assert body["error"] != "Request failed"
    assert "bar" in body["error"]


# ============================================================
# 4. CSRF hardening — SameSite=Strict on the auth cookie
# ============================================================

async def test_login_cookie_is_samesite_strict(client, test_db):
    email = f"cookie-test-{uuid.uuid4().hex[:8]}@test.local"
    await test_db["users"].insert_one(
        create_user_document({"fullName": "Cookie Test", "email": email, "role": 1, "is_active": True}, hash_password(PASSWORD))
    )

    resp = await client.post("/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200

    set_cookie_headers = resp.headers.get_list("set-cookie")
    assert any("samesite=strict" in h.lower() for h in set_cookie_headers)
