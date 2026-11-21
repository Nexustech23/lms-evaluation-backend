"""Bulk update, phase 4: the history and the undo. A fake database answers; no real database, no network, no AI.
Things that must hold: an undo restores exactly the old values, all students or none, only while nothing has changed
since, only for a limited time, and a second undo does nothing."""
import asyncio
import copy
from datetime import timedelta

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.api.routers import bulk_update as router
from app.core.config import settings
from app.schemas.bulk_update import BulkUpdateApplyRequest
from app.services import bulk_update_preview as bp
from app.services import bulk_update_undo as bu_undo
from tests.test_bulk_update_apply import (
    ADMIN, HEADER, INSTITUTE, OTHER, T0, Db, account, apply, batch, make_run, run, setup, student, xlsx,
)

APPLIED_AT = T0 + timedelta(hours=1)
UNDO_AT = APPLIED_AT + timedelta(days=1)


def undo(db, batch_id, reason="Applied the wrong file", now=UNDO_AT, institute=INSTITUTE, runner=None):
    return run(bu_undo.undo_batch(db, institute, ADMIN, batch_id, reason, now, run=runner or make_run(db)))


def applied(students, users, rows):
    db, bid = setup(students, users, rows)
    apply(db, bid, now=APPLIED_AT)
    return db, bid


def state(db):
    return copy.deepcopy({name: col.docs for name, col in db.c.items()})


# ---- undoing ----------------------------------------------------------------------------------------------------------

def test_an_undo_puts_the_old_values_back_on_the_students_and_their_accounts():
    a1, a2 = account(), account(fullName="Ravi", phone="9000000001")
    s1, s2 = student("E1", a1), student("E2", a2, name="Ravi", contact_no="9000000001")
    original = copy.deepcopy([s1, s2]); original_accounts = copy.deepcopy([a1, a2])
    db, bid = applied([s1, s2], [a1, a2], [HEADER, ["E1", "Asha Verma", "", "", "98765 00000"], ["E2", "", "Ram Kumar", "", ""]])
    view = undo(db, bid)

    for now_doc, was in zip(db["studentDetails"].docs, original):
        assert {k: v for k, v in now_doc.items() if k != "updated_at"} == {k: v for k, v in was.items() if k != "updated_at"}
        assert now_doc["updated_at"] == UNDO_AT
    assert [{k: v for k, v in d.items() if k != "updated_at"} for d in db["users"].docs] == [
        {k: v for k, v in d.items() if k != "updated_at"} for d in original_accounts]
    assert view["status"] == "undone" and view["undone_count"] == 2 and view["already_undone"] is False and view["can_undo"] is False


def test_the_undo_is_recorded_with_who_when_and_why():
    db, bid = applied([student("E1")], [], [HEADER, ["E1", "New", "", "", ""]])
    undo(db, bid, reason="  Applied   the wrong file ")
    saved = batch(db)
    assert saved["status"] == "undone" and saved["undo_reason"] == "Applied the wrong file"
    assert saved["undone_by"] == ADMIN and saved["undone_at"] == UNDO_AT and saved["reason"] == "Corrected after verification"
    assert saved["changes"][0]["undone_version"] == UNDO_AT.isoformat()


def test_a_field_the_student_never_had_is_removed_again_not_left_empty():
    s = student("E1"); del s["father_name"]
    db, bid = applied([s], [], [HEADER, ["E1", "", "Mohan Rao", "", ""]])
    assert db["studentDetails"].docs[0]["father_name"] == "Mohan Rao"
    undo(db, bid)
    assert "father_name" not in db["studentDetails"].docs[0]


def test_a_student_not_in_the_file_is_untouched_by_the_undo():
    other = student("E9")
    db, bid = applied([student("E1"), other], [], [HEADER, ["E1", "New", "", "", ""]])
    before = copy.deepcopy(db["studentDetails"].docs[1])
    undo(db, bid)
    assert db["studentDetails"].docs[1] == before


def test_undoing_twice_changes_nothing_the_second_time():
    db, bid = applied([student("E1")], [], [HEADER, ["E1", "New", "", "", ""]])
    undo(db, bid)
    after_first = state(db)
    writes = db["studentDetails"].updates
    again = undo(db, bid, reason="Another reason", now=UNDO_AT + timedelta(hours=2))
    assert again["already_undone"] is True and again["status"] == "undone"
    assert state(db) == after_first and db["studentDetails"].updates == writes
    assert batch(db)["undo_reason"] == "Applied the wrong file"


