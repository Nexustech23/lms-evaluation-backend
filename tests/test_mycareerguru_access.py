# ============================================================
# Coverage for the MyCareerGuru institute-enablement gate:
#   - app.api.deps.can_use_mycareerguru — the two-level (institute AND
#     school) resolver
#   - app.api.deps.require_mycareerguru_access — the router-level
#     dependency wired onto the 5 MyCareerGuru routers, which must only
#     ever block INSTITUTE_STUDENT callers and never touch any other role
#   - the hasMyCareerGuruAccess / mycareerguru_enabled flags round-tripping
#     through institute registration+update and school create+update
# ============================================================
import uuid

from bson import ObjectId

from app.api.deps import can_use_mycareerguru
from app.core.security import hash_password
from app.models.user import create_user_document
from tests.conftest import login, register
from tests.test_security_fixes import PASSWORD, _register_institute_admin, _seed_and_login_user

ROADMAP_LIST_PATH = "/api/self-learner/roadmap"


async def _seed_institute_student(
    test_db, *, institute_mycareerguru: bool, school_mycareerguru: bool,
) -> tuple[str, str]:
    """Directly seeds an institute admin (with hasMyCareerGuruAccess set),
    one school (with mycareerguru_enabled set), and one institute_student
    belonging to that school — bypassing the full registration API chain,
    matching this test suite's established _seed_and_login_user rationale.
    Returns (student_user_id, institute_admin_user_id) as strings."""
    admin_id = (await test_db["users"].insert_one(create_user_document(
        {
            "fullName": "MCG Institute Admin", "email": f"mcg-admin-{uuid.uuid4().hex[:8]}@test.local",
            "role": 2, "is_active": True, "hasMyCareerGuruAccess": institute_mycareerguru,
        },
        hash_password(PASSWORD),
    ))).inserted_id

    institute_id = (await test_db["instituteDetails"].insert_one({
        "user_id": admin_id, "institute_name": "MCG Test Institute", "is_deleted": False,
    })).inserted_id

    school_id = (await test_db["schoolDetails"].insert_one({
        "institute_id": institute_id, "school_name": "MCG Test School",
        "mycareerguru_enabled": school_mycareerguru, "is_deleted": False,
    })).inserted_id

    student_id = (await test_db["users"].insert_one(create_user_document(
        {
            "fullName": "MCG Student", "email": f"mcg-student-{uuid.uuid4().hex[:8]}@test.local",
            "role": 4, "is_active": True,
        },
        hash_password(PASSWORD),
    ))).inserted_id

    await test_db["studentDetails"].insert_one({
        "user_id": student_id, "role": 4, "institute_id": institute_id, "school_id": school_id,
    })

    return str(student_id), str(admin_id)


# ---------------------------------------------------------------- can_use_mycareerguru (unit)

async def test_self_learner_always_allowed(test_db):
    assert await can_use_mycareerguru(test_db, {"user_id": str(ObjectId()), "role": 7}) is True


async def test_faculty_role_not_allowed_by_this_resolver(test_db):
    # can_use_mycareerguru only ever returns True for roles 4/7 — callers of
    # other roles fall through to False, but require_mycareerguru_access
    # (tested separately below) never actually calls this for them.
    assert await can_use_mycareerguru(test_db, {"user_id": str(ObjectId()), "role": 3}) is False


async def test_institute_student_blocked_when_institute_flag_off(test_db):
    student_id, _ = await _seed_institute_student(
        test_db, institute_mycareerguru=False, school_mycareerguru=True,
    )
    assert await can_use_mycareerguru(test_db, {"user_id": student_id, "role": 4}) is False


async def test_institute_student_blocked_when_school_flag_off(test_db):
    student_id, _ = await _seed_institute_student(
        test_db, institute_mycareerguru=True, school_mycareerguru=False,
    )
    assert await can_use_mycareerguru(test_db, {"user_id": student_id, "role": 4}) is False


