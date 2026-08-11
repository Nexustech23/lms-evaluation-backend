# LMS Fast API

FastAPI rewrite of the `lms-evaluation-backend` Flask API.

**Ported so far:**
- **Phase 1** — auth, roles, profile, contact, institute hierarchy (school →
  programme/department → batch → subject).
- **Phase 2** — exams/folders, evaluation rubrics, answer sheets.
- **Phase 3a** — AI Tutor: homework help & notes generation
  (`/api/ai-tutor/...`).
- **Phase 3b** — AI question-paper generation, full CRUD, and the
  HTML-editor-to-docx save flow (`/question-paper/...`).
- **Phase 3c** — the matplotlib/schemdraw diagram-rendering engine
  (`app/services/diagram_render.py`): `<<<DIAGRAM>>>` blocks in generated
  papers embed as real images (or a native docx table for `data_table`),
  and `POST /question-paper/render-diagram` renders a single spec for the
  editor's live preview. Security fix vs. Flask: the `graph` type's
  per-curve expression is evaluated with `asteval.Interpreter` (AST-level
  sandboxing) instead of Python's builtin `eval()`.
- **Phase 4** — AI answer-script grading (`POST /evaluate-answer-script`):
  Gemini OCR of the student's scanned answer → Claude grading against the
  evaluation rubric → an optional AI-generated transcript → both rendered to
  PDF and uploaded to ImageKit, with server-recomputed marks and per-CO
  results written back to `answerDetails`.
- **Phase 5a** — bulk Excel marks import (`POST /import-marks-excel`, fuzzy
  header-alias matching, replace-not-append per batch/semester) and
  AI-generated mock tests (`/mock-tests/...`, Claude `claude-haiku-4-5`)
  with locally-graded student attempts.
