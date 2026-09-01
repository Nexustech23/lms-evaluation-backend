"""
Regression suite for Phase 1's security fixes. Each test proves a specific
gap identified in the FastAPI codebase analysis is actually closed — this
file is a hard gate: if it fails, Phase 1 regressed and Phase 2 (Pydantic
retrofit) should not proceed until it's green again.
"""

import json
import uuid

import pytest
from bson import ObjectId

from app.core.config import settings
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
# 5. Institute-hierarchy routes scoped to the caller's institute
#    (cross-tenant IDOR: DELETE/PUT/GET on school / programme /
#     department / batch / subject by id, previously guarded only by
#     "is logged in as any role")
# ============================================================

async def _build_hierarchy(client) -> dict:
    """Create school -> programme -> department -> batch -> subject through
    the real API as `client` (an institute admin), returning every id."""
    school_id = (await client.post("/schools", json={"school_name": "H School"})).json()["school"]["id"]
    programme_id = (await client.post(
        "/programmes", json={"school_id": school_id, "programme_name": "H Prog"}
    )).json()["programme"]["id"]
    department_id = (await client.post(
        "/departments", json={"programme_id": programme_id, "department_name": "H Dept"}
    )).json()["department"]["id"]
    batch_id = (await client.post("/batches", json={
        "programme_id": programme_id,
        "batch_name": "H Batch",
        "semesters": [{"semester_number": 1, "subjects": []}],
    })).json()["batch_id"]
    subject_id = (await client.post("/subjects", json={
        "school_id": school_id, "programme_id": programme_id, "subject_name": "H Subject",
    })).json()["subject_id"]
    return {
        "school_id": school_id, "programme_id": programme_id, "department_id": department_id,
        "batch_id": batch_id, "subject_id": subject_id,
    }


async def test_institute_hierarchy_denies_cross_institute_access(superadmin_client, client_factory, test_db):
    institute_a = await _register_institute_admin(superadmin_client, client_factory, "Hier A")
    institute_b = await _register_institute_admin(superadmin_client, client_factory, "Hier B")

    ids = await _build_hierarchy(institute_a)

    # Institute B — a real institute admin, but of a different tenant — must
    # not read, edit, or destroy any of institute A's hierarchy.
    cross_tenant_calls = [
        ("get",    f"/schools/{ids['school_id']}/delete-summary", None),
        ("get",    f"/programme/{ids['programme_id']}", None),
        ("get",    f"/programmes/{ids['school_id']}", None),
        ("get",    f"/departments/{ids['programme_id']}", None),
        ("get",    f"/subject/{ids['subject_id']}", None),
        ("get",    f"/subjects/{ids['programme_id']}", None),
        ("get",    f"/programmes_po_target/{ids['subject_id']}", None),
        ("put",    f"/schools/{ids['school_id']}", {"school_name": "hijacked"}),
        ("put",    f"/programmes/{ids['programme_id']}", {"programme_name": "hijacked"}),
        ("put",    "/programmes/po", {"programme_id": ids["programme_id"], "po": []}),
        ("put",    f"/departments/{ids['department_id']}", {"department_name": "hijacked"}),
        ("put",    f"/subjects/{ids['subject_id']}", {"credits": 99}),
        ("delete", f"/subjects/{ids['subject_id']}", None),
        ("delete", f"/batches/{ids['batch_id']}", None),
        ("delete", f"/departments/{ids['department_id']}", None),
        ("delete", f"/programmes/{ids['programme_id']}", None),
        ("delete", f"/schools/{ids['school_id']}", None),
    ]
    for method, url, body in cross_tenant_calls:
        kwargs = {"json": body} if body is not None else {}
        resp = await getattr(institute_b, method)(url, **kwargs)
        assert resp.status_code in (400, 403, 404), f"{method.upper()} {url} -> {resp.status_code} (expected block)"

    # Institute A's data is still fully intact after B's attempts.
    assert await test_db["schoolDetails"].find_one({"_id": ObjectId(ids["school_id"])})
    assert await test_db["programmeDetails"].find_one({"_id": ObjectId(ids["programme_id"])})
    assert await test_db["departmentDetails"].find_one({"_id": ObjectId(ids["department_id"])})
    assert await test_db["batchDetails"].find_one({"_id": ObjectId(ids["batch_id"])})
    subject = await test_db["subjectDetails"].find_one({"_id": ObjectId(ids["subject_id"])})
    assert subject and subject.get("is_deleted") is not True and subject.get("credits") != 99

    # The owner can still operate on its own hierarchy.
    assert (await institute_a.get(f"/programme/{ids['programme_id']}")).status_code == 200
    assert (await institute_a.put(
        f"/schools/{ids['school_id']}", json={"school_name": "Renamed By Owner"}
    )).status_code == 200
    assert (await institute_a.delete(f"/schools/{ids['school_id']}")).status_code == 200


