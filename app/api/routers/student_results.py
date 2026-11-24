# ============================================================
# STUDENT RESULTS ROUTER
# The logged-in institute student's own results. Read-only; the route takes no student id, so a student can only
# ever see their own.
# ============================================================

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import INSTITUTE_STUDENT, get_current_identity
from app.db.mongodb import get_database
from app.services.student_results import build_results

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["student-results"])


@router.get("/student/results")
async def student_results(
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    if identity.get("role") != INSTITUTE_STUDENT:
        raise HTTPException(status_code=403, detail="Only institute students allowed")
    return await build_results(db, identity["user_id"])
