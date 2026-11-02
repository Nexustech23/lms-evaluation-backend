"""Parts inside one question: rubric validation, per-part marking, counted parts, CO allocation, report."""
import pytest

from app.models.answer import validate_questionwise
from app.models.evaluation import validate_evaluation_details
from app.services import question_parts as qp
from app.services.grading import GRADING_POLICY, build_grading_rubric, generate_evaluation_report_html
from app.services.grading_validation import validate_grading


def parts(n=10, marks=2.0):
    labels = ["i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi"]
    return [{"label": f"({labels[i]})", "maxMarks": marks, "guidelines": ""} for i in range(n)]


def test_ten_parts_of_two_marks_make_a_twenty_mark_question():
    clean, groups = qp.validate_parts(parts(), [], 20)
    assert len(clean) == 10 and groups == []


def test_any_ten_of_eleven_parts_is_still_twenty_marks():
    clean, groups = qp.validate_parts(parts(11), [{"id": "g1", "questions": list(range(1, 12)), "answerN": 10}], 20)
    assert groups[0]["answerN"] == 10 and len(clean) == 11


@pytest.mark.parametrize("bad_parts,groups,row_max", [
    (parts(10), [], 22),                                                                    # max must equal the parts' counted marks
    ([parts()[0]], [], 2),                                                                  # at least two parts
    ([{"label": "(i)", "maxMarks": 2}, {"label": "(I)", "maxMarks": 2}], [], 4),            # duplicate label
    ([{"label": "", "maxMarks": 2}, {"label": "(ii)", "maxMarks": 2}], [], 4),
    ([{"label": "(i)", "maxMarks": 0}, {"label": "(ii)", "maxMarks": 2}], [], 2),
    ([{"label": "(i)", "maxMarks": True}, {"label": "(ii)", "maxMarks": 2}], [], 3),
    (parts(3), [{"id": "g", "questions": [1, 2], "answerN": 2}], 6),                        # a group must offer more than it requires
    ("nope", [], 4),
])
def test_invalid_parts_are_rejected(bad_parts, groups, row_max):
    with pytest.raises(ValueError):
        qp.validate_parts(bad_parts, groups, row_max)


def test_part_groups_without_parts_are_rejected():
    with pytest.raises(ValueError):
        qp.validate_parts([], [{"id": "g", "questions": [1, 2], "answerN": 1}], 4)


def row(**extra):
    return {"maxMarks": 20, "minMarks": 0, "guidelines": "", "parameters": [], "cos": [], "parts": parts(), **extra}


def test_rubric_rows_keep_their_parts_and_reject_parameters_alongside():
    saved = validate_evaluation_details([row()])[0]
    assert len(saved["parts"]) == 10 and saved["partGroups"] == []
    with pytest.raises(ValueError, match="cannot also have evaluation parameters"):
        validate_evaluation_details([row(parameters=[{"name": "Accuracy", "percentage": 100, "isCustom": True}])])
    with pytest.raises(ValueError):
        validate_evaluation_details([{**row(), "maxMarks": 18}])
    assert "parts" not in validate_evaluation_details([{"maxMarks": 5}])[0]                 # rows without parts are unchanged


def ai(scores, rows_parts=None):
    rows_parts = rows_parts or parts(len(scores))
    return [{"label": p["label"], "ai_awarded_marks": s, "reasoning": f"r{i}"}
            for i, (p, s) in enumerate(zip(rows_parts, scores), 1)]


def test_question_mark_is_the_sum_of_the_parts():
    stored, awarded = qp.mark_parts(parts(), [], ai([2, 1, 0, 2, 2, 0, 1, 2, 0, 2]))
    assert awarded == 12
    assert stored[0] == {"part_no": 1, "label": "(i)", "max_marks": 2.0, "ai_awarded_marks": 2.0, "reasoning": "r1"}
    assert all("counted" not in p for p in stored)


def test_best_ten_of_eleven_parts_count():
    groups = [{"id": "g1", "questions": list(range(1, 12)), "answerN": 10, "rule": "best"}]
    stored, awarded = qp.mark_parts(parts(11), groups, ai([2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1]))
    assert awarded == 20 and stored[-1]["counted"] is False             # the lowest-scoring part does not count
    stored, awarded = qp.mark_parts(parts(11), groups, ai([2, 2, 0, 2, 2, 2, 2, 2, 2, 2, 2]))
    assert awarded == 20 and stored[2]["counted"] is False


def test_one_of_two_alternatives_counts_the_better_answer():
    two = [{"label": "(ii)", "maxMarks": 2}, {"label": "(ii) OR", "maxMarks": 2}, {"label": "(iii)", "maxMarks": 2}]
    groups = [{"id": "g1", "questions": [1, 2], "answerN": 1, "rule": "best"}]
    stored, awarded = qp.mark_parts(two, groups, ai([0, 2, 1], two))
    assert awarded == 3 and [p.get("counted") for p in stored] == [False, True, None]


@pytest.mark.parametrize("returned", [
    None, [], [{"label": "(i)", "ai_awarded_marks": 1}],                       # missing parts
    ai([1] * 9) + [{"label": "(zz)", "ai_awarded_marks": 1}],                  # unknown label
    ai([1] * 9) + [{"label": "(i)", "ai_awarded_marks": 1}],                   # duplicate label
    ai([3] + [1] * 9),                                                         # above the part maximum
    ai([-1] + [1] * 9),
])
def test_parts_returned_by_the_ai_must_match_the_rubric(returned):
    with pytest.raises(ValueError):
        qp.mark_parts(parts(), [], returned)


