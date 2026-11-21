# ============================================================
# Bulk update of existing students from a file: Phase 1, reading and checking the file.
#
# Pure code: no database, no network, no AI. It turns an uploaded .xlsx or .csv into checked rows, then
# (given the students it should match, which a later phase loads) works out exactly what would change.
#
# Rules (approved): students are matched by enrollment number; a blank cell keeps the current value;
# only name, father's name, date of birth and contact number can change; every other column is ignored.
# ============================================================

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

KEY_FIELD = "enrollment_no"
UPDATE_FIELDS = ("name", "father_name", "dob", "contact_no")
FIELD_LABELS = {
    "enrollment_no": "Enrollment No", "name": "Name", "father_name": "Father's name",
    "dob": "Date of birth", "contact_no": "Contact number",
}

# Header text a person might type, written in the same normalised form normalize_header() produces.
HEADER_ALIASES = {
    "enrollment_no": ["enrollment_no", "enrolment_no", "enrollment_number", "enrolment_number", "enrollment", "enrolment",
                      "enrollmentno", "enrolmentno", "enrollment_id", "enrolment_id"],
    "name": ["name", "student_name", "student", "full_name", "fullname", "name_of_the_student", "name_of_student"],
    "father_name": ["father_name", "fathers_name", "father", "father_s_name", "name_of_father", "fathername"],
    "dob": ["dob", "date_of_birth", "birth_date", "birthdate", "d_o_b", "dateofbirth"],
    "contact_no": ["contact_no", "contact_number", "contact", "phone", "phone_no", "phone_number", "mobile", "mobile_no",
                   "mobile_number", "contactno"],
}
MAX_NAME_LENGTH = 120
MAX_KEY_LENGTH = 50
MIN_YEAR = 1900


class BulkUpdateError(ValueError):
    """A problem with the whole file (not one row). The message is written for the person who uploaded it."""


@dataclass
class ParsedRow:
    row: int                                  # the row number in the file (the header is row 1)
    enrollment_no: Optional[str]
    values: Dict[str, str] = field(default_factory=dict)       # only the cells that have a value, already cleaned
    errors: List[str] = field(default_factory=list)


@dataclass
class ParsedFile:
    rows: List[ParsedRow]
    columns_used: Dict[str, str]              # field -> the header as written in the file
    ignored_columns: List[str]                # headers that are not allowed to change, or not recognised
    total_rows: int


@dataclass
class PlannedChange:
    row: int
    enrollment_no: str
    student_id: Any
    student_name: str
    changes: Dict[str, Dict[str, Any]]        # field -> {"old": ..., "new": ..., "missing": bool}
    version: str                              # the student's version when planned, to spot a later change


@dataclass
class Plan:
    changes: List[PlannedChange] = field(default_factory=list)
    unchanged: List[Dict[str, Any]] = field(default_factory=list)      # {"row", "enrollment_no", "reason"}
    rejected: List[Dict[str, Any]] = field(default_factory=list)       # {"row", "enrollment_no", "error"}


# ---------------------------------------------------------------- reading the file

def normalize_header(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(text).strip().lower()).strip("_")


_ALIAS_TO_FIELD = {alias: name for name, aliases in HEADER_ALIASES.items() for alias in aliases}


