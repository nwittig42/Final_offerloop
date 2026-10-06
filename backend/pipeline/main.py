#!/usr/bin/env python3
"""
Offerloop Job Pipeline — entry point.

Usage:
    python pipeline/main.py                          # Default: full pipeline EXCEPT the paid 7d Fantastic.jobs sweep
    python pipeline/main.py --include-fantastic-7d    # Full pipeline INCLUDING the paid 7d FJ sweep (~500-1000 Jobs credits)
    python pipeline/main.py --skip-fantastic          # (Alias for default — kept for backward compat)
    python pipeline/main.py --fantastic-only          # Fantastic.jobs only (7d window) — gated by FJ_FULL_BACKFILL_ENABLED=true
    python pipeline/main.py --fantastic-modified      # FJ daily delta via /modified-ats-24h (no Jobs credits)
    python pipeline/main.py --sweep-expired           # FJ Expired Jobs sweep — mark Firestore docs expired=true
    python pipeline/main.py --cleanup                 # Delete expired jobs only
    python pipeline/main.py --fix-salaries            # Recalculate WEEK salaries
    python pipeline/main.py --enrich-only             # Firecrawl JD enrichment for pending jobs
    python pipeline/main.py --enrich-only --limit=300 # Custom batch size (caps at 500)
    python pipeline/main.py --backfill-enrich                            # Backfill legacy jobs (default: last 14 days only)
    python pipeline/main.py --backfill-enrich --since-days=30 --limit=2000  # Custom backfill window + cap
    python pipeline/main.py --title-enrich-only       # PDL title enrichment for pending jobs
    python pipeline/main.py --backfill-title-enrich   # Backfill title enrichment for legacy jobs
    python pipeline/main.py --crawl-ats --tier=hot                  # Direct-ATS crawl of hot-tier slugs (~270 curated)
    python pipeline/main.py --crawl-ats --tier=cold --shard=0/4     # Cold-tier shard 0 of 4 (~2400 slugs)
    python pipeline/main.py --health-snapshot                       # Read-only current-state metrics → pipeline_runs
"""
from dotenv import load_dotenv
load_dotenv()

import sys
import os
import logging
import uuid
from collections import Counter
from datetime import datetime, timezone

# Ensure both project root (for `from backend.app.*`) and the backend/
# directory (for `from app.*` — Flask-style relative imports used by
# services like firecrawl_client) are on sys.path. The enricher transitively
# imports modules that rely on the latter.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))           # …/backend/pipeline
_BACKEND_DIR = os.path.dirname(_THIS_DIR)                         # …/backend
_PROJECT_ROOT = os.path.dirname(_BACKEND_DIR)                     # repo root
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _BACKEND_DIR)

from flask import Flask
from backend.app.extensions import init_firebase

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

PIPELINE_RUNS_COLLECTION = "pipeline_runs"


def _bootstrap_app():
    """Create a minimal Flask app and initialize Firebase (matching existing admin script pattern)."""
    app = Flask(__name__)
    init_firebase(app)
    return app


def _source_breakdown(raw: list[dict]) -> dict:
    counts = Counter((item.get("source") or "unknown") for item in raw)
    return dict(counts)


def _fetch_existing_coresignal_ids() -> set[str]:
    """Pull job_ids of Coresignal-sourced jobs already in Firestore. Used by
    the Coresignal fetcher to skip Collect on jobs we already have (each
    duplicate Collect wastes a Starter-tier credit).

    Only pages source='coresignal' to keep the read cheap. Falls back to
    an empty set on any Firestore error.
    """
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter
        from backend.app.extensions import get_db

        db = get_db()
        if not db:
            return set()

        query = db.collection("jobs").where(
            filter=FieldFilter("source", "==", "coresignal")
        ).select([])  # projection: __name__ only, no other fields transferred
        return {doc.id for doc in query.stream()}
    except Exception as exc:
        logger.warning("Coresignal existing-id lookup failed: %s", exc)
        return set()


