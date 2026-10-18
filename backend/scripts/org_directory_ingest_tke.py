"""One-time bulk load of the USC TKE directory into production Firestore.

Writes three collections, all new, none read by any live app code yet:
  orgs/{org_id}                one doc describing the ingested directory
  people/{person_key}          canonical person, deduped by email when present
  person_org_links/{link_id}   the membership fact: status, type, years, raw headline

Idempotent: re-running with the same CSV upserts the same doc IDs rather than
duplicating. Safe to re-run after a parser fix.

Usage:
  DRY RUN (no writes):  python org_directory_ingest_tke.py --cred <key.json> --csv <path> --dry-run
  LIVE:                 python org_directory_ingest_tke.py --cred <key.json> --csv <path> --live

--cred is a Firebase service account JSON (Firebase console -> Project
Settings -> Service Accounts -> Generate new private key). Never commit it.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from org_directory_parse_tke import ORG_ID, ORG_NAME, SOURCE_LABEL, parse_csv

PEOPLE_COLLECTION = "people"
ORGS_COLLECTION = "orgs"
LINKS_COLLECTION = "person_org_links"


def _link_id(person_key: str) -> str:
    # Firestore doc IDs can't contain "/"; nothing else here does.
    return f"{ORG_ID}__{person_key}"


def build_batches(members):
    """Returns (people_docs, link_docs, stats) — pure, no Firestore calls."""
    people_docs = {}
    link_docs = {}
    for m in members:
        key = m.person_key()

        people_docs[key] = {
            "name": m.name,
            "firstName": m.first_name,
            "lastName": m.last_name,
            "email": m.email,
            "phone": m.phone,
            "directoryHeadline": m.headline_raw,
            "linkedinUrl": None,
            "photoUrl": None,
        }

        link_docs[_link_id(key)] = {
            "personKey": key,
            "orgId": ORG_ID,
            "membershipType": m.membership_type,
            "membershipStatus": m.membership_status,
            "chapterYear": m.chapter_year,
            "classOfYear": m.class_of_year,
            "headlineRaw": m.headline_raw,
            "source": SOURCE_LABEL,
        }

    stats = {
        "members_parsed": len(members),
        "unique_people": len(people_docs),
        "unique_links": len(link_docs),
    }
    return people_docs, link_docs, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cred", required=True, help="path to service account JSON")
    ap.add_argument("--csv", required=True, help="path to the TKE directory CSV export")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    args = ap.parse_args()

    members = parse_csv(args.csv)
    people_docs, link_docs, stats = build_batches(members)
    print("stats:", stats)

    import firebase_admin
    from firebase_admin import credentials, firestore

    cred = credentials.Certificate(args.cred)
    app = firebase_admin.initialize_app(cred)
    db = firestore.client()

    print(f"connected to project: {app.project_id}")

    existing_org = db.collection(ORGS_COLLECTION).document(ORG_ID).get()
    if existing_org.exists:
        print(f"NOTE: orgs/{ORG_ID} already exists, this run will upsert over it")

    if args.dry_run:
        print("dry run only, no writes performed")
        print("sample person doc:", next(iter(people_docs.items())))
        print("sample link doc:", next(iter(link_docs.items())))
        return

    now = datetime.now(timezone.utc).isoformat()

    db.collection(ORGS_COLLECTION).document(ORG_ID).set(
        {
            "name": ORG_NAME,
            "slug": ORG_ID,
            "type": "fraternity",
            "memberCount": stats["members_parsed"],
            "source": SOURCE_LABEL,
            "ingestedAt": now,
        },
        merge=True,
    )

    def _commit_in_batches(collection_name, docs):
        items = list(docs.items())
        written = 0
        for i in range(0, len(items), 450):
            chunk = items[i : i + 450]
            batch = db.batch()
            for doc_id, data in chunk:
                ref = db.collection(collection_name).document(doc_id)
                batch.set(ref, {**data, "updatedAt": now}, merge=True)
            batch.commit()
            written += len(chunk)
            print(f"  {collection_name}: committed {written}/{len(items)}")
        return written

    print(f"writing {len(people_docs)} people docs...")
    _commit_in_batches(PEOPLE_COLLECTION, people_docs)

    print(f"writing {len(link_docs)} person_org_links docs...")
    _commit_in_batches(LINKS_COLLECTION, link_docs)

    print("done")


if __name__ == "__main__":
    main()
