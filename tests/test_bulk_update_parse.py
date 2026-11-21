"""Bulk update, phase 1: reading and checking the file, and working out what would change.
Pure code: no database, no network, no AI."""
import copy
import io
from datetime import date, datetime

import pytest
from openpyxl import Workbook

from app.services import bulk_update as bu
from app.services.bulk_update import BulkUpdateError


# ---- sample files -----------------------------------------------------------------------------------------------------

def xlsx(rows):
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def csv_bytes(text, encoding="utf-8"):
    return text.encode(encoding)


HEADER = ["Enrollment No", "Name", "Father's Name", "Date of Birth", "Contact No"]


def parse(rows, filename="students.xlsx", max_rows=2000):
    return bu.parse_upload(xlsx(rows), filename, max_rows)


# ---- column names -----------------------------------------------------------------------------------------------------

def test_column_names_are_recognised_however_they_are_written():
    parsed = parse([["Enrolment Number", "Student Name", "Fathers Name", "D.O.B", "Mobile"],
                    ["E1", "Asha Rao", "Mohan Rao", "2004-05-06", "9876543210"]])
    assert parsed.columns_used == {"enrollment_no": "Enrolment Number", "name": "Student Name",
                                   "father_name": "Fathers Name", "dob": "D.O.B", "contact_no": "Mobile"}
    row = parsed.rows[0]
    assert row.errors == [] and row.values == {"name": "Asha Rao", "father_name": "Mohan Rao", "dob": "2004-05-06", "contact_no": "9876543210"}


def test_columns_that_may_not_change_are_ignored_and_reported():
    parsed = parse([HEADER + ["Roll No", "Email", "Gender", "Programme"], ["E1", "Asha", "", "", "", "12", "a@b.c", "F", "BTech"]])
    assert parsed.ignored_columns == ["Roll No", "Email", "Gender", "Programme"]
    assert set(parsed.rows[0].values) == {"name"}                    # nothing from the ignored columns is read


def test_the_file_needs_an_enrollment_column():
    with pytest.raises(BulkUpdateError, match="Enrollment No"):
        parse([["Name", "Contact No"], ["Asha", "9876543210"]])


def test_the_file_needs_a_column_to_update():
    with pytest.raises(BulkUpdateError, match="no column to update"):
        parse([["Enrollment No", "Roll No", "Email"], ["E1", "1", "a@b.c"]])


def test_two_columns_for_the_same_thing_are_refused():
    with pytest.raises(BulkUpdateError, match="Two columns"):
        parse([["Enrollment No", "Name", "Student Name"], ["E1", "A", "B"]])


def test_a_header_only_file_has_no_students():
    with pytest.raises(BulkUpdateError, match="no student rows"):
        parse([HEADER])


# ---- the file itself --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["students.xls", "students.txt", "students", "students.xlsx.exe", ""])
def test_only_xlsx_and_csv_are_accepted(name):
    with pytest.raises(BulkUpdateError, match="xlsx or .csv"):
        bu.parse_upload(b"x", name, 10)


def test_an_empty_file_is_refused():
    with pytest.raises(BulkUpdateError, match="empty"):
        bu.parse_upload(b"", "a.csv", 10)
    with pytest.raises(BulkUpdateError, match="empty"):
        bu.parse_upload(b",,\n\n", "a.csv", 10)


def test_a_broken_excel_file_gives_a_plain_message_not_a_trace():
    with pytest.raises(BulkUpdateError, match="Could not read the file"):
        bu.parse_upload(b"this is not a workbook", "a.xlsx", 10)


def test_too_many_rows_are_refused():
    rows = [HEADER] + [[f"E{i}", "Asha", "", "", ""] for i in range(11)]
    with pytest.raises(BulkUpdateError, match="limit is 10"):
        parse(rows, max_rows=10)
    assert len(parse(rows, max_rows=11).rows) == 11


def test_the_first_row_must_be_the_column_names():
    with pytest.raises(BulkUpdateError, match="first row"):
        bu.parse_upload(csv_bytes("\nEnrollment No,Name\nE1,Asha\n"), "a.csv", 10)


