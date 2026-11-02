# Answer-sheet grading model

Answer checking uses `claude-sonnet-5-5` with `max_tokens=90000` (raised from 30000 after a 38-question, fully answered sheet exhausted the old cap), adaptive thinking and explicit `high` effort. Configuration is in `app/services/grading.py`. Phase 3 updates the grading prompt to `grading-evidence-v3`; the JSON contract remains unchanged.

The v3 policy explicitly awards full allocated credit for concise, complete
equivalent meaning. It requires academic reasons for deductions and component
credit for valid incomplete understanding, separates optional improvements from
deductions, and prohibits repeated deductions for one omission. Distinct missing
requirements retain their own consequences. Explicit faculty-approved credit
levels remain binding; excessive or overlapping saved criteria require review,
not silent replacement. There is no automatic bonus, rounding up or matching of
historical teacher totals. Deterministic MCQ scoring is unchanged.

This prompt update does not regenerate existing rubrics or re-evaluate saved
marks. Its version identifies future evaluations after deployment. Offline
contract checks cannot demonstrate an accuracy increase; a representative,
faculty-reviewed comparison needs separate authorization for provider calls.

The cap covers thinking plus the visible grading JSON. It is a ceiling, not a guaranteed usage amount or accuracy improvement. Review actual completion rates and token usage on representative scripts before adjusting it. Thinking tokens are included in the provider's output-token usage and therefore in the existing usage ledger.

`app/services/claude.py` streams requests with explicit effort, reads the complete response, extracts text blocks and rejects truncated/refused responses. New thinking/effort fields use `extra_body` for compatibility with the pinned SDK. Callers without effort retain their existing request path. There is no added retry or model fallback.

Transcript generation has its own model constant (`claude-sonnet-4-5`) and retains its 18,000-token cap. Its success and failure usage metadata use the transcript model. OCR, MCQ-key generation and publication/review policy are unchanged.

Migration reference: [Anthropic's Sonnet 5.5 migration guide](https://platform.claude.com/docs/en/models/sonnet-5-5/migration-guide).

Offline verification: `tests/test_grading_model_config.py` covers request configuration, transcript separation, SDK request serialization/stream parsing, thinking-block filtering, usage preservation and rejection of incomplete output. Network connections are blocked and provider responses are mocked. These checks do not measure grading accuracy or verify live API behavior.

## Detailed reasoning (grading-evidence-v4-detailed-reasoning)

The grading prompt now asks for a per-question breakdown in `reasoning`: for each part or criterion, what the student wrote, whether it is correct and why, the marks awarded and the marks lost. The sentence limits apply to `feedback` and `improvement` only, which stay short. The JSON contract, `max_tokens=90000` and the effort level are unchanged. Longer reasoning increases output tokens (estimated 30-50% more per sheet). Results saved earlier keep `grading-evidence-v3`; only new evaluations use the new prompt.

## Image resolution for OCR

Scanned answer pages are sent to Gemini as they were scanned (typically about 100 dpi). The application does not resample them, and code cannot add detail that is not in a scan. `GEMINI_OCR_MEDIA_RESOLUTION` (default `high`) asks Gemini to read each page at higher image detail, about 4x the tokens per page of the default. It read handwritten maths better in testing. Set it to `default` to restore the old behaviour.

Question papers are typed PDFs, so `PAPER_EXTRACT_RENDER_DPI` (default `0`, off: the PDF is sent as before) can render each page to an image at that dpi, for example `300`, and read it at high resolution. It is off by default because rendering at the default resolution produced worse text in testing. Enable it on the server with `PAPER_EXTRACT_RENDER_DPI=300` and check a few papers against their PDFs. Rescanning answer sheets at 200-300 dpi remains the best way to improve handwriting accuracy.

## Question-paper extraction model

The model that reads a question paper into text is `GEMINI_PAPER_MODEL` (default `gemini-3.1-flash-lite`; it was `gemini-2.5-flash`). Set it in `.env` to try another Gemini model and restart the backend; use a model that has a price in `app/utils/ai_pricing.py` so its cost is recorded. Only paper extraction uses it: the tutor, roadmap, notes and mock-test features keep their own model. It was not compared against the previous model on real papers before becoming the default, so check a few extracted papers against their PDFs. `PAPER_EXTRACT_RENDER_DPI` is separate and still off by default.
