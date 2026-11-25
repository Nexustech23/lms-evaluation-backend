# ============================================================
# PDF versions of the Reports: Result Analysis, Data Check and CO / PO Attainment. These reports hold counts, averages and
# percentages only, never a student's name, so a PDF of them can be shared or printed for a meeting.
#
# Each PDF has the institute's name, the title, the scope in words, when it was generated and by whom, the numbers, a chart
# where the screen has one, the tables with the header row repeated on every page, "how this is worked out", and page
# numbers. Everything that came from data is HTML-escaped, so a subject or programme name can never inject markup.
# The HTML is rendered with the same renderer the transcripts use (services/pdf_render.py); no AI is involved.
# ============================================================

import html
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.services.reports_result_analysis import scope_words

FOOTER = ('<div style="font-size:8px;width:100%;text-align:center;color:#555;">'
          'Page <span class="pageNumber"></span> of <span class="totalPages"></span></div>')

CSS = """
@page { size: A4; }
body { font-family: "Segoe UI", "Noto Sans", "Noto Sans Devanagari", "Noto Sans Bengali", Arial, sans-serif;
       font-size: 10.5px; color: #111; line-height: 1.4; }
h1 { font-size: 20px; margin: 0 0 2px 0; }
h2 { font-size: 13px; margin: 18px 0 6px 0; break-after: avoid-page; }
.inst { font-size: 12px; font-weight: 600; color: #1d4ed8; }
.meta { color: #444; margin: 2px 0; }
.rule { border: 0; border-top: 2px solid #1d4ed8; margin: 8px 0 10px 0; }
.summary { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0; }
.card { border: 1px solid #bbb; border-radius: 5px; padding: 6px 9px; min-width: 110px; break-inside: avoid; }
.card .l { color: #555; font-size: 9px; } .card .v { font-size: 16px; font-weight: 700; } .card .s { color: #555; font-size: 9px; }
table { width: 100%; border-collapse: collapse; margin: 4px 0 8px 0; }
thead { display: table-header-group; }
tr { break-inside: avoid; }
th, td { border-bottom: 1px solid #ddd; padding: 4px 6px; text-align: left; vertical-align: top; }
th { background: #eef2ff; font-weight: 600; border-bottom: 1px solid #99a; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
.note { color: #555; font-size: 9px; } .fig { break-inside: avoid; margin: 6px 0; }
ul { margin: 4px 0 4px 16px; padding: 0; } li { margin: 2px 0; }
"""

BLUE, RED, GREY = "#1d4ed8", "#dc2626", "#6b7280"


def e(value: Any) -> str:
    """Escaped text; a missing value is a dash, never an invented zero."""
    return "-" if value is None or value == "" else html.escape(str(value))


def _num(value: Any, suffix: str = "") -> Optional[str]:
    return None if value is None else f"{value:g}{suffix}" if isinstance(value, (int, float)) else str(value)


def _pct_note(value: Any, small: bool) -> Optional[str]:
    return None if value is None else f"{value:g}%" + (" (small group)" if small else "")


