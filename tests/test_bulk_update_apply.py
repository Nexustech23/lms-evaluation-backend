"""Bulk update, phase 3: applying a preview. A fake database answers; no real database, no network, no AI.
The things that must hold: all students change or none, a reason is needed, a changed student stops it,
and applying twice does nothing the second time."""
import asyncio
import copy
import io
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException
from openpyxl import Workbook

from app.api.routers import bulk_update as router
from app.core.config import settings
from app.schemas.bulk_update import BulkUpdateApplyRequest
from app.services import bulk_update_apply as ba
from app.services import bulk_update_preview as bp

T0 = datetime(2026, 11, 21, 9, 0, 0, tzinfo=timezone.utc)
INSTITUTE, OTHER, ADMIN = ObjectId(), ObjectId(), ObjectId()
HEADER = ["Enrollment No", "Name", "Father's Name", "Date of Birth", "Contact No"]


def run(coro):
    return asyncio.run(coro)


# ---- a fake database that can update, and a fake all-or-nothing step -------------------------------------------------

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


class Result:
    def __init__(self, matched):
        self.matched_count = matched
        self.modified_count = matched


class Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.updates = 0
        self.fail_on_update = None             # make the Nth update blow up, to test the all-or-nothing rule

    async def find_one(self, query, projection=None, session=None):
        return next((copy.deepcopy(d) for d in self.docs if _matches(d, query)), None)

    def find(self, query=None, projection=None, session=None):
        return Cursor(d for d in self.docs if _matches(d, query or {}))

    async def insert_one(self, doc):
        doc = copy.deepcopy(doc)
        doc.setdefault("_id", ObjectId())
        self.docs.append(doc)
        return type("R", (), {"inserted_id": doc["_id"]})()

    async def update_one(self, query, update, upsert=False, session=None):
        self.updates += 1
        if self.fail_on_update == self.updates:
            raise RuntimeError("the database went away")
        doc = next((d for d in self.docs if _matches(d, query)), None)
        if doc is None:
            return Result(0)
        doc.update(update.get("$set", {}))
        for name in update.get("$unset", {}):
            doc.pop(name, None)
        return Result(1)


class Db:
    def __init__(self, **collections):
        self.c = {name: Collection(docs) for name, docs in collections.items()}

    def __getitem__(self, name):
        return self.c.setdefault(name, Collection())


def make_run(db):
    """Stands in for the real all-or-nothing step: on any error every collection goes back to how it was."""
    async def runner(_db, operation):
        snapshot = {name: copy.deepcopy(col.docs) for name, col in db.c.items()}
        try:
            return await operation(None)
        except BaseException:
            for name, docs in snapshot.items():
                db.c[name].docs = docs
            raise
    return runner


def account(**extra):
    base = {"_id": ObjectId(), "fullName": "Asha Rao", "phone": "9876543210", "role": 4, "email": "asha@college.test"}
    base.update(extra)
    return base


def student(key, user=None, institute=INSTITUTE, **extra):
    base = {"_id": ObjectId(), "institute_id": institute, "enrollment_no": key, "name": "Asha Rao", "father_name": "Mohan Rao",
            "dob": "2004-05-06", "contact_no": "9876543210", "roll_no": "7", "user_id": user["_id"] if user else None,
            "updated_at": datetime(2026, 11, 1, 10, 0, 0), "is_deleted": False}
    base.update(extra)
    return base


def xlsx(rows):
    book = Workbook()
    for row in rows:
        book.active.append(row)
    buffer = io.BytesIO(); book.save(buffer)
    return buffer.getvalue()


def setup(students, users=(), rows=None):
    db = Db(studentDetails=list(students), users=list(users), bulkUpdateBatches=[])
    view = run(bp.create_preview(db, INSTITUTE, ADMIN, xlsx(rows), "s.xlsx", T0))
    return db, view["batch_id"]


def apply(db, batch_id, reason="Corrected after verification", skip=False, now=T0 + timedelta(hours=1), institute=INSTITUTE):
    return run(ba.apply_batch(db, institute, ADMIN, batch_id, reason, skip, now, run=make_run(db)))


def batch(db):
    return db["bulkUpdateBatches"].docs[0]


# ---- the happy path ----------------------------------------------------------------------------------------------------

