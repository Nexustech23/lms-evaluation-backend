"""
Phase-0 measurement helper. NOT part of the app — run it by hand.

Two things:
  1. --bench : log in, hit a list of endpoints N times each concurrently,
     print p50/p95/p99/max per endpoint. Tells us which endpoints are slow
     and how they behave under light concurrency.
  2. --explain : run .explain("executionStats") on the heavy list/report
     queries against the real DB and print whether an index is used, docs
     examined vs returned, and time. Tells us if the Phase-0 indexes landed.

Usage (from the backend dir, venv active):
    python scripts/perf_probe.py --bench --base http://localhost:5050 \
        --email you@example.com --password 'secret' --n 30 --concurrency 10
    python scripts/perf_probe.py --explain
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time

# Allow `python scripts/perf_probe.py ...` from the backend directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Endpoints to benchmark. Adjust to your data (ids etc.).
DEFAULT_ENDPOINTS = [
    "/me",
    "/dashboard/institute",
    "/faculty/filter-data",
    "/schools?limit=25",
    "/subjects/institute?limit=25",
    "/institute-students?limit=25",
]


async def _bench(base: str, email: str, password: str, endpoints: list[str], n: int, concurrency: int) -> None:
    import httpx

    async with httpx.AsyncClient(base_url=base, timeout=60) as c:
        r = await c.post("/login", json={"email": email, "password": password})
        r.raise_for_status()

        for ep in endpoints:
            sem = asyncio.Semaphore(concurrency)
            samples: list[float] = []
            errors = 0

            async def _one() -> None:
                nonlocal errors
                async with sem:
                    t = time.perf_counter()
                    try:
                        resp = await c.get(ep)
                        dt = (time.perf_counter() - t) * 1000
                        if resp.status_code >= 400:
                            errors += 1
                        samples.append(dt)
                    except Exception:
                        errors += 1

            await asyncio.gather(*[_one() for _ in range(n)])
            if not samples:
                print(f"{ep:<40} no successful samples ({errors} errors)")
                continue
            samples.sort()
            p = lambda q: samples[min(len(samples) - 1, int(len(samples) * q))]  # noqa: E731
            print(
                f"{ep:<40} p50={p(0.5):6.0f}ms  p95={p(0.95):6.0f}ms  "
                f"p99={p(0.99):6.0f}ms  max={samples[-1]:6.0f}ms  mean={statistics.mean(samples):6.0f}ms  err={errors}"
            )


async def _explain() -> None:
    from motor.motor_asyncio import AsyncIOMotorClient

    from app.core.config import settings

    client = AsyncIOMotorClient(settings.MONGODB_URI, serverSelectionTimeoutMS=8000)
    db = client[settings.DB_NAME]

    probes = [
        ("subjectDetails", {"institute_id": {"$exists": True}, "is_deleted": False}),
        ("subjectDetails", {"faculty_id": {"$exists": True}, "is_deleted": False}),
        ("answerDetails", {"exam_id": {"$exists": True}}),
        ("selfLearnerRoadmaps", {"user_id": {"$exists": True}}),
        ("newsavedDocs", {"subject_id": {"$exists": True}}),
        ("users", {"email": {"$exists": True}}),
    ]
    for coll, filt in probes:
        try:
            plan = await db[coll].find(filt).limit(25).explain()
            ex = plan.get("executionStats", {})
            stage = plan.get("queryPlanner", {}).get("winningPlan", {})
            uses_index = "IXSCAN" in str(stage)
            print(
                f"{coll:<22} index={'YES' if uses_index else 'NO (COLLSCAN)'}  "
                f"examined={ex.get('totalDocsExamined')}  returned={ex.get('nReturned')}  "
                f"{ex.get('executionTimeMillis')}ms"
            )
        except Exception as exc:
            print(f"{coll:<22} explain failed: {exc}")
    client.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--base", default="http://localhost:5050")
    ap.add_argument("--email")
    ap.add_argument("--password")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--concurrency", type=int, default=10)
    args = ap.parse_args()

    if args.explain:
        asyncio.run(_explain())
    if args.bench:
        if not (args.email and args.password):
            ap.error("--bench needs --email and --password")
        asyncio.run(_bench(args.base, args.email, args.password, DEFAULT_ENDPOINTS, args.n, args.concurrency))
    if not (args.bench or args.explain):
        ap.error("pass --bench and/or --explain")


if __name__ == "__main__":
    main()