async def test_institute_hierarchy_router_blocks_non_admin_roles(superadmin_client, client_factory, test_db):
    institute_a = await _register_institute_admin(superadmin_client, client_factory, "Hier RoleGate")
    ids = await _build_hierarchy(institute_a)

    # A self-learner (role 7) — the public-signup role — must not even reach
    # this router, let alone any object in it.
    learner = await _seed_and_login_user(test_db, client_factory, role=7, name="Nosy Learner")
    for method, url in [
        ("get", f"/programme/{ids['programme_id']}"),
        ("delete", f"/schools/{ids['school_id']}"),
        ("get", "/schools"),
    ]:
        resp = await getattr(learner, method)(url)
        assert resp.status_code == 403, f"{method.upper()} {url} -> {resp.status_code} (expected 403 role gate)"

    assert await test_db["schoolDetails"].find_one({"_id": ObjectId(ids["school_id"])})


# ============================================================
# 6. JWT hardening
#    (a) production refuses a weak/default JWT_SECRET_KEY at startup
#    (b) a token is no longer enough on its own — the account must still
#        be active, and the token must not predate a logout
# ============================================================

def test_weak_jwt_secret_rejected_only_in_production():
    from app.core.config import Settings

    # dev / test envs stay permissive (local runs use the default secret)
    Settings(_env_file=None, ENV="development", JWT_SECRET_KEY="change-me-in-production")

    # production refuses the known-weak default and anything too short
    for bad in ("change-me-in-production", "secret", "short-key"):
        with pytest.raises(Exception):
            Settings(_env_file=None, ENV="production", JWT_SECRET_KEY=bad)

    # a real random secret is accepted in production
    Settings(_env_file=None, ENV="production", JWT_SECRET_KEY="k" * 48)


async def test_deactivated_user_loses_access_before_token_expiry(client_factory, test_db):
    c = await _seed_and_login_user(test_db, client_factory, role=7, name="Soon Disabled")
    assert (await c.get("/me")).status_code == 200

    await test_db["users"].update_one({"fullName": "Soon Disabled"}, {"$set": {"is_active": False}})
    assert (await c.get("/me")).status_code == 401

    await test_db["users"].update_one(
        {"fullName": "Soon Disabled"}, {"$set": {"is_active": True, "is_deleted": True}}
    )
    assert (await c.get("/me")).status_code == 401


async def test_logout_revokes_the_token_not_just_the_cookie(client_factory, test_db):
    victim = await _seed_and_login_user(test_db, client_factory, role=7, name="Logout Victim")

    # an attacker who copied the cookie before logout
    stolen = victim.cookies.get(settings.JWT_COOKIE_NAME)
    attacker = await client_factory()
    attacker.cookies.set(settings.JWT_COOKIE_NAME, stolen)
    assert (await attacker.get("/me")).status_code == 200

    assert (await victim.post("/logout")).status_code == 200

    # the copied, still-unexpired token is now rejected too
    assert (await attacker.get("/me")).status_code == 401


# ============================================================
# 7. Institute-student credentials — random one-time password, never a
#    guessable institute-wide default, and forced change on first login
# ============================================================

