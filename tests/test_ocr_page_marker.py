"""A page's closing PAGE_COMPLETE marker: small format differences are accepted, a missing marker is retried once."""
import io

import PyPDF2
import pytest

import app.services.grading as grading_module
from app.services.grading import _marker_diagnostics, _strip_page_marker, extract_answer_text_with_gemini


def one_page_pdf():
    writer = PyPDF2.PdfWriter(); writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO(); writer.write(buffer)
    return buffer.getvalue()


USAGE = {"prompt_tokens": 10, "candidate_tokens": 5, "total_tokens": 15}


def script(monkeypatch, replies):
    """Fake Gemini: returns the given (text, truncated) replies in order, counting calls."""
    calls = []

    def fake(pdf_bytes, mime_type, prompt, model=None, media_resolution=None):
        text, truncated = replies[len(calls)]
        calls.append(prompt)
        return text, dict(USAGE), truncated

    monkeypatch.setattr(grading_module, "generate_content_from_file_checked", fake)
    return calls


@pytest.mark.parametrize("text,body", [
    ("answer text\nPAGE_COMPLETE", "answer text"),
    ("answer text PAGE_COMPLETE", "answer text"),
    ("answer text\n\nPAGE_COMPLETE\n", "answer text"),
    ("answer text\nPAGE_COMPLETE.", "answer text"),
    ("answer text\n**PAGE_COMPLETE**", "answer text"),
    ("answer text\n`PAGE_COMPLETE`", "answer text"),
    ("answer text\n[PAGE_COMPLETE]", "answer text"),
    ("answer text\npage_complete", "answer text"),
    ("answer text\nPAGE COMPLETE", "answer text"),
    ("answer text.\nPAGE_COMPLETE", "answer text."),                 # the student's own final full stop is kept
    ("1 - 2 -\n**PAGE_COMPLETE**", "1 - 2 -"),
    ("PAGE_COMPLETE", ""),                                      # a blank page
])
def test_marker_with_small_format_differences_is_accepted(text, body):
    assert _strip_page_marker(text) == body


@pytest.mark.parametrize("text", [
    "answer text", "", None, "   ",
    "answer PAGE_COMPLETE and more words",                      # real words after the marker
    "PAGE_COMPLETE\nthen the student wrote more",
    "answer text\nPAGE_INCOMPLETE",
])
def test_missing_or_misplaced_marker_is_not_accepted(text):
    assert _strip_page_marker(text) is None


def test_the_marker_is_removed_from_the_text_used_for_grading(monkeypatch):
    script(monkeypatch, [("x = 5 PAGE_COMPLETE here\nPAGE_COMPLETE.", False)])
    text, _ = extract_answer_text_with_gemini(one_page_pdf())
    assert text == "PAGE 1\nx = 5 PAGE_COMPLETE here"


def test_a_formatting_variant_is_accepted_without_a_second_call(monkeypatch):
    calls = script(monkeypatch, [("answer\n**PAGE_COMPLETE**", False)])
    text, usage = extract_answer_text_with_gemini(one_page_pdf())
    assert text == "PAGE 1\nanswer" and len(calls) == 1 and usage["total_tokens"] == 15


def test_a_missing_marker_is_retried_once_and_both_attempts_are_counted(monkeypatch):
    calls = script(monkeypatch, [("answer", False), ("answer\nPAGE_COMPLETE", False)])
    text, usage = extract_answer_text_with_gemini(one_page_pdf())
    assert text == "PAGE 1\nanswer" and len(calls) == 2
    assert calls[0] == calls[1]
    assert usage["total_tokens"] == 30 and usage["prompt_tokens"] == 20


def test_a_marker_missing_twice_rejects_the_sheet_and_still_reports_all_usage(monkeypatch):
    calls = script(monkeypatch, [("answer", False), ("answer again", False)])
    with pytest.raises(RuntimeError, match="incomplete") as error:
        extract_answer_text_with_gemini(one_page_pdf())
    assert len(calls) == 2 and error.value.usage["total_tokens"] == 30


def test_a_page_that_was_cut_off_is_rejected_without_a_retry(monkeypatch):
    calls = script(monkeypatch, [("answer\nPAGE_COMPLETE", True), ("answer\nPAGE_COMPLETE", False)])
    with pytest.raises(RuntimeError, match="incomplete") as error:
        extract_answer_text_with_gemini(one_page_pdf())
    assert len(calls) == 1 and error.value.usage["total_tokens"] == 15


def test_diagnostics_describe_the_shape_not_the_students_words():
    info = _marker_diagnostics("Rolle's theorem: f(a)=f(b).\nPAGE_COMPLETEx!")
    assert info["marker_anywhere"] is True and info["text_length"] > 10
    assert "Rolle" not in info["tail_shape"] and set(info["tail_shape"]) <= set("x \n.!_()=':,")
    assert _marker_diagnostics("")["marker_anywhere"] is False


def test_rejection_is_logged_with_a_reason_and_the_retry_is_logged(monkeypatch):
    events = []
    monkeypatch.setattr(grading_module, "diagnostic_event", lambda name, **fields: events.append(fields))
    script(monkeypatch, [("answer", False), ("answer", False)])
    with pytest.raises(RuntimeError):
        extract_answer_text_with_gemini(one_page_pdf())
    outcomes = [(e.get("outcome"), e.get("reason_code")) for e in events]
    assert ("retrying", "PAGE_COMPLETE_MARKER_MISSING") in outcomes
    assert ("rejected_incomplete", "PAGE_COMPLETE_MARKER_MISSING") in outcomes
    assert any(e.get("attempt") == 2 and "tail_shape" in e for e in events)
