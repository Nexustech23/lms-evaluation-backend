# ============================================================
# Display-only hierarchy cache (Perf Phase 3a).
#
# The institute-hierarchy dropdown endpoints (schools / programmes /
# departments / batches / subjects lists, and the student's academic
# filters) are refetched on almost every page mount, and each is 1-3 Mongo
# round-trips to the *remote* database. Their contents change only when an
# admin edits the hierarchy — rare. This caches them in Redis for a short
# window, busted on every hierarchy write.
#
# ── Accuracy guarantee ──────────────────────────────────────
# ONLY the names in _ALLOWED may be cached — cached_get() raises otherwise.
# Every allowed target returns id + display-name lists for UI navigation
# ONLY. Nothing here is an input to a computed mark / grade / CGPA / CO-PO
# attainment / transcript:
#   - subject `credits`, `co` arrays, relative-grading curves, CO/PO
#     weights, programme PO targets are deliberately NOT cached (the
#     /subjects/{programme_id} and /faculty/filter-data endpoints, which
#     carry those, are intentionally left uncached).
# A stale entry can at worst show an old *name* for up to the TTL; it can
# never change a number.
#
# Fails OPEN: any Redis error → the loader runs and its result is returned
# uncached, exactly as if the cache weren't there.
# ============================================================
from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Optional

from app.core.redis_client import get_redis

logger = logging.getLogger("cache")

# Bump this suffix whenever any cached endpoint's response SHAPE changes —
# it instantly invalidates every previously stored entry (old keys just
# expire unread).
_VERSION = "v1"

# The complete set of things allowed to use this cache. A name not listed
# here raises in cached_get(), so no future caller can quietly cache
# something on the grading / transcript / results / marks path.
_ALLOWED = frozenset({
    "schools_dropdown",
    "programmes_dropdown",
    "departments_dropdown",
    "batches_dropdown",
    "subjects_dropdown",
    "student_academic_filters",
})

DEFAULT_TTL = 300  # seconds — safety net even if a bust is somehow missed


def _key(name: str, institute_id: str, sub_id: Optional[str]) -> str:
    tail = f":{sub_id}" if sub_id else ""
    return f"hcache:{_VERSION}:{name}:{institute_id}{tail}"


async def cached_get(
    name: str,
    institute_id: Any,
    loader: Callable[[], Awaitable[Any]],
    *,
    sub_id: Any = None,
    ttl: int = DEFAULT_TTL,
) -> Any:
    """Return the cached value for (name, institute_id, sub_id), or run
    `loader()` and cache its JSON-serialisable result. Fails open."""
    if name not in _ALLOWED:
        raise ValueError(f"cache name not in allowlist: {name!r}")

    key = _key(name, str(institute_id), None if sub_id is None else str(sub_id))

    try:
        raw = await get_redis().get(key)
    except Exception:
        return await loader()  # Redis unreachable — behave as if uncached

    if raw is not None:
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("cache: corrupt entry at %s — reloading", key)

    result = await loader()
    try:
        await get_redis().setex(key, ttl, json.dumps(result, default=str))
    except Exception:
        pass
    return result


async def bust_institute_hierarchy(institute_id: Any) -> None:
    """Drop every hierarchy-cache entry for one institute. Called by every
    hierarchy write endpoint. Coarse on purpose — a hierarchy edit is rare
    and correctness beats granularity."""
    pattern = f"hcache:{_VERSION}:*:{institute_id}*"
    try:
        r = get_redis()
        keys = [k async for k in r.scan_iter(match=pattern, count=200)]
        if keys:
            await r.delete(*keys)
    except Exception:
        logger.warning("bust_institute_hierarchy: Redis unavailable, institute_id=%s", institute_id)
