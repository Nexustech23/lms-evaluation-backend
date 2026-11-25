"""PDF versions of the Reports. No real browser is needed for most of these: the HTML is built by a pure function and checked,
and the routes are tested with the renderer swapped out. Things that must hold: everything that came from data is HTML-escaped,
a missing number is a dash and never an invented zero, no student is named, the footer carries page numbers, and only an
Institute Admin can ask. One test renders a real PDF and is skipped where Chromium is not installed."""
import asyncio

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.api.routers import reports_overview as router
from app.services import reports_attainment as ra
from app.services import reports_data_check as rdc
from app.services import reports_pdf as pdf
from app.services import reports_result_analysis as rra
from tests.test_reports_attainment import MATRIX, answer, course, exam, standard, world
from tests.test_reports_overview import ADMIN_USER, BATCH, INSTITUTE, database
from tests.test_reports_result_analysis import rec


def run(coro):
    return asyncio.run(coro)


def analysis_html(db, institute="Demo University", **scope):
    return pdf.result_analysis_html(run(rra.build_result_analysis(db, INSTITUTE, **scope)), institute, "A. Admin")


def sample():
    return database([rec("S1", 1, [("Maths", "A", 85), ("Physics", "B", 70)], 8.5),
                     rec("S2", 1, [("Maths", "U", 20), ("Physics", "U", 30)], 3.0)])


# ---- the HTML ---------------------------------------------------------------------------------------------------------------

def test_the_document_has_the_institute_title_scope_stamp_numbers_and_the_rules():
    html = analysis_html(sample(), batch=str(BATCH))
    for text in ("Demo University", "Result Analysis", "Scope: Semester 1 | All programmes | CSE 2023", "by A. Admin",
                 "Pass % by subject, lowest first", "Grade distribution", "<svg", "50%", "Maths", "Physics",
                 "How this report is worked out", "No student is named", "failing grade (F, U)"):
        assert text in html, text


def test_everything_that_came_from_data_is_html_escaped():
    db = database([rec("S1", 1, [("<script>alert(1)</script>", "A", 80)], 8.0)])
    db.c["programmeDetails"].docs[0]["programme_name"] = 'B.Tech & "CSE" <b>'
    html = analysis_html(db, institute='<img src=x onerror=alert(1)>')
    assert "<script>" not in html and "<img" not in html and "<b>" not in html.replace("<b>Everything", "")
    assert "&lt;script&gt;" in html and "B.Tech &amp; &quot;CSE&quot;" in html and "&lt;img src=x" in html


def test_a_missing_number_is_a_dash_never_a_zero_and_an_empty_scope_says_so():
    html = analysis_html(database())
    assert "No results yet for this selection." in html
    assert '<div class="v">-</div>' in html and '<div class="v">0</div>' not in html
    assert "<svg" not in html                                           # no chart without data


def test_small_groups_and_programmes_under_target_are_marked_in_the_tables():
    html = analysis_html(sample())
    assert "(small group)" in html and "(below target)" in html


def test_no_student_is_named_in_any_report_pdf():
    db = sample()
    for doc in db.c["academic_transcripts"].docs:
        doc["student_name"] = "Secret Name"
    assert "Secret Name" not in analysis_html(db) and "S1" not in analysis_html(db)


def test_the_data_check_pdf_lists_the_problems_and_says_when_all_is_clear():
    db = database(subjectDetails=[course({}, name=None, subject_name="Algebra")] if False else
                  [{"_id": ObjectId(), "institute_id": INSTITUTE, "programme_id": ObjectId(), "subject_name": "Algebra",
                    "co_po_matrix": {}}])
    html = pdf.data_check_html(run(rdc.build_data_check(db, INSTITUTE)), "Demo University", "A. Admin")
    assert "Courses without CO-PO mapping (1)" in html and "Algebra" in html and "Data Check" in html
    clear = {"_id": ObjectId(), "institute_id": INSTITUTE, "programme_id": ObjectId(), "subject_name": "X", "co_po_matrix": MATRIX}
    ok = pdf.data_check_html(run(rdc.build_data_check(database(subjectDetails=[clear]), INSTITUTE)), "U", "A")
    assert "Everything checked is complete." in ok
    empty = pdf.data_check_html(run(rdc.build_data_check(database(), INSTITUTE)), "U", "A")
    assert "Nothing to check yet" in empty


def test_the_attainment_pdf_shows_po_cells_with_levels_statuses_and_the_all_semesters_scope():
    db, _ = standard()
    html = pdf.attainment_html(run(ra.build_attainment(db, INSTITUTE)), "Demo University", "A. Admin")
    for text in ("CO / PO Attainment", "Scope: All semesters | All programmes | All batches", "40% (level 1.2)", "0% (level 0)",
                 "Worked out", "CO2 3/1.8 attained", "CO1 1/1.8 not attained", "no PO target"):
        assert text in html, text


