# ============================================================
# AUTH ROUTER
# Roles: superadmin(1), institute(2), faculty(3),
#        institute_student(4), tutor(5), tutor_student(6), self_learner(7)
# Ported from controllers/auth_controller.py + routes/auth_routes.py
# ============================================================

import re
import secrets
from typing import Any, Dict

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Response
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity, get_current_user
from app.core.config import settings
from app.core.rate_limit import login_rate_limit
from app.core.redis_client import revoke_user_tokens
from app.core.security import create_access_token, hash_password, set_access_cookie, unset_access_cookie, verify_password
from app.db.mongodb import get_database
from app.models.faculty import create_faculty_document
from app.models.institute import create_institute_document
from app.models.student import create_student_document
from app.models.user import create_user_document, serialize_user
from app.schemas.auth import BulkStudentEnrollmentRequest, LoginRequest, RegisterPayload

router = APIRouter(tags=["auth"])

# ============================================================
# CONSTANTS
# ============================================================

ROLE_NAME_TO_NUMBER = {
    "superadmin": 1,
    "institute": 2,
    "faculty": 3,
    "institute_student": 4,
    "tutor": 5,
    "tutor_student": 6,
    "self_learner": 7,
}

PENDING_APPROVAL_ROLES = {5, 7}  # tutor, self_learner

# Precomputed once at import so an "unknown email" login still pays one
# bcrypt verification (constant-ish timing vs. a real wrong-password login).
_DUMMY_PASSWORD_HASH = hash_password("not-a-real-password-timing-equalizer")


def _validate_color(color) -> str:
    if isinstance(color, str) and re.match(r"^#([A-Fa-f0-9]{6})$", color):
        return color
    return "#FF7F10"


def _validate_language(language) -> str:
    if isinstance(language, str) and language.lower() in ["english", "hindi"]:
        return language.lower()
    return "english"


def _resolve_role(data: dict):
    role_name = data.get("role")

    if not role_name or not isinstance(role_name, str):
        return None, ({"error": "role is required and must be a string (e.g. 'tutor')"}, 400)

    role_number = ROLE_NAME_TO_NUMBER.get(role_name.strip().lower())
    if role_number is None:
        return None, ({"error": f"Unknown role '{role_name}'. Valid roles: {list(ROLE_NAME_TO_NUMBER.keys())}"}, 400)

    return role_number, None


async def _get_caller(db: AsyncIOMotorDatabase, current_user_identity: str):
    if not current_user_identity or not ObjectId.is_valid(current_user_identity):
        return None
    return await db["users"].find_one({"_id": ObjectId(current_user_identity)})


# ============================================================
# ROLE-SPECIFIC REGISTRATION HANDLERS (private)
# ============================================================

async def _register_institute(db, data, password_hash, current_user_identity):
    caller = await _get_caller(db, current_user_identity)
    if not caller or caller.get("role") != 1:
        return {"error": "Only superadmin can create an institute account"}, 403

    institute_data = data.get("institute")
    if not institute_data:
        return {"error": "Institute details are required in the 'institute' field"}, 400

    color = _validate_color(data.get("color"))
    language = _validate_language(data.get("language"))

    user_doc = create_user_document(
        {
            "fullName": data["fullName"],
            "email": data["email"].lower(),
            "role": 2,
            "phone": data.get("phone"),
            "hasCOAccess": data.get("hasCOAccess", False),
            "hasQPGAccess": data.get("hasQPGAccess", False),
            "hasMyCareerGuruAccess": data.get("hasMyCareerGuruAccess", False),
            "is_active": True,
            "color": color,
            "language": language,
        },
        password_hash,
    )

    user_result = await db["users"].insert_one(user_doc)
    user_id = user_result.inserted_id

    institute_data["color"] = color
    institute_doc = create_institute_document({**institute_data, "user_id": str(user_id)})
    await db["instituteDetails"].insert_one(institute_doc)

    return {"message": "Institute registered successfully"}, 201


