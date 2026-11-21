"""Marks import: the old marks are replaced by the new ones as one all-or-nothing step.
A fake database answers; no real database, no network, no AI. The request and the response must stay exactly as they were."""
import asyncio
import copy
import io

import pytest
from bson import ObjectId
from fastapi import HTTPException, UploadFile
from openpyxl import Workbook

from app.api.routers import marks_import as mi

INSTITUTE, OTHER_INSTITUTE = ObjectId(), ObjectId()
BATCH, OTHER_BATCH = ObjectId(), ObjectId()


def run(coro):
    return asyncio.run(coro)


def _matches(doc, query):
    return all(doc.get(k) == v for k, v in query.items())


class Collection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.sessions = []                       # the session each write was given
        self.fail_on_insert = False

    async def delete_many(self, query, session=None):
        self.sessions.append(("delete", session))
        self.docs = [d for d in self.docs if not _matches(d, query)]

    async def insert_many(self, documents, session=None):
        self.sessions.append(("insert", session))
        if self.fail_on_insert:
            raise RuntimeError("the database went away")
        self.docs.extend(copy.deepcopy(list(documents)))


class Db:
    def __init__(self):
        self.c = {"importedMarks": Collection()}

    def __getitem__(self, name):
        return self.c.setdefault(name, Collection())


SESSION = object()


def make_run(db, session=SESSION):
    """Stands in for the real all-or-nothing step: on any error every collection goes back to how it was."""
    async def runner(_db, operation):
        snapshot = {name: copy.deepcopy(col.docs) for name, col in db.c.items()}
        try:
            return await operation(session)
        except BaseException:
            for name, docs in snapshot.items():
                db.c[name].docs = docs
            raise
    return runner


def mark(name, marks, institute=INSTITUTE, batch=BATCH, semester=1):
    return {"institute_id": institute, "batch_id": batch, "semester": semester, "student_name": name, "marks": marks}


def seeded():
    db = Db()
    db["importedMarks"].docs = [mark("Old A", 50), mark("Old B", 60),                      # the batch being replaced
                                mark("Other sem", 70, semester=2), mark("Other batch", 71, batch=OTHER_BATCH),
                                mark("Other college", 72, institute=OTHER_INSTITUTE)]
    return db


def workbook(rows, header=("Student Name", "Marks")):
    book = Workbook()
    book.active.append(list(header))
    for row in rows:
        book.active.append(list(row))
    buffer = io.BytesIO(); book.save(buffer)
    return buffer.getvalue()


# ---- the swap itself ---------------------------------------------------------------------------------------------------

def test_the_batches_old_marks_are_replaced_and_nothing_else_moves():
    db = seeded()
    new = [mark("New A", 80), mark("New B", 90)]
    run(mi.replace_imported_marks(db, INSTITUTE, BATCH, 1, new, run=make_run(db)))
    names = sorted(d["student_name"] for d in db["importedMarks"].docs)
    assert names == ["New A", "New B", "Other batch", "Other college", "Other sem"]


def test_if_saving_the_new_marks_fails_the_old_marks_are_still_there():
    db = seeded()
    before = copy.deepcopy(db["importedMarks"].docs)
    db["importedMarks"].fail_on_insert = True
    with pytest.raises(RuntimeError):
        run(mi.replace_imported_marks(db, INSTITUTE, BATCH, 1, [mark("New", 80)], run=make_run(db)))
    assert db["importedMarks"].docs == before                      # before this change the batch would now have had none


def test_both_writes_are_made_inside_the_same_protected_step():
    db = seeded()
    run(mi.replace_imported_marks(db, INSTITUTE, BATCH, 1, [mark("New", 80)], run=make_run(db)))
    assert db["importedMarks"].sessions == [("delete", SESSION), ("insert", SESSION)]


