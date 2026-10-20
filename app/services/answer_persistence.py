"""Transactional receipt binding and explicit deletion tombstones."""
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import PyMongoError
from app.services.answer_identity import student_id_from_filename, require_unique_identity
from app.services.result_revision import touch_result_source


async def _transaction(db, callback):
    try:
        async with await db.client.start_session() as session:
            return await session.with_transaction(callback)
    except PyMongoError as exc:
        raise HTTPException(503, "Answer save/delete could not be confirmed. Refresh and retry; MongoDB transactions are required.") from exc


async def _exam_fence(db, folder, session):
    result = await db["newsavedDocs"].update_one({"_id": folder["_id"], "faculty_id": folder["faculty_id"]},
        {"$inc": {"answer_write_revision": 1}}, session=session)
    if result.matched_count != 1:
        raise HTTPException(409, "Exam changed or was deleted")
    return await db["newsavedDocs"].find_one({"_id": folder["_id"]}, session=session)


async def save_uploaded_answer(db, identity, folder, payload):
    query = {"_id": payload.fileId, "owner_user_id": ObjectId(identity["user_id"]),
        "exam_id": folder["_id"], "purpose": "answer-script", "url": payload.answer_script_url,
        "filename": payload.filename}
    candidate = ObjectId()
    async def save(session):
        current_exam = await _exam_fence(db, folder, session)
        receipt = await db["storageAssets"].find_one(query, session=session)
        if not receipt:
            raise HTTPException(404, "Verified upload not found. Upload this answer script again.")
        if receipt.get("state") == "deleted":
            raise HTTPException(409, "This answer was deleted. Upload a new file to create another answer.")
        answer_id = receipt.get("answer_id") or candidate
        existing = await db["answerDetails"].find_one({"_id": answer_id}, session=session)
        if existing:
            if existing.get("exam_id") != folder["_id"] or existing.get("fileId") != payload.fileId:
                raise HTTPException(409, "Upload receipt links to an inconsistent answer")
            return existing
        if receipt.get("answer_id"):
            # Legacy claimed/missing records cannot distinguish a prior delete
            # from an incomplete pre-transaction save. Never resurrect them.
            raise HTTPException(409, "Legacy upload needs reconciliation; upload a new file instead.")
        sid = student_id_from_filename(receipt["filename"])
        await require_unique_identity(db, folder["_id"], sid, answer_id, session)
        await touch_result_source(db, current_exam, session)
        now = datetime.now(timezone.utc)
        answer = {"_id": answer_id, "exam_id": folder["_id"], "faculty_id": ObjectId(str(folder["faculty_id"])),
            "student_id": sid, "filename": receipt["filename"], "answer_script_url": receipt["url"],
            "fileId": receipt["_id"], "verified_storage_ids": [receipt["_id"]], "created_at": now, "updated_at": now}
        await db["answerDetails"].insert_one(answer, session=session)
        await db["storageAssets"].update_one(query, {"$set": {"answer_id": answer_id, "state": "saved"}}, session=session)
        return answer
    return await _transaction(db, save)


async def remove_answer(db, identity, folder, answer_id):
    async def remove(session):
        current_exam = await _exam_fence(db, folder, session)
        query = {"_id": answer_id, "exam_id": folder["_id"]}
        answer = await db["answerDetails"].find_one(query, session=session)
        if not answer:
            raise HTTPException(409, "Answer changed or was already deleted")
        await touch_result_source(db, current_exam, session)
        if answer.get("fileId"):
            await db["storageAssets"].update_one({"_id": answer["fileId"], "answer_id": answer_id,
                "exam_id": folder["_id"], "owner_user_id": ObjectId(identity["user_id"])},
                {"$set": {"state": "deleted", "deleted_at": datetime.now(timezone.utc)}}, session=session)
        await db["answerDetails"].delete_one(query, session=session)
        return answer
    return await _transaction(db, remove)
