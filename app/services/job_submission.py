"""Mongo compare-and-set guards: a lost HTTP response cannot start spend twice."""
from datetime import datetime, timezone
import hashlib
import json

from fastapi import HTTPException
from app.core.diagnostics import event

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
    for attempt in range(3):
        document = await db[collection].find_one(query)
        if document is None:
            event("submission.rejected",candidate_job_id=candidate,collection=collection,reason="record_not_found")
            raise HTTPException(404, "Record not found")
        active = nested(document, field)
        if active:
            job = await get_owned_job(prefix, active["job_id"], identity)
            if job is None:
                event("submission.rejected",candidate_job_id=candidate,job_id=active["job_id"],reason="active_job_status_unavailable_or_not_owned")
                raise HTTPException(409, "An earlier job has no available status. Check it before starting another paid job.")
            if job.get("status") not in TERMINAL:
                event("submission.reused",candidate_job_id=candidate,job_id=active["job_id"],reason="active_owned_job",new_execution=False)
                return active["job_id"], False, active.get("options", {})
            event("submission.previous_terminal",job_id=active["job_id"],candidate_job_id=candidate,status=job.get("status"))
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
            event("submission.claimed",job_id=candidate,collection=collection,attempt=attempt+1,new_job_claim=True)
            return candidate, True, options or {}
        event("submission.race_lost",candidate_job_id=candidate,collection=collection,attempt=attempt+1)
        await update_job(prefix, candidate, {"status": "error", "message": "Another request already started this job."})
    event("submission.rejected",candidate_job_id=candidate,reason="contention_exhausted")
    raise HTTPException(409, "Another request changed this record. Retry to reconnect to its job.")


async def release_submission(db, collection, query, field, job_id):
    result = await db[collection].update_one(
        {**query, f"{field}.job_id": job_id}, {"$unset": {field: ""}},
    )
    event("submission.release",job_id=job_id,collection=collection,
          matched_count=getattr(result,'matched_count',None),modified_count=getattr(result,'modified_count',None))
