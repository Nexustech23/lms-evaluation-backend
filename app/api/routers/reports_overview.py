# ============================================================
# REPORTS ROUTER
# The Institute Admin's Reports dashboard and its detail reports. Read-only; an admin sees only their own institute.
# ============================================================

from typing import Optional

from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity, get_current_user_and_institute
from app.db.mongodb import get_database
from app.models.user import INSTITUTE
from app.services.reports_data_check import build_data_check
from app.services.reports_overview import build_overview
from app.services.reports_result_analysis import build_result_analysis, build_workbook

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["reports"])


async def _admin(identity: dict, db: AsyncIOMotorDatabase):
    """The calling Institute Admin's user and institute; anyone else is refused."""
    if identity.get("role") != INSTITUTE:
        raise HTTPException(status_code=403, detail="Only institute admins allowed")
    user, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)
    return user, institute_id


async def _admin_institute(identity: dict, db: AsyncIOMotorDatabase) -> ObjectId:
    return (await _admin(identity, db))[1]


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


@router.get("/reports/result-analysis")
async def reports_result_analysis(
    programme: Optional[str] = Query(None),
    batch: Optional[str] = Query(None),
    semester: Optional[int] = Query(None, ge=1, le=20),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    return await build_result_analysis(db, await _admin_institute(identity, db), programme, batch, semester)


@router.get("/reports/result-analysis/export")
async def reports_result_analysis_export(
    programme: Optional[str] = Query(None),
    batch: Optional[str] = Query(None),
    semester: Optional[int] = Query(None, ge=1, le=20),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, institute_id = await _admin(identity, db)
    report = await build_result_analysis(db, institute_id, programme, batch, semester)
    content = build_workbook(report, user.get("fullName") or "-", user.get("fullName") or "-")
    name = f"result-analysis-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.xlsx"
    return Response(content=content, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{name}"'})
