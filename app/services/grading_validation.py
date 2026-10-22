"""Validate model judgments against immutable rubric limits before publishing."""
from copy import deepcopy
import math


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


def equal(a, b):
    return math.isclose(a, b, rel_tol=0, abs_tol=1e-6)


def validate_grading(result, rules, total_marks, answer_text, *, trusted_mcq=(), review_invalid_evidence=False):
    result = deepcopy(result)
    rows = result.get("questionwise_marking")
    if not isinstance(rows, list) or len(rows) != len(rules):
        raise ValueError("Evaluation must include every rubric question exactly once")
    seen = set()
    for row in rows:
        n = number(row.get("question_no"), "question_no")
        if not n.is_integer() or not 1 <= n <= len(rules) or n in seen:
            raise ValueError("Invalid or duplicate question number")
        seen.add(n); n = int(n); row["question_no"] = n
        rule = rules[n - 1]
        maximum = number(rule["maxMarks"], "rubric maximum")
        if not equal(number(row.get("max_marks"), "max_marks"), maximum):
            raise ValueError(f"Question {n}: maximum does not match approved rubric")
        awarded = number(row.get("ai_awarded_marks"), "awarded marks")
        if not 0 <= awarded <= maximum:
            raise ValueError(f"Question {n}: marks outside approved range")
        flags = row.get("flags")
        if not isinstance(flags, dict) or "needs_review" not in flags or any(not isinstance(v, bool) for v in flags.values()):
            raise ValueError("Question flags must be booleans")
        if flags.get("unanswered") and awarded:
            raise ValueError("Unanswered question cannot earn marks")
        evidence_invalid = False
        if awarded and n not in trusted_mcq:
            quotes = row.get("evidence")
            normalized = " ".join(answer_text.split())
            if not isinstance(quotes, list) or not quotes or any(
                    not isinstance(q, str) or not q.strip() or " ".join(q.split()) not in normalized for q in quotes):
                evidence_invalid = True
                if not review_invalid_evidence:
                    raise ValueError(f"Question {n}: positive score requires a real student-answer evidence quote")
        for field in ("reasoning", "feedback", "improvement"):
            if not isinstance(row.get(field), str):
                raise ValueError(f"Question {n}: missing {field}")
        cos = row.get("cos", [])
        expected_cos = {co["co_code"]: co for co in rule.get("cos", [])}
        if not isinstance(cos, list) or len(cos) != len(expected_cos):
            raise ValueError("CO entries do not match the rubric")
        codes = set(); co_total = 0
        for co in cos:
            code = co.get("co_code")
            if code not in expected_cos or code in codes:
                raise ValueError("Unknown or duplicate CO")
            codes.add(code)
            cap = number(expected_cos[code]["marks"], "CO rubric maximum")
            score = number(co.get("ai_marks"), "CO score")
            if not equal(number(co.get("max_marks"), "CO maximum"), cap) or not 0 <= score <= cap:
                raise ValueError("CO score or maximum is invalid")
            co_total += score
        if cos and not equal(co_total, awarded):
            raise ValueError("CO scores must sum to the question score")
        params = row.get("parameters", [])
        expected_params = {p["name"]: p["percentage"] for p in rule.get("parameters", [])}
        # The backend replaces MCQ judgments with an exact key comparison.
        if n not in trusted_mcq:
            if not isinstance(params, list) or len(params) != len(expected_params):
                raise ValueError("Parameters do not match the rubric")
            names = set(); param_total = 0
            for p in params:
                name = p.get("name")
                if name not in expected_params or name in names:
                    raise ValueError("Unknown or duplicate grading parameter")
                names.add(name); weight = number(expected_params[name], "rubric parameter weight")
                score = number(p.get("ai_score"), "parameter score")
                if not equal(number(p.get("weight_percentage"), "parameter weight"), weight) or not 0 <= score <= maximum * weight / 100 + 1e-7:
                    raise ValueError("Parameter score or weight is invalid")
                param_total += score
            if params and not equal(param_total, awarded):
                raise ValueError("Parameter scores must sum to the question score")
        if evidence_invalid:
            # The approved policy publishes valid AI marks immediately. Keep
            # uncertainty visible inside Self Evaluate without changing scores.
            from app.services.evaluation_review import flag_review
            flag_review(row, "Supporting evidence could not be verified against the extracted answer. Inspect the original answer sheet.")
        row.update(max_marks=maximum, ai_awarded_marks=awarded, final_marks=awarded, grace_marks=0)
    rows.sort(key=lambda q: q["question_no"])
    rubric_total = sum(number(r["maxMarks"], "maximum") for r in rules)
    if not equal(rubric_total, number(total_marks, "exam maximum")):
        raise ValueError("Rubric total does not match exam maximum")
    total = sum(q["ai_awarded_marks"] for q in rows)
    result["summary"] = {"total_ai_marks": round(total, 2), "total_max_marks": rubric_total,
        "questions_total": len(rows), "questions_attempted": sum(not q["flags"].get("unanswered", False) for q in rows)}
    return result
