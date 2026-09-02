# ============================================================
# Lightweight, always-cheap instrumentation for finding where time goes.
#
#   - RequestTimingMiddleware: one log line per request —
#     "method path -> status  Nms". Slow ones (> PERF_LOG_SLOW_MS) at WARNING.
#   - SlowQueryLogger: a pymongo command monitor that logs any MongoDB
#     command slower than SLOW_QUERY_MS, with the collection + filter shape.
#
# Neither samples response bodies or query values beyond field names, so
# there's nothing sensitive in the logs.
# ============================================================
import logging
import time

from pymongo import monitoring
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import settings

_req_log = logging.getLogger("perf.request")
_query_log = logging.getLogger("perf.mongo")


class RequestTimingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not settings.PERF_LOG_ENABLED:
            return await call_next(request)

        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000

        level = logging.WARNING if elapsed_ms >= settings.PERF_LOG_SLOW_MS else logging.INFO
        _req_log.log(
            level, "%s %s -> %s  %.0fms", request.method, request.url.path, response.status_code, elapsed_ms
        )
        response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.0f}"
        return response


class _SlowQueryListener(monitoring.CommandListener):
    """Logs slow MongoDB commands. Only the command name + collection +
    top-level filter keys are recorded — never the values."""

    _INTERESTING = {"find", "aggregate", "count", "distinct", "update", "delete", "findAndModify"}

    def __init__(self, threshold_ms: int):
        self._threshold_us = threshold_ms * 1000
        self._pending: dict[int, tuple[str, str]] = {}

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        if event.command_name not in self._INTERESTING:
            return
        coll = event.command.get(event.command_name)
        filt = event.command.get("filter") or event.command.get("query") or {}
        keys = ",".join(sorted(k for k in filt if not k.startswith("$"))) or "-"
        self._pending[event.request_id] = (f"{coll}", keys)

    def succeeded(self, event: monitoring.CommandSucceededEvent) -> None:
        self._finish(event.request_id, event.duration_micros, ok=True)

    def failed(self, event: monitoring.CommandFailedEvent) -> None:
        self._finish(event.request_id, event.duration_micros, ok=False)

    def _finish(self, request_id: int, duration_us: int, ok: bool) -> None:
        info = self._pending.pop(request_id, None)
        if info is None or duration_us < self._threshold_us:
            return
        coll, keys = info
        _query_log.warning(
            "slow %s  %s  filter=[%s]  %.0fms%s",
            "query" if ok else "query(FAILED)", coll, keys, duration_us / 1000,
            "" if ok else "",
        )


_registered = False


def install_slow_query_logging() -> None:
    """Idempotent — safe to call once on startup."""
    global _registered
    if _registered or settings.SLOW_QUERY_MS <= 0:
        return
    monitoring.register(_SlowQueryListener(settings.SLOW_QUERY_MS))
    _registered = True
    _query_log.info("slow-query logging enabled (threshold %dms)", settings.SLOW_QUERY_MS)
