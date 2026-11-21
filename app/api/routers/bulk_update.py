# ============================================================
# BULK UPDATE ROUTER
# Update existing students from an Excel or CSV file: preview first, then (a later phase) apply.
# Institute Admin only, and only that institute's own students.
# ============================================================

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity, get_current_user_and_institute
from app.core.rate_limit import bulk_update_rate_limit
from app.db.mongodb import get_database
from app.services.bulk_update import BulkUpdateError
from app.schemas.bulk_update import BulkUpdateApplyRequest
from app.services.bulk_update_apply import apply_batch
from app.services.bulk_update_preview import create_preview, get_batch
from app.services.bulk_update_undo import list_history, undo_batch
from app.utils.uploads import read_upload_capped

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["bulk-update"])


async def _admin_institute(identity: dict, db: AsyncIOMotorDatabase):
    user, institute_id, error = await get_current_user_and_institute(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)
    return user, institute_id


@router.post("/bulk-update/students/preview", dependencies=[Depends(bulk_update_rate_limit)])
async def preview_students(
    file: UploadFile = File(...),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Check the file and show what would change. Nothing is changed."""
    user, institute_id = await _admin_institute(identity, db)
    data = await read_upload_capped(file)
    try:
        return await create_preview(db, institute_id, user["_id"], data, file.filename)
    except BulkUpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/bulk-update/history")
async def bulk_update_history(
    limit: int = Query(50, ge=1, le=100),
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """The college's bulk updates, newest first, with whether each can still be undone."""
    _, institute_id = await _admin_institute(identity, db)
    return await list_history(db, institute_id, limit)


@router.get("/bulk-update/{batch_id}")
async def get_bulk_update(
    batch_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    _, institute_id = await _admin_institute(identity, db)
    return await get_batch(db, institute_id, batch_id)


@router.post("/bulk-update/{batch_id}/apply", dependencies=[Depends(bulk_update_rate_limit)])
async def apply_bulk_update(
    batch_id: str,
    payload: BulkUpdateApplyRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Apply a preview, all students together or none. Needs a reason."""
    user, institute_id = await _admin_institute(identity, db)
    return await apply_batch(db, institute_id, user["_id"], batch_id, payload.reason, payload.skip_rejected)


@router.post("/bulk-update/{batch_id}/undo", dependencies=[Depends(bulk_update_rate_limit)])
async def undo_bulk_update(
    batch_id: str,
    payload: BulkUpdateApplyRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Put the old values back, all students together or none. Needs a reason, and works for a limited time."""
    user, institute_id = await _admin_institute(identity, db)
    return await undo_batch(db, institute_id, user["_id"], batch_id, payload.reason)
