"""Capped Fantastic.jobs coverage probe. Read-only: never calls the writer.

Usage:
    python backend/scripts/fantastic_jobs_probe.py [--confirm] [--max-requests 10]

Without --confirm nothing is requested and Firestore is not touched. With
--confirm it pages through /active-ats-7d (title-filtered to early-career
terms), stopping at --max-requests, then reads Firestore (batched, by
apply_url) to measure overlap with jobs we already have.
"""
import argparse
import os
import re
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from backend.pipeline.fetcher import (  # noqa: E402
    FANTASTICJOBS_DEFAULT_PARAMS,
    FANTASTICJOBS_BASE_URL,
    REQUEST_TIMEOUT,
    _fantasticjobs_headers,
    _normalize_fj_job,
)

EARLY_CAREER_TERMS = ("intern", "internship", "analyst", "new grad", "early career", "summer")
PAGE_SIZE = 100
FIRESTORE_IN_LIMIT = 30  # Firestore caps "in" queries at 30 values
_TITLE_RE = re.compile(r"\b(" + "|".join(re.escape(t) for t in EARLY_CAREER_TERMS) + r")\b", re.I)


def _get_db():
    from backend.app.extensions import get_db
    return get_db()


def _title_filter() -> str:
    return " | ".join(f"'{t}'" for t in EARLY_CAREER_TERMS)


def _count_in_firestore(db, urls: list) -> int:
    from google.cloud.firestore_v1.base_query import FieldFilter

    unique = sorted({u for u in urls if u})
    found = set()
    for i in range(0, len(unique), FIRESTORE_IN_LIMIT):
        chunk = unique[i:i + FIRESTORE_IN_LIMIT]
        q = db.collection("jobs").where(filter=FieldFilter("apply_url", "in", chunk))
        for doc in q.stream():
            u = (doc.to_dict() or {}).get("apply_url")
            if u:
                found.add(u)
    return sum(1 for u in urls if u in found)


def run_probe(confirm: bool, max_requests: int, http_get=None, db=None) -> dict:
    """Page the API with exactly one HTTP call per request (no retry, no sleep).

    Stops at max_requests, on a short page, or immediately on any non-200
    (429 sets rate_limited). Does not use fetcher._fj_fetch_page because that
    retries a 429 after sleeping, which could double the request count.
    """
    report = {"jobs_returned": 0, "early_career": 0, "in_firestore": None,
              "with_hiring_manager": 0, "requests_used": 0,
              "rate_limited": False, "error": None}
    if not confirm:
        return report
    http_get = http_get or requests.get
    headers = _fantasticjobs_headers()
    jobs = []
    offset = 0
    while report["requests_used"] < max_requests:
        params = {**FANTASTICJOBS_DEFAULT_PARAMS, "limit": str(PAGE_SIZE), "offset": offset,
                  "agency": "false", "advanced_title_filter": _title_filter(),
                  "location_filter": "United States"}
        report["requests_used"] += 1
        try:
            resp = http_get(FANTASTICJOBS_BASE_URL, headers=headers, params=params,
                            timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                report["rate_limited"] = True
                break
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # stop on any failure; never retry
            report["error"] = str(exc)
            break
        raw = data if isinstance(data, list) else data.get("data", [])
        page = [j for j in raw if j.get("id") and j.get("title")]
        jobs.extend(_normalize_fj_job(j) for j in page)
        if len(raw) < PAGE_SIZE:
            break
        offset += PAGE_SIZE

    report["jobs_returned"] = len(jobs)
    report["early_career"] = sum(1 for j in jobs if _TITLE_RE.search(j["title"] or ""))
    report["with_hiring_manager"] = sum(1 for j in jobs if j.get("ai_hiring_manager_name"))
    if db is not None and jobs:
        report["in_firestore"] = _count_in_firestore(db, [j["apply_url"] for j in jobs])
    return report


def _pct(n, total):
    return f"{n} ({100.0 * n / total:.1f}%)" if total else f"{n} (n/a)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--confirm", action="store_true", help="make paid requests")
    ap.add_argument("--max-requests", type=int, default=10)
    args = ap.parse_args(argv)

    if not os.getenv("RAPIDAPI_KEY"):
        print("RAPIDAPI_KEY is not set; nothing to do.")
        return 0
    if not args.confirm:
        print(f"Dry run, nothing requested (would make up to {args.max_requests} "
              f"requests of {PAGE_SIZE} jobs). Re-run with --confirm to spend quota.")
        return 0

    r = run_probe(True, args.max_requests, db=_get_db())
    n = r["jobs_returned"]
    print(f"Jobs returned:                    {n}")
    print(f"Early-career share (title):       {_pct(r['early_career'], n)}")
    print(f"Already in Firestore (apply_url): {_pct(r['in_firestore'] or 0, n)}")
    print(f"With ai_hiring_manager_name:      {_pct(r['with_hiring_manager'], n)}")
    print(f"Requests used:                    {r['requests_used']} / {args.max_requests}")
    if r["rate_limited"]:
        print("Stopped early: HTTP 429 rate limited (not retried).")
    if r["error"]:
        print(f"Stopped early: {r['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
