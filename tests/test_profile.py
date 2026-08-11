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