async def _register_faculty(db, data, password_hash, current_user_identity):
    caller = await _get_caller(db, current_user_identity)
    if not caller or caller.get("role") != 2:
        return {"error": "Only institute admin can create faculty"}, 403

    if "color" in data:
        return {"error": "Faculty color is inherited from institute and cannot be set manually"}, 400

    institute = await db["instituteDetails"].find_one(
        {"user_id": ObjectId(current_user_identity), "is_deleted": {"$ne": True}}
    )
    if not institute:
        return {"error": "Institute not found"}, 404

    co_access = caller.get("hasCOAccess", False)
    qpg_access = caller.get("hasQPGAccess", False)
    color = institute.get("color", "#FF7F10")
    language = _validate_language(data.get("language"))

    user_doc = create_user_document(
        {
            "fullName": data["fullName"],
            "email": data["email"].lower(),
            "role": 3,
            "phone": data.get("phone"),
            "hasCOAccess": co_access,
            "hasQPGAccess": qpg_access,
            "is_active": True,
            "color": color,
            "language": language,
        },
        password_hash,
    )

    user_result = await db["users"].insert_one(user_doc)
    user_id = user_result.inserted_id

    faculty_doc = create_faculty_document({
        **data,
        "user_id": str(user_id),
        "institute_id": str(institute["_id"]),
    })
    await db["facultyDetails"].insert_one(faculty_doc)

    return {"message": "Faculty registered successfully"}, 201


async def _register_institute_student(db, data, password_hash, current_user_identity):
    caller = await _get_caller(db, current_user_identity)
    if not caller or caller.get("role") not in [2, 3]:
        return {"error": "Only institute admin or faculty can enroll students"}, 403

    institute = None
    if caller.get("role") == 2:
        institute = await db["instituteDetails"].find_one(
            {"user_id": ObjectId(current_user_identity), "is_deleted": {"$ne": True}}
        )
    elif caller.get("role") == 3:
        faculty = await db["facultyDetails"].find_one(
            {"user_id": ObjectId(current_user_identity), "is_deleted": {"$ne": True}}
        )
        if faculty:
            institute = await db["instituteDetails"].find_one(
                {"_id": faculty["institute_id"], "is_deleted": {"$ne": True}}
            )

    if not institute:
        return {"error": "Institute not found"}, 404

    # NOTE: Flask's original (InstituteStudentModel.py) also independently
    # requires contact_no/enrollment_no at the model layer, but its own
    # controller-level required_fields list (mirrored above) didn't include
    # them either — so a missing contact_no/enrollment_no raised an uncaught
    # ValueError -> 500 in Flask too. Fixed here by listing them up front,
    # turning that into a proper 400 (found while adding test coverage).
    required_fields = ["fullName", "email", "school_id", "programme_id", "roll_no", "contact_no", "enrollment_no"]
    for field in required_fields:
        if not data.get(field):
            return {"error": f"{field} is required"}, 400

    # NOTE: Flask's original (auth_controller.py) has this identical bug —
    # institute.get("short_name", "college") only falls back when the key is
    # *absent*, not when it's None, and short_name is None for any institute
    # that didn't set one at registration. Fixed here (found while adding
    # test coverage for this path): `or` catches both cases.
    institute_short_name = re.sub(r"[^a-z0-9]", "", (institute.get("short_name") or "college").strip().lower())
    clean_name = re.sub(r"[^a-z0-9]", "", data["fullName"].split()[0].strip().lower())

    # Unique login id: append a numeric suffix on collision instead of hard
    # failing (two students named "John" at the same institute must both
    # enroll). Not @gmail.com — that namespace isn't ours and the address is
    # never actually mailed; it's only a login handle.
    base_local = f"{clean_name}.{institute_short_name}"
    college_email = f"{base_local}@{settings.STUDENT_EMAIL_DOMAIN}"
    _suffix = 1
    while await db["users"].find_one({"email": college_email.lower(), "is_deleted": {"$ne": True}}):
        _suffix += 1
        college_email = f"{base_local}{_suffix}@{settings.STUDENT_EMAIL_DOMAIN}"

    # Password: use the caller-supplied one if given, otherwise a per-student
    # random one-time password. NEVER a guessable institute-wide default
    # (the old "shortname@123" let anyone who knew the short name log in as
    # every student). A generated password forces a change on first login.
    supplied_password = data.get("password")
    password_was_generated = not supplied_password
    default_password = supplied_password or secrets.token_urlsafe(9)
    password_hash = hash_password(default_password)

    existing_student = await db["studentDetails"].find_one({
        "roll_no": data.get("roll_no"),
        "programme_id": ObjectId(data.get("programme_id")),
        "is_deleted": {"$ne": True},
    })
    if existing_student:
        return {"error": "Roll number already exists"}, 400

    language = _validate_language(data.get("language"))

    user_doc = create_user_document(
        {
            "fullName": data["fullName"].strip(),
            "email": college_email.lower(),
            "role": 4,
            "phone": data.get("contact_no"),
            "hasCOAccess": False,
            "hasQPGAccess": False,
            "is_active": True,
            "color": institute.get("color", "#FF7F10"),
            "language": language,
        },
        password_hash,
    )
    if password_was_generated:
        # Frontend must route this user to a mandatory password-change screen
        # on next login (flag is surfaced in /login and /me, cleared by
        # PUT /profile/change-password).
        user_doc["must_change_password"] = True

    user_result = await db["users"].insert_one(user_doc)
    user_id = user_result.inserted_id

    student_payload = {
        "user_id": user_id,
        "institute_id": institute["_id"],
        "school_id": ObjectId(data.get("school_id")),
        "programme_id": ObjectId(data.get("programme_id")),

        "programme_name": data.get("programme_name"),
        "enrollment_no": data.get("enrollment_no"),
        "roll_no": data.get("roll_no"),

        "name": data.get("fullName"),
        "father_name": data.get("father_name"),
        "dob": data.get("dob"),
        "gender": data.get("gender"),
        "address": data.get("address"),

        "personal_email": data.get("email"),
        "email": college_email,
        "college_email": college_email,
        "contact_no": data.get("contact_no"),

        "college_short_name": institute_short_name,
    }

    student_doc = create_student_document(student_payload)
    student_doc["role"] = 4
    student_doc["is_deleted"] = False

    await db["studentDetails"].insert_one(student_doc)

    return {
        "message": "Student enrolled successfully",
        "college_email": college_email,
        "default_password": default_password,
        "login_email": college_email,
        "must_change_password": password_was_generated,
    }, 201


