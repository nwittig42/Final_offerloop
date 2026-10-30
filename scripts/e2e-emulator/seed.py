"""Seed the Firestore EMULATOR with synthetic recommended professionals.

Four made-up people at different firms, all USC alumni so the ranker matches
the synthetic test resume (make_test_resume.py) through the school query.
None are real people. One has a "verified" work email belonging to the test
inbox (E2E_RECIPIENT_EMAIL in .env.e2e.local); the lookup reads it from the
stored warehouse field, so no email vendor is ever called. One has no email,
for the "no email found" path.

Refuses to run unless it is pointed at the emulator.
Usage: .venv/bin/python scripts/e2e-emulator/seed.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1]), str(HERE.parents[1] / "backend")]

import e2e_server  # noqa: E402  (isolation setup + whitelisted local env)

e2e_server.configure_environment()
e2e_server.assert_isolated()

import os  # noqa: E402
import firebase_admin  # noqa: E402
from firebase_admin import firestore  # noqa: E402

from app.extensions import _AnonymousCredential  # noqa: E402
from app.services.firm_cache.schema import (  # noqa: E402
    FIRM_EMPLOYEES_COLLECTION, derive_title_level, slugify_company, slugify_school,
)

RECIPIENT = (os.environ.get("E2E_RECIPIENT_EMAIL") or "e2e-recipient@example.test").strip()
NOW = datetime.now(timezone.utc).isoformat()

PEOPLE = [
    # (id, first, last, title, company, email)
    ("e2e-taylor-testwell", "Taylor", "Testwell", "Investment Banking Analyst", "Goldman Sachs", RECIPIENT),
    ("e2e-morgan-mockford", "Morgan", "Mockford", "Investment Banking Associate", "Evercore", ""),
    ("e2e-jordan-sampleton", "Jordan", "Sampleton", "Strategy Consultant", "McKinsey & Company", "jordan.sampleton@example.test"),
    ("e2e-casey-fixture", "Casey", "Fixture", "Private Equity Analyst", "Blackstone", "casey.fixture@example.test"),
]


def doc_for(pid, first, last, title, company, work_email) -> dict:
    doc = {
        "linkedin_id": pid,
        "linkedin_url": f"https://www.linkedin.com/in/{pid}",
        "pdl_id": "",
        "name": f"{first} {last}",
        "first_name": first,
        "last_name": last,
        "headline": title,
        "title": title.lower(),
        "title_level": derive_title_level(title),
        "company": slugify_company(company),
        "company_display": company,
        "schools": [slugify_school("University of Southern California")],
        "schools_display": ["University of Southern California"],
        "joined_year": 2024,
        "office_location": "new_york_ny",
        "city": "New York",
        "state": "NY",
        "country_code": "US",
        "photo_url": "",
        "e2e_synthetic": True,
        "last_seen_at": NOW,
    }
    if work_email:
        doc.update({"work_email": work_email, "work_email_source": "e2e_seed", "work_email_verified_at": NOW})
    return doc


def main() -> None:
    if not firebase_admin._apps:
        firebase_admin.initialize_app(_AnonymousCredential(), {"projectId": "offerloop-native"})
    db = firestore.client()
    for p in PEOPLE:
        db.collection(FIRM_EMPLOYEES_COLLECTION).document(p[0]).set(doc_for(*p))
        print(f"seeded {p[1]} {p[2]} ({p[4]}) email={'<recipient>' if p[5] == RECIPIENT else (p[5] or 'none')}")
    print(f"recipient inbox for Taylor Testwell: {RECIPIENT}")


if __name__ == "__main__":
    main()
