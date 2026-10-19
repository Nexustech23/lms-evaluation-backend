"""Mongo compare-and-set guards: a lost HTTP response cannot start spend twice."""
from datetime import datetime, timezone
import hashlib
import json

from fastapi import HTTPException

from app.services.job_store import get_owned_job, set_job, update_job, TERMINAL


def generation_field(params, files):
    files = {name: hashlib.sha256(value).hexdigest() if isinstance(value, bytes) else value
             for name, value in files.items()}
    digest = hashlib.sha256(json.dumps([params, files], sort_keys=True, default=str).encode()).hexdigest()
    return f"active_paper_jobs.{digest}"


def nested(document, field):
    for part in field.split("."):
        document = document.get(part) if isinstance(document, dict) else None
    return document


async def claim_submission(db, collection, query, field, prefix, candidate, identity, options=None):
    for _ in range(3):
        document = await db[collection].find_one(query)
        if document is None:
            raise HTTPException(404, "Record not found")
        active = nested(document, field)
        if active:
            job = await get_owned_job(prefix, active["job_id"], identity)
            if job is None:
                raise HTTPException(409, "An earlier job has no available status. Check it before starting another paid job.")
            if job.get("status") not in TERMINAL:
                return active["job_id"], False, active.get("options", {})
            await release_submission(db, collection, query, field, active["job_id"])
            continue
        payload = {"owner_user_id": str(identity["user_id"]), "status": "queued", "progress": 0}
        await set_job(prefix, candidate, payload)
        result = await db[collection].update_one(
            {**query, field: None}, {"$set": {field: {
                "job_id": candidate, "owner_user_id": str(identity["user_id"]),
                "started_at": datetime.now(timezone.utc), "options": options or {},
            }}},
        )
        if result.matched_count:
            return candidate, True, options or {}
        await update_job(prefix, candidate, {"status": "error", "message": "Another request already started this job."})
    raise HTTPException(409, "Another request changed this record. Retry to reconnect to its job.")


async def release_submission(db, collection, query, field, job_id):
    await db[collection].update_one(
        {**query, f"{field}.job_id": job_id}, {"$unset": {field: ""}},
    )
