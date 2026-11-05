"""Paper quality fixes: tidy maths, marks tag after the stem, no formula pictures, answer key and check, layout, export."""
import asyncio
import base64
import json
from datetime import date
from io import BytesIO

import pytest
from docx import Document
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from PIL import Image

import app.api.routers.question_paper as qp
from app.api.deps import get_current_identity
from app.db.mongodb import get_database
from app.services import docx_common, paper_check
from app.services.docx_from_text import _strip_markdown, build_docx
from app.services.paper_blueprint import PAPER_PROMPT_VERSION, place_marks_tag, validate_paper

SECTIONS = [{"label": "Section 1", "percent": 100, "bloomLevels": []}]


def paper(questions, label="Section 1"):
    return json.dumps({"sections": [{"label": label, "attempt_count": len(questions), "questions": questions}]})


def question(number, text, marks=4, **extra):
    return {"number": number, "text": text, "marks": marks, "bloom_level": 2, "co_codes": [], "diagrams": [], **extra}


MCQ = "A body of mass 2 kg moves at 3 m/s^2 for 5 s. Find F = m*a.\na) 5 N\nb) 6 N\nc) 7 N\nd) 8 N"


# ---------------- the paper check ----------------

def test_the_prompt_version_moves_on():
    assert PAPER_PROMPT_VERSION == "paper-blueprint-v2"


def test_maths_in_a_question_is_tidied_before_it_is_printed():
    text, _ = validate_paper(paper([question(1, MCQ), question(2, "Find sqrt(750) at 30 degrees")]), SECTIONS, 8, [])
    assert "3 m/s²" in text and "F = m × a" in text and "√(750)" in text and "30°" in text
    assert "^" not in text and "sqrt" not in text


def test_the_marks_tag_follows_the_stem_not_the_last_option():
    text, _ = validate_paper(paper([question(1, MCQ), question(2, "Define work")]), SECTIONS, 8, [])
    lines = text.splitlines()
    stem = next(l for l in lines if l.startswith("Q1."))
    assert stem.endswith("[4 Marks]")
    assert next(l for l in lines if l.startswith("d)")) == "d) 8 N"
    assert next(l for l in lines if l.startswith("Q2.")).endswith("[4 Marks]")  # no options: tag stays at the end


def test_course_outcome_tags_travel_with_the_marks_tag():
    cos = [{"co_code": "CO1", "description": "x"}]
    text, _ = validate_paper(paper([question(1, MCQ, co_codes=["CO1"]), question(2, "Define work", co_codes=["CO1"])]),
                             SECTIONS, 8, cos)
    assert next(l for l in text.splitlines() if l.startswith("Q1.")).endswith("[4 Marks] [CO1]")


@pytest.mark.parametrize("content,tag,expected", [
    ("Stem\na) x\nb) y", "[2 Marks]", "Stem [2 Marks]\na) x\nb) y"),
    ("Stem line 1\nStem line 2\n\n(a) x\n(b) y", "[2 Marks]", "Stem line 1\nStem line 2 [2 Marks]\n\n(a) x\n(b) y"),
    ("Stem only", "[2 Marks]", "Stem only [2 Marks]"),
    ("a) starts with an option\nb) y", "[2 Marks]", "a) starts with an option\nb) y [2 Marks]"),
    ("Name the law. A) Newton", "[2 Marks]", "Name the law. A) Newton [2 Marks]"),
])
def test_place_marks_tag(content, tag, expected):
    assert place_marks_tag(content, tag) == expected


def test_formula_pictures_are_dropped_but_real_figures_are_kept():
    formula = {"type": "math_expression", "title": "Formula", "expressions": ["a = F/m"]}
    table = {"type": "data_table", "title": "Data", "headers": ["t", "v"], "rows": [["1", "2"]]}
    text, _ = validate_paper(paper([question(1, MCQ, diagrams=[formula, table]), question(2, "Define work")]), SECTIONS, 8, [])
    assert "math_expression" not in text and "a = F/m" not in text
    assert '"data_table"' in text


def test_the_answer_key_is_collected_and_never_printed_or_returned_with_the_sections():
    text, blueprint = validate_paper(paper([
        question(1, MCQ, answer="b", working="F = 2 x 3"),
        question(2, "Define work", answer=["a", "c"], working=None),
        ]), SECTIONS, 8, [])
    assert blueprint["answer_key"] == [{"number": 1, "answer": "b", "working": "F = 2 x 3"},
                                       {"number": 2, "answer": "a, c", "working": None}]
    assert "F = 2 x 3" not in text
    assert all("answer" not in q and "working" not in q for s in blueprint["sections"] for q in s["questions"])


def test_a_paper_without_answers_is_still_accepted():
    _, blueprint = validate_paper(paper([question(1, MCQ), question(2, "Define work")]), SECTIONS, 8, [])
    assert blueprint["answer_key"] == []


# ---------------- the answer check ----------------

KEY = [{"number": 1, "answer": "b"}, {"number": 2, "answer": "3.16 m/s"}, {"number": 3, "answer": "a, c"}, {"number": 4, "answer": "24 N"}]


