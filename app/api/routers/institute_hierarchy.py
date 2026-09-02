# ============================================================
# INSTITUTE HIERARCHY ROUTER
# school -> programme / department -> batch -> subject
# Ported from routes/institute/school_programme_routes.py +
# controllers/institute/{school,programme,department,batch,subject}_controller.py
#
# Scope: create / list / get / update / delete only. Subject *results*,
# combined-result export/print, and relative grading are out of Phase 1 —
# they depend on evaluation/answer collections that don't exist yet.
# ============================================================

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import (
    FACULTY,
    INSTITUTE,
    SUPERADMIN,
    get_current_identity,
    get_current_user_and_faculty_details,
    get_current_user_and_institute,
    require_entity_in_institute,
    require_role,
    validate_entity_ownership,
)
from app.core.cache import bust_institute_hierarchy, cached_get
from app.db.mongodb import get_database
from app.models.batch import create_batch_document, delete_batch_cascade, serialize_batch
from app.models.department import create_department_document, serialize_department, update_department_document
from app.models.programme import (
    create_programme_document,
    serialize_programme,
    update_programme_document,
    update_programme_po_targets,
)
from app.models.school import create_school_document, serialize_school, update_school_document
from app.models.subject import create_subject_document, serialize_subject
from app.schemas.institute_hierarchy import (
    CreateBatchRequest,
    CreateDepartmentRequest,
    CreateProgrammeRequest,
    CreateSchoolRequest,
    CreateSubjectRequest,
    UpdateBatchRequest,
    UpdateDepartmentRequest,
    UpdateProgrammePoRequest,
    UpdateProgrammeRequest,
    UpdateSchoolRequest,
    UpdateSubjectRequest,
)
from app.utils.batch import load_by_ids
from app.utils.query import search_regex

# Router-level gate: this whole surface is institute-admin / faculty only.
# Previously it was just Depends(get_current_identity) (any authenticated user
# of any of the 7 roles), which — combined with several ID-addressed routes
# below that did no per-object tenant check — let a student or a public
# self-learner signup delete or read another institute's academic data.
# SUPERADMIN is kept in the allow-list so platform-level tooling/tests still
# reach these endpoints; the per-route ownership checks below are what
# actually enforce tenant isolation.
router = APIRouter(
    dependencies=[Depends(require_role(SUPERADMIN, INSTITUTE, FACULTY))],
    tags=["institute-hierarchy"],
)


async def _resolve_institute_or_faculty(identity: dict, db: AsyncIOMotorDatabase):
    """Mirrors the Flask pattern: try institute-admin first, fall back to faculty."""
    user, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
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

        return faculty_user, institute_id

    return user, institute_id


# =====================================================
# DASHBOARD
# =====================================================

