"""
One-shot backfill: stamp relevance_tier on every existing Firestore job doc.

Must run BEFORE deploying the feed tier filter in jobs.py — otherwise legacy
docs without relevance_tier would silently drop off the feed (Firestore `in`
queries don't match missing fields).

Idempotent: skips docs that already have relevance_tier set. Safe to re-run.

With --recompute it re-grades docs that already carry a tier (use after
changing the tiering rules in quality_gate) and writes ONLY the docs whose
tier actually changes. It used to write every scanned doc, which meant a
rules change that moved 122 jobs still issued ~70,000 no-op writes and blew
past Firestore's query deadline partway through.

Pages with a document-ID cursor rather than one long `coll.stream()`. A single
stream over ~70k docs exceeds the server-side query deadline and fails with
"503 Query timed out", and the client's retry path then dies on
`'_UnaryStreamMultiCallable' object has no attribute '_retry'`. Short paged
queries avoid both, and the run resumes cleanly if interrupted.

Usage:
    cd ~/work/Offerloop
    GOOGLE_APPLICATION_CREDENTIALS=firebase-sa.json python -m backend.scripts.backfill_relevance_tier
    # dry-run first (reports how many tiers would CHANGE):
    ... python -m backend.scripts.backfill_relevance_tier --dry-run
    # re-grade after a quality_gate rules change:
    ... python -m backend.scripts.backfill_relevance_tier --recompute
    # limit batch (for smoke tests):
    ... python -m backend.scripts.backfill_relevance_tier --limit=1000
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter

import firebase_admin
from firebase_admin import credentials, firestore

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from backend.pipeline.quality_gate import compute_relevance_tier  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BATCH_WRITE_SIZE = 400
# Docs per paged read. Small enough that each query finishes well inside the
# server-side deadline that killed the single-stream version.
PAGE_SIZE = 2000


def _init_db():
    if not firebase_admin._apps:
        cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "firebase-sa.json")
        firebase_admin.initialize_app(credentials.Certificate(cred_path))
    return firestore.client()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="scan + compute but don't write anything")
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after processing N docs (for smoke tests)")
    parser.add_argument("--recompute", action="store_true",
                        help="overwrite existing relevance_tier fields")
    args = parser.parse_args()

    db = _init_db()
    coll = db.collection("jobs")

    total_scanned = 0
    total_skipped = 0
    total_unchanged = 0
    tier_counts: Counter = Counter()
    transitions: Counter = Counter()
    batch = db.batch()
    batch_size = 0
    total_written = 0

    logger.info("Paging jobs collection (%d per page)...", PAGE_SIZE)
    cursor = None
    done = False
    while not done:
        page = coll.order_by("__name__").limit(PAGE_SIZE)
        if cursor is not None:
            # Pass the snapshot, not a {"__name__": id} dict — the field form
            # needs a full document path and silently mis-pages with a bare id.
            page = page.start_after(cursor)
        docs = list(page.stream())
        if not docs:
            break
        cursor = docs[-1]

        for doc in docs:
            total_scanned += 1
            data = doc.to_dict() or {}
            existing = data.get("relevance_tier")

            if not args.recompute and existing is not None:
                total_skipped += 1
                continue

            tier = compute_relevance_tier(data)
            tier_counts[tier] += 1

            # Only write when the value actually changes. Re-stamping a doc
            # with the tier it already has costs a write and a trigger for no
            # benefit, and at ~70k docs that is what exceeded the query
            # deadline on the first --recompute run.
            if tier == existing:
                total_unchanged += 1
                continue
            transitions[(existing, tier)] += 1

            if not args.dry_run:
                batch.update(doc.reference, {"relevance_tier": tier})
                batch_size += 1
                if batch_size >= BATCH_WRITE_SIZE:
                    batch.commit()
                    total_written += batch_size
                    logger.info("committed %d writes (running total %d)",
                                batch_size, total_written)
                    batch = db.batch()
                    batch_size = 0

            if args.limit and total_scanned >= args.limit:
                logger.info("hit --limit=%d, stopping", args.limit)
                done = True
                break

        if not done and len(docs) < PAGE_SIZE:
            break
        if total_scanned % (PAGE_SIZE * 10) == 0:
            logger.info("scanned %d docs...", total_scanned)

    if batch_size and not args.dry_run:
        batch.commit()
        total_written += batch_size
        logger.info("final commit of %d writes", batch_size)

    print("\n--- BACKFILL SUMMARY ---")
    print(f"scanned:            {total_scanned}")
    print(f"skipped (existing): {total_skipped}")
    print(f"computed tiers:     {sum(tier_counts.values())}")
    for t in sorted(tier_counts):
        print(f"  tier {t}: {tier_counts[t]}")
    print(f"already correct:    {total_unchanged} (no write needed)")
    if transitions:
        print("tier changes:")
        for (old, new), n in sorted(transitions.items(), key=lambda kv: -kv[1]):
            print(f"  {old} -> {new}: {n}")
    print(f"writes {'that WOULD be made' if args.dry_run else 'committed'}:"
          f"   {total_written if not args.dry_run else sum(transitions.values())}"
          f" {'(DRY RUN)' if args.dry_run else ''}")


if __name__ == "__main__":
    main()
