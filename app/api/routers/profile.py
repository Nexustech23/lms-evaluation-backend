# ============================================================
# PROFILE ROUTER
# Ported from routes/profile_routes.py + controllers/profile_controller.py
#
# Role map: 1 superadmin, 2 institute, 3 faculty, 4 institute_student,
#           5 tutor, 6 tutor_student, 7 self_learner
# ============================================================

import re
from datetime import datetime, timezone
from typing import Any, Dict

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import (
    FACULTY,
    INSTITUTE,
    SUPERADMIN,
    TUTOR,
    get_current_identity,
    get_current_user_and_institute,
    require_role,
)
from app.core.security import hash_password, verify_password
from app.db.mongodb import get_database
from app.models.user import serialize_doc
from app.schemas.profile import (
    ChangePasswordRequest,
    FacultyUpdateRequest,
    InstituteUpdateRequest,
    ProfileUpdateRequest,
    SelfLearnerUpdateRequest,
    TutorUpdateRequest,
)
from app.utils.cascade import cascade_institute_access, cascade_institute_status, cascade_tutor_status

router = APIRouter(tags=["profile"])


def _validate_color(color) -> str:
    if isinstance(color, str) and re.match(r"^#([A-Fa-f0-9]{6})$", color):
        return color
    return "#FF7F10"


def _validate_language(language) -> str:
    if isinstance(language, str) and language.lower() in ["english", "hindi", "bengali"]:
        return language.lower()
    return "english"


def _common_user_fields(data: dict) -> dict:
    fields = {}
    if "fullName" in data and data["fullName"]:
        fields["fullName"] = data["fullName"].strip()
    if "phone" in data:
        fields["phone"] = data["phone"]
    if "profileImage" in data:
        fields["profileImage"] = data["profileImage"]
    if "language" in data:
        fields["language"] = _validate_language(data["language"])
    return fields


# ============================================================
# PROFILE (any logged-in user)
# ============================================================

