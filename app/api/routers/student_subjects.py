# ============================================================
# STUDENT <-> SUBJECT ENROLLMENT LINKING ROUTER
# Ported from controllers/institute/student_controller.py +
# routes/institute/student_routes.py.
#
# Feeds Faculty Materials' GET /student/materials (app/api/routers/
# faculty_materials.py), which reads the same StudentSubjectRelationModel
# collection this router writes to.
# ============================================================

import logging
from typing import Any, Dict, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import FACULTY, INSTITUTE, INSTITUTE_STUDENT, get_current_identity
from app.db.mongodb import get_database
from app.models.student_subject_relation import (
    create_student_subject_relation_document,
    serialize_student_subject_relation,
)
from app.schemas.student_subject import LinkStudentSubjectsRequest

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["student-subjects"])


async def _resolve_institute_for_admin_or_faculty(identity: dict, db: AsyncIOMotorDatabase):
    """Mirrors Flask: institute-admin resolves by own user_id, faculty by their institute_id."""
    if identity.get("role") == INSTITUTE:
        return await db["instituteDetails"].find_one({"user_id": ObjectId(identity["user_id"])})

    faculty = await db["facultyDetails"].find_one({"user_id": ObjectId(identity["user_id"])})
    if not faculty:
        return None
    return await db["instituteDetails"].find_one({"_id": faculty["institute_id"]})


