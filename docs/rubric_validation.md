# Reviewable AI parameter drafts

The provider still generates the full parameter draft. Validation now has two
levels: question identity/order and malformed/nonfinite JSON fail the whole
response; an individual question's unverified marks, invalid percentages, CO
evidence or other row errors flag only that question as needs_clarification.
Other valid rows remain draft_ready. Existing parameters are never overwritten
by flagged rows. Faculty explicitly edits unresolved fields and saves through
the existing marks/CO validation and paper-lock workflow.

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

Responses from failures before this change cannot be recovered if they were not
saved. This change does not automatically repeat those paid requests. Tests
mock providers and storage; no paid generations are used for verification.
