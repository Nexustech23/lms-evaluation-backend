import json
import logging
import time

import redis.asyncio as redis_asyncio

from app.core.config import settings

_client: redis_asyncio.Redis | None = None


def connect_to_redis() -> None:
    global _client
    _client = redis_asyncio.from_url(settings.REDIS_URL, decode_responses=True)


async def close_redis_connection() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


def get_redis() -> redis_asyncio.Redis:
    if _client is None:
        raise RuntimeError("Redis client not initialized — call connect_to_redis() first")
    return _client


# ============================================================
# TOKEN REVOCATION
# The JWT is a stateless cookie, so "logout" (or a forced logout / password
# change) can't invalidate an already-issued token on its own. We record a
# per-user cutoff timestamp here; app.api.deps.get_current_identity rejects
# any token whose `iat` predates it. Fails open on Redis errors — a Redis
# outage must not lock every user out (the DB is_active check still applies).
# ============================================================

_REVOKED_KEY = "revoked_before:{user_id}"


async def revoke_user_tokens(user_id: str, ttl_seconds: int) -> None:
    """Invalidate every token issued for user_id up to now. TTL matches the
    max token lifetime, so the key self-expires once no pre-cutoff token
    could still be valid anyway."""
    try:
        await get_redis().setex(_REVOKED_KEY.format(user_id=user_id), ttl_seconds, repr(time.time()))
    except Exception:
        logging.warning("revoke_user_tokens: Redis unavailable, user_id=%s not revoked", user_id)


async def tokens_revoked_after(user_id: str) -> float | None:
    """Unix-seconds cutoff (sub-second precision) for user_id, or None when
    nothing is recorded or Redis is unreachable (fail-open)."""
    try:
        raw = await get_redis().get(_REVOKED_KEY.format(user_id=user_id))
        return float(raw) if raw else None
    except Exception:
        return None


# ============================================================
# ACCOUNT-STATE CACHE
# get_current_identity needs to know, per request, whether the account is
# still active/undeleted and whether a password change is pending. Reading
# users on every authenticated request makes Mongo a hot-path dependency
# (and caused intermittent 5xx on pollers during Mongo slowness). This
# caches that tiny state for a short window. Fails OPEN on any Redis/Mongo
# error — an unknown state is treated as "not blocked"; the token signature
# is already verified and token revocation is checked separately.
# ============================================================

_ACCOUNT_STATE_KEY = "acctstate:{user_id}"
ACCOUNT_STATE_TTL = 30  # seconds — deactivation / delete takes effect within this window


async def get_cached_account_state(user_id: str):
    try:
        raw = await get_redis().get(_ACCOUNT_STATE_KEY.format(user_id=user_id))
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def set_cached_account_state(user_id: str, state: dict) -> None:
    try:
        await get_redis().setex(_ACCOUNT_STATE_KEY.format(user_id=user_id), ACCOUNT_STATE_TTL, json.dumps(state))
    except Exception:
        pass


async def bust_account_state(user_id: str) -> None:
    """Drop the cached state now (call after a password change / reactivation
    so the user isn't stuck behind a stale 'must change password' / 'inactive'
    for up to ACCOUNT_STATE_TTL seconds)."""
    try:
        await get_redis().delete(_ACCOUNT_STATE_KEY.format(user_id=user_id))
    except Exception:
        pass