@router.post("/link-student-subjects")
async def link_student_subjects(
    payload: LinkStudentSubjectsRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    student = await db["studentDetails"].find_one({
        "user_id": ObjectId(identity["user_id"]), "role": INSTITUTE_STUDENT, "is_deleted": {"$ne": True},
    })
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    subject_ids = payload.subject_ids

    linked_count = 0
    for subject_id in subject_ids:
        if not ObjectId.is_valid(subject_id):
            continue

        existing = await db["StudentSubjectRelationModel"].find_one({
            "student_id": student["_id"], "subject_id": ObjectId(subject_id), "is_deleted": {"$ne": True},
        })
        if existing:
            continue

        subject = await db["subjectDetails"].find_one({"_id": ObjectId(subject_id), "is_deleted": {"$ne": True}})
        if not subject:
            continue

        relation_doc = create_student_subject_relation_document({
            "student_id": str(student["_id"]),
            "user_id": identity["user_id"],
            "institute_id": str(student["institute_id"]),
            "school_id": str(student["school_id"]),
            "programme_id": str(student["programme_id"]),
            "department_id": str(subject.get("department_id")) if subject.get("department_id") else None,
            "batch_id": str(subject.get("batch_id")) if subject.get("batch_id") else None,
            "subject_id": subject_id,
            "semester": subject.get("semester"),
        })

        await db["StudentSubjectRelationModel"].insert_one(relation_doc)
        linked_count += 1

    return {"message": f"{linked_count} subjects linked successfully"}


@router.get("/student-subjects")
async def get_subjects_by_filters(
    batch: str = Query(...),
    semester: str = Query(...),
    department: Optional[str] = Query(None),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    student = await db["studentDetails"].find_one({
        "user_id": ObjectId(identity["user_id"]), "role": INSTITUTE_STUDENT, "is_deleted": {"$ne": True},
    })
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    if not ObjectId.is_valid(batch):
        raise HTTPException(status_code=400, detail="Invalid batch id")

    query: Dict[str, Any] = {
        "semester": int(semester),
        "batch_id": ObjectId(batch),
        "is_deleted": {"$ne": True},
    }
    if department and ObjectId.is_valid(department):
        query["department_id"] = ObjectId(department)

    subjects = []
    async for subject in db["subjectDetails"].find(query):
        faculty_name = None
        if subject.get("faculty_id"):
            faculty_details = await db["facultyDetails"].find_one({
                "_id": subject.get("faculty_id"), "is_deleted": {"$ne": True},
            })
            if faculty_details and faculty_details.get("user_id"):
                faculty_user = await db["users"].find_one({
                    "_id": faculty_details.get("user_id"), "is_deleted": {"$ne": True},
                })
                if faculty_user:
                    faculty_name = faculty_user.get("fullName")

        subjects.append({
            "id": str(subject["_id"]),
            "subject_name": subject.get("subject_name"),
            "subject_code": subject.get("subject_code"),
            "credits": subject.get("credits"),
            "faculty_name": faculty_name,
            "semester": subject.get("semester"),
            "batch_id": str(subject.get("batch_id")) if subject.get("batch_id") else None,
            "department_id": str(subject.get("department_id")) if subject.get("department_id") else None,
        })

    return {"subjects": subjects}


@router.get("/student-academic-filters")
async def get_academic_filters(
    department_id: Optional[str] = Query(None),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if identity.get("role") != INSTITUTE_STUDENT:
        raise HTTPException(status_code=403, detail="Only institute students allowed")

    student = await db["studentDetails"].find_one({
        "user_id": ObjectId(identity["user_id"]), "role": INSTITUTE_STUDENT, "is_deleted": {"$ne": True},
    })
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    programme_id = student["programme_id"]

    departments = [
        {"id": str(dep["_id"]), "name": dep.get("department_name")}
        async for dep in db["departmentDetails"].find({"programme_id": programme_id, "is_deleted": {"$ne": True}})
        if dep.get("department_name")
    ]

    batch_query: Dict[str, Any] = {"programme_id": programme_id, "is_deleted": {"$ne": True}}
    if department_id and ObjectId.is_valid(department_id):
        batch_query["department_id"] = ObjectId(department_id)

    batches = [
        {"id": str(b["_id"]), "name": b.get("batch_name"), "semesters": b.get("semesters")}
        async for b in db["batchDetails"].find(batch_query)
        if b.get("batch_name")
    ]

    return {"departments": departments, "batches": batches}


@router.get("/student-groups")
async def get_student_groups(
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if identity.get("role") not in (INSTITUTE, FACULTY):
        raise HTTPException(status_code=403, detail="Unauthorized")

    institute = await _resolve_institute_for_admin_or_faculty(identity, db)
    if not institute:
        raise HTTPException(status_code=404, detail="Institute not found")

    grouped: Dict[str, Dict[str, Any]] = {}

    async for student in db["studentDetails"].find({
        "institute_id": institute["_id"], "role": INSTITUTE_STUDENT, "is_deleted": {"$ne": True},
    }):
        school_id = str(student.get("school_id"))
        programme_id = str(student.get("programme_id"))
        batch = student.get("batch") or "N/A"
        semester = student.get("semester") or "N/A"
        key = f"{school_id}_{programme_id}_{batch}_{semester}"

        if key not in grouped:
            school = await db["schoolDetails"].find_one({"_id": ObjectId(school_id)}) if ObjectId.is_valid(school_id) else None
            programme = await db["programmeDetails"].find_one({"_id": ObjectId(programme_id)}) if ObjectId.is_valid(programme_id) else None
            grouped[key] = {
                "_id": key,
                "semester": school.get("school_name", "-") if school else "-",
                "subject_code": programme.get("programme_name", "-") if programme else "-",
                "subject_name": batch,
                "credits": semester,
                "subject_type": 0,
                "status": "Active",
                "students": [],
            }

        grouped[key]["subject_type"] += 1
        grouped[key]["students"].append({
            "id": str(student["_id"]),
            "name": student.get("name"),
            "roll": student.get("roll_no"),
            "email": student.get("college_email"),
            "status": "Active" if student.get("is_active", True) else "Inactive",
        })

    return {"groups": list(grouped.values())}


@router.get("/enrolled-students")
async def get_enrolled_students(
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if identity.get("role") not in (INSTITUTE, FACULTY):
        raise HTTPException(status_code=403, detail="Unauthorized")

    institute = await _resolve_institute_for_admin_or_faculty(identity, db)
    if not institute:
        raise HTTPException(status_code=404, detail="Institute not found")

    students = []
    async for student in db["studentDetails"].find({
        "institute_id": institute["_id"], "role": INSTITUTE_STUDENT, "is_deleted": {"$ne": True},
    }).sort("created_at", -1):
        students.append({
            "id": str(student["_id"]),
            "name": student.get("name"),
            "email": student.get("email"),
            "college_email": student.get("college_email"),
            "roll_no": student.get("roll_no"),
            "enrollment_no": student.get("enrollment_no"),
            "programme_name": student.get("programme_name"),
            "contact_no": student.get("contact_no"),
            "gender": student.get("gender"),
            "dob": student.get("dob"),
            "is_active": student.get("is_active", True),
        })

    return {"students": students}


@router.get("/student-enrolled-subjects")
async def get_student_enrolled_subjects(
    semester: Optional[str] = Query(None),
    batch: Optional[str] = Query(None),
    department: Optional[str] = Query(None),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if identity.get("role") != INSTITUTE_STUDENT:
        raise HTTPException(status_code=403, detail="Only institute students allowed")

    student = await db["studentDetails"].find_one({
        "user_id": ObjectId(identity["user_id"]), "role": INSTITUTE_STUDENT, "is_deleted": {"$ne": True},
    })
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    relation_query: Dict[str, Any] = {
        "student_id": student["_id"], "user_id": ObjectId(identity["user_id"]), "is_deleted": {"$ne": True},
    }
    if semester:
        relation_query["semester"] = int(semester)
    if batch and ObjectId.is_valid(batch):
        relation_query["batch_id"] = ObjectId(batch)
    if department and ObjectId.is_valid(department):
        relation_query["department_id"] = ObjectId(department)

    relations = [r async for r in db["StudentSubjectRelationModel"].find(relation_query)]

    subjects = []
    for relation in relations:
        subject = await db["subjectDetails"].find_one({"_id": relation.get("subject_id"), "is_deleted": {"$ne": True}})
        if not subject:
            continue

        faculty_name = None
        if subject.get("faculty_id"):
            faculty_details = await db["facultyDetails"].find_one({
                "_id": subject.get("faculty_id"), "is_deleted": {"$ne": True},
            })
            if faculty_details and faculty_details.get("user_id"):
                faculty_user = await db["users"].find_one({
                    "_id": faculty_details.get("user_id"), "is_deleted": {"$ne": True},
                })
                if faculty_user:
                    faculty_name = faculty_user.get("fullName")

        subjects.append({
            "relation_id": str(relation["_id"]),
            "id": str(subject["_id"]),
            "subject_name": subject.get("subject_name"),
            "subject_code": subject.get("subject_code"),
            "credits": subject.get("credits"),
            "faculty_name": faculty_name,
            "semester": subject.get("semester"),
            "batch_id": str(subject.get("batch_id")) if subject.get("batch_id") else None,
            "department_id": str(subject.get("department_id")) if subject.get("department_id") else None,
            "enrolled_at": relation.get("created_at"),
        })

    semesters = sorted({r.get("semester") for r in relations if r.get("semester") is not None})

    return {
        "already_enrolled": len(subjects) > 0,
        "semesters": semesters,
        "subjects": subjects,
    }