def test_a_difference_between_the_written_answer_and_the_check_is_flagged():
    checks = [{"number": 1, "my_answer": "c", "problem": ""}, {"number": 2, "my_answer": "5 m/s", "problem": ""},
              {"number": 3, "my_answer": "a, c", "problem": ""}, {"number": 4, "my_answer": "24.0 N", "problem": ""}]
    flags = paper_check.compare_answers(KEY, checks)
    assert [f["number"] for f in flags] == [1, 2]
    assert "b" in flags[0]["issue"] and "c" in flags[0]["issue"]


def test_the_checkers_own_objection_is_a_flag_even_when_the_answers_agree():
    flags = paper_check.compare_answers(KEY, [{"number": 3, "my_answer": "a, c", "problem": "No option matches 3.16 m/s"}])
    assert flags == [{"number": 3, "issue": "No option matches 3.16 m/s"}]


def test_numbers_agree_within_one_percent_and_letters_in_any_order():
    flags = paper_check.compare_answers(
        [{"number": 1, "answer": "6.00 N"}, {"number": 2, "answer": "c, a"}],
        [{"number": 1, "my_answer": "6.04 N"}, {"number": 2, "my_answer": "a and c"}])
    assert flags == []


def test_answers_that_cannot_be_compared_raise_no_flag():
    assert paper_check.compare_answers([{"number": 1, "answer": "Newton's first law"}],
                                       [{"number": 1, "my_answer": "the law of inertia"}]) == []


def test_the_reply_is_read_even_inside_a_code_block():
    reply = "```json\n" + json.dumps({"checks": [{"number": 1, "my_answer": "b", "problem": ""}, {"number": "x"}]}) + "\n```"
    assert paper_check.parse_checks(reply) == [{"number": 1, "my_answer": "b", "problem": ""}]


@pytest.mark.parametrize("reply", ["not json", json.dumps({"other": 1}), json.dumps([1, 2])])
def test_a_reply_without_checks_is_an_error(reply):
    with pytest.raises((ValueError, TypeError)):
        paper_check.parse_checks(reply)


def test_the_check_sees_the_questions_but_never_the_answer_key(monkeypatch):
    seen = {}

    def fake(prompt, model, max_tokens, system=None, effort=None):
        seen.update(prompt=prompt, model=model, max_tokens=max_tokens, system=system, effort=effort)
        return "{}", {}

    monkeypatch.setattr(paper_check, "generate_text", fake)
    paper_check.check_paper([{"number": 1, "marks": 4, "text": "Find F"}], "claude-sonnet-5-5", 16000, "medium")
    assert "Find F" in seen["prompt"] and "answer" not in seen["prompt"].lower()
    assert seen["system"] == paper_check.CHECK_SYSTEM and seen["effort"] == "medium" and seen["model"] == "claude-sonnet-5-5"


# ---------------- the job ----------------

class JobHarness:
    def __init__(self, monkeypatch, check_reply=None, check_error=None):
        self.updates, self.calls = [], []
        questions = [question(1, MCQ, answer="b", working="x"), question(2, "Define work", answer="Rate of energy")]

        async def tracked(db, function, *args, **kwargs):
            self.calls.append(kwargs["feature"])
            if function is qp.generate_text:
                return paper(questions), {"output_tokens": 10}
            if check_error:
                raise check_error
            return check_reply, {}

        async def update_job(prefix, job_id, fields):
            self.updates.append(fields)

        async def claimed(prefix, job_id):
            return True

        monkeypatch.setattr(qp, "tracked_faculty_call", tracked)
        monkeypatch.setattr(qp, "update_job", update_job)
        monkeypatch.setattr(qp, "claim_job_execution", claimed)
        monkeypatch.setattr(qp, "get_database", lambda: object())
        monkeypatch.setattr(qp, "build_docx", lambda *a, **k: b"docx")
        monkeypatch.setattr(qp, "upload_file_to_imagekit", lambda *a, **k: {"url": "u", "file_id": "f"})

    def run(self, check_answers=True):
        params = {"institute_name": "I", "department_name": "D", "subject_name": "S", "exam_type": "E", "semester": "1",
                  "academic_year": "", "duration": "3 Hours", "total_marks": 8, "sections": SECTIONS, "prompt": "p",
                  "faculty_id": "f1", "user_id": None, "co_list": [], "check_answers": check_answers}
        asyncio.run(qp._run_generation_job("job1", params, {}))
        return self.updates[-1]


def test_the_job_returns_flags_from_the_answer_check(monkeypatch):
    reply = json.dumps({"checks": [{"number": 1, "my_answer": "c", "problem": ""}, {"number": 2, "my_answer": "x", "problem": ""}]})
    harness = JobHarness(monkeypatch, check_reply=reply)
    done = harness.run()
    assert done["status"] == "completed" and done["answerCheck"] == "done"
    assert [f["number"] for f in done["reviewFlags"]] == [1]
    assert done["answerKey"][0] == {"number": 1, "answer": "b", "working": "x"}
    assert qp.Feature.QP_ANSWER_CHECK in harness.calls
    assert "Q1." in done["paperText"] and "working" not in done["paperText"]


