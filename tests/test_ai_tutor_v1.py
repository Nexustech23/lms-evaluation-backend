"""
ai_tutor_v1 is confirmed dead code (the frontend never calls it — see the
module docstring in app/api/routers/ai_tutor_v1.py) but is still ported for
endpoint parity, so it still gets the same retrofit + coverage as every
other router. Tests stick to the no-file-upload path plus CRUD to avoid
on-disk side effects from this file's local disk writes (uploads/homework/).
"""

from unittest.mock import patch

from bson import ObjectId

from tests.test_security_fixes import _seed_and_login_user

_CLAUDE_PATCH = "app.api.routers.ai_tutor_v1.generate_homework_with_claude"
_PDF_PATCH = "app.api.routers.ai_tutor_v1.generate_homework_pdf"


async def _learner(client_factory, test_db):
    return await _seed_and_login_user(test_db, client_factory, role=7, name="AI Tutor V1 Learner")


async def test_create_homework_requires_auth(client):
    resp = await client.post("/homework-help", data={"prompt": "help"})
    assert resp.status_code == 401


async def test_create_homework_requires_prompt_or_file(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.post("/homework-help", data={"prompt": ""})
    assert resp.status_code == 400


async def test_create_homework_queues_and_completes(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    with patch(_CLAUDE_PATCH, return_value={
        "generated_content": "<html>solution</html>",
        "token_usage": {"input_tokens": 10, "output_tokens": 20},
    }), patch(_PDF_PATCH, return_value={"pdf_path": "uploads/generated_pdfs/x.pdf", "pdf_filename": "x.pdf"}):
        created = await learner.post("/homework-help", data={"prompt": "solve 2+2"})
    assert created.status_code == 202
    job_id = created.json()["jobId"]

    status = await learner.get(f"/homework-help/status/{job_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "completed"


async def test_homework_status_not_found(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.get("/homework-help/status/does-not-exist")
    assert resp.status_code == 404


async def test_crud_lifecycle(client_factory, test_db):
    learner = await _learner(client_factory, test_db)

    doc = await test_db["ai_tutor"].insert_one({
        "feature_type": "homework", "prompt": "test", "status": "completed", "notes_type": None,
    })
    doc_id = str(doc.inserted_id)

    listed = await learner.get("/get-all")
    assert listed.status_code == 200
    assert listed.json()["count"] == 1

    single = await learner.get(f"/get/{doc_id}")
    assert single.status_code == 200

    updated = await learner.put(f"/update/{doc_id}", json={"homework_type": "Detailed Solution"})
    assert updated.status_code == 200
    assert updated.json()["data"]["homework_type"] == "Detailed Solution"

    deleted = await learner.delete(f"/delete/{doc_id}")
    assert deleted.status_code == 200

    missing = await learner.get(f"/get/{doc_id}")
    assert missing.status_code == 404


async def test_update_rejects_unknown_fields_silently_ignored(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    doc = await test_db["ai_tutor"].insert_one({"feature_type": "homework", "status": "completed"})
    resp = await learner.put(f"/update/{doc.inserted_id}", json={"not_a_real_field": "x"})
    # Pydantic model has no extra="allow", so unknown top-level keys are
    # simply dropped by model_dump() rather than raising -- matches the
    # original allowed_updates-set filtering behavior.
    assert resp.status_code == 200


async def test_get_single_not_found(client_factory, test_db):
    learner = await _learner(client_factory, test_db)
    resp = await learner.get(f"/get/{ObjectId()}")
    assert resp.status_code == 404
