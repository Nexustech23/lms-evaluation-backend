VALID_PAYLOAD = {
    "first_name": "Jane",
    "last_name": "Doe",
    "role": "institute",
    "topic": "pricing",
    "email": "jane@example.com",
    "message": "How much does this cost?",
}


async def test_create_contact_success(client):
    resp = await client.post("/contact", json=VALID_PAYLOAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["email"] == "jane@example.com"
    assert body["data"]["read"] is False


async def test_create_contact_rejects_missing_field(client):
    payload = {**VALID_PAYLOAD}
    del payload["message"]
    resp = await client.post("/contact", json=payload)
    assert resp.status_code == 422


async def test_create_contact_rejects_invalid_email(client):
    resp = await client.post("/contact", json={**VALID_PAYLOAD, "email": "not-an-email"})
    assert resp.status_code == 422


async def test_create_contact_rejects_invalid_role(client):
    resp = await client.post("/contact", json={**VALID_PAYLOAD, "role": "hacker"})
    assert resp.status_code == 422


async def test_admin_routes_require_superadmin(client):
    resp = await client.get("/admin/contact-queries")
    assert resp.status_code == 401


async def test_admin_can_list_and_manage_contacts(client, superadmin_client):
    created = await client.post("/contact", json=VALID_PAYLOAD)
    contact_id = created.json()["data"]["id"]

    listed = await superadmin_client.get("/admin/contact-queries")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["unread_count"] == 1

    single = await superadmin_client.get(f"/admin/contact-queries/{contact_id}")
    assert single.status_code == 200
    assert single.json()["data"]["id"] == contact_id

    marked = await superadmin_client.patch(f"/admin/contact-queries/{contact_id}/read")
    assert marked.status_code == 200
    assert marked.json()["data"]["read"] is True

    deleted = await superadmin_client.delete(f"/admin/contact-queries/{contact_id}")
    assert deleted.status_code == 200

    missing = await superadmin_client.get(f"/admin/contact-queries/{contact_id}")
    assert missing.status_code == 404