@router.get("/profile")
async def get_profile(
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user_id = identity["user_id"]
    user = await db["users"].find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    role = user.get("role")
    profile = {
        "id": str(user["_id"]),
        "fullName": user.get("fullName"),
        "email": user.get("email"),
        "phone": user.get("phone"),
        "role": role,
        "profileImage": user.get("profileImage"),
        "color": user.get("color", "#FF7F10"),
        "language": user.get("language", "english"),
        "is_active": user.get("is_active", True),
        "hasCOAccess": user.get("hasCOAccess", False),
        "hasQPGAccess": user.get("hasQPGAccess", False),
    }

    if role == 2:
        institute = await db["instituteDetails"].find_one({"user_id": ObjectId(user_id)})
        if institute:
            profile["institute_profile"] = serialize_doc(institute)

    elif role == 3:
        faculty = await db["facultyDetails"].find_one({"user_id": ObjectId(user_id)})
        if faculty:
            profile["faculty_profile"] = serialize_doc(faculty)
            institute = await db["instituteDetails"].find_one({"_id": faculty.get("institute_id")})
            if institute:
                profile["institute_id"] = str(institute["_id"])
                profile["institute_name"] = institute.get("institute_name", "")

    elif role == 4:
        student = await db["studentDetails"].find_one({"user_id": ObjectId(user_id)})
        if student:
            profile["student_profile"] = serialize_doc(student)
            profile["institute_id"] = str(student.get("institute_id", ""))

    elif role == 5:
        tutor = await db["tutorDetails"].find_one({"user_id": ObjectId(user_id)})
        if tutor:
            profile["tutor_profile"] = serialize_doc(tutor)

    elif role == 6:
        student = await db["studentDetails"].find_one({"user_id": ObjectId(user_id)})
        if student:
            profile["student_profile"] = serialize_doc(student)
            profile["tutor_id"] = str(student.get("tutor_id", ""))

    return profile


@router.put("/profile")
async def update_profile(
    payload: ProfileUpdateRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status_code=400, detail="No data provided")

    user_id = identity["user_id"]
    user = await db["users"].find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    role = user.get("role")
    now = datetime.now(timezone.utc)
    user_fields = _common_user_fields(data)

    if role == 1:
        if "color" in data:
            user_fields["color"] = _validate_color(data["color"])
        message = "Superadmin profile updated successfully"

    elif role == 2:
        if "color" in data:
            color = _validate_color(data["color"])
            user_fields["color"] = color

            institute = await db["instituteDetails"].find_one({"user_id": ObjectId(user_id)})
            if institute:
                institute_id = institute["_id"]
                await db["instituteDetails"].update_one(
                    {"_id": institute_id}, {"$set": {"color": color, "updated_at": now}}
                )
                await cascade_institute_access(db, institute_id, ObjectId(user_id), color=color)

        institute_fields = {}
        allowed_institute_fields = [
            "institute_name", "short_name", "institute_code", "email", "phone", "website",
            "address_line1", "address_line2", "city", "state", "country", "pincode",
            "affiliation", "accreditation", "established_year", "logo_url", "banner_url",
            "description",
        ]
        for field in allowed_institute_fields:
            if field in data:
                institute_fields[field] = data[field]

        if institute_fields:
            institute_fields["updated_at"] = now
            await db["instituteDetails"].update_one({"user_id": ObjectId(user_id)}, {"$set": institute_fields})

        message = "Institute profile updated successfully"

    elif role == 3:
        if "color" in data:
            raise HTTPException(status_code=403, detail="Faculty color is inherited from institute and cannot be changed")

        faculty_fields = {}
        allowed_faculty_fields = [
            "designation", "qualification", "experience_years", "bio",
            "profile_image", "specialization", "joining_date", "employee_code",
        ]
        for field in allowed_faculty_fields:
            if field in data:
                faculty_fields[field] = data[field]

        if faculty_fields:
            faculty_fields["updated_at"] = now
            await db["facultyDetails"].update_one({"user_id": ObjectId(user_id)}, {"$set": faculty_fields})

        message = "Faculty profile updated successfully"

    elif role == 4:
        if "color" in data:
            raise HTTPException(status_code=403, detail="Students cannot update color")

        student_fields = {}
        for field in ["roll_number", "enrollment_number", "year", "bio"]:
            if field in data:
                student_fields[field] = data[field]

        if student_fields:
            student_fields["updated_at"] = now
            await db["studentDetails"].update_one(
                {"user_id": ObjectId(user_id), "role": 4}, {"$set": student_fields}
            )

        message = "Student profile updated successfully"

    elif role == 5:
        if "color" in data:
            user_fields["color"] = _validate_color(data["color"])

        tutor_fields = {}
        for field in ["bio", "qualification", "experience", "subject_specialization"]:
            if field in data:
                tutor_fields[field] = data[field]

        if tutor_fields:
            tutor_fields["updated_at"] = now
            await db["tutorDetails"].update_one({"user_id": ObjectId(user_id)}, {"$set": tutor_fields})

        message = "Tutor profile updated successfully"

    elif role == 6:
        if "color" in data:
            raise HTTPException(status_code=403, detail="Students cannot update color")

        student_fields = {}
        for field in ["bio", "year"]:
            if field in data:
                student_fields[field] = data[field]

        if student_fields:
            student_fields["updated_at"] = now
            await db["studentDetails"].update_one(
                {"user_id": ObjectId(user_id), "role": 6}, {"$set": student_fields}
            )

        message = "Tutor student profile updated successfully"

    elif role == 7:
        if "color" in data:
            raise HTTPException(status_code=403, detail="Self learners cannot update color")
        message = "Profile updated successfully"

    else:
        raise HTTPException(status_code=400, detail=f"Profile update not supported for role {role}")

    if user_fields:
        user_fields["updated_at"] = now
        await db["users"].update_one({"_id": ObjectId(user_id)}, {"$set": user_fields})

    return {"message": message}


@router.put("/profile/change-password")
async def change_password(
    payload: ChangePasswordRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user_id = identity["user_id"]
    user = await db["users"].find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not verify_password(payload.currentPassword, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Incorrect current password")

    new_hash = hash_password(payload.newPassword)
    await db["users"].update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"password_hash": new_hash, "updated_at": datetime.now(timezone.utc)}},
    )

    return {"message": "Password updated successfully"}