def test_an_undone_update_cannot_be_applied_again_but_the_file_can_be_previewed_afresh():
    db, bid = applied([student("E1")], [], [HEADER, ["E1", "New", "", "", ""]])
    undo(db, bid)
    with pytest.raises(HTTPException) as error:
        apply(db, bid, now=UNDO_AT)
    assert error.value.status_code == 409
    fresh = run(bp.create_preview(db, INSTITUTE, ADMIN, xlsx([HEADER, ["E1", "New", "", "", ""]]), "s.xlsx", UNDO_AT))
    assert fresh["counts"]["to_update"] == 1 and not any("already applied" in w for w in fresh["warnings"])


# ---- when an undo is refused -------------------------------------------------------------------------------------------

def test_if_any_student_changed_since_the_update_nothing_is_undone():
    db, bid = applied([student("E1"), student("E2")], [], [HEADER, ["E1", "New 1", "", "", ""], ["E2", "New 2", "", "", ""]])
    db["studentDetails"].docs[1]["updated_at"] = APPLIED_AT + timedelta(hours=3)          # someone edited E2 afterwards
    before = state(db)
    with pytest.raises(HTTPException) as error:
        undo(db, bid)
    assert error.value.status_code == 409 and [r["enrollment_no"] for r in error.value.detail["rows"]] == ["E2"]
    assert state(db) == before and batch(db)["status"] == "applied"      # E1 was fine, but is not undone either


def test_if_a_login_account_was_edited_since_the_update_nothing_is_undone():
    a = account()
    db, bid = applied([student("E1", a)], [a], [HEADER, ["E1", "New Name", "", "", ""]])
    db["users"].docs[0]["fullName"] = "Edited By Student"
    before = state(db)
    with pytest.raises(HTTPException) as error:
        undo(db, bid)
    assert error.value.status_code == 409 and "login account" in error.value.detail["rows"][0]["reason"]
    assert state(db) == before


def test_a_student_removed_since_the_update_stops_the_undo():
    db, bid = applied([student("E1")], [], [HEADER, ["E1", "New", "", "", ""]])
    db["studentDetails"].docs[0]["is_deleted"] = True
    with pytest.raises(HTTPException) as error:
        undo(db, bid)
    assert error.value.status_code == 409 and "no longer exists" in error.value.detail["rows"][0]["reason"]


def test_a_login_account_that_is_gone_does_not_block_the_undo_of_the_student():
    ghost = account()
    db, bid = applied([student("E1", ghost)], [account(_id=ghost["_id"])], [HEADER, ["E1", "New Name", "", "", ""]])
    db["users"].docs.clear()
    undo(db, bid)
    assert db["studentDetails"].docs[0]["name"] == "Asha Rao"


def test_the_undo_has_a_time_limit_and_the_last_moment_still_counts():
    db, bid = applied([student("E1")], [], [HEADER, ["E1", "New", "", "", ""]])
    limit = APPLIED_AT + timedelta(days=settings.BULK_UPDATE_UNDO_DAYS)
    with pytest.raises(HTTPException) as error:
        undo(db, bid, now=limit + timedelta(minutes=1))
    assert error.value.status_code == 409 and "passed" in error.value.detail["message"]
    assert db["studentDetails"].docs[0]["name"] == "New"
    undo(db, bid, now=limit)                                       # exactly at the limit is still allowed
    assert db["studentDetails"].docs[0]["name"] == "Asha Rao"


@pytest.mark.parametrize("reason", [None, "", "  ", "ab", "x" * 501])
def test_an_undo_needs_a_proper_reason(reason):
    db, bid = applied([student("E1")], [], [HEADER, ["E1", "New", "", "", ""]])
    before = state(db)
    with pytest.raises(HTTPException) as error:
        undo(db, bid, reason=reason)
    assert error.value.status_code == 400 and state(db) == before