async def test_institute_student_allowed_when_both_flags_on(test_db):
    student_id, _ = await _seed_institute_student(
        test_db, institute_mycareerguru=True, school_mycareerguru=True,
    )
    assert await can_use_mycareerguru(test_db, {"user_id": student_id, "role": 4}) is True


async def test_institute_student_without_student_record_blocked(test_db):
    assert await can_use_mycareerguru(test_db, {"user_id": str(ObjectId()), "role": 4}) is False


# ---------------------------------------------------------------- require_mycareerguru_access (end-to-end)

async def test_self_learner_reaches_gated_router(client_factory, test_db):
    learner = await _seed_and_login_user(test_db, client_factory, role=7, name="MCG Self Learner")
    resp = await learner.get(ROADMAP_LIST_PATH)
    assert resp.status_code == 200


async def test_institute_student_without_access_gets_403(client_factory, test_db):
    student_id, _ = await _seed_institute_student(
        test_db, institute_mycareerguru=False, school_mycareerguru=False,
    )
    student = await test_db["users"].find_one({"_id": ObjectId(student_id)})
    client = await client_factory()
    await login(client, student["email"], PASSWORD)

    resp = await client.get(ROADMAP_LIST_PATH)
    assert resp.status_code == 403


async def test_institute_student_with_access_reaches_gated_router(client_factory, test_db):
    student_id, _ = await _seed_institute_student(
        test_db, institute_mycareerguru=True, school_mycareerguru=True,
    )
    student = await test_db["users"].find_one({"_id": ObjectId(student_id)})
    client = await client_factory()
    await login(client, student["email"], PASSWORD)

    resp = await client.get(ROADMAP_LIST_PATH)
    assert resp.status_code == 200


async def test_other_roles_unaffected_by_the_new_gate(client_factory, test_db):
    # Faculty was never part of this feature's scope — must keep whatever
    # access it already had (get_current_identity's normal 200), not be
    # newly blocked by require_mycareerguru_access.
    faculty = await _seed_and_login_user(test_db, client_factory, role=3, name="MCG Faculty")
    resp = await faculty.get(ROADMAP_LIST_PATH)
    assert resp.status_code == 200


# ---------------------------------------------------------------- flag round-trips

async def test_institute_registration_sets_hasMyCareerGuruAccess(superadmin_client, client_factory):
    email = f"mcg-reg-{uuid.uuid4().hex[:8]}@test.local"
    await register(
        superadmin_client, role="institute", fullName="MCG Reg Admin", email=email, password=PASSWORD,
        institute={"institute_name": "MCG Reg Institute"}, hasMyCareerGuruAccess=True,
    )
    client = await client_factory()
    await login(client, email, PASSWORD)
    resp = await client.get("/me")
    assert resp.status_code == 200
    assert resp.json()["user"]["hasMyCareerGuruAccess"] is True


async def test_institute_update_toggles_hasMyCareerGuruAccess(superadmin_client, client_factory, test_db):
    institute = await _register_institute_admin(superadmin_client, client_factory, "MCG Update Institute")
    resp = await institute.get("/me")
    user_id = resp.json()["user"]["id"]

    put_resp = await superadmin_client.put(f"/institute/{user_id}", json={"hasMyCareerGuruAccess": True})
    assert put_resp.status_code == 200

    updated_user = await test_db["users"].find_one({"_id": ObjectId(user_id)})
    assert updated_user["hasMyCareerGuruAccess"] is True


async def test_school_create_and_update_round_trip_mycareerguru_enabled(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "MCG School Institute")

    created = await institute.post("/schools", json={
        "school_name": "MCG Pilot School", "mycareerguru_enabled": True,
    })
    assert created.status_code == 200
    school = created.json()["school"]
    assert school["mycareerguru_enabled"] is True

    updated = await institute.put(f"/schools/{school['id']}", json={"mycareerguru_enabled": False})
    assert updated.status_code == 200

    listed = await institute.get("/schools")
    assert listed.status_code == 200
    refetched = next(s for s in listed.json()["schools"] if s["id"] == school["id"])
    assert refetched["mycareerguru_enabled"] is False