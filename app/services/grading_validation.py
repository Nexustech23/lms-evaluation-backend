"""Validate model judgments against immutable rubric limits before publishing."""
from app.core.diagnostics import trace_function
from app.core.diagnostic_details import validation_error
from copy import deepcopy
import math


def number(value, label, question_no=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise validation_error('GRADE_FINITE_NUMBER_REQUIRED', f"{label} must be a finite number", field=label, question_no=question_no)
    return float(value)


def equal(a, b):
    return math.isclose(a, b, rel_tol=0, abs_tol=1e-6)


@trace_function('evaluation.validate')
def validate_grading(result, rules, total_marks, answer_text, *, trusted_mcq=(), review_invalid_evidence=False):
    result = deepcopy(result)
    rows = result.get("questionwise_marking")
    if not isinstance(rows, list) or len(rows) != len(rules):
        raise validation_error('GRADE_QUESTION_COUNT_MISMATCH', "Evaluation must include every rubric question exactly once")
    seen = set()
    for row in rows:
        n = number(row.get("question_no"), "question_no")
        if not n.is_integer() or not 1 <= n <= len(rules) or n in seen:
            raise validation_error('GRADE_QUESTION_NUMBER_INVALID', "Invalid or duplicate question number", question_no=n, field="question_no")
        seen.add(n); n = int(n); row["question_no"] = n
        rule = rules[n - 1]
        maximum = number(rule["maxMarks"], "rubric maximum", question_no=n)
        if not equal(number(row.get("max_marks"), "max_marks", question_no=n), maximum):
            raise validation_error('GRADE_MAXIMUM_MISMATCH', f"Question {n}: maximum does not match approved rubric", question_no=n, field="max_marks")
        awarded = number(row.get("ai_awarded_marks"), "awarded marks", question_no=n)
        if not 0 <= awarded <= maximum:
            raise validation_error('GRADE_SCORE_OUT_OF_RANGE', f"Question {n}: marks outside approved range", question_no=n, field="ai_awarded_marks")
        flags = row.get("flags")
        if not isinstance(flags, dict) or "needs_review" not in flags or any(not isinstance(v, bool) for v in flags.values()):
            raise validation_error('GRADE_FLAGS_INVALID', "Question flags must be booleans", question_no=n, field="flags")
        if flags.get("unanswered") and awarded:
            raise validation_error('GRADE_UNANSWERED_HAS_MARKS', "Unanswered question cannot earn marks", question_no=n)
        evidence_invalid = False
        if awarded and n not in trusted_mcq:
            quotes = row.get("evidence")
            normalized = " ".join(answer_text.split())
            if not isinstance(quotes, list) or not quotes or any(
                    not isinstance(q, str) or not q.strip() or " ".join(q.split()) not in normalized for q in quotes):
                evidence_invalid = True
                if not review_invalid_evidence:
                    raise validation_error('GRADE_EVIDENCE_NOT_VERIFIED', f"Question {n}: positive score requires a real student-answer evidence quote", question_no=n, field="evidence")
        for field in ("reasoning", "feedback", "improvement"):
            if not isinstance(row.get(field), str):
                raise validation_error('GRADE_EXPLANATION_FIELD_MISSING', f"Question {n}: missing {field}", question_no=n, field=field)
        cos = row.get("cos", [])
        expected_cos = {co["co_code"]: co for co in rule.get("cos", [])}
        if not isinstance(cos, list) or len(cos) != len(expected_cos):
            raise validation_error('GRADE_CO_COUNT_MISMATCH', "CO entries do not match the rubric", question_no=n, field="cos")
        codes = set(); co_total = 0
        for co in cos:
            code = co.get("co_code")
            if code not in expected_cos or code in codes:
                raise validation_error('GRADE_CO_CODE_INVALID', "Unknown or duplicate CO", question_no=n, field="cos")
            codes.add(code)
            cap = number(expected_cos[code]["marks"], "CO rubric maximum", question_no=n)
            score = number(co.get("ai_marks"), "CO score", question_no=n)
            if not equal(number(co.get("max_marks"), "CO maximum", question_no=n), cap) or not 0 <= score <= cap:
                raise validation_error('GRADE_CO_SCORE_INVALID', "CO score or maximum is invalid", question_no=n, field="cos")
            co_total += score
        if cos and not equal(co_total, awarded):
            raise validation_error('GRADE_CO_TOTAL_MISMATCH', "CO scores must sum to the question score", question_no=n, field="cos")
        params = row.get("parameters", [])
        expected_params = {p["name"]: p["percentage"] for p in rule.get("parameters", [])}
        # The backend replaces MCQ judgments with an exact key comparison.
        if n not in trusted_mcq:
            if not isinstance(params, list) or len(params) != len(expected_params):
                raise validation_error('GRADE_PARAMETER_COUNT_MISMATCH', "Parameters do not match the rubric", question_no=n, field="parameters")
            names = set(); param_total = 0
            for p in params:
                name = p.get("name")
                if name not in expected_params or name in names:
                    raise validation_error('GRADE_PARAMETER_NAME_INVALID', "Unknown or duplicate grading parameter", question_no=n, field="parameters")
                names.add(name); weight = number(expected_params[name], "rubric parameter weight", question_no=n)
                score = number(p.get("ai_score"), "parameter score", question_no=n)
                if not equal(number(p.get("weight_percentage"), "parameter weight", question_no=n), weight) or not 0 <= score <= maximum * weight / 100 + 1e-7:
                    raise validation_error('GRADE_PARAMETER_SCORE_INVALID', "Parameter score or weight is invalid", question_no=n, field="parameters")
                param_total += score
            if params and not equal(param_total, awarded):
                raise validation_error('GRADE_PARAMETER_TOTAL_MISMATCH', "Parameter scores must sum to the question score", question_no=n, field="parameters")
        if evidence_invalid:
            # The approved policy publishes valid AI marks immediately. Keep
            # uncertainty visible inside Self Evaluate without changing scores.
            from app.services.evaluation_review import flag_review
            from app.core.diagnostics import event
            event("validation.flagged", reason_code="GRADE_EVIDENCE_NOT_VERIFIED", question_no=n,
                  field="evidence", outcome="optional_faculty_review", stage="evaluation.validate")
            flag_review(row, "Supporting evidence could not be verified against the extracted answer. Inspect the original answer sheet.")
        row.update(max_marks=maximum, ai_awarded_marks=awarded, final_marks=awarded, grace_marks=0)
    rows.sort(key=lambda q: q["question_no"])
    rubric_total = sum(number(r["maxMarks"], "maximum") for r in rules)
    if not equal(rubric_total, number(total_marks, "exam maximum")):
        raise validation_error('GRADE_RUBRIC_TOTAL_MISMATCH', "Rubric total does not match exam maximum", field="total_marks")
    total = sum(q["ai_awarded_marks"] for q in rows)
    result["summary"] = {"total_ai_marks": round(total, 2), "total_max_marks": rubric_total,
        "questions_total": len(rows), "questions_attempted": sum(not q["flags"].get("unanswered", False) for q in rows)}
    return result
