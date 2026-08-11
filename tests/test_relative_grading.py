from bson import ObjectId

from tests.test_security_fixes import _get_institute_id, _register_institute_admin

VALID_100 = {
    "a_plus_percentage": 10, "a_percentage": 10, "a_minus_percentage": 10,
    "b_plus_percentage": 10, "b_percentage": 10, "b_minus_percentage": 10,
    "c_plus_percentage": 10, "c_percentage": 10, "c_minus_percentage": 10,
    "d_percentage": 10, "u_percentage": 0,
}


async def test_create_relative_grading_requires_auth(client):
    resp = await client.post("/relative-grading", json=VALID_100)
    assert resp.status_code == 401


async def test_create_relative_grading_rejects_non_numeric_field(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "RG Institute")
    resp = await institute.post("/relative-grading", json={**VALID_100, "a_percentage": "not-a-number"})
    assert resp.status_code == 422


async def test_create_relative_grading_rejects_wrong_total(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "RG Institute")
    resp = await institute.post("/relative-grading", json={**VALID_100, "a_percentage": 50})
    assert resp.status_code == 400
    assert "100" in resp.json()["error"]


async def test_create_and_fetch_relative_grading(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "RG Institute")
    created = await institute.post("/relative-grading", json=VALID_100)
    assert created.status_code == 200

    institute_id = await _get_institute_id(institute)
    fetched = await institute.get(f"/relative-grading/{institute_id}")
    assert fetched.status_code == 200
    assert fetched.json()["data"]["a_plus_percentage"] == 10


async def test_create_relative_grading_upserts(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "RG Institute")
    first = await institute.post("/relative-grading", json=VALID_100)
    assert "created" in first.json()["message"]

    second = await institute.post("/relative-grading", json=VALID_100)
    assert "updated" in second.json()["message"]
    assert second.json()["id"] == first.json()["id"]


async def test_update_relative_grading_not_found(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "RG Institute")
    resp = await institute.put(f"/relative-grading/{ObjectId()}", json=VALID_100)
    assert resp.status_code == 404
