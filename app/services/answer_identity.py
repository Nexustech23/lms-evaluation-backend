"""Stable student identities, independent of subsequent display-file renames."""
from fastapi import HTTPException


def student_id_from_filename(filename):
    if not isinstance(filename, str):
        return ""
    filename = filename.strip()
    if filename.startswith("MANUAL-"):
        return filename[len("MANUAL-"):].strip()
    return filename.rsplit(".", 1)[0].strip() if "." in filename else filename


def answer_student_id(answer):
    value = answer.get("student_id")
    return (str(value).strip() if value is not None else "") or student_id_from_filename(answer.get("filename"))


async def require_unique_identity(db, exam_id, student_id, answer_id=None, session=None):
    if not student_id:
        raise HTTPException(409, "Student identity is missing; correct it before saving")
    options = {"session": session} if session is not None else {}
    async for answer in db["answerDetails"].find({"exam_id": exam_id}, **options):
        if answer.get("_id") != answer_id and answer_student_id(answer) == student_id:
            raise HTTPException(409, "This student already has an answer in the exam. Resolve the duplicate identity first.")
