"""Bulk update, phase 2: the preview. A fake database answers; no real database, no network, no AI.
The most important check: a preview never changes a student or a login account."""
import asyncio
import copy
import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException, UploadFile
from openpyxl import Workbook

from app.api.routers import bulk_update as router
from app.core.config import settings
from app.services import bulk_update_preview as bp
from app.services.bulk_update import BulkUpdateError

NOW = datetime(2026, 11, 21, 9, 0, 0, tzinfo=timezone.utc)
INSTITUTE, OTHER_INSTITUTE, ADMIN = ObjectId(), ObjectId(), ObjectId()
HEADER = ["Enrollment No", "Name", "Father's Name", "Date of Birth", "Contact No", "Roll No"]


def run(coro):
    return asyncio.run(coro)


# ---- a small fake database ---------------------------------------------------------------------------------------------

def _matches(doc, query):
    for key, want in query.items():
        have = doc.get(key)
        if isinstance(want, dict):
            if "$ne" in want and have == want["$ne"]:
                return False
            if "$in" in want and have not in want["$in"]:
                return False
        elif have != want:
            return False
    return True


class Cursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, key, direction=1):
        self.docs.sort(key=lambda d: d.get(key), reverse=direction < 0)
        return self

    def limit(self, n):
        self.docs = self.docs[:n]
        return self

    def __aiter__(self):
        async def gen():
            for doc in self.docs:
                yield copy.deepcopy(doc)
        return gen()


class Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.writes = 0

    async def find_one(self, query, projection=None):
        return next((copy.deepcopy(d) for d in self.docs if _matches(d, query)), None)

    def find(self, query=None, projection=None):
        return Cursor(d for d in self.docs if _matches(d, query or {}))

    async def insert_one(self, doc):
        self.writes += 1
        doc = copy.deepcopy(doc)
        doc.setdefault("_id", ObjectId())
        self.docs.append(doc)
        return type("R", (), {"inserted_id": doc["_id"]})()

    async def update_one(self, *a, **k):          # a preview must never call these
        self.writes += 1
        raise AssertionError("a preview must not update anything")

    delete_one = delete_many = insert_many = update_many = update_one


class Db:
    def __init__(self, **collections):
        self.c = {name: Collection(docs) for name, docs in collections.items()}

    def __getitem__(self, name):
        return self.c.setdefault(name, Collection())


def student(key, institute=INSTITUTE, **extra):
    base = {"_id": ObjectId(), "institute_id": institute, "enrollment_no": key, "name": "Asha Rao", "father_name": "Mohan Rao",
            "dob": "2004-05-06", "contact_no": "9876543210", "roll_no": "7", "user_id": ObjectId(),
            "updated_at": datetime(2026, 11, 1, 10, 0, 0), "is_deleted": False}
    base.update(extra)
    return base


def xlsx(rows):
    book = Workbook()
    for row in rows:
        book.active.append(row)
    buffer = io.BytesIO(); book.save(buffer)
    return buffer.getvalue()


def make_db(*students):
    return Db(studentDetails=list(students), users=[], bulkUpdateBatches=[])


def preview(db, rows, filename="students.xlsx", now=NOW, data=None):
    return run(bp.create_preview(db, INSTITUTE, ADMIN, data if data is not None else xlsx(rows), filename, now))


# ---- what the admin sees -----------------------------------------------------------------------------------------------

def test_the_preview_shows_old_against_new_and_the_counts():
    db = make_db(student("E1"), student("E2"), student("E3"))
    view = preview(db, [HEADER, ["E1", "New Name", "", "", "", ""], ["E2", "Asha Rao", "", "", "", ""], ["E9", "Ghost", "", "", "", ""], ["E3", "X", "", "99-99-9999", "", ""]])
    assert view["counts"] == {"rows": 4, "to_update": 1, "unchanged": 1, "rejected": 2}
    (change,) = view["changes"]
    assert change["enrollment_no"] == "E1" and change["changes"]["name"] == {"old": "Asha Rao", "new": "New Name", "missing": False}
    assert {r["enrollment_no"] for r in view["rejected"]} == {"E9", "E3"}
    assert view["status"] == "previewed" and view["can_apply"] is True and view["unchanged_count"] == 1


