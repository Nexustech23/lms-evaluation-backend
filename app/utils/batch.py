# ============================================================
# Batch id lookup (Perf Phase 4).
#
# Replaces the "for row in rows: await db[c].find_one({_id: row.fk})"
# N+1 pattern in list / enrichment endpoints with a single {_id: {$in: [...]}}
# fetch. The output is unchanged — callers still look each id up by hand,
# they just do it against an in-memory dict instead of a round-trip.
# ============================================================
from __future__ import annotations

from typing import Any, Iterable, Optional

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase


async def load_by_ids(
    db: AsyncIOMotorDatabase,
    collection: str,
    ids: Iterable[Any],
    projection: Optional[dict] = None,
    *,
    match: Optional[dict] = None,
) -> dict[ObjectId, dict]:
    """Return {_id: document} for every distinct, non-null id in `ids` that
    exists in `collection` (and satisfies `match`, if given — e.g.
    {"is_deleted": {"$ne": True}} to mirror a per-row find_one that filtered
    out soft-deleted rows). Missing / filtered ids are simply absent from
    the result, exactly as `find_one` returning None. Accepts ObjectId or
    str ids; non-ObjectId-coercible values are skipped."""
    wanted: set[ObjectId] = set()
    for raw in ids:
        if raw is None:
            continue
        if isinstance(raw, ObjectId):
            wanted.add(raw)
        elif ObjectId.is_valid(raw):
            wanted.add(ObjectId(raw))

    if not wanted:
        return {}

    query: dict[str, Any] = {"_id": {"$in": list(wanted)}}
    if match:
        query.update(match)

    cursor = db[collection].find(query, projection)
    return {doc["_id"]: doc async for doc in cursor}
