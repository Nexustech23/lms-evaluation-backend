# ============================================================
# EVALUATION RUBRIC ROUTER
# Ported from routes/institute/evaluation_routes.py +
# controllers/institute/evaluation_controller.py
# ============================================================

from datetime import datetime, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.deps import get_current_identity
from app.api.answer_access import require_exam_access
from app.db.mongodb import get_database
from app.models.evaluation import validate_evaluation_details, _validate_total_marks
from app.models.exam_validation import covered_co_list
from app.schemas.evaluation import SaveEvaluationDetailsRequest

router = APIRouter(dependencies=[Depends(get_current_identity)], tags=["evaluation"])


@router.get("/evaluation-details/{folder_id}")
async def get_evaluation_details(
    folder_id: str,
    identity: dict = Depends(get_current_identity),
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    exam = await require_exam_access(identity, db, folder_id, allow_institute_read=True)
    evaluation = await db["evaluationDetails"].find_one({"exam_id": exam["_id"]})
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
    exam = await require_exam_access(identity, db, folder_id)
    faculty_object_id = ObjectId(str(exam["faculty_id"]))
    exam_object_id = exam["_id"]
    total_marks = payload.totalMarks

    try:
        validated_details = validate_evaluation_details(payload.questionEvaluationDetails)
        total_marks = _validate_total_marks(total_marks, validated_details)
        covered_cos = set(covered_co_list(exam.get("covered_cos")))
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e) or "questionEvaluationDetails is invalid")

    used_cos = {co["co_code"] for q in validated_details for co in q["cos"]}
    unknown_cos = sorted(used_cos - covered_cos)
    if unknown_cos:
        raise HTTPException(
            status_code=400,
            detail=(
                f"These COs are used in the rubric but not declared in the exam's covered COs: "
                f"{', '.join(unknown_cos)}. Edit the exam's covered COs first, or remove them from the rubric."
            ),
        )

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
