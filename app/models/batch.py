from datetime import datetime, timezone
from typing import Any, Dict, List
from bson import ObjectId


def validate_batch_data(data: Dict[str, Any]):
    if not data:
        raise ValueError("No data provided")

    institute_id = data.get("institute_id")
    school_id = data.get("school_id")
    programme_id = data.get("programme_id")
    department_id = data.get("department_id")
    semesters = data.get("semesters")

    if not institute_id or not ObjectId.is_valid(institute_id):
        raise ValueError("Valid institute_id is required")
    if not school_id or not ObjectId.is_valid(school_id):
        raise ValueError("Valid school_id is required")
    if not programme_id or not ObjectId.is_valid(programme_id):
        raise ValueError("Valid programme_id is required")
    if department_id and not ObjectId.is_valid(department_id):
        raise ValueError("Invalid department_id")
    if not isinstance(semesters, list) or len(semesters) == 0:
        raise ValueError("At least one semester is required")

    return (
        ObjectId(institute_id),
        ObjectId(school_id),
        ObjectId(programme_id),
        ObjectId(department_id) if department_id else None,
    )


def create_batch_document(data: Dict[str, Any]) -> Dict[str, Any]:
    institute_id, school_id, programme_id, department_id = validate_batch_data(data)

    semester_numbers: List[int] = []
    for semester in data.get("semesters", []):
        semester_number = semester.get("semester_number")
        if semester_number is None:
            raise ValueError("semester_number is required")
        semester_numbers.append(int(semester_number))

    now = datetime.now(timezone.utc)

    return {
        "institute_id": institute_id,
        "school_id": school_id,
        "programme_id": programme_id,
        "department_id": department_id,

        "batch_name": data.get("batch_name"),
        "total_semesters": int(data.get("total_semesters", 0)),

        "semesters": semester_numbers,

        "is_active": True,

        "created_at": now,
        "updated_at": now,
    }


def update_batch_document(data: Dict[str, Any]) -> Dict[str, Any]:
    update_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}

    allowed_fields = ["batch_name", "total_semesters", "semesters", "is_active", "department_id"]

    for field in allowed_fields:
        if field in data:
            if field == "total_semesters":
                update_fields[field] = int(data[field])
            elif field == "department_id":
                update_fields[field] = (
                    ObjectId(data[field]) if data[field] and ObjectId.is_valid(data[field]) else None
                )
            else:
                update_fields[field] = data[field]

    return {"$set": update_fields}


async def delete_batch_cascade(db, batch_id: str) -> Dict[str, Any]:
    """Delete a batch and everything hanging off it (exams, answer sheets, subjects)."""
    if not batch_id or not ObjectId.is_valid(batch_id):
        raise ValueError("Valid batch_id is required")

    batch_oid = ObjectId(batch_id)

    exam_ids = await db["newsavedDocs"].distinct("_id", {"batch_id": batch_oid})

    answer_deleted = 0
    if exam_ids:
        answer_result = await db["answerDetails"].delete_many({"exam_id": {"$in": exam_ids}})
        answer_deleted = answer_result.deleted_count

    exam_result = await db["newsavedDocs"].delete_many({"batch_id": batch_oid})
    subject_result = await db["subjectDetails"].delete_many({"batch_id": batch_oid})
    batch_result = await db["batchDetails"].delete_one({"_id": batch_oid})

    if batch_result.deleted_count == 0:
        raise ValueError(f"Batch '{batch_id}' not found")

    return {
        "batch_id": batch_id,
        "deleted": {
            "answer_sheets": answer_deleted,
            "exams": exam_result.deleted_count,
            "subjects": subject_result.deleted_count,
            "batch": batch_result.deleted_count,
        },
    }


def serialize_batch(doc: Dict[str, Any]) -> Dict[str, Any] | None:
    if not doc:
        return None

    return {
        "id": str(doc.get("_id")),
        "institute_id": str(doc.get("institute_id")),
        "school_id": str(doc.get("school_id")),
        "programme_id": str(doc.get("programme_id")),
        "department_id": str(doc.get("department_id")) if doc.get("department_id") else None,

        "batch_name": doc.get("batch_name"),
        "total_semesters": doc.get("total_semesters"),

        "semesters": doc.get("semesters", []),

        "is_active": doc.get("is_active", True),
        "created_at": doc.get("created_at"),
    }
