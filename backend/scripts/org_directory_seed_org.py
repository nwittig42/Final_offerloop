"""Make an ingested org directory joinable in the app (OFF-42).

The ingest script writes orgs/{org_id} with name/type/memberCount only. The
app also needs a short display name (badges, filter pill), the school, the
words students type for it ("tke"), and joinable: true before the org shows
up in onboarding, Account Settings or search.

Usage:
  python org_directory_seed_org.py --cred <fs.json> --org-id usc_tke \\
    --short-name "USC TKE" --school "University of Southern California" \\
    --alias tke --alias "tau kappa epsilon" --alias teke --dry-run
  ... same with --live to write.

--cred is a Firebase service account JSON. Never commit it.
"""
from __future__ import annotations

import argparse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cred", required=True)
    ap.add_argument("--org-id", required=True)
    ap.add_argument("--short-name", required=True)
    ap.add_argument("--school", required=True)
    ap.add_argument("--alias", action="append", default=[])
    ap.add_argument("--not-joinable", action="store_true", help="hide the org from the app again")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    args = ap.parse_args()

    import firebase_admin
    from firebase_admin import credentials, firestore

    firebase_admin.initialize_app(credentials.Certificate(args.cred))
    db = firestore.client()

    ref = db.collection("orgs").document(args.org_id)
    snap = ref.get()
    if not snap.exists:
        print(f"orgs/{args.org_id} does not exist. Ingest the directory first.")
        return

    update = {
        "shortName": args.short_name,
        "school": args.school,
        "aliases": sorted({a.strip().lower() for a in args.alias if a.strip()}),
        "joinable": not args.not_joinable,
    }
    print("current:", snap.to_dict())
    print("update:", update)
    if args.dry_run:
        print("dry run only, no writes performed")
        return
    ref.set(update, merge=True)
    print("done")


if __name__ == "__main__":
    main()