def _decode_csv(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise BulkUpdateError("Could not read the CSV file. Save it as UTF-8 or from Excel and try again.")


def _xlsx_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():     # 9876543210.0 is shown as 9876543210, as Excel shows it
        value = int(value)
    return str(value).strip()


def _read_xlsx(data: bytes, max_rows: int) -> List[List[str]]:
    """First sheet, read as a stream and stopped early, so a huge or padded file cannot use up the server's memory."""
    from openpyxl import load_workbook
    book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sheet = book.worksheets[0]
        rows: List[List[str]] = []
        filled = 0
        for record in sheet.iter_rows(values_only=True):
            cells = [_xlsx_text(cell) for cell in record]
            rows.append(cells)
            if any(cells):
                filled += 1
            if filled > max_rows + 1:                         # already over the limit: no need to read further
                break
            if len(rows) > (max_rows + 1) * 5 + 1000:         # a file that is mostly blank rows
                raise BulkUpdateError("The file has too many empty rows. Remove them and try again.")
        return rows
    finally:
        book.close()


def read_table(data: bytes, filename: str, max_rows: int) -> List[List[str]]:
    """Every row of the file as a list of text cells, header first. Blank rows are dropped."""
    name = (filename or "").strip().lower()
    if name.endswith(".csv"):
        kind = "csv"
    elif name.endswith(".xlsx"):
        kind = "xlsx"
    else:
        raise BulkUpdateError("Upload an .xlsx or .csv file.")
    if not data:
        raise BulkUpdateError("The file is empty.")

    try:
        if kind == "csv":
            # The csv module keeps blank lines as empty rows, so the row numbers match what the person sees.
            rows = [[cell.strip() for cell in record] for record in csv.reader(io.StringIO(_decode_csv(data)))]
        else:
            rows = _read_xlsx(data, max_rows)
    except BulkUpdateError:
        raise
    except Exception:
        raise BulkUpdateError("Could not read the file. Check that it is a valid Excel (.xlsx) or CSV file.") from None

    positions = [(number, cells) for number, cells in enumerate(rows, start=1) if any(cells)]
    if not positions:
        raise BulkUpdateError("The file is empty.")
    if positions[0][0] != 1:
        raise BulkUpdateError("The first row of the file must be the column names.")
    if len(positions) - 1 > max_rows:
        raise BulkUpdateError(f"The file has more than {max_rows} rows. The limit is {max_rows} rows in one file.")
    return [[number] + cells for number, cells in positions]


def map_columns(headers: List[str]) -> Tuple[Dict[str, int], Dict[str, str], List[str]]:
    """(field -> column position, field -> header as written, ignored headers). Raises when the key or every update column is missing."""
    position: Dict[str, int] = {}
    written: Dict[str, str] = {}
    ignored: List[str] = []
    for index, header in enumerate(headers):
        if not header.strip():
            continue
        target = _ALIAS_TO_FIELD.get(normalize_header(header))
        if target is None:
            ignored.append(header)
        elif target in position:
            raise BulkUpdateError(f"Two columns look like {FIELD_LABELS[target]} ('{written[target]}' and '{header}'). Keep only one.")
        else:
            position[target] = index
            written[target] = header
    if KEY_FIELD not in position:
        raise BulkUpdateError("The file needs an 'Enrollment No' column, to find each student.")
    if not any(name in position for name in UPDATE_FIELDS):
        allowed = ", ".join(FIELD_LABELS[name] for name in UPDATE_FIELDS)
        raise BulkUpdateError(f"The file has no column to update. Add at least one of: {allowed}.")
    return position, written, ignored


# ---------------------------------------------------------------- cleaning one value

def _collapse(text: str) -> str:
    return " ".join(str(text).split())


def _shown(raw: str) -> str:
    return raw if len(raw) <= 40 else raw[:37] + "..."


def _drop_float_tail(text: str) -> str:
    """Excel hands numbers over as 9876543210.0; keep the whole number."""
    return text[:-2] if re.fullmatch(r"\d+\.0", text) else text


def clean_enrollment_no(raw: str) -> Tuple[Optional[str], Optional[str]]:
    text = _collapse(_drop_float_tail(raw.strip()))
    if not text:
        return None, "Enrollment No is empty."
    if len(text) > MAX_KEY_LENGTH:
        return None, f"Enrollment No is longer than {MAX_KEY_LENGTH} characters."
    if any(unicodedata.category(c).startswith("C") for c in text):
        return None, "Enrollment No has a character that is not allowed."
    return text, None


def _clean_person_name(raw: str, label: str) -> Tuple[Optional[str], Optional[str]]:
    text = _collapse(raw)
    if not text:
        return None, None
    if len(text) > MAX_NAME_LENGTH:
        return None, f"{label} is longer than {MAX_NAME_LENGTH} characters."
    if any(unicodedata.category(c).startswith("C") for c in text):
        return None, f"{label} has a character that is not allowed."
    if not any(c.isalpha() for c in text):
        return None, f"{label} '{_shown(text)}' has no letters."
    return text, None


def clean_name(raw: str) -> Tuple[Optional[str], Optional[str]]:
    return _clean_person_name(raw, "Name")


def clean_father_name(raw: str) -> Tuple[Optional[str], Optional[str]]:
    return _clean_person_name(raw, "Father's name")


_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d.%m.%Y", "%d %b %Y", "%d-%b-%Y", "%d %B %Y")


def parse_date(text: str) -> Optional[date]:
    """A date in any of the accepted layouts (an Excel time such as ' 00:00:00' is ignored), or None."""
    text = re.sub(r"[ T](?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?:\.\d+)?$", "", _collapse(text))
    for pattern in _DATE_FORMATS:
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def clean_dob(raw: str, today: Optional[date] = None) -> Tuple[Optional[str], Optional[str]]:
    text = _collapse(raw)
    if not text:
        return None, None
    parsed = parse_date(text)
    if parsed is None:
        return None, f"Date of birth '{_shown(text)}' is not a valid date. Use YYYY-MM-DD or DD-MM-YYYY."
    if parsed.year < MIN_YEAR or parsed > (today or date.today()):
        return None, f"Date of birth '{_shown(text)}' is not possible."
    return parsed.isoformat(), None


def normalize_contact(text: str) -> Optional[str]:
    """Digits, with a leading + kept; None when it is not a plausible number."""
    text = _drop_float_tail(text.strip())
    if not text or not re.fullmatch(r"\+?[\d\s\-()]+", text):
        return None
    digits = re.sub(r"\D", "", text)
    if not 7 <= len(digits) <= 15:
        return None
    return ("+" if text.startswith("+") else "") + digits


def clean_contact_no(raw: str) -> Tuple[Optional[str], Optional[str]]:
    text = raw.strip()
    if not text:
        return None, None
    cleaned = normalize_contact(text)
    if cleaned is None:
        return None, f"Contact number '{_shown(text)}' is not valid. Use 7 to 15 digits."
    return cleaned, None


_CLEANERS: Dict[str, Callable[[str], Tuple[Optional[str], Optional[str]]]] = {
    "name": clean_name, "father_name": clean_father_name, "dob": clean_dob, "contact_no": clean_contact_no,
}


# ---------------------------------------------------------------- the whole file

def parse_upload(data: bytes, filename: str, max_rows: int) -> ParsedFile:
    table = read_table(data, filename, max_rows)
    position, written, ignored = map_columns(table[0][1:])
    if len(table) < 2:
        raise BulkUpdateError("The file has no student rows under the column names.")

    rows: List[ParsedRow] = []
    for record in table[1:]:
        number, cells = record[0], record[1:]
        cell = lambda name: cells[position[name]] if name in position and position[name] < len(cells) else ""
        key, key_error = clean_enrollment_no(cell(KEY_FIELD))
        parsed = ParsedRow(row=number, enrollment_no=key)
        if key_error:
            parsed.errors.append(key_error)
        for name in UPDATE_FIELDS:
            if name not in position:
                continue
            value, error = _CLEANERS[name](cell(name))
            if error:
                parsed.errors.append(error)
            elif value is not None:
                parsed.values[name] = value
        rows.append(parsed)

    seen: Dict[str, List[int]] = {}
    for parsed in rows:
        if parsed.enrollment_no is not None:
            seen.setdefault(parsed.enrollment_no, []).append(parsed.row)
    for parsed in rows:
        places = seen.get(parsed.enrollment_no or "", [])
        if len(places) > 1:
            others = ", ".join(str(n) for n in places if n != parsed.row)
            parsed.errors.append(f"Enrollment No '{parsed.enrollment_no}' appears more than once in the file (also row {others}).")

    return ParsedFile(rows=rows, columns_used=written, ignored_columns=ignored, total_rows=len(rows))


# ---------------------------------------------------------------- working out the changes

def version_of(student: Dict[str, Any]) -> str:
    """A stable text for 'this student as last saved', used to notice that someone changed them in between."""
    stamp = student.get("updated_at")
    if stamp is None:
        return ""
    return stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp)


