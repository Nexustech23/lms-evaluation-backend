"""A school's paper says Class where a college's says Semester: in the AI prompt and in the Word header. No AI calls."""
from io import BytesIO

from docx import Document

from app.api.routers.question_paper import _build_claude_prompt, _semester_label
from app.services.docx_from_html import generate_docx_from_html
from app.services.docx_from_text import build_docx

SECTIONS = [{"label": "Section A", "percent": 100, "bloomLevels": []}]


def prompt(semester, school):
    return _build_claude_prompt("Chapter 1", "", "", "Some Institute", "Physics", "Annual Examination", semester, "2026-27",
                                50, "2 Hours", SECTIONS, [], school=school)


def all_text(docx_bytes):
    doc = Document(BytesIO(docx_bytes))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


def test_the_label_follows_the_institute_type():
    assert _semester_label("school") == "CLASS"
    assert [_semester_label(kind) for kind in ("college", "coaching", "", None)] == ["SEMESTER"] * 4


def test_a_school_prompt_says_class_and_a_college_prompt_is_unchanged():
    school = prompt("Class 12", True)
    assert ("* Class:".ljust(18) + "Class 12") in school and "Semester" not in school.split("REFERENCE CONTENT")[0]
    assert "class level" in school and "semester level" not in school
    college = prompt("Semester 3", False)
    assert "class level" not in college and "semester level" in college


def test_the_college_line_is_exactly_what_it_was():
    # the original template line: "* Semester:" followed by seven spaces and the value
    assert "* Semester:       Semester 3\n" in prompt("Semester 3", False)
    assert "* Class:".ljust(18) + "Class 12\n" in prompt("Class 12", True)


def test_a_school_word_header_says_class():
    html = "<p>Q1. Name the SI unit of force.</p>"
    school = all_text(generate_docx_from_html(html, {"institute": "School", "examType": "Annual Examination", "subjectName": "Physics",
                                                     "semester": "Class 12", "semesterLabel": "CLASS", "duration": "3 Hours", "totalMarks": 70}))
    assert "CLASS" in school and "Class 12" in school and "SEMESTER" not in school


def test_a_college_word_header_still_says_semester():
    html = "<p>Q1. Name the SI unit of force.</p>"
    header = {"institute": "College", "examType": "End Semester Examination", "subjectName": "Physics",
              "semester": "Semester 3", "duration": "3 Hours", "totalMarks": 70}
    assert "SEMESTER" in all_text(generate_docx_from_html(html, header))        # no label given: the old default
    assert "SEMESTER" in all_text(generate_docx_from_html(html, {**header, "semesterLabel": "SEMESTER"}))


def test_the_generated_paper_header_uses_the_label_too():
    args = ("Q1. Name the SI unit of force. (2 marks)", "School", "", "Physics", "Annual Examination", "Class 12", "2026-27", 70, "3 Hours")
    assert "CLASS" in all_text(build_docx(*args, semester_label="CLASS"))
    assert "SEMESTER" in all_text(build_docx(*args))
