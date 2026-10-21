"""
Write normalized jobs to Firestore 'jobs' collection.
Deduplicates by job_id and handles batch writes.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from google.cloud.firestore_v1.base_query import FieldFilter

from backend.app.extensions import get_db
from backend.pipeline import crawl_state

logger = logging.getLogger(__name__)

COLLECTION = "jobs"
BATCH_WRITE_SIZE = 400
EXISTENCE_CHECK_CHUNK = 300
DELETE_BATCH_SIZE = 500


def _apply_enrichment_gate(doc: dict) -> None:
    """Stamp enrichment_status / title_enrichment_status based on relevance_tier.

    Only tier-1 (early-career + target function) gets flagged for the
    expensive Firecrawl + PDL + Perplexity enrichment path. Everything else
    is marked skipped_low_priority so pipeline/enricher.py leaves it alone.
    Prevents enrichment-cost explosion when we scale from 270 → 10K slugs.

    Mutates the doc in place. Safe on docs already carrying either field
    (setdefault preserves anything explicitly set upstream).
    """
    tier = doc.get("relevance_tier")
    if tier == 1:
        doc.setdefault("enrichment_status", "pending")
        doc.setdefault("title_enrichment_status", "pending")
    else:
        doc.setdefault("enrichment_status", "skipped_low_priority")
        doc.setdefault("title_enrichment_status", "skipped_low_priority")


def write_jobs(normalized_jobs: list[dict]) -> dict:
    """
    Write net-new jobs to Firestore. Skips any job_id that already exists.
    Returns { written, skipped_duplicates, total }.
    """
    db = get_db()
    if not db:
        raise RuntimeError("Firestore DB not initialized")

    total = len(normalized_jobs)
    if total == 0:
        return {"written": 0, "skipped_duplicates": 0, "total": 0}

    # Build lookup: job_id -> doc
    jobs_by_id = {}
    for job in normalized_jobs:
        jid = job["job_id"]
        if jid not in jobs_by_id:
            jobs_by_id[jid] = job

    all_ids = list(jobs_by_id.keys())

    # Check which already exist in chunks
    existing_ids = set()
    for i in range(0, len(all_ids), EXISTENCE_CHECK_CHUNK):
        chunk = all_ids[i : i + EXISTENCE_CHECK_CHUNK]
        refs = [db.collection(COLLECTION).document(jid) for jid in chunk]
        docs = db.get_all(refs)
        for doc in docs:
            if doc.exists:
                existing_ids.add(doc.id)

    # Filter to net-new only
    new_jobs = {jid: doc for jid, doc in jobs_by_id.items() if jid not in existing_ids}
    skipped = len(jobs_by_id) - len(new_jobs)

    # Write in batches
    written = 0
    new_items = list(new_jobs.items())
    for i in range(0, len(new_items), BATCH_WRITE_SIZE):
        batch = db.batch()
        chunk = new_items[i : i + BATCH_WRITE_SIZE]
        for jid, doc in chunk:
            # Tier-gated: only relevance_tier==1 flags for Firecrawl / PDL
            # enrichment. Prevents cost explosion when we scale to 10K slugs.
            _apply_enrichment_gate(doc)
            ref = db.collection(COLLECTION).document(jid)
            batch.set(ref, doc)
        batch.commit()
        written += len(chunk)
        logger.info("  Batch write: %d jobs committed", len(chunk))

    # Embed newly-written jobs so Firestore vector search has them ready.
    # Fail-soft: embedding failure never fails the write; the on-demand
    # embed path in embedding_ranker will catch stragglers, and the
    # backfill script covers anything both miss.
    embed_count = _embed_new_jobs_batch(list(new_jobs.values()))
    if embed_count:
        logger.info("  Embedded %d newly-written jobs at ingest", embed_count)

    result = {"written": written, "skipped_duplicates": skipped, "total": total}
    logger.info("Write complete: %s", result)
    return result


def _embed_new_jobs_batch(new_jobs: list[dict]) -> int:
    """Compute embeddings for newly-written jobs and upsert into
    job_embeddings collection with filter attrs mirrored so Firestore
    vector search prefilters work.

    Batched: OpenAI batch-embed handles up to 2048 inputs per call; we
    chunk at 500 to keep memory pressure low and align with the
    embedding_ranker's EMBED_BATCH_SIZE budget. Never raises.
    """
    if not new_jobs:
        return 0

    try:
        from backend.app.utils.embedding_ranker import (
            _embed_batch,
            _job_text,
            EMBEDDING_DIM,
        )
        from backend.app.services.vector_store import upsert_job_embedding
    except Exception as e:
        logger.warning("embed-at-ingest: imports failed: %s", e)
        return 0

    CHUNK = 500
    total_embedded = 0
    for start in range(0, len(new_jobs), CHUNK):
        chunk = new_jobs[start : start + CHUNK]
        texts = [_job_text(j) for j in chunk]

        try:
            embs = _embed_batch(texts)
        except Exception as e:
            logger.warning("embed-at-ingest: batch failed: %s", e)
            continue

        for job, emb in zip(chunk, embs):
            if not emb or not isinstance(emb, list) or len(emb) != EMBEDDING_DIM:
                continue
            jid = job.get("job_id")
            if not jid:
                continue
            ok = upsert_job_embedding(
                jid,
                emb,
                filter_attrs={
                    "expired": bool(job.get("expired", False)),
                    "career_domain": job.get("career_domain"),
                    "source": job.get("source"),
                },
            )
            if ok:
                total_embedded += 1

    return total_embedded


def _expire_by_firestore_ids(firestore_ids: list[str]) -> int:
    """Core expiry primitive: mark each Firestore job doc as expired.

    Verifies existence (avoids empty-doc updates from unknown IDs), batch-
    updates, and mirrors the flag onto job_embeddings so vector search
    prefilters correctly. Returns count actually flagged.

    Public callers are mark_expired_jobs (FJ daemon) and sync_board_jobs
    (direct-ATS diff expiry). Kept private so both callers share one
    implementation.
    """
    db = get_db()
    if not db:
        raise RuntimeError("Firestore DB not initialized")
    if not firestore_ids:
        return 0

    now = datetime.now(timezone.utc)

    existing_ids: set[str] = set()
    for i in range(0, len(firestore_ids), EXISTENCE_CHECK_CHUNK):
        chunk = firestore_ids[i : i + EXISTENCE_CHECK_CHUNK]
        refs = [db.collection(COLLECTION).document(jid) for jid in chunk]
        for doc in db.get_all(refs):
            if doc.exists:
                existing_ids.add(doc.id)

    marked = 0
    targets = list(existing_ids)
    for i in range(0, len(targets), BATCH_WRITE_SIZE):
        batch = db.batch()
        chunk = targets[i : i + BATCH_WRITE_SIZE]
        for jid in chunk:
            ref = db.collection(COLLECTION).document(jid)
            batch.update(ref, {"expired": True, "expired_at": now})
        batch.commit()
        marked += len(chunk)
        logger.info("  Expired-mark batch: %d jobs flagged", len(chunk))

    # Mirror onto job_embeddings so vector-search prefilters catch expiries.
    if targets:
        try:
            from backend.app.services.vector_store import mark_expired as _mark_vec_expired
            vec_marked = _mark_vec_expired(list(targets))
            logger.info("  Mirrored expired=true onto %d embedding docs", vec_marked)
        except Exception as e:
            logger.warning("Failed to mirror expired flag into job_embeddings: %s", e)

    return marked


def mark_expired_jobs(fj_ids: list[str]) -> dict:
    """Flag Firestore docs as expired based on the Fantastic.jobs Expired Jobs feed.

    Args:
        fj_ids: Raw FJ-side IDs (not yet prefixed). Each is translated to our
            Firestore job_id of the form `fantasticjobs_{id}` before update.

    Returns: {"marked": N, "not_found": M, "total": len(fj_ids)}.

    Doesn't delete — only sets `expired=true` and `expired_at`. Downstream
    job-board reads should filter expired=true. Keeping the doc lets the
    UI optionally show "this role closed" for users who saved it.
    """
    if not fj_ids:
        return {"marked": 0, "not_found": 0, "total": 0}
    firestore_ids = [f"fantasticjobs_{fid}" for fid in fj_ids]
    marked = _expire_by_firestore_ids(firestore_ids)
    result = {
        "marked": marked,
        "not_found": len(fj_ids) - marked,
        "total": len(fj_ids),
    }
    logger.info("Expired sweep complete: %s", result)
    return result


def sync_board_jobs(
    platform: str,
    slug: str,
    snapshot_jobs: list[dict],
    *,
    prior_state: Optional[dict] = None,
) -> dict:
    """Reconcile one board's full snapshot against Firestore state.

    The direct-ATS pipeline's core primitive. Steps:
      1. Hash the snapshot's (job_id, posted_at) pairs.
      2. If hash matches the prior state, this board hasn't changed since
         the last crawl — return with zero Firestore writes. Co-founder's
         plan projects 85-95% skip rate at steady state, which is what
         keeps naive-$54/mo Firestore cost at ~$1-3/mo.
      3. Otherwise diff current vs prior kept_job_ids:
           new_ids     = in snapshot, not in prior kept
           removed_ids = in prior kept, not in snapshot
      4. Batch-write new jobs (skipping existence check — the diff already
         guarantees they're new relative to what we last saw).
      5. Mark removed_ids as expired=True (auto-apply safety — filled roles
         must never appear in a live feed once we scale to 10K slugs).
      6. Persist updated state via crawl_state.write_state.

    Args:
        platform: "greenhouse" | "lever" | "ashby"
        slug: company slug (e.g. "stripe")
        snapshot_jobs: normalized job dicts with `direct_{platform}_{slug}_*`
            prefixed job_ids
        prior_state: pre-fetched state from crawl_state.read_state_batch; if
            omitted, this function will read the state doc itself (adds one
            Firestore read per call — batch reads at the caller are cheaper
            for large sweeps).

    Returns: {board_hash_matched, snapshot_count, written, expired}.
    """
    db = get_db()
    if not db:
        raise RuntimeError("Firestore DB not initialized")

    snapshot_count = len(snapshot_jobs)
    new_hash = crawl_state.compute_board_hash(snapshot_jobs)

    if prior_state is None:
        prior_state = crawl_state.read_state(platform, slug)

    prior_hash = (prior_state or {}).get("board_hash")
    prior_kept = set((prior_state or {}).get("kept_job_ids") or [])
    snapshot_ids = [j["job_id"] for j in snapshot_jobs if j.get("job_id")]
    snapshot_set = set(snapshot_ids)

    if prior_hash == new_hash and prior_hash is not None:
        logger.info(
            "  sync[%s/%s]: hash match, %d jobs unchanged, no writes",
            platform, slug, snapshot_count,
        )
        return {
            "board_hash_matched": True,
            "snapshot_count": snapshot_count,
            "written": 0,
            "expired": 0,
        }

    new_ids = snapshot_set - prior_kept
    removed_ids = prior_kept - snapshot_set

    # Write new jobs — skip existence check since diff already isolates
    # net-new relative to prior kept_ids. Any collision that DOES survive
    # (rare, from partial prior crawls) gets overwritten cleanly.
    new_jobs = [j for j in snapshot_jobs if j.get("job_id") in new_ids]
    written = 0
    if new_jobs:
        for i in range(0, len(new_jobs), BATCH_WRITE_SIZE):
            batch = db.batch()
            chunk = new_jobs[i : i + BATCH_WRITE_SIZE]
            for doc in chunk:
                _apply_enrichment_gate(doc)
                ref = db.collection(COLLECTION).document(doc["job_id"])
                batch.set(ref, doc)
            batch.commit()
            written += len(chunk)
        logger.info("  sync[%s/%s]: wrote %d new", platform, slug, written)

        # Fail-soft embedding, same pattern as write_jobs.
        try:
            _embed_new_jobs_batch(new_jobs)
        except Exception as e:
            logger.warning("sync[%s/%s]: embed failed: %s", platform, slug, e)

    expired_count = 0
    if removed_ids:
        expired_count = _expire_by_firestore_ids(list(removed_ids))
        logger.info("  sync[%s/%s]: expired %d removed", platform, slug, expired_count)

    # Phase 3 signal: how many of this snapshot's kept jobs graded tier-1.
    # Used later to decide whether a cold slug deserves hot promotion.
    tier1_count = sum(1 for j in snapshot_jobs if j.get("relevance_tier") == 1)

    crawl_state.write_state(
        platform,
        slug,
        board_hash=new_hash,
        kept_job_ids=snapshot_ids,
        jobs_count=snapshot_count,
        tier1_count=tier1_count,
        prior_state=prior_state,
    )

    return {
        "board_hash_matched": False,
        "snapshot_count": snapshot_count,
        "written": written,
        "expired": expired_count,
    }


_DIRECT_ATS_PREFIXES = ("direct_greenhouse_", "direct_lever_", "direct_ashby_")


def _ats_board(doc_id: str) -> Optional[tuple[str, str]]:
    """(platform, slug) for a direct-ATS job id, else None."""
    for prefix in _DIRECT_ATS_PREFIXES:
        if doc_id.startswith(prefix):
            platform = prefix[len("direct_"):-1]
            slug = doc_id[len(prefix):].rsplit("_", 1)[0]
            return platform, slug
    return None


def _ttl_deletable(doc, board_state) -> bool:
    """Direct-ATS jobs are written once and never rewritten on unchanged
    boards, so their expires_at lapses while they are still posted (and
    kept_job_ids stops them ever being re-added). Delete them only once
    sync_board_jobs marked them expired, or their board is no longer
    crawled (dormant or no state), since then nothing would expire them."""
    board = _ats_board(doc.id)
    if board is None:
        return True
    if (doc.to_dict() or {}).get("expired") is True:
        return True
    state = board_state(*board)
    return state is None or crawl_state.is_dormant(state)


def delete_expired_jobs() -> int:
    """Delete jobs where expires_at < now, except live direct-ATS jobs.
    Returns total deleted."""
    db = get_db()
    if not db:
        raise RuntimeError("Firestore DB not initialized")

    states: dict = {}

    def board_state(platform: str, slug: str):
        key = (platform, slug)
        if key not in states:
            states[key] = crawl_state.read_state(platform, slug)
        return states[key]

    now = datetime.now(timezone.utc)
    query = db.collection(COLLECTION).where(filter=FieldFilter("expires_at", "<", now))
    refs = [doc.reference for doc in query.stream() if _ttl_deletable(doc, board_state)]

    for i in range(0, len(refs), DELETE_BATCH_SIZE):
        chunk = refs[i : i + DELETE_BATCH_SIZE]
        batch = db.batch()
        for ref in chunk:
            batch.delete(ref)
        batch.commit()
        logger.info("  Deleted batch of %d expired jobs", len(chunk))

    logger.info("Total expired jobs deleted: %d", len(refs))
    return len(refs)
