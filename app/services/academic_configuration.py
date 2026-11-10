"""Validate hierarchy links and version every input to academic transcripts."""
from datetime import datetime, timezone
from bson import ObjectId
from fastapi import HTTPException


async def validate_subject_links(db, subject, session=None, pending_batch=None):
    iid = subject["institute_id"]
    async def owned(collection, value, label):
        if not value:
            raise HTTPException(400, f"{label} is required")
        row = await db[collection].find_one({"_id": value, "institute_id": iid,
            "is_deleted": {"$ne": True}, "is_active": {"$ne": False}}, session=session)
        if row is None:
            raise HTTPException(400, f"{label} does not belong to the selected institute or is inactive")
        return row
    await owned("schoolDetails", subject.get("school_id"), "School")
    programme = await owned("programmeDetails", subject.get("programme_id"), "Programme")
    if programme.get("school_id") != subject["school_id"]:
        raise HTTPException(400, "Programme does not belong to the selected school")
    department = subject.get("department_id")
    if programme.get("has_department") and not department:
        raise HTTPException(400, "Department is required for this programme")
    if department:
        row = await owned("departmentDetails", department, "Department")
        if row.get("programme_id") != programme["_id"] or row.get("school_id") != subject["school_id"]:
            raise HTTPException(400, "Department does not match the selected programme and school")
    if subject.get("batch_id"):
        batch = pending_batch if pending_batch and pending_batch.get("_id") == subject["batch_id"] else await owned("batchDetails", subject["batch_id"], "Batch")
        for key in ("institute_id", "school_id", "programme_id", "department_id"):
            if batch.get(key) != subject.get(key):
                raise HTTPException(400, "Subject hierarchy does not match the selected batch")
        if batch.get("total_semesters") and subject["semester"] > int(batch["total_semesters"]):
            raise HTTPException(400, "Semester exceeds the selected batch duration")
    faculty_id = subject.get("faculty_id")
    if faculty_id:
        faculty = await owned("facultyDetails", faculty_id, "Faculty")
        if faculty.get("school_id") and faculty["school_id"] != subject["school_id"]:
            raise HTTPException(400, "Faculty does not belong to the selected school")
        account = await db["users"].find_one({"_id": faculty.get("user_id"),
            "is_deleted": {"$ne": True}, "is_active": {"$ne": False}}, session=session)
        if account is None:
            raise HTTPException(400, "Faculty login account is inactive or missing")
    co_codes = {row["co_code"] for row in subject.get("co", [])}
    po_codes = {row["po_code"] for row in programme.get("po", [])}
    for co, mapping in subject.get("co_po_matrix", {}).items():
        if co not in co_codes or not set(mapping).issubset(po_codes):
            raise HTTPException(400, "CO/PO matrix references an undefined course or programme outcome")


async def invalidate_subject(db, subject, session):
    if not subject.get("batch_id"):
        return
    scope = {"institute_id": subject["institute_id"], "batch_id": subject["batch_id"]}
    result = await db["batchDetails"].update_one({"_id": subject["batch_id"],
        "institute_id": subject["institute_id"]}, {"$inc": {"result_source_revision": 1}}, session=session)
    if result.matched_count != 1:
        raise HTTPException(409, "Subject batch link needs correction")
    semester = int(subject["semester"])
    stale = {"is_stale": True, "stale_at": datetime.now(timezone.utc),
             "stale_reason": "Academic configuration changed. Regenerate this semester."}
    await db["academic_transcripts"].update_many({**scope, "semester_no": semester},
        {"$set": {**stale, "results_stale": True}}, session=session)
    await db["academic_transcripts"].update_many({**scope, "semester_no": {"$gt": semester}},
        {"$set": {**stale, "cgpa_stale": True}}, session=session)


async def invalidate_policy(db, institute_id, session):
    # Same batch fence as publication: a concurrent preview cannot publish
    # old policy results after the policy transaction commits.
    await db["batchDetails"].update_many({"institute_id": institute_id},
        {"$inc": {"result_source_revision": 1}}, session=session)
    await db["academic_transcripts"].update_many({"institute_id": institute_id},
        {"$set": {"is_stale": True, "results_stale": True, "cgpa_stale": True,
        "stale_at": datetime.now(timezone.utc), "stale_reason": "Grading policy changed. Regenerate transcripts."}}, session=session)
