from bson import ObjectId

from tests.conftest import login, register
from tests.test_security_fixes import PASSWORD, _get_institute_id, _register_institute_admin


async def test_get_profile_requires_auth(client):
    resp = await client.get("/profile")
    assert resp.status_code == 401


async def test_get_profile_superadmin(superadmin_client):
    resp = await superadmin_client.get("/profile")
    assert resp.status_code == 200
    assert resp.json()["role"] == 1


async def test_update_profile_rejects_empty_body(superadmin_client):
    resp = await superadmin_client.put("/profile", json={})
    assert resp.status_code == 400


async def test_update_profile_success(superadmin_client):
    resp = await superadmin_client.put("/profile", json={"fullName": "New Name", "language": "hindi"})
    assert resp.status_code == 200

    fetched = await superadmin_client.get("/profile")
    assert fetched.json()["fullName"] == "New Name"
    assert fetched.json()["language"] == "hindi"


async def test_update_profile_faculty_cannot_set_color(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "PF Institute")
    faculty_email = "faculty-pf@test.local"
    await register(
        institute, role="faculty", fullName="PF Faculty", email=faculty_email,
        password=PASSWORD, school_id=str(ObjectId()),
    )
    faculty_client = await client_factory()
    await login(faculty_client, faculty_email, PASSWORD)

    resp = await faculty_client.put("/profile", json={"color": "#123456"})
    assert resp.status_code == 403


async def test_change_password_rejects_missing_field(superadmin_client):
    resp = await superadmin_client.put("/profile/change-password", json={"currentPassword": PASSWORD})
    assert resp.status_code == 422


async def test_change_password_rejects_weak_new_password(superadmin_client):
    resp = await superadmin_client.put(
        "/profile/change-password", json={"currentPassword": PASSWORD, "newPassword": "abc"}
    )
    assert resp.status_code == 422


async def test_change_password_rejects_wrong_current(superadmin_client):
    resp = await superadmin_client.put(
        "/profile/change-password", json={"currentPassword": "WrongOne123!", "newPassword": "NewStrongPass1!"}
    )
    assert resp.status_code == 401


async def test_change_password_success_and_relogin(superadmin_client, client_factory, test_db):
    me = (await superadmin_client.get("/profile")).json()
    email = me["email"]

    resp = await superadmin_client.put(
        "/profile/change-password", json={"currentPassword": PASSWORD, "newPassword": "NewStrongPass1!"}
    )
    assert resp.status_code == 200

    new_client = await client_factory()
    relogin = await new_client.post("/login", json={"email": email, "password": "NewStrongPass1!"})
    assert relogin.status_code == 200


async def test_get_institutes_requires_superadmin(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "List Institute")
    resp = await institute.get("/institutes")
    assert resp.status_code == 403

    resp2 = await superadmin_client.get("/institutes")
    assert resp2.status_code == 200
    assert resp2.json()["total"] == 1