def test_excel_numbers_dates_and_leading_zeros_survive():
    book = Workbook()
    sheet = book.active
    sheet.append(HEADER)
    sheet.append([2021001, "Asha Rao", None, datetime(2004, 5, 6), 9876543210])      # real numbers and a real date
    sheet.append(["00123", "Ravi", None, None, "+91 98765 43211"])                   # text keeps its zeros
    buffer = io.BytesIO(); book.save(buffer)
    first, second = bu.parse_upload(buffer.getvalue(), "s.xlsx", 10).rows
    assert (first.enrollment_no, first.values["dob"], first.values["contact_no"]) == ("2021001", "2004-05-06", "9876543210")
    assert (second.enrollment_no, second.values["contact_no"]) == ("00123", "+919876543211")


def test_blank_rows_are_skipped_but_row_numbers_stay_true_to_the_file():
    parsed = parse([HEADER, ["E1", "Asha", "", "", ""], [None] * 5, ["E2", "Ravi", "", "", ""]])
    assert [(r.row, r.enrollment_no) for r in parsed.rows] == [(2, "E1"), (4, "E2")]


def test_csv_with_a_byte_order_mark_and_blank_lines():
    text = "Enrollment No,Name\nE1,Asha\n\nE2,Ravi\n"
    parsed = bu.parse_upload(csv_bytes(text, "utf-8-sig"), "s.csv", 10)
    assert [(r.row, r.values["name"]) for r in parsed.rows] == [(2, "Asha"), (4, "Ravi")]


def test_csv_saved_by_excel_in_windows_encoding():
    parsed = bu.parse_upload(csv_bytes("Enrollment No,Name\nE1,René Dsouza\n", "cp1252"), "s.csv", 10)
    assert parsed.rows[0].values["name"] == "René Dsouza"


def test_csv_rows_with_missing_trailing_cells_are_fine():
    parsed = bu.parse_upload(csv_bytes("Enrollment No,Name,Contact No\nE1,Asha\nE2,Ravi,9876543210\n"), "s.csv", 10)
    assert parsed.rows[0].values == {"name": "Asha"} and parsed.rows[1].values["contact_no"] == "9876543210"


# ---- one value at a time ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [("Asha Rao", "Asha Rao"), ("  asha   rao ", "asha rao"), ("A. P. J. Kumar", "A. P. J. Kumar"),
                                          ("अशोक", "अशोक")])
def test_names_are_tidied(raw, expected):
    assert bu.clean_name(raw) == (expected, None)


@pytest.mark.parametrize("raw,part", [("12345", "no letters"), ("a" * 121, "longer than"), ("Asha​Rao", "not allowed")])
def test_bad_names_are_refused(raw, part):
    value, error = bu.clean_name(raw)
    assert value is None and part in error


def test_a_blank_name_or_father_name_means_keep():
    assert bu.clean_name("   ") == (None, None) and bu.clean_father_name("") == (None, None)


@pytest.mark.parametrize("raw,expected", [("2004-05-06", "2004-05-06"), ("06-05-2004", "2004-05-06"), ("06/05/2004", "2004-05-06"),
                                          ("2004/05/06", "2004-05-06"), ("06.05.2004", "2004-05-06"), ("6 May 2004", "2004-05-06"),
                                          ("2004-05-06 00:00:00", "2004-05-06"), ("06-05-2004 00:00:00", "2004-05-06")])
def test_dates_in_common_layouts_become_iso(raw, expected):
    assert bu.clean_dob(raw) == (expected, None)


@pytest.mark.parametrize("raw", ["31-02-2004", "2004-13-01", "yesterday", "05-06", "1899-12-31", "2099-01-01", "2004-05-06 99:99"])
def test_bad_or_impossible_dates_are_refused(raw):
    value, error = bu.clean_dob(raw, today=date(2026, 11, 21))
    assert value is None and error and "Date of birth" in error


def test_a_date_today_is_allowed_and_tomorrow_is_not():
    assert bu.clean_dob("2026-11-21", today=date(2026, 11, 21))[0] == "2026-11-21"
    assert bu.clean_dob("2026-11-22", today=date(2026, 11, 21))[0] is None