def _write_run_log(mode: str, started_at: datetime, result: dict | None, error: str | None = None):
    """Write a pipeline_runs/{run_id} doc summarizing this run. Never raises."""
    try:
        from backend.app.extensions import get_db
        db = get_db()
        if not db:
            logger.warning("Skipping pipeline_runs log: Firestore not initialized")
            return
        ended_at = datetime.now(timezone.utc)
        run_id = ended_at.strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:6]
        r = result or {}
        # The full-pipeline result dict uses {written, skipped_duplicates,
        # total}; the enricher uses {processed, enriched, failed, skipped}.
        # Fall back from total → processed so the unified `total` field on
        # pipeline_runs is meaningful for BOTH modes. Without this fallback,
        # enrich-only runs always logged total=0 even when they processed
        # hundreds of jobs — masking real failures (see FIRECRAWL_API_KEY
        # secret bug fixed today).
        unified_total = r.get("total", r.get("processed", 0))
        doc = {
            "run_id": run_id,
            "mode": mode,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": (ended_at - started_at).total_seconds(),
            "written": r.get("written", 0),
            "skipped_duplicates": r.get("skipped_duplicates", 0),
            "total": unified_total,
            "processed": r.get("processed"),  # enricher-only, kept explicit too
            "source_breakdown": r.get("source_breakdown") or {},
            "deleted": (result or {}).get("deleted", 0),
            "error": error,
            "ok": error is None,
        }
        db.collection(PIPELINE_RUNS_COLLECTION).document(run_id).set(doc)
        logger.info("pipeline_runs/%s written (ok=%s)", run_id, error is None)
    except Exception as e:
        logger.warning("Failed to write pipeline_runs log: %s", e)


def _gate(normalized: list[dict]) -> tuple[list[dict], dict]:
    """Filter normalized docs through the quality gate before write.

    Drops staffing-agency reposts, senior-only roles inappropriate for the
    undergrad audience, and postings >60 days old. Returns (kept, drops_dict).
    If the gate itself raises, log and pass everything through — better noisy
    results than zero results."""
    try:
        from backend.pipeline.quality_gate import apply as gate_apply
        kept, drops = gate_apply(normalized)
        logger.info("Quality gate kept %d / %d jobs", len(kept), len(normalized))
        return kept, drops
    except Exception:
        logger.warning("quality_gate failed — bypassing", exc_info=True)
        return normalized, {}


def run_pipeline(skip_fantastic: bool = False):
    from backend.pipeline.fetcher import fetch_jobs
    from backend.pipeline.coresignal import fetch_all_coresignal
    from backend.pipeline.normalizer import normalize_all
    from backend.pipeline.writer import write_jobs

    sources = "Greenhouse, Lever, Ashby, Simplify, Coresignal" + ("" if skip_fantastic else ", Fantastic.jobs")
    logger.info("Fetching jobs from %s...", sources)
    raw = fetch_jobs(skip_fantastic=skip_fantastic)

    # Coresignal runs separately so it can dedup against jobs already in
    # Firestore before spending Collect credits. It also has a strict
    # per-run credit budget (see backend/pipeline/coresignal.py). Skipped
    # entirely if CORESIGNAL_API_KEY is not set.
    try:
        existing_ids = _fetch_existing_coresignal_ids()
        coresignal_jobs = fetch_all_coresignal(existing_job_ids=existing_ids)
        raw.extend(coresignal_jobs)
    except Exception as exc:
        logger.warning("Coresignal fetch failed (non-fatal): %s", exc)

    breakdown = _source_breakdown(raw)

    logger.info("Normalizing %d raw results...", len(raw))
    normalized = normalize_all(raw)

    gated, drops = _gate(normalized)

    logger.info("Writing %d normalized jobs to Firestore...", len(gated))
    result = write_jobs(gated)
    result["source_breakdown"] = breakdown
    result["quality_gate_drops"] = drops

    print()
    print("Pipeline complete.")
    print(f"  New jobs written:     {result['written']}")
    print(f"  Duplicates skipped:   {result['skipped_duplicates']}")
    print(f"  Total processed:      {result['total']}")
    print(f"  Source breakdown:     {breakdown}")
    return result