def test_the_attainment_pdf_for_too_many_courses_asks_for_a_narrower_scope():
    ra_report = {"too_many": True, "subjects_in_scope": 90, "limit": 40, "counts": {}, "po_codes": [], "programmes": [],
                 "batches": [], "subjects": [], "scope": {"programme": None, "batch": None, "semester": None},
                 "filters": {"programmes": [], "batches": []}, "generated_at": "2026-11-25T09:00:00+00:00"}
    html = pdf.attainment_html(ra_report, "U", "A")
    assert "90 courses" in html and "Choose a programme, a batch or a semester" in html and "<table" not in html


def test_the_charts_clamp_labels_and_escape_text():
    svg = pdf.hbars([("<b>" + "x" * 60, 50.0, False), ("low", -5.0, True)], target=60)
    assert "<b>" not in svg and "&lt;b&gt;" in svg and "stroke-dasharray" in svg
    assert 'width="0.0"' in svg                                         # a negative value never draws a negative width
    assert pdf.hbars([]) == "" and pdf.columns_chart([]) == ""
    assert "&lt;" in pdf.columns_chart([("<i>", 3, False)])


def test_the_footer_carries_page_numbers():
    assert 'class="pageNumber"' in pdf.FOOTER and 'class="totalPages"' in pdf.FOOTER


# ---- the routes -------------------------------------------------------------------------------------------------------------

@pytest.fixture
def renderer(monkeypatch):
    seen = {}

    def fake(html, footer=None):
        seen["html"], seen["footer"] = html, footer
        return b"%PDF-fake"
    monkeypatch.setattr(router, "render_html_to_pdf", fake)
    return seen


def call(name, db, role=2, **extra):
    args = dict(programme=None, batch=None, semester=None, identity={"role": role, "user_id": str(ADMIN_USER)}, db=db)
    args.update(extra)
    return run(getattr(router, name)(**args))


@pytest.mark.parametrize("name,prefix,title", [
    ("reports_result_analysis_pdf", "result-analysis-", "Result Analysis"),
    ("reports_data_check_pdf", "data-check-", "Data Check"),
    ("reports_attainment_pdf", "co-po-attainment-", "CO / PO Attainment"),
])
def test_each_pdf_route_returns_a_pdf_download_with_page_numbers_and_the_institute_name(renderer, name, prefix, title):
    db = sample()
    db.c["users"].docs[0]["fullName"] = "Demo University"
    response = call(name, db)
    assert response.media_type == "application/pdf" and response.body == b"%PDF-fake"
    disposition = response.headers["content-disposition"]
    assert disposition.startswith(f'attachment; filename="{prefix}') and disposition.endswith('.pdf"')
    assert "Demo University" in renderer["html"] and title in renderer["html"] and renderer["footer"] == pdf.FOOTER


@pytest.mark.parametrize("name", ["reports_result_analysis_pdf", "reports_data_check_pdf", "reports_attainment_pdf"])
@pytest.mark.parametrize("role", [1, 3, 4, 5, 6, 7, None])
def test_only_an_institute_admin_may_ask_for_a_pdf(renderer, name, role):
    with pytest.raises(HTTPException) as error:
        call(name, database(), role=role)
    assert error.value.status_code == 403 and "html" not in renderer


def test_a_school_institute_is_refused_before_any_rendering(renderer):
    with pytest.raises(HTTPException) as error:
        call("reports_result_analysis_pdf", database(institute_type="school"))
    assert error.value.status_code == 403 and "html" not in renderer


def test_if_the_renderer_fails_the_admin_gets_a_clear_503(monkeypatch):
    def broken(html, footer=None):
        raise RuntimeError("Chromium is not installed")
    monkeypatch.setattr(router, "render_html_to_pdf", broken)
    with pytest.raises(HTTPException) as error:
        call("reports_result_analysis_pdf", sample())
    assert error.value.status_code == 503 and error.value.detail == "The PDF could not be created"


def test_the_shared_renderer_still_works_without_a_footer_for_its_other_callers():
    import inspect
    from app.services.pdf_render import render_html_to_pdf
    assert inspect.signature(render_html_to_pdf).parameters["footer_template"].default is None


def test_a_real_pdf_is_rendered_when_chromium_is_available():
    from app.services.pdf_render import render_html_to_pdf
    try:
        data = render_html_to_pdf(analysis_html(sample()), pdf.FOOTER)
    except RuntimeError as exc:
        pytest.skip(f"Chromium is not available here: {exc}")
    assert data.startswith(b"%PDF") and len(data) > 2000
