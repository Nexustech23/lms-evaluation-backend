from datetime import datetime, timezone
from typing import Any, Dict
from bson import ObjectId

# Field shape confirmed against create_question_paper_controller /
# update_question_paper_controller / serialize_question_paper in Flask.
# models/QuestionPaperModel.py in the original is dead code (never imported
# by the controller) and is intentionally not used as a reference here.

_OPTIONAL_ID_FIELDS = {
    "schoolId": "school_id",
    "programmeId": "programme_id",
    "departmentId": "department_id",
    "batchId": "batch_id",
}
_OPTIONAL_NAME_FIELDS = {
    "schoolName": "school_name",
    "programmeName": "programme_name",
    "batchName": "batch_name",
}
_HEADER_FIELDS = {
    "instituteName": "institute_name",
    "departmentName": "department_name",
    "examType": "exam_type",
    "academicYear": "academic_year",
    "semester": "semester",
    "totalMarks": "total_marks",
    "duration": "duration",
    "folderName": "folder_name",
}


def build_create_document(data: Dict[str, Any], faculty_id: ObjectId, created_by: ObjectId) -> Dict[str, Any]:
    subject_id = data.get("subjectId")
    if not subject_id or not ObjectId.is_valid(subject_id):
        raise ValueError("Valid subjectId is required")

    doc: Dict[str, Any] = {
        "subject_id": ObjectId(subject_id),
        "faculty_id": faculty_id,
        "editor_content": data.get("editorContent") or "",
        "subject_name": data.get("subjectName"),
        "generation_source": data.get("generationSource"),
        "prompt_used": data.get("promptUsed"),
        "is_ai_generated": False,
        "is_deleted": False,
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "created_by": created_by,
    }

    for frontend_field, db_field in _HEADER_FIELDS.items():
        doc[db_field] = data.get(frontend_field, "" if db_field == "department_name" else None)

    for frontend_field, db_field in _OPTIONAL_ID_FIELDS.items():
        value = data.get(frontend_field)
        if value:
            if not ObjectId.is_valid(value):
                raise ValueError(f"Invalid {frontend_field}")
            doc[db_field] = ObjectId(value)

    for frontend_field, db_field in _OPTIONAL_NAME_FIELDS.items():
        value = data.get(frontend_field)
        if value:
            doc[db_field] = value

    return doc


def build_update_fields(data: Dict[str, Any], updated_by: ObjectId) -> Dict[str, Any]:
    update_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc), "updated_by": updated_by}

    if "editorContent" in data:
        update_fields["editor_content"] = data["editorContent"]

    for frontend_field, db_field in _HEADER_FIELDS.items():
        if frontend_field in data:
            update_fields[db_field] = data[frontend_field]

    for frontend_field, db_field in _OPTIONAL_ID_FIELDS.items():
        if frontend_field in data and data[frontend_field]:
            if not ObjectId.is_valid(data[frontend_field]):
                raise ValueError(f"Invalid {frontend_field}")
            update_fields[db_field] = ObjectId(data[frontend_field])

    for frontend_field, db_field in _OPTIONAL_NAME_FIELDS.items():
        if frontend_field in data and data[frontend_field]:
            update_fields[db_field] = data[frontend_field]

    return update_fields


def serialize_question_paper(doc: Dict[str, Any]) -> Dict[str, Any] | None:
    if not doc:
        return None

    return {
        "_id": str(doc["_id"]),
        "subjectId": str(doc.get("subject_id")) if doc.get("subject_id") else None,
        "facultyId": str(doc.get("faculty_id")) if doc.get("faculty_id") else None,
        "subjectName": doc.get("subject_name"),
        "instituteName": doc.get("institute_name"),
        "departmentName": doc.get("department_name"),
        "questionPaperUrl": doc.get("question_paper_url"),
        "questionPaperFileId": doc.get("question_paper_file_id"),
        "editorContent": doc.get("editor_content"),
        "examType": doc.get("exam_type"),
        "academicYear": doc.get("academic_year"),
        "semester": doc.get("semester"),
        "totalMarks": doc.get("total_marks"),
        "duration": doc.get("duration"),
        "folderName": doc.get("folder_name"),
        "isAiGenerated": doc.get("is_ai_generated", False),
        "generationSource": doc.get("generation_source"),
        "schoolId": str(doc.get("school_id")) if doc.get("school_id") else None,
        "programmeId": str(doc.get("programme_id")) if doc.get("programme_id") else None,
        "departmentId": str(doc.get("department_id")) if doc.get("department_id") else None,
        "batchId": str(doc.get("batch_id")) if doc.get("batch_id") else None,
        "schoolName": doc.get("school_name"),
        "programmeName": doc.get("programme_name"),
        "batchName": doc.get("batch_name"),
        "isActive": doc.get("is_active", True),
        "createdAt": doc.get("created_at").isoformat() if doc.get("created_at") else None,
    }
