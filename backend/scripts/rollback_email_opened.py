"""
Roll back the email_opened pipeline stage (OFF-12 open tracking).

If open tracking has to be switched off, contacts already sitting in
email_opened would keep showing "Opened" forever. This script moves every
contact under users/{uid}/contacts with

    pipelineStage == "email_opened"

back to

    pipelineStage = "waiting_on_reply"

which is what the contact was before its first counted open. The open stamps
(emailOpenedAt, emailLastOpenedAt, emailOpenCount) are left alone: they are
history, not stage. Turn tracking off first (unset EMAIL_OPEN_TRACKING /
EMAIL_OPEN_TRACKING_UIDS) so pixel loads stop promoting contacts again.

Dry-run by default: prints the count per user and writes nothing. Re-run with
--apply to commit, in batched writes. Idempotent: re-running after --apply
finds nothing to do.

Usage:
    python backend/scripts/rollback_email_opened.py            # dry-run
    python backend/scripts/rollback_email_opened.py --apply    # live
    python backend/scripts/rollback_email_opened.py --limit 50 # cap users
"""
import argparse
import os
import sys
from datetime import datetime, timezone

# Allow running from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

EMAIL_OPENED = "email_opened"
WAITING_ON_REPLY = "waiting_on_reply"

# Firestore allows up to 500 writes per batch; stay under it.
BATCH_LIMIT = 450


def get_db():
    """Initialize Firebase and return Firestore client, or None on failure."""
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        if not firebase_admin._apps:
            cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            if cred_path:
                cred = credentials.Certificate(cred_path)
                firebase_admin.initialize_app(cred)
            else:
                firebase_admin.initialize_app()
        return firestore.client()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: could not init Firestore: {e}")
        return None


def rollback_update(contact):
    """Firestore update for one contact doc, or None when it needs nothing."""
    if (contact or {}).get("pipelineStage") != EMAIL_OPENED:
        return None
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {"pipelineStage": WAITING_ON_REPLY, "updatedAt": now_iso}


def run(db, apply=False, limit=0, batch_limit=BATCH_LIMIT, log=print):
    """Scan every user's contacts. Returns {uid: contacts rolled back (or that
    would be, in dry-run)} for users with at least one."""
    users_ref = db.collection("users")
    counts = {}
    batch = db.batch() if apply else None
    pending = 0

    for users_scanned, user_snap in enumerate(users_ref.stream(), start=1):
        if limit and users_scanned > limit:
            log(f"(limit reached: {limit} users)")
            break
        uid = user_snap.id
        contacts_ref = users_ref.document(uid).collection("contacts")
        try:
            # Single-field equality: served by Firestore's automatic index.
            snaps = list(contacts_ref.where("pipelineStage", "==", EMAIL_OPENED).stream())
        except Exception as e:  # noqa: BLE001
            log(f"  WARN: query failed for uid={uid}: {e}")
            continue

        for c_snap in snaps:
            update = rollback_update(c_snap.to_dict() or {})
            if update is None:
                continue
            counts[uid] = counts.get(uid, 0) + 1
            if apply:
                batch.update(c_snap.reference, update)
                pending += 1
                if pending >= batch_limit:
                    batch.commit()
                    batch = db.batch()
                    pending = 0

        if counts.get(uid):
            log(f"  {uid}: {counts[uid]} contact(s) email_opened -> waiting_on_reply")

    if apply and pending:
        batch.commit()
    return counts


def main():
    parser = argparse.ArgumentParser(
        description="Move every email_opened contact back to waiting_on_reply."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write to Firestore (default is dry-run).",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Stop after this many users (0 = all).",
    )
    args = parser.parse_args()
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== email_opened rollback ({mode}) ===\n")

    db = get_db()
    if db is None:
        print("ERROR: Firestore client unavailable. Check GOOGLE_APPLICATION_CREDENTIALS.")
        sys.exit(1)

    counts = run(db, apply=args.apply, limit=args.limit)
    total = sum(counts.values())
    print(f"\nUsers with email_opened contacts: {len(counts)}")
    print(f"Contacts {'rolled back' if args.apply else 'to roll back'}: {total}")
    if not args.apply and total:
        print("\nDry-run only. Re-run with --apply to write.")


if __name__ == "__main__":
    main()
