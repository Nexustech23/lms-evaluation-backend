# ============================================================
# REPORTS ROUTER
# The Institute Admin's Reports dashboard and its detail reports. Read-only; an admin sees only their own institute.
# ============================================================

from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity, get_current_user_and_institute
from app.db.mongodb import get_database
from app.models.user import INSTITUTE
from app.services.reports_data_check import build_data_check
from app.services.reports_overview import build_overview

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["reports"])


async def _admin_institute(identity: dict, db: AsyncIOMotorDatabase) -> ObjectId:
    """The calling Institute Admin's institute; anyone else is refused."""
    if identity.get("role") != INSTITUTE:
        raise HTTPException(status_code=403, detail="Only institute admins allowed")
    _, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)
    return institute_id


@router.get("/reports/overview")
async def reports_overview(
    programme: Optional[str] = Query(None),
    batch: Optional[str] = Query(None),
    semester: Optional[int] = Query(None, ge=1, le=20),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    return await build_overview(db, await _admin_institute(identity, db), programme, batch, semester)


@router.get("/reports/data-check")
async def reports_data_check(
    programme: Optional[str] = Query(None),
    batch: Optional[str] = Query(None),
    semester: Optional[int] = Query(None, ge=1, le=20),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    return await build_data_check(db, await _admin_institute(identity, db), programme, batch, semester)