def run_fantastic_only():
    from backend.pipeline.fetcher import fetch_fantasticjobs
    from backend.pipeline.normalizer import normalize_all
    from backend.pipeline.writer import write_jobs

    # Guard: the 7d window hits /active-ats-7d which spends Jobs credits
    # (~500-1000 per full run across the 10 student-cycle recipes). Require
    # explicit opt-in via env so an accidental --fantastic-only doesn't burn
    # the monthly quota.
    if os.getenv("FJ_FULL_BACKFILL_ENABLED", "false").lower() != "true":
        print(
            "FJ_FULL_BACKFILL_ENABLED is not 'true' — refusing to run the 7d "
            "Fantastic.jobs backfill (~500-1000 Jobs credits/run). "
            "Set FJ_FULL_BACKFILL_ENABLED=true to override."
        )
        sys.exit(2)

    logger.info("Fetching jobs from Fantastic.jobs only...")
    raw = fetch_fantasticjobs()
    breakdown = _source_breakdown(raw)

    logger.info("Normalizing %d raw results...", len(raw))
    normalized = normalize_all(raw)

    gated, drops = _gate(normalized)

    logger.info("Writing %d normalized jobs to Firestore...", len(gated))
    result = write_jobs(gated)
    result["source_breakdown"] = breakdown
    result["quality_gate_drops"] = drops

    print()
    print("Fantastic.jobs pipeline complete.")
    print(f"  New jobs written:     {result['written']}")
    print(f"  Duplicates skipped:   {result['skipped_duplicates']}")
    print(f"  Total processed:      {result['total']}")
    print(f"  Source breakdown:     {breakdown}")
    return result


def run_fantastic_modified():
    """Daily delta from FJ Modified Jobs (/modified-ats-24h).

    Doesn't burn Jobs credits — only 1 Request credit per recipe call.
    Recommended cron: once per day at a fixed UTC time.
    """
    from backend.pipeline.fetcher import fetch_fantasticjobs_modified
    from backend.pipeline.normalizer import normalize_all
    from backend.pipeline.writer import write_jobs

    logger.info("Fetching modified jobs from Fantastic.jobs (last 24h)...")
    raw = fetch_fantasticjobs_modified()
    breakdown = _source_breakdown(raw)

    logger.info("Normalizing %d raw results...", len(raw))
    normalized = normalize_all(raw)

    gated, drops = _gate(normalized)

    logger.info("Writing %d normalized jobs to Firestore...", len(gated))
    result = write_jobs(gated)
    result["source_breakdown"] = breakdown
    result["quality_gate_drops"] = drops

    print()
    print("Fantastic.jobs modified-delta pipeline complete.")
    print(f"  New jobs written:     {result['written']}")
    print(f"  Duplicates skipped:   {result['skipped_duplicates']}")
    print(f"  Total processed:      {result['total']}")
    print(f"  Source breakdown:     {breakdown}")
    return result


def run_sweep_expired():
    """Pull FJ Expired Jobs ID list and mark matching Firestore docs.

    Doesn't burn Jobs credits — only 1 Request credit. Recommended cron:
    once per day, ideally right after the modified-delta run.
    """
    from backend.pipeline.fetcher import fetch_expired_job_ids
    from backend.pipeline.writer import mark_expired_jobs

    logger.info("Fetching expired job IDs from Fantastic.jobs...")
    ids = fetch_expired_job_ids()
    result = mark_expired_jobs(ids)

    print()
    print("Expired-jobs sweep complete.")
    print(f"  IDs returned by FJ:   {result['total']}")
    print(f"  Marked expired:       {result['marked']}")
    print(f"  Not in our corpus:    {result['not_found']}")
    return result


def run_fix_salaries():
    from backend.app.extensions import get_db
    from backend.pipeline.normalizer import _format_salary_display, _salary_normalized_annual

    db = get_db()
    if not db:
        raise RuntimeError("Firestore DB not initialized")

    logger.info("Scanning jobs for WEEK salary fixes...")
    docs_to_fix = []
    for doc in db.collection("jobs").stream():
        data = doc.to_dict()
        if data.get("salary_extracted") and data.get("salary_period") == "WEEK":
            docs_to_fix.append((doc.reference, data))

    if not docs_to_fix:
        print("No WEEK-period extracted salaries found. Nothing to fix.")
        return 0

    batch = db.batch()
    for ref, data in docs_to_fix:
        sal_min = data.get("salary_min")
        sal_max = data.get("salary_max")
        batch.update(ref, {
            "salary_normalized_annual": _salary_normalized_annual(sal_min, sal_max, "WEEK"),
            "salary_display": _format_salary_display(sal_min, sal_max, "WEEK", True),
        })
    batch.commit()

    print()
    print(f"Fixed {len(docs_to_fix)} jobs with WEEK salary period.")
    return len(docs_to_fix)


