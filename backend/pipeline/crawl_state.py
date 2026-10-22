"""
Per-slug crawl state for the direct-ATS pipeline.

Stored in Firestore at `ats_crawl_state/{platform}:{slug}`. One doc per
company per ATS. Purpose:
  1. Board hash — sha256 of sorted (job_id, posted_at) pairs from the last
     snapshot. sync_board_jobs compares this to the new snapshot's hash to
     decide whether to touch Firestore at all (co-founder's plan projects
     85-95% skip rate → keeps the $54/mo naive Firestore bill at ~$1-3/mo).
  2. kept_job_ids — the previous crawl's returned job_ids, so we can diff
     against the new snapshot and mark disappeared jobs as expired=True
     (auto-apply safety: never submit to filled roles).
  3. Health tracking — last_seen_jobs_at and consecutive_failures let us
     mark stale slugs `dormant` (60-day threshold per plan Section 2).

Reads happen at the start of a per-slug crawl (`read_state_batch` for
efficient bulk reads); writes happen at the end (only when hash changed,
or on failure).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from backend.app.extensions import get_db

logger = logging.getLogger(__name__)

COLLECTION = "ats_crawl_state"
DORMANT_THRESHOLD_DAYS = 60
DORMANT_FAILURE_THRESHOLD = 3  # consecutive fetch failures (404 / connection reset)
BATCH_GET_CHUNK = 400  # Firestore get_all cap is ~500


def _doc_id(platform: str, slug: str) -> str:
    """State doc IDs use `:` as separator — matches co-founder plan convention."""
    return f"{platform}:{slug}"


def compute_board_hash(jobs: Iterable[dict]) -> str:
    """Deterministic sha256 over the (job_id, posted_at) tuples in a snapshot.

    Uses posted_at (which fetcher populates from the ATS's `updated_at` field)
    so any per-job update flips the hash. An empty snapshot returns the hash
    of the empty string — distinct from any real snapshot's hash.
    """
    pairs = sorted(
        (str(j.get("job_id") or ""), str(j.get("posted_at") or ""))
        for j in jobs
    )
    payload = "\n".join(f"{jid}\t{ts}" for jid, ts in pairs)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def read_state(platform: str, slug: str) -> Optional[dict]:
    """Fetch one state doc. Returns None if the slug has never been crawled."""
    db = get_db()
    if not db:
        raise RuntimeError("Firestore DB not initialized")
    snap = db.collection(COLLECTION).document(_doc_id(platform, slug)).get()
    return snap.to_dict() if snap.exists else None


def read_state_batch(platform: str, slugs: list[str]) -> dict[str, dict]:
    """Batched get_all for many slugs. Returns {slug: state_dict} for those
    that exist. Callers should default missing slugs to a fresh state."""
    if not slugs:
        return {}
    db = get_db()
    if not db:
        raise RuntimeError("Firestore DB not initialized")
    result: dict[str, dict] = {}
    for i in range(0, len(slugs), BATCH_GET_CHUNK):
        chunk = slugs[i : i + BATCH_GET_CHUNK]
        refs = [db.collection(COLLECTION).document(_doc_id(platform, s)) for s in chunk]
        for doc in db.get_all(refs):
            if doc.exists:
                data = doc.to_dict() or {}
                s = data.get("slug")
                if s:
                    result[s] = data
    return result


def write_state(
    platform: str,
    slug: str,
    *,
    board_hash: Optional[str],
    kept_job_ids: list[str],
    jobs_count: int,
    tier1_count: int = 0,
    prior_state: Optional[dict] = None,
) -> None:
    """Persist one state doc after a successful crawl.

    Passes `prior_state` in so we can preserve `last_seen_jobs_at` on empty
    crawls (dormancy computed from time-since-seen, not consecutive-empty
    count — cadence-independent).

    `tier1_count` (Phase 3 signal): how many tier-1 jobs the current snapshot
    yielded. Persisted for future hot-promotion logic — a cold slug that
    consistently produces tier-1 jobs deserves the 2h crawl cadence instead
    of the 24h longtail cadence. No auto-promotion is applied here; the field
    is data-collection only until we've observed a few weeks of distribution.
    """
    db = get_db()
    if not db:
        raise RuntimeError("Firestore DB not initialized")

    now = datetime.now(timezone.utc)
    doc: dict = {
        "platform": platform,
        "slug": slug,
        "board_hash": board_hash,
        "kept_job_ids": kept_job_ids,
        "last_crawled_at": now,
        "last_seen_jobs_count": jobs_count,
        "last_tier1_count": tier1_count,
        "consecutive_failures": 0,
    }

    doc.update(_liveness_fields(jobs_count, prior_state, now))

    db.collection(COLLECTION).document(_doc_id(platform, slug)).set(doc, merge=True)


def _liveness_fields(jobs_count: int, prior_state: Optional[dict], now: datetime) -> dict:
    """`last_seen_jobs_at` + `dormant` after a successful crawl."""
    if jobs_count > 0:
        return {"last_seen_jobs_at": now, "dormant": False}
    prev_seen = (prior_state or {}).get("last_seen_jobs_at")
    if not prev_seen:
        # Never seen jobs; not dormant yet, just new.
        return {"dormant": False}
    # last_seen_jobs_at may come back as a Firestore Timestamp — coerce.
    try:
        days_empty = (now - prev_seen).days
    except TypeError:
        days_empty = 0
    return {"last_seen_jobs_at": prev_seen, "dormant": days_empty > DORMANT_THRESHOLD_DAYS}


def needs_recovery(prior_state: Optional[dict]) -> bool:
    """True when the state doc carries failure/dormancy flags that a
    successful crawl must clear."""
    s = prior_state or {}
    return bool(s.get("dormant")) or int(s.get("consecutive_failures") or 0) > 0


def mark_recovered(
    platform: str, slug: str, *, jobs_count: int, prior_state: Optional[dict] = None,
) -> None:
    """Successful crawl of an unchanged board (hash match): reset
    consecutive_failures and dormant (and the last_seen fields, as write_state
    does) without touching board_hash or kept_job_ids. Without this a board
    that failed 3 times stays dormant forever once it recovers unchanged, and
    the TTL cleanup deletes its live jobs."""
    db = get_db()
    if not db:
        raise RuntimeError("Firestore DB not initialized")
    now = datetime.now(timezone.utc)
    update: dict = {
        "platform": platform,
        "slug": slug,
        "consecutive_failures": 0,
        "last_crawled_at": now,
        "last_seen_jobs_count": jobs_count,
    }
    update.update(_liveness_fields(jobs_count, prior_state, now))
    db.collection(COLLECTION).document(_doc_id(platform, slug)).set(update, merge=True)


def forget_jobs(platform: str, slug: str, job_ids: Iterable[str], prior_state: dict) -> None:
    """Drop `job_ids` (deleted by the TTL cleanup) from the board's
    kept_job_ids and clear board_hash, so a revived board re-adds them."""
    db = get_db()
    if not db:
        raise RuntimeError("Firestore DB not initialized")
    gone = set(job_ids)
    kept = [j for j in (prior_state.get("kept_job_ids") or []) if j not in gone]
    db.collection(COLLECTION).document(_doc_id(platform, slug)).set(
        {"kept_job_ids": kept, "board_hash": None}, merge=True)


def mark_failure(platform: str, slug: str, prior_state: Optional[dict] = None) -> None:
    """Increment consecutive_failures on the state doc after a fetch exception.

    After DORMANT_FAILURE_THRESHOLD (3) consecutive failures, also flag the
    slug as dormant so the next crawl skips it. This handles slug renames /
    ATS migrations without waiting 60 days of empty-crawl accrual (which is
    the other dormancy path — see write_state).

    Doesn't touch board_hash or kept_job_ids (a failed crawl doesn't imply
    the board changed).
    """
    db = get_db()
    if not db:
        raise RuntimeError("Firestore DB not initialized")
    prev_fail = int((prior_state or {}).get("consecutive_failures") or 0)
    new_fail = prev_fail + 1
    update: dict = {
        "platform": platform,
        "slug": slug,
        "consecutive_failures": new_fail,
        "last_crawled_at": datetime.now(timezone.utc),
    }
    if new_fail >= DORMANT_FAILURE_THRESHOLD:
        update["dormant"] = True
        logger.warning(
            "auto-dormant %s/%s after %d consecutive failures",
            platform, slug, new_fail,
        )
    db.collection(COLLECTION).document(_doc_id(platform, slug)).set(update, merge=True)


def is_dormant(state: Optional[dict]) -> bool:
    """True if the slug has been empty longer than DORMANT_THRESHOLD_DAYS.

    A missing state doc means "never crawled" — not dormant, just new.
    """
    if not state:
        return False
    return bool(state.get("dormant"))
