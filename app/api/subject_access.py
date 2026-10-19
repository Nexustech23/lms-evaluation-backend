"""Read access to a subject's combined student reports."""
from bson import ObjectId
from fastapi import HTTPException

from app.api.deps import get_current_user_and_faculty_details
from app.models.user import FACULTY, INSTITUTE


def _object_id(value):
    return ObjectId(str(value)) if value is not None and ObjectId.is_valid(str(value)) else None


async def require_subject_report_access(identity: dict, db, subject_id: str) -> dict:
    role = identity.get("role")
    if role not in (FACULTY, INSTITUTE):
        raise HTTPException(status_code=403, detail="Access denied")
    subject_oid = _object_id(subject_id)
    if subject_oid is None:
        raise HTTPException(status_code=400, detail="Invalid subject_id")
    subject = await db["subjectDetails"].find_one({"_id": subject_oid})
    if not subject or subject.get("is_deleted") is True:
        raise HTTPException(status_code=404, detail="Subject not found")

    subject_institute = _object_id(subject.get("institute_id"))
    if role == FACULTY:
        _, faculty_id, error = await get_current_user_and_faculty_details(identity, db)
        if error:
            message, code = error
            raise HTTPException(status_code=code, detail=message)
        faculty_oid = _object_id(faculty_id)
        faculty = await db["facultyDetails"].find_one({"_id": faculty_oid})
        allowed = (
            faculty_oid is not None
            and _object_id(subject.get("faculty_id")) == faculty_oid
            and subject_institute is not None
            and _object_id((faculty or {}).get("institute_id")) == subject_institute
        )
    else:
        user_oid = _object_id(identity.get("user_id"))
        institute = await db["instituteDetails"].find_one({"user_id": user_oid}) if user_oid else None
        allowed = subject_institute is not None and _object_id((institute or {}).get("_id")) == subject_institute

    if not allowed:
        raise HTTPException(status_code=404, detail="Subject not found")
    return subject
