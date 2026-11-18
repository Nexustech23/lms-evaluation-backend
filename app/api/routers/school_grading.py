# ============================================================
# SCHOOL GRADING ROUTER
# A school's grade bands (CBSE nine-point scale by default) and pass mark, editable by the institute admin.
# A college keeps /relative-grading; these routes refuse a college.
# ============================================================

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity, get_current_user_and_institute
from app.db.mongodb import get_database
from app.schemas.school_grading import SchoolGradingRequest
from app.utils.school_grading import config_from_doc, default_config, validate_config

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["school-grading"])

COLLECTION = "schoolGradings"


async def _school_institute(identity: dict, db: AsyncIOMotorDatabase):
    user, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)
    institute = await db["instituteDetails"].find_one({"_id": institute_id}, {"institute_type": 1})
    if not institute or institute.get("institute_type") != "school":
        raise HTTPException(status_code=400, detail="School grading applies to school institutes only")
    return institute_id


def _response(config: dict, is_default: bool) -> dict:
    return {"success": True, "data": {**config, "is_default": is_default}, "default": default_config()}


@router.get("/school-grading")
async def get_school_grading(identity: dict = Depends(get_current_identity), db: AsyncIOMotorDatabase = Depends(get_database)):
    institute_id = await _school_institute(identity, db)
    doc = await db[COLLECTION].find_one({"institute_id": institute_id})
    return _response(config_from_doc(doc), is_default=doc is None)


@router.put("/school-grading")
async def save_school_grading(
    payload: SchoolGradingRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    institute_id = await _school_institute(identity, db)
    config, error = validate_config(payload.model_dump())
    if error:
        raise HTTPException(status_code=400, detail=error)

    now = datetime.now(timezone.utc)
    await db[COLLECTION].update_one(
        {"institute_id": institute_id},
        {"$set": {**config, "updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    return {**_response(config, is_default=False), "message": "Grading saved"}


@router.delete("/school-grading")
async def reset_school_grading(identity: dict = Depends(get_current_identity), db: AsyncIOMotorDatabase = Depends(get_database)):
    """Back to the CBSE default: the saved setup is removed, and results use the default again."""
    institute_id = await _school_institute(identity, db)
    await db[COLLECTION].delete_one({"institute_id": institute_id})
    return {**_response(default_config(), is_default=True), "message": "Grading reset to the CBSE default"}
