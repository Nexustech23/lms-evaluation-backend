# ============================================================
# Ported from models/Transcriptmodel.py. No schema-enforcing class in
# Flask either — Mongo documents for `academic_transcripts` are built as
# raw dicts by app/utils/transcript_excel_helper.py (Excel import) and
# app/utils/transcript_generation_helper.py (live exam-data generation).
# ============================================================

from typing import Any, Dict, List, Optional
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

TRANSCRIPT_COLLECTION = "academic_transcripts"
IMPORT_COLLECTION = "transcriptImports"


async def get_semesters(
    db: AsyncIOMotorDatabase, student_id: str, institute_id: ObjectId, batch_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    query: Dict[str, Any] = {"student_id": str(student_id).strip(), "institute_id": ObjectId(institute_id)}
    if batch_id:
        query["batch_id"] = ObjectId(batch_id)
    return [doc async for doc in db[TRANSCRIPT_COLLECTION].find(query, {"_id": 0}).sort("semester_no", 1)]


async def get_imports(db: AsyncIOMotorDatabase, institute_id: ObjectId, limit: int = 20) -> List[Dict[str, Any]]:
    return [
        doc async for doc in
        db[IMPORT_COLLECTION].find({"institute_id": ObjectId(institute_id)}, {"_id": 0})
        .sort("imported_at", -1).limit(limit)
    ]


def serialize_semester(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "semester": record.get("semester_no"),
        "termLabel": record.get("term_label"),
        "subjects": record.get("subjects", []),
        "overallTotal": record.get("overall_total", 0),
        "totalCredits": record.get("total_credits", 0),
        "totalCreditPoints": record.get("total_credit_points", 0),
        "tgpa": record.get("tgpa", 0),
        "cgpa": record.get("cgpa", 0),
    }
