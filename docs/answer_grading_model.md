# Answer-sheet grading model

Answer checking uses `claude-sonnet-5-5` with `max_tokens=30000`, adaptive thinking and explicit `high` effort. Configuration is in `app/services/grading.py`. The existing `grading-evidence-v2` prompt and JSON contract remain in use.

The cap covers thinking plus the visible grading JSON. It is a ceiling, not a guaranteed usage amount or accuracy improvement. Review actual completion rates and token usage on representative scripts before adjusting it. Thinking tokens are included in the provider's output-token usage and therefore in the existing usage ledger.

`app/services/claude.py` streams requests with explicit effort, reads the complete response, extracts text blocks and rejects truncated/refused responses. New thinking/effort fields use `extra_body` for compatibility with the pinned SDK. Callers without effort retain their existing request path. There is no added retry or model fallback.

Transcript generation has its own model constant (`claude-sonnet-4-5`) and retains its 18,000-token cap. Its success and failure usage metadata use the transcript model. OCR, MCQ-key generation and publication/review policy are unchanged.

Migration reference: [Anthropic's Sonnet 5.5 migration guide](https://platform.claude.com/docs/en/models/sonnet-5-5/migration-guide).

Offline verification: `tests/test_grading_model_config.py` covers request configuration, transcript separation, SDK request serialization/stream parsing, thinking-block filtering, usage preservation and rejection of incomplete output. Network connections are blocked and provider responses are mocked. These checks do not measure grading accuracy or verify live API behavior.
