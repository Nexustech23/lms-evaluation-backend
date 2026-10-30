# Faculty rubric preparation

Rubric preparation uses Sonnet 5.5 (`claude-sonnet-5-5`). Answer grading now separately uses the same model with a 90,000-token output cap and explicit high effort. Transcript generation remains on Sonnet 4.5; MCQ-key generation and OCR retain their existing model choices.

Both rubric editors offer **Generate suggestions (uses credits)**. Nothing is generated on page load: the initial GET only loads a cached draft. Faculty can preview source quotations and apply a draft to a single question, edit its fields, and confirm through the existing rubric save workflow. Applying a draft is a local form edit, not a database save. The six generic parameters remain available for manual selection but no longer automatically populate every question.

The production prompt is the concise `SYSTEM_PROMPT` in `app/services/rubric_suggestions.py`. It extracts explicit maxima and CO tags, defaults unspecified minimum scores to zero, and suggests editable parameters plus credit guidelines. It does not infer missing CO tags or invent multiple-CO allocations. Multiple tags require faculty allocation; one eligible tag receives the question maximum. Existing same-set allocations are preserved when their maximum is unchanged.

Phase 2 uses `rubric-preparation-v2`: criteria assess only the requested task,
accept concise equivalent meaning, define full/partial/zero credit, and avoid
overlapping content/clarity deductions. Examples cover K-means K, preprocessing
and discretization with a required example. Incorrect concepts and objective
answers receive no automatic bonus or attempt credit. These are generation
instructions, not evidence of measured model accuracy.

The prompt version participates in the cache fingerprint. After deployment, old
v1 drafts are not reused as v2 drafts; an unsaved form referencing a v1 suggestion
may fail the existing freshness check and require review against current context.
Saved evaluation parameters and historical marks are not rewritten. Generating a
new v2 draft still requires the explicit paid generation action. This parameter
update does not change model choices, token limits or deterministic MCQ scoring.
The separate grading policy update is documented in `answer_grading_model.md`.

## API and execution

- `GET /evaluation-details/{exam_id}/suggestions`: read current-paper draft/status. No spend.
- `POST /evaluation-details/{exam_id}/suggestions`, body `{}`: explicitly request one generation, or reconnect to an existing attempt.
- POST with `{"retry_failed": true}`: explicitly start a new attempt after a confirmed failure. The UI labels this as using credits. No automatic paid retries or model fallback.

Only the owning faculty may generate or read drafts. Generation respects existing paper/rubric locking, the AI rate limit and the institute Claude token budget. Provider attempts and known/unknown usage use the existing faculty ledger. Price estimates include Sonnet 5.5 at the rates verified on 27 October 2026.

Jobs use the existing inline/arq dispatch mechanism. The new `run_rubric_suggestion` wrapper must be deployed with both API and worker updates. MongoDB `rubricSuggestions` records are keyed by a fingerprint of the owner, paper version/content, question count, available covered CO descriptions, model and prompt version. No extra database migration is required: the `_id` index handles uniqueness. Concurrent requests for the same context reuse one attempt, and execution is claimed atomically. Results persist after Redis status expiry because this feature stores its status in MongoDB.

`queued` and `processing` attempts are never automatically restarted. A lost dispatch acknowledgement or process restart can leave such an attempt unresolved. Verify queue/process/provider state and accounting before an operator marks it failed for an explicit retry; do not delete the record to trigger an automatic paid repeat. Cancellation is deliberately not treated as a confirmed failure, since provider work may still be in flight.

The provider uses one bounded structured-output request, with no temperature argument and no automatic SDK retries. The existing SDK passes newer request fields through `extra_body`. Shape and business checks run locally after usage recording. Truncated/refused/invalid output produces a failed suggestion, preserving existing rubric data.

## Source and save guards

The first version supports up to 60 uniquely and consecutively numbered top-level questions (`Q1.`, `1.`, `Question 1.`) and 80,000 extracted characters. Explicit `Q1`/`Question 1` labels take precedence over bare numbered instructions and lists. Plain numbering is used only when no explicit question labels exist; malformed explicit labels still fail rather than silently falling back. Ambiguous numbering, question counts or subpart-only labels require manual correction rather than silent mapping. Suggestions use existing extracted text; they do not cause another OCR call.

Quoted marks and CO tags must belong to their own question block. Maxima need explicit mark notation; minimums need explicit minimum-score evidence and cannot come from pass thresholds. Missing marks or essential context remain unresolved. CO codes outside the exam's covered subject outcomes are shown but cannot be automatically mapped into its rubric.

Cached results are tied to source context and checked again before application. The editor sends `suggestion_source_id` on save after applying a draft; the server rejects stale paper/CO context. The transactional rubric write also checks the paper snapshot. Generated guidelines are now retained and editable in the CO editor as well as the non-CO editor.

## Verification

Deploy the backend and rebuild the frontend together. When Redis/arq mode is used, restart the worker too so its function registry includes `run_rubric_suggestion`. No new dependencies or environment variables are required beyond the existing provider, queue and quota configuration.

Already-graded exams remain locked under the existing rubric policy; this feature does not introduce a way to revise their saved parameters. Use an ungraded exam for the demo. A failed or malformed rubric read must show a reload error in either editor, never an apparently new empty rubric.

`tests/test_rubric_suggestions.py` uses an in-memory database adapter and a blocked network. Provider output is mocked. `scripts/test-rubric-suggestions.mjs` tests form application and CO allocation behavior. These checks do not establish real model accuracy or live SDK compatibility; no paid provider call was made during implementation.