def student_key(student: Dict[str, Any]) -> str:
    return _collapse(_drop_float_tail(str(student.get("enrollment_no") if student.get("enrollment_no") is not None else "").strip()))


def index_students(students: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group students by their enrollment number, the shape plan_changes() expects."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for student in students:
        grouped.setdefault(student_key(student), []).append(student)
    return grouped


def _comparable(name: str, value: Any) -> str:
    """The stored value in the same shape a cleaned file value has, so 'same' is judged fairly."""
    if value is None:
        return ""
    text = str(value)
    if name == "dob":
        parsed = parse_date(text)
        return parsed.isoformat() if parsed else _collapse(text)
    if name == "contact_no":
        return normalize_contact(text) or _collapse(text)
    return _collapse(text)


def plan_changes(parsed: ParsedFile, students_by_key: Dict[str, List[Dict[str, Any]]]) -> Plan:
    """What would change. `students_by_key` maps each enrollment number to the students found with it (the caller reads them).
    Nothing is modified: neither the rows nor the students."""
    plan = Plan()
    for row in parsed.rows:
        key = row.enrollment_no or ""
        if row.errors:
            plan.rejected.append({"row": row.row, "enrollment_no": row.enrollment_no, "error": " ".join(row.errors)})
            continue
        found = students_by_key.get(key, [])
        if not found:
            plan.rejected.append({"row": row.row, "enrollment_no": key, "error": "No student in your college has this Enrollment No."})
            continue
        if len(found) > 1:
            plan.rejected.append({"row": row.row, "enrollment_no": key, "error": "More than one student has this Enrollment No, so it cannot be matched safely."})
            continue
        if not row.values:
            plan.unchanged.append({"row": row.row, "enrollment_no": key, "reason": "No values to change in this row."})
            continue

        student = found[0]
        changes: Dict[str, Dict[str, Any]] = {}
        for name, new in row.values.items():
            current = student.get(name)
            if _comparable(name, current) == new:
                continue
            changes[name] = {"old": current, "new": new, "missing": name not in student}
        if not changes:
            plan.unchanged.append({"row": row.row, "enrollment_no": key, "reason": "Already has these values."})
            continue
        plan.changes.append(PlannedChange(
            row=row.row, enrollment_no=key, student_id=student.get("_id"), student_name=str(student.get("name") or ""),
            changes=changes, version=version_of(student)))
    return plan
