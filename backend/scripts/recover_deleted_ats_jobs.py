"""
Recover live ATS jobs that the old TTL cleanup deleted.

The cleanup bug removed `jobs/{id}` docs for jobs that were still live, but
their IDs stayed in `ats_crawl_state/{platform}:{slug}.kept_job_ids`, so
`writer.sync_board_jobs` treats them as already seen and never re-adds them.

For each greenhouse/lever/ashby state doc this finds kept IDs with no
`jobs/{id}` doc. With --confirm it rewrites the state doc to
`kept_job_ids = kept - missing` and `board_hash = None`, so the next crawl
sees those jobs as new and rewrites them. This script never writes `jobs`.

Usage:
  DRY RUN:  python -m backend.scripts.recover_deleted_ats_jobs
  LIVE:     python -m backend.scripts.recover_deleted_ats_jobs --confirm
"""
from __future__ import annotations

import argparse
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from collections import Counter

STATE_COLLECTION = "ats_crawl_state"
JOBS_COLLECTION = "jobs"
PLATFORMS = ("greenhouse", "lever", "ashby")
BATCH = 300
WORKERS = 8

logger = logging.getLogger(__name__)


def _get_all_with_retry(db, refs, attempts: int = 4):
    """get_all, retrying transient gRPC drops (ping timeouts) with backoff."""
    for attempt in range(1, attempts + 1):
        try:
            return list(db.get_all(refs))
        except Exception as exc:  # reads only, so retrying is always safe
            if attempt == attempts:
                raise
            logger.warning("get_all failed (%s); retry %d/%d", exc, attempt, attempts - 1)
            time.sleep(2 * attempt)


def _missing_ids(db, job_ids: list[str]) -> set[str]:
    """IDs in `job_ids` with no jobs doc. Batches of 300, fetched concurrently."""
    unique = list(dict.fromkeys(job_ids))
    chunks = [unique[i : i + BATCH] for i in range(0, len(unique), BATCH)]

    def check(chunk: list[str]) -> set[str]:
        refs = [db.collection(JOBS_COLLECTION).document(j) for j in chunk]
        return {s.id for s in _get_all_with_retry(db, refs) if not s.exists}

    missing: set[str] = set()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for found in pool.map(check, chunks):
            missing |= found
    return missing


def recover(db, confirm: bool) -> dict:
    """Return {"boards": boards with missing jobs, "missing": total missing IDs,
    "updated": state docs rewritten}. Per-platform counts are logged."""
    # Drain the stream first: a slow consumer lets Firestore drop it.
    states = []
    for snap in db.collection(STATE_COLLECTION).stream():
        state = snap.to_dict() or {}
        if state.get("platform") in PLATFORMS:
            states.append((snap.id, state["platform"], list(state.get("kept_job_ids") or [])))

    all_ids = [j for _, _, kept in states for j in kept]
    logger.info("checking %d job ids across %d boards", len(all_ids), len(states))
    missing_all = _missing_ids(db, all_ids)

    boards = missing_total = updated = 0
    per_platform_boards: Counter = Counter()
    per_platform_missing: Counter = Counter()
    for doc_id, platform, kept in states:
        missing = {j for j in kept if j in missing_all}
        if not missing:
            continue
        boards += 1
        missing_total += len(missing)
        per_platform_boards[platform] += 1
        per_platform_missing[platform] += len(missing)
        if confirm:
            db.collection(STATE_COLLECTION).document(doc_id).update(
                {"kept_job_ids": [j for j in kept if j not in missing], "board_hash": None}
            )
            updated += 1

    for platform in PLATFORMS:
        logger.info("%s: %d boards, %d missing jobs",
                    platform, per_platform_boards[platform], per_platform_missing[platform])
    return {"boards": boards, "missing": missing_total, "updated": updated}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--confirm", action="store_true",
                        help="Write the state-doc updates (default is a read-only dry run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from backend.pipeline.main import _bootstrap_app
    from backend.app.extensions import get_db

    app = _bootstrap_app()
    with app.app_context():
        result = recover(get_db(), confirm=args.confirm)
    mode = "CONFIRMED" if args.confirm else "DRY RUN (nothing written; pass --confirm to apply)"
    print(f"{mode}: {result}")


if __name__ == "__main__":
    main()
