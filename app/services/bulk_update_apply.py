# ============================================================
# Bulk update of existing students: Phase 3, applying a preview.
#
# Applies exactly what the preview showed, as ONE all-or-nothing step: every student and login account changes, or none
# does. Before it writes anything it checks, inside that same step, that no student was changed by anyone since the
# preview. A reason is required. Applying the same preview twice does nothing the second time.
# ============================================================

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException

from app.core.diagnostics import event
from app.services.bulk_update import UPDATE_FIELDS, version_of
from app.services.bulk_update_preview import COLLECTION, KIND, _status_of, batch_view
from app.services.result_revision import transaction

MIN_REASON = 3
MAX_REASON = 500
# A change to these fields also changes the login account, so a person's name and phone never disagree between the two.
USER_FIELD_FOR = {"name": "fullName", "contact_no": "phone"}


def clean_reason(reason: Optional[str]) -> str:
    text = " ".join((reason or "").split())
    if len(text) < MIN_REASON:
        raise HTTPException(status_code=400, detail="Give a reason for this update (a few words).")
    if len(text) > MAX_REASON:
        raise HTTPException(status_code=400, detail=f"The reason is longer than {MAX_REASON} characters.")
    return text


def _conflict(message: str, rows: Optional[List[Dict[str, Any]]] = None) -> HTTPException:
    return HTTPException(status_code=409, detail={"message": message, "rows": rows or []})


async def apply_batch(db, institute_id, user_id, batch_id: str, reason: Optional[str], skip_rejected: bool = False,
                      now: Optional[datetime] = None, run=transaction) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if not ObjectId.is_valid(batch_id):
        raise HTTPException(status_code=404, detail="Bulk update not found")
    batch_oid = ObjectId(batch_id)
    batch = await db[COLLECTION].find_one({"_id": batch_oid, "institute_id": institute_id, "kind": KIND})
    if not batch:
        raise HTTPException(status_code=404, detail="Bulk update not found")

    status = _status_of(batch, now)
    if status == "applied":                                    # a second click, or a retry: nothing more happens
        return {**batch_view(batch, now), "already_applied": True}
    if status == "expired":
        raise _conflict("This preview has expired. Upload the file again to get a new preview.")
    if status != "previewed":
        raise _conflict(f"This update cannot be applied because it is {status or 'not ready'}.")

    reason = clean_reason(reason)
    counts = batch.get("counts", {})
    if counts.get("to_update", 0) <= 0:
        raise HTTPException(status_code=400, detail="There is nothing to apply: no student would change.")
    if counts.get("rejected", 0) > 0 and not skip_rejected:
        raise _conflict(f"{counts['rejected']} rows have problems and would be skipped. Fix the file and upload it again, "
                        "or confirm that those rows should be skipped.")

    async def operation(session):
        rows = [dict(row) for row in batch["changes"]]
        for row in rows:                                       # only the allowed fields may ever be written
            if any(name not in UPDATE_FIELDS for name in row.get("changes", {})):
                raise _conflict("This update contains a field that cannot be changed, so nothing was done.")
        stale: List[Dict[str, Any]] = []
        current: Dict[Any, Dict[str, Any]] = {}
        for row in rows:                                       # 1. nobody changed these students since the preview
            student = await db["studentDetails"].find_one(
                {"_id": row["student_id"], "institute_id": institute_id, "is_deleted": {"$ne": True}}, session=session)
            if student is None:
                stale.append({"row": row["row"], "enrollment_no": row["enrollment_no"], "reason": "This student no longer exists."})
            elif version_of(student) != row["version"]:
                stale.append({"row": row["row"], "enrollment_no": row["enrollment_no"], "reason": "This student was changed after the preview."})
            else:
                current[row["student_id"]] = student
        if stale:
            raise _conflict("Some students changed after the preview, so nothing was applied. Upload the file again.", stale)

        for row in rows:                                       # 2. change the student and the login account together
            fields = {name: change["new"] for name, change in row["changes"].items()}
            result = await db["studentDetails"].update_one(
                {"_id": row["student_id"], "institute_id": institute_id}, {"$set": {**fields, "updated_at": now}}, session=session)
            if result.matched_count != 1:
                raise _conflict("A student changed while the update was being applied, so nothing was applied. Try again.")
            after = await db["studentDetails"].find_one({"_id": row["student_id"]}, session=session)
            row["applied_version"] = version_of(after or {})

            row["user_before"] = None
            user_fields = {USER_FIELD_FOR[name]: value for name, value in fields.items() if name in USER_FIELD_FOR}
            account_id = current[row["student_id"]].get("user_id")
            if user_fields and account_id:
                account = await db["users"].find_one({"_id": account_id}, session=session)
                if account is not None:
                    row["user_before"] = {name: account.get(name) for name in user_fields}
                    await db["users"].update_one({"_id": account_id}, {"$set": {**user_fields, "updated_at": now}}, session=session)

        done = await db[COLLECTION].update_one(                # 3. mark the batch applied, only if nobody else did first
            {"_id": batch_oid, "institute_id": institute_id, "status": "previewed"},
            {"$set": {"status": "applied", "reason": reason, "applied_at": now, "applied_by": user_id,
                      "skip_rejected": bool(skip_rejected), "changes": rows}}, session=session)
        if done.matched_count != 1:
            raise _conflict("This update was already applied.")
        return len(rows)

    applied = await run(db, operation)
    event("bulk_update.applied", students=applied)
    fresh = await db[COLLECTION].find_one({"_id": batch_oid})
    return {**batch_view(fresh, now), "applied_count": applied, "skipped_count": counts.get("rejected", 0),
            "already_applied": False}
