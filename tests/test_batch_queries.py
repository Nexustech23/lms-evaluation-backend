"""Phase 4 — N+1 query loops replaced by batched $in / aggregation.

These endpoints must return byte-identical bodies to the per-row version:
same fields, same row order, None for a missing FK. Multi-row fixtures with
distinct foreign keys prove the batched maps line up.
"""
from datetime import datetime, timezone

from bson import ObjectId

from tests.conftest import login
from tests.test_security_fixes import (
    PASSWORD,
    _get_institute_id,
    _register_institute_admin,
    register,
)
from app.models.subject import create_subject_document


# ── /subjects/institute (populated) — 4 batched $in instead of 4 find_one/row ──

async def test_subjects_institute_enrichment_matches_per_row(superadmin_client, client_factory, test_db):
    admin = await _register_institute_admin(superadmin_client, client_factory, "Batch Inst")
    institute_id = await _get_institute_id(admin)
    inst_oid = ObjectId(institute_id)

    school_a = (await test_db["schoolDetails"].insert_one(
        {"school_name": "School A", "institute_id": inst_oid})).inserted_id
    school_b = (await test_db["schoolDetails"].insert_one(
        {"school_name": "School B", "institute_id": inst_oid})).inserted_id
    prog = (await test_db["programmeDetails"].insert_one(
        {"programme_name": "B.Tech", "institute_id": inst_oid, "school_id": school_a})).inserted_id
    dept = (await test_db["departmentDetails"].insert_one(
        {"department_name": "CSE", "institute_id": inst_oid, "programme_id": prog})).inserted_id
    batch = (await test_db["batchDetails"].insert_one(
        {"batch_name": "2021-25", "institute_id": inst_oid, "programme_id": prog})).inserted_id

    # subject 1: fully wired to school A
    await test_db["subjectDetails"].insert_one(create_subject_document({
        "institute_id": institute_id, "school_id": str(school_a), "programme_id": str(prog),
        "department_id": str(dept), "batch_id": str(batch), "subject_name": "Algorithms",
    }, str(ObjectId())))
    # subject 2: school B, and NO department -> department_name must be None
    await test_db["subjectDetails"].insert_one(create_subject_document({
        "institute_id": institute_id, "school_id": str(school_b), "programme_id": str(prog),
        "batch_id": str(batch), "subject_name": "Databases",
    }, str(ObjectId())))

    resp = await admin.get("/subjects/institute?limit=10")
    assert resp.status_code == 200
    rows = {r["subject_name"]: r for r in resp.json()["subjects"]}

    assert rows["Algorithms"]["school_name"] == "School A"
    assert rows["Algorithms"]["department_name"] == "CSE"
    assert rows["Algorithms"]["batch_name"] == "2021-25"
    assert rows["Algorithms"]["programme_name"] == "B.Tech"

    assert rows["Databases"]["school_name"] == "School B"
    assert rows["Databases"]["department_name"] is None
    assert rows["Databases"]["department_id"] is None
    assert rows["Databases"]["batch_name"] == "2021-25"


# ── /faculty/filter-data — 4 batched $in instead of nested find_one loops ──

async def test_faculty_filter_data_maps_all_referenced_rows(superadmin_client, client_factory, test_db):
    admin = await _register_institute_admin(superadmin_client, client_factory, "FF Inst")
    institute_id = await _get_institute_id(admin)
    faculty_email = f"ff-{ObjectId()}@test.local"
    await register(admin, role="faculty", fullName="FF Faculty", email=faculty_email,
                   password=PASSWORD, school_id=str(ObjectId()))
    faculty_doc = await test_db["facultyDetails"].find_one({})
    inst_oid = ObjectId(institute_id)

    s1 = (await test_db["schoolDetails"].insert_one({"school_name": "S1", "institute_id": inst_oid})).inserted_id
    s2 = (await test_db["schoolDetails"].insert_one({"school_name": "S2", "institute_id": inst_oid})).inserted_id
    p1 = (await test_db["programmeDetails"].insert_one({"programme_name": "P1", "institute_id": inst_oid})).inserted_id

    for name, sch in (("Sub One", s1), ("Sub Two", s2)):
        await test_db["subjectDetails"].insert_one(create_subject_document({
            "institute_id": institute_id, "school_id": str(sch), "programme_id": str(p1),
            "subject_name": name,
        }, str(ObjectId())))
        await test_db["subjectDetails"].update_one(
            {"subject_name": name}, {"$set": {"faculty_id": faculty_doc["_id"]}}
        )

    fc = await client_factory()
    await login(fc, faculty_email, PASSWORD)
    resp = await fc.get("/faculty/filter-data")
    assert resp.status_code == 200
    filters = resp.json()["filters"]
    assert sorted(s["school_name"] for s in filters["schools"]) == ["S1", "S2"]
    assert [p["programme_name"] for p in filters["programmes"]] == ["P1"]
    assert sorted(s["subject_name"] for s in filters["subjects"]) == ["Sub One", "Sub Two"]


# ── /newsaved-documents-subject/{id} — one $group instead of 2 counts/exam ──

async def test_exam_sheet_counts_via_aggregation(superadmin_client, test_db):
    subject_id = ObjectId()
    exam1 = (await test_db["newsavedDocs"].insert_one(
        {"subject_id": subject_id, "folder_name": "Midterm"})).inserted_id
    exam2 = (await test_db["newsavedDocs"].insert_one(
        {"subject_id": subject_id, "folder_name": "Final"})).inserted_id

    now = datetime.now(timezone.utc)
    await test_db["answerDetails"].insert_many([
        {"exam_id": exam1, "evaluated_at": now},
        {"exam_id": exam1, "evaluated_at": now},
        {"exam_id": exam1},                       # not evaluated
    ])
    # exam2: no sheets at all

    resp = await superadmin_client.get(f"/newsaved-documents-subject/{subject_id}")
    assert resp.status_code == 200
    by_name = {e["folder_name"]: e for e in resp.json()["exams"]}

    assert by_name["Midterm"]["total_sheets"] == 3
    assert by_name["Midterm"]["evaluated_sheets"] == 2
    assert by_name["Midterm"]["evaluation_progress"] == 66.67

    assert by_name["Final"]["total_sheets"] == 0
    assert by_name["Final"]["evaluated_sheets"] == 0
    assert by_name["Final"]["evaluation_progress"] == 0


# ── load_by_ids unit behaviour ──

async def test_load_by_ids_dedupes_coerces_and_filters(test_db):
    from app.utils.batch import load_by_ids

    a = (await test_db["schoolDetails"].insert_one({"school_name": "A"})).inserted_id
    b = (await test_db["schoolDetails"].insert_one({"school_name": "B", "is_deleted": True})).inserted_id

    got = await load_by_ids(test_db, "schoolDetails", [a, str(a), None, "not-an-oid", ObjectId()])
    assert set(got) == {a}
    assert got[a]["school_name"] == "A"

    # match filter mirrors a find_one that excluded soft-deleted rows
    got2 = await load_by_ids(test_db, "schoolDetails", [a, b], match={"is_deleted": {"$ne": True}})
    assert set(got2) == {a}