@router.get("/dashboard/institute")
async def dashboard_details(
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    institute_obj_id = ObjectId(institute_id)

    # Independent reads — run them concurrently instead of serially.
    schools_count, programmes_count, departments_count, faculty_count, institute = await asyncio.gather(
        db["schoolDetails"].count_documents({"institute_id": institute_obj_id}),
        db["programmeDetails"].count_documents({"institute_id": institute_obj_id}),
        db["departmentDetails"].count_documents({"institute_id": institute_obj_id}),
        db["facultyDetails"].count_documents({"institute_id": institute_obj_id}),
        db["instituteDetails"].find_one({"_id": institute_obj_id}),
    )

    return {
        "success": True,
        "institute": {
            "full_name": user.get("fullName"),
            "banner_url": institute.get("banner_url") if institute else None,
            "logo_url": institute.get("logo_url") if institute else None,
        },
        "counts": {
            "schools": schools_count,
            "programmes": programmes_count,
            "departments": departments_count,
            "faculty": faculty_count,
        },
    }


# =====================================================
# SCHOOL
# =====================================================

@router.post("/schools")
async def create_school(
    payload: CreateSchoolRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    data = payload.model_dump()
    data["institute_id"] = str(institute_id)

    try:
        school_doc = create_school_document(data, str(user["_id"]))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db["schoolDetails"].insert_one(school_doc)
    created = await db["schoolDetails"].find_one({"_id": result.inserted_id})

    await bust_institute_hierarchy(str(institute_id))
    return {"success": True, "school": serialize_school(created)}


@router.get("/schools")
async def get_schools(
    page: int = Query(1),
    limit: int = Query(10),
    search: str = Query(""),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    _, institute_id = await _resolve_institute_or_faculty(identity, db)

    skip = (page - 1) * limit if limit > 0 else 0
    query: Dict[str, Any] = {"institute_id": ObjectId(institute_id)}

    regex = search_regex(search)
    if regex:
        query["$or"] = [{"school_name": regex}, {"school_code": regex}, {"description": regex}]

    # Display-only dropdown call (no pagination, no search) — cache it.
    if limit == 0 and not search:
        async def _load():
            rows = [s async for s in db["schoolDetails"].find(query).sort("created_at", -1)]
            return {
                "success": True, "page": page, "limit": limit, "total": len(rows), "total_pages": 1,
                "filters": {"search": search},
                "schools": [{"id": str(s["_id"]), "school_name": s.get("school_name")} for s in rows],
            }
        return await cached_get("schools_dropdown", institute_id, _load)

    total = await db["schoolDetails"].count_documents(query)
    cursor = db["schoolDetails"].find(query).sort("created_at", -1)
    if limit > 0:
        cursor = cursor.skip(skip).limit(limit)
    schools = [s async for s in cursor]

    if limit == 0:
        return {
            "success": True, "page": page, "limit": limit, "total": total, "total_pages": 1,
            "filters": {"search": search},
            "schools": [{"id": str(s["_id"]), "school_name": s.get("school_name")} for s in schools],
        }

    return {
        "success": True, "page": page, "limit": limit, "total": total,
        "total_pages": ((total + limit - 1) // limit) if limit > 0 else 1,
        "filters": {"search": search},
        "schools": [serialize_school(s) for s in schools],
    }


@router.put("/schools/{school_id}")
async def update_school(
    school_id: str,
    payload: UpdateSchoolRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(school_id):
        raise HTTPException(status_code=400, detail="Invalid school_id")

    _, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)
    await require_entity_in_institute(db["schoolDetails"], school_id, institute_id)

    try:
        update_payload = update_school_document(payload.model_dump(exclude_unset=True), identity["user_id"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db["schoolDetails"].update_one(
        {"_id": ObjectId(school_id), "institute_id": ObjectId(institute_id)}, update_payload
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="School not found")

    await bust_institute_hierarchy(str(institute_id))
    return {"success": True, "message": "School updated successfully"}


async def _school_delete_summary(db: AsyncIOMotorDatabase, school_id: str):
    school_obj_id = ObjectId(school_id)
    school_str_id = str(school_obj_id)

    programmes = [p async for p in db["programmeDetails"].find(
        {"$or": [{"school_id": school_obj_id}, {"school_id": school_str_id}]}
    )]
    programme_ids = [p["_id"] for p in programmes]

    departments = [d async for d in db["departmentDetails"].find({"programme_id": {"$in": programme_ids}})]
    department_ids = [d["_id"] for d in departments]

    batches = [b async for b in db["batchDetails"].find({
        "$or": [{"programme_id": {"$in": programme_ids}}, {"department_id": {"$in": department_ids}}]
    })]
    batch_ids = [b["_id"] for b in batches]

    faculty_docs = [f async for f in db["facultyDetails"].find(
        {"$or": [{"school_id": school_obj_id}, {"school_id": school_str_id}]}
    )]
    faculty_ids = [f["_id"] for f in faculty_docs]
    user_ids = [f.get("user_id") for f in faculty_docs if f.get("user_id")]

    question_papers = [qp async for qp in db["questionPaperDetails"].find(
        {"$or": [{"school_id": school_obj_id}, {"school_id": school_str_id}]}
    )]
    exam_ids = [qp["_id"] for qp in question_papers]
    exam_ids_str = [str(e) for e in exam_ids]

    subjects_count = await db["subjectDetails"].count_documents({
        "$or": [
            {"school_id": school_obj_id}, {"school_id": school_str_id},
            {"programme_id": {"$in": programme_ids}}, {"department_id": {"$in": department_ids}},
            {"batch_id": {"$in": batch_ids}},
        ]
    })

    summary = {
        "programmes": len(programme_ids),
        "departments": len(department_ids),
        "batches": len(batch_ids),
        "subjects": subjects_count,
        "faculty": len(faculty_ids),
        "users": len(user_ids),
        "question_papers": len(exam_ids),
        "answers": await db["answerDetails"].count_documents({"exam_id": {"$in": exam_ids_str}}),
        "evaluations": await db["evaluationDetails"].count_documents({"exam_id": {"$in": exam_ids_str}}),
    }

    return summary, programme_ids, department_ids, batch_ids, faculty_ids, user_ids


@router.delete("/schools/{school_id}")
async def delete_school(
    school_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(school_id):
        raise HTTPException(status_code=400, detail="Invalid school_id")

    _, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)
    await require_entity_in_institute(db["schoolDetails"], school_id, institute_id)
    institute_oid = ObjectId(institute_id)

    summary, programme_ids, department_ids, batch_ids, faculty_ids, user_ids = await _school_delete_summary(db, school_id)

    # Every downstream filter below is keyed by _id lists derived from THIS
    # school's subtree (or the school_id itself), so it can only ever touch
    # this institute's data — but the school-level delete is still pinned to
    # institute_oid as a belt-and-suspenders tenant guard.
    await db["subjectDetails"].delete_many({
        "$or": [
            {"school_id": ObjectId(school_id)}, {"school_id": school_id},
            {"programme_id": {"$in": programme_ids}}, {"department_id": {"$in": department_ids}},
            {"batch_id": {"$in": batch_ids}},
        ]
    })
    if batch_ids:
        await db["batchDetails"].delete_many({"_id": {"$in": batch_ids}})
    if department_ids:
        await db["departmentDetails"].delete_many({"_id": {"$in": department_ids}})
    if programme_ids:
        await db["programmeDetails"].delete_many({"_id": {"$in": programme_ids}})
    if faculty_ids:
        await db["facultyDetails"].delete_many({"_id": {"$in": faculty_ids}})
    # Faculty LOGIN accounts are deactivated, never hard-deleted from a
    # cascade: a mistaken or malicious school delete stays reversible, audit
    # history is preserved, and rows other collections still reference don't
    # dangle.
    if user_ids:
        await db["users"].update_many(
            {"_id": {"$in": user_ids}},
            {"$set": {"is_active": False, "is_deleted": True, "updated_at": datetime.now(timezone.utc)}},
        )

    result = await db["schoolDetails"].delete_one({"_id": ObjectId(school_id), "institute_id": institute_oid})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="School not found")

    await bust_institute_hierarchy(str(institute_id))
    return {"success": True, "message": "School deleted successfully", "deleted": summary}


@router.get("/schools/{school_id}/delete-summary")
async def get_delete_summary(
    school_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(school_id):
        raise HTTPException(status_code=400, detail="Invalid school_id")

    _, institute_id = await _resolve_institute_or_faculty(identity, db)
    await require_entity_in_institute(db["schoolDetails"], school_id, institute_id)

    summary, *_ = await _school_delete_summary(db, school_id)
    return summary


# =====================================================
# PROGRAMME
# =====================================================

@router.post("/programmes")
async def create_programme(
    payload: CreateProgrammeRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    school, err, code = await validate_entity_ownership(db["schoolDetails"], payload.school_id, institute_id)
    if err:
        raise HTTPException(status_code=code, detail=err["error"])

    data = payload.model_dump()
    data["institute_id"] = str(institute_id)

    try:
        programme_doc = create_programme_document(data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db["programmeDetails"].insert_one(programme_doc)
    created = await db["programmeDetails"].find_one({"_id": result.inserted_id})

    await bust_institute_hierarchy(str(institute_id))
    return {"success": True, "programme": serialize_programme(created)}


@router.get("/programme/{programme_id}")
async def get_programme(
    programme_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(programme_id):
        raise HTTPException(status_code=400, detail="Invalid programme_id")

    _, institute_id = await _resolve_institute_or_faculty(identity, db)
    programme = await require_entity_in_institute(db["programmeDetails"], programme_id, institute_id)

    return {"success": True, "programme": serialize_programme(programme)}


@router.get("/programmes_po_target/{subject_id}")
async def get_programme_po(
    subject_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(subject_id):
        raise HTTPException(status_code=400, detail="Invalid subject_id")

    _, institute_id = await _resolve_institute_or_faculty(identity, db)
    subject = await require_entity_in_institute(db["subjectDetails"], subject_id, institute_id)

    programme_id = subject.get("programme_id")
    if not programme_id:
        raise HTTPException(status_code=404, detail="Programme ID not found in subject")
    if isinstance(programme_id, str):
        programme_id = ObjectId(programme_id)

    programme = await db["programmeDetails"].find_one(
        {"_id": programme_id, "institute_id": ObjectId(institute_id)}
    )
    if not programme:
        raise HTTPException(status_code=404, detail="Programme not found")

    return {
        "success": True,
        "programme": serialize_programme(programme),
        "po": programme.get("po", []),
        "targets": programme.get("targets", []),
        "coAttainmentTarget": programme.get("coAttainmentTarget", []),
    }


@router.get("/programmes/{school_id}")
async def get_programmes(
    school_id: str,
    page: int = Query(1),
    limit: int = Query(10),
    search: str = Query(""),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(school_id):
        raise HTTPException(status_code=400, detail="Invalid school_id")

    _, institute_id = await _resolve_institute_or_faculty(identity, db)
    await require_entity_in_institute(db["schoolDetails"], school_id, institute_id)

    skip = (page - 1) * limit if limit > 0 else 0
    query: Dict[str, Any] = {"school_id": ObjectId(school_id), "institute_id": ObjectId(institute_id)}

    regex = search_regex(search)
    if regex:
        query["$or"] = [{"programme_name": regex}, {"programme_code": regex}, {"description": regex}]

    if limit == 0 and not search:
        async def _load():
            rows = [p async for p in db["programmeDetails"].find(query).sort("created_at", -1)]
            return {
                "success": True, "page": page, "limit": limit, "total": len(rows), "total_pages": 1,
                "filters": {"search": search},
                "programmes": [{"id": str(p["_id"]), "programme_name": p.get("programme_name")} for p in rows],
            }
        return await cached_get("programmes_dropdown", institute_id, _load, sub_id=school_id)

    total = await db["programmeDetails"].count_documents(query)
    cursor = db["programmeDetails"].find(query).sort("created_at", -1)
    if limit > 0:
        cursor = cursor.skip(skip).limit(limit)
    programmes = [p async for p in cursor]

    if limit == 0:
        return {
            "success": True, "page": page, "limit": limit, "total": total, "total_pages": 1,
            "filters": {"search": search},
            "programmes": [{"id": str(p["_id"]), "programme_name": p.get("programme_name")} for p in programmes],
        }

    return {
        "success": True, "page": page, "limit": limit, "total": total,
        "total_pages": ((total + limit - 1) // limit) if limit > 0 else 1,
        "filters": {"search": search},
        "programmes": [serialize_programme(p) for p in programmes],
    }


@router.put("/programmes/po")
async def update_programme_po(
    payload: UpdateProgrammePoRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    programme_id = payload.programme_id
    if not ObjectId.is_valid(programme_id):
        raise HTTPException(status_code=400, detail="Valid programme_id is required")

    _, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)
    await require_entity_in_institute(db["programmeDetails"], programme_id, institute_id)

    try:
        update_payload = update_programme_po_targets(payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db["programmeDetails"].update_one(
        {"_id": ObjectId(programme_id), "institute_id": ObjectId(institute_id)}, update_payload
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Programme not found")

    await bust_institute_hierarchy(str(institute_id))
    return {"success": True, "message": "Programme PO/targets updated successfully"}


@router.put("/programmes/{programme_id}")
async def update_programme(
    programme_id: str,
    payload: UpdateProgrammeRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(programme_id):
        raise HTTPException(status_code=400, detail="Invalid programme_id")

    _, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)
    await require_entity_in_institute(db["programmeDetails"], programme_id, institute_id)

    try:
        update_payload = update_programme_document(payload.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db["programmeDetails"].update_one(
        {"_id": ObjectId(programme_id), "institute_id": ObjectId(institute_id)}, update_payload
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Programme not found")

    await bust_institute_hierarchy(str(institute_id))
    return {"success": True, "message": "Programme updated successfully"}


@router.delete("/programmes/{programme_id}")
async def delete_programme(
    programme_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(programme_id):
        raise HTTPException(status_code=400, detail="Invalid programme_id")

    _, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)
    await require_entity_in_institute(db["programmeDetails"], programme_id, institute_id)

    programme_obj_id = ObjectId(programme_id)

    departments = [d async for d in db["departmentDetails"].find({"programme_id": programme_obj_id})]
    department_ids = [d["_id"] for d in departments]

    batches = [b async for b in db["batchDetails"].find({
        "$or": [{"programme_id": programme_obj_id}, {"department_id": {"$in": department_ids}}]
    })]
    batch_ids = [b["_id"] for b in batches]

    await db["subjectDetails"].delete_many({
        "$or": [
            {"programme_id": programme_obj_id},
            {"department_id": {"$in": department_ids}},
            {"batch_id": {"$in": batch_ids}},
        ]
    })
    await db["batchDetails"].delete_many({"_id": {"$in": batch_ids}})
    await db["departmentDetails"].delete_many({"_id": {"$in": department_ids}})

    result = await db["programmeDetails"].delete_one(
        {"_id": programme_obj_id, "institute_id": ObjectId(institute_id)}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Programme not found")

    await bust_institute_hierarchy(str(institute_id))
    return {"success": True, "message": "Programme and related data deleted"}


# =====================================================
# DEPARTMENT
# =====================================================

@router.post("/departments")
async def create_department(
    payload: CreateDepartmentRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    programme, err, code = await validate_entity_ownership(db["programmeDetails"], payload.programme_id, institute_id)
    if err:
        raise HTTPException(status_code=code, detail=err["error"])

    data = payload.model_dump()
    data["institute_id"] = str(institute_id)
    data["school_id"] = str(programme["school_id"])

    try:
        department_doc = create_department_document(data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db["departmentDetails"].insert_one(department_doc)
    created = await db["departmentDetails"].find_one({"_id": result.inserted_id})

    await bust_institute_hierarchy(str(institute_id))
    return {"success": True, "department": serialize_department(created)}


@router.get("/departments/{programme_id}")
async def get_departments(
    programme_id: str,
    page: int = Query(1),
    limit: int = Query(10),
    search: str = Query(""),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(programme_id):
        raise HTTPException(status_code=400, detail="Invalid programme_id")

    _, institute_id = await _resolve_institute_or_faculty(identity, db)
    await require_entity_in_institute(db["programmeDetails"], programme_id, institute_id)

    skip = (page - 1) * limit if limit > 0 else 0
    query: Dict[str, Any] = {"programme_id": ObjectId(programme_id), "institute_id": ObjectId(institute_id)}

    regex = search_regex(search)
    if regex:
        query["$or"] = [{"department_name": regex}, {"code": regex}]

    if limit == 0 and not search:
        async def _load():
            rows = [d async for d in db["departmentDetails"].find(query).sort("created_at", -1)]
            return {
                "success": True, "page": page, "limit": limit, "total": len(rows), "total_pages": 1,
                "filters": {"search": search},
                "departments": [{"id": str(d["_id"]), "department_name": d.get("department_name")} for d in rows],
            }
        return await cached_get("departments_dropdown", institute_id, _load, sub_id=programme_id)

    total = await db["departmentDetails"].count_documents(query)
    cursor = db["departmentDetails"].find(query).sort("created_at", -1)
    if limit > 0:
        cursor = cursor.skip(skip).limit(limit)
    departments = [d async for d in cursor]

    if limit == 0:
        return {
            "success": True, "page": page, "limit": limit, "total": total, "total_pages": 1,
            "filters": {"search": search},
            "departments": [{"id": str(d["_id"]), "department_name": d.get("department_name")} for d in departments],
        }

    return {
        "success": True, "page": page, "limit": limit, "total": total,
        "total_pages": ((total + limit - 1) // limit) if limit > 0 else 1,
        "filters": {"search": search},
        "departments": [serialize_department(d) for d in departments],
    }


@router.put("/departments/{department_id}")
async def update_department(
    department_id: str,
    payload: UpdateDepartmentRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(department_id):
        raise HTTPException(status_code=400, detail="Invalid department_id")

    _, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)
    await require_entity_in_institute(db["departmentDetails"], department_id, institute_id)

    try:
        update_payload = update_department_document(payload.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db["departmentDetails"].update_one(
        {"_id": ObjectId(department_id), "institute_id": ObjectId(institute_id)}, update_payload
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    await bust_institute_hierarchy(str(institute_id))
    return {"success": True, "message": "Department updated successfully"}


@router.delete("/departments/{department_id}")
async def delete_department(
    department_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(department_id):
        raise HTTPException(status_code=400, detail="Invalid department_id")

    _, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)
    await require_entity_in_institute(db["departmentDetails"], department_id, institute_id)

    dept_obj_id = ObjectId(department_id)

    batches = [b async for b in db["batchDetails"].find({"department_id": dept_obj_id})]
    batch_ids = [b["_id"] for b in batches]

    await db["subjectDetails"].delete_many({"$or": [{"department_id": dept_obj_id}, {"batch_id": {"$in": batch_ids}}]})
    await db["batchDetails"].delete_many({"_id": {"$in": batch_ids}})

    result = await db["departmentDetails"].delete_one(
        {"_id": dept_obj_id, "institute_id": ObjectId(institute_id)}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    await bust_institute_hierarchy(str(institute_id))
    return {"success": True, "message": "Department and related data deleted"}


# =====================================================
# BATCH
# =====================================================

@router.post("/batches")
async def create_batch(
    payload: CreateBatchRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    data = payload.model_dump()
    programme_id = data.get("programme_id")
    department_id = data.get("department_id")

    if programme_id:
        programme, err, code = await validate_entity_ownership(db["programmeDetails"], programme_id, institute_id)
        if err:
            raise HTTPException(status_code=code, detail=err["error"])
        school_id = str(programme["school_id"])
    elif department_id:
        if not ObjectId.is_valid(department_id):
            raise HTTPException(status_code=400, detail="Invalid department_id")
        department = await db["departmentDetails"].find_one(
            {"_id": ObjectId(department_id), "institute_id": institute_id}
        )
        if not department:
            raise HTTPException(status_code=404, detail="Department not found or unauthorized")
        programme_id = str(department["programme_id"])
        school_id = str(department["school_id"])
        data["programme_id"] = programme_id
    else:
        raise HTTPException(status_code=400, detail="Either programme_id or department_id is required")

    data["institute_id"] = str(institute_id)
    data["school_id"] = school_id
    if department_id:
        data["department_id"] = department_id

    try:
        batch_doc = create_batch_document(data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db["batchDetails"].insert_one(batch_doc)
    batch_id = result.inserted_id

    for semester in data.get("semesters", []):
        semester_number = semester.get("semester_number")
        for subject in semester.get("subjects", []):
            subject_payload = {
                "institute_id": str(institute_id),
                "programme_id": programme_id,
                "school_id": school_id,
                "faculty_id": subject.get("faculty_id"),
                "batch_id": str(batch_id),
                "subject_name": subject.get("subject_name"),
                "subject_code": subject.get("subject_code"),
                "semester": semester_number,
                "credits": subject.get("credits", 0),
            }
            if department_id:
                subject_payload["department_id"] = department_id

            subject_doc = create_subject_document(subject_payload, str(user["_id"]))
            await db["subjectDetails"].insert_one(subject_doc)

    await bust_institute_hierarchy(str(institute_id))
    return {"success": True, "batch_id": str(batch_id)}


@router.put("/batches/{batch_id}")
async def update_batch(
    batch_id: str,
    payload: UpdateBatchRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    data = payload.model_dump(exclude_unset=True)
    user, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    if not ObjectId.is_valid(batch_id):
        raise HTTPException(status_code=400, detail="Invalid batch_id")

    batch = await db["batchDetails"].find_one({"_id": ObjectId(batch_id), "institute_id": institute_id})
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found or unauthorized")

    now = datetime.now(timezone.utc)
    update_fields: Dict[str, Any] = {"updated_at": now}
    if "batch_name" in data:
        update_fields["batch_name"] = data["batch_name"]
    if "total_semesters" in data:
        update_fields["total_semesters"] = int(data["total_semesters"])

    await db["batchDetails"].update_one({"_id": ObjectId(batch_id)}, {"$set": update_fields})

    for subject_id in data.get("subjectsToBeDeleted", []):
        if ObjectId.is_valid(subject_id):
            await db["subjectDetails"].update_one(
                {"_id": ObjectId(subject_id), "institute_id": institute_id},
                {"$set": {"is_deleted": True, "is_active": False, "updated_by": ObjectId(user["_id"]), "updated_at": now}},
            )

    for semester in data.get("semesters", []):
        semester_number = semester.get("semester_number")
        for subject in semester.get("subjects", []):
            subject_id = subject.get("id")

            if subject_id and ObjectId.is_valid(subject_id):
                await db["subjectDetails"].update_one(
                    {"_id": ObjectId(subject_id), "institute_id": institute_id},
                    {"$set": {
                        "subject_name": subject.get("subject_name"),
                        "subject_code": subject.get("subject_code"),
                        "credits": int(subject.get("credits", 0)),
                        "semester": semester_number,
                        "faculty_id": (
                            ObjectId(subject["faculty_id"])
                            if subject.get("faculty_id") and ObjectId.is_valid(subject["faculty_id"])
                            else None
                        ),
                        "updated_by": ObjectId(user["_id"]),
                        "updated_at": now,
                    }},
                )
            else:
                subject_payload = {
                    "institute_id": str(institute_id),
                    "school_id": str(batch["school_id"]),
                    "programme_id": str(batch["programme_id"]),
                    "batch_id": str(batch_id),
                    "subject_name": subject.get("subject_name"),
                    "subject_code": subject.get("subject_code"),
                    "faculty_id": subject.get("faculty_id"),
                    "semester": semester_number,
                    "credits": subject.get("credits", 0),
                }
                if batch.get("department_id"):
                    subject_payload["department_id"] = str(batch["department_id"])

                subject_doc = create_subject_document(subject_payload, str(user["_id"]))
                await db["subjectDetails"].insert_one(subject_doc)

    await bust_institute_hierarchy(str(institute_id))
    return {"success": True, "message": "Batch and subjects updated successfully"}


@router.get("/batches")
async def get_batches(
    department_id: str | None = Query(None),
    programme_id: str | None = Query(None),
    page: int = Query(1),
    limit: int = Query(10),
    search: str = Query(""),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    _, institute_id = await _resolve_institute_or_faculty(identity, db)

    skip = (page - 1) * limit if limit > 0 else 0
    query: Dict[str, Any] = {"institute_id": institute_id, "is_active": True}

    if department_id:
        if not ObjectId.is_valid(department_id):
            raise HTTPException(status_code=400, detail="Invalid department_id")
        department = await db["departmentDetails"].find_one({"_id": ObjectId(department_id), "institute_id": institute_id})
        if not department:
            raise HTTPException(status_code=404, detail="Department not found or unauthorized")
        query["department_id"] = ObjectId(department_id)
    elif programme_id:
        if not ObjectId.is_valid(programme_id):
            raise HTTPException(status_code=400, detail="Invalid programme_id")
        programme = await db["programmeDetails"].find_one({"_id": ObjectId(programme_id), "institute_id": institute_id})
        if not programme:
            raise HTTPException(status_code=404, detail="Programme not found or unauthorized")
        query["programme_id"] = ObjectId(programme_id)
    else:
        raise HTTPException(status_code=400, detail="Either department_id or programme_id is required")

    regex = search_regex(search)
    if regex:
        query["$or"] = [{"batch_name": regex}]

    if limit == 0 and not search:
        async def _load():
            rows = [b async for b in db["batchDetails"].find(query).sort("created_at", -1)]
            return {
                "success": True, "page": page, "limit": limit, "total": len(rows), "total_pages": 1,
                "filters": {"search": search, "department_id": department_id, "programme_id": programme_id},
                "batches": [
                    {"id": str(b["_id"]), "batch_name": b.get("batch_name"), "semesters": b.get("semesters")}
                    for b in rows
                ],
            }
        return await cached_get(
            "batches_dropdown", institute_id, _load, sub_id=f"{department_id or ''}:{programme_id or ''}"
        )

    total = await db["batchDetails"].count_documents(query)
    cursor = db["batchDetails"].find(query).sort("created_at", -1)
    if limit > 0:
        cursor = cursor.skip(skip).limit(limit)
    batches = [b async for b in cursor]

    if limit == 0:
        return {
            "success": True, "page": page, "limit": limit, "total": total, "total_pages": 1,
            "filters": {"search": search, "department_id": department_id, "programme_id": programme_id},
            "batches": [{"id": str(b["_id"]), "batch_name": b.get("batch_name"), "semesters": b.get("semesters")} for b in batches],
        }

    return {
        "success": True, "page": page, "limit": limit, "total": total,
        "total_pages": ((total + limit - 1) // limit) if limit > 0 else 1,
        "filters": {"search": search, "department_id": department_id, "programme_id": programme_id},
        "batches": [serialize_batch(b) for b in batches],
    }


@router.get("/batches/{batch_id}")
async def get_batch(
    batch_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    if not ObjectId.is_valid(batch_id):
        raise HTTPException(status_code=400, detail="Invalid batch_id")

    batch = await db["batchDetails"].find_one({"_id": ObjectId(batch_id), "institute_id": institute_id, "is_active": True})
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found or unauthorized")

    subjects = [serialize_subject(s) async for s in db["subjectDetails"].find(
        {"batch_id": ObjectId(batch_id), "is_deleted": False}
    )]

    semesters_map: Dict[Any, list] = {}
    for subject in subjects:
        semesters_map.setdefault(subject.get("semester"), []).append(subject)

    semesters_with_subjects = [
        {"semester_number": sem, "subjects": semesters_map[sem]} for sem in sorted(semesters_map.keys())
    ]

    return {
        "success": True,
        "batch": {**serialize_batch(batch), "semesters_with_subjects": semesters_with_subjects},
    }


@router.delete("/batches/{batch_id}")
async def delete_batch(
    batch_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(batch_id):
        raise HTTPException(status_code=400, detail="Invalid batch_id")

    _, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)
    await require_entity_in_institute(db["batchDetails"], batch_id, institute_id)

    try:
        result = await delete_batch_cascade(db, batch_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    await bust_institute_hierarchy(str(institute_id))
    return {"success": True, "message": "Batch and all related data deleted successfully", "deleted": result["deleted"]}


# =====================================================
# SUBJECT (basic CRUD only — results/exports are a later phase)
# =====================================================

@router.post("/subjects")
async def create_subject(
    payload: CreateSubjectRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    data = payload.model_dump()
    data["institute_id"] = str(institute_id)

    try:
        subject_doc = create_subject_document(data, str(user["_id"]))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = await db["subjectDetails"].insert_one(subject_doc)

    await bust_institute_hierarchy(str(institute_id))
    return {"success": True, "subject_id": str(result.inserted_id)}


def _semester_query_value(semester: str):
    return int(semester) if str(semester).isdigit() else semester


# NOTE: /faculty/filter-data, /subjects/faculty and /subjects/institute must be
# declared before /subjects/{programme_id} below — otherwise FastAPI would match
# e.g. GET /subjects/faculty as programme_id="faculty" on the param route.

@router.get("/faculty/filter-data")
async def get_faculty_filter_data(
    school_id: str | None = Query(None),
    programme_id: str | None = Query(None),
    department_id: str | None = Query(None),
    batch_id: str | None = Query(None),
    semester: str | None = Query(None),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, faculty_id, error = await get_current_user_and_faculty_details(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    subjects = [s async for s in db["subjectDetails"].find({"faculty_id": faculty_id, "is_deleted": False})]

    # Preload every referenced hierarchy row in 4 $in queries (Phase 4) — the
    # map-building loops below then do dict lookups instead of a find_one per
    # subject. Progressive filtering of `subjects` still controls which rows
    # land in each map; preloading a superset is harmless.
    _schools_by_id = await load_by_ids(
        db, "schoolDetails", (s.get("school_id") for s in subjects),
        {"_id": 1, "school_name": 1, "school_code": 1, "co": 1},
    )
    _programmes_by_id = await load_by_ids(
        db, "programmeDetails", (s.get("programme_id") for s in subjects),
        {"_id": 1, "programme_name": 1, "programme_code": 1},
    )
    _departments_by_id = await load_by_ids(
        db, "departmentDetails", (s.get("department_id") for s in subjects),
        {"_id": 1, "department_name": 1, "code": 1},
    )
    _batches_by_id = await load_by_ids(
        db, "batchDetails", (s.get("batch_id") for s in subjects), {"_id": 1, "batch_name": 1},
    )

    school_map: Dict[str, Any] = {}
    for s in subjects:
        sid = s.get("school_id")
        if not sid or str(sid) in school_map:
            continue
        school = _schools_by_id.get(sid)
        if school:
            school_map[str(sid)] = {
                "id": str(school["_id"]),
                "school_name": school.get("school_name"),
                "school_code": school.get("school_code"),
                "co": school.get("co"),
            }

    if school_id and ObjectId.is_valid(school_id):
        oid = ObjectId(school_id)
        subjects = [s for s in subjects if s.get("school_id") == oid]

    programme_map: Dict[str, Any] = {}
    for s in subjects:
        pid = s.get("programme_id")
        if not pid or str(pid) in programme_map:
            continue
        programme = _programmes_by_id.get(pid)
        if programme:
            programme_map[str(pid)] = {
                "id": str(programme["_id"]),
                "programme_name": programme.get("programme_name"),
                "programme_code": programme.get("programme_code"),
            }

    if programme_id and ObjectId.is_valid(programme_id):
        oid = ObjectId(programme_id)
        subjects = [s for s in subjects if s.get("programme_id") == oid]

    department_map: Dict[str, Any] = {}
    for s in subjects:
        did = s.get("department_id")
        if not did or str(did) in department_map:
            continue
        department = _departments_by_id.get(did)
        if department:
            department_map[str(did)] = {
                "id": str(department["_id"]),
                "department_name": department.get("department_name"),
                "department_code": department.get("code"),
            }

    has_departments = len(department_map) > 0

    if department_id and ObjectId.is_valid(department_id):
        oid = ObjectId(department_id)
        subjects = [s for s in subjects if s.get("department_id") == oid]

    batch_map: Dict[str, Any] = {}
    for s in subjects:
        bid = s.get("batch_id")
        if not bid or str(bid) in batch_map:
            continue
        batch = _batches_by_id.get(bid)
        if batch:
            batch_map[str(bid)] = {"id": str(batch["_id"]), "batch_name": batch.get("batch_name")}

    if batch_id and ObjectId.is_valid(batch_id):
        oid = ObjectId(batch_id)
        subjects = [s for s in subjects if s.get("batch_id") == oid]

    semester_list = sorted({str(s.get("semester")) for s in subjects if s.get("semester")})

    if semester:
        subjects = [s for s in subjects if str(s.get("semester")) == str(semester)]

    subject_list = [
        {"id": str(s["_id"]), "subject_name": s.get("subject_name"), "subject_code": s.get("subject_code"), "co": s.get("co")}
        for s in subjects
    ]

    return {
        "success": True,
        "filters": {
            "schools": list(school_map.values()),
            "programmes": list(programme_map.values()),
            "departments": list(department_map.values()),
            "has_departments": has_departments,
            "batches": list(batch_map.values()),
            "semesters": semester_list,
            "subjects": subject_list,
        },
    }


@router.get("/subjects/faculty")
async def get_subjects_by_faculty(
    page: int = Query(1),
    limit: int = Query(10),
    search: str = Query(""),
    school_id: str | None = Query(None),
    programme_id: str | None = Query(None),
    department_id: str | None = Query(None),
    batch_id: str | None = Query(None),
    semester: str | None = Query(None),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, faculty_id, error = await get_current_user_and_faculty_details(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    skip = (page - 1) * limit if limit > 0 else 0
    query: Dict[str, Any] = {"faculty_id": faculty_id, "is_deleted": False}

    if school_id and ObjectId.is_valid(school_id):
        query["school_id"] = ObjectId(school_id)
    if programme_id and ObjectId.is_valid(programme_id):
        query["programme_id"] = ObjectId(programme_id)
    if department_id and ObjectId.is_valid(department_id):
        query["department_id"] = ObjectId(department_id)
    if batch_id and ObjectId.is_valid(batch_id):
        query["batch_id"] = ObjectId(batch_id)
    if semester:
        query["semester"] = _semester_query_value(semester)

    regex = search_regex(search)
    if regex:
        # NOTE: mirrors Flask — a regex filter on "semester" is a no-op when
        # semester is stored as an int, but ported as-is for parity.
        query["$or"] = [{"subject_name": regex}, {"subject_code": regex}, {"semester": regex}]

    total = await db["subjectDetails"].count_documents(query)
    cursor = db["subjectDetails"].find(query).sort("created_at", -1)
    if limit > 0:
        cursor = cursor.skip(skip).limit(limit)
    subjects = [s async for s in cursor]

    filters = {
        "search": search, "school_id": school_id, "programme_id": programme_id,
        "department_id": department_id, "batch_id": batch_id, "semester": semester,
    }

    if limit == 0:
        return {
            "success": True, "page": page, "limit": limit, "total": total, "total_pages": 1,
            "filters": filters,
            "subjects": [{"id": str(s["_id"]), "subject_name": s.get("subject_name")} for s in subjects],
        }

    # Batched FK enrichment (Phase 4) — one $in per collection instead of
    # four find_one per subject. Same lookups, same output.
    schools_by_id = await load_by_ids(
        db, "schoolDetails", (s["school_id"] for s in subjects), {"_id": 1, "school_name": 1, "institute_id": 1}
    )
    depts_by_id = await load_by_ids(
        db, "departmentDetails", (s.get("department_id") for s in subjects), {"_id": 1, "code": 1}
    )
    batches_by_id = await load_by_ids(
        db, "batchDetails", (s.get("batch_id") for s in subjects), {"_id": 1, "batch_name": 1}
    )
    progs_by_id = await load_by_ids(
        db, "programmeDetails", (s.get("programme_id") for s in subjects), {"_id": 1, "programme_code": 1}
    )

    populated = []
    for subject in subjects:
        school = schools_by_id.get(subject["school_id"])
        department = depts_by_id.get(subject.get("department_id"))
        batch = batches_by_id.get(subject.get("batch_id"))
        programme = progs_by_id.get(subject.get("programme_id"))

        populated.append({
            "_id": str(subject["_id"]),
            "subject_name": subject.get("subject_name"),
            "subject_code": subject.get("subject_code"),
            "semester": subject.get("semester"),
            "credits": subject.get("credits"),
            "cos": subject.get("co"),
            "school_name": school.get("school_name") if school else None,
            "school_id": str(school["_id"]) if school else None,
            "institute_id": str(school["institute_id"]) if school and school.get("institute_id") else None,
            # NOTE: mirrors a Flask bug — this faculty-scoped endpoint returns
            # department.code / programme.programme_code mislabeled as *_name.
            # The /subjects/institute endpoint below returns the real name
            # fields. Ported as-is (see plan's "known Flask bugs" list).
            "department_name": department.get("code") if department else None,
            "department_id": str(department["_id"]) if department else None,
            "batch_name": batch.get("batch_name") if batch else None,
            "batch_id": str(batch["_id"]) if batch else None,
            "programme_name": programme.get("programme_code") if programme else None,
            "programme_id": str(programme["_id"]) if programme else None,
            "created_at": subject.get("created_at"),
        })

    return {
        "success": True, "page": page, "limit": limit, "total": total,
        "total_pages": ((total + limit - 1) // limit) if limit > 0 else 1,
        "filters": filters,
        "subjects": populated,
    }


@router.get("/subjects/institute")
async def get_subjects_by_institute(
    page: int = Query(1),
    limit: int = Query(10),
    search: str = Query(""),
    school_id: str | None = Query(None),
    programme_id: str | None = Query(None),
    department_id: str | None = Query(None),
    batch_id: str | None = Query(None),
    semester: str | None = Query(None),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    skip = (page - 1) * limit if limit > 0 else 0
    query: Dict[str, Any] = {"institute_id": ObjectId(institute_id), "is_deleted": False}

    if school_id and ObjectId.is_valid(school_id):
        query["school_id"] = ObjectId(school_id)
    if programme_id and ObjectId.is_valid(programme_id):
        query["programme_id"] = ObjectId(programme_id)
    if department_id and ObjectId.is_valid(department_id):
        query["department_id"] = ObjectId(department_id)
    if batch_id and ObjectId.is_valid(batch_id):
        query["batch_id"] = ObjectId(batch_id)
    if semester:
        query["semester"] = _semester_query_value(semester)

    regex = search_regex(search)
    if regex:
        query["$or"] = [{"subject_name": regex}, {"subject_code": regex}, {"semester": regex}]

    total = await db["subjectDetails"].count_documents(query)
    cursor = db["subjectDetails"].find(query).sort("created_at", -1)
    if limit > 0:
        cursor = cursor.skip(skip).limit(limit)
    subjects = [s async for s in cursor]

    filters = {
        "search": search, "school_id": school_id, "programme_id": programme_id,
        "department_id": department_id, "batch_id": batch_id, "semester": semester,
    }

    if limit == 0 and not search:
        async def _load():
            rows = [s async for s in db["subjectDetails"].find(query).sort("created_at", -1)]
            return {
                "success": True, "page": page, "limit": limit, "total": len(rows), "total_pages": 1,
                "filters": filters,
                "subjects": [{"id": str(s["_id"]), "subject_name": s.get("subject_name")} for s in rows],
            }
        return await cached_get(
            "subjects_dropdown", institute_id, _load,
            sub_id=f"{school_id or ''}:{programme_id or ''}:{department_id or ''}:{batch_id or ''}:{semester or ''}",
        )

    if limit == 0:
        return {
            "success": True, "page": page, "limit": limit, "total": total, "total_pages": 1,
            "filters": filters,
            "subjects": [{"id": str(s["_id"]), "subject_name": s.get("subject_name")} for s in subjects],
        }

    # Batched FK enrichment (Phase 4) — one $in per collection instead of
    # four find_one per subject. Same lookups, same output.
    schools_by_id = await load_by_ids(
        db, "schoolDetails", (s["school_id"] for s in subjects), {"_id": 1, "school_name": 1, "institute_id": 1}
    )
    depts_by_id = await load_by_ids(
        db, "departmentDetails", (s.get("department_id") for s in subjects), {"_id": 1, "department_name": 1}
    )
    batches_by_id = await load_by_ids(
        db, "batchDetails", (s.get("batch_id") for s in subjects), {"_id": 1, "batch_name": 1}
    )
    progs_by_id = await load_by_ids(
        db, "programmeDetails", (s.get("programme_id") for s in subjects), {"_id": 1, "programme_name": 1}
    )

    populated = []
    for subject in subjects:
        school = schools_by_id.get(subject["school_id"])
        department = depts_by_id.get(subject.get("department_id"))
        batch = batches_by_id.get(subject.get("batch_id"))
        programme = progs_by_id.get(subject.get("programme_id"))

        populated.append({
            "_id": str(subject["_id"]),
            "subject_name": subject.get("subject_name"),
            "subject_code": subject.get("subject_code"),
            "semester": subject.get("semester"),
            "credits": subject.get("credits"),
            "cos": subject.get("co"),
            "school_name": school.get("school_name") if school else None,
            "school_id": str(school["_id"]) if school else None,
            "institute_id": str(school["institute_id"]) if school and school.get("institute_id") else None,
            "department_name": department.get("department_name") if department else None,
            "department_id": str(department["_id"]) if department else None,
            "batch_name": batch.get("batch_name") if batch else None,
            "batch_id": str(batch["_id"]) if batch else None,
            "programme_name": programme.get("programme_name") if programme else None,
            "programme_id": str(programme["_id"]) if programme else None,
            "created_at": subject.get("created_at"),
        })

    return {
        "success": True, "page": page, "limit": limit, "total": total,
        "total_pages": ((total + limit - 1) // limit) if limit > 0 else 1,
        "filters": filters,
        "subjects": populated,
    }


@router.get("/subject/{subject_id}")
async def get_subject_by_id(
    subject_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(subject_id):
        raise HTTPException(status_code=400, detail="Invalid subject_id")

    _, institute_id = await _resolve_institute_or_faculty(identity, db)
    subject = await db["subjectDetails"].find_one(
        {"_id": ObjectId(subject_id), "institute_id": ObjectId(institute_id), "is_deleted": False}
    )
    if not subject:
        raise HTTPException(status_code=404, detail="Subject not found")

    return {"success": True, "subject": serialize_subject(subject)}


@router.get("/subjects/{programme_id}")
async def get_subjects(
    programme_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(programme_id):
        raise HTTPException(status_code=400, detail="Invalid programme_id")

    _, institute_id = await _resolve_institute_or_faculty(identity, db)
    await require_entity_in_institute(db["programmeDetails"], programme_id, institute_id)

    subjects = [
        serialize_subject(doc)
        async for doc in db["subjectDetails"].find(
            {"programme_id": ObjectId(programme_id), "institute_id": ObjectId(institute_id), "is_deleted": False}
        )
    ]

    return {"success": True, "subjects": subjects}


@router.put("/subjects/{subject_id}")
async def update_subject(
    subject_id: str,
    payload: UpdateSubjectRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    data = payload.model_dump(exclude_unset=True)
    if not ObjectId.is_valid(subject_id):
        raise HTTPException(status_code=400, detail="Invalid subject_id")

    _, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    update_fields: Dict[str, Any] = {}
    for field in ["subject_name", "subject_code", "teaching_periods", "credits"]:
        if field in data:
            update_fields[field] = data[field]
    if "co_list" in data:
        update_fields["co"] = data["co_list"]
    if "co_po_matrix" in data:
        update_fields["co_po_matrix"] = data["co_po_matrix"]

    update_fields["updated_at"] = datetime.now(timezone.utc)
    update_fields["updated_by"] = identity["user_id"]

    result = await db["subjectDetails"].update_one(
        {"_id": ObjectId(subject_id), "institute_id": ObjectId(institute_id), "is_deleted": False},
        {"$set": update_fields},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Subject not found")

    await bust_institute_hierarchy(str(institute_id))
    return {"success": True, "message": "Subject updated successfully"}


@router.delete("/subjects/{subject_id}")
async def delete_subject(
    subject_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if not ObjectId.is_valid(subject_id):
        raise HTTPException(status_code=400, detail="Invalid subject_id")

    _, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    result = await db["subjectDetails"].update_one(
        {"_id": ObjectId(subject_id), "institute_id": ObjectId(institute_id), "is_deleted": False},
        {"$set": {
            "is_deleted": True,
            "is_active": False,
            "updated_by": ObjectId(identity["user_id"]),
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Subject not found")

    await bust_institute_hierarchy(str(institute_id))
    return {"success": True, "message": "Subject deleted successfully"}