def table(columns: Sequence[str], rows: Iterable[Sequence[Any]], numeric: Sequence[int] = ()) -> str:
    head = "".join(f'<th class="{"n" if i in numeric else ""}">{e(c)}</th>' for i, c in enumerate(columns))
    body = "".join("<tr>" + "".join(f'<td class="{"n" if i in numeric else ""}">{e(c)}</td>' for i, c in enumerate(row)) + "</tr>"
                   for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def cards(items: Iterable[Tuple[str, Any, Optional[str]]]) -> str:
    return '<div class="summary">' + "".join(
        f'<div class="card"><div class="l">{e(label)}</div><div class="v">{e(value)}</div>'
        f'<div class="s">{e(sub) if sub else "&nbsp;"}</div></div>' for label, value, sub in items) + "</div>"


def hbars(items: Sequence[Tuple[str, float, bool]], target: Optional[float] = None, width: int = 640) -> str:
    """Horizontal bars for percentages 0-100: (label, value, is_low). Labels and values are written out as text."""
    if not items:
        return ""
    row, left = 20, 190
    height = row * len(items) + 14
    scale = (width - left - 60) / 100
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" role="img">']
    for i, (label, value, low) in enumerate(items):
        y = 6 + i * row
        parts.append(f'<text x="{left - 6}" y="{y + 12}" font-size="10" text-anchor="end">{e(label)[:34]}</text>')
        parts.append(f'<rect x="{left}" y="{y + 2}" width="{max(value, 0) * scale:.1f}" height="13" fill="{RED if low else BLUE}"/>')
        parts.append(f'<text x="{left + max(value, 0) * scale + 4:.1f}" y="{y + 12}" font-size="10">{value:g}%</text>')
    if target is not None:
        x = left + target * scale
        parts.append(f'<line x1="{x:.1f}" y1="2" x2="{x:.1f}" y2="{height - 6}" stroke="{GREY}" stroke-dasharray="4 3"/>')
    return "".join(parts) + "</svg>"


def columns_chart(items: Sequence[Tuple[str, float, bool]], width: int = 460, height: int = 150) -> str:
    """Columns for counts: (label, value, is_fail). Each column's count is written above it."""
    if not items:
        return ""
    top = max(v for _, v, _ in items) or 1
    slot = (width - 20) / len(items)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" role="img">']
    for i, (label, value, fail) in enumerate(items):
        h = (height - 40) * value / top
        x = 10 + i * slot + slot * 0.15
        parts.append(f'<rect x="{x:.1f}" y="{height - 22 - h:.1f}" width="{slot * 0.7:.1f}" height="{h:.1f}" fill="{RED if fail else BLUE}"/>')
        parts.append(f'<text x="{x + slot * 0.35:.1f}" y="{height - 26 - h:.1f}" font-size="9" text-anchor="middle">{value:g}</text>')
        parts.append(f'<text x="{x + slot * 0.35:.1f}" y="{height - 8}" font-size="9" text-anchor="middle">{e(label)[:10]}</text>')
    return "".join(parts) + "</svg>"


def document(title: str, institute: str, scope: str, generated_at: str, generated_by: str, body: str,
             definitions: Sequence[str]) -> str:
    notes = "".join(f"<li>{e(d)}</li>" for d in definitions)
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{e(title)}</title><style>{CSS}</style></head><body>'
            f'<div class="inst">{e(institute)}</div><h1>{e(title)}</h1>'
            f'<div class="meta">Scope: {e(scope)}</div>'
            f'<div class="meta">Generated {e(generated_at)} UTC by {e(generated_by)}</div><hr class="rule"/>'
            f'{body}<h2>How this report is worked out</h2><ul>{notes}</ul>'
            f'<div class="note">This report contains counts, averages and percentages only. No student is named.</div>'
            f'</body></html>')


def _stamp(report: Dict[str, Any]) -> str:
    return report["generated_at"][:16].replace("T", " ")


# ---- Result Analysis ---------------------------------------------------------------------------------------------------------

