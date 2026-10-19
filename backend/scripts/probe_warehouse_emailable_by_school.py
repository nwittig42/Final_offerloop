"""READ-ONLY probe: how many emailable alumni does the warehouse hold per school?

Decides whether the time-to-value onboarding walkthrough can be served purely
from firm_employees (instant, 0 credits) or whether it needs a live vendor
search. The walkthrough needs >= 3 people per school who have a verified
work_email still inside the TTL.

Counts, per target school:
  - total docs matching schools array-contains <slug>
  - how many carry a work_email at all
  - how many carry a work_email still inside WAREHOUSE_EMAIL_TTL_DAYS (the
    only ones the reader will actually hand out as Email)
  - distinct companies among the emailable set (variety of suggestions)

Usage:
    cd ~/work/Offerloop
    GOOGLE_APPLICATION_CREDENTIALS=firebase-sa.json \
        python backend/scripts/probe_warehouse_emailable_by_school.py
    ... --schools usc,ucla,michigan --cap 3000
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import firebase_admin
from firebase_admin import credentials, firestore

from app.services.firm_cache.schema import (
    FIRM_EMPLOYEES_COLLECTION,
    slugify_school,
)

TTL_DAYS = int(os.environ.get("WAREHOUSE_EMAIL_TTL_DAYS", "90"))

# The schools CLAUDE.md names as the target market.
DEFAULT_SCHOOLS = [
    "USC",
    "UCLA",
    "University of Michigan",
    "NYU",
    "Georgetown University",
    "University of Pennsylvania",
]


def _init() -> None:
    if firebase_admin._apps:
        return
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "firebase-sa.json")
    firebase_admin.initialize_app(
        credentials.Certificate(cred_path), {"projectId": "offerloop-native"}
    )


def _fresh(stamp) -> bool:
    """Mirror reader._warehouse_email's TTL check."""
    ts = None
    if isinstance(stamp, datetime):
        ts = stamp
    elif isinstance(stamp, str) and stamp:
        try:
            ts = datetime.fromisoformat(stamp.rstrip("Z"))
        except ValueError:
            return False
    if ts is None:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return 0 <= age <= TTL_DAYS * 86400


def _total_count(db) -> str:
    """Collection-wide count via the aggregation API (1 read, not N)."""
    try:
        res = db.collection(FIRM_EMPLOYEES_COLLECTION).count().get()
        return str(res[0][0].value)
    except Exception as e:  # older SDK / no aggregation support
        return f"unavailable ({type(e).__name__})"


def probe_school(db, name: str, cap: int) -> dict:
    slug = slugify_school(name)
    docs = list(
        db.collection(FIRM_EMPLOYEES_COLLECTION)
        .where("schools", "array_contains", slug)
        .limit(cap)
        .stream()
    )
    total = len(docs)
    has_email = 0
    emailable = 0
    companies: Counter = Counter()
    titles: Counter = Counter()
    for snap in docs:
        d = snap.to_dict() or {}
        email = (d.get("work_email") or "").strip()
        if email and "@" in email:
            has_email += 1
            if _fresh(d.get("work_email_verified_at")):
                emailable += 1
                companies[d.get("company_display") or d.get("company") or "?"] += 1
                titles[d.get("title_level") or "other"] += 1
    return {
        "name": name,
        "slug": slug,
        "total": total,
        "has_email": has_email,
        "emailable": emailable,
        "capped": total >= cap,
        "companies": companies,
        "titles": titles,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schools", default="", help="comma-separated school names")
    ap.add_argument("--cap", type=int, default=3000, help="max docs scanned per school")
    args = ap.parse_args()

    schools = (
        [s.strip() for s in args.schools.split(",") if s.strip()]
        if args.schools
        else DEFAULT_SCHOOLS
    )

    _init()
    db = firestore.client()

    print(f"firm_employees total docs: {_total_count(db)}")
    print(f"email TTL: {TTL_DAYS}d   scan cap/school: {args.cap}\n")
    print(f"{'school':<34} {'docs':>7} {'w/email':>9} {'emailable':>10} {'firms':>7}")
    print("-" * 72)

    results = []
    for name in schools:
        r = probe_school(db, name, args.cap)
        results.append(r)
        flag = " (CAPPED)" if r["capped"] else ""
        print(
            f"{r['name'][:33]:<34} {r['total']:>7} {r['has_email']:>9} "
            f"{r['emailable']:>10} {len(r['companies']):>7}{flag}"
        )

    print("\n--- detail: emailable pool composition ---")
    for r in results:
        if not r["emailable"]:
            print(f"\n{r['name']} (slug={r['slug']}): NO emailable alumni")
            continue
        print(f"\n{r['name']} (slug={r['slug']}): {r['emailable']} emailable")
        print("  top firms:  " + ", ".join(f"{c}({n})" for c, n in r["companies"].most_common(8)))
        print("  seniority:  " + ", ".join(f"{t}={n}" for t, n in r["titles"].most_common()))

    print("\n--- verdict ---")
    ok = [r for r in results if r["emailable"] >= 3]
    print(f"{len(ok)}/{len(results)} schools can serve 3 emailable suggestions from warehouse alone.")
    for r in results:
        if r["emailable"] < 3:
            print(f"  SHORT: {r['name']} has only {r['emailable']}")


if __name__ == "__main__":
    main()