async def test_generated_institute_student_password_is_random_and_forces_change(
    superadmin_client, client_factory, test_db
):
    institute = await _register_institute_admin(superadmin_client, client_factory, "CredInst")

    body = await register(
        institute,
        role="institute_student",
        fullName="Nikhil Student",
        email=f"personal-{uuid.uuid4().hex[:8]}@example.com",
        school_id=str(ObjectId()),
        programme_id=str(ObjectId()),
        roll_no="R777",
        contact_no="9999999999",
        enrollment_no="ENR777",
        # no password supplied -> must be generated
    )

    pwd = body["default_password"]
    email = body["college_email"]

    assert body["must_change_password"] is True
    assert not email.endswith("@gmail.com")
    assert email.endswith("@" + settings.STUDENT_EMAIL_DOMAIN)
    # not the old guessable "<shortname>@123" style default
    assert "@123" not in pwd and len(pwd) >= 10

    student = await client_factory()
    login_resp = await student.post("/login", json={"email": email, "password": pwd})
    assert login_resp.status_code == 200
    assert login_resp.json()["user"]["must_change_password"] is True

    changed = await student.put(
        "/profile/change-password", json={"currentPassword": pwd, "newPassword": "BrandNewPass1!"}
    )
    assert changed.status_code == 200

    # flag cleared, and the temp-password session was revoked
    assert (await student.get("/me")).status_code == 401
    relogin = await client_factory()
    r = await relogin.post("/login", json={"email": email, "password": "BrandNewPass1!"})
    assert r.status_code == 200
    assert r.json()["user"]["must_change_password"] is False


# ============================================================
# 8. Search inputs are re.escape()'d before hitting MongoDB $regex
#    (a crafted pattern must not trigger a catastrophic-backtracking /
#     full-collection-scan DoS)
# ============================================================

def test_search_regex_helper_neutralises_metacharacters():
    from app.utils.query import search_regex

    assert search_regex("") is None
    assert search_regex("   ") is None

    clause = search_regex("(.*a){30}$")
    # matched literally, not as a pattern
    assert clause["$regex"] == r"\(\.\*a\)\{30\}\$"
    assert clause["$options"] == "i"

    # overlong input is bounded, not rejected
    assert len(search_regex("x" * 5000)["$regex"]) <= 200


async def test_hierarchy_search_accepts_regex_metacharacters_without_error(
    superadmin_client, client_factory
):
    institute = await _register_institute_admin(superadmin_client, client_factory, "RegexInst")
    await institute.post("/schools", json={"school_name": "School of Engineering"})

    # a pathological pattern is treated as a literal substring -> 200, 0 hits
    evil = await institute.get("/schools", params={"search": "(a+)+$"})
    assert evil.status_code == 200
    assert evil.json()["total"] == 0

    # a real substring still works
    ok = await institute.get("/schools", params={"search": "Engineering"})
    assert ok.status_code == 200
    assert ok.json()["total"] == 1


# ============================================================
# 9. SSRF guard — the server must refuse to fetch internal / cloud-metadata
#    addresses (or follow redirects) for client-influenced URLs
# ============================================================

def test_ssrf_guard_blocks_internal_and_metadata_targets():
    from app.utils.net import SsrfError, assert_url_allowed

    for bad in [
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "http://127.0.0.1:6379/",                      # local Redis
        "http://localhost/admin",
        "http://10.0.0.5/",                            # RFC1918
        "http://192.168.1.1/",
        "http://[::1]/",                               # IPv6 loopback
        "file:///etc/passwd",                          # non-http scheme
        "ftp://example.com/x",
        "gopher://x/_",
    ]:
        with pytest.raises(SsrfError):
            assert_url_allowed(bad)


def test_ssrf_guard_allows_the_imagekit_host():
    # The store the app actually uploads to must stay reachable.
    from app.utils.net import assert_url_allowed

    assert_url_allowed("https://ik.imagekit.io/demo/answer_script.pdf")


