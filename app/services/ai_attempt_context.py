"""Thread-propagated hook for visible provider retry attempts."""
from contextvars import ContextVar

retry_recorder = ContextVar("ai_retry_recorder", default=None)


def record_provider_retry(error, provider, model):
    recorder = retry_recorder.get()
    if recorder is not None:
        recorder(error, provider, model)
