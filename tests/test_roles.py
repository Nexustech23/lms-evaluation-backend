async def test_create_role_requires_superadmin(client):
    resp = await client.post("/create_role", json={"name": "faculty"})
    assert resp.status_code == 401  # not authenticated at all


async def test_create_role_rejects_non_superadmin(superadmin_client, client_factory, test_db):
    from tests.conftest import login
    from tests.test_security_fixes import _seed_and_login_user

    faculty_client = await _seed_and_login_user(test_db, client_factory, role=3, name="Some Faculty")
    resp = await faculty_client.post("/create_role", json={"name": "faculty"})
    assert resp.status_code == 403


async def test_create_role_success(superadmin_client):
    resp = await superadmin_client.post("/create_role", json={"name": "faculty", "description": "Faculty role"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["role"]["name"] == "faculty"
    assert body["role"]["display_name"] == "Faculty Member"


async def test_create_role_rejects_unknown_name(superadmin_client):
    resp = await superadmin_client.post("/create_role", json={"name": "not-a-real-role"})
    assert resp.status_code == 422  # Pydantic validation, not the old create_role_document ValueError->400


async def test_create_role_rejects_duplicate(superadmin_client):
    first = await superadmin_client.post("/create_role", json={"name": "institute"})
    assert first.status_code == 200

    second = await superadmin_client.post("/create_role", json={"name": "institute"})
    assert second.status_code == 400
    assert "already exists" in second.json()["error"].lower()
