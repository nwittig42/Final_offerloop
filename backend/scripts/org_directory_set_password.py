"""Set (or change) an org's join password (OFF-42).

Students join an org in the app by typing this password. Only a werkzeug
hash is stored, in org_secrets/{org_id}, which no client and no org read
can see. Changing it doesn't remove existing members.

The password is typed at a hidden prompt (twice), never passed as a flag,
so it stays out of shell history.

Usage:
  python org_directory_set_password.py --org-id usc_tke
  python org_directory_set_password.py --org-id usc_tke --cred <fs.json>

Without --cred it uses your gcloud application-default login.
"""
from __future__ import annotations

import argparse
import getpass
from datetime import datetime, timezone

PROJECT = "offerloop-native"
MIN_LENGTH = 6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--org-id", required=True)
    ap.add_argument("--cred", help="service account JSON; defaults to gcloud application-default credentials")
    args = ap.parse_args()

    from werkzeug.security import generate_password_hash

    if args.cred:
        import firebase_admin
        from firebase_admin import credentials, firestore
        firebase_admin.initialize_app(credentials.Certificate(args.cred))
        db = firestore.client()
    else:
        from google.cloud import firestore
        db = firestore.Client(project=PROJECT)

    org = db.collection("orgs").document(args.org_id).get()
    if not org.exists:
        print(f"orgs/{args.org_id} does not exist.")
        return
    print(f"Setting the join password for {(org.to_dict() or {}).get('name') or args.org_id}")

    pw = getpass.getpass("New password: ")
    if len(pw) < MIN_LENGTH:
        print(f"Too short (at least {MIN_LENGTH} characters). Nothing changed.")
        return
    if getpass.getpass("Type it again: ") != pw:
        print("They don't match. Nothing changed.")
        return

    db.collection("org_secrets").document(args.org_id).set({
        "passwordHash": generate_password_hash(pw),
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    })
    print("Done. Students can now join with that password.")


if __name__ == "__main__":
    main()
