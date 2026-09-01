# ============================================================
# FACULTY MATERIALS ROUTER
# Ported from controllers/institute/faculty_material_controller.py +
# routes/institute/faculty_material_routes.py.
#
# Note: GET /student/materials filters by the student's enrolled subjects
# (the StudentSubjectRelationModel collection). Until Phase 3 (student <->
# subject linking) lands, no enrollment rows exist yet, so this returns an
# empty list for every student — same collection, no behavior change once
# Phase 3 ships.
# ============================================================

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import FACULTY, INSTITUTE, INSTITUTE_STUDENT, get_current_identity
from app.db.mongodb import get_database
from app.models.faculty_material import create_faculty_material_document, serialize_faculty_material
from app.models.student_material_interaction import create_student_material_interaction_document
from app.schemas.faculty_material import FacultyMaterialCreate, StudentMaterialInteractionCreate

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["faculty-materials"])


@router.post("/faculty/materials")
async def create_faculty_material(
    payload: FacultyMaterialCreate,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if identity.get("role") not in (INSTITUTE, FACULTY):
        raise HTTPException(status_code=403, detail="Only institute admin or faculty can publish material")

    data = payload.model_dump()
    user_id = identity["user_id"]
    faculty = None

    if identity.get("role") == FACULTY:
        faculty = await db["facultyDetails"].find_one({"user_id": ObjectId(user_id), "is_deleted": {"$ne": True}})
        if not faculty:
            raise HTTPException(status_code=404, detail="Faculty profile not found")
    elif identity.get("role") == INSTITUTE:
        admin_institute = await db["instituteDetails"].find_one(
            {"user_id": ObjectId(user_id), "is_deleted": {"$ne": True}}
        )
        if not admin_institute:
            raise HTTPException(status_code=404, detail="Institute profile not found")
        faculty_id = data.get("faculty_id")
        if faculty_id and ObjectId.is_valid(faculty_id):
            # the target faculty must belong to THIS admin's institute
            faculty = await db["facultyDetails"].find_one({
                "_id": ObjectId(faculty_id),
                "institute_id": admin_institute["_id"],
                "is_deleted": {"$ne": True},
            })
        if not faculty:
            raise HTTPException(status_code=400, detail="faculty_id is required and must belong to your institute")

    if not ObjectId.is_valid(data.get("subject_id", "")):
        raise HTTPException(status_code=404, detail="Subject not found")
    subject = await db["subjectDetails"].find_one({"_id": ObjectId(data["subject_id"]), "is_deleted": {"$ne": True}})
    if not subject:
        raise HTTPException(status_code=404, detail="Subject not found")

    # the subject must be in the same institute as the (resolved) faculty
    if str(subject.get("institute_id")) != str(faculty["institute_id"]):
        raise HTTPException(status_code=403, detail="Subject does not belong to this institute")

    if identity.get("role") == FACULTY and str(subject.get("faculty_id")) != str(faculty["_id"]):
        raise HTTPException(status_code=403, detail="Subject not assigned to this faculty")

    try:
        material_doc = create_faculty_material_document({
            "title": data.get("title"),
            "description": data.get("description"),
            "type": data.get("type"),
            "subject_id": str(subject["_id"]),
            "faculty_id": str(faculty["_id"]),
            "institute_id": str(faculty["institute_id"]),
            "school_id": str(subject.get("school_id")) if subject.get("school_id") else None,
            "programme_id": str(subject.get("programme_id")) if subject.get("programme_id") else None,
            "department_id": str(subject.get("department_id")) if subject.get("department_id") else None,
            "batch_id": str(subject.get("batch_id")) if subject.get("batch_id") else None,
            "semester": subject.get("semester"),
            "file_url": data.get("file_url"),
            "file_id": data.get("file_id"),
            "filename": data.get("filename"),
            "mime_type": data.get("mime_type"),
            "size": data.get("size"),
            "due_date": data.get("due_date"),
            "total_marks": data.get("total_marks"),
        })
    except Exception:
        logging.exception("Create faculty material error")
        raise HTTPException(status_code=500, detail="Failed to publish material")

    result = await db["facultyMaterials"].insert_one(material_doc)

    return JSONResponse(status_code=201, content={
        "success": True,
        "message": "Material published successfully",
        "material_id": str(result.inserted_id),
    })


@router.get("/faculty/materials")
async def get_faculty_materials(
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if identity.get("role") not in (INSTITUTE, FACULTY):
        raise HTTPException(status_code=403, detail="Unauthorized")

    query: Dict[str, Any] = {"is_deleted": {"$ne": True}}

    if identity.get("role") == FACULTY:
        faculty = await db["facultyDetails"].find_one({
            "user_id": ObjectId(identity["user_id"]), "is_deleted": {"$ne": True},
        })
        if not faculty:
            raise HTTPException(status_code=404, detail="Faculty profile not found")
        query["faculty_id"] = faculty["_id"]

    materials = [
        serialize_faculty_material(doc)
        async for doc in db["facultyMaterials"].find(query).sort("created_at", -1)
    ]

    return {"materials": materials}


@router.get("/student/materials")
async def get_student_materials(
    subject_id: Optional[str] = Query(None),
    material_type: Optional[str] = Query(None, alias="type"),
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
        "student_id": student["_id"],
        "user_id": ObjectId(identity["user_id"]),
        "is_deleted": {"$ne": True},
    }
    if subject_id and ObjectId.is_valid(subject_id):
        relation_query["subject_id"] = ObjectId(subject_id)

    enrolled_subject_ids = [
        relation["subject_id"]
        async for relation in db["StudentSubjectRelationModel"].find(relation_query)
        if relation.get("subject_id")
    ]

    if not enrolled_subject_ids:
        return {"materials": []}

    material_query: Dict[str, Any] = {
        "subject_id": {"$in": enrolled_subject_ids},
        "is_published": True,
        "is_deleted": {"$ne": True},
    }
    if material_type:
        material_query["type"] = material_type

    materials = [
        serialize_faculty_material(doc)
        async for doc in db["facultyMaterials"].find(material_query).sort("created_at", -1)
    ]

    return {"materials": materials}


@router.post("/student/materials/{material_id}/interaction")
async def create_student_material_interaction(
    material_id: str,
    payload: StudentMaterialInteractionCreate,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    data = payload.model_dump()
    if identity.get("role") != INSTITUTE_STUDENT:
        raise HTTPException(status_code=403, detail="Only institute students allowed")

    if not ObjectId.is_valid(material_id):
        raise HTTPException(status_code=400, detail="Invalid material ID")

    student = await db["studentDetails"].find_one({
        "user_id": ObjectId(identity["user_id"]), "role": INSTITUTE_STUDENT, "is_deleted": {"$ne": True},
    })
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    material = await db["facultyMaterials"].find_one({
        "_id": ObjectId(material_id), "is_published": True, "is_deleted": {"$ne": True},
    })
    if not material:
        raise HTTPException(status_code=404, detail="Material not found")

    relation = await db["StudentSubjectRelationModel"].find_one({
        "student_id": student["_id"],
        "user_id": ObjectId(identity["user_id"]),
        "subject_id": material["subject_id"],
        "is_deleted": {"$ne": True},
    })
    if not relation:
        raise HTTPException(status_code=403, detail="You are not enrolled in this subject")

    status = payload.status

    existing = await db["studentMaterialInteractions"].find_one({
        "material_id": ObjectId(material_id), "student_id": student["_id"], "is_deleted": {"$ne": True},
    })

    now = datetime.now(timezone.utc)
    update_doc: Dict[str, Any] = {"status": status, "updated_at": now}
    if status == "viewed":
        update_doc["viewed_at"] = now
    if status == "completed":
        update_doc["completed_at"] = now
    if status == "submitted":
        update_doc["submitted_at"] = now
        update_doc["submission"] = {
            "text": data.get("submission_text"),
            "file_url": data.get("submission_file_url"),
            "fileId": data.get("submission_file_id"),
            "filename": data.get("submission_filename"),
        }

    if existing:
        await db["studentMaterialInteractions"].update_one({"_id": existing["_id"]}, {"$set": update_doc})
        return {
            "success": True,
            "message": "Interaction updated successfully",
            "interaction_id": str(existing["_id"]),
        }

    interaction_doc = create_student_material_interaction_document({
        "material_id": material_id,
        "student_id": str(student["_id"]),
        "student_user_id": identity["user_id"],
        "subject_id": str(material["subject_id"]),
        "status": status,
        "submission_text": data.get("submission_text"),
        "submission_file_url": data.get("submission_file_url"),
        "submission_file_id": data.get("submission_file_id"),
        "submission_filename": data.get("submission_filename"),
    })

    result = await db["studentMaterialInteractions"].insert_one(interaction_doc)

    return JSONResponse(status_code=201, content={
        "success": True,
        "message": "Interaction saved successfully",
        "interaction_id": str(result.inserted_id),
    })