def result_analysis_html(report: Dict[str, Any], institute: str, generated_by: str) -> str:
    s, rules = report["summary"], report["rules"]
    low = s["lowest_subject"]
    body = cards([
        ("Appeared", s["appeared"] or None, f"{s['passed']} passed, {s['failed']} failed" if s["appeared"] else None),
        ("Pass %", _num(s["pass_pct"], "%"), f"{_num(s['previous_pass_pct'], '%')} last semester" if s["previous_pass_pct"] is not None else None),
        ("Average CGPA", _num(s["average_cgpa"]), f"Highest {s['highest_cgpa']:g}, lowest {s['lowest_cgpa']:g}" if s["highest_cgpa"] is not None else None),
        ("Lowest subject", low["name"] if low else None, f"{low['pass_pct']:g}% pass, {low['n']} students" if low else None),
    ])
    if not s["appeared"]:
        body += "<p>No results yet for this selection.</p>"
    else:
        subjects = [(x["name"], x["pass_pct"], x["pass_pct"] < rules["pass_target_percent"]) for x in report["subjects"] if x["pass_pct"] is not None]
        body += f'<h2>Pass % by subject, lowest first (target {rules["pass_target_percent"]:g}%)</h2><div class="fig">{hbars(subjects, rules["pass_target_percent"])}</div>'
        grades = [(g["grade"], g["count"], g["grade"] in rules["fail_grades"]) for g in report["grades"]["overall"]]
        body += f'<h2>Grade distribution</h2><div class="fig">{columns_chart(grades)}</div>'
        if report["programmes"]:
            body += "<h2>Programmes</h2>" + table(
                ["Programme", "Appeared", "Passed", "Pass %", "Average CGPA"],
                [[p["name"] + (" (below target)" if p["below_target"] else ""), p["appeared"], p["passed"],
                  _pct_note(p["pass_pct"], p["small"]), _num(p["average_cgpa"])] for p in report["programmes"]],
                numeric=[1, 2, 3, 4])
        body += "<h2>Subjects</h2>" + table(
            ["Subject", "Appeared", "Passed", "Pass %", "Mean", "Median", "Highest", "Lowest"],
            [[x["name"], x["appeared"], x["passed"], _pct_note(x["pass_pct"], x["small"]),
              _num(x["mean"]), _num(x["median"]), _num(x["highest"]), _num(x["lowest"])] for x in report["subjects"]],
            numeric=[1, 2, 3, 4, 5, 6, 7])
        if report["cgpa_distribution"]:
            body += "<h2>CGPA distribution</h2>" + table(["CGPA band", "Students"], [[b["label"], b["count"]] for b in report["cgpa_distribution"]], numeric=[1])
    return document("Result Analysis", institute, scope_words(report), _stamp(report), generated_by, body, [
        f"A student passes a semester when no subject has a failing grade ({', '.join(rules['fail_grades'])}). Pass % = passed / appeared.",
        "Subject pass % = students with a non-failing grade / students graded in that subject.",
        f"Groups under {rules['small_group']} students are marked, because one student can move the percentage a lot.",
        "Results marked as needing regeneration, and subjects without a grade, are left out."])


# ---- Data Check -------------------------------------------------------------------------------------------------------------

def data_check_html(report: Dict[str, Any], institute: str, generated_by: str) -> str:
    s = report["summary"]
    body = cards([
        ("Courses without CO-PO mapping", s["courses"]["count"] if report["checks_ran"] else None, f"of {s['courses']['of']}"),
        ("Exams without CO tags", s["exams"]["count"] if report["checks_ran"] else None, f"of {s['exams']['of']}"),
        ("Answer sheets waiting for review", s["evaluation"]["pending"] if s["evaluation"]["of"] else None, f"of {s['evaluation']['of']}"),
        ("Results to regenerate", s["stale"]["count"] if report["checks_ran"] else None, None),
    ])
    if not report["checks_ran"]:
        body += "<p>Nothing to check yet: there are no courses or exams in this scope.</p>"
    elif report["all_clear"]:
        body += "<p><b>Everything checked is complete.</b></p>"
    for key, heading, columns, row_of in (
        ("courses", "Courses without CO-PO mapping", ["Course", "Code", "Programme", "Batch", "Semester"],
         lambda c: [c["name"], c["code"], c["programme"], c["batch"], c["semester"]]),
        ("exams", "Exams without CO tags", ["Exam", "Subject", "Semester", "Faculty"], lambda x: [x["name"], x["subject"], x["semester"], x["faculty"]]),
        ("evaluation", "Answer sheets waiting for review", ["Exam", "Subject", "Semester", "Pending", "Total"],
         lambda x: [x["name"], x["subject"], x["semester"], x["pending"], x["total"]]),
        ("stale", "Results to regenerate", ["Batch", "Semester", "Students"], lambda x: [x["batch"], x["semester"], x["students"]]),
    ):
        section = report[key]
        if section["total"]:
            more = f' <span class="note">(showing the first {len(section["items"])} of {section["total"]})</span>' if section["truncated"] else ""
            body += f"<h2>{e(heading)} ({section['total']}){more}</h2>" + table(columns, [row_of(i) for i in section["items"]])
    return document("Data Check", institute, scope_words(report), _stamp(report), generated_by, body, [
        "A course with no CO-PO matrix cannot have CO and PO attainment worked out.",
        "An exam with no covered COs cannot count towards CO attainment. Archived exams are not checked.",
        "An answer sheet is waiting for review when marking is not final yet.",
        "A result marked as needing regeneration is left out of every number until it is regenerated."])