def run_cleanup():
    from backend.pipeline.writer import delete_expired_jobs

    logger.info("Running expired job cleanup...")
    deleted = delete_expired_jobs()

    print()
    print("Cleanup complete.")
    print(f"  Expired jobs deleted: {deleted}")
    return {"deleted": deleted}


def run_enrich(limit: int = 200, backfill: bool = False, since_days: int | None = None):
    """Firecrawl-backed JD enrichment: fill in `structured` on pending jobs."""
    from backend.pipeline.enricher import enrich_jobs

    logger.info("Running enricher (limit=%d, backfill=%s, since_days=%s)...",
                limit, backfill, since_days)
    result = enrich_jobs(limit=limit, backfill=backfill, since_days=since_days)

    print()
    print("Enrichment complete.")
    print(f"  Processed:           {result.get('processed', 0)}")
    print(f"  Enriched (structured saved): {result.get('enriched', 0)}")
    print(f"  Failed:              {result.get('failed', 0)}")
    print(f"  Skipped (no URL):    {result.get('skipped', 0)}")
    print(f"  Estimated cost:      ${result.get('cost_estimate_usd', 0.0):.4f}")
    return result


def run_title_enrich(limit: int = 200, backfill: bool = False, since_days: int | None = None):
    """PDL Job Title Enrichment: fill in structured.title_meta on pending jobs.

    Mirrors run_enrich's shape. Budget guards live in pdl_title_cache (per-run
    cap, 45k circuit breaker, persistent Firestore cache).
    """
    from backend.pipeline.title_enricher import enrich_titles

    logger.info("Running title-enricher (limit=%d, backfill=%s, since_days=%s)...",
                limit, backfill, since_days)
    result = enrich_titles(limit=limit, backfill=backfill, since_days=since_days)

    print()
    print("Title enrichment complete.")
    print(f"  Processed:           {result.get('processed', 0)}")
    print(f"  Enriched (signal):   {result.get('enriched', 0)}")
    print(f"  Noop (no synonyms):  {result.get('noop', 0)}")
    print(f"  Skipped (no title):  {result.get('skipped', 0)}")
    print(f"  PDL calls used:      {result.get('pdl_calls', 0)} (rest were cache hits)")
    return result


def run_extract_deadlines(limit: int = 50):
    """Perplexity-backed deadline extraction for consulting/IB/quant jobs.

    Phase 4 of the Job Board Elevation Plan. Targets cycle-driven categories
    where the deadline is rarely on the individual posting but is well-known
    from the broader recruiting calendar.
    """
    from backend.pipeline.deadline_extractor import extract_deadlines

    logger.info("Running deadline extractor (limit=%d)...", limit)
    result = extract_deadlines(limit=limit)

    print()
    print("Deadline extraction complete.")
    print(f"  Processed:           {result.get('processed', 0)}")
    print(f"  Completed:           {result.get('completed', 0)}")
    print(f"  Failed:              {result.get('failed', 0)}")
    print(f"  Skipped:             {result.get('skipped', 0)}")
    print(f"  Estimated cost:      ${result.get('cost_estimate_usd', 0.0):.4f}")
    return result


def _parse_limit(default: int = 200) -> int:
    for arg in sys.argv:
        if arg.startswith("--limit="):
            try:
                return max(1, int(arg.split("=", 1)[1]))
            except ValueError:
                pass
    return default


def _parse_since_days(default: int | None = None) -> int | None:
    for arg in sys.argv:
        if arg.startswith("--since-days="):
            try:
                return max(1, int(arg.split("=", 1)[1]))
            except ValueError:
                pass
    return default


