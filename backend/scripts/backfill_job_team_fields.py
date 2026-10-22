"""
Backfill `department` / `team` onto existing early-career ATS job docs.

Targets `jobs` docs with relevance_tier in [1, 2] and source in
greenhouse/lever/ashby that have no `department` field. Each board is
re-fetched once; matching jobs (by job_id) get only `department` and `team`
written (merge), after re-checking that each doc still exists. A board whose fetch returns nothing is skipped and reported
(the legacy fetchers return [] on error), never treated as "nothing to update".

Usage:
  DRY RUN:  python -m backend.scripts.backfill_job_team_fields [--limit N]
  LIVE:     python -m backend.scripts.backfill_job_team_fields --confirm
"""
from __future__ import annotations

import argparse
import logging
from collections import defaultdict

from google.cloud.firestore_v1.base_query import FieldFilter

JOBS_COLLECTION = "jobs"
PLATFORMS = ("greenhouse", "lever", "ashby")
BATCH_SIZE = 400

logger = logging.getLogger(__name__)


def _default_fetchers() -> dict:
    from backend.pipeline import fetcher
    return {"greenhouse": fetcher._fetch_greenhouse,
            "lever": fetcher._fetch_lever,
            "ashby": fetcher._fetch_ashby}


def _targets(db) -> dict:
    """{(platform, slug): [job doc ids]} for tier 1/2 ATS docs missing department.
    Only relevance_tier is filtered server-side; source is checked client-side
    to avoid needing a composite index."""
    from backend.pipeline.writer import _ats_board

    query = db.collection(JOBS_COLLECTION).where(
        filter=FieldFilter("relevance_tier", "in", [1, 2]))
    boards: dict = defaultdict(list)
    for snap in query.stream():
        data = snap.to_dict() or {}
        if data.get("source") not in PLATFORMS or "department" in data:
            continue
        board = _ats_board(snap.id)
        if board is None or board[0] not in PLATFORMS:
            continue
        boards[board].append(snap.id)
    return boards


def backfill(db, confirm: bool, fetchers: dict | None = None, limit: int | None = None) -> dict:
    fetchers = fetchers or _default_fetchers()
    boards = _targets(db)
    docs_to_update = sum(len(v) for v in boards.values())
    keys = sorted(boards)
    if limit is not None:
        keys = keys[:limit]

    skipped, pending, unmatched = [], [], 0
    for platform, slug in keys:
        jobs = fetchers[platform](slug)
        if not jobs:
            logger.warning("skipping %s:%s (empty fetch, %d docs)", platform, slug, len(boards[(platform, slug)]))
            skipped.append((platform, slug))
            continue
        by_id = {j["job_id"]: j for j in jobs}
        for doc_id in boards[(platform, slug)]:
            job = by_id.get(doc_id)
            if job is None:
                unmatched += 1  # no longer posted
                continue
            pending.append((doc_id, {"department": job.get("department") or "",
                                     "team": job.get("team") or ""}))

    updated = vanished = 0
    if confirm:
        for i in range(0, len(pending), BATCH_SIZE):
            chunk = pending[i:i + BATCH_SIZE]
            refs = {doc_id: db.collection(JOBS_COLLECTION).document(doc_id) for doc_id, _ in chunk}
            # Board fetches take minutes; a job deleted meanwhile must not be
            # recreated as a stub by set(merge=True). Re-check right before writing.
            existing = {snap.id for snap in db.get_all(list(refs.values())) if snap.exists}
            live = [(doc_id, fields) for doc_id, fields in chunk if doc_id in existing]
            vanished += len(chunk) - len(live)
            if not live:
                continue
            batch = db.batch()
            for doc_id, fields in live:
                batch.set(refs[doc_id], fields, merge=True)
            batch.commit()
            updated += len(live)
        if vanished:
            logger.warning("%d matched docs were deleted before the write; skipped", vanished)

    return {"docs_to_update": docs_to_update if limit is None else sum(len(boards[k]) for k in keys),
            "total_docs_missing": docs_to_update,
            "total_boards": len(boards),
            "boards": len(keys), "matched": len(pending), "unmatched": unmatched,
            "skipped_boards": skipped, "updated": updated, "vanished": vanished}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--confirm", action="store_true",
                        help="Write the updates (default is a read-only dry run)")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N boards")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from backend.pipeline.main import _bootstrap_app
    from backend.app.extensions import get_db

    app = _bootstrap_app()
    with app.app_context():
        result = backfill(get_db(), confirm=args.confirm, limit=args.limit)
    mode = "CONFIRMED" if args.confirm else "DRY RUN (nothing written; pass --confirm to apply)"
    print(f"{mode}: { {k: (v if k != 'skipped_boards' else f'{len(v)} boards: {v}') for k, v in result.items()} }")


if __name__ == "__main__":
    main()
