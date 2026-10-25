"""
Gated review run for Find contacts (OFF-49).

Picks tier-1/2 postings balanced across company size (large/startup) x function
bucket (finance, tech, consulting, other), then optionally runs the full
find_contacts pipeline on each and writes a CSV for manual review.

Dry run is the default: it reads Firestore (read-only), prints the chosen jobs
and the maximum spend estimate, and makes ZERO paid calls (no LLM, no Coresignal,
no FullEnrich). Only --confirm runs the service, which WRITES to PROD Firestore
(jobs/{id}/contacts/result and posting_facts) and spends vendor credits.

Usage:
    python backend/scripts/find_contacts_review.py            # dry run
    python backend/scripts/find_contacts_review.py --n 50 --confirm
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _p in (_ROOT, os.path.join(_ROOT, "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CSV_PATH = ("/private/tmp/claude-501/-Users-nicholaswittig-Offerloop/"
            "faf1ce69-0b5e-4904-bb62-7d1309bdda4a/scratchpad/find_contacts_review.csv")
from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

MAX_DOCS = 2000
SLOTS = ("leader", "process", "recruiter")
BUCKETS = ("finance", "tech", "business")
LARGE_SHARE = 0.7
STARTUP_MAX_EMPLOYEES = 200  # mirrors playbooks.LARGE_EMPLOYEE_THRESHOLD

_BUCKET_PATTERNS = (
    ("finance", re.compile(
        r"\b(financ\w*|bank\w*|invest\w*|equity|trading|trader|asset management|"
        r"private equity|credit|capital markets|m&a|wealth|treasury|underwrit\w*|"
        r"accounting|fp&a|quant\w*)\b", re.I)),
    ("tech", re.compile(
        r"\b(software|engineer\w*|developer|swe|data|machine learning|ml|ai|"
        r"backend|frontend|front-end|back-end|full[- ]?stack|devops|sre|"
        r"security|programmer|scientist|analytics)\b", re.I)),
    ("business", re.compile(
        r"\b(consult\w*|advisory|strategy|strategic|marketing|operations|"
        r"business|product|sales|growth|gtm|account|people|recruit\w*)\b", re.I)),
)
_BLOCKLIST = re.compile(
    r"\b(retail|installer|installation|technician|shift|"
    r"mental health|roofing|warehouse|cashier|barista|store associate|part[- ]time|"
    r"seasonal|nurse|driver|cultivation|janitor|server|housekeeping|solar|kitchen|dashmart|optician|jewelry|dasher)\b", re.I)
# Tier 1/2 still holds senior roles; the review is for early-career postings.
# A "manager" title stays in only next to associate/assistant (Associate Project
# Manager); "Staff Accountant" is entry level, so staff is not on the list.
_SENIOR = re.compile(
    r"\b(senior|sr\.?|director|principal|head of|vp|vice president|chief|"
    r"lead|phd|postdoc\w*)\b", re.I)
_MANAGER = re.compile(r"\bmanager\b", re.I)
_JUNIOR_MANAGER = re.compile(r"\b(associate|assistant)\b", re.I)


def function_bucket(job: dict) -> str:
    """Word-boundary heuristic, no LLM. Prefers department/team, then title."""
    for field in ("department", "team", "title"):
        text = job.get(field)
        if not isinstance(text, str) or not text.strip():
            continue
        for bucket, pattern in _BUCKET_PATTERNS:
            if pattern.search(text):
                return bucket
    return "business"


def is_target(job: dict) -> bool:
    title = job.get("title") or ""
    if _SENIOR.search(title) or (_MANAGER.search(title) and not _JUNIOR_MANAGER.search(title)):
        return False
    return not _BLOCKLIST.search(f"{job.get('title') or ''} {job.get('company') or ''}")


def size_of(job: dict) -> str:
    """large / startup / unknown from what the job doc itself says. Not
    playbooks.company_size: that treats unknown as large, which made every job
    "large" here (no job doc carries a headcount yet)."""
    from app.services.find_contacts.playbooks import _canonicalize, _large_employers
    if _canonicalize(job.get("company")) in _large_employers():
        return "large"
    employees = job.get("linkedin_org_employees")
    if isinstance(employees, (int, float)) and not isinstance(employees, bool) and employees > 0:
        return "startup" if employees < STARTUP_MAX_EMPLOYEES else "large"
    return "unknown"


def _round_robin(items: list[dict], k: int, has_result, rng, per_company: int = 1) -> list[dict]:
    """Spread k picks across function buckets, at most `per_company` per company.
    Jobs that already have a result are used only after the clean ones run out."""
    by_bucket = {b: [] for b in BUCKETS}
    for job in items:
        by_bucket[job["bucket"]].append(job)
    for lst in by_bucket.values():
        rng.shuffle(lst)
    chosen, fallback, counts = [], [], {}
    while len(chosen) < k and any(by_bucket.values()):
        for bucket in BUCKETS:
            if len(chosen) >= k:
                break
            while by_bucket[bucket]:
                job = by_bucket[bucket].pop()
                company = (job.get("company") or "").strip().lower()
                if counts.get(company, 0) >= per_company:
                    continue
                if has_result(job):
                    fallback.append(job)
                    continue
                counts[company] = counts.get(company, 0) + 1
                chosen.append(job)
                break
    for job in fallback:
        company = (job.get("company") or "").strip().lower()
        if len(chosen) >= k:
            break
        if counts.get(company, 0) < per_company:
            counts[company] = counts.get(company, 0) + 1
            chosen.append(job)
    return chosen[:k]


def select_jobs(jobs: list[dict], n: int, has_result=lambda job: False,
                seed: int = 49, large_per_company: int = 3) -> list[dict]:
    """~70% large, ~30% startup, balanced across function buckets inside each
    group. Large is capped per company so one employer cannot dominate; unknown
    -size jobs (one per company) fill whatever the quotas cannot.
    Each returned dict is the job plus 'size' and 'bucket'."""
    rng = random.Random(seed)
    groups: dict[str, list[dict]] = {"large": [], "startup": [], "unknown": []}
    seen: set[tuple[str, str]] = set()
    for job in jobs:
        if not is_target(job):
            continue
        # The same posting is often listed per office; one copy is enough.
        key = ((job.get("company") or "").strip().lower(), (job.get("title") or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        size = size_of(job)
        groups[size].append(dict(job, size=size, bucket=function_bucket(job)))
    want_large = round(n * LARGE_SHARE)
    chosen = _round_robin(groups["large"], want_large, has_result, rng, large_per_company)
    chosen += _round_robin(groups["startup"], n - want_large, has_result, rng, 1)
    if len(chosen) < n:
        chosen += _round_robin(groups["unknown"], n - len(chosen), has_result, rng, 1)
    return chosen[:n]


def _load_candidate_jobs(db) -> list[dict]:
    jobs = []
    query = db.collection("jobs").where(filter=FieldFilter("relevance_tier", "in", [1, 2])).limit(MAX_DOCS)
    for doc in query.stream():
        data = doc.to_dict() or {}
        if data.get("expired") is True:
            continue
        data["id"] = doc.id
        jobs.append(data)
    return jobs


def _result_ref(db, job_id: str):
    return db.collection("jobs").document(job_id).collection("contacts").document("result")


def _ids_with_result(db, job_ids: list[str], chunk: int = 300) -> set[str]:
    """Batched read-only existence check of jobs/{id}/contacts/result."""
    found: set[str] = set()
    for i in range(0, len(job_ids), chunk):
        refs = [_result_ref(db, jid) for jid in job_ids[i:i + chunk]]
        for snap in db.get_all(refs, field_paths=["status"]):
            if snap.exists:
                found.add(snap.reference.parent.parent.id)
    return found


def _csv_header() -> list[str]:
    header = ["job_id", "title", "company", "team", "team_source"]
    for slot in SLOTS:
        header += [f"{slot}_name", f"{slot}_title", f"{slot}_label", f"{slot}_found_by"]
    return header + ["steps", "error"]


def _csv_row(job: dict, result: dict, error: str = "") -> list:
    """`result` is the stored jobs/{id}/contacts/result doc (it holds steps)."""
    people = {p.get("slot"): p for p in result.get("people") or []}
    row = [job["id"], job.get("title", ""), job.get("company", ""),
           result.get("team", ""), result.get("team_source", "")]
    for slot in SLOTS:
        p = people.get(slot) or {}
        row += [p.get("name", ""), p.get("title", ""), p.get("label", ""), p.get("found_by", "")]
    steps = result.get("steps") or {}
    return row + [json.dumps(steps, sort_keys=True) if steps else "", error]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--out", default=CSV_PATH, help="CSV path for --confirm")
    parser.add_argument("--confirm", action="store_true",
                        help="actually run the pipeline (writes PROD, spends credits)")
    args = parser.parse_args(argv)

    from app.extensions import get_db, init_firebase
    init_firebase(None)
    db = get_db()
    jobs = _load_candidate_jobs(db)
    done = _ids_with_result(db, [j["id"] for j in jobs])
    chosen = select_jobs(jobs, args.n, lambda job: job["id"] in done)

    print(f"Read {len(jobs)} tier-1/2 non-expired jobs; chose {len(chosen)}")
    for i, job in enumerate(chosen, 1):
        print(f"{i:>2}. {job['id']} | {job.get('company', '')} | {job.get('title', '')} "
              f"| {job['bucket']} | {job['size']}")
    n = len(chosen)
    print(f"\nMax spend: up to {n * 6} Coresignal collects + {n} FullEnrich calls "
          f"+ {n} LLM calls")

    if not args.confirm:
        print("Dry run only. Re-run with --confirm to execute.")
        return 0

    from app.services.find_contacts.service import find_contacts_for_job
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_csv_header())
        for job in chosen:
            error = ""
            try:
                outcome = find_contacts_for_job(db, job["id"], None)
                if outcome.get("status") != "ready":
                    error = f"status={outcome.get('status')}"
            except Exception as exc:  # keep going; one bad job must not lose the rest
                error = f"{type(exc).__name__}: {exc}"
            # steps only live on the stored doc; the service response omits them
            stored = _result_ref(db, job["id"]).get().to_dict() or {}
            writer.writerow(_csv_row(job, stored, error))
            fh.flush()
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
