"""Publish a complete transcript set atomically, serializing batch writers."""
from copy import deepcopy
from fastapi import HTTPException
from pymongo.errors import PyMongoError
from app.services.result_revision import source_snapshot


async def commit_transcript_set(db, institute_id, batch_id, documents, record, semester=None, expected_revision=None):
    if not documents:
        raise HTTPException(400, "Cannot publish an empty transcript set")
    generation = record["import_id"]
    if expected_revision is None:
        raise HTTPException(409, "A source snapshot is required before publishing transcripts")
    seen = set()
    for doc in documents:
        key = (doc.get("student_id"), doc.get("semester_no"))
        if (doc.get("institute_id") != institute_id or doc.get("batch_id") != batch_id
                or doc.get("import_id") != generation or key in seen
                or (semester is not None and doc.get("semester_no") != int(semester))):
            raise HTTPException(400, "Transcript set scope is inconsistent")
        seen.add(key)

    async def publish(session):
        # Every import, manual generation and auto-refresh writes this same
        # batch fence. Mongo retries conflicting transactions on a fresh snapshot.
        batch_query = {"_id": batch_id, "institute_id": institute_id, "is_deleted": {"$ne": True}}
        current = await db["batchDetails"].find_one(batch_query, session=session)
        if not current or source_snapshot(current) != tuple(expected_revision):
            raise HTTPException(409, "Results or transcripts changed during calculation. Generate a fresh preview and retry.")
        result = await db["batchDetails"].update_one(
            batch_query,
            {"$inc": {"transcript_write_revision": 1}}, session=session)
        if result.matched_count != 1:
            raise HTTPException(409, "Batch changed or was deleted")
        scope = {"institute_id": institute_id, "batch_id": batch_id}
        if semester is not None:
            scope["semester_no"] = int(semester)
        # Delete and replace are invisible to readers until the commit. No
        # outside cleanup may delete another writer's current generation.
        await db["academic_transcripts"].delete_many(scope, session=session)
        fresh = deepcopy(documents)
        for doc in fresh:
            doc.update(is_stale=False, results_stale=False, cgpa_stale=False,
                       published_source_revision=expected_revision[0],
                       published_write_revision=expected_revision[1] + 1)
        await db["academic_transcripts"].insert_many(fresh, ordered=True, session=session)
        # Per-semester credit totals are the source for cumulative GPA. Replacing
        # semester 5 must also repair semester 6's cumulative value atomically.
        # Never count a later semester in an earlier semester's CGPA.
        students = {doc["student_id"] for doc in fresh}
        for student in students:
            rows = [row async for row in db["academic_transcripts"].find(
                {"institute_id": institute_id, "batch_id": batch_id, "student_id": student},
                session=session).sort("semester_no", 1)]
            credits, points, blocked = 0, 0, False
            for row in rows:
                own_results_stale = row.get("results_stale") or (
                    row.get("is_stale") and not row.get("cgpa_stale"))
                blocked = blocked or bool(own_results_stale)
                credits += row.get("total_credits", 0)
                points += row.get("total_credit_points", 0)
                if semester is not None and row["semester_no"] < int(semester):
                    if blocked:
                        raise HTTPException(409, "Regenerate earlier stale semesters before publishing this semester.")
                    continue
                if blocked:
                    changes = {"is_stale": True, "cgpa_stale": True}
                else:
                    changes = {"cgpa": round(points / credits, 2) if credits else 0,
                               "is_stale": False, "cgpa_stale": False}
                await db["academic_transcripts"].update_one({"_id": row["_id"]},
                    {"$set": changes}, session=session)
        await db["transcriptImports"].insert_one(deepcopy(record), session=session)

    try:
        async with await db.client.start_session() as session:
            await session.with_transaction(publish)
    except PyMongoError as exc:
        raise HTTPException(503, "Transcript commit could not be confirmed. Check the current transcript before retrying; MongoDB transaction support is required.") from exc
