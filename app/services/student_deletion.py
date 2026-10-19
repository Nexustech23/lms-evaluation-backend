"""Tenant-scoped, transactional student/account deletion."""
import logging
from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import PyMongoError
from app.api.deps import get_current_user_and_institute
from app.models.user import SUPERADMIN, INSTITUTE, TUTOR
from app.core.redis_client import bust_account_state, revoke_user_tokens


async def delete_scoped_student(db, identity, student_id, student_role):
    if not ObjectId.is_valid(student_id):
        raise HTTPException(400, "Invalid student ID")
    allowed_role = INSTITUTE if student_role == 4 else TUTOR
    if identity.get("role") not in (SUPERADMIN, allowed_role):
        raise HTTPException(403, "Unauthorized")
    query = {"_id": ObjectId(student_id), "role": student_role}
    if identity["role"] == INSTITUTE:
        _, institute, error = await get_current_user_and_institute(identity, db)
        if error:
            raise HTTPException(error[1], error[0])
        query["institute_id"] = ObjectId(str(institute))
    elif identity["role"] == TUTOR:
        query["tutor_id"] = ObjectId(identity["user_id"])
    if not await db["studentDetails"].find_one(query):
        raise HTTPException(404, "Student not found")

    async def remove(session):
        student = await db["studentDetails"].find_one(query, session=session)
        if not student:
            raise HTTPException(409, "Student changed. Refresh and try again.")
        uid = student.get("user_id")
        if not isinstance(uid, ObjectId):
            raise HTTPException(409, "Student account link needs correction before deletion")
        account_query = {"_id": uid, "role": student_role}
        if not await db["users"].find_one(account_query, session=session):
            raise HTTPException(409, "Student account link needs correction before deletion")
        deleted = await db["studentDetails"].delete_one({**query, "user_id": uid}, session=session)
        if deleted.deleted_count != 1:
            raise HTTPException(409, "Student changed. Refresh and try again.")
        result = await db["users"].delete_one(account_query, session=session)
        if result.deleted_count != 1:
            raise HTTPException(409, "Student account changed. Refresh and try again.")
        return uid

    try:
        async with await db.client.start_session() as session:
            uid = await session.with_transaction(remove)
    except PyMongoError as exc:
        logging.exception("Student deletion transaction failed")
        raise HTTPException(503, "Student deletion could not be committed. No partial deletion is allowed; verify MongoDB transaction support and retry.") from exc
    # The committed user deletion remains authoritative if cache invalidation
    # fails. Account-state lookups reject the missing user after cache expiry.
    for invalidate in (revoke_user_tokens, bust_account_state):
        try:
            await invalidate(str(uid))
        except Exception:
            logging.exception("Could not invalidate deleted student session cache")
    return {"success": True, "message": "Student and login account deleted successfully"}