def test_the_real_all_or_nothing_step_is_used_unless_another_is_given(monkeypatch):
    db = seeded()
    seen = {}

    async def fake_transaction(db_, operation):
        seen["called"] = True
        return await make_run(db)(db_, operation)

    monkeypatch.setattr(mi, "transaction", fake_transaction)
    run(mi.replace_imported_marks(db, INSTITUTE, BATCH, 1, [mark("New", 80)]))
    assert seen == {"called": True} and "New" in [d["student_name"] for d in db["importedMarks"].docs]


# ---- the route: the same request and the same response as before ------------------------------------------------------

@pytest.fixture
def route(monkeypatch):
    db = seeded()

    async def institute(identity, db_):
        return INSTITUTE

    monkeypatch.setattr(mi, "resolve_current_institute_id", institute)
    monkeypatch.setattr(mi, "transaction", make_run(db))

    def call(data, batch_id=str(BATCH), semester=1, name="marks.xlsx"):
        return run(mi.import_marks_excel(file=UploadFile(filename=name, file=io.BytesIO(data)), batch_id=batch_id,
                                         semester=semester, relative_grading_id=None, identity={}, db=db))
    return db, call


def test_the_response_has_the_same_shape_and_wording_as_before(route):
    db, call = route
    out = call(workbook([("Asha", 81), ("Ravi", 72.5)]))
    assert out == {"success": True, "message": "2 students processed successfully.", "processed_count": 2}


def test_the_new_marks_are_stored_the_way_they_always_were(route):
    db, call = route
    call(workbook([("Asha", 81)]))
    saved = [d for d in db["importedMarks"].docs if d["student_name"] == "Asha"]
    assert len(saved) == 1 and {k: saved[0][k] for k in ("institute_id", "batch_id", "semester", "marks")} == {
        "institute_id": INSTITUTE, "batch_id": BATCH, "semester": 1, "marks": 81.0} and "uploaded_at" in saved[0]


def test_a_second_upload_still_replaces_the_batch_as_before(route):
    db, call = route
    call(workbook([("Asha", 81), ("Ravi", 72)]))
    call(workbook([("Meena", 66)]))
    ours = [d["student_name"] for d in db["importedMarks"].docs
            if d["institute_id"] == INSTITUTE and d["batch_id"] == BATCH and d["semester"] == 1]
    assert ours == ["Meena"]
    assert "Other college" in [d["student_name"] for d in db["importedMarks"].docs]     # another college's marks are never touched


@pytest.mark.parametrize("rows,part", [([("Asha", "abc")], "must be a number"), ([("Asha", 101)], "between 0 and 100"),
                                       ([("Asha", -1)], "between 0 and 100"), ([], "does not contain any valid marks")])
def test_a_bad_file_is_refused_before_anything_is_touched(route, rows, part):
    db, call = route
    before = copy.deepcopy(db["importedMarks"].docs)
    with pytest.raises(HTTPException) as error:
        call(workbook(rows))
    assert error.value.status_code == 400 and part in error.value.detail
    assert db["importedMarks"].docs == before and db["importedMarks"].sessions == []     # no write was even attempted


def test_a_file_without_a_marks_column_is_refused_with_the_same_message(route):
    db, call = route
    with pytest.raises(HTTPException) as error:
        call(workbook([("Asha", 81)], header=("Student Name", "Comment")))
    assert error.value.status_code == 400 and "Could not find a marks column" in error.value.detail


def test_a_bad_batch_id_is_still_a_400(route):
    db, call = route
    with pytest.raises(HTTPException) as error:
        call(workbook([("Asha", 81)]), batch_id="nope")
    assert error.value.status_code == 400 and error.value.detail == "Valid batch_id is required"
    assert db["importedMarks"].sessions == []


def test_a_failure_while_saving_through_the_route_leaves_the_old_marks(route):
    db, call = route
    before = copy.deepcopy(db["importedMarks"].docs)
    db["importedMarks"].fail_on_insert = True
    with pytest.raises(RuntimeError):
        call(workbook([("Asha", 81)]))
    assert db["importedMarks"].docs == before