async def test_update_institute_by_superadmin(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Update Institute")
    me = (await institute.get("/profile")).json()
    user_id = me["id"]

    resp = await superadmin_client.put(f"/institute/{user_id}", json={
        "institute": {"institute_name": "Renamed Institute", "city": "Metropolis"},
        "hasCOAccess": True,
    })
    assert resp.status_code == 200
    assert resp.json()["institute"]["institute_name"] == "Renamed Institute"
    assert resp.json()["institute"]["city"] == "Metropolis"


async def test_update_faculty_by_institute_admin(superadmin_client, client_factory, test_db):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Faculty Update Institute")
    institute_id = await _get_institute_id(institute)
    await register(
        institute, role="faculty", fullName="Original Name", email="orig-faculty@test.local",
        password=PASSWORD, school_id=str(ObjectId()),
    )
    faculty_doc = await test_db["facultyDetails"].find_one({"institute_id": ObjectId(institute_id)})

    resp = await institute.put(f"/faculty/{faculty_doc['_id']}", json={
        "designation": "Professor", "experience_years": 5,
    })
    assert resp.status_code == 200
    assert resp.json()["faculty"]["designation"] == "Professor"
    assert resp.json()["faculty"]["experience_years"] == 5


async def test_update_faculty_rejects_non_int_experience(superadmin_client, client_factory, test_db):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Faculty Bad Institute")
    resp = await institute.put(f"/faculty/{ObjectId()}", json={"experience_years": "a lot"})
    assert resp.status_code == 422


# ============================================================
# AI USAGE SUMMARY
# ============================================================

async def test_ai_usage_summary_requires_superadmin(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "AI Usage Non-Admin")
    resp = await institute.get("/ai-usage")
    assert resp.status_code == 403


async def test_ai_usage_summary_aggregates_across_users_within_window(superadmin_client, test_db):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    user_a, user_b = ObjectId(), ObjectId()

    await test_db["aiUsageEvents"].insert_many([
        {
            "user_id": user_a, "tenant_type": "individual", "institute_id": None, "school_id": None,
            "programme_id": None, "provider": "claude", "model": "claude-sonnet-4-5",
            "feature": "roadmap_curriculum", "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
            "cost_usd": 0.001, "grounded": None, "context_id": None, "job_id": None, "created_at": now,
        },
        {
            "user_id": user_b, "tenant_type": "individual", "institute_id": None, "school_id": None,
            "programme_id": None, "provider": "claude", "model": "claude-sonnet-4-5",
            "feature": "roadmap_curriculum", "input_tokens": 200, "output_tokens": 80, "total_tokens": 280,
            "cost_usd": 0.002, "grounded": None, "context_id": None, "job_id": None, "created_at": now,
        },
        # Outside the default 30-day window — must be excluded from totals.
        {
            "user_id": user_a, "tenant_type": "individual", "institute_id": None, "school_id": None,
            "programme_id": None, "provider": "claude", "model": "claude-sonnet-4-5",
            "feature": "roadmap_curriculum", "input_tokens": 9999, "output_tokens": 9999, "total_tokens": 19998,
            "cost_usd": 50.0, "grounded": None, "context_id": None, "job_id": None,
            "created_at": now - timedelta(days=60),
        },
    ])

    resp = await superadmin_client.get("/ai-usage")
    assert resp.status_code == 200
    body = resp.json()

    row = next(r for r in body["byFeature"] if r["feature"] == "roadmap_curriculum")
    assert row["total_tokens"] == 430  # 150 + 280, the stale row excluded
    assert row["call_count"] == 2
    assert row["distinct_users"] == 2

    assert body["totals"]["total_tokens"] == 430


async def test_ai_usage_summary_respects_days_param(superadmin_client, test_db):
    from datetime import datetime, timedelta, timezone

    old_event = {
        "user_id": ObjectId(), "tenant_type": "individual", "institute_id": None, "school_id": None,
        "programme_id": None, "provider": "gemini", "model": "gemini-2.5-flash",
        "feature": "roadmap_notes", "input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
        "cost_usd": 0.0001, "grounded": None, "context_id": None, "job_id": None,
        "created_at": datetime.now(timezone.utc) - timedelta(days=10),
    }
    await test_db["aiUsageEvents"].insert_one(old_event)

    resp_wide = await superadmin_client.get("/ai-usage?days=30")
    assert resp_wide.json()["totals"]["total_tokens"] == 15

    resp_narrow = await superadmin_client.get("/ai-usage?days=5")
    assert resp_narrow.json()["totals"]["total_tokens"] == 0


# ============================================================
# ACTIVITY LOGS — /institute-students/activity-logs (institute-scoped, no
# tokens/cost) and /self-learners/activity-logs (superadmin, with tokens/cost)
# ============================================================

async def _insert_ai_usage_event(test_db, *, user_id, feature="roadmap_curriculum", cost_usd=0.01):
    from datetime import datetime, timezone

    await test_db["aiUsageEvents"].insert_one({
        "user_id": ObjectId(user_id), "tenant_type": "individual", "institute_id": None,
        "school_id": None, "programme_id": None, "provider": "claude", "model": "claude-sonnet-4-5",
        "feature": feature, "input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
        "cost_usd": cost_usd, "grounded": None, "context_id": None, "job_id": None,
        "created_at": datetime.now(timezone.utc),
    })


async def _register_institute_student(institute_client, test_db, name: str) -> str:
    import uuid

    body = await register(
        institute_client,
        role="institute_student",
        fullName=name,
        email=f"personal-{uuid.uuid4().hex[:10]}@example.com",
        password=PASSWORD,
        school_id=str(ObjectId()),
        programme_id=str(ObjectId()),
        roll_no=f"R{uuid.uuid4().hex[:6]}",
        contact_no="9999999999",
        enrollment_no=f"ENR{uuid.uuid4().hex[:6]}",
    )
    # institute_student registration logs the student in under a generated
    # college_email, not the personal email supplied above (see
    # auth.py::_register_institute_student) — look the user up by that.
    user = await test_db["users"].find_one({"email": body["college_email"].lower()})
    return str(user["_id"])


async def test_institute_activity_logs_requires_institute_role(superadmin_client, client_factory, test_db):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Logs Institute")
    student_id = await _register_institute_student(institute, test_db, "Logs Student")
    await _insert_ai_usage_event(test_db, user_id=student_id)

    student_client = await client_factory()
    student_user = await test_db["users"].find_one({"_id": ObjectId(student_id)})
    await login(student_client, student_user["email"], PASSWORD)

    resp = await student_client.get("/institute-students/activity-logs")
    assert resp.status_code == 403


async def test_institute_activity_logs_scoped_to_own_institute_and_hides_cost(
    superadmin_client, client_factory, test_db,
):
    institute_a = await _register_institute_admin(superadmin_client, client_factory, "Logs Institute A")
    institute_b = await _register_institute_admin(superadmin_client, client_factory, "Logs Institute B")

    student_a = await _register_institute_student(institute_a, test_db, "Student A")
    student_b = await _register_institute_student(institute_b, test_db, "Student B")

    await _insert_ai_usage_event(test_db, user_id=student_a, feature="roadmap_curriculum", cost_usd=1.23)
    await _insert_ai_usage_event(test_db, user_id=student_b, feature="self_review_homework_help", cost_usd=4.56)

    resp = await institute_a.get("/institute-students/activity-logs")
    assert resp.status_code == 200
    body = resp.json()

    assert body["total"] == 1
    row = body["logs"][0]
    assert row["student_name"] == "Student A"
    assert row["action"] == "Generated roadmap"
    assert "cost_usd" not in row
    assert "input_tokens" not in row
    assert "provider" not in row


async def test_self_learner_activity_logs_requires_superadmin(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "SL Logs Institute")
    resp = await institute.get("/self-learners/activity-logs")
    assert resp.status_code == 403


async def test_self_learner_activity_logs_includes_tokens_and_cost_across_roles(
    superadmin_client, client_factory, test_db,
):
    institute = await _register_institute_admin(superadmin_client, client_factory, "SL Logs Institute 2")
    institute_student_id = await _register_institute_student(institute, test_db, "Institute Student X")

    self_learner_body = await register(
        superadmin_client, role="self_learner", fullName="Self Learner X",
        email="self-learner-x@example.com", password=PASSWORD,
    )
    self_learner_id = self_learner_body["id"] if "id" in self_learner_body else str(
        (await test_db["users"].find_one({"email": "self-learner-x@example.com"}))["_id"]
    )

    await _insert_ai_usage_event(test_db, user_id=institute_student_id, feature="roadmap_curriculum", cost_usd=1.0)
    await _insert_ai_usage_event(test_db, user_id=self_learner_id, feature="self_review_homework_help", cost_usd=2.0)

    resp = await superadmin_client.get("/self-learners/activity-logs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2

    by_role = {row["student_role"]: row for row in body["logs"]}
    assert by_role["institute_student"]["cost_usd"] == 1.0
    assert by_role["self_learner"]["cost_usd"] == 2.0
    assert by_role["self_learner"]["provider"] == "claude"

    filtered = await superadmin_client.get("/self-learners/activity-logs?email=self-learner-x")
    assert filtered.json()["total"] == 1
    assert filtered.json()["logs"][0]["student_email"] == "self-learner-x@example.com"


async def test_institute_activity_logs_allows_faculty_scoped_to_own_institute(
    superadmin_client, client_factory, test_db,
):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Faculty Logs Institute")
    student_id = await _register_institute_student(institute, test_db, "Faculty-Visible Student")
    await _insert_ai_usage_event(test_db, user_id=student_id, feature="roadmap_curriculum", cost_usd=0.5)

    faculty_email = "faculty-logs@test.local"
    await register(
        institute, role="faculty", fullName="Logs Faculty", email=faculty_email,
        password=PASSWORD, school_id=str(ObjectId()),
    )
    faculty_client = await client_factory()
    await login(faculty_client, faculty_email, PASSWORD)

    resp = await faculty_client.get("/institute-students/activity-logs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["logs"][0]["student_name"] == "Faculty-Visible Student"
    assert "cost_usd" not in body["logs"][0]


async def test_self_learner_activity_logs_institute_filter_uses_admin_user_id(
    superadmin_client, client_factory, test_db,
):
    institute_a = await _register_institute_admin(superadmin_client, client_factory, "Filter Institute A")
    institute_b = await _register_institute_admin(superadmin_client, client_factory, "Filter Institute B")

    student_a = await _register_institute_student(institute_a, test_db, "Filter Student A")
    student_b = await _register_institute_student(institute_b, test_db, "Filter Student B")

    await _insert_ai_usage_event(test_db, user_id=student_a, feature="roadmap_curriculum", cost_usd=1.0)
    await _insert_ai_usage_event(test_db, user_id=student_b, feature="roadmap_curriculum", cost_usd=2.0)

    # The dropdown/query param is the institute ADMIN's own user_id — the
    # same id /institutes and PUT /institute/{user_id} already use — not
    # instituteDetails' internal _id, which differs from it.
    admin_a_user_id = (await institute_a.get("/profile")).json()["id"]

    resp = await superadmin_client.get(f"/self-learners/activity-logs?institute_id={admin_a_user_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["logs"][0]["student_name"] == "Filter Student A"


async def test_self_learner_activity_logs_independent_filter_excludes_institute_students(
    superadmin_client, client_factory, test_db,
):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Independent Filter Institute")
    institute_student_id = await _register_institute_student(institute, test_db, "Institute Student Y")

    self_learner_body = await register(
        superadmin_client, role="self_learner", fullName="Self Learner Y",
        email="self-learner-y@example.com", password=PASSWORD,
    )
    self_learner_id = self_learner_body["id"] if "id" in self_learner_body else str(
        (await test_db["users"].find_one({"email": "self-learner-y@example.com"}))["_id"]
    )

    await _insert_ai_usage_event(test_db, user_id=institute_student_id, feature="roadmap_curriculum")
    await _insert_ai_usage_event(test_db, user_id=self_learner_id, feature="self_review_homework_help")

    resp = await superadmin_client.get("/self-learners/activity-logs?institute_id=independent")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["logs"][0]["student_role"] == "self_learner"
