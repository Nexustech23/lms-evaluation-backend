# ============================================================
# MARKS IMPORT ROUTER
# Ported from routes/import_marks_routes.py + controllers/import_marks_controller.py
# ============================================================

from datetime import datetime, timezone
from io import BytesIO

from bson import ObjectId
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity, resolve_current_institute_id
from app.db.mongodb import get_database
from app.utils.excel_import_helper import read_marks_excel
from app.utils.uploads import read_upload_capped

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["marks-import"])


@router.post("/import-marks-excel")
async def import_marks_excel(
    file: UploadFile = File(...),
    batch_id: str = Form(...),
    semester: int = Form(...),
    relative_grading_id: str = Form(None),  # accepted for compatibility, unused — matches Flask
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    institute_id = await resolve_current_institute_id(identity, db)

    if not ObjectId.is_valid(batch_id):
        raise HTTPException(status_code=400, detail="Valid batch_id is required")

    file_bytes = await read_upload_capped(file)

    try:
        students = read_marks_excel(BytesIO(file_bytes))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to import marks Excel")

    batch_object_id = ObjectId(batch_id)
    semester_int = semester
    now = datetime.now(timezone.utc)

    documents = [
        {
            "institute_id": institute_id,
            "batch_id": batch_object_id,
            "semester": semester_int,
            "student_name": s["student_name"],
            "marks": s["marks"],
            "uploaded_at": now,
        }
        for s in students
    ]

    await db["importedMarks"].delete_many(
        {"institute_id": institute_id, "batch_id": batch_object_id, "semester": semester_int}
    )
    await db["importedMarks"].insert_many(documents)

    return {
        "success": True,
        "message": f"{len(documents)} students processed successfully.",
        "processed_count": len(documents),
    }