def test_safe_get_refuses_redirects_and_bad_status(monkeypatch):
    import app.utils.net as net

    monkeypatch.setattr(net, "assert_url_allowed", lambda url: None)

    class _Resp:
        def __init__(self, status, headers=None):
            self.status_code = status
            self.headers = headers or {}
            self.is_redirect = 300 <= status < 400
            self.is_permanent_redirect = status in (301, 308)

        def iter_content(self, chunk_size=0):
            return iter([b""])

        def close(self):
            pass

    monkeypatch.setattr(net.requests, "get", lambda *a, **k: _Resp(302, {"location": "http://169.254.169.254/"}))
    with pytest.raises(net.SsrfError):
        net.safe_get("https://ik.imagekit.io/x")

    monkeypatch.setattr(net.requests, "get", lambda *a, **k: _Resp(500))
    with pytest.raises(net.SsrfError):
        net.safe_get("https://ik.imagekit.io/x")


# ============================================================
# 10. Cross-tenant access on the remaining institute routers
#     (profile.py institute/faculty endpoints, relative_grading.py)
# ============================================================

async def _register_faculty_for(admin_client, test_db, institute_id: str, email: str) -> str:
    await register(
        admin_client, role="faculty", fullName="X Faculty", email=email,
        password=PASSWORD, school_id=str(ObjectId()),
    )
    doc = await test_db["facultyDetails"].find_one({"institute_id": ObjectId(institute_id)})
    return str(doc["_id"])


async def test_institute_admin_cannot_touch_another_institutes_faculty_or_config(
    superadmin_client, client_factory, test_db
):
    a = await _register_institute_admin(superadmin_client, client_factory, "Tenant A")
    b = await _register_institute_admin(superadmin_client, client_factory, "Tenant B")
    a_id = await _get_institute_id(a)
    a_inst = await test_db["instituteDetails"].find_one({"_id": ObjectId(a_id)})
    a_user_id = str(a_inst["user_id"])

    a_faculty_id = await _register_faculty_for(a, test_db, a_id, f"fa-{uuid.uuid4().hex[:8]}@test.local")

    # B must not read A's institute record
    assert (await b.get(f"/institute/{a_user_id}")).status_code == 403

    # B must not edit or delete A's faculty
    assert (await b.put(f"/faculty/{a_faculty_id}", json={"designation": "hijacked"})).status_code == 404
    assert (await b.delete(f"/faculty/{a_faculty_id}")).status_code == 404
    assert await test_db["facultyDetails"].find_one({"_id": ObjectId(a_faculty_id)}) is not None

    # A's own admin still can
    assert (await a.get(f"/institute/{a_user_id}")).status_code == 200
    assert (await a.put(f"/faculty/{a_faculty_id}", json={"designation": "HOD"})).status_code == 200


async def test_relative_grading_is_scoped_to_caller_institute(superadmin_client, client_factory, test_db):
    grading = {
        "a_plus_percentage": 10, "a_percentage": 10, "a_minus_percentage": 10,
        "b_plus_percentage": 10, "b_percentage": 10, "b_minus_percentage": 10,
        "c_plus_percentage": 10, "c_percentage": 10, "c_minus_percentage": 10,
        "d_percentage": 10, "u_percentage": 0,
    }
    a = await _register_institute_admin(superadmin_client, client_factory, "RG A")
    b = await _register_institute_admin(superadmin_client, client_factory, "RG B")
    a_id = await _get_institute_id(a)

    created = await a.post("/relative-grading", json=grading)
    assert created.status_code == 200, created.text
    doc = await test_db["relativeGradings"].find_one({"university_id": ObjectId(a_id)})
    grading_id = str(doc["_id"])

    # B cannot read A's scheme, nor overwrite it
    assert (await b.get(f"/relative-grading/{a_id}")).status_code == 403
    assert (await b.put(f"/relative-grading/{grading_id}", json=grading)).status_code == 404

    # A can
    assert (await a.get(f"/relative-grading/{a_id}")).status_code == 200
    assert (await a.put(f"/relative-grading/{grading_id}", json=grading)).status_code == 200


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