@pytest.mark.parametrize("raw,expected", [("9876543210", "9876543210"), ("98765 43210", "9876543210"), ("+91 98765-43210", "+919876543210"),
                                          ("(022) 2345 6789", "02223456789"), ("9876543210.0", "9876543210")])
def test_contact_numbers_become_plain_digits(raw, expected):
    assert bu.clean_contact_no(raw) == (expected, None)


@pytest.mark.parametrize("raw", ["12345", "1234567890123456", "98765abc10", "9.87654E+09", "98765.43210", "--"])
def test_bad_contact_numbers_are_refused(raw):
    value, error = bu.clean_contact_no(raw)
    assert value is None and "Contact number" in error


def test_enrollment_numbers_keep_their_text_and_lose_excel_decimals():
    assert bu.clean_enrollment_no("2021001.0") == ("2021001", None)
    assert bu.clean_enrollment_no(" 00123 ") == ("00123", None)
    assert bu.clean_enrollment_no("")[0] is None and "empty" in bu.clean_enrollment_no("")[1]
    assert "longer" in bu.clean_enrollment_no("x" * 51)[1]


# ---- the file as a whole ----------------------------------------------------------------------------------------------

def test_a_blank_cell_means_keep_not_clear():
    row = parse([HEADER, ["E1", "Asha", "", "", ""]]).rows[0]
    assert row.values == {"name": "Asha"} and row.errors == []


def test_each_bad_cell_is_reported_on_its_own_row_and_good_rows_stay_good():
    parsed = parse([HEADER, ["E1", "Asha", "", "31-02-2004", "123"], ["E2", "Ravi", "", "2004-05-06", "9876543210"], ["", "Meena", "", "", ""]])
    bad, good, no_key = parsed.rows
    assert len(bad.errors) == 2 and bad.row == 2
    assert good.errors == [] and good.values["dob"] == "2004-05-06"
    assert no_key.errors == ["Enrollment No is empty."]


def test_the_same_enrollment_number_twice_marks_both_rows():
    parsed = parse([HEADER, ["E1", "Asha", "", "", ""], ["E2", "Ravi", "", "", ""], ["e1".upper(), "Meena", "", "", ""]])
    first, second, third = parsed.rows
    assert "appears more than once" in first.errors[0] and "row 4" in first.errors[0]
    assert second.errors == [] and "row 2" in third.errors[0]


# ---- working out what would change ------------------------------------------------------------------------------------

def student(key, **extra):
    base = {"_id": f"id-{key}", "enrollment_no": key, "name": "Asha Rao", "father_name": "Mohan Rao", "dob": "2004-05-06",
            "contact_no": "9876543210", "updated_at": datetime(2026, 11, 1, 10, 0, 0)}
    base.update(extra)
    return base


def plan(rows, students):
    parsed = parse(rows)
    return parsed, bu.plan_changes(parsed, bu.index_students(students))


def test_only_values_that_really_differ_are_changes():
    parsed, p = plan([HEADER, ["E1", "Asha Rao", "Mohan Rao", "2004-05-06", "9876543210"],
                      ["E2", "New Name", "", "", ""]], [student("E1"), student("E2")])
    assert [u["enrollment_no"] for u in p.unchanged] == ["E1"] and p.unchanged[0]["reason"] == "Already has these values."
    (change,) = p.changes
    assert change.enrollment_no == "E2" and change.student_id == "id-E2" and change.student_name == "Asha Rao"
    assert change.changes == {"name": {"old": "Asha Rao", "new": "New Name", "missing": False}}


def test_sameness_is_judged_after_tidying_both_sides():
    _, p = plan([HEADER, ["E1", " Asha   Rao ", "", "06-05-2004", "98765 43210"]],
                [student("E1", dob="2004-05-06 00:00:00", contact_no="+9876543210")])
    # the stored number has a plus the file's does not: that is a real difference; the date and name are not
    assert [c for c in p.changes[0].changes] == ["contact_no"]


def test_a_stored_date_written_another_way_is_not_a_change():
    _, p = plan([HEADER, ["E1", "", "", "2004-05-06", ""]], [student("E1", dob="06/05/2004")])
    assert p.changes == [] and len(p.unchanged) == 1


