"""Validate an exam's complete hierarchy before persisting references."""
from fastapi import HTTPException
from app.api.subject_access import _object_id, require_subject_report_access


async def require_exam_hierarchy(identity, db, values):
    subject = await require_subject_report_access(identity, db, str(values.get("subject_id", "")))
    institute = _object_id(subject.get("institute_id"))
    selected = {}
    for field, collection in (("school_id", "schoolDetails"), ("programme_id", "programmeDetails"),
                              ("department_id", "departmentDetails"), ("batch_id", "batchDetails")):
        oid = _object_id(values.get(field))
        expected = _object_id(subject.get(field))
        if oid != expected:
            raise HTTPException(400, f"{field} must match the assigned subject")
        if oid is None:
            if field in ("school_id", "programme_id"):
                raise HTTPException(400, f"{field} is required")
            continue
        entity = await db[collection].find_one({"_id": oid})
        if (not entity or entity.get("is_deleted") is True
                or _object_id(entity.get("institute_id")) != institute):
            raise HTTPException(404, "Exam hierarchy not found")
        selected[field] = entity
    links = (("programme_id", "school_id"), ("department_id", "programme_id"),
             ("batch_id", "school_id"), ("batch_id", "programme_id"), ("batch_id", "department_id"))
    for child, parent in links:
        if child in selected and _object_id(selected[child].get(parent)) != _object_id(values.get(parent)):
            raise HTTPException(400, "Selected exam hierarchy is inconsistent")
    if int(values.get("semester", 0)) != subject.get("semester"):
        raise HTTPException(400, "Semester must match the assigned subject")
    batch = selected.get("batch_id")
    if batch and int(values["semester"]) not in batch.get("semesters", []):
        raise HTTPException(400, "Semester does not belong to this batch")
    return subject
