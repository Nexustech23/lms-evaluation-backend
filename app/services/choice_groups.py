"""Choice groups: "answer N of these questions" and which answers count.

A rubric may declare groups of separate questions the student chooses between (for
example Group C "answer any 7 of 9", or Q4 and Q5 "answer 1 of 2"). The AI marks every
row the student attempted exactly as if it were required; the application then counts
the N highest-scoring rows of each group ("best N"; a tie goes to the earlier question).
Rows outside any group are always counted, so a rubric without groups behaves as before.

Rubric document field `choiceGroups`: [{"id": "g1", "questions": [4, 5], "answerN": 1, "rule": "best"}]
where `questions` are 1-based rubric row numbers. Every row of a group must carry the same
maximum, so a group's counted maximum is answerN x that maximum. Pure functions, no I/O.
"""
RULE_BEST = "best"


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def validate_groups(groups, rows):
    """Return normalised groups, or raise ValueError. `rows` are validated rubric rows (with maxMarks)."""
    if groups in (None, []):
        return []
    if not isinstance(groups, list):
        raise ValueError("choiceGroups must be a list")
    seen_ids, seen_questions, clean = set(), set(), []
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("Each choice group must be an object")
        group_id = group.get("id")
        if not isinstance(group_id, str) or not group_id.strip():
            raise ValueError("Each choice group needs an id")
        group_id = group_id.strip()
        if group_id in seen_ids:
            raise ValueError(f"Choice group id {group_id!r} is used twice")
        seen_ids.add(group_id)
        questions = group.get("questions")
        if (not isinstance(questions, list) or len(questions) < 2
                or any(not _is_int(q) for q in questions) or len(set(questions)) != len(questions)):
            raise ValueError(f"Choice group {group_id!r} needs at least two different questions")
        if any(not 1 <= q <= len(rows) for q in questions):
            raise ValueError(f"Choice group {group_id!r} names a question that is not in the rubric")
        if seen_questions.intersection(questions):
            raise ValueError("A question can belong to only one choice group")
        seen_questions.update(questions)
        answer_n = group.get("answerN")
        if not _is_int(answer_n) or not 1 <= answer_n < len(questions):
            raise ValueError(f"Choice group {group_id!r}: answerN must be at least 1 and fewer than its questions")
        rule = group.get("rule", RULE_BEST)
        if rule != RULE_BEST:
            raise ValueError(f"Choice group {group_id!r}: only the 'best' counting rule is supported")
        maxima = {float(rows[q - 1]["maxMarks"]) for q in questions}
        if len(maxima) != 1:
            raise ValueError(f"Choice group {group_id!r}: every question in a group must have the same maximum marks")
        clean.append({"id": group_id, "questions": sorted(questions), "answerN": answer_n, "rule": rule})
    return clean


def counted_max(rows, groups):
    """Maximum the student can earn: required rows plus answerN x marks for each group."""
    grouped = {q for g in (groups or []) for q in g["questions"]}
    total = sum(float(r["maxMarks"]) for i, r in enumerate(rows, 1) if i not in grouped)
    for g in groups or []:
        total += g["answerN"] * float(rows[g["questions"][0] - 1]["maxMarks"])
    return round(total, 6)


def group_for_rows(groups):
    """{row number: {"id", "answer_n"}} for rows that belong to a group."""
    return {q: {"id": g["id"], "answer_n": g["answerN"]} for g in groups or [] for q in g["questions"]}


def attach_groups(questions, groups):
    """Label each marked question that belongs to a group with its group (in place)."""
    labels = group_for_rows(groups)
    for q in questions:
        label = labels.get(q.get("question_no"))
        if label:
            q["choice_group"] = dict(label)
    return questions


def _score(question):
    value = question.get("final_marks")
    if value is None:
        value = question.get("ai_awarded_marks", 0)
    return float(value or 0)


def reselect_counted(questions):
    """Set `counted` on every grouped question: the best answer_n of its group count (in place).

    Safe to call again after marks change. Questions without a `choice_group` are left
    untouched (an absent `counted` means counted)."""
    by_group = {}
    for q in questions:
        group = q.get("choice_group")
        if group:
            by_group.setdefault(group["id"], []).append(q)
    for members in by_group.values():
        keep = members[0]["choice_group"]["answer_n"]
        ranked = sorted(members, key=lambda q: (-_score(q), q.get("question_no", 0)))
        winners = {id(q) for q in ranked[:keep]}
        for q in members:
            q["counted"] = id(q) in winners
    return questions


def is_counted(question):
    return question.get("counted", True) is not False


def counted_sum(questions, field):
    """Sum a marks field over counted questions only."""
    total = 0.0
    for q in questions:
        if is_counted(q):
            value = q.get(field)
            if value is None and field == "final_marks":
                value = q.get("ai_awarded_marks", 0)
            if value is not None:
                total += float(value)    # a non-finite value must still surface, as with a plain sum
    return total