def test_a_preview_is_applied_to_the_students_and_their_login_accounts():
    a1, a2 = account(), account(fullName="Ravi", phone="9000000001")
    s1, s2 = student("E1", a1), student("E2", a2, name="Ravi", contact_no="9000000001")
    db, bid = setup([s1, s2], [a1, a2], [HEADER, ["E1", "Asha Verma", "", "", "98765 00000"], ["E2", "", "Ram Kumar", "", ""]])
    now = T0 + timedelta(hours=1)
    view = apply(db, bid, now=now)

    first = next(d for d in db["studentDetails"].docs if d["enrollment_no"] == "E1")
    second = next(d for d in db["studentDetails"].docs if d["enrollment_no"] == "E2")
    assert (first["name"], first["contact_no"], first["updated_at"]) == ("Asha Verma", "9876500000", now)
    assert second["father_name"] == "Ram Kumar" and second["name"] == "Ravi"
    login1 = next(d for d in db["users"].docs if d["_id"] == a1["_id"])
    assert (login1["fullName"], login1["phone"]) == ("Asha Verma", "9876500000")            # the login account follows
    login2 = next(d for d in db["users"].docs if d["_id"] == a2["_id"])
    assert (login2["fullName"], login2["phone"]) == ("Ravi", "9000000001")                  # a father's name is not on the login
    assert view["status"] == "applied" and view["applied_count"] == 2 and view["already_applied"] is False


def test_only_the_changed_fields_are_written_and_nothing_else_moves():
    a = account()
    s, other = student("E1", a), student("E9")
    db, bid = setup([s, other], [a], [HEADER, ["E1", "New Name", "", "", ""]])
    before_other = copy.deepcopy(db["studentDetails"].docs[1])
    apply(db, bid)
    changed = db["studentDetails"].docs[0]
    expected = {**s, "name": "New Name", "updated_at": T0 + timedelta(hours=1)}
    assert changed == expected                                    # roll_no, dob, father's name, ids, institute: untouched
    assert db["studentDetails"].docs[1] == before_other           # a student not in the file is untouched
    login = db["users"].docs[0]
    assert login["email"] == a["email"] and login["role"] == 4 and login["phone"] == a["phone"]


def test_the_batch_keeps_the_audit_record_with_old_values_for_an_undo():
    a = account()
    db, bid = setup([student("E1", a)], [a], [HEADER, ["E1", "New Name", "", "", "9000011111"]])
    apply(db, bid, reason="  Fixed   spelling  ")
    saved = batch(db)
    assert saved["status"] == "applied" and saved["reason"] == "Fixed spelling" and saved["applied_by"] == ADMIN
    assert saved["applied_at"] == T0 + timedelta(hours=1)
    row = saved["changes"][0]
    assert row["changes"]["name"]["old"] == "Asha Rao" and row["changes"]["name"]["new"] == "New Name"
    assert row["user_before"] == {"fullName": "Asha Rao", "phone": "9876543210"}
    assert row["applied_version"] == (T0 + timedelta(hours=1)).isoformat()


def test_a_student_without_a_login_account_still_updates():
    db, bid = setup([student("E1", None)], [], [HEADER, ["E1", "New Name", "", "", ""]])
    apply(db, bid)
    assert db["studentDetails"].docs[0]["name"] == "New Name" and db["users"].updates == 0
    assert batch(db)["changes"][0]["user_before"] is None


def test_a_missing_login_account_is_skipped_not_an_error():
    ghost = account()
    db, bid = setup([student("E1", ghost)], [], [HEADER, ["E1", "New Name", "", "", ""]])      # account not in users
    apply(db, bid)
    assert db["studentDetails"].docs[0]["name"] == "New Name"


def test_a_change_that_is_only_a_father_name_does_not_touch_the_login_account():
    a = account()
    db, bid = setup([student("E1", a)], [a], [HEADER, ["E1", "", "New Father", "", ""]])
    apply(db, bid)
    assert db["users"].updates == 0


# ---- a reason, and rows with problems ----------------------------------------------------------------------------------

@pytest.mark.parametrize("reason", [None, "", "   ", "ab", "x" * 501])
def test_without_a_proper_reason_nothing_is_applied(reason):
    db, bid = setup([student("E1")], [], [HEADER, ["E1", "New Name", "", "", ""]])
    before = copy.deepcopy(db["studentDetails"].docs)
    with pytest.raises(HTTPException) as error:
        apply(db, bid, reason=reason)
    assert error.value.status_code == 400
    assert db["studentDetails"].docs == before and batch(db)["status"] == "previewed"


