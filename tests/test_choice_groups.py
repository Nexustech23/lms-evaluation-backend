"""Choice groups ("answer N of these"): validation, counted maximum, best-N counting, totals."""
import pytest

from app.models.answer import update_answer_evaluation_document, validate_questionwise
from app.models.evaluation import _validate_total_marks, validate_evaluation_details
from app.schemas.evaluation import SaveEvaluationDetailsRequest
from app.services import choice_groups as cg
from app.services.grading import build_grading_rubric, generate_evaluation_report_html
from app.services.grading_validation import validate_grading
from app.services.manual_marks import prepare_manual_marks
from app.services.review_marks import validate_review_marks


def rows(*maxima):
    return [{"maxMarks": m, "minMarks": 0, "guidelines": "", "parameters": [], "cos": []} for m in maxima]


# Pharmaceutical Analysis: Q1 20, Group B 3 x 10 (answer 2), Group C 9 x 5 (answer 7) = 20 + 20 + 35 = 75
PHARMA = rows(20, 10, 10, 10, *([5] * 9))
PHARMA_GROUPS = [{"id": "B", "questions": [2, 3, 4], "answerN": 2}, {"id": "C", "questions": list(range(5, 14)), "answerN": 7}]


def test_groups_are_validated_and_normalised():
    groups = cg.validate_groups(PHARMA_GROUPS, PHARMA)
    assert [g["id"] for g in groups] == ["B", "C"] and all(g["rule"] == "best" for g in groups)
    assert cg.validate_groups(None, PHARMA) == [] and cg.validate_groups([], PHARMA) == []


@pytest.mark.parametrize("bad", [
    [{"id": "x", "questions": [2], "answerN": 1}],                                   # one question is no choice
    [{"id": "x", "questions": [2, 3], "answerN": 2}],                                # must answer fewer than offered
    [{"id": "x", "questions": [2, 3], "answerN": 0}],
    [{"id": "x", "questions": [2, 3], "answerN": True}],
    [{"id": "x", "questions": [2, 99], "answerN": 1}],                               # not in the rubric
    [{"id": "x", "questions": [2, 2], "answerN": 1}],
    [{"id": "x", "questions": [1, 2], "answerN": 1}],                                # different maxima (20 vs 10)
    [{"id": "x", "questions": [2, 3], "answerN": 1}, {"id": "y", "questions": [3, 4], "answerN": 1}],   # overlap
    [{"id": "x", "questions": [2, 3], "answerN": 1}, {"id": "x", "questions": [4, 5], "answerN": 1}],   # duplicate id
    [{"id": "", "questions": [2, 3], "answerN": 1}],
    [{"id": "x", "questions": [2, 3], "answerN": 1, "rule": "first"}],
    "nope",
])
def test_invalid_groups_are_rejected(bad):
    with pytest.raises(ValueError):
        cg.validate_groups(bad, PHARMA)


def test_counted_maximum_is_the_real_paper_total():
    groups = cg.validate_groups(PHARMA_GROUPS, PHARMA)
    assert cg.counted_max(PHARMA, groups) == 75
    assert sum(r["maxMarks"] for r in PHARMA) == 95
    assert cg.counted_max(PHARMA, []) == 95


def test_rubric_total_check_uses_the_counted_maximum():
    groups = cg.validate_groups(PHARMA_GROUPS, PHARMA)
    details = validate_evaluation_details(PHARMA)
    assert _validate_total_marks(75, details, groups) == 75
    with pytest.raises(ValueError, match="counted marks"):
        _validate_total_marks(95, details, groups)
    assert _validate_total_marks(95, details) == 95          # no groups: unchanged behaviour
    with pytest.raises(ValueError):
        _validate_total_marks(75, details)


def test_request_schema_accepts_optional_groups():
    request = SaveEvaluationDetailsRequest(questionEvaluationDetails=PHARMA, totalMarks=75, choiceGroups=PHARMA_GROUPS)
    assert request.choiceGroups == PHARMA_GROUPS
    assert SaveEvaluationDetailsRequest(questionEvaluationDetails=PHARMA, totalMarks=95).choiceGroups is None


def marked(scores, maxima):
    return [{"question_no": i, "max_marks": m, "ai_awarded_marks": s, "final_marks": s, "flags": {"needs_review": False}}
            for i, (s, m) in enumerate(zip(scores, maxima), 1)]


def test_best_answers_count_and_ties_go_to_the_earlier_question():
    groups = cg.validate_groups([{"id": "g", "questions": [2, 3, 4], "answerN": 2}], rows(5, 10, 10, 10))
    questions = marked([5, 4, 9, 4], [5, 10, 10, 10])
    cg.attach_groups(questions, groups); cg.reselect_counted(questions)
    assert [q.get("counted") for q in questions] == [None, True, True, False]        # 9 and the earlier 4 count
    assert cg.counted_sum(questions, "final_marks") == 5 + 4 + 9


def test_fewer_answers_than_asked_is_not_an_error():
    groups = cg.validate_groups([{"id": "g", "questions": [1, 2, 3], "answerN": 2}], rows(5, 5, 5))
    questions = marked([3, 0, 0], [5, 5, 5])
    questions[1]["flags"]["unanswered"] = True; questions[2]["flags"]["unanswered"] = True
    cg.attach_groups(questions, groups); cg.reselect_counted(questions)
    assert sum(q["counted"] for q in questions) == 2
    assert cg.counted_sum(questions, "final_marks") == 3
    assert cg.counted_sum(questions, "max_marks") == 10                              # still out of 2 x 5