async def _register_tutor(db, data, password_hash):
    language = _validate_language(data.get("language"))

    user_doc = create_user_document(
        {
            "fullName": data["fullName"],
            "email": data["email"].lower(),
            "role": 5,
            "phone": data.get("phone"),
            "hasCOAccess": False,
            "hasQPGAccess": False,
            "is_active": False,
            "color": "#FF7F10",
            "language": language,
        },
        password_hash,
    )

    await db["users"].insert_one(user_doc)

    return {"message": "Tutor registration submitted. Your account is pending superadmin approval."}, 201


async def _register_tutor_student(db, data, password_hash, current_user_identity):
    caller = await _get_caller(db, current_user_identity)
    if not caller or caller.get("role") != 5:
        return {"error": "Only a tutor can add tutor students"}, 403

    if not caller.get("is_active", False):
        return {"error": "Your tutor account is not yet verified. Please wait for superadmin approval."}, 403

    language = _validate_language(data.get("language"))

    user_doc = create_user_document(
        {
            "fullName": data["fullName"],
            "email": data["email"].lower(),
            "role": 6,
            "phone": data.get("phone"),
            "hasCOAccess": False,
            "hasQPGAccess": False,
            "is_active": True,
            "color": "#FF7F10",
            "language": language,
        },
        password_hash,
    )

    user_result = await db["users"].insert_one(user_doc)
    user_id = user_result.inserted_id

    await db["studentDetails"].insert_one({
        "user_id": user_id,
        "tutor_id": ObjectId(current_user_identity),
        "role": 6,
        "is_deleted": False,
    })

    return {"message": "Tutor student registered successfully"}, 201