def test_rows_with_problems_stop_the_update_unless_the_admin_confirms_skipping_them():
    db, bid = setup([student("E1"), student("E2")], [], [HEADER, ["E1", "New", "", "", ""], ["NOPE", "Ghost", "", "", ""]])
    before = copy.deepcopy(db["studentDetails"].docs)
    with pytest.raises(HTTPException) as error:
        apply(db, bid)
    assert error.value.status_code == 409 and "skipped" in error.value.detail["message"]
    assert db["studentDetails"].docs == before
    view = apply(db, bid, skip=True)
    assert view["applied_count"] == 1 and view["skipped_count"] == 1
    assert db["studentDetails"].docs[0]["name"] == "New" and batch(db)["skip_rejected"] is True


def test_a_preview_with_nothing_to_change_is_refused():
    db, bid = setup([student("E1")], [], [HEADER, ["E1", "Asha Rao", "", "", ""]])
    with pytest.raises(HTTPException) as error:
        apply(db, bid)
    assert error.value.status_code == 400 and "nothing to apply" in error.value.detail


# ---- a student changed since the preview -------------------------------------------------------------------------------

def test_if_any_student_changed_after_the_preview_nothing_at_all_is_applied():
    s1, s2 = student("E1"), student("E2")
    db, bid = setup([s1, s2], [], [HEADER, ["E1", "New 1", "", "", ""], ["E2", "New 2", "", "", ""]])
    db["studentDetails"].docs[1]["updated_at"] = datetime(2026, 11, 21, 8, 30, 0)          # someone edited E2 meanwhile
    before = copy.deepcopy(db["studentDetails"].docs)
    with pytest.raises(HTTPException) as error:
        apply(db, bid)
    detail = error.value.detail
    assert error.value.status_code == 409 and [r["enrollment_no"] for r in detail["rows"]] == ["E2"]
    assert db["studentDetails"].docs == before                    # E1 was fine, but is not applied either
    assert batch(db)["status"] == "previewed"


def test_a_student_removed_after_the_preview_stops_the_update():
    db, bid = setup([student("E1")], [], [HEADER, ["E1", "New", "", "", ""]])
    db["studentDetails"].docs[0]["is_deleted"] = True
    with pytest.raises(HTTPException) as error:
        apply(db, bid)
    assert error.value.status_code == 409 and "no longer exists" in error.value.detail["rows"][0]["reason"]


def test_a_failure_halfway_puts_everything_back():
    a1, a2, a3 = account(), account(), account()
    students = [student("E1", a1), student("E2", a2), student("E3", a3)]
    db, bid = setup(students, [a1, a2, a3], [HEADER, ["E1", "New 1", "", "", ""], ["E2", "New 2", "", "", ""], ["E3", "New 3", "", "", ""]])
    before = copy.deepcopy({n: c.docs for n, c in db.c.items()})
    db["studentDetails"].fail_on_update = 3                       # the third student's update fails
    with pytest.raises(RuntimeError):
        apply(db, bid)
    assert {n: c.docs for n, c in db.c.items()} == before        # students, accounts and the batch: all as they were
    assert batch(db)["status"] == "previewed"


def test_a_failure_while_saving_the_login_account_also_puts_everything_back():
    a = account()
    db, bid = setup([student("E1", a)], [a], [HEADER, ["E1", "New", "", "", ""]])
    before = copy.deepcopy({n: c.docs for n, c in db.c.items()})
    db["users"].fail_on_update = 1
    with pytest.raises(RuntimeError):
        apply(db, bid)
    assert {n: c.docs for n, c in db.c.items()} == before


# ---- applying twice, expiry, and who may -------------------------------------------------------------------------------

def test_applying_the_same_preview_twice_changes_nothing_the_second_time():
    db, bid = setup([student("E1")], [], [HEADER, ["E1", "New", "", "", ""]])
    apply(db, bid)
    writes = db["studentDetails"].updates
    snapshot = copy.deepcopy(db["studentDetails"].docs)
    again = apply(db, bid, reason="A different reason", now=T0 + timedelta(hours=5))
    assert again["already_applied"] is True and again["status"] == "applied"
    assert db["studentDetails"].updates == writes and db["studentDetails"].docs == snapshot
    assert batch(db)["reason"] == "Corrected after verification"