def test_a_preview_changes_nothing_in_the_database_except_to_store_itself():
    students = [student("E1"), student("E2")]
    db = make_db(*students)
    before = copy.deepcopy(db["studentDetails"].docs)
    preview(db, [HEADER, ["E1", "New Name", "Mohan", "2001-01-01", "9000000000", "99"], ["E2", "Other", "", "", "", ""]])
    assert db["studentDetails"].docs == before and db["studentDetails"].writes == 0     # a write would have raised
    assert db["users"].docs == [] and db["users"].writes == 0
    assert len(db["bulkUpdateBatches"].docs) == 1 and db["bulkUpdateBatches"].writes == 1


def test_the_stored_batch_holds_what_a_later_step_needs_and_not_the_file():
    s = student("E1")
    db = make_db(s)
    preview(db, [HEADER, ["E1", "New Name", "", "", "", ""]], filename=r"C:\fakepath\May students.xlsx")
    batch = db["bulkUpdateBatches"].docs[0]
    assert batch["status"] == "previewed" and batch["kind"] == "students" and batch["institute_id"] == INSTITUTE
    assert batch["created_by"] == ADMIN and batch["file_name"] == "May students.xlsx"
    assert batch["expires_at"] == NOW + timedelta(hours=settings.BULK_UPDATE_PREVIEW_HOURS)
    stored = batch["changes"][0]
    assert stored["student_id"] == s["_id"] and stored["version"] == "2026-11-01T10:00:00"
    assert len(batch["file_sha256"]) == 64
    assert not any(isinstance(v, (bytes, bytearray)) for v in batch.values())          # the file itself is not kept


def test_only_this_institutes_own_students_can_be_matched():
    db = make_db(student("E1", institute=OTHER_INSTITUTE), student("E2", is_deleted=True), student("E3"))
    view = preview(db, [HEADER, ["E1", "Hack", "", "", "", ""], ["E2", "Gone", "", "", "", ""], ["E3", "Fine", "", "", "", ""]])
    assert [c["enrollment_no"] for c in view["changes"]] == ["E3"]
    assert {r["enrollment_no"] for r in view["rejected"]} == {"E1", "E2"}
    assert all("No student in your college" in r["error"] for r in view["rejected"])


def test_a_student_stored_with_a_numeric_enrollment_number_is_found():
    db = make_db(student(2021001))
    view = preview(db, [HEADER, ["2021001", "New", "", "", "", ""]])
    assert view["counts"]["to_update"] == 1


def test_a_leading_zero_is_never_mistaken_for_a_number():
    db = make_db(student(123))
    view = preview(db, [HEADER, ["00123", "New", "", "", "", ""]])
    assert view["counts"] == {"rows": 1, "to_update": 0, "unchanged": 0, "rejected": 1}


def test_ignored_columns_and_a_repeated_file_are_warned_about():
    db = make_db(student("E1"))
    rows = [HEADER, ["E1", "New Name", "", "", "", "55"]]
    first = preview(db, rows)
    assert any("Roll No" in w for w in first["warnings"])
    # pretend the first batch was applied, then upload the identical file again
    db["bulkUpdateBatches"].docs[0].update({"status": "applied", "applied_at": NOW})
    again = preview(db, rows, now=NOW + timedelta(hours=1))
    assert any("already applied" in w for w in again["warnings"])


def test_a_file_that_changes_nothing_cannot_be_applied():
    db = make_db(student("E1"))
    view = preview(db, [HEADER, ["E1", "Asha Rao", "", "", "", ""]])
    assert view["counts"]["to_update"] == 0 and view["can_apply"] is False


def test_long_lists_are_cut_in_the_response_but_kept_in_full():
    rows = [HEADER] + [[f"E{i}", f"Name {i}", "", "", "", ""] for i in range(250)]
    db = make_db(*[student(f"E{i}") for i in range(250)])
    view = preview(db, rows)
    assert view["counts"]["to_update"] == 250 and len(view["changes"]) == 200 and view["changes_truncated"] is True
    assert len(db["bulkUpdateBatches"].docs[0]["changes"]) == 250


