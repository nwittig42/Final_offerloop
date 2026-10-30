"""FullEnrich email-recovery pass for USC TKE 'lost' person_org_links.

Reads every person_org_links doc with orgId=usc_tke and membershipStatus=lost,
pulls the matching people/{personKey} doc for name + directory headline, and
runs them through the same fullenrich_client.start_bulk / fetch_bulk waterfall
the live reveal flow uses (DELIVERABLE / HIGH_PROBABILITY only).

IMPORTANT: FullEnrich rejects the WHOLE bulk request if even one row has no
company_name/domain/linkedin_url to search against (400
error.enrichment.domain.empty) — this script splits those out first rather
than letting one empty-company person fail an entire 100-person batch.

On a find: writes workEmail / workEmailStatus / workEmailSource /
workEmailVerifiedAt onto the people/{personKey} doc. Never overwrites the
original directory `email` field (that's the raw source-of-truth).

Every attempted personKey (hit, miss, or skipped for no company) gets
enrichmentAttemptedAt stamped so a re-run doesn't re-spend on the same people.

Usage:
  python org_directory_enrich_fullenrich.py --cred <firestore_key.json> --fullenrich-key-file <key.txt> --dry-run
  python org_directory_enrich_fullenrich.py --cred <firestore_key.json> --fullenrich-key-file <key.txt> --live

--fullenrich-key-file is a plain text file holding only the FULLENRICH_API_KEY
value (Render env var on the backend service). Never commit it.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

ORG_ID = "usc_tke"
BATCH_SIZE = 100  # FullEnrich bulk cap per job
POLL_INTERVAL_SEC = 5
POLL_TIMEOUT_SEC = 600  # 10 min ceiling per batch, no live user waiting


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cred", required=True)
    ap.add_argument("--fullenrich-key-file", required=True)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    args = ap.parse_args()

    key = Path(args.fullenrich_key_file).read_text().strip()
    os.environ["FULLENRICH_API_KEY"] = key

    import firebase_admin
    from firebase_admin import credentials, firestore
    from google.cloud.firestore_v1.base_query import FieldFilter

    cred = credentials.Certificate(args.cred)
    firebase_admin.initialize_app(cred)
    db = firestore.client()

    from app.services import fullenrich_client as fe

    if not fe.enabled(db):
        print("FullEnrich not enabled (bad key or budget exhausted). Aborting.")
        return

    links = list(
        db.collection("person_org_links")
        .where(filter=FieldFilter("orgId", "==", ORG_ID))
        .where(filter=FieldFilter("membershipStatus", "==", "lost"))
        .stream()
    )
    print(f"found {len(links)} lost links")

    targets = []  # (person_key, first, last, company)
    for link in links:
        person_key = link.to_dict().get("personKey")
        if not person_key:
            continue
        pdoc = db.collection("people").document(person_key).get()
        if not pdoc.exists:
            continue
        pdata = pdoc.to_dict() or {}
        if pdata.get("enrichmentAttemptedAt"):
            continue  # idempotent: don't re-spend on an already-attempted person
        first = pdata.get("firstName") or ""
        last = pdata.get("lastName") or ""
        company = pdata.get("directoryHeadline") or ""
        targets.append((person_key, first, last, company))

    print(f"{len(targets)} not yet attempted (of {len(links)} lost)")

    eligible = [t for t in targets if t[3].strip()]
    skipped_no_company = [t for t in targets if not t[3].strip()]
    print(f"{len(eligible)} have a company name to search against, "
          f"{len(skipped_no_company)} skipped (name only, nothing to match on)")
    targets = eligible

    if skipped_no_company and not args.dry_run:
        ts = datetime.now(timezone.utc).isoformat()
        batch = db.batch()
        for pk, *_ in skipped_no_company:
            batch.set(
                db.collection("people").document(pk),
                {"enrichmentAttemptedAt": ts, "enrichmentSource": "fullenrich",
                 "enrichmentResult": "skipped_no_company_data"},
                merge=True,
            )
        batch.commit()
        print(f"marked {len(skipped_no_company)} as skipped_no_company_data")

    if args.dry_run:
        print("dry run only. sample targets:")
        for t in targets[:5]:
            print(" ", t)
        return

    now = lambda: datetime.now(timezone.utc).isoformat()
    total_found = 0
    total_attempted = 0

    for i in range(0, len(targets), BATCH_SIZE):
        chunk = targets[i : i + BATCH_SIZE]
        people_arg = [(pk, first, last, company, "") for pk, first, last, company in chunk]
        print(f"\nbatch {i // BATCH_SIZE + 1}: starting {len(chunk)} lookups...")
        job_id = fe.start_bulk(people_arg, db=db)
        if not job_id:
            print("  start_bulk failed, skipping this batch")
            continue

        found = {}
        deadline = time.time() + POLL_TIMEOUT_SEC
        while time.time() < deadline:
            ready, found = fe.fetch_bulk(job_id, wait_seconds=0, db=db)
            if ready:
                break
            time.sleep(POLL_INTERVAL_SEC)
        else:
            print("  poll timeout, treating remainder of this batch as misses")

        print(f"  {len(found)} sellable emails found of {len(chunk)}")
        total_found += len(found)
        total_attempted += len(chunk)

        batch = db.batch()
        ts = now()
        for pk, first, last, company in chunk:
            ref = db.collection("people").document(pk)
            update = {"enrichmentAttemptedAt": ts, "enrichmentSource": "fullenrich"}
            hit = found.get(pk)
            if hit:
                update["workEmail"] = hit["email"]
                update["workEmailStatus"] = hit["status"]
                update["workEmailSource"] = "fullenrich"
                update["workEmailVerifiedAt"] = ts
            batch.set(ref, update, merge=True)
        batch.commit()
        print(f"  wrote results for {len(chunk)} people")

    print(f"\ndone. attempted {total_attempted}, found {total_found} sellable emails")
    spent = fe._spent(db)
    print(f"fullenrich spent_estimate now: {spent}")


if __name__ == "__main__":
    main()
