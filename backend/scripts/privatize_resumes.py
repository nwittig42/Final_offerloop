"""
One-off: lock down every resume already on file and give each a thumbnail.

Before 2026-09-08 both upload paths called blob.make_public(), so the files
under resumes/{uid}/ were readable by anyone with the storage.googleapis.com
path. This walks every user with a resumeUrl and, for each file:

  1. revokes anonymous read (the public ACL make_public() added)
  2. mints a Firebase download token and rewrites resumeUrl to the token
     form, which every backend reader and the website already accept
  3. renders page one to resumes/{uid}/thumb.png and stores resumeThumbUrl

Idempotent: a user whose URL is already the token form only gets a
thumbnail if missing. Nothing is deleted.

Usage (on a service with the Firebase credentials, e.g. offerloop-staging):
  DRY RUN:  python backend/scripts/privatize_resumes.py --dry-run
  LIVE:     python backend/scripts/privatize_resumes.py
"""
from __future__ import annotations

import argparse
import logging
import sys
import urllib.parse
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("privatize_resumes")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="stop after N users (0 = all)")
    args = ap.parse_args()

    from app.extensions import get_db, init_firebase
    from app.services.resume_storage import (
        THUMB_FILENAME, _bucket, revoke_public, store_thumbnail, storage_path_from_url, token_url,
    )

    init_firebase(None)
    db = get_db()
    bucket = _bucket()

    scanned = public_fixed = rewritten = thumbed = missing = 0
    for snap in db.collection("users").where("resumeUrl", ">", "").stream():
        scanned += 1
        if args.limit and scanned > args.limit:
            break
        u = snap.to_dict() or {}
        url = u.get("resumeUrl") or ""
        path = storage_path_from_url(url)
        if not path:
            logger.info("%s: resumeUrl not ours (%s), skip", snap.id, url[:60])
            continue
        blob = bucket.blob(path)
        if not blob.exists():
            missing += 1
            logger.warning("%s: file missing at %s", snap.id, path)
            continue
        updates: dict = {}
        # Old public form is host storage.googleapis.com; the token form is
        # host firebasestorage.googleapis.com. Test the HOST, not the whole
        # URL: the bucket is named offerloop-native.firebasestorage.app, so a
        # substring test on the URL called every public link "token form"
        # and the first live run locked 121 files without rewriting their
        # links (2026-09-08, Rylan hit AccessDenied in the viewer).
        was_public = urllib.parse.urlparse(url).netloc == "storage.googleapis.com"
        # Revoke anonymous read on EVERY file, not only those whose stored link
        # is the old public form: the dry run found all 149 links already in
        # token form while the objects themselves could still carry the ACL
        # make_public() added. Idempotent; counts only real changes.
        if not args.dry_run and revoke_public(blob):
            public_fixed += 1
        if was_public and not args.dry_run:
            updates["resumeUrl"] = token_url(blob)
            rewritten += 1
        if not u.get("resumeThumbUrl") and path.lower().endswith(".pdf"):
            if args.dry_run:
                thumbed += 1
            else:
                try:
                    thumb = store_thumbnail(snap.id, blob.download_as_bytes())
                    if thumb:
                        updates["resumeThumbUrl"] = thumb
                        thumbed += 1
                except Exception:
                    logger.exception("%s: thumbnail failed", snap.id)
        if args.dry_run:
            logger.info("%s: %s%s", snap.id, "PUBLIC -> private, " if was_public else "already private, ",
                        "needs thumb" if (not u.get("resumeThumbUrl") and path.lower().endswith(".pdf")) else "has thumb or not a pdf")
        elif updates:
            db.collection("users").document(snap.id).update(updates)
    logger.info("done: scanned %d, public fixed %d, urls rewritten %d, thumbnails %d, files missing %d%s",
                scanned, public_fixed, rewritten, thumbed, missing, " (dry run)" if args.dry_run else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
