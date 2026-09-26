# ============================================================
# Pure-logic tests for the deterministic MCQ pipeline (app/utils/mcq_detect.py,
# app/services/mcq_grading.py). No DB, no network/AI calls — these are exactly
# the parts of the feature that can be fully verified without spending API
# credit, and they're where an accuracy regression would matter most.
# ============================================================

from app.services.mcq_grading import (
    _letter_map,
    apply_deterministic_mcq_overrides,
    parse_mcq_answers_from_ocr_text,
    score_mcq_answer,
)
from app.utils.mcq_detect import detect_mcq_questions


# ---------- detect_mcq_questions ----------

def test_detect_mcq_questions_finds_lettered_options():
    text = (
        "Q1. Which is true?\n"
        "    [1 Mark] [CO1]\n"
        "    a) Option one\n"
        "    b) Option two\n"
        "    c) Option three\n"
        "\n"
        "Q2. Explain in detail.\n"
        "    [5 Marks] [CO2]\n"
    )
    mcq = detect_mcq_questions(text)
    assert list(mcq.keys()) == [1]
    assert mcq[1] == ["Option one", "Option two", "Option three"]


def test_detect_mcq_questions_ignores_subpart_labels():
    # (a)/(b) sub-parts of an essay question must never be mistaken for MCQ options.
    text = (
        "Q1. Differentiate list and tuple.\n"
        "    [5 Marks] [CO1]\n"
        "    (a) Explain lists.\n"
        "    (b) Explain tuples.\n"
    )
    assert detect_mcq_questions(text) == {}


def test_detect_mcq_questions_single_option_line_not_mcq():
    # One lettered line alone shouldn't be enough — needs >=2 to count as MCQ.
    text = "Q1. Define X.\n    a) not really an option list\n"
    assert detect_mcq_questions(text) == {}


def test_detect_mcq_questions_empty_or_none_input():
    assert detect_mcq_questions("") == {}
    assert detect_mcq_questions(None) == {}


# ---------- parse_mcq_answers_from_ocr_text ----------

def test_parse_mcq_answers_extracts_tagged_lines():
    text = (
        "1.B\nMCQ_ANSWER 1: B\nMCQ_ANSWER 2: A\nMCQ_ANSWER 6: UNCLEAR\n"
        "some essay text about supervised learning\nMCQ_ANSWER 14: c\n"
    )
    parsed = parse_mcq_answers_from_ocr_text(text)
    assert parsed == {"1": "B", "2": "A", "6": None, "14": "C"}


def test_parse_mcq_answers_empty_text():
    assert parse_mcq_answers_from_ocr_text("") == {}
    assert parse_mcq_answers_from_ocr_text(None) == {}


# ---------- score_mcq_answer: the actual deterministic-matching logic ----------

def test_score_mcq_answer_correct_match_full_marks():
    result = score_mcq_answer(1, "B", {"answer": "B", "confident": True}, max_marks=1)
    assert result["final_marks"] == 1.0
    assert result["flags"]["needs_review"] is False
    assert result["flags"]["unanswered"] is False


def test_score_mcq_answer_wrong_match_is_always_zero_never_partial():
    # This is the exact bug case found in production: student picked the wrong
    # letter and Claude still awarded 1 mark ("partial leniency could apply").
    # Deterministic scoring must never do that — wrong is always exactly 0.
    result = score_mcq_answer(9, "C", {"answer": "A", "confident": True}, max_marks=1)
    assert result["final_marks"] == 0.0
    assert result["ai_awarded_marks"] == 0.0


def test_score_mcq_answer_unclear_ocr_is_unanswered_not_wrong():
    result = score_mcq_answer(6, None, {"answer": "C", "confident": True}, max_marks=1)
    assert result["final_marks"] == 0
    assert result["flags"]["unanswered"] is True
    assert result["flags"]["needs_review"] is False


def test_score_mcq_answer_unconfident_key_flags_for_review_not_guessed():
    result = score_mcq_answer(9, "B", {"answer": "B", "confident": False}, max_marks=1)
    assert result["flags"]["needs_review"] is True
    assert result["final_marks"] == 0


def test_score_mcq_answer_missing_key_flags_for_review_not_guessed():
    result = score_mcq_answer(9, "B", None, max_marks=1)
    assert result["flags"]["needs_review"] is True


def test_score_mcq_answer_co_credit_all_or_nothing():
    cos = [{"co_code": "CO3", "marks": 1, "description": "x"}]
    correct = score_mcq_answer(1, "A", {"answer": "A", "confident": True}, max_marks=1, cos=cos)
    assert correct["cos"][0]["ai_marks"] == 1

    wrong = score_mcq_answer(1, "B", {"answer": "A", "confident": True}, max_marks=1, cos=cos)
    assert wrong["cos"][0]["ai_marks"] == 0


def test_score_mcq_answer_accepts_claude_output_shaped_cos_too():
    # apply_deterministic_mcq_overrides passes Claude's OWN output cos shape
    # ({"max_marks": ...}) rather than the rubric input shape ({"marks": ...}) —
    # both must resolve the same max-marks value.
    cos_output_shape = [{"co_code": "CO3", "ai_marks": 1, "remarks": "x", "max_marks": 1}]
    correct = score_mcq_answer(1, "A", {"answer": "A", "confident": True}, max_marks=1, cos=cos_output_shape)
    assert correct["cos"][0]["max_marks"] == 1
    assert correct["cos"][0]["ai_marks"] == 1


