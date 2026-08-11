# ============================================================
# EVALUATION RUBRIC ROUTER
# Ported from routes/institute/evaluation_routes.py +
# controllers/institute/evaluation_controller.py
# ============================================================

from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity, get_current_user_and_faculty_details
from app.db.mongodb import get_database
from app.models.evaluation import validate_evaluation_details
from app.schemas.evaluation import SaveEvaluationDetailsRequest

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["evaluation"])


@router.get("/evaluation-details/{folder_id}")
async def get_evaluation_details(folder_id: str, db: AsyncIOMotorDatabase = Depends(get_database)):
    if not ObjectId.is_valid(folder_id):
        raise HTTPException(status_code=400, detail="Invalid exam_id")

    evaluation = await db["evaluationDetails"].find_one({"exam_id": ObjectId(folder_id)})
    if not evaluation:
        return {"success": True, "message": "No evaluation details found", "evaluation": None}

    evaluation["_id"] = str(evaluation["_id"])
    evaluation["faculty_id"] = str(evaluation["faculty_id"])
    evaluation["exam_id"] = str(evaluation["exam_id"])

    return {"success": True, "evaluation": evaluation}


@router.post("/evaluation-details/{folder_id}")
async def save_evaluation_details(
    folder_id: str,
    payload: SaveEvaluationDetailsRequest,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    user, faculty_id, error = await get_current_user_and_faculty_details(identity, db)
    if error:
        message, code = error
        raise HTTPException(status_code=code, detail=message)

    if not ObjectId.is_valid(faculty_id):
        raise HTTPException(status_code=400, detail="Invalid faculty_id")

    faculty_object_id = ObjectId(faculty_id)

    if not folder_id or not ObjectId.is_valid(folder_id):
        raise HTTPException(status_code=400, detail="Valid exam_id is required")

    exam_object_id = ObjectId(folder_id)
    total_marks = payload.totalMarks

    try:
        validated_details = validate_evaluation_details(payload.questionEvaluationDetails)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e) or "questionEvaluationDetails is invalid")

    now = datetime.now(timezone.utc)
    existing_eval = await db["evaluationDetails"].find_one(
        {"exam_id": exam_object_id, "faculty_id": faculty_object_id}
    )

    if existing_eval:
        await db["evaluationDetails"].update_one(
            {"exam_id": exam_object_id, "faculty_id": faculty_object_id},
            {"$set": {"questionEvaluationDetails": validated_details, "totalMarks": total_marks, "updated_at": now}},
        )
    else:
        await db["evaluationDetails"].insert_one({
            "faculty_id": faculty_object_id,
            "exam_id": exam_object_id,
            "totalMarks": total_marks,
            "questionEvaluationDetails": validated_details,
            "created_at": now,
            "updated_at": now,
        })

    return {
        "success": True,
        "message": "Evaluation details saved successfully",
        "totalMarks": total_marks,
        "questionEvaluationDetails": validated_details,
    }
