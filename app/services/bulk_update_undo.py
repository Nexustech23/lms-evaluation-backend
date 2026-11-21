# ============================================================
# Bulk update of existing students: Phase 4, history and undo.
#
# The history is the list of updates (previewed, applied, undone). An undo puts back exactly the old values the update
# recorded, as ONE all-or-nothing step, and only while those students and their login accounts are still exactly as the
# update left them: if anyone changed one since, nothing is undone and the rows are named. It works for a limited time.
# ============================================================

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException

from app.core.diagnostics import event
from app.services.bulk_update import UPDATE_FIELDS, version_of
from app.services.bulk_update_apply import USER_FIELD_FOR, _conflict, clean_reason
from app.services.bulk_update_preview import COLLECTION, KIND, _aware, _status_of, batch_view, plain, undo_until
from app.services.result_revision import transaction

HISTORY_PROJECTION = {"changes": 0, "unchanged": 0, "rejected": 0}      # the long lists are not needed for a summary


def history_item(batch: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """One line of the history: what, when, by whom, why, how many, and whether it can still be undone."""
    status = _status_of(batch, now)
    item = {
        "batch_id": str(batch["_id"]), "status": status, "file_name": batch.get("file_name"),
        "created_at": plain(batch.get("created_at")), "counts": batch.get("counts", {}),
        "reason": batch.get("reason"), "applied_at": plain(batch.get("applied_at")),
        "applied_by": plain(batch.get("applied_by")), "undone_at": plain(batch.get("undone_at")),
        "undo_reason": batch.get("undo_reason"), "can_undo": False, "undo_until": None,
    }
    if batch.get("applied_at"):
        until = undo_until(batch)
        item["undo_until"] = plain(until)
        item["can_undo"] = status == "applied" and _aware(now) <= until
    return item


async def list_history(db, institute_id, limit: int = 50, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    limit = max(1, min(int(limit), 100))
    cursor = db[COLLECTION].find({"institute_id": institute_id, "kind": KIND}, HISTORY_PROJECTION).sort("created_at", -1).limit(limit)
    return {"items": [history_item(batch, now) async for batch in cursor], "limit": limit}


async def undo_batch(db, institute_id, user_id, batch_id: str, reason: Optional[str],
                     now: Optional[datetime] = None, run=transaction) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if not ObjectId.is_valid(batch_id):
        raise HTTPException(status_code=404, detail="Bulk update not found")
    batch_oid = ObjectId(batch_id)
    batch = await db[COLLECTION].find_one({"_id": batch_oid, "institute_id": institute_id, "kind": KIND})
    if not batch:
        raise HTTPException(status_code=404, detail="Bulk update not found")

    status = _status_of(batch, now)
    if status == "undone":                                     # a second click, or a retry: nothing more happens
        return {**batch_view(batch, now), "already_undone": True}
    if status != "applied":
        raise _conflict("Only an update that was applied can be undone.")
    if _aware(now) > undo_until(batch):
        raise _conflict("The time to undo this update has passed.")
    reason = clean_reason(reason)

    async def operation(session):
        rows = [dict(row) for row in batch["changes"]]
        for row in rows:                                       # only the allowed fields may ever be written
            if any(name not in UPDATE_FIELDS for name in row.get("changes", {})):
                raise _conflict("This update contains a field that cannot be changed, so nothing was done.")
        stale: List[Dict[str, Any]] = []
        current: Dict[Any, Dict[str, Any]] = {}
        accounts: Dict[Any, Optional[Dict[str, Any]]] = {}
        for row in rows:                                       # 1. everything is still exactly as the update left it
            student = await db["studentDetails"].find_one({"_id": row["student_id"], "institute_id": institute_id}, session=session)
            if student is None or student.get("is_deleted"):
                stale.append({"row": row["row"], "enrollment_no": row["enrollment_no"], "reason": "This student no longer exists."})
                continue
            if version_of(student) != row.get("applied_version"):
                stale.append({"row": row["row"], "enrollment_no": row["enrollment_no"], "reason": "This student was changed after the update."})
                continue
            current[row["student_id"]] = student
            account_id = student.get("user_id")
            if row.get("user_before") and account_id:
                account = await db["users"].find_one({"_id": account_id}, session=session)
                accounts[row["student_id"]] = account
                expected = {USER_FIELD_FOR[name]: change["new"] for name, change in row["changes"].items() if name in USER_FIELD_FOR}
                if account is not None and any(account.get(key) != expected.get(key) for key in row["user_before"]):
                    stale.append({"row": row["row"], "enrollment_no": row["enrollment_no"],
                                  "reason": "This student's login account was changed after the update."})
        if stale:
            raise _conflict("Some students or accounts changed after the update, so nothing was undone.", stale)

        for row in rows:                                       # 2. put the old values back, student and account together
            restore = {name: change["old"] for name, change in row["changes"].items() if not change.get("missing")}
            remove = {name: "" for name, change in row["changes"].items() if change.get("missing")}
            change_set: Dict[str, Any] = {"$set": {**restore, "updated_at": now}}
            if remove:
                change_set["$unset"] = remove
            result = await db["studentDetails"].update_one({"_id": row["student_id"], "institute_id": institute_id}, change_set, session=session)
            if result.matched_count != 1:
                raise _conflict("A student changed while the undo was being applied, so nothing was undone. Try again.")
            after = await db["studentDetails"].find_one({"_id": row["student_id"]}, session=session)
            row["undone_version"] = version_of(after or {})

            account_id = current[row["student_id"]].get("user_id")
            if row.get("user_before") and account_id and accounts.get(row["student_id"]) is not None:
                await db["users"].update_one({"_id": account_id}, {"$set": {**row["user_before"], "updated_at": now}}, session=session)

        done = await db[COLLECTION].update_one(                # 3. mark the batch undone, only if nobody else did first
            {"_id": batch_oid, "institute_id": institute_id, "status": "applied"},
            {"$set": {"status": "undone", "undone_at": now, "undone_by": user_id, "undo_reason": reason, "changes": rows}}, session=session)
        if done.matched_count != 1:
            raise _conflict("This update was already undone.")
        return len(rows)

    undone = await run(db, operation)
    event("bulk_update.undone", students=undone)
    fresh = await db[COLLECTION].find_one({"_id": batch_oid})
    return {**batch_view(fresh, now), "undone_count": undone, "already_undone": False}