def run_health_snapshot() -> dict:
    """Read-only pipeline health snapshot. Emits current-state metrics that
    aren't visible from per-crawl telemetry alone.

    Metrics:
      - jobs_total                — count of all docs in jobs/
      - jobs_last_24h             — feed-visibility (matches feed query window)
      - jobs_last_24h_by_tier     — tier 1 / 2 / 3 count in the same window
      - jobs_last_24h_eligible    — auto-apply-eligible count in the window
      - slug_state_total          — ats_crawl_state doc count
      - slug_state_dormant        — count where dormant=True (auto-pruned)
      - slug_state_dormant_pct    — dormancy ratio

    Runs 5-6 Firestore aggregate queries (all cheap — count() is < $0.001).
    Writes result to pipeline_runs under mode='health-snapshot' for trend
    tracking. Also logged/printed for GH Actions run visibility.
    """
    from datetime import datetime, timedelta, timezone
    from backend.app.extensions import get_db
    from backend.app.services.auto_apply.ats_detector import is_eligible

    db = get_db()
    if not db:
        raise RuntimeError("Firestore DB not initialized")

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=24)

    def _agg_count(query) -> int:
        try:
            snap = query.count().get()
            # Firestore returns [[AggregationResult]] shape
            return int(snap[0][0].value)
        except Exception as e:
            logger.warning("aggregate count failed: %s", e)
            return -1

    jobs_ref = db.collection("jobs")
    state_ref = db.collection("ats_crawl_state")

    jobs_total = _agg_count(jobs_ref)
    jobs_last_24h = _agg_count(jobs_ref.where("posted_at", ">=", window_start))
    slug_state_total = _agg_count(state_ref)
    slug_state_dormant = _agg_count(state_ref.where("dormant", "==", True))

    # Single sample-scan handles both eligibility % AND tier distribution —
    # avoids needing composite indexes for every (posted_at, relevance_tier)
    # combination. Capped at 500 for speed; extrapolate to the full 24h pool.
    sample_size = 0
    sample_eligible = 0
    tier_sample: dict[int, int] = {1: 0, 2: 0, 3: 0, 0: 0}
    for doc in jobs_ref.where("posted_at", ">=", window_start).limit(500).stream():
        sample_size += 1
        d = doc.to_dict() or {}
        if is_eligible(d):
            sample_eligible += 1
        t = d.get("relevance_tier")
        tier_sample[t if t in (1, 2, 3) else 0] += 1
    eligible_pct = round(100 * sample_eligible / max(sample_size, 1), 1)
    est_eligible_last_24h = int(jobs_last_24h * eligible_pct / 100) if jobs_last_24h > 0 else 0
    tier_counts = {
        f"tier{t}_est": int(jobs_last_24h * tier_sample[t] / max(sample_size, 1))
        for t in (1, 2, 3)
    }
    tier_counts["untiered_est"] = int(jobs_last_24h * tier_sample[0] / max(sample_size, 1))

    snap = {
        "captured_at": now.isoformat(),
        "jobs_total": jobs_total,
        "jobs_last_24h": jobs_last_24h,
        "jobs_last_24h_by_tier_est": tier_counts,
        "sample_size": sample_size,
        "auto_apply_eligible_sample_pct": eligible_pct,
        "auto_apply_eligible_last_24h_est": est_eligible_last_24h,
        "slug_state_total": slug_state_total,
        "slug_state_dormant": slug_state_dormant,
        "slug_state_dormant_pct": round(
            100 * slug_state_dormant / max(slug_state_total, 1), 1
        ),
    }
    logger.info("health snapshot: %s", snap)
    return snap


def _parse_tier(default: str = "hot") -> str:
    for arg in sys.argv:
        if arg.startswith("--tier="):
            val = arg.split("=", 1)[1].strip().lower()
            if val in ("hot", "cold", "all"):
                return val
    return default


def _parse_shard() -> tuple[int, int] | None:
    """Parse --shard=N/M into (N, M). Only valid for cold-tier crawls."""
    for arg in sys.argv:
        if arg.startswith("--shard="):
            try:
                n_str, m_str = arg.split("=", 1)[1].split("/", 1)
                n, m = int(n_str), int(m_str)
                if m > 1 and 0 <= n < m:
                    return (n, m)
            except ValueError:
                pass
    return None


