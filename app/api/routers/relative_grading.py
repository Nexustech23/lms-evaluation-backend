# ============================================================
# RELATIVE GRADING ROUTER
# Ported from controllers/institute/relative_grading_controller.py
# ============================================================

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity, get_current_user_and_institute
from app.db.mongodb import get_database
from app.models.relative_grading import (
    build_relative_grading_update_fields,
    create_relative_grading_document,
    serialize_relative_grading,
    validate_percentage_total,
)
from app.schemas.relative_grading import RelativeGradingRequest

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["relative-grading"])


@router.post("/relative-grading")
async def create_or_update_relative_grading(
    payload: RelativeGradingRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    data = payload.model_dump()
    validation_error = validate_percentage_total(data)
    if validation_error:
        raise HTTPException(status_code=400, detail=validation_error)

    data = {**data, "university_id": str(institute_id)}

    existing = await db["relativeGradings"].find_one({"university_id": institute_id})

    if existing:
        update_fields = build_relative_grading_update_fields(data)
        await db["relativeGradings"].update_one({"_id": existing["_id"]}, {"$set": update_fields})
        return {
            "success": True,
            "message": "Relative grading configuration updated successfully",
            "id": str(existing["_id"]),
        }

    grading_doc = create_relative_grading_document(data)
    result = await db["relativeGradings"].insert_one(grading_doc)

    return {
        "success": True,
        "message": "Relative grading configuration created successfully",
        "id": str(result.inserted_id),
    }


@router.get("/relative-grading/{university_id}")
async def get_relative_grading(university_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(university_id):
        raise HTTPException(status_code=400, detail="Invalid university_id")

    doc = await db["relativeGradings"].find_one({"university_id": ObjectId(university_id)})
    if not doc:
        return {"success": True, "data": None}

    return {"success": True, "data": serialize_relative_grading(doc)}


@router.put("/relative-grading/{grading_id}")
async def update_relative_grading(
    grading_id: str, payload: RelativeGradingRequest, db: AsyncIOMotorDatabase = Depends(get_database)
):
    if not ObjectId.is_valid(grading_id):
        raise HTTPException(status_code=400, detail="Invalid grading_id")

    data = payload.model_dump()
    validation_error = validate_percentage_total(data)
    if validation_error:
        raise HTTPException(status_code=400, detail=validation_error)

    update_fields = build_relative_grading_update_fields(data)
    result = await db["relativeGradings"].update_one({"_id": ObjectId(grading_id)}, {"$set": update_fields})

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Relative grading configuration not found")

    return {"success": True, "message": "Relative grading configuration updated successfully"}
