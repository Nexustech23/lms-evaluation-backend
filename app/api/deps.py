from typing import Optional, Tuple

import jwt
from bson import ObjectId
from fastapi import Depends, HTTPException, Request, status
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from app.core.config import settings
from app.core.security import decode_access_token
from app.db.mongodb import get_database
from app.models.user import (
    FACULTY,
    INSTITUTE,
    INSTITUTE_STUDENT,
    SELF_LEARNER,
    SUPERADMIN,
    TUTOR,
    TUTOR_STUDENT,
)

# ============================================================
# ROLE CONSTANTS
# Canonical source is app.models.user — re-exported here so existing
# `from app.api.deps import SUPERADMIN, ...` call sites keep working.
# ============================================================


# ============================================================
# IDENTITY (equivalent to @jwt_required() + get_jwt_identity()/get_jwt())
# ============================================================

def get_current_identity(request: Request) -> dict:
    token = request.cookies.get(settings.JWT_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing authentication token")

    try:
        payload = decode_access_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    return {"user_id": user_id, "role": payload.get("role")}


def require_role(*allowed_roles: int):
    """Dependency factory — equivalent to Flask's inline `if role not in [...]: 403`."""

    def _dependency(identity: dict = Depends(get_current_identity)) -> dict:
        if identity.get("role") not in allowed_roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Unauthorized")
        return identity

    return _dependency


# ============================================================
# FULL USER DOCUMENT (equivalent to /me's user_collection.find_one)
# ============================================================

async def get_current_user(
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> dict:
    if not ObjectId.is_valid(identity["user_id"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid user id")

    user = await db["users"].find_one({"_id": ObjectId(identity["user_id"]), "is_deleted": False})
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    return user


# ============================================================
# INSTITUTE / FACULTY CONTEXT HELPERS
# (ported from middlewares/institute_middleware.py — kept as plain
#  helpers rather than Depends() since several controllers need to
#  try one, then fall back to the other, on failure)
# ============================================================

ErrorTuple = Optional[Tuple[str, int]]


async def get_current_user_and_institute(
    identity: dict, db: AsyncIOMotorDatabase
) -> Tuple[Optional[dict], Optional[ObjectId], ErrorTuple]:
    user_id = identity.get("user_id")

    if not ObjectId.is_valid(user_id):
        return None, None, ("Invalid user id", 401)

    user = await db["users"].find_one({"_id": ObjectId(user_id)})
    if not user:
        return None, None, ("User not found", 401)

    institute = await db["instituteDetails"].find_one({"user_id": ObjectId(user_id)})
    if not institute:
        return None, None, ("Institute profile not found", 400)

    return user, institute["_id"], None


async def get_current_user_and_faculty_details(
    identity: dict, db: AsyncIOMotorDatabase
) -> Tuple[Optional[dict], Optional[ObjectId], ErrorTuple]:
    user_id = identity.get("user_id")

    if not ObjectId.is_valid(user_id):
        return None, None, ("Invalid user id", 401)

    user = await db["users"].find_one({"_id": ObjectId(user_id)})
    if not user:
        return None, None, ("User not found", 401)

    faculty = await db["facultyDetails"].find_one({"user_id": ObjectId(user_id)})
    if not faculty:
        return None, None, ("Faculty profile not found", 400)

    return user, faculty["_id"], None


async def resolve_current_institute_id(identity: dict, db: AsyncIOMotorDatabase) -> ObjectId:
    """
    Resolves the institute_id for the calling user, whether they're an
    institute admin or a faculty member of that institute (mirrors the
    institute-then-faculty-fallback pattern used across several Flask
    controllers, e.g. import_marks_controller.py's _get_current_institute_id).
    Raises HTTPException directly — convenient for router call sites that
    don't need the finer-grained (user, id, error) tuple.
    """
    user, institute_id, error = await get_current_user_and_institute(identity, db)
    if not error:
        return institute_id

    faculty_user, faculty_id, faculty_error = await get_current_user_and_faculty_details(identity, db)
    if faculty_error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    institute_id = faculty_user.get("institute_id")
    if not institute_id:
        faculty_doc = await db["facultyDetails"].find_one({"_id": faculty_id})
        institute_id = faculty_doc.get("institute_id") if faculty_doc else None

    if not institute_id:
        raise HTTPException(status_code=400, detail="Institute profile not found")

    return institute_id if isinstance(institute_id, ObjectId) else ObjectId(institute_id)


async def validate_entity_ownership(
    collection: AsyncIOMotorCollection, entity_id: str, institute_id: ObjectId
) -> Tuple[Optional[dict], Optional[dict], Optional[int]]:
    if not entity_id or not ObjectId.is_valid(entity_id):
        return None, {"error": "Invalid ID"}, 400

    entity = await collection.find_one({"_id": ObjectId(entity_id), "institute_id": ObjectId(institute_id)})
    if not entity:
        return None, {"error": "Access denied or entity not found"}, 403

    return entity, None, None