async def _register_self_learner(db, data, password_hash):
    language = _validate_language(data.get("language"))

    user_doc = create_user_document(
        {
            "fullName": data["fullName"],
            "email": data["email"].lower(),
            "role": 7,
            "phone": data.get("phone"),
            "hasCOAccess": False,
            "hasQPGAccess": False,
            "is_active": False,
            "color": "#FF7F10",
            "language": language,
        },
        password_hash,
    )

    await db["users"].insert_one(user_doc)

    return {"message": "Registration submitted. Your account is pending superadmin approval."}, 201


# ============================================================
# REGISTER
# ============================================================

@router.post("/register")
async def register(
    payload: RegisterPayload,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    # role/fullName/email/password/faculty-color/institute-details shape are
    # now enforced by the RegisterPayload discriminated union (422 instead of
    # the previous custom 400 messages for those specific cases) — see
    # app/schemas/auth.py. Per-role business-rule checks (caller authorization,
    # duplicate email, duplicate roll number, etc.) stay in the handlers below
    # unchanged, since those depend on DB state Pydantic can't see.
    data = payload.model_dump(exclude_unset=True)
    email = payload.email
    password = payload.password

    try:
        role, role_error = _resolve_role(data)
        if role_error:
            body, code = role_error
            raise HTTPException(status_code=code, detail=body["error"])

        if await db["users"].find_one({"email": email.lower()}):
            raise HTTPException(status_code=400, detail="A user with this email already exists")

        # institute_student is the one role whose password may be omitted (an
        # auto-generated one-time password is issued in _register_institute_student,
        # which derives its own hash and ignores this value). Every other role's
        # schema requires a password, so this is never None for them.
        password_hash = hash_password(password) if password else None
        current_user_identity = identity["user_id"]

        if role == 2:
            body, code = await _register_institute(db, data, password_hash, current_user_identity)
        elif role == 3:
            body, code = await _register_faculty(db, data, password_hash, current_user_identity)
        elif role == 4:
            body, code = await _register_institute_student(db, data, password_hash, current_user_identity)
        elif role == 5:
            body, code = await _register_tutor(db, data, password_hash)
        elif role == 6:
            body, code = await _register_tutor_student(db, data, password_hash, current_user_identity)
        elif role == 7:
            body, code = await _register_self_learner(db, data, password_hash)
        else:
            body, code = {"error": "Registration not supported for this role"}, 400

        if code >= 400:
            raise HTTPException(status_code=code, detail=body.get("error"))
        return body

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Registration failed")


# ============================================================
# LOGIN
# ============================================================

@router.post("/login", dependencies=[Depends(login_rate_limit)])
async def login(
    response: Response,
    payload: LoginRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    try:
        email = payload.email
        password = payload.password

        user = await db["users"].find_one({"email": email, "is_deleted": False})
        # One response for "no such account" and "wrong password" so an
        # attacker can't enumerate which emails are registered. When the
        # account is absent, still run one bcrypt verification against a
        # fixed dummy hash so the response timing doesn't give it away.
        if not user:
            verify_password(password, _DUMMY_PASSWORD_HASH)
            raise HTTPException(status_code=401, detail="Invalid email or password")

        if not verify_password(password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid email or password")

        if not user.get("is_active", True):
            if user.get("role") in PENDING_APPROVAL_ROLES:
                raise HTTPException(status_code=403, detail="Your account is pending superadmin approval. Please wait.")
            raise HTTPException(status_code=403, detail="Your account has been deactivated. Please contact your administrator.")

        access_token = create_access_token(identity=str(user["_id"]), additional_claims={"role": user["role"]})

        serialized_user = serialize_user(user)
        serialized_user["hasCOAccess"] = user.get("hasCOAccess", False)
        serialized_user["hasQPGAccess"] = user.get("hasQPGAccess", False)
        serialized_user["must_change_password"] = bool(user.get("must_change_password", False))

        set_access_cookie(response, access_token)

        return {"message": "Login successful", "user": serialized_user}

    except HTTPException:
        raise
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception:
        raise HTTPException(status_code=500, detail="Login failed")


# ============================================================
# GET CURRENT USER (/me)
# ============================================================

@router.get("/me")
async def get_me(
    identity: dict = Depends(get_current_identity),
    user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    try:
        role = identity.get("role")
        user_id = identity["user_id"]

        serialized_user = serialize_user(user)
        serialized_user["hasCOAccess"] = user.get("hasCOAccess", False)
        serialized_user["hasQPGAccess"] = user.get("hasQPGAccess", False)
        serialized_user["must_change_password"] = bool(user.get("must_change_password", False))

        if role == 2:
            institute = await db["instituteDetails"].find_one(
                {"user_id": ObjectId(user_id), "is_deleted": {"$ne": True}}
            )
            if institute:
                serialized_user["institute_id"] = str(institute["_id"])
                serialized_user["banner_url"] = institute.get("banner_url", "")
                serialized_user["logo_url"] = institute.get("logo_url", "")

        elif role == 3:
            faculty = await db["facultyDetails"].find_one(
                {"user_id": ObjectId(user_id), "is_deleted": {"$ne": True}}
            )
            if faculty:
                institute = await db["instituteDetails"].find_one(
                    {"_id": ObjectId(faculty["institute_id"]), "is_deleted": {"$ne": True}}
                )
                if institute:
                    serialized_user["institute_id"] = str(institute["_id"])
                    serialized_user["banner_url"] = institute.get("banner_url", "")
                    serialized_user["logo_url"] = institute.get("logo_url", "")

        elif role == 5:
            tutor = await db["tutorDetails"].find_one(
                {"user_id": ObjectId(user_id), "is_deleted": {"$ne": True}}
            )
            if tutor:
                serialized_user["tutor_id"] = str(tutor["_id"])
                serialized_user["coaching_name"] = tutor.get("coaching_name", "")

        elif role == 4:
            student = await db["studentDetails"].find_one(
                {"user_id": ObjectId(user_id), "is_deleted": {"$ne": True}}
            )
            if student:
                serialized_user["institute_id"] = str(student.get("institute_id", ""))

        elif role == 6:
            student = await db["studentDetails"].find_one(
                {"user_id": ObjectId(user_id), "is_deleted": {"$ne": True}}
            )
            if student:
                serialized_user["tutor_id"] = str(student.get("tutor_id", ""))

        return {"role": role, "user": serialized_user}

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to fetch user")


# ============================================================
# LOGOUT
# ============================================================

@router.post("/logout")
async def logout(response: Response, identity: dict = Depends(get_current_identity)):
    # Clearing the cookie only stops THIS client from sending the token; the
    # token itself stays valid until it expires. Record a revocation cutoff
    # so a copy of the token (stolen, or kept in another client) is rejected
    # by get_current_identity from now on.
    await revoke_user_tokens(identity["user_id"], settings.JWT_ACCESS_TOKEN_EXPIRE_DAYS * 24 * 60 * 60)
    unset_access_cookie(response)
    return {"message": "Logged out successfully"}


# ============================================================
# BULK INSTITUTE STUDENT ENROLLMENT
# ============================================================

@router.post("/bulk-student-enrollment")
async def bulk_student_enrollment(
    payload: BulkStudentEnrollmentRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    students = payload.students

    success_count = 0
    enrolled = []
    failed_students = []

    for index, student in enumerate(students):
        try:
            # password_hash here is ignored by _register_institute_student
            # (it derives its own from student["password"] or a generated
            # one-time password), passed only to satisfy the shared signature.
            body, code = await _register_institute_student(
                db, student, hash_password(secrets.token_urlsafe(9)), identity["user_id"]
            )

            if code == 201:
                success_count += 1
                enrolled.append({
                    "row": index + 1,
                    "student": student.get("fullName", "Unknown"),
                    "college_email": body.get("college_email"),
                    "default_password": body.get("default_password"),
                    "must_change_password": body.get("must_change_password", False),
                })
            else:
                failed_students.append({
                    "row": index + 1,
                    "student": student.get("fullName", "Unknown"),
                    "error": body.get("error", "Registration failed"),
                })
        except Exception as e:
            failed_students.append({
                "row": index + 1,
                "student": student.get("fullName", "Unknown"),
                "error": str(e),
            })

    return {
        "message": f"{success_count} students enrolled successfully",
        "success_count": success_count,
        "failed_count": len(failed_students),
        "enrolled": enrolled,
        "failed_students": failed_students,
    }