# ---- CO / PO Attainment -------------------------------------------------------------------------------------------------------

def attainment_html(report: Dict[str, Any], institute: str, generated_by: str) -> str:
    counts, overall = report["counts"], report.get("overall") or {}
    words = {"ok": "Worked out", "no_matrix": "No CO-PO matrix", "no_data": "No answer sheets",
             "pending_review": "Review pending", "no_targets": "No programme level bands"}
    scope = scope_words(report, "All semesters")
    if report["too_many"]:
        body = (f"<p>This scope has {report['subjects_in_scope']} courses, more than the {report['limit']} that can be worked "
                f"out at once. Choose a programme, a batch or a semester.</p>")
    else:
        co_line = f"{overall['co_attained']} of {overall['co_total']} course outcomes" if overall.get("co_total") else None
        left = counts["no_data"] + counts["pending_review"] + counts["no_targets"]
        body = cards([("Courses in scope", counts["in_scope"], None),
                      ("Courses worked out", counts["computed"] if counts["in_scope"] else None, f"of {counts['in_scope']}"),
                      ("Course outcomes attained", _num(round(overall["co_attained"] / overall["co_total"] * 100), "%") if overall.get("co_total") else None, co_line),
                      ("Courses left out", left if counts["in_scope"] else None, None)])
        po_cols = report["po_codes"]
        for key, heading, first in (("programmes", "By programme", "Programme"), ("batches", "By batch", "Batch")):
            if report[key]:
                body += f"<h2>{heading}</h2><p class='note'>Each cell shows the PO % and its level from 0 to 3.</p>" + table(
                    [first, "Courses", "COs attained", *po_cols],
                    [[g["name"], g["subjects"], f"{g['co_attained']} / {g['co_total']}",
                      *[(f"{g['po'][po]['percent']:g}% (level {g['po'][po]['level']:g})" if po in g["po"] else None) for po in po_cols]]
                     for g in report[key]], numeric=[1, 2])
        if report["subjects"]:
            body += "<h2>Courses</h2>" + table(
                ["Course", "Code", "Programme", "Batch", "Semester", "Status", "Course outcomes (score / target)"],
                [[c["name"], c["code"], c["programme"], c["batch"], c["semester"], words.get(c["status"], c["status"]),
                  "; ".join(f"{x['code']} {x['score']:g}/{x['target']:g} {'attained' if x['attained'] else 'not attained'}" for x in c["co"]) or None]
                 for c in report["subjects"]])
    return document("CO / PO Attainment", institute, scope, _stamp(report), generated_by, body, [
        "CO scores are worked out the same way as the per-course Excel report: each exam's level comes from the share of students at or above the threshold (level bands set on the programme), weighted by the exam weight, with the course-exit (indirect) part added by weight. A CO is attained when its final score reaches the CO target.",
        "PO % = the CO-PO weights of the attained COs / all the CO-PO weights for that PO x 100. The level is the PO % on a scale of 0 to 3. A programme's or batch's PO % is the mean over its courses that address the PO.",
        "Absent and malpractice students are left out. A course with no answer sheets, a pending review, or no programme level bands is listed with its reason and left out of the roll-ups, never counted as zero.",
        "The app stores no PO target, so PO values are shown without one."])