def test_the_response_is_plain_json_even_when_a_stored_date_is_a_real_date():
    db = make_db(student("E1", dob=datetime(2004, 5, 6, 0, 0)))
    view = preview(db, [HEADER, ["E1", "New", "", "2001-01-01", "", ""]])
    text = json.dumps(view)
    assert "2004-05-06T00:00:00" in text


def test_file_problems_are_reported_as_one_plain_message():
    db = make_db()
    with pytest.raises(BulkUpdateError, match="xlsx or .csv"):
        preview(db, None, filename="notes.txt", data=b"hello")
    assert db["bulkUpdateBatches"].docs == []                     # nothing stored for a bad file


# ---- reading a stored preview ------------------------------------------------------------------------------------------

def test_a_stored_preview_can_be_read_back_and_expires():
    db = make_db(student("E1"))
    preview(db, [HEADER, ["E1", "New", "", "", "", ""]])
    batch_id = str(db["bulkUpdateBatches"].docs[0]["_id"])
    fresh = run(bp.get_batch(db, INSTITUTE, batch_id, NOW + timedelta(hours=1)))
    assert fresh["status"] == "previewed" and fresh["can_apply"] is True
    late = run(bp.get_batch(db, INSTITUTE, batch_id, NOW + timedelta(hours=settings.BULK_UPDATE_PREVIEW_HOURS, minutes=1)))
    assert late["status"] == "expired" and late["can_apply"] is False


def test_another_institute_cannot_read_a_preview_and_bad_ids_are_not_found():
    db = make_db(student("E1"))
    preview(db, [HEADER, ["E1", "New", "", "", "", ""]])
    batch_id = str(db["bulkUpdateBatches"].docs[0]["_id"])
    for institute, ident in [(OTHER_INSTITUTE, batch_id), (INSTITUTE, "not-an-id"), (INSTITUTE, str(ObjectId()))]:
        with pytest.raises(HTTPException) as error:
            run(bp.get_batch(db, institute, ident))
        assert error.value.status_code == 404


# ---- the routes --------------------------------------------------------------------------------------------------------

@pytest.fixture
def as_admin(monkeypatch):
    def pick(result):
        async def fake(identity, db):
            return result
        monkeypatch.setattr(router, "get_current_user_and_institute", fake)
    return pick


def upload(data, name="students.xlsx"):
    return UploadFile(filename=name, file=io.BytesIO(data))


def test_the_preview_route_works_for_an_institute_admin(as_admin):
    as_admin(({"_id": ADMIN}, INSTITUTE, None))
    db = make_db(student("E1"))
    view = run(router.preview_students(file=upload(xlsx([HEADER, ["E1", "New", "", "", "", ""]])), identity={}, db=db))
    assert view["counts"]["to_update"] == 1 and view["status"] == "previewed"


def test_someone_who_is_not_an_institute_admin_is_refused(as_admin):
    as_admin((None, None, ("Institute profile not found", 400)))
    with pytest.raises(HTTPException) as error:
        run(router.preview_students(file=upload(b"x"), identity={}, db=make_db()))
    assert error.value.status_code == 400
    with pytest.raises(HTTPException):
        run(router.get_bulk_update("x", identity={}, db=make_db()))


def test_a_bad_file_is_a_400_with_the_reason_not_a_server_error(as_admin):
    as_admin(({"_id": ADMIN}, INSTITUTE, None))
    with pytest.raises(HTTPException) as error:
        run(router.preview_students(file=upload(b"hello", "notes.txt"), identity={}, db=make_db()))
    assert error.value.status_code == 400 and "xlsx or .csv" in error.value.detail


def test_the_get_route_returns_only_the_callers_own_preview(as_admin):
    db = make_db(student("E1"))
    as_admin(({"_id": ADMIN}, INSTITUTE, None))
    view = run(router.preview_students(file=upload(xlsx([HEADER, ["E1", "New", "", "", "", ""]])), identity={}, db=db))
    assert run(router.get_bulk_update(view["batch_id"], identity={}, db=db))["batch_id"] == view["batch_id"]
    as_admin(({"_id": ADMIN}, OTHER_INSTITUTE, None))
    with pytest.raises(HTTPException) as error:
        run(router.get_bulk_update(view["batch_id"], identity={}, db=db))
    assert error.value.status_code == 404