def test_a_failed_check_never_fails_the_paper(monkeypatch):
    harness = JobHarness(monkeypatch, check_error=RuntimeError("provider down"))
    done = harness.run()
    assert done["status"] == "completed" and done["answerCheck"] == "failed" and done["reviewFlags"] == []


def test_the_check_can_be_switched_off(monkeypatch):
    harness = JobHarness(monkeypatch)
    done = harness.run(check_answers=False)
    assert done["status"] == "completed" and done["answerCheck"] == "skipped"
    assert qp.Feature.QP_ANSWER_CHECK not in harness.calls


# ---------------- the Word files ----------------

def build(paper_text, academic_year=""):
    return Document(BytesIO(build_docx(paper_text, "Inst", "Dept", "Physics", "Mock", "1", academic_year, 60, "3 Hours", [])))


def texts(document):
    return [p.text for p in document.paragraphs]


def test_instructions_are_not_numbered_twice():
    document = build("INSTRUCTIONS:\n1. Follow the attempt rule printed under each section.\nSection 1 - Single Correct (+3, -1)\nQ1. Stem [3 Marks]\na) x")
    assert "1. Follow the attempt rule printed under each section." in texts(document)
    assert not any(t.startswith("1. 1.") for t in texts(document))


def test_the_stray_crest_line_is_gone_and_a_blank_year_shows_the_current_one():
    document = build("INSTRUCTIONS:\n1. Go.\nSection 1\nQ1. Stem [3 Marks]")
    assert "- * -" not in texts(document)
    assert f"Academic Year: {docx_common.current_academic_year()}" in " ".join(texts(document)).replace("  ", " ")
    assert "2024 - 2025" not in " ".join(texts(document))


def test_a_year_that_was_typed_is_kept():
    assert "2025 - 2026" in " ".join(texts(build("INSTRUCTIONS:\n1. Go.\nSection 1\nQ1. Stem [3 Marks]", "2025 - 2026")))


@pytest.mark.parametrize("today,expected", [(date(2026, 11, 5), "2026 - 2027"), (date(2026, 6, 1), "2026 - 2027"),
                                            (date(2026, 5, 31), "2025 - 2026"), (date(2027, 1, 15), "2026 - 2027")])
def test_the_academic_year_runs_june_to_may(today, expected):
    assert docx_common.current_academic_year(today) == expected


def test_a_numbered_section_is_a_heading_and_options_are_indented():
    document = build("INSTRUCTIONS:\n1. Go.\nSection 1 - Single Correct (+3, -1)\nQ1. Stem [3 Marks]\na) first\nb) second")
    heading = next(p for p in document.paragraphs if p.text.startswith("Section 1"))
    assert heading.runs[0].bold and heading.runs[0].font.size.pt == 12
    option = next(p for p in document.paragraphs if "first" in p.text)
    assert option.paragraph_format.left_indent is not None and option.text.startswith("a)")


@pytest.mark.parametrize("line", ["Describe L = (0+1)*00", "Is a*b* ambiguous?", "5 × 3 = 15", "x = 2*y if needed"])
def test_a_single_star_is_no_longer_deleted_as_markdown(line):
    assert _strip_markdown(line) == line


def test_bold_markers_are_still_removed():
    assert _strip_markdown("**bold** and ***both***") == "bold and both"


# ---------------- Download DOCX from the editor ----------------

def png_uri():
    buffer = BytesIO()
    Image.new("RGBA", (96, 64), (0, 0, 0, 0)).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(qp.router)
    app.dependency_overrides[get_current_identity] = lambda: {"user_id": "u", "role": 3}
    app.dependency_overrides[get_database] = lambda: object()

    async def faculty(identity, db):
        return None, "faculty-1"

    monkeypatch.setattr(qp, "_require_faculty", faculty)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_the_download_contains_the_editors_edits_and_equations(client):
    html = (f'<p>Edited stem {"<img class=lms-eq src=" + png_uri() + " data-latex=x^2 data-display=0 data-w=2 data-h=1.5 data-d=0.4>"} end</p>'
            "<p>A second line added in the editor</p>")
    async with client as c:
        response = await c.post("/question-paper/export-docx", json={"editorContent": html, "subjectName": "Physics", "academicYear": "2026 - 2027"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert "attachment" in response.headers["content-disposition"] and ".docx" in response.headers["content-disposition"]
    document = Document(BytesIO(response.content))
    assert len(document.inline_shapes) == 1
    body = " ".join(p.text for p in document.paragraphs)
    assert "Edited stem" in body and "A second line added in the editor" in body and "2026 - 2027" in body


async def test_an_empty_paper_cannot_be_downloaded(client):
    async with client as c:
        assert (await c.post("/question-paper/export-docx", json={"editorContent": "  "})).status_code == 400


async def test_an_absurdly_large_paper_is_refused(client):
    async with client as c:
        assert (await c.post("/question-paper/export-docx", json={"editorContent": "x" * 8_000_001})).status_code == 422
