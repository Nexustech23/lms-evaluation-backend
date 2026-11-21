# ============================================================
# Bulk update of existing students: Phase 2, the preview.
#
# Reads the file (phase 1), reads the matching students (READ ONLY), shows old against new for every student, and stores
# the preview so a later step can apply exactly what was shown. This module never changes a student.
# ============================================================

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from bson import ObjectId
from fastapi import HTTPException

from app.core.config import settings
from app.core.diagnostics import event
from app.services.bulk_update import (
    KEY_FIELD, ParsedFile, Plan, index_students, parse_upload, plan_changes,
)

COLLECTION = "bulkUpdateBatches"
KIND = "students"
SHOWN_LIMIT = 200            # rows listed in a response; the full list stays stored
STUDENT_FIELDS = {"_id": 1, "enrollment_no": 1, "name": 1, "father_name": 1, "dob": 1, "contact_no": 1,
                  "updated_at": 1, "user_id": 1}


def plain(value: Any) -> Any:
    """A value that can go into a JSON response (dates as text, ids as text)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, ObjectId):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return str(value)


def safe_file_name(name: Optional[str]) -> str:
    return os.path.basename((name or "").replace("\\", "/")).strip()[:120] or "upload"


def key_variants(keys: List[str]) -> List[Any]:
    """The enrollment numbers to look for. A number stored as a number (123) is found by the text '123' too,
    but only when no leading zero would be lost."""
    variants: List[Any] = list(keys)
    for key in keys:
        if key.isdigit() and str(int(key)) == key:
            variants.append(int(key))
    return variants


async def load_students(db, institute_id, keys: List[str]) -> List[Dict[str, Any]]:
    """The institute's own, not-deleted students with these enrollment numbers. Read only."""
    if not keys:
        return []
    cursor = db["studentDetails"].find(
        {"institute_id": institute_id, "is_deleted": {"$ne": True}, KEY_FIELD: {"$in": key_variants(keys)}}, STUDENT_FIELDS)
    return [student async for student in cursor]


def _stored_change(change) -> Dict[str, Any]:
    return {"row": change.row, "enrollment_no": change.enrollment_no, "student_id": change.student_id,
            "student_name": change.student_name, "changes": change.changes, "version": change.version}


def _status_of(batch: Dict[str, Any], now: datetime) -> str:
    expires = batch.get("expires_at")
    if batch.get("status") == "previewed" and expires is not None and _aware(expires) < _aware(now):
        return "expired"
    return batch.get("status", "")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def batch_view(batch: Dict[str, Any], now: Optional[datetime] = None, limit: int = SHOWN_LIMIT) -> Dict[str, Any]:
    """A stored batch as the response the admin sees."""
    now = now or datetime.now(timezone.utc)
    changes = batch.get("changes", [])
    rejected = batch.get("rejected", [])
    counts = batch.get("counts", {})
    view = {
        "batch_id": str(batch["_id"]), "status": _status_of(batch, now), "file_name": batch.get("file_name"),
        "created_at": plain(batch.get("created_at")), "expires_at": plain(batch.get("expires_at")),
        "counts": counts, "columns_used": batch.get("columns_used", {}), "ignored_columns": batch.get("ignored_columns", []),
        "changes": [_change_view_stored(c) for c in changes[:limit]], "changes_truncated": len(changes) > limit,
        "rejected": plain(rejected[:limit]), "rejected_truncated": len(rejected) > limit,
        "unchanged_count": counts.get("unchanged", 0), "warnings": batch.get("warnings", []),
        "can_apply": _status_of(batch, now) == "previewed" and counts.get("to_update", 0) > 0,
    }
    if batch.get("applied_at"):
        until = undo_until(batch)
        view["applied_at"] = plain(batch["applied_at"])
        view["applied_by"] = plain(batch.get("applied_by"))
        view["reason"] = batch.get("reason")
        view["undo_until"] = plain(until)
        view["can_undo"] = view["status"] == "applied" and _aware(now) <= until
    if batch.get("undone_at"):
        view["undone_at"] = plain(batch["undone_at"])
        view["undo_reason"] = batch.get("undo_reason")
    return view


def undo_until(batch: Dict[str, Any]) -> datetime:
    """The last moment an applied update can still be undone."""
    return _aware(batch["applied_at"]) + timedelta(days=settings.BULK_UPDATE_UNDO_DAYS)


def _change_view_stored(change: Dict[str, Any]) -> Dict[str, Any]:
    return {"row": change["row"], "enrollment_no": change["enrollment_no"], "student_id": plain(change["student_id"]),
            "student_name": change.get("student_name", ""), "changes": plain(change["changes"])}


async def create_preview(db, institute_id, user_id, data: bytes, filename: Optional[str],
                         now: Optional[datetime] = None) -> Dict[str, Any]:
    """Read the file, match the students, store the preview, and return what the admin should see. Changes no student."""
    now = now or datetime.now(timezone.utc)
    parsed: ParsedFile = parse_upload(data, filename or "", settings.BULK_UPDATE_MAX_ROWS)

    keys = sorted({row.enrollment_no for row in parsed.rows if row.enrollment_no and not row.errors})
    students = await load_students(db, institute_id, keys)
    plan: Plan = plan_changes(parsed, index_students(students))

    digest = hashlib.sha256(data).hexdigest()
    warnings: List[str] = []
    earlier = None
    async for batch in db[COLLECTION].find({"institute_id": institute_id, "kind": KIND, "file_sha256": digest, "status": "applied"}, {"applied_at": 1}):
        if earlier is None or _aware(batch.get("applied_at") or now) > _aware(earlier.get("applied_at") or now):
            earlier = batch
    if earlier is not None:
        warnings.append(f"This exact file was already applied on {plain(earlier.get('applied_at'))}. Applying it again changes nothing new.")
    if parsed.ignored_columns:
        warnings.append("These columns are not read, because they cannot be changed here: " + ", ".join(parsed.ignored_columns) + ".")

    document = {
        "institute_id": institute_id, "kind": KIND, "status": "previewed", "created_by": user_id,
        "created_at": now, "expires_at": now + timedelta(hours=settings.BULK_UPDATE_PREVIEW_HOURS),
        "file_name": safe_file_name(filename), "file_sha256": digest,
        "columns_used": parsed.columns_used, "ignored_columns": parsed.ignored_columns,
        "counts": {"rows": parsed.total_rows, "to_update": len(plan.changes), "unchanged": len(plan.unchanged),
                   "rejected": len(plan.rejected)},
        "changes": [_stored_change(c) for c in plan.changes],
        "unchanged": plan.unchanged, "rejected": plan.rejected, "warnings": warnings,
    }
    result = await db[COLLECTION].insert_one(document)
    document["_id"] = result.inserted_id
    event("bulk_update.previewed", rows=parsed.total_rows, to_update=len(plan.changes), rejected=len(plan.rejected))
    return batch_view(document, now)


async def get_batch(db, institute_id, batch_id: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """One stored batch, only if it belongs to this institute."""
    if not ObjectId.is_valid(batch_id):
        raise HTTPException(status_code=404, detail="Bulk update not found")
    batch = await db[COLLECTION].find_one({"_id": ObjectId(batch_id), "institute_id": institute_id, "kind": KIND})
    if not batch:
        raise HTTPException(status_code=404, detail="Bulk update not found")
    return batch_view(batch, now)