def test_reselecting_after_a_faculty_change_moves_the_counted_answer():
    groups = cg.validate_groups([{"id": "g", "questions": [1, 2], "answerN": 1}], rows(10, 10))
    questions = marked([4, 6], [10, 10])
    cg.attach_groups(questions, groups); cg.reselect_counted(questions)
    assert [q["counted"] for q in questions] == [False, True]
    questions[0]["final_marks"] = 8
    cg.reselect_counted(questions)
    assert [q["counted"] for q in questions] == [True, False]


def test_ungrouped_questions_are_always_counted():
    assert cg.is_counted({"final_marks": 3}) and not cg.is_counted({"counted": False})
    assert cg.counted_sum([{"final_marks": 3}, {"final_marks": 4, "counted": False}], "final_marks") == 3


def grading_result(scores, maxima):
    return {"questionwise_marking": [
        {"question_no": i, "max_marks": m, "ai_awarded_marks": s, "evidence": ["ans"] if s else [], "reasoning": "r",
         "feedback": "f", "improvement": "", "cos": [], "parameters": [],
         "flags": {"needs_review": False, "unanswered": s == 0}}
        for i, (s, m) in enumerate(zip(scores, maxima), 1)]}


def test_grading_validation_counts_only_the_best_answers():
    rules = rows(20, 10, 10, 10)
    groups = cg.validate_groups([{"id": "B", "questions": [2, 3, 4], "answerN": 2}], rules)
    result = validate_grading(grading_result([15, 8, 0, 6], [20, 10, 10, 10]), rules, 40, "ans", groups=groups)
    assert result["summary"]["total_max_marks"] == 40
    assert result["summary"]["total_ai_marks"] == 15 + 8 + 6
    by_no = {q["question_no"]: q for q in result["questionwise_marking"]}
    assert by_no[3]["counted"] is False and by_no[2]["counted"] and by_no[4]["counted"]
    assert by_no[2]["choice_group"] == {"id": "B", "answer_n": 2} and "choice_group" not in by_no[1]


def test_grading_validation_without_groups_is_unchanged():
    rules = rows(20, 10)
    result = validate_grading(grading_result([15, 8], [20, 10]), rules, 30, "ans")
    assert result["summary"]["total_ai_marks"] == 23 and result["summary"]["total_max_marks"] == 30
    assert all("counted" not in q and "choice_group" not in q for q in result["questionwise_marking"])


def test_grading_validation_rejects_a_total_that_ignores_the_groups():
    rules = rows(20, 10, 10, 10)
    groups = cg.validate_groups([{"id": "B", "questions": [2, 3, 4], "answerN": 2}], rules)
    with pytest.raises(Exception):
        validate_grading(grading_result([15, 8, 0, 6], [20, 10, 10, 10]), rules, 50, "ans", groups=groups)


def test_answer_model_keeps_the_group_labels_and_totals_count_only_counted():
    questions = [
        {"question_no": 1, "max_marks": 10, "ai_awarded_marks": 8, "flags": {}, "choice_group": {"id": "g", "answer_n": 1}, "counted": True},
        {"question_no": 2, "max_marks": 10, "ai_awarded_marks": 3, "flags": {}, "choice_group": {"id": "g", "answer_n": 1}, "counted": False},
    ]
    validated = validate_questionwise(questions)
    assert validated[0]["counted"] is True and validated[1]["counted"] is False
    assert validated[1]["choice_group"] == {"id": "g", "answer_n": 1}
    update = update_answer_evaluation_document({"questionwise_marking": questions})
    fields = update.get("$set", update)
    assert fields["total_final_marks"] == 8 and fields["total_max_marks"] == 10


def test_ai_is_told_about_the_choice_group_and_the_policy_is_explicit():
    rubric = build_grading_rubric([{"maxMarks": 10, "choice_group": {"id": "B", "answer_n": 2}}, {"maxMarks": 5}])
    assert "Choice group B" in rubric and "answers 2 of" in rubric
    from app.services.grading import GRADING_POLICY
    assert "explicit selection policy" in " ".join(GRADING_POLICY.split())


def test_report_shows_not_counted_and_totals_only_counted():
    questions = [
        {"question_no": 1, "max_marks": 10, "ai_awarded_marks": 8, "final_marks": 8, "flags": {}, "counted": True},
        {"question_no": 2, "max_marks": 10, "ai_awarded_marks": 3, "final_marks": 3, "flags": {}, "counted": False},
    ]
    html = generate_evaluation_report_html({"questionwise_marking": questions, "summary": {}}, "Student", total_max_marks=10)
    assert "Not counted (choice)" in html
    assert "8.0 / 10.0" in html or "80.0" in html or "80%" in html


def test_manual_marks_count_the_best_answers_and_use_the_counted_maximum():
    class Payload:
        entries = [{"student_id": "s1", "question_marks": [{"question_no": 1, "marks": 4}, {"question_no": 2, "marks": 9}, {"question_no": 3, "marks": 2}]}]
        max_marks = None
    evaluation = {"questionEvaluationDetails": rows(10, 10, 10), "totalMarks": 20,
                  "choiceGroups": [{"id": "g", "questions": [1, 2, 3], "answerN": 2, "rule": "best"}]}
    prepared = prepare_manual_marks(Payload, evaluation)[0]
    assert prepared["total_max_marks"] == 20 and prepared["total_final_marks"] == 13
    assert [q["counted"] for q in prepared["questionwise_marking"]] == [True, True, False]