def run_crawl_ats(tier: str = "hot", shard: tuple[int, int] | None = None) -> dict:
    """Direct-ATS scale-up crawler (Phase 0 orchestrator).

    For each platform (greenhouse / lever / ashby):
      1. Load slugs from slug_loader for the requested tier + shard
      2. Batch-read prior crawl state for those slugs
      3. Parallel per-platform pool: fetch → normalize → sync_board_jobs
      4. Aggregate results

    Cold tier runs the strict positive-allowlist quality gate; hot tier runs
    the original drop-list. Both stamp relevance_tier on kept docs.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from backend.pipeline import fetcher, writer, slug_loader, crawl_state
    from backend.pipeline.normalizer import normalize_job, _is_non_us_non_remote
    from backend.pipeline.quality_gate import apply as apply_quality_gate

    gate_mode = tier if tier in ("hot", "cold") else "hot"
    platforms = {
        "greenhouse": fetcher._fetch_greenhouse,
        "lever": fetcher._fetch_lever,
        "ashby": fetcher._fetch_ashby,
    }

    def _empty_platform_stats() -> dict:
        return {
            "slugs_crawled": 0,
            "slugs_failed": 0,
            "board_hash_matched": 0,
            "snapshot_count": 0,
            "written": 0,
            "expired": 0,
            "tier1": 0,
            "tier2": 0,
            "tier3": 0,
        }

    totals = {
        "tier": tier,
        "shard": f"{shard[0]+1}/{shard[1]}" if shard else "all",
        "slugs_crawled": 0,
        "slugs_failed": 0,
        "board_hash_matched": 0,
        "snapshot_count": 0,
        "written": 0,
        "expired": 0,
        "tier1": 0,
        "tier2": 0,
        "tier3": 0,
        "per_platform": {p: _empty_platform_stats() for p in platforms},
    }

    def _crawl_one(platform: str, slug: str, prior_state):
        """Fetch → normalize → gate → sync. Returns sync result + tier counts."""
        raw = platforms[platform](slug)
        # normalize per-slug (mirror the pieces of normalize_all we need)
        normalized: list[dict] = []
        for r in raw:
            doc = normalize_job(r)
            if not doc or _is_non_us_non_remote(doc):
                continue
            normalized.append(doc)
        kept, _ = apply_quality_gate(normalized, mode=gate_mode)
        # Tier breakdown of the kept snapshot (before sync writes).
        # Note: not all kept jobs are "new writes" — sync will diff against
        # prior state. But tier distribution of what surfaces is the metric
        # that matters for feed quality.
        tiers = {"tier1": 0, "tier2": 0, "tier3": 0}
        for d in kept:
            key = f"tier{d.get('relevance_tier') or 3}"
            if key in tiers:
                tiers[key] += 1
        result = writer.sync_board_jobs(platform, slug, kept, prior_state=prior_state)
        result.update(tiers)
        return result

    for platform in platforms:
        slugs = slug_loader.load_slugs(platform, tier=tier, shard=shard)
        logger.info(
            "[%s/%s] loaded %d slugs%s",
            platform, tier, len(slugs),
            f" (shard {shard[0]+1}/{shard[1]})" if shard else "",
        )
        if not slugs:
            continue

        # One batched read up front — 5-6 Firestore round-trips even for a 2500-slug shard.
        state_map = crawl_state.read_state_batch(platform, slugs)

        pool_size = fetcher.POOL_SIZE.get(platform, 8)
        with ThreadPoolExecutor(max_workers=pool_size) as pool:
            futures = {
                pool.submit(_crawl_one, platform, slug, state_map.get(slug)): slug
                for slug in slugs
            }
            for future in as_completed(futures):
                slug = futures[future]
                p_stats = totals["per_platform"][platform]
                try:
                    result = future.result()
                except Exception as e:
                    logger.warning("[%s/%s] slug %s failed: %s", platform, tier, slug, e)
                    totals["slugs_failed"] += 1
                    p_stats["slugs_failed"] += 1
                    try:
                        crawl_state.mark_failure(platform, slug, state_map.get(slug))
                    except Exception:
                        pass
                    continue
                # Aggregate + per-platform in one pass
                totals["slugs_crawled"] += 1
                p_stats["slugs_crawled"] += 1
                totals["snapshot_count"] += result["snapshot_count"]
                p_stats["snapshot_count"] += result["snapshot_count"]
                for tk in ("tier1", "tier2", "tier3"):
                    totals[tk] += result.get(tk, 0)
                    p_stats[tk] += result.get(tk, 0)
                if result["board_hash_matched"]:
                    totals["board_hash_matched"] += 1
                    p_stats["board_hash_matched"] += 1
                else:
                    totals["written"] += result["written"]
                    p_stats["written"] += result["written"]
                    totals["expired"] += result["expired"]
                    p_stats["expired"] += result["expired"]

    # Compute board-hash skip rate — the % of crawls that avoided all Firestore
    # writes because nothing changed. 85-95% is the target at steady state
    # (co-founder's plan projection). Divide by crawls that actually happened
    # (skip failed slugs from the denominator to avoid bias).
    denom = totals["board_hash_matched"] + totals["written"] + totals["expired"]
    totals["board_hash_skip_rate_pct"] = round(
        100 * totals["board_hash_matched"] / max(totals["slugs_crawled"], 1), 1
    )
    for p, p_stats in totals["per_platform"].items():
        p_stats["board_hash_skip_rate_pct"] = round(
            100 * p_stats["board_hash_matched"] / max(p_stats["slugs_crawled"], 1), 1
        )

    logger.info("crawl-ats %s complete: %s", tier, totals)
    return totals


if __name__ == "__main__":
    app = _bootstrap_app()

    with app.app_context():
        if "--cleanup" in sys.argv:
            mode, runner = "cleanup", run_cleanup
        elif "--fix-salaries" in sys.argv:
            mode, runner = "fix-salaries", run_fix_salaries
        elif "--enrich-only" in sys.argv:
            limit = _parse_limit(200)
            mode, runner = "enrich-only", (lambda: run_enrich(limit=limit, backfill=False))
        elif "--backfill-enrich" in sys.argv:
            limit = _parse_limit(500)
            since_days = _parse_since_days(default=14)  # default: last 14 days only
            mode, runner = "backfill-enrich", (
                lambda: run_enrich(limit=limit, backfill=True, since_days=since_days)
            )
        elif "--title-enrich-only" in sys.argv:
            limit = _parse_limit(200)
            mode, runner = "title-enrich-only", (
                lambda: run_title_enrich(limit=limit, backfill=False)
            )
        elif "--backfill-title-enrich" in sys.argv:
            limit = _parse_limit(500)
            since_days = _parse_since_days(default=14)
            mode, runner = "backfill-title-enrich", (
                lambda: run_title_enrich(limit=limit, backfill=True, since_days=since_days)
            )
        elif "--extract-deadlines" in sys.argv:
            limit = _parse_limit(50)
            mode, runner = "extract-deadlines", (lambda: run_extract_deadlines(limit=limit))
        elif "--fantastic-only" in sys.argv:
            mode, runner = "fantastic-only", run_fantastic_only
        elif "--fantastic-modified" in sys.argv:
            mode, runner = "fantastic-modified", run_fantastic_modified
        elif "--sweep-expired" in sys.argv:
            mode, runner = "sweep-expired", run_sweep_expired
        elif "--skip-fantastic" in sys.argv:
            mode, runner = "skip-fantastic", (lambda: run_pipeline(skip_fantastic=True))
        elif "--crawl-ats" in sys.argv:
            tier = _parse_tier(default="hot")
            shard = _parse_shard()
            mode, runner = f"crawl-ats-{tier}", (lambda: run_crawl_ats(tier=tier, shard=shard))
        elif "--health-snapshot" in sys.argv:
            mode, runner = "health-snapshot", run_health_snapshot
        elif "--include-fantastic-7d" in sys.argv:
            # Explicit opt-in for the paid 7d FJ sweep alongside the other sources.
            # Still gated by FJ_FULL_BACKFILL_ENABLED inside run_fantastic_only-equivalent
            # path; here we just flip the skip flag in run_pipeline.
            mode, runner = "full-with-fantastic", (lambda: run_pipeline(skip_fantastic=False))
        else:
            # Default now SKIPS the paid 7d Fantastic.jobs sweep. The daily
            # `_fantastic_modified_loop` + `_fantastic_expired_loop` daemons
            # (wsgi.py) already keep the index fresh via FREE endpoints
            # (/modified-ats-24h, /active-ats-expired). Opt back in with
            # --include-fantastic-7d when running an intentional backfill.
            mode, runner = "full", (lambda: run_pipeline(skip_fantastic=True))

        started = datetime.now(timezone.utc)
        try:
            result = runner()
            if not isinstance(result, dict):
                result = {"total": int(result) if isinstance(result, (int, float)) else 0}
            _write_run_log(mode, started, result, error=None)
        except Exception as e:
            logger.exception("Pipeline run failed: %s", e)
            _write_run_log(mode, started, None, error=f"{type(e).__name__}: {e}")
            sys.exit(1)
