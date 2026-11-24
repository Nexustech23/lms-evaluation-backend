# ============================================================
# REPORTS OVERVIEW ROUTER
# The numbers for the Institute Admin's Reports dashboard. Read-only; an admin sees only their own institute.
# ============================================================

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity, get_current_user_and_institute
from app.db.mongodb import get_database
from app.models.user import INSTITUTE
from app.services.reports_overview import build_overview

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["reports"])


@router.get("/reports/overview")
async def reports_overview(
    programme: Optional[str] = Query(None),
    batch: Optional[str] = Query(None),
    semester: Optional[int] = Query(None, ge=1, le=20),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if identity.get("role") != INSTITUTE:
        raise HTTPException(status_code=403, detail="Only institute admins allowed")
    _, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)
    return await build_overview(db, institute_id, programme, batch, semester)