def grading_result(question_parts, awarded=0, flags=None):
    return {"questionwise_marking": [{
        "question_no": 1, "max_marks": 20, "ai_awarded_marks": awarded, "evidence": ["ans"], "reasoning": "r",
        "feedback": "f", "improvement": "", "cos": [], "parameters": [], "parts": question_parts,
        "flags": flags or {"needs_review": False, "unanswered": False}}]}


def test_grading_derives_the_question_mark_from_its_parts_even_if_the_ai_total_is_wrong():
    rules = validate_evaluation_details([row()])
    result = validate_grading(grading_result(ai([2, 1, 0, 2, 2, 0, 1, 2, 0, 2]), awarded=20), rules, 20, "ans")
    question = result["questionwise_marking"][0]
    assert question["ai_awarded_marks"] == 12 and question["final_marks"] == 12
    assert len(question["parts"]) == 10 and result["summary"]["total_ai_marks"] == 12


def test_grading_rejects_parts_that_do_not_match_the_rubric():
    rules = validate_evaluation_details([row()])
    with pytest.raises(Exception, match="parts"):
        validate_grading(grading_result(ai([1] * 9)), rules, 20, "ans")


def test_a_question_with_parts_and_a_score_cannot_stay_marked_unanswered():
    rules = validate_evaluation_details([row()])
    flags = {"needs_review": False, "unanswered": True}
    result = validate_grading(grading_result(ai([2] + [0] * 9), flags=flags), rules, 20, "ans")
    assert result["questionwise_marking"][0]["flags"]["unanswered"] is False


def test_part_marks_are_allocated_to_course_outcomes_deterministically():
    cos = [{"co_code": "CO1", "marks": 12.0, "description": ""}, {"co_code": "CO2", "marks": 8.0, "description": ""}]
    rules = validate_evaluation_details([row(cos=cos)])
    result = validate_grading(grading_result(ai([2, 2, 2, 2, 2, 0, 0, 0, 0, 0]), awarded=3), rules, 20, "ans")
    allocated = {c["co_code"]: c["ai_marks"] for c in result["questionwise_marking"][0]["cos"]}
    assert sum(allocated.values()) == pytest.approx(10) and allocated["CO1"] > allocated["CO2"]


def plain(number, marks):
    return {"question_no": number, "max_marks": 20, "ai_awarded_marks": marks, "evidence": ["ans"], "reasoning": "r",
            "feedback": "f", "improvement": "", "cos": [], "parameters": [],
            "flags": {"needs_review": False, "unanswered": False}}


def test_question_parts_work_alongside_a_choice_group_between_questions():
    rules = validate_evaluation_details([row(), {"maxMarks": 20}, {"maxMarks": 20}])
    groups = [{"id": "g", "questions": [2, 3], "answerN": 1, "rule": "best"}]
    result = {"questionwise_marking": [grading_result(ai([2] * 10))["questionwise_marking"][0], plain(2, 5), plain(3, 9)]}
    checked = validate_grading(result, rules, 40, "ans", groups=groups)
    assert checked["summary"]["total_ai_marks"] == 20 + 9


def test_the_ai_is_told_each_part_and_the_policy_is_explicit():
    rule = {"maxMarks": 20, "parts": parts(3), "partGroups": [{"id": "g", "questions": [1, 2], "answerN": 1}]}
    rubric = build_grading_rubric([rule])
    assert "Part (i): maximum 2" in rubric and "answers 1 of parts (i), (ii)" in rubric
    assert "marked part by part" in " ".join(GRADING_POLICY.split())


def test_answer_rows_keep_part_marks():
    good = {"part_no": 2, "label": "(ii)", "max_marks": 2, "ai_awarded_marks": 0, "reasoning": "x",
            "choice_group": {"id": "g", "answer_n": 1}, "counted": False}
    stored = validate_questionwise([{"question_no": 1, "max_marks": 20, "ai_awarded_marks": 12, "flags": {}, "parts": [
        {"part_no": 1, "label": "(i)", "max_marks": 2, "ai_awarded_marks": 2, "reasoning": "ok"}, good]}])[0]
    assert stored["parts"][1]["counted"] is False and stored["parts"][0]["label"] == "(i)"
    bad = {"part_no": 1, "label": "(i)", "max_marks": 2, "ai_awarded_marks": 5}
    with pytest.raises(ValueError):
        validate_questionwise([{"question_no": 1, "max_marks": 20, "ai_awarded_marks": 1, "flags": {}, "parts": [bad]}])


def test_report_lists_the_parts_and_marks_uncounted_ones():
    questions = [{"question_no": 1, "max_marks": 4, "ai_awarded_marks": 2, "final_marks": 2, "flags": {}, "reasoning": "r",
                  "parts": [{"label": "(i)", "max_marks": 2, "ai_awarded_marks": 2, "reasoning": "good", "counted": True},
                            {"label": "(ii)", "max_marks": 2, "ai_awarded_marks": 0, "reasoning": "none", "counted": False}]}]
    html = generate_evaluation_report_html({"questionwise_marking": questions, "summary": {}}, "Student", total_max_marks=4)
    assert "Part-wise Marks" in html and "(not counted)" in html and "good" in html
