# ============================================================
# STUDENT DASHBOARD ROUTER
# The real values for an institute student's own dashboard. Read-only; a student sees only their own data.
# ============================================================

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import INSTITUTE_STUDENT, get_current_identity
from app.db.mongodb import get_database
from app.services.student_dashboard import build_dashboard

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["student-dashboard"])


@router.get("/student/dashboard")
async def student_dashboard(
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if identity.get("role") != INSTITUTE_STUDENT:
        raise HTTPException(status_code=403, detail="Only institute students allowed")
    return await build_dashboard(db, identity["user_id"])
