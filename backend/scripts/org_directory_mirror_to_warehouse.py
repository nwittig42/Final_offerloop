"""Mirror Coresignal-matched-with-clean-company org-directory people into the
shared firm_employees warehouse, so the website/extension/MCP/mobile don't
re-pay Coresignal/FullEnrich for the same person later.

Deliberately excludes:
  - Coresignal matches with no active_experience (no clean company,
    firm_employee_doc_from_app_contact requires one and returns None)
  - FullEnrich-only email finds that only have a smashed raw directory
    headline, not a clean company name — writing that into the shared
    warehouse would put garbled company names in front of every live
    user's search.

Uses the SAME normalizer + write shape as the production writer
(firm_cache/writer.py's _safe_batch_upsert), called directly since this is
a one-time manual backfill, not live search traffic — the
ENABLE_FIRM_CACHE_WRITE flag that gates automatic writes during searches
doesn't apply here.

Usage:
  python org_directory_mirror_to_warehouse.py --cred <fs.json> --school "University of Southern California" --dry-run
  python org_directory_mirror_to_warehouse.py --cred <fs.json> --school "University of Southern California" --live
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.services.firm_cache.schema import (
    FIRM_EMPLOYEES_COLLECTION,
    firm_employee_doc_from_app_contact,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cred", required=True)
    ap.add_argument(
        "--school",
        required=True,
        help="display name written to the College field, e.g. 'University of Southern California'",
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    args = ap.parse_args()

    import firebase_admin
    from firebase_admin import credentials, firestore
    from google.cloud.firestore import SERVER_TIMESTAMP
    from google.cloud.firestore_v1.base_query import FieldFilter

    cred = credentials.Certificate(args.cred)
    firebase_admin.initialize_app(cred)
    db = firestore.client()

    docs_to_write = []
    skipped_no_normalize = []

    people = list(
        db.collection("people").where(filter=FieldFilter("coresignalResult", "==", "matched")).stream()
    )
    for d in people:
        p = d.to_dict()
        company = (p.get("coresignalCompany") or "").strip()
        if not company:
            continue  # no active job on Coresignal — handled separately, not here
        app_contact = {
            "FirstName": p.get("firstName") or "",
            "LastName": p.get("lastName") or "",
            "LinkedIn": p.get("linkedinUrl") or "",
            "Company": company,
            "Title": p.get("coresignalTitle") or "",
            "College": args.school,
            "PhotoUrl": p.get("photoUrl") or "",
        }
        doc = firm_employee_doc_from_app_contact(app_contact)
        if doc is None:
            skipped_no_normalize.append(d.id)
            continue
        docs_to_write.append(doc)

    print(f"{len(docs_to_write)} docs ready to warehouse, "
          f"{len(skipped_no_normalize)} failed normalization: {skipped_no_normalize}")

    if args.dry_run:
        print("sample:", docs_to_write[0] if docs_to_write else None)
        return

    col = db.collection(FIRM_EMPLOYEES_COLLECTION)
    batch = db.batch()
    for i, doc in enumerate(docs_to_write):
        payload = dict(doc)
        payload["last_seen_at"] = SERVER_TIMESTAMP
        batch.set(col.document(doc["linkedin_id"]), payload, merge=True)
        if (i + 1) % 400 == 0:
            batch.commit()
            batch = db.batch()
    batch.commit()
    print(f"warehoused {len(docs_to_write)} docs into {FIRM_EMPLOYEES_COLLECTION}")


if __name__ == "__main__":
    main()
