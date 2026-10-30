# Reviewable AI parameter drafts

The provider still generates the full parameter draft. Validation now has two
levels: question identity/order and malformed/nonfinite JSON fail the whole
response; an individual question's unverified marks, invalid percentages, CO
evidence or other row errors flag only that question as needs_clarification.
Other valid rows remain draft_ready. Existing parameters are never overwritten
by flagged rows. Faculty explicitly edits unresolved fields and saves through
the existing marks/CO validation and paper-lock workflow.

Generation uses at most six questions per sequential request, adaptive thinking
with medium effort, and a 30,000-token output cap per request. The cap is an upper
bound, not a target or an estimate of actual usage. Guidelines are normally
30-80 words and shorter for simple questions; missing data is never expanded into
invented content. Batch payloads include only the selected questions and printed
CO definitions relevant to those questions. With no printed CO tags, extract_co_tags
is false and the expected CO list is empty. Unprinted marks remain unconfirmed.

Supported maxima include explicit Marks labels, conventional trailing [5] or
(5) tokens (with optional CO tags), and numbers in a Markdown table's explicitly
labelled Marks column. Arbitrary numbers in prose, minimum/pass thresholds,
unquoted values and numbers from other questions remain unverified. Flattened
tables with no reliable column evidence stay review cases rather than guessed
allocations. No question-mark sums or optional-attempt rules are invented.

Flagged rows include a question-specific explanation and, when the schema is
valid, a review_draft containing the proposed values. This is informational,
never automatically applied. The frontend displays expandable review details.

The job stores raw_response and response_saved_at before validating output.
Raw text is not returned by the public job serializer. A failed-attempt retry
reuses that stored response without an AI call, API key requirement or token
budget check. An explicit API request with retry_failed=true and regenerate=true
opts into a fresh paid generation and normal budget checks. Reading suggestions
does not queue jobs. Source fingerprints, ownership and locking still apply.
validation_version tracks validation changes independently of the source/model
prompt fingerprint, permitting failed stored outputs to be revalidated.

Each complete batch response is saved before validation in batch_responses,
keyed by its global question indices. Failed-attempt retries reuse those exact
responses and call the provider only for unfinished batches, after budget and
source checks. A fully cached response needs no provider key or token budget.
Truncated/refused output is retained separately in incomplete_responses with its
stop reason for diagnosis, never accepted as a completed batch. There are no
automatic paid retries. completed_questions/completed_batches/total_batches
report progress. All calls still pass through the per-call usage ledger.

Every uncached batch checks the exam owner, paper/source fingerprint, edit lock
and remaining AI budget. The merged response is validated against the full
question order and current paper before publication. No partial batch result is
saved into evaluationDetails or student marks. Faculty explicitly retries an
interrupted job; completed batch artifacts remain available even on failure.

Responses from failures before this change cannot be recovered if they were not
saved. This change does not automatically repeat those paid requests. Tests
mock providers and storage; no paid generations are used for verification.
