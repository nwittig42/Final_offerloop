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
import os
import random
import sys
from collections import defaultdict

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for _p in (_ROOT, os.path.join(_ROOT, "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

CSV_PATH = ("/private/tmp/claude-501/-Users-nicholaswittig-Offerloop/"
            "faf1ce69-0b5e-4904-bb62-7d1309bdda4a/scratchpad/find_contacts_review.csv")
MAX_DOCS = 2000
SLOTS = ("leader", "process", "recruiter")
BUCKETS = ("finance", "tech", "consulting", "other")
SIZES = ("large", "startup")

_BUCKET_KEYWORDS = (
    ("consulting", ("consult", "advisory", "strategy analyst", "business analyst")),
    ("finance", ("financ", "bank", "invest", "equity", "trading", "asset manage",
                 "private equity", "credit", "capital markets", "m&a", "wealth", "treasury")),
    ("tech", ("software", "engineer", "developer", "data", "machine learning", "ml ",
              "product", "swe", "backend", "frontend", "full stack", "full-stack",
              "devops", "ai ", "analytics", "security")),
)


def function_bucket(title: str) -> str:
    """Cheap title heuristic (no LLM). Consulting is checked first so that
    'Strategy Consulting Analyst' does not land in finance/tech."""
    t = f" {(title or '').lower()} "
    for bucket, words in _BUCKET_KEYWORDS:
        if any(w in t for w in words):
            return bucket
    return "other"


def classify(job: dict) -> tuple[str, str]:
    from app.services.find_contacts.playbooks import company_size
    return company_size({"size_hint": "unknown", "company": job.get("company")}, job), function_bucket(job.get("title"))


def select_jobs(jobs: list[dict], n: int, has_result=lambda job: False,
                seed: int = 49) -> list[dict]:
    """Round-robin over the size x function cells so each is represented.

    Prefers jobs with no existing result and one job per company; those that
    break either preference are used only to fill leftover slots. Each returned
    dict is the job plus 'size' and 'bucket'.
    """
    rng = random.Random(seed)
    cells: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for job in jobs:
        size, bucket = classify(job)
        cells[(size, bucket)].append(dict(job, size=size, bucket=bucket))
    for items in cells.values():
        rng.shuffle(items)

    chosen: list[dict] = []
    fallback: list[dict] = []
    companies: set[str] = set()
    order = [(s, b) for s in SIZES for b in BUCKETS]
    while len(chosen) < n and any(cells[c] for c in order):
        for cell in order:
            if len(chosen) >= n:
                break
            while cells[cell]:
                job = cells[cell].pop()
                company = (job.get("company") or "").strip().lower()
                if company in companies or has_result(job):
                    fallback.append(job)
                    continue
                companies.add(company)
                chosen.append(job)
                break
    for job in fallback:
        if len(chosen) >= n:
            break
        chosen.append(job)
    return chosen[:n]


def _load_candidate_jobs(db) -> list[dict]:
    jobs = []
    query = db.collection("jobs").where("relevance_tier", "in", [1, 2]).limit(MAX_DOCS)
    for doc in query.stream():
        data = doc.to_dict() or {}
        if data.get("expired") is True:
            continue
        data["id"] = doc.id
        jobs.append(data)
    return jobs


def _has_result_fn(db):
    def has_result(job: dict) -> bool:
        snap = (db.collection("jobs").document(job["id"])
                .collection("contacts").document("result").get())
        return bool(snap.exists)
    return has_result


def _csv_header() -> list[str]:
    header = ["job_id", "title", "company", "team", "team_source"]
    for slot in SLOTS:
        header += [f"{slot}_name", f"{slot}_title", f"{slot}_label", f"{slot}_found_by"]
    return header + ["steps"]


def _csv_row(job: dict, result: dict) -> list:
    people = {p.get("slot"): p for p in result.get("people") or []}
    row = [job["id"], job.get("title", ""), job.get("company", ""),
           result.get("team", ""), result.get("team_source", "")]
    for slot in SLOTS:
        p = people.get(slot) or {}
        row += [p.get("name", ""), p.get("title", ""), p.get("label", ""), p.get("found_by", "")]
    return row + [result.get("steps") or result.get("status", "")]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--confirm", action="store_true",
                        help="actually run the pipeline (writes PROD, spends credits)")
    args = parser.parse_args(argv)

    from app.extensions import get_db, init_firebase
    init_firebase(None)
    db = get_db()
    jobs = _load_candidate_jobs(db)
    chosen = select_jobs(jobs, args.n, _has_result_fn(db))

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
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(_csv_header())
        for job in chosen:
            try:
                result = find_contacts_for_job(db, job["id"], None)
            except Exception as exc:  # keep going; one bad job must not lose the rest
                result = {"status": f"error: {exc}"}
            writer.writerow(_csv_row(job, result))
            fh.flush()
    print(f"Wrote {CSV_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
