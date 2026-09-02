import logging
from typing import Optional, Tuple

import jwt
from bson import ObjectId
from fastapi import Depends, HTTPException, Request, status
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from app.core.config import settings
from app.core.redis_client import (
    get_cached_account_state,
    set_cached_account_state,
    tokens_revoked_after,
)
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

async def get_current_identity(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> dict:
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
    if not user_id or not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    # A valid signature is no longer enough on its own: the account must
    # still exist and be active, must not predate a logout / forced-logout /
    # password change, and (when flagged) must have changed a temporary
    # password before doing anything else. This state is cached in Redis for
    # ACCOUNT_STATE_TTL seconds so it isn't a Mongo round-trip on every
    # request; a Redis/Mongo error fails OPEN (unknown -> not blocked).
    state = await _resolve_account_state(db, user_id)
    if state.get("blocked"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account is inactive")

    cutoff = await tokens_revoked_after(user_id)
    if cutoff is not None:
        iat = payload.get("iat", 0)
        if isinstance(iat, (int, float)) and iat < cutoff:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session has been logged out")

    if state.get("must_change_password") and request.url.path not in _PASSWORD_CHANGE_EXEMPT_PATHS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password change required. Set a new password to continue.",
        )

    return {"user_id": user_id, "role": payload.get("role", state.get("role"))}


# Endpoints reachable while must_change_password is set (so the user can
# actually change it and the client can render the prompt).
_PASSWORD_CHANGE_EXEMPT_PATHS = {"/me", "/logout", "/profile", "/profile/change-password"}


async def _resolve_account_state(db: AsyncIOMotorDatabase, user_id: str) -> dict:
    cached = await get_cached_account_state(user_id)
    if cached is not None:
        return cached

    try:
        user = await db["users"].find_one(
            {"_id": ObjectId(user_id)},
            {"is_active": 1, "is_deleted": 1, "role": 1, "must_change_password": 1},
        )
    except Exception:
        logging.warning("get_current_identity: users lookup failed for %s — failing open", user_id)
        return {}  # fail open — unknown state, not treated as blocked

    if not user:
        state = {"blocked": True}
    else:
        state = {
            "blocked": user.get("is_deleted") is True or not user.get("is_active", True),
            "must_change_password": bool(user.get("must_change_password", False)),
            "role": user.get("role"),
        }
    await set_cached_account_state(user_id, state)
    return state


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


async def can_use_mycareerguru(db: AsyncIOMotorDatabase, identity: dict) -> bool:
    """
    MyCareerGuru access rule:
      - SELF_LEARNER (role 7): always true — it's their own product.
      - INSTITUTE_STUDENT (role 4): true only if BOTH the student's
        institute has MyCareerGuru enabled (hasMyCareerGuruAccess on the
        institute admin's own user doc, set by Super Admin at onboarding /
        via PUT /institute/{user_id}) AND the student's specific school has
        it enabled (mycareerguru_enabled on that schoolDetails document, set
        by the institute admin via PUT /schools/{school_id}) — an institute
        can pilot the feature with one school before enabling it campus-wide.
      - Every other role: false. Resolved live on each call rather than
        cached on the student's own user doc, so toggling either flag takes
        effect immediately without needing to re-cascade to every student.
    """
    role = identity.get("role")
    if role == SELF_LEARNER:
        return True
    if role != INSTITUTE_STUDENT:
        return False

    user_id = identity.get("user_id")
    if not user_id or not ObjectId.is_valid(user_id):
        return False

    student = await db["studentDetails"].find_one({"user_id": ObjectId(user_id), "role": INSTITUTE_STUDENT})
    if not student:
        return False

    institute = await db["instituteDetails"].find_one({"_id": student.get("institute_id")})
    if not institute:
        return False

    institute_admin = await db["users"].find_one({"_id": institute.get("user_id")})
    if not institute_admin or not institute_admin.get("hasMyCareerGuruAccess", False):
        return False

    school = await db["schoolDetails"].find_one({"_id": student.get("school_id")})
    return bool(school and school.get("mycareerguru_enabled", False))


async def require_mycareerguru_access(
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> dict:
    """
    Router-level dependency for the MyCareerGuru surface (roadmap.py,
    self_learner_analytics.py, self_learner_course_material.py, ai_tutor.py,
    mock_tests.py). Only ever blocks INSTITUTE_STUDENT callers who fail
    can_use_mycareerguru — every other role that could already reach these
    routers keeps its existing access unchanged, since gating them wasn't
    part of this feature's scope and could regress access nobody asked to
    have revoked.
    """
    if identity.get("role") == INSTITUTE_STUDENT and not await can_use_mycareerguru(db, identity):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="MyCareerGuru is not enabled for your institute or school yet. Contact your institute admin.",
        )
    return identity


async def validate_entity_ownership(
    collection: AsyncIOMotorCollection, entity_id: str, institute_id: ObjectId
) -> Tuple[Optional[dict], Optional[dict], Optional[int]]:
    if not entity_id or not ObjectId.is_valid(entity_id):
        return None, {"error": "Invalid ID"}, 400

    entity = await collection.find_one({"_id": ObjectId(entity_id), "institute_id": ObjectId(institute_id)})
    if not entity:
        return None, {"error": "Access denied or entity not found"}, 403

    return entity, None, None


async def require_entity_in_institute(
    collection: AsyncIOMotorCollection, entity_id: str, institute_id: ObjectId
) -> dict:
    """
    Raising variant of validate_entity_ownership, for the many ID-addressed
    routes (institute_hierarchy.py's get/update/delete of school / programme /
    department / batch / subject) that previously acted on any ObjectId with
    no tenant check — a cross-institute IDOR.

    Returns the entity document only when it exists AND belongs to
    institute_id. Otherwise raises:
      - 400 when entity_id is malformed
      - 404 when it's missing OR owned by another institute (deliberately
        indistinguishable, so a caller can't enumerate other tenants' ids)
    """
    entity, err, code = await validate_entity_ownership(collection, entity_id, institute_id)
    if err:
        raise HTTPException(status_code=400 if code == 400 else 404, detail=err["error"])
    return entity
