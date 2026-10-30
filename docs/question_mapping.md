# Question mapping for evaluation parameters

Printed question labels no longer have to be consecutive. The parser recognises
Q1, Question 7, Q.1, 1(i), 8 (a), table rows, and numbering restarted in a named
Section or Part. Internal `question_no` values remain 1..N in rubric order.
Original labels, section, stable source IDs and exact question blocks are saved
with new rubrics and supplied to grading. Existing rubrics remain compatible.

The exam's configured question count determines the number of evaluation rows.
A root such as 1(i)..1(x) can be one row or several, depending on that count.
Parent question text and subparts are never automatically counted twice.
The parser does not infer an optional-answer policy or change maximum marks.

If the layout is ambiguous, suggestions return HTTP 422 with
`detail.code = QUESTION_MAPPING_REQUIRED`, the source fingerprint, original
nonblank source lines and detected candidates. The frontend displays a question
mapping form. Faculty chooses exactly N starting lines in source order and
confirms each printed label. This confirmation itself uses no AI credits. Papers
with unrecognised labels can use the same form. If OCR has merged several
questions onto a single line, its extraction must first be corrected; automatic
splitting would not establish reliable question boundaries.

POST `/evaluation-details/{exam_id}/question-mapping` accepts `source_id` and
`questions: [{start, question_label}]`. Starts are Python source character
offsets returned by the server, not client-computed UTF-16 offsets. The route
requires exam ownership, rejects locked exams, validates source and boundaries,
and atomically compares the complete question-paper snapshot before writing.
Mappings are scoped to text, version, URL, file ID and question count. Changed
source invalidates them and generated drafts. Confirmation does not create an
AI job; Generate parameters remains explicit.

Saving a suggestion-backed rubric attaches the server-verified mapping and
rejects mismatched row counts or identity. Marks, CO evidence, percentages,
paper locking, grading output validation and explicit save remain enforced.
The grading prompt receives the printed labels and source blocks but returns
integer rubric IDs. Noncanonical mappings do not use the legacy integer-keyed
MCQ cache or OCR override, which could otherwise attach a key to the wrong row.
Objective answers whose key cannot be established remain faculty-review cases
under the existing grading policy.

Deployment requires both backend and frontend updates. No data migration or
paid AI calls are needed to test the mapping workflow. Automated tests use
synthetic paper text and mock database/provider calls.
