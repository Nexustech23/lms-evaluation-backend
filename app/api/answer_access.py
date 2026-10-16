"""Access checks for exam-scoped answer sheets.

The parent exam, rather than the answer's uploader or a supplied faculty ID,
is authoritative. Institute administrators may read within their institute;
only the exam's owning faculty member may change its answer sheets.
"""

from bson import ObjectId
from fastapi import HTTPException

from app.api.deps import get_current_user_and_faculty_details
from app.models.user import FACULTY, INSTITUTE


def _object_id(value):
    if value is None or not ObjectId.is_valid(str(value)):
        return None
    return ObjectId(str(value))


def _require_role(identity: dict, allow_institute_read: bool):
    allowed = {FACULTY, INSTITUTE} if allow_institute_read else {FACULTY}
    if identity.get("role") not in allowed:
        raise HTTPException(status_code=403, detail="Access denied")


async def require_exam_access(
    identity: dict, db, exam_id: str, *, allow_institute_read: bool = False
) -> dict:
    _require_role(identity, allow_institute_read)
    exam_object_id = _object_id(exam_id)
    if exam_object_id is None:
        raise HTTPException(status_code=400, detail="Invalid exam_id")

    exam = await db["newsavedDocs"].find_one({"_id": exam_object_id})
    if exam is None:
        raise HTTPException(status_code=404, detail="Exam not found")

    if identity.get("role") == FACULTY:
        _, faculty_id, error = await get_current_user_and_faculty_details(identity, db)
        if error:
            message, code = error
            raise HTTPException(status_code=code, detail=message)
        owner_id = _object_id(exam.get("faculty_id"))
        if owner_id is None or owner_id != _object_id(faculty_id):
            raise HTTPException(status_code=404, detail="Exam not found")
    else:
        user_id = _object_id(identity.get("user_id"))
        school_id = _object_id(exam.get("school_id"))
        if user_id is None or school_id is None:
            raise HTTPException(status_code=404, detail="Exam not found")
        institute = await db["instituteDetails"].find_one({"user_id": user_id})
        school = await db["schoolDetails"].find_one({"_id": school_id})
        institute_id = _object_id(institute.get("_id")) if institute else None
        school_institute_id = _object_id(school.get("institute_id")) if school else None
        if institute_id is None or school_institute_id != institute_id:
            raise HTTPException(status_code=404, detail="Exam not found")

    return exam


async def require_answer_access(identity: dict, db, answer_id: str) -> dict:
    _require_role(identity, allow_institute_read=False)
    answer_object_id = _object_id(answer_id)
    if answer_object_id is None:
        raise HTTPException(status_code=400, detail="Invalid answer_id")
    answer = await db["answerDetails"].find_one({"_id": answer_object_id})
    if answer is None or _object_id(answer.get("exam_id")) is None:
        raise HTTPException(status_code=404, detail="Answer script not found")
    await require_exam_access(identity, db, str(answer["exam_id"]))
    return answer
