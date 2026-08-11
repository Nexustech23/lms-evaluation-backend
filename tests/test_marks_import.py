import io

import openpyxl
from bson import ObjectId

from tests.test_security_fixes import _register_institute_admin


def _build_marks_xlsx(rows: list[tuple[str, float]]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Student Name", "Marks"])
    for name, marks in rows:
        ws.append([name, marks])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def test_import_marks_requires_auth(client):
    xlsx = _build_marks_xlsx([("Alice", 90)])
    resp = await client.post(
        "/import-marks-excel",
        files={"file": ("marks.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"batch_id": str(ObjectId()), "semester": "1"},
    )
    assert resp.status_code == 401


async def test_import_marks_rejects_non_integer_semester(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Marks Institute")
    xlsx = _build_marks_xlsx([("Alice", 90)])
    resp = await institute.post(
        "/import-marks-excel",
        files={"file": ("marks.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"batch_id": str(ObjectId()), "semester": "not-a-number"},
    )
    assert resp.status_code == 422


async def test_import_marks_rejects_invalid_batch_id(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Marks Institute")
    xlsx = _build_marks_xlsx([("Alice", 90)])
    resp = await institute.post(
        "/import-marks-excel",
        files={"file": ("marks.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"batch_id": "not-an-object-id", "semester": "1"},
    )
    assert resp.status_code == 400


async def test_import_marks_rejects_out_of_range_marks(superadmin_client, client_factory):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Marks Institute")
    xlsx = _build_marks_xlsx([("Alice", 150)])
    resp = await institute.post(
        "/import-marks-excel",
        files={"file": ("marks.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"batch_id": str(ObjectId()), "semester": "1"},
    )
    assert resp.status_code == 400
    assert "between 0 and 100" in resp.json()["error"]


async def test_import_marks_success(superadmin_client, client_factory, test_db):
    institute = await _register_institute_admin(superadmin_client, client_factory, "Marks Institute")
    xlsx = _build_marks_xlsx([("Alice", 90), ("Bob", 75)])
    batch_id = str(ObjectId())
    resp = await institute.post(
        "/import-marks-excel",
        files={"file": ("marks.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"batch_id": batch_id, "semester": "2"},
    )
    assert resp.status_code == 200
    assert resp.json()["processed_count"] == 2

    stored = await test_db["importedMarks"].find({"batch_id": ObjectId(batch_id)}).to_list(None)
    assert len(stored) == 2
    assert stored[0]["semester"] == 2

    # re-importing replaces the prior batch/semester rows rather than appending
    resp2 = await institute.post(
        "/import-marks-excel",
        files={"file": ("marks.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"batch_id": batch_id, "semester": "2"},
    )
    assert resp2.status_code == 200
    stored_again = await test_db["importedMarks"].find({"batch_id": ObjectId(batch_id)}).to_list(None)
    assert len(stored_again) == 2