- **Phase 5b** — relative grading configuration (`/relative-grading...`),
  per-subject results (`/subject/result/{id}`, weighted composite scoring
  across an exam's exams by `weightage`, curved into a course grade),
  combined semester results (`/combined-result...`, imported-marks vs.
  exam-derived aggregation, Excel export, and an inline-HTML print view),
  and the single-exam detailed per-question/per-CO Excel report
  (`/download-detailed-excel/{folder_id}`).
- **Faculty Materials** — publish/list notes-assignments-tests
  (`/faculty/materials`), student-side listing filtered by enrolled
  subjects and progress tracking (`/student/materials...`).
- **Student↔Subject linking** — `/link-student-subjects`,
  `/student-subjects`, `/student-academic-filters`, `/student-groups`,
  `/enrolled-students`, `/student-enrolled-subjects`. Feeds Faculty
  Materials' student-side filtering.
- **Phase 5c** — academic transcripts (`/transcripts/...`, all 10 Flask
  endpoints: CGPA/grade-point computation, Excel import, PDF generation).
  Was already complete before this pass — this README previously listed it
  under "not ported yet" in error; corrected here.
- **Phase 6a** — faculty subject-filter endpoints: `/faculty/filter-data`
  (cascading school→programme→department→batch→semester filter-option
  lists), `/subjects/faculty` and `/subjects/institute` (paginated,
  searchable subject listings, faculty-scoped vs. institute-scoped).
- **Phase 6b** — CO-PO attainment Excel report
  (`GET /co-detailed-excel/{subject_id}`): a 5-sheet openpyxl workbook
  (per-exam grids, Direct Attainment, CO Attainment, CO-PO Matrix) with
  live cross-sheet Excel formulas, not just static computed values —
  ported into its own module, `app/services/co_excel.py`, since it's
  ~900 lines of workbook-construction logic.
- **Phase 7 — Pomodoro** (`/api/pomodoro/...`, 10 endpoints): AI-driven,
  AI-assisted (file upload), and custom focus-session modes; Claude
  generates study notes + section quizzes, Gemini vision OCRs handwritten
  answer photos during grading. Async generation uses the shared
  `job_store.py` (Redis) + `BackgroundTasks` in place of Flask's raw
  `Thread` + in-memory job dict.
- **Phase 8 — Roadmap** (`/api/self-learner/roadmap/...`, 10 endpoints):
  Claude generates a full 4-stage curriculum as a background job; Gemini
  generates on-demand, cached subtopic notes and stage quizzes; backend-only
  grading with a level-unlock progression system and per-user Claude/Gemini
  token tracking.

**Not ported (by design):** the MongoDB-backed "v1" AI Tutor history CRUD
(`controllers/institute/ai_tutor_controller.py`'s `/get-all` `/get/<id>`
`/update/<id>` `/delete/<id>`) has no frontend caller — confirmed via the
frontend's hardcoded absolute URLs, which call the different,
already-ported `homework_help_controller.py` / `notes_generate_controller.py`
implementation instead. Left unported as dead code, not a gap.

**Known Flask bugs ported as-is** (intentionally, for behavioral parity —
see inline `# NOTE:` comments at each site rather than fixed silently):
- `GET /ai-tutor/get-all|get|update|delete` — not applicable, see above.
- `GET /faculty/filter-data`, `/subjects/faculty` — no institute/ownership
  scoping beyond the caller's own faculty record (matches Flask).
- `GET /subjects/faculty` — mislabels `department.code` /
  `programme.programme_code` as `department_name` / `programme_name`;
  `/subjects/institute` returns the real name fields correctly.
- `GET /co-detailed-excel/{subject_id}` — no institute/subject ownership
  check; `subject_id` is trusted as-is from the URL.
- Pomodoro `GET /session/{id}/evaluation` — the cached-or-generate check
  isn't atomic; two concurrent first-time requests could both trigger AI
  evaluation before either write lands.
- Roadmap `POST /{id}/quiz/submit` — the actual pass threshold is `>=50%`,
  not the `>=70%` stated in Flask's own route docstring.
- Roadmap `progress.streakDays` — increments once per *passed quiz*, not
  once per calendar day, despite the name.
- Roadmap `GET /status/{job_id}` and Pomodoro `GET /job/{job_id}` — job
  polling isn't scoped by user; any authenticated caller who knows/guesses
  a job_id can poll it.

**Still open:** no automated test suite exists anywhere in this project
(no `tests/` dir, no pytest config) — worth addressing before treating any
phase as production-hardened.

## Setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# edit .env with real credentials (MongoDB, JWT secret, Gemini, Anthropic, ImageKit, Redis)
```

**Playwright** (used to render homework-help/notes-generation PDFs) needs a
one-time browser install that `pip install` does not do automatically:

```bash
playwright install chromium
```

**Legacy `.doc` extraction** (question-paper question-bank/course-planner
uploads) falls back to a LibreOffice (`soffice`) conversion if installed —
not required for the common formats (pdf/docx/pptx/xlsx/txt/csv/md/odt).

## Run

```bash
uvicorn app.main:app --reload --port 5050
```

Interactive API docs: http://localhost:5050/docs

## Design notes

- **API-compatible with the existing Flask backend**: same URL paths (mounted
  at root, no `/api` prefix — except the AI Tutor, Pomodoro, and Roadmap
  routes, which the frontend calls directly at `/api/ai-tutor/...`,
  `/api/pomodoro/...`, and `/api/self-learner/roadmap/...` respectively via
  hardcoded backend URLs, bypassing the Next.js rewrite), same JSON response
  shapes, same
  JWT-in-httponly-cookie login flow (`access_token_cookie`), so the existing
  Next.js frontend in `LMS main` could point at this backend later without
  frontend changes.
- **Async Motor** driver for MongoDB throughout; async Redis (`redis.asyncio`)
  for job-status tracking.
- **Security/consistency fixes vs. the Flask original**, applied while
  porting: the JWT cookie's `secure` flag is environment-conditional (on in
  production) and CSRF protection is enabled; `POST /create_role` requires an
  authenticated superadmin; the AI Tutor endpoints require authentication
  (Flask had none); `GET /question-paper/{id}` and
  `POST /evaluate-answer-script` are scoped to the calling faculty (Flask's
  versions had no ownership check at all); `GET /mock-tests/{id}` (used to
  fetch a test for the student to take) redacts `correct_answer`/
  `explanation` from each question — Flask returned them as-is, inspectable
  via dev tools before submitting.
- Background jobs (AI extraction/generation/grading pipelines) run as
  FastAPI `BackgroundTasks` rather than Flask's raw `Thread`s, with blocking
  calls (Gemini/Claude/Playwright/ImageKit SDKs) wrapped in
  `asyncio.to_thread`, and Flask's `ThreadPoolExecutor(max_workers=2)` pairs
  (grading job's parallel evaluation/transcript generation, PDF renders, and
  uploads) become `asyncio.gather`. Job status for the AI Tutor,
  question-paper generation, and grading features is tracked through one
  shared `app/services/job_store.py` (Redis), replacing five near-identical
  `_set_job`/`_update_job`/`_get_job` implementations duplicated across the
  original Flask codebase.
- **PDF rendering** uses Playwright/Chromium (`app/services/pdf_render.py`,
  added in Phase 3a) throughout, including for the grading pipeline's
  evaluation report and transcript PDFs — Flask used `wkhtmltopdf`/`pdfkit`
  for those specifically; reusing the renderer already required for AI Tutor
  avoids a second PDF engine and system-level binary dependency.
