from bson import ObjectId

from tests.test_security_fixes import _get_institute_id, _register_institute_admin


async def test_generate_preview_requires_auth(client):
    resp = await client.post("/transcript/generate/preview", json={"batch_id": str(ObjectId()), "semester": 1})
    assert resp.status_code == 401


async def test_generate_preview_rejects_missing_semester(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Transcript Institute")
    resp = await institute.post("/transcript/generate/preview", json={"batch_id": str(ObjectId())})
    assert resp.status_code == 422


async def test_generate_preview_rejects_non_int_semester(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Transcript Institute")
    resp = await institute.post(
        "/transcript/generate/preview", json={"batch_id": str(ObjectId()), "semester": "not-a-number"}
    )
    assert resp.status_code == 422


async def test_generate_preview_rejects_batch_not_owned(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Transcript Institute")
    resp = await institute.post("/transcript/generate/preview", json={"batch_id": str(ObjectId()), "semester": 1})
    assert resp.status_code == 404
    assert "batch" in resp.json()["error"].lower()


async def test_generate_preview_rejects_missing_grading_config(superadmin_client, client_factory, test_db):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Transcript Institute")
    institute_id = await _get_institute_id(institute)

    batch = await test_db["batchDetails"].insert_one({
        "institute_id": ObjectId(institute_id), "batch_name": "2024-2028", "is_deleted": False,
    })

    resp = await institute.post(
        "/transcript/generate/preview", json={"batch_id": str(batch.inserted_id), "semester": 1}
    )
    assert resp.status_code == 404
    assert "grading" in resp.json()["error"].lower()


async def test_generate_confirm_requires_auth(client):
    resp = await client.post("/transcript/generate/confirm", json={"batch_id": str(ObjectId()), "semester": 1})
    assert resp.status_code == 401


async def test_imports_list_requires_institute_or_faculty(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Transcript Institute")
    resp = await institute.get("/transcript/imports")
    assert resp.status_code == 200
    assert resp.json()["imports"] == []


async def test_transcript_by_student_id_invalid(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Transcript Institute")
    resp = await institute.get(f"/transcript/{ObjectId()}")
    assert resp.status_code in (400, 404)
