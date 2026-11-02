# Choice groups: "answer N of these questions"

Some papers let the student choose, for example "answer any 7 of 9" or "answer one of Q4 and Q5". A rubric
can now declare such groups so the maximum, the totals and the counted answers match the paper.

## Rubric field

`choiceGroups` on the saved rubric (`evaluationDetails`) and in `POST /evaluation-details/{exam}`:

```json
{"id": "g1", "questions": [4, 5], "answerN": 1, "rule": "best"}
```

- `questions` are 1-based rubric row numbers. Each printed question is its own row.
- `answerN` is how many the student answers: at least 1 and fewer than the questions listed.
- `rule` is `best` (the only rule): the highest-scoring `answerN` answers count. A tie goes to the earlier question.
- A question belongs to at most one group, and every question in a group has the same maximum marks.
- Rows outside any group are required. A rubric without `choiceGroups` behaves exactly as before.

## Totals

The maximum a student can earn is the required rows plus `answerN x marks` for each group. `totalMarks` must equal
that counted maximum (for example 75 for a paper of 95 offered marks with "any 2 of 3" and "any 7 of 9").

## Marking

The AI marks every attempted row exactly as if it were required; an unattempted row is unanswered with zero. The
grading prompt (`grading-evidence-v5-choice-groups`) states the selection policy, so choices alone no longer put
review flags on questions. After marking, the application sets `counted` on each grouped question and
`choice_group: {id, answer_n}`. `total_ai_marks`, `total_final_marks` and `total_max_marks` use counted questions
only. Selection is repeated when marks change (faculty review, manual marks), so a faculty adjustment can move which
answer counts.

## Compatibility

Results saved before this change have no `counted` field; a missing `counted` means counted. Exams whose rubric has
no groups are unchanged. A rubric with groups cannot be applied to an exam that is already evaluated (the rubric is
locked once marking starts); use a new exam.

## Not included

Per-sheet manual override of which answers count, and suggesting groups from the paper text, are separate steps.
`co_excel` CO attainment still reads every question's CO marks, including answers that do not count.

## Parts inside a question

A question can also carry **parts**, for example Q1 with ten parts of 2 marks. A rubric row gets
`parts: [{label, maxMarks, guidelines}]` and optional `partGroups` (the same "answer N of these" groups, here
between parts: "any 10 of 11 parts", or "one of the two (ii) alternatives"). The row's `maxMarks` must equal the
counted maximum of its parts, and a row with parts has no evaluation parameters (each part has its own guidelines).

The grader returns a mark and a short reasoning for every part (`grading-evidence-v6-question-parts`). The
application picks the counted parts (best N of each part group, a tie to the earlier part) and sets the question's
mark to their sum; the AI's own question total is ignored. Course-outcome marks for such a question are allocated
from the part marks, not by the AI. Part marks are stored on the answer (`parts`) and shown in the report; faculty
adjust the question total, not individual parts. Rows without parts behave exactly as before.