def test_two_applies_racing_for_the_same_batch_only_one_wins():
    db, bid = setup([student("E1")], [], [HEADER, ["E1", "New", "", "", ""]])
    runner = make_run(db)

    async def racing(_db, operation):
        db["bulkUpdateBatches"].docs[0]["status"] = "applied"      # the other request got there first
        return await runner(_db, operation)

    with pytest.raises(HTTPException) as error:
        run(ba.apply_batch(db, INSTITUTE, ADMIN, bid, "A good reason", False, T0 + timedelta(hours=1), run=racing))
    assert error.value.status_code == 409 and "already applied" in error.value.detail["message"]
    assert db["studentDetails"].docs[0]["name"] == "Asha Rao"      # rolled back


def test_an_expired_preview_cannot_be_applied():
    db, bid = setup([student("E1")], [], [HEADER, ["E1", "New", "", "", ""]])
    late = T0 + timedelta(hours=settings.BULK_UPDATE_PREVIEW_HOURS, minutes=1)
    with pytest.raises(HTTPException) as error:
        apply(db, bid, now=late)
    assert error.value.status_code == 409 and "expired" in error.value.detail["message"]
    assert db["studentDetails"].docs[0]["name"] == "Asha Rao"


@pytest.mark.parametrize("status", ["undone", "applying", ""])
def test_a_batch_in_another_state_cannot_be_applied(status):
    db, bid = setup([student("E1")], [], [HEADER, ["E1", "New", "", "", ""]])
    db["bulkUpdateBatches"].docs[0]["status"] = status
    with pytest.raises(HTTPException) as error:
        apply(db, bid)
    assert error.value.status_code == 409


def test_another_institute_cannot_apply_and_bad_ids_are_not_found():
    db, bid = setup([student("E1")], [], [HEADER, ["E1", "New", "", "", ""]])
    for institute, ident in [(OTHER, bid), (INSTITUTE, "nope"), (INSTITUTE, str(ObjectId()))]:
        with pytest.raises(HTTPException) as error:
            apply(db, ident, institute=institute)
        assert error.value.status_code == 404
    assert db["studentDetails"].docs[0]["name"] == "Asha Rao"


# ---- the route ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def as_admin(monkeypatch):
    def pick(result):
        async def fake(identity, db):
            return result
        monkeypatch.setattr(router, "get_current_user_and_institute", fake)
    return pick


def test_the_apply_route_passes_the_reason_and_the_skip_choice(as_admin, monkeypatch):
    as_admin(({"_id": ADMIN}, INSTITUTE, None))
    seen = {}

    async def fake_apply(db, institute_id, user_id, batch_id, reason, skip_rejected):
        seen.update(institute=institute_id, user=user_id, batch=batch_id, reason=reason, skip=skip_rejected)
        return {"ok": True}

    monkeypatch.setattr(router, "apply_batch", fake_apply)
    out = run(router.apply_bulk_update("abc", BulkUpdateApplyRequest(reason="Because", skip_rejected=True), identity={}, db=None))
    assert out == {"ok": True} and seen == {"institute": INSTITUTE, "user": ADMIN, "batch": "abc", "reason": "Because", "skip": True}


def test_the_apply_route_refuses_someone_who_is_not_an_institute_admin(as_admin):
    as_admin((None, None, ("Institute profile not found", 400)))
    with pytest.raises(HTTPException) as error:
        run(router.apply_bulk_update("abc", BulkUpdateApplyRequest(reason="Because"), identity={}, db=None))
    assert error.value.status_code == 400


def test_the_apply_request_defaults_to_not_skipping_anything():
    assert BulkUpdateApplyRequest().skip_rejected is False and BulkUpdateApplyRequest().reason == ""


def test_the_route_end_to_end_with_the_real_service(as_admin, monkeypatch):
    as_admin(({"_id": ADMIN}, INSTITUTE, None))
    db, bid = setup([student("E1")], [], [HEADER, ["E1", "New", "", "", ""]])
    monkeypatch.setattr(ba, "transaction", make_run(db))
    original = ba.apply_batch

    async def with_fake_step(db_, institute_id, user_id, batch_id, reason, skip_rejected):
        return await original(db_, institute_id, user_id, batch_id, reason, skip_rejected, T0 + timedelta(hours=1), run=make_run(db))

    monkeypatch.setattr(router, "apply_batch", with_fake_step)
    out = run(router.apply_bulk_update(bid, BulkUpdateApplyRequest(reason="Because"), identity={}, db=db))
    assert out["status"] == "applied" and db["studentDetails"].docs[0]["name"] == "New"
