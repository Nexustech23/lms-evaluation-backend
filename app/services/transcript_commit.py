"""Publish a complete transcript set atomically, serializing batch writers."""
from copy import deepcopy
from fastapi import HTTPException
from pymongo.errors import PyMongoError


async def commit_transcript_set(db, institute_id, batch_id, documents, record, semester=None):
    if not documents:
        raise HTTPException(400, "Cannot publish an empty transcript set")
    generation = record["import_id"]
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
        result = await db["batchDetails"].update_one(
            {"_id": batch_id, "institute_id": institute_id, "is_deleted": {"$ne": True}},
            {"$inc": {"transcript_write_revision": 1}}, session=session)
        if result.matched_count != 1:
            raise HTTPException(409, "Batch changed or was deleted")
        scope = {"institute_id": institute_id, "batch_id": batch_id}
        if semester is not None:
            scope["semester_no"] = int(semester)
        # Delete and replace are invisible to readers until the commit. No
        # outside cleanup may delete another writer's current generation.
        await db["academic_transcripts"].delete_many(scope, session=session)
        await db["academic_transcripts"].insert_many(deepcopy(documents), ordered=True, session=session)
        await db["transcriptImports"].insert_one(deepcopy(record), session=session)

    try:
        async with await db.client.start_session() as session:
            await session.with_transaction(publish)
    except PyMongoError as exc:
        raise HTTPException(503, "Transcript commit could not be confirmed. Check the current transcript before retrying; MongoDB transaction support is required.") from exc