# ============================================================
# INSTITUTES — superadmin only
# ============================================================

@router.get("/institutes", dependencies=[Depends(require_role(SUPERADMIN))])
async def get_all_institutes(
    page: int = Query(1),
    limit: int = Query(10),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    skip = (page - 1) * limit
    query = {"role": 2}

    cursor = db["users"].find(
        query, {"_id": 1, "fullName": 1, "email": 1, "created_at": 1, "role": 1, "is_active": 1}
    ).skip(skip).limit(limit)
    users = [u async for u in cursor]

    user_ids = [u["_id"] for u in users]
    inst_map = {}
    async for inst in db["instituteDetails"].find(
        {"user_id": {"$in": user_ids}}, {"user_id": 1, "token_usage": 1, "token_limit": 1}
    ):
        inst_map[str(inst["user_id"])] = {
            "token_usage": inst.get("token_usage", {}),
            "token_limit": inst.get("token_limit"),  # None = unlimited (pre-existing institute)
        }

    result = []
    for user in users:
        uid = str(user["_id"])
        entry = inst_map.get(uid, {})
        token_usage = entry.get("token_usage", {})
        token_limit = entry.get("token_limit")

        gemini_used = token_usage.get("gemini", {}).get("total_tokens", 0)
        claude_used = token_usage.get("claude", {}).get("total_tokens", 0)
        gemini_limit = (token_limit or {}).get("gemini")
        claude_limit = (token_limit or {}).get("claude")

        result.append({
            "id": uid,
            "fullName": user.get("fullName"),
            "email": user.get("email"),
            "created_at": user.get("created_at"),
            "is_active": user.get("is_active", True),
            "gemini_total_tokens": gemini_used,
            "claude_total_tokens": claude_used,
            "gemini_token_limit": gemini_limit,
            "claude_token_limit": claude_limit,
            "gemini_tokens_remaining": (gemini_limit - gemini_used) if gemini_limit is not None else None,
            "claude_tokens_remaining": (claude_limit - claude_used) if claude_limit is not None else None,
        })

    total = await db["users"].count_documents(query)

    return {"success": True, "page": page, "limit": limit, "total": total, "data": result}


@router.get("/institute/{user_id}", dependencies=[Depends(require_role(SUPERADMIN, INSTITUTE))])
async def get_institute(user_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    institute = await db["instituteDetails"].find_one({"user_id": ObjectId(user_id), "is_deleted": {"$ne": True}})
    if not institute:
        raise HTTPException(status_code=404, detail="Institute not found")

    user_doc = await db["users"].find_one({"_id": ObjectId(user_id)})

    institute["_id"] = str(institute["_id"])
    institute["user_id"] = str(institute["user_id"])

    return {
        "success": True,
        "institute": institute,
        "hasCOAccess": user_doc.get("hasCOAccess", False) if user_doc else False,
        "hasQPGAccess": user_doc.get("hasQPGAccess", False) if user_doc else False,
    }


@router.put("/institute/{user_id}", dependencies=[Depends(require_role(SUPERADMIN))])
async def update_institute(
    user_id: str,
    payload: InstituteUpdateRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    user_object_id = ObjectId(user_id)
    institute = await db["instituteDetails"].find_one({"user_id": user_object_id, "is_deleted": {"$ne": True}})
    if not institute:
        raise HTTPException(status_code=404, detail="Institute not found")

    institute_id = institute["_id"]
    institute_data = payload.institute
    has_co_access = payload.hasCOAccess
    has_qpg_access = payload.hasQPGAccess

    field_map = {
        "institute_name": institute_data.get("institute_name"),
        "short_name": institute_data.get("short_name"),
        "institute_code": institute_data.get("institute_code"),
        "phone": institute_data.get("phone"),
        "website": institute_data.get("website"),
        "address_line1": institute_data.get("address_line1"),
        "address_line2": institute_data.get("address_line2"),
        "city": institute_data.get("city"),
        "state": institute_data.get("state"),
        "country": institute_data.get("country"),
        "pincode": institute_data.get("pincode"),
        "affiliation": institute_data.get("affiliation"),
        "accreditation": institute_data.get("accreditation"),
        "established_year": institute_data.get("established_year"),
        "logo_url": institute_data.get("logo_url"),
        "banner_url": institute_data.get("banner_url"),
        "description": institute_data.get("description"),
        "color": institute_data.get("color"),
    }
    update_fields = {k: v for k, v in field_map.items() if v is not None}
    update_fields["updated_at"] = datetime.now(timezone.utc)

    # Top-up: superadmin sets a new total limit (not an amount to add).
    # Uses dot notation so a bare {"gemini": X} doesn't clobber the other
    # provider's limit, and so this also works for institutes that had no
    # token_limit document at all (previously "unlimited") — setting one
    # provider's limit here leaves the other provider unlimited until the
    # superadmin sets that one too.
    gemini_limit = institute_data.get("gemini_token_limit")
    if gemini_limit is not None:
        if not isinstance(gemini_limit, int) or gemini_limit < 0:
            raise HTTPException(status_code=400, detail="gemini_token_limit must be a non-negative integer")
        update_fields["token_limit.gemini"] = gemini_limit

    claude_limit = institute_data.get("claude_token_limit")
    if claude_limit is not None:
        if not isinstance(claude_limit, int) or claude_limit < 0:
            raise HTTPException(status_code=400, detail="claude_token_limit must be a non-negative integer")
        update_fields["token_limit.claude"] = claude_limit

    await db["instituteDetails"].update_one({"_id": institute_id}, {"$set": update_fields})

    if institute_data.get("is_active") is not None:
        await db["users"].update_one({"_id": user_object_id}, {"$set": {"is_active": institute_data["is_active"]}})
        await cascade_institute_status(db, user_object_id, institute_data["is_active"])

    await cascade_institute_access(
        db, institute_id, user_object_id,
        co_access=has_co_access, qpg_access=has_qpg_access, color=institute_data.get("color"),
    )

    updated = await db["instituteDetails"].find_one({"_id": institute_id})
    updated["_id"] = str(updated["_id"])
    updated["user_id"] = str(updated["user_id"])

    return {"success": True, "message": "Institute updated successfully", "institute": updated}


# ============================================================
# FACULTY — institute admin (role 2) manages
# ============================================================

@router.get("/faculty", dependencies=[Depends(require_role(SUPERADMIN, INSTITUTE, FACULTY))])
async def get_all_faculties(
    search: str = Query(""),
    page: int = Query(1),
    limit: int = Query(10),
    programmeId: str | None = Query(None),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, institute_id, error = await get_current_user_and_institute(identity, db)
    if not institute_id or not ObjectId.is_valid(str(institute_id)):
        raise HTTPException(status_code=400, detail="Valid institute_id required")

    query: Dict[str, Any] = {"institute_id": ObjectId(institute_id)}

    programme = None
    if programmeId and ObjectId.is_valid(programmeId):
        programme = await db["programmeDetails"].find_one({"_id": ObjectId(programmeId)})
        if programme and programme.get("school_id"):
            query["school_id"] = programme["school_id"]

    result = []
    async for doc in db["facultyDetails"].find(query).sort("created_at", -1):
        u = await db["users"].find_one(
            {"_id": doc["user_id"]}, {"fullName": 1, "email": 1, "phone": 1, "is_active": 1}
        ) or {}

        if search and search.strip():
            search_lower = search.strip().lower()
            searchable_text = " ".join([
                str(u.get("fullName", "")), str(u.get("email", "")), str(u.get("phone", "")),
                str(doc.get("designation", "")), str(doc.get("qualification", "")),
                str(doc.get("specialization", "")), str(doc.get("employee_code", "")),
            ]).lower()
            if search_lower not in searchable_text:
                continue

        if limit == 0:
            result.append({"id": str(doc["_id"]), "user_id": str(doc["user_id"]), "fullName": u.get("fullName")})
            continue

        school = None
        if doc.get("school_id"):
            school = await db["schoolDetails"].find_one(
                {"_id": doc["school_id"]}, {"school_name": 1, "school_code": 1}
            )

        result.append({
            "id": str(doc["_id"]),
            "user_id": str(doc["user_id"]),
            "school_id": str(doc.get("school_id", "")),
            "institute_id": str(doc["institute_id"]),
            "fullName": u.get("fullName"),
            "email": u.get("email"),
            "phone": u.get("phone"),
            "is_active": u.get("is_active", True),
            "designation": doc.get("designation"),
            "qualification": doc.get("qualification"),
            "experience_years": doc.get("experience_years"),
            "specialization": doc.get("specialization"),
            "employee_code": doc.get("employee_code"),
            "joining_date": doc.get("joining_date"),
            "created_at": doc.get("created_at"),
            "programme_code": (programme.get("programme_code") if programme else None),
            "school_name": (school.get("school_name") if school else None),
            "school_code": (school.get("school_code") if school else None),
        })

    total = len(result)
    if limit > 0:
        start = (page - 1) * limit
        result = result[start:start + limit]

    return {
        "success": True,
        "page": page,
        "limit": limit,
        "total": total,
        "total_pages": ((total + limit - 1) // limit) if limit > 0 else 1,
        "filters": {"programmeId": programmeId, "search": search},
        "faculties": result,
    }


@router.put("/faculty/{faculty_id}", dependencies=[Depends(require_role(SUPERADMIN, INSTITUTE))])
async def update_faculty(
    faculty_id: str,
    payload: FacultyUpdateRequest = FacultyUpdateRequest(),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    data = payload.model_dump(exclude_unset=True)
    if not ObjectId.is_valid(faculty_id):
        raise HTTPException(status_code=400, detail="Invalid faculty ID")

    faculty_doc = await db["facultyDetails"].find_one({"_id": ObjectId(faculty_id), "is_deleted": {"$ne": True}})
    if not faculty_doc:
        raise HTTPException(status_code=404, detail="Faculty not found")

    user_id = faculty_doc.get("user_id")
    if not user_id:
        raise HTTPException(status_code=404, detail="Faculty user not found")

    user_fields: Dict[str, Any] = {}
    if "fullName" in data and data["fullName"]:
        user_fields["fullName"] = data["fullName"].strip()
    if "phone" in data:
        user_fields["phone"] = data["phone"]
    if "email" in data and data["email"]:
        if await db["users"].find_one({"email": data["email"], "_id": {"$ne": user_id}}):
            raise HTTPException(status_code=409, detail="Email already in use")
        user_fields["email"] = data["email"]
    if "password" in data and data["password"]:
        user_fields["password_hash"] = hash_password(data["password"])

    if user_fields:
        user_fields["updated_at"] = datetime.now(timezone.utc)
        await db["users"].update_one({"_id": user_id}, {"$set": user_fields})

    faculty_fields: Dict[str, Any] = {}
    for field in ["designation", "qualification", "experience_years", "specialization",
                  "employee_code", "joining_date", "bio", "is_active"]:
        if field in data:
            faculty_fields[field] = data[field]
    if "school_id" in data and ObjectId.is_valid(data["school_id"]):
        faculty_fields["school_id"] = ObjectId(data["school_id"])

    if faculty_fields:
        faculty_fields["updated_at"] = datetime.now(timezone.utc)
        await db["facultyDetails"].update_one({"_id": ObjectId(faculty_id)}, {"$set": faculty_fields})

    updated_doc = await db["facultyDetails"].find_one({"_id": ObjectId(faculty_id)}) or {}
    updated_user = await db["users"].find_one({"_id": user_id}, {"fullName": 1, "email": 1, "phone": 1}) or {}

    return {
        "success": True,
        "message": "Faculty updated successfully",
        "faculty": {
            "id": str(updated_doc.get("_id", "")),
            "user_id": str(updated_doc.get("user_id", "")),
            "school_id": str(updated_doc.get("school_id", "")),
            "fullName": updated_user.get("fullName"),
            "email": updated_user.get("email"),
            "phone": updated_user.get("phone"),
            "designation": updated_doc.get("designation"),
            "qualification": updated_doc.get("qualification"),
            "experience_years": updated_doc.get("experience_years"),
            "specialization": updated_doc.get("specialization"),
            "employee_code": updated_doc.get("employee_code"),
            "joining_date": updated_doc.get("joining_date"),
            "bio": updated_doc.get("bio"),
            "is_active": updated_doc.get("is_active", True),
        },
    }


@router.delete("/faculty/{faculty_id}", dependencies=[Depends(require_role(SUPERADMIN, INSTITUTE))])
async def delete_faculty(faculty_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(faculty_id):
        raise HTTPException(status_code=400, detail="Invalid faculty ID")

    faculty_doc = await db["facultyDetails"].find_one({"_id": ObjectId(faculty_id)})
    if not faculty_doc:
        raise HTTPException(status_code=404, detail="Faculty not found")

    fid = faculty_doc["_id"]
    uid = faculty_doc["user_id"]

    await db["facultyDetails"].delete_one({"_id": fid})
    await db["users"].delete_one({"_id": uid})

    return {"success": True, "message": f"Faculty deleted (faculty_id: {fid}, user_id: {uid})"}


# ============================================================
# INSTITUTE STUDENTS (role 4)
# ============================================================

@router.get("/institute-students", dependencies=[Depends(require_role(SUPERADMIN, INSTITUTE, FACULTY))])
async def get_all_institute_students(
    page: int = Query(1),
    limit: int = Query(10),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, institute_id, error = await get_current_user_and_institute(identity, db)
    if not institute_id or not ObjectId.is_valid(str(institute_id)):
        raise HTTPException(status_code=400, detail="Valid institute_id required")

    query = {"institute_id": ObjectId(institute_id), "role": 4}
    skip = (page - 1) * limit

    cursor = db["studentDetails"].find(query).skip(skip).limit(limit)
    result = []
    async for doc in cursor:
        u = await db["users"].find_one(
            {"_id": doc["user_id"]}, {"fullName": 1, "email": 1, "phone": 1, "is_active": 1, "created_at": 1}
        ) or {}
        result.append({
            "id": str(doc["_id"]),
            "user_id": str(doc["user_id"]),
            "institute_id": str(doc["institute_id"]),
            "fullName": u.get("fullName"),
            "email": u.get("email"),
            "phone": u.get("phone"),
            "is_active": u.get("is_active", True),
            "created_at": u.get("created_at"),
        })

    total = await db["studentDetails"].count_documents(query)

    return {"success": True, "page": page, "limit": limit, "total": total, "students": result}


@router.delete("/institute-students/{student_id}", dependencies=[Depends(require_role(SUPERADMIN, INSTITUTE))])
async def delete_institute_student(student_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(student_id):
        raise HTTPException(status_code=400, detail="Invalid student ID")

    doc = await db["studentDetails"].find_one({"_id": ObjectId(student_id), "role": 4})
    if not doc:
        raise HTTPException(status_code=404, detail="Institute student not found")

    uid = doc["user_id"]
    await db["studentDetails"].delete_one({"_id": ObjectId(student_id)})
    await db["users"].delete_one({"_id": uid})

    return {"success": True, "message": f"Institute student deleted (student_id: {student_id}, user_id: {uid})"}


# ============================================================
# TUTORS (role 5) — managed by superadmin
# ============================================================

@router.get("/tutors", dependencies=[Depends(require_role(SUPERADMIN))])
async def get_all_tutors(
    page: int = Query(1),
    limit: int = Query(10),
    status: str | None = Query(None),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    skip = (page - 1) * limit
    query: Dict[str, Any] = {"role": 5}
    if status == "pending":
        query["is_active"] = False
    elif status == "active":
        query["is_active"] = True

    cursor = db["users"].find(
        query, {"_id": 1, "fullName": 1, "email": 1, "phone": 1, "is_active": 1, "created_at": 1}
    ).skip(skip).limit(limit)

    result = []
    async for u in cursor:
        uid = u["_id"]
        tutor = await db["tutorDetails"].find_one({"user_id": uid}) or {}
        result.append({
            "id": str(uid),
            "fullName": u.get("fullName"),
            "email": u.get("email"),
            "phone": u.get("phone"),
            "is_active": u.get("is_active", False),
            "created_at": u.get("created_at"),
            "coaching_name": tutor.get("coaching_name", ""),
            "student_count": await db["studentDetails"].count_documents({"tutor_id": uid, "role": 6}),
        })

    total = await db["users"].count_documents(query)

    return {"success": True, "page": page, "limit": limit, "total": total, "tutors": result}


@router.put("/tutor/{tutor_user_id}", dependencies=[Depends(require_role(SUPERADMIN))])
async def update_tutor(
    tutor_user_id: str,
    payload: TutorUpdateRequest = TutorUpdateRequest(),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    data = payload.model_dump(exclude_unset=True)
    if not ObjectId.is_valid(tutor_user_id):
        raise HTTPException(status_code=400, detail="Invalid tutor user ID")

    tutor_object_id = ObjectId(tutor_user_id)
    tutor_user = await db["users"].find_one({"_id": tutor_object_id, "role": 5})
    if not tutor_user:
        raise HTTPException(status_code=404, detail="Tutor not found")

    user_fields: Dict[str, Any] = {}
    for field in ["fullName", "phone", "email"]:
        if field in data and data[field]:
            user_fields[field] = data[field]

    if "is_active" in data:
        is_active = bool(data["is_active"])
        await db["users"].update_one(
            {"_id": tutor_object_id},
            {"$set": {"is_active": is_active, "updated_at": datetime.now(timezone.utc)}},
        )
        await cascade_tutor_status(db, tutor_user_id, is_active)

    if user_fields:
        user_fields["updated_at"] = datetime.now(timezone.utc)
        await db["users"].update_one({"_id": tutor_object_id}, {"$set": user_fields})

    updated = await db["users"].find_one({"_id": tutor_object_id}, {"password_hash": 0}) or {}
    updated["_id"] = str(updated["_id"])

    return {"success": True, "message": "Tutor updated successfully", "tutor": updated}


@router.delete("/tutor/{tutor_user_id}", dependencies=[Depends(require_role(SUPERADMIN))])
async def delete_tutor(tutor_user_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(tutor_user_id):
        raise HTTPException(status_code=400, detail="Invalid tutor user ID")

    tutor_object_id = ObjectId(tutor_user_id)
    tutor_user = await db["users"].find_one({"_id": tutor_object_id, "role": 5})
    if not tutor_user:
        raise HTTPException(status_code=404, detail="Tutor not found")

    student_docs = [s async for s in db["studentDetails"].find({"tutor_id": tutor_object_id, "role": 6})]
    student_user_ids = [s["user_id"] for s in student_docs]

    if student_user_ids:
        await db["users"].delete_many({"_id": {"$in": [ObjectId(uid) for uid in student_user_ids]}})

    await db["studentDetails"].delete_many({"tutor_id": tutor_object_id, "role": 6})
    await db["tutorDetails"].delete_one({"user_id": tutor_object_id})
    await db["users"].delete_one({"_id": tutor_object_id})

    return {"success": True, "message": f"Tutor and {len(student_user_ids)} tutor_student(s) deleted"}


# ============================================================
# TUTOR STUDENTS (role 6)
# ============================================================

@router.get("/tutor-students", dependencies=[Depends(require_role(SUPERADMIN, TUTOR))])
async def get_all_tutor_students(
    page: int = Query(1),
    limit: int = Query(10),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    tutor_user_id = identity["user_id"]
    query = {"tutor_id": ObjectId(tutor_user_id), "role": 6}
    skip = (page - 1) * limit

    cursor = db["studentDetails"].find(query).skip(skip).limit(limit)
    result = []
    async for doc in cursor:
        u = await db["users"].find_one(
            {"_id": doc["user_id"]}, {"fullName": 1, "email": 1, "phone": 1, "is_active": 1, "created_at": 1}
        ) or {}
        result.append({
            "id": str(doc["_id"]),
            "user_id": str(doc["user_id"]),
            "tutor_id": str(doc["tutor_id"]),
            "fullName": u.get("fullName"),
            "email": u.get("email"),
            "phone": u.get("phone"),
            "is_active": u.get("is_active", True),
            "created_at": u.get("created_at"),
        })

    total = await db["studentDetails"].count_documents(query)

    return {"success": True, "page": page, "limit": limit, "total": total, "students": result}


@router.delete("/tutor-students/{student_id}", dependencies=[Depends(require_role(SUPERADMIN, TUTOR))])
async def delete_tutor_student(student_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(student_id):
        raise HTTPException(status_code=400, detail="Invalid student ID")

    doc = await db["studentDetails"].find_one({"_id": ObjectId(student_id), "role": 6})
    if not doc:
        raise HTTPException(status_code=404, detail="Tutor student not found")

    uid = doc["user_id"]
    await db["studentDetails"].delete_one({"_id": ObjectId(student_id)})
    await db["users"].delete_one({"_id": uid})

    return {"success": True, "message": f"Tutor student deleted (student_id: {student_id}, user_id: {uid})"}


# ============================================================
# SELF LEARNERS (role 7) — managed by superadmin
# ============================================================

@router.get("/self-learners", dependencies=[Depends(require_role(SUPERADMIN))])
async def get_all_self_learners(
    page: int = Query(1),
    limit: int = Query(10),
    status: str | None = Query(None),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    skip = (page - 1) * limit
    query: Dict[str, Any] = {"role": 7}
    if status == "pending":
        query["is_active"] = False
    elif status == "active":
        query["is_active"] = True

    cursor = db["users"].find(
        query, {"_id": 1, "fullName": 1, "email": 1, "phone": 1, "is_active": 1, "created_at": 1}
    ).skip(skip).limit(limit)

    result = []
    async for u in cursor:
        result.append({
            "id": str(u["_id"]),
            "fullName": u.get("fullName"),
            "email": u.get("email"),
            "phone": u.get("phone"),
            "is_active": u.get("is_active", False),
            "created_at": u.get("created_at"),
        })

    total = await db["users"].count_documents(query)

    return {"success": True, "page": page, "limit": limit, "total": total, "self_learners": result}


@router.put("/self-learner/{learner_user_id}", dependencies=[Depends(require_role(SUPERADMIN))])
async def update_self_learner(
    learner_user_id: str,
    payload: SelfLearnerUpdateRequest = SelfLearnerUpdateRequest(),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    data = payload.model_dump(exclude_unset=True)
    if not ObjectId.is_valid(learner_user_id):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    learner = await db["users"].find_one({"_id": ObjectId(learner_user_id), "role": 7})
    if not learner:
        raise HTTPException(status_code=404, detail="Self learner not found")

    update_fields: Dict[str, Any] = {}
    for field in ["fullName", "phone", "is_active"]:
        if field in data:
            update_fields[field] = data[field]

    if update_fields:
        update_fields["updated_at"] = datetime.now(timezone.utc)
        await db["users"].update_one({"_id": ObjectId(learner_user_id)}, {"$set": update_fields})

    updated = await db["users"].find_one({"_id": ObjectId(learner_user_id)}, {"password_hash": 0}) or {}
    updated["_id"] = str(updated["_id"])

    return {"success": True, "message": "Self learner updated successfully", "self_learner": updated}


@router.delete("/self-learner/{learner_user_id}", dependencies=[Depends(require_role(SUPERADMIN))])
async def delete_self_learner(learner_user_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(learner_user_id):
        raise HTTPException(status_code=400, detail="Invalid user ID")

    learner = await db["users"].find_one({"_id": ObjectId(learner_user_id), "role": 7})
    if not learner:
        raise HTTPException(status_code=404, detail="Self learner not found")

    await db["users"].delete_one({"_id": ObjectId(learner_user_id)})

    return {"success": True, "message": f"Self learner deleted (user_id: {learner_user_id})"}