# ---------- apply_deterministic_mcq_overrides: the merge step ----------

def _claude_entry(q_no, marks, max_marks=1, co_code="CO1"):
    return {
        "question_no": q_no, "max_marks": max_marks, "ai_awarded_marks": marks, "final_marks": marks,
        "cos": [{"co_code": co_code, "ai_marks": marks, "remarks": "x", "max_marks": max_marks}],
        "flags": {}, "reasoning": "", "feedback": "", "improvement": "", "parameters": [],
    }


def test_apply_overrides_corrects_leniency_bug_and_leaves_subjective_untouched():
    claude_output = [
        _claude_entry(1, marks=1),   # Claude leniently gave full marks to a wrong answer
        _claude_entry(2, marks=0),   # Claude under-credited a correct answer
        _claude_entry(21, marks=1.5, max_marks=2, co_code="CO1"),  # subjective — must stay untouched
    ]
    mcq_answer_key = {"1": {"answer": "A", "confident": True}, "2": {"answer": "C", "confident": True}}
    student_mcq_answers = {"1": "C", "2": "C"}  # Q1 wrong, Q2 correct

    merged, count = apply_deterministic_mcq_overrides(claude_output, mcq_answer_key, student_mcq_answers)

    assert count == 2
    by_q = {q["question_no"]: q for q in merged}
    assert by_q[1]["final_marks"] == 0.0     # corrected down (leniency bug fixed)
    assert by_q[2]["final_marks"] == 1.0     # corrected up (was under-credited)
    assert by_q[21] == claude_output[2]      # subjective question byte-for-byte unchanged


def test_apply_overrides_noop_when_no_answer_key():
    claude_output = [_claude_entry(1, marks=1)]
    merged, count = apply_deterministic_mcq_overrides(claude_output, {}, {})
    assert count == 0
    assert merged == claude_output


def test_apply_overrides_preserves_question_order():
    claude_output = [_claude_entry(1, marks=1), _claude_entry(2, marks=0), _claude_entry(3, marks=0.5, max_marks=2)]
    mcq_answer_key = {"1": {"answer": "A", "confident": True}}
    merged, _ = apply_deterministic_mcq_overrides(claude_output, mcq_answer_key, {"1": "A"})
    assert [q["question_no"] for q in merged] == [1, 2, 3]


# ---------- _letter_map: the fix for "student wrote the answer's text, not a letter" ----------

def test_letter_map_assigns_in_order():
    assert _letter_map(["Little or no autocorrelation", "High multicollinearity"]) == {
        "A": "Little or no autocorrelation",
        "B": "High multicollinearity",
    }


def test_letter_map_matches_detect_mcq_questions_option_order():
    # The options saved into mcq_answer_key (for the OCR step) must use the exact
    # same order/letters as the ones actually detected from the paper — otherwise
    # option "B" saved for OCR could silently mean a different option than "B" in
    # the resolved answer key.
    text = "Q1. Pick one.\n    a) Zebra\n    b) Apple\n    c) Mango\n"
    detected = detect_mcq_questions(text)
    assert _letter_map(detected[1]) == {"A": "Zebra", "B": "Apple", "C": "Mango"}


def test_extract_answer_text_with_gemini_prompt_includes_option_wording(monkeypatch):
    # No real API call — generate_content_from_file is monkeypatched to capture the
    # prompt instead of sending it anywhere. Confirms a student who writes the
    # answer's WORDING (not a bare letter) can actually be matched: the option text
    # itself must appear in the prompt sent to the OCR model, not just the question
    # number, which was the exact gap this fix closes.
    import app.services.grading as grading_module

    captured = {}

    def fake_generate_content_from_file(pdf_bytes, mime_type, prompt, model=None):
        captured["prompt"] = prompt
        return "OCR text", {"prompt_tokens": 1, "candidate_tokens": 1, "total_tokens": 2}

    monkeypatch.setattr(grading_module, "generate_content_from_file", fake_generate_content_from_file)

    grading_module.extract_answer_text_with_gemini(
        b"fake-pdf-bytes",
        mcq_questions_with_options={1: {"A": "Little or no autocorrelation", "B": "High multicollinearity"}},
    )

    assert "Little or no autocorrelation" in captured["prompt"]
    assert "High multicollinearity" in captured["prompt"]
    assert "Question 1" in captured["prompt"]
    assert "MCQ_ANSWER" in captured["prompt"]


def test_extract_answer_text_with_gemini_omits_mcq_block_when_no_options(monkeypatch):
    import app.services.grading as grading_module

    captured = {}

    def fake_generate_content_from_file(pdf_bytes, mime_type, prompt, model=None):
        captured["prompt"] = prompt
        return "OCR text", {"prompt_tokens": 1, "candidate_tokens": 1, "total_tokens": 2}

    monkeypatch.setattr(grading_module, "generate_content_from_file", fake_generate_content_from_file)

    grading_module.extract_answer_text_with_gemini(b"fake-pdf-bytes", mcq_questions_with_options=None)

    assert "MCQ_ANSWER" not in captured["prompt"]