@pytest.mark.parametrize("status", ["previewed", "expired", "applying", ""])
def test_only_an_applied_update_can_be_undone(status):
    db, bid = setup([student("E1")], [], [HEADER, ["E1", "New", "", "", ""]])
    db["bulkUpdateBatches"].docs[0]["status"] = status
    with pytest.raises(HTTPException) as error:
        undo(db, bid, now=T0 + timedelta(minutes=5))
    assert error.value.status_code == 409


def test_a_failure_halfway_through_an_undo_puts_everything_back_to_the_applied_state():
    a1, a2 = account(), account()
    db, bid = applied([student("E1", a1), student("E2", a2)], [a1, a2], [HEADER, ["E1", "New 1", "", "", ""], ["E2", "New 2", "", "", ""]])
    before = state(db)
    db["studentDetails"].fail_on_update = db["studentDetails"].updates + 2          # the second student's restore fails
    with pytest.raises(RuntimeError):
        undo(db, bid)
    assert state(db) == before and batch(db)["status"] == "applied"


def test_two_undos_racing_for_the_same_update_only_one_wins():
    db, bid = applied([student("E1")], [], [HEADER, ["E1", "New", "", "", ""]])
    runner = make_run(db)

    async def racing(_db, operation):
        db["bulkUpdateBatches"].docs[0]["status"] = "undone"        # the other request got there first
        return await runner(_db, operation)

    with pytest.raises(HTTPException) as error:
        undo(db, bid, runner=racing)
    assert error.value.status_code == 409 and "already undone" in error.value.detail["message"]
    assert db["studentDetails"].docs[0]["name"] == "New"            # rolled back to how the racing request left it


def test_another_institute_cannot_undo_and_bad_ids_are_not_found():
    db, bid = applied([student("E1")], [], [HEADER, ["E1", "New", "", "", ""]])
    for institute, ident in [(OTHER, bid), (INSTITUTE, "nope"), (INSTITUTE, str(ObjectId()))]:
        with pytest.raises(HTTPException) as error:
            undo(db, ident, institute=institute)
        assert error.value.status_code == 404
    assert db["studentDetails"].docs[0]["name"] == "New"


# ---- the history -------------------------------------------------------------------------------------------------------

def make_history():
    db = Db(studentDetails=[student("E1"), student("E2"), student("E3")], users=[], bulkUpdateBatches=[])
    ids = {}
    for key, name, when in [("E1", "First", T0), ("E2", "Second", T0 + timedelta(days=1)), ("E3", "Third", T0 + timedelta(days=2))]:
        view = run(bp.create_preview(db, INSTITUTE, ADMIN, xlsx([HEADER, [key, name, "", "", ""]]), f"{name}.xlsx", when))
        ids[name] = view["batch_id"]
    apply(db, ids["First"], now=T0 + timedelta(hours=2))
    apply(db, ids["Second"], now=T0 + timedelta(days=1, hours=2))
    undo(db, ids["Second"], now=T0 + timedelta(days=2))
    return db, ids


def history(db, limit=50, now=T0 + timedelta(days=3), institute=INSTITUTE):
    return run(bu_undo.list_history(db, institute, limit, now))


def test_the_history_lists_every_update_newest_first_with_its_state():
    db, ids = make_history()
    items = history(db)["items"]
    assert [i["file_name"] for i in items] == ["Third.xlsx", "Second.xlsx", "First.xlsx"]
    third, second, first = items
    assert third["status"] == "previewed" and third["can_undo"] is False and third["applied_at"] is None
    assert second["status"] == "undone" and second["undo_reason"] == "Applied the wrong file" and second["can_undo"] is False
    assert first["status"] == "applied" and first["reason"] == "Corrected after verification" and first["can_undo"] is True
    assert first["counts"]["to_update"] == 1 and first["undo_until"].startswith("2026-11-28")


def test_the_history_marks_an_old_preview_expired_and_an_old_update_as_no_longer_undoable():
    db, ids = make_history()
    far = T0 + timedelta(days=60)
    items = {i["file_name"]: i for i in history(db, now=far)["items"]}
    assert items["Third.xlsx"]["status"] == "expired" and items["First.xlsx"]["can_undo"] is False
    assert items["First.xlsx"]["status"] == "applied"


def test_the_history_is_short_summaries_not_the_long_lists():
    db, _ = make_history()
    assert all("changes" not in item and "rejected" not in item for item in history(db)["items"])


