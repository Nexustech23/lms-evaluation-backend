"""Phase 3a — display-only hierarchy cache.

Covers: cache hit, write-triggered invalidation, fail-open when Redis is
down, the name allowlist, and a guard that no computation router touches
the cache.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_security_fixes import _register_institute_admin


async def _institute(superadmin_client, client_factory):
    return await _register_institute_admin(superadmin_client, client_factory, "Cache Institute")


async def test_schools_dropdown_is_cached_and_busted_on_write(superadmin_client, client_factory, test_db):
    institute = await _institute(superadmin_client, client_factory)

    a = await institute.post("/schools", json={"school_name": "School A"})
    assert a.status_code == 200

    # First dropdown call populates the cache.
    first = await institute.get("/schools?limit=0")
    assert first.status_code == 200
    assert [s["school_name"] for s in first.json()["schools"]] == ["School A"]

    # Insert a school straight into Mongo — no API call, so no cache bust.
    institute_oid = (await test_db["schoolDetails"].find_one({"school_name": "School A"}))["institute_id"]
    await test_db["schoolDetails"].insert_one({
        "school_name": "School B (direct)", "institute_id": institute_oid,
    })

    # Still served from cache — the direct insert is invisible.
    cached = await institute.get("/schools?limit=0")
    assert [s["school_name"] for s in cached.json()["schools"]] == ["School A"]

    # A real write through the API busts the institute's hierarchy cache.
    c = await institute.post("/schools", json={"school_name": "School C"})
    assert c.status_code == 200

    after = await institute.get("/schools?limit=0")
    names = sorted(s["school_name"] for s in after.json()["schools"])
    assert names == ["School A", "School B (direct)", "School C"]


async def test_paginated_and_searched_calls_are_not_cached(superadmin_client, client_factory, test_db):
    institute = await _institute(superadmin_client, client_factory)
    await institute.post("/schools", json={"school_name": "Alpha"})

    # limit>0 (paginated) is never cached — a direct insert shows up immediately.
    await institute.get("/schools?limit=10")
    admin_school = await test_db["schoolDetails"].find_one({"school_name": "Alpha"})
    await test_db["schoolDetails"].insert_one({
        "school_name": "Beta", "institute_id": admin_school["institute_id"],
    })
    listed = await institute.get("/schools?limit=10")
    assert listed.json()["total"] == 2


async def test_cache_fails_open_when_redis_unavailable(superadmin_client, client_factory):
    institute = await _institute(superadmin_client, client_factory)
    await institute.post("/schools", json={"school_name": "Only School"})

    def _boom():
        raise RuntimeError("redis down")

    with patch("app.core.cache.get_redis", side_effect=_boom):
        resp = await institute.get("/schools?limit=0")

    assert resp.status_code == 200
    assert [s["school_name"] for s in resp.json()["schools"]] == ["Only School"]


async def test_cached_get_rejects_names_outside_the_allowlist():
    from app.core.cache import cached_get

    async def _loader():
        return {"x": 1}

    with pytest.raises(ValueError):
        await cached_get("grades_or_anything_computed", "inst-1", _loader)


def test_no_computation_router_imports_the_cache():
    """The cache must never be reachable from a router that returns a
    computed mark / grade / CGPA / CO-PO / transcript value."""
    routers = Path("app/api/routers")
    forbidden = ["grading.py", "transcripts.py", "relative_grading.py",
                 "subject_results.py", "evaluation.py", "marks_import.py", "answers.py"]
    offenders = [
        name for name in forbidden
        if "app.core.cache" in (routers / name).read_text(encoding="utf-8")
    ]
    assert offenders == [], f"computation routers import the cache: {offenders}"