def test_a_student_without_the_field_gets_it_marked_missing():
    stored = student("E1"); del stored["father_name"]
    _, p = plan([HEADER, ["E1", "", "Mohan Rao", "", ""]], [stored])
    assert p.changes[0].changes["father_name"] == {"old": None, "new": "Mohan Rao", "missing": True}


def test_a_row_with_nothing_to_change_is_unchanged_not_an_error():
    _, p = plan([HEADER, ["E1", "", "", "", ""]], [student("E1")])
    assert p.changes == [] and p.unchanged[0]["reason"] == "No values to change in this row." and p.rejected == []


def test_an_unknown_enrollment_number_is_rejected_with_a_reason():
    _, p = plan([HEADER, ["NOPE", "Asha", "", "", ""]], [student("E1")])
    assert p.rejected == [{"row": 2, "enrollment_no": "NOPE", "error": "No student in your college has this Enrollment No."}]


def test_two_students_with_one_enrollment_number_are_not_guessed_between():
    _, p = plan([HEADER, ["E1", "Asha", "", "", ""]], [student("E1", _id="a"), student("E1", _id="b")])
    assert p.changes == [] and "More than one" in p.rejected[0]["error"]


def test_rows_with_file_errors_are_rejected_and_never_planned():
    _, p = plan([HEADER, ["E1", "Asha", "", "31-02-2004", ""]], [student("E1")])
    assert p.changes == [] and "Date of birth" in p.rejected[0]["error"]


def test_a_stored_enrollment_number_that_is_a_number_still_matches():
    _, p = plan([HEADER, ["2021001", "New", "", "", ""]], [student(2021001)])
    assert len(p.changes) == 1


def test_the_version_comes_from_the_last_saved_time():
    _, p = plan([HEADER, ["E1", "New", "", "", ""]], [student("E1")])
    assert p.changes[0].version == "2026-11-01T10:00:00"
    _, p = plan([HEADER, ["E1", "New", "", "", ""]], [student("E1", updated_at=None)])
    assert p.changes[0].version == ""


def test_planning_changes_nothing_it_was_given():
    parsed = parse([HEADER, ["E1", "New", "", "", ""], ["E2", "", "", "", ""]])
    students = [student("E1"), student("E2")]
    before_rows, before_students = copy.deepcopy(parsed), copy.deepcopy(students)
    bu.plan_changes(parsed, bu.index_students(students))
    assert parsed == before_rows and students == before_students


def test_every_row_ends_up_in_exactly_one_place():
    parsed, p = plan([HEADER, ["E1", "New", "", "", ""], ["E2", "Asha Rao", "", "", ""], ["E3", "X", "", "", ""],
                      ["E4", "Y", "", "bad", ""], ["E1", "Dup", "", "", ""]],
                     [student("E1"), student("E2"), student("E4")])
    placed = len(p.changes) + len(p.unchanged) + len(p.rejected)
    assert placed == parsed.total_rows == 5
    assert sorted(r["row"] for r in p.rejected) == [2, 4, 5, 6]        # E1 twice (both rows), E3 unknown, E4 bad date


# ---- phase 6: the Excel file is read as a stream and stopped early ----------------------------------------------------

def test_a_huge_file_is_stopped_early_with_the_plain_limit_message():
    rows = [["Enrollment No", "Name"]] + [[f"E{i}", "A"] for i in range(60)]
    with pytest.raises(BulkUpdateError, match="more than 10 rows"):
        bu.parse_upload(xlsx(rows), "s.xlsx", 10)


def test_a_file_padded_with_blank_rows_is_refused_not_read_forever():
    rows = [["Enrollment No", "Name"], ["E1", "A"]] + [[None, None]] * 1200
    with pytest.raises(BulkUpdateError, match="too many empty rows"):
        bu.parse_upload(xlsx(rows), "s.xlsx", 2)


def test_a_file_exactly_at_the_limit_is_accepted():
    rows = [["Enrollment No", "Name"]] + [[f"E{i}", "A"] for i in range(10)]
    assert len(bu.parse_upload(xlsx(rows), "s.xlsx", 10).rows) == 10