def test_the_history_only_shows_this_institutes_own_updates_and_obeys_the_limit():
    db, _ = make_history()
    assert history(db, institute=OTHER)["items"] == []
    assert len(history(db, limit=2)["items"]) == 2
    assert history(db, limit=0)["limit"] == 1 and history(db, limit=500)["limit"] == 100


def test_the_batch_view_shows_whether_it_can_still_be_undone():
    db, ids = make_history()
    first = run(bp.get_batch(db, INSTITUTE, ids["First"], T0 + timedelta(days=3)))
    assert first["can_undo"] is True and first["applied_by"] == str(ADMIN) and "undo_until" in first
    second = run(bp.get_batch(db, INSTITUTE, ids["Second"], T0 + timedelta(days=3)))
    assert second["status"] == "undone" and second["undo_reason"] and second["can_undo"] is False


# ---- the routes --------------------------------------------------------------------------------------------------------

@pytest.fixture
def as_admin(monkeypatch):
    def pick(result):
        async def fake(identity, db):
            return result
        monkeypatch.setattr(router, "get_current_user_and_institute", fake)
    return pick


def test_the_word_history_is_not_read_as_an_update_id():
    paths = [route.path for route in router.router.routes if "GET" in getattr(route, "methods", ())]
    assert paths.index("/bulk-update/history") < paths.index("/bulk-update/{batch_id}")


def test_the_history_route_returns_the_callers_list(as_admin, monkeypatch):
    as_admin(({"_id": ADMIN}, INSTITUTE, None))
    seen = {}

    async def fake_list(db, institute_id, limit):
        seen.update(institute=institute_id, limit=limit)
        return {"items": [], "limit": limit}

    monkeypatch.setattr(router, "list_history", fake_list)
    assert run(router.bulk_update_history(limit=20, identity={}, db=None)) == {"items": [], "limit": 20}
    assert seen == {"institute": INSTITUTE, "limit": 20}


def test_the_undo_route_passes_the_reason(as_admin, monkeypatch):
    as_admin(({"_id": ADMIN}, INSTITUTE, None))
    seen = {}

    async def fake_undo(db, institute_id, user_id, batch_id, reason):
        seen.update(institute=institute_id, user=user_id, batch=batch_id, reason=reason)
        return {"ok": True}

    monkeypatch.setattr(router, "undo_batch", fake_undo)
    assert run(router.undo_bulk_update("abc", BulkUpdateApplyRequest(reason="Because"), identity={}, db=None)) == {"ok": True}
    assert seen == {"institute": INSTITUTE, "user": ADMIN, "batch": "abc", "reason": "Because"}


def test_the_history_and_undo_routes_refuse_someone_who_is_not_an_institute_admin(as_admin):
    as_admin((None, None, ("Institute profile not found", 400)))
    with pytest.raises(HTTPException) as error:
        run(router.bulk_update_history(limit=10, identity={}, db=None))
    assert error.value.status_code == 400
    with pytest.raises(HTTPException):
        run(router.undo_bulk_update("abc", BulkUpdateApplyRequest(reason="Because"), identity={}, db=None))


# ---- phase 6: a stored change to a field that may never change is refused -----------------------------------------------

def test_a_stored_change_to_a_forbidden_field_is_never_applied_or_undone():
    a = account()
    s = student("E1", a)
    db, bid = setup([s], [a], [["Enrollment No", "Name"], ["E1", "New Name"]])
    batch(db)["changes"][0]["changes"]["roll_no"] = {"old": "7", "new": "99"}
    before = state(db)
    with pytest.raises(HTTPException) as error:
        apply(db, bid, now=APPLIED_AT)
    assert error.value.status_code == 409 and state(db) == before


def test_an_undo_refuses_a_stored_change_to_a_forbidden_field():
    a = account()
    db, bid = applied([student("E1", a)], [a], [["Enrollment No", "Name"], ["E1", "New Name"]])
    batch(db)["changes"][0]["changes"]["roll_no"] = {"old": "7", "new": "99"}
    before = state(db)
    with pytest.raises(HTTPException) as error:
        undo(db, bid)
    assert error.value.status_code == 409 and state(db) == before
