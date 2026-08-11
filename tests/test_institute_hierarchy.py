from bson import ObjectId

from tests.test_security_fixes import _register_institute_admin


async def _institute(superadmin_client, client_factory):
    return await _register_institute_admin(superadmin_client, client_factory, "IH Institute")


# ============================================================
# SCHOOL
# ============================================================

async def test_create_school_requires_auth(client):
    resp = await client.post("/schools", json={"school_name": "School of Engineering"})
    assert resp.status_code == 401


async def test_create_school_rejects_blank_name(superadmin_client, client_factory):
    institute = await _institute(superadmin_client, client_factory)
    resp = await institute.post("/schools", json={"school_name": "  "})
    assert resp.status_code == 422


async def test_create_and_list_school(superadmin_client, client_factory):
    institute = await _institute(superadmin_client, client_factory)
    created = await institute.post("/schools", json={"school_name": "School of Engineering"})
    assert created.status_code == 200
    school_id = created.json()["school"]["id"]

    listed = await institute.get("/schools")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["schools"][0]["id"] == school_id


async def test_update_school_not_found(superadmin_client, client_factory):
    institute = await _institute(superadmin_client, client_factory)
    resp = await institute.put(f"/schools/{ObjectId()}", json={"school_name": "Renamed"})
    assert resp.status_code == 404


async def test_update_and_delete_school(superadmin_client, client_factory):
    institute = await _institute(superadmin_client, client_factory)
    created = await institute.post("/schools", json={"school_name": "School A"})
    school_id = created.json()["school"]["id"]

    updated = await institute.put(f"/schools/{school_id}", json={"school_name": "School A Renamed"})
    assert updated.status_code == 200

    deleted = await institute.delete(f"/schools/{school_id}")
    assert deleted.status_code == 200

    listed = await institute.get("/schools")
    assert listed.json()["total"] == 0


# ============================================================
# PROGRAMME
# ============================================================

async def test_create_programme_rejects_missing_school_id(superadmin_client, client_factory):
    institute = await _institute(superadmin_client, client_factory)
    resp = await institute.post("/programmes", json={"programme_name": "B.Tech CS"})
    assert resp.status_code == 422


async def test_create_programme_rejects_unowned_school(superadmin_client, client_factory):
    institute = await _institute(superadmin_client, client_factory)
    resp = await institute.post("/programmes", json={
        "school_id": str(ObjectId()), "programme_name": "B.Tech CS",
    })
    assert resp.status_code in (403, 404)


async def test_create_and_get_programme(superadmin_client, client_factory):
    institute = await _institute(superadmin_client, client_factory)
    school = await institute.post("/schools", json={"school_name": "School A"})
    school_id = school.json()["school"]["id"]

    created = await institute.post("/programmes", json={"school_id": school_id, "programme_name": "B.Tech CS"})
    assert created.status_code == 200
    programme_id = created.json()["programme"]["id"]

    fetched = await institute.get(f"/programme/{programme_id}")
    assert fetched.status_code == 200
    assert fetched.json()["programme"]["programme_name"] == "B.Tech CS"


async def test_update_programme_po_requires_programme_id(superadmin_client):
    resp = await superadmin_client.put("/programmes/po", json={})
    assert resp.status_code == 422


# ============================================================
# DEPARTMENT
# ============================================================

async def test_create_department_rejects_missing_programme_id(superadmin_client, client_factory):
    institute = await _institute(superadmin_client, client_factory)
    resp = await institute.post("/departments", json={"department_name": "CSE"})
    assert resp.status_code == 422


async def test_create_and_list_department(superadmin_client, client_factory):
    institute = await _institute(superadmin_client, client_factory)
    school = await institute.post("/schools", json={"school_name": "School A"})
    school_id = school.json()["school"]["id"]
    programme = await institute.post("/programmes", json={"school_id": school_id, "programme_name": "B.Tech"})
    programme_id = programme.json()["programme"]["id"]

    created = await institute.post("/departments", json={"programme_id": programme_id, "department_name": "CSE"})
    assert created.status_code == 200

    listed = await institute.get(f"/departments/{programme_id}")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1


# ============================================================
# BATCH
# ============================================================

async def test_create_batch_requires_programme_or_department(superadmin_client, client_factory):
    institute = await _institute(superadmin_client, client_factory)
    resp = await institute.post("/batches", json={"semesters": [{"semester_number": 1}]})
    assert resp.status_code == 400


async def test_create_batch_requires_semesters(superadmin_client, client_factory):
    institute = await _institute(superadmin_client, client_factory)
    resp = await institute.post("/batches", json={"programme_id": str(ObjectId())})
    assert resp.status_code == 422


async def test_create_and_get_batch(superadmin_client, client_factory):
    institute = await _institute(superadmin_client, client_factory)
    school = await institute.post("/schools", json={"school_name": "School A"})
    school_id = school.json()["school"]["id"]
    programme = await institute.post("/programmes", json={"school_id": school_id, "programme_name": "B.Tech"})
    programme_id = programme.json()["programme"]["id"]

    created = await institute.post("/batches", json={
        "programme_id": programme_id, "semesters": [{"semester_number": 1}],
    })
    assert created.status_code == 200
    batch_id = created.json()["batch_id"]

    fetched = await institute.get(f"/batches/{batch_id}")
    assert fetched.status_code == 200


# ============================================================
# SUBJECT
# ============================================================

async def test_create_subject_rejects_missing_fields(superadmin_client, client_factory):
    institute = await _institute(superadmin_client, client_factory)
    resp = await institute.post("/subjects", json={"subject_name": "Physics"})
    assert resp.status_code == 422


async def test_create_and_update_subject(superadmin_client, client_factory):
    institute = await _institute(superadmin_client, client_factory)
    school = await institute.post("/schools", json={"school_name": "School A"})
    school_id = school.json()["school"]["id"]
    programme = await institute.post("/programmes", json={"school_id": school_id, "programme_name": "B.Tech"})
    programme_id = programme.json()["programme"]["id"]

    created = await institute.post("/subjects", json={
        "school_id": school_id, "programme_id": programme_id, "subject_name": "Physics",
    })
    assert created.status_code == 200
    subject_id = created.json()["subject_id"]

    updated = await institute.put(f"/subjects/{subject_id}", json={"credits": 4})
    assert updated.status_code == 200

    fetched = await institute.get(f"/subject/{subject_id}")
    assert fetched.status_code == 200
    assert fetched.json()["subject"]["credits"] == 4


async def test_update_subject_not_found(superadmin_client, client_factory):
    institute = await _institute(superadmin_client, client_factory)
    resp = await institute.put(f"/subjects/{ObjectId()}", json={"credits": 4})
    assert resp.status_code == 404
