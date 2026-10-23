"""
Jobs API routes, feed, search, feedback, and filters.
"""
from flask import Blueprint, jsonify, request
from backend.app.extensions import require_firebase_auth, get_db
from backend.app.utils.job_ranking import (
    prefilter_candidates,
    rank_with_gpt,
    apply_feedback_adjustments,
    cap_per_company,
    _is_excluded as _is_excluded_job,
    _is_non_us as _is_international_job,
)
from backend.pipeline.normalizer import build_search_terms, canonicalize_company
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from google.cloud.firestore_v1.base_query import FieldFilter
import base64
import json
import logging
import threading

logger = logging.getLogger(__name__)

jobs_bp = Blueprint("jobs", __name__)

_filters_cache = {"data": None, "cached_at": None}
FILTERS_CACHE_TTL = 3600

_ranking_lock = threading.Lock()
_ranking_in_progress = set()  # UIDs currently being re-ranked
_ranking_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="job-rank")

_pipeline_summary_cache = {"data": None, "cached_at": 0.0}
PIPELINE_SUMMARY_TTL = 60  # seconds

# Refresh-button rotation. The user's `jobFeedCache.job_ids` stores up to 1000
# ranked IDs but each response only hydrates `MAX_DISPLAY_TOP_JOBS` of them.
# A refresh advances `jobFeedOffset` by STRIDE and hydrates a different slice,
# so successive presses surface genuinely different jobs while staying inside
# the same personalized ranked list. Wraps to 0 once the offset would leave
# no docs to surface.
_FEED_OFFSET_STRIDE = 100


def _advance_feed_offset(current: int, cache_len: int, stride: int = _FEED_OFFSET_STRIDE) -> tuple[int, bool]:
    """Return (new_offset, wrapped) for the next refresh slice.

    Wraps to 0 when the next slice would land at or past the end of the
    cached list (so we never return an empty hydration). Cache lengths shorter
    than the stride cause every refresh to wrap, which is the right behavior:
    the user does not have enough cached jobs to rotate through.
    """
    if cache_len <= 0:
        return 0, False
    nxt = max(0, current) + max(1, stride)
    if nxt >= cache_len:
        return 0, True
    return nxt, False


def _format_freshness(minutes: int | None) -> str:
    if minutes is None:
        return "Unknown"
    if minutes < 2:
        return "Just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _get_pipeline_summary() -> dict:
    """Return {last_pipeline_run, freshness_label, stale} for the feed response.

    Cached in-process for PIPELINE_SUMMARY_TTL seconds. Safe no-op shape on any error.
    """
    import time
    now = time.time()
    cached = _pipeline_summary_cache.get("data")
    if cached is not None and (now - _pipeline_summary_cache.get("cached_at", 0)) < PIPELINE_SUMMARY_TTL:
        return cached

    summary = {"last_pipeline_run": None, "freshness_label": "Unknown", "stale": True}
    try:
        db = get_db()
        if not db:
            return summary
        query = (
            db.collection("pipeline_runs")
            .order_by("started_at", direction="DESCENDING")
            .limit(5)
        )
        for doc in query.stream():
            data = doc.to_dict() or {}
            mode = data.get("mode")
            ok = data.get("ok", data.get("error") is None)
            if not ok or mode not in ("full", "fantastic-only", "skip-fantastic"):
                continue
            started = data.get("started_at")
            if started is None:
                continue
            try:
                delta = datetime.now(timezone.utc) - started
                minutes = int(delta.total_seconds() // 60)
            except Exception:
                continue
            summary = {
                "last_pipeline_run": started.isoformat() if hasattr(started, "isoformat") else None,
                "freshness_label": _format_freshness(minutes),
                "stale": minutes > 360,  # >6h
            }
            break
    except Exception:
        logger.warning("pipeline summary lookup failed", exc_info=True)

    _pipeline_summary_cache["data"] = summary
    _pipeline_summary_cache["cached_at"] = now
    return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


import re as _re

_TITLE_NOISE_RE = _re.compile(
    r"\s*[\(\[\-–—|/,]\s*(full[\s-]?time|part[\s-]?time|contract|temporary|temp|seasonal|"
    r"remote|hybrid|on[\s-]?site|in[\s-]?person|i+|ii+|iii+|iv|v|jr|sr|junior|senior|"
    r"associate|lead|level\s*\d+|l\d+|\d+|w\d+\b|location|posted|new)"
    r"[^a-z0-9]*.*$",
    _re.IGNORECASE,
)


def _normalize_title(title: str | None) -> str:
    """Collapse title variants like 'Teller (Full Time)' / 'Teller (Part Time)' to 'teller'."""
    if not title:
        return ""
    t = title.lower().strip()
    t = _TITLE_NOISE_RE.sub("", t)
    t = _re.sub(r"\s+", " ", t).strip(" -–—|/,([")
    return t


def _dedup_by_title_company(jobs: list[dict]) -> list[dict]:
    """Deduplicate jobs by (normalized_title, company), keeping the higher-scored one.

    Normalization collapses 'Teller (Full Time)' and 'Teller (Part Time)' into
    one bucket so they no longer escape `cap_per_company`.
    """
    seen = {}
    for job in jobs:
        key = (_normalize_title(job.get("title")), (job.get("company") or "").lower().strip())
        existing = seen.get(key)
        if existing is None or (job.get("match_score") or 0) > (existing.get("match_score") or 0):
            seen[key] = job
    return sorted(seen.values(), key=lambda j: j.get("match_score") or 0, reverse=True)


def _log_suppressed_by_eligibility(jobs: list[dict], top_n: int = 10) -> None:
    """Measure what the old eligibility re-sort used to cost us.

    Counts jobs in the top `top_n` of the quality ranking that are NOT
    auto-apply-eligible — exactly the ones the previous sort pushed below
    every eligible job. Lets us quantify the suppression after the fact
    instead of guessing at it.
    """
    if not jobs:
        return
    head = jobs[:top_n]
    demoted = sum(1 for j in head if not j.get("auto_apply_eligible"))
    if demoted:
        logger.info(
            "feed: %d/%d top-ranked jobs are not auto-apply-eligible "
            "(previously demoted below every eligible job); platforms=%s",
            demoted, len(head),
            sorted({(j.get("ats_platform") or j.get("source") or "?")
                    for j in head if not j.get("auto_apply_eligible")}),
        )


def _serialize_jobs(jobs: list[dict]) -> list[dict]:
    """Convert Firestore timestamps to ISO strings, strip large/internal fields.

    The caller's ranking (match score / posted_at) is preserved exactly.
    This used to re-sort the list to push auto-apply-eligible jobs
    (Greenhouse / Lever / Ashby) ahead of ineligible ones, which buried a
    well-matched Workday summer-analyst posting at a target employer below
    a weak Greenhouse match purely because of which ATS it sat on. Jobs are
    ranked on quality; `auto_apply_eligible` still rides along so the SPA
    can render the Auto-apply button, but it no longer decides order.
    """
    from app.services.auto_apply.ats_detector import detect_platform, is_eligible
    cleaned = []
    for job in jobs:
        doc = dict(job)
        for ts_field in ("posted_at", "fetched_at", "expires_at"):
            val = doc.get(ts_field)
            if val is not None and hasattr(val, "isoformat"):
                doc[ts_field] = val.isoformat()
        # Phase 1 structured payload: serialize its enriched_at timestamp
        structured = doc.get("structured")
        if isinstance(structured, dict):
            sd = dict(structured)
            ea = sd.get("enriched_at")
            if ea is not None and hasattr(ea, "isoformat"):
                sd["enriched_at"] = ea.isoformat()
            doc["structured"] = sd
        doc.pop("description_raw", None)
        # 12KB embedding vector — internal only, never send to the SPA
        doc.pop("titleEmbedding", None)
        # Auto-apply eligibility — derived from FantasticJobs ats_* tagging.
        # The SPA reads these to decide whether to render the "Auto-apply" button.
        doc["ats_platform"] = detect_platform(job)
        doc["auto_apply_eligible"] = is_eligible(job)
        cleaned.append(doc)
    _log_suppressed_by_eligibility(cleaned)
    return cleaned


def _derive_match_signals(
    job: dict,
    profile: dict | None,
    saved_companies: set[str],
    user_signals=None,
) -> list[str]:
    """Build the multi-line 'Why this ranked' signals shown in the editorial UI.

    The ranker only stores a single `match_reason` string; this expands that
    into the bullet-list shape the design expects without re-running GPT.

    Phase 2: when `user_signals` (a UserSignals dataclass) is provided, we
    surface dream/target/alumni context as additional bullets. The ranking
    boost is applied elsewhere (apply_feedback_adjustments); this function
    only affects what the user reads, not the sort order.
    """
    profile = profile or {}
    signals: list[str] = []

    reason = (job.get("match_reason") or "").strip()
    if reason:
        signals.append(reason)

    # Phase 2: dream/target/alumni bullets — only the strings that aren't
    # already covered by the saved-companies signal below.
    if user_signals is not None:
        for bullet in user_signals.editorial_badges(job):
            signals.append(bullet)

    company = (job.get("company") or "").strip()
    if company and company.lower() in saved_companies:
        signals.append(f"{company} is on your saved-companies list")

    posted_at = job.get("posted_at")
    if posted_at is not None:
        try:
            ts = posted_at if isinstance(posted_at, datetime) else None
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts:
                delta_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
                if delta_hours <= 24:
                    signals.append("Posted within the last 24 hours")
                elif delta_hours >= 24 * 10:
                    signals.append(f"Posted {int(delta_hours / 24)} days ago — may be stale")
        except Exception:
            pass

    loc_raw = job.get("location")
    if isinstance(loc_raw, dict):
        loc = " ".join(str(v) for v in loc_raw.values() if v).strip()
    elif isinstance(loc_raw, str):
        loc = loc_raw.strip()
    else:
        loc = ""
    target_locs = profile.get("targetLocations") or profile.get("preferredLocations") or []
    if loc and target_locs:
        if any(t and t.lower() in loc.lower() for t in target_locs if isinstance(t, str)):
            signals.append(f"{loc} matches your geo preferences")

    school = (profile.get("university") or profile.get("school") or "").strip()
    if school and (job.get("alumni_count") or 0) > 0:
        signals.append(f"{job['alumni_count']} {school} alum{'i' if job['alumni_count'] != 1 else 'us'} on team")

    return signals[:4]


# ---------------------------------------------------------------------------
# GET /api/jobs/feed
# ---------------------------------------------------------------------------

@jobs_bp.route("/api/jobs/feed", methods=["GET"])
@require_firebase_auth
def get_feed():
    """Timing wrapper around _get_feed_impl.

    Emits a single `[FeedPerf] total=...` line per request so cold vs warm
    latency is visible in the server logs without external profiling. The
    per-phase laps inside _get_feed_impl (prefs_signals / top_query_stream /
    new_matches_stream / serialize) attribute the wall-clock to each stage.
    Set JOBS_FEED_PERF=0 to silence.
    """
    import time as _time
    import os as _os
    _perf_on = _os.getenv("JOBS_FEED_PERF", "1") != "0"
    _t0 = _time.perf_counter()
    try:
        return _get_feed_impl(_t0 if _perf_on else None)
    finally:
        if _perf_on:
            try:
                logger.info(
                    "[FeedPerf] total=%.0fms uid=%s",
                    (_time.perf_counter() - _t0) * 1000.0,
                    request.firebase_user.get("uid"),
                )
            except Exception:
                pass


def _get_feed_impl(_perf_t0=None):
    import time as _time
    uid = request.firebase_user["uid"]
    db = get_db()
    now = datetime.now(timezone.utc)

    # Per-phase timing. `_lap(label)` logs ms elapsed since the previous lap.
    # No-op when perf logging is disabled so the hot path stays clean.
    _perf = {"t": _time.perf_counter()} if _perf_t0 is not None else None

    def _lap(label):
        if _perf is None:
            return
        _now = _time.perf_counter()
        logger.info("[FeedPerf] %s=%.0fms uid=%s", label, (_now - _perf["t"]) * 1000.0, uid)
        _perf["t"] = _now
    refresh = request.args.get("refresh", "").lower() == "true"
    # Phase 2 escape hatch: ?ungated=true skips hard intent gates even when
    # the feature flag is on for this user. Useful for "Show all" toggle.
    ungated = request.args.get("ungated", "").lower() == "true"

    # Load user profile
    user_ref = db.collection("users").document(uid)
    user_doc = user_ref.get()
    if not user_doc.exists:
        return jsonify({"error": "User not found"}), 404

    # ungated changes the feed shape (the gates apply differently), so we
    # must blow the cache away. refresh used to do the same, which is why
    # the button felt like a no-op: it forced a fresh rank over identical
    # inputs and returned the same top 300 in the same order. Now refresh
    # KEEPS the cache and rotates which slice of the 1000 cached IDs to
    # hydrate (see _advance_feed_offset and the cache-hit path below).
    if ungated:
        user_ref.update({"jobFeedCache": None})

    profile = user_doc.to_dict()

    # ----- Phase 2: hard intent gates (feature-flag gated) ---------------
    # Built once per request so all 4 return paths can call _apply_gates().
    from backend.app.services import feature_flags
    from backend.app.utils.intent_gates import (
        build_user_intent, apply_intent_gates, intent_hash, expand_intent_with_pdl,
    )

    _gating_on = (
        not ungated
        and feature_flags.is_enabled("hardIntentGating", uid=uid, default=False)
    )
    _user_intent = build_user_intent(profile) if _gating_on else None
    # Independent flag — flippable without touching the gates themselves.
    # When on, augments career_interests with PDL synonyms so jobs titled
    # "Associate Product Manager" pass the gate for users who picked
    # "Product Manager".
    if _user_intent and feature_flags.is_enabled(
        "pdlInterestExpansion", uid=uid, default=False
    ):
        _user_intent = expand_intent_with_pdl(_user_intent)
    _intent_hash_str = intent_hash(_user_intent) if _user_intent else None

    def _apply_gates(new_matches, top_jobs):
        """Return (new_matches, top_jobs, gated_dict) honoring flag + ungated."""
        if not _gating_on or _user_intent is None:
            return new_matches, top_jobs, {
                "by_level": 0, "by_location": 0, "by_interest": 0,
                "applied": False, "ungated": ungated,
            }
        gated_top, counts = apply_intent_gates(top_jobs, _user_intent)
        gated_new, counts_new = apply_intent_gates(new_matches, _user_intent)
        for k in ("by_level", "by_location", "by_interest"):
            counts[k] = counts.get(k, 0) + counts_new.get(k, 0)
        counts["applied"] = True
        counts["ungated"] = False
        counts["intent_hash"] = _intent_hash_str
        return gated_new, gated_top, counts

    # Load negative-signal job_ids so dismissed rows stop reappearing.
    dismissed_ids: set[str] = set()
    try:
        prefs_snap = (
            user_ref.collection("jobPreferences")
            .where("signal", "==", "negative")
            .stream()
        )
        for d in prefs_snap:
            pd = d.to_dict() or {}
            jid = pd.get("job_id") or d.id
            if jid:
                dismissed_ids.add(jid)
    except Exception as e:
        logger.debug(f"could not load dismissed jobs for {uid}: {e}")

    saved_companies: set[str] = set()
    try:
        saved_snap = user_ref.collection("savedJobs").stream()
        for d in saved_snap:
            sd = d.to_dict() or {}
            co = (sd.get("company") or "").strip().lower()
            if co:
                saved_companies.add(co)
    except Exception:
        pass

    # Phase 2: load dream/target/alumni signals once per request. Cached for
    # 5 min so repeat pulls from get_feed within a session are free. Failing
    # to load is non-fatal — feed still renders, just without the new badges.
    try:
        from app.services.job_ranker_signals import load_user_signals
        user_signals = load_user_signals(uid)
    except Exception:
        user_signals = None
        logger.exception("[JobsFeed] load_user_signals failed for uid=%s", uid)

    # Covers user_doc.get + dismissed/saved streams + load_user_signals.
    _lap("prefs_signals")

    def _enrich(jobs: list[dict]) -> list[dict]:
        """Filter dismissed jobs and attach the editorial match_signals array."""
        out: list[dict] = []
        for j in jobs:
            jid = j.get("job_id")
            if jid and jid in dismissed_ids:
                continue
            j["match_signals"] = _derive_match_signals(j, profile, saved_companies, user_signals)
            # Phase 2: expose stable badge codes so the frontend can render
            # a dedicated ⭐/🎓 chip without parsing prose strings.
            if user_signals is not None:
                _, codes = user_signals.boost(j)
                if codes:
                    j["match_badges"] = codes
                # Phase 5: when the user has a saved contact at this company,
                # surface the best one so the frontend can render a
                # "Reach out to Sarah" CTA next to "Apply →".
                from app.models.users import normalize_company as _nc
                slug = _nc(j.get("company") or "")
                if slug:
                    contact = user_signals.top_contact_per_company.get(slug)
                    if contact:
                        j["referral_contact"] = contact
            out.append(j)

        # Phase 3: fill any null/generic match_reason on the top 10 with a
        # data-derived sentence so the SPA never has to show "Matched to your
        # profile". The background rerank still runs and overwrites these
        # with GPT-quality reasons once cached, but this guarantees the
        # first-load UX is specific.
        try:
            from app.services.match_reasoning import fill_match_reasons
            fill_match_reasons(out, profile, top_n=10)
        except Exception:
            logger.exception("[JobsFeed] match_reasoning fill failed for uid=%s", uid)

        return out

    # Check cache. Refresh no longer invalidates the cache; instead it walks
    # an offset over the same cached ranked list (see _advance_feed_offset).
    cache = profile.get("jobFeedCache") or {}
    cache_ranked_at = cache.get("ranked_at")
    cache_valid = False
    cache_stale_ok = False
    if cache_ranked_at:
        if hasattr(cache_ranked_at, "timestamp"):
            cache_age = (now - cache_ranked_at.replace(tzinfo=timezone.utc)).total_seconds()
        elif hasattr(cache_ranked_at, "isoformat"):
            cache_age = (now - cache_ranked_at).total_seconds()
        else:
            cache_age = float("inf")
        cache_valid = cache_age < 1800
        cache_stale_ok = not cache_valid and cache_age < 7200  # 2 hours

    # Check new_matches cache (short TTL — 5 min). refresh still bypasses
    # this so the user gets the latest 24h window on every press, even
    # though the ranked slice is now rotation-based.
    nm_cache = cache.get("new_matches_cache") or {}
    nm_cached_at = nm_cache.get("cached_at")
    nm_valid = False
    if nm_cached_at and not refresh:
        if hasattr(nm_cached_at, "timestamp"):
            nm_age = (now - nm_cached_at.replace(tzinfo=timezone.utc)).total_seconds()
        elif hasattr(nm_cached_at, "isoformat"):
            nm_age = (now - nm_cached_at).total_seconds()
        else:
            nm_age = float("inf")
        # Align with the main feed cache TTL (1800s). Previously 300s, which
        # forced warm loads to re-stream the 24h `new_matches` window every
        # 5 min even while the main ranked cache (30 min TTL) was still served
        # from cache. The 24h window barely changes inside 30 min and the
        # background ingestion daemons keep it fresh, so a 30-min snapshot is
        # the same product surface with none of the per-load re-stream cost.
        nm_valid = nm_age < 1800  # 30 minutes (was 300s — matched to main cache)

    twenty_four_hours_ago = now - timedelta(hours=24)

    def _fetch_new_matches(cached_scores=None, cached_reasons=None):
        """Fetch new_matches from Firestore, or return from cache if fresh."""
        if nm_valid:
            return nm_cache.get("jobs", []), True
        # Pull a wider window so dedup + cap_per_company have headroom for the
        # display cap. Without this slack a few high-volume employers would
        # saturate the window before the cap_per_company stage.
        # 400 is ~2.6x the 150-job display cap after dedup + cap_per_company(8),
        # which is ample headroom for high-volume employers in the 24h window.
        # Was 800 — pure over-fetch: the extra 400 docs were streamed,
        # deserialized, then discarded by the [:150] slice. Recency order is
        # preserved, so the surfaced set is unchanged in practice.
        # Phase 2 tier filter (2026-07-15 pre-launch): the loose-gate cold
        # matrix wrote ~85K tier-3 jobs, which pushed the 24h feed window
        # from 288 → 7,133 matches and caused cold-cache feed loads to stall
        # 10+ seconds. Filter narrows the query to tier-1 (early-career +
        # target function) and tier-2 (early-career off-target), dropping
        # tier-3 from the default feed. Composite index
        # (relevance_tier ASC, posted_at DESC) was deployed pre-flip.
        #
        # Legacy jobs without relevance_tier are excluded by this IN filter
        # — acceptable trade because tier-stamped jobs already dominate the
        # 24h window post-Phase-0. Backfill script exists at
        # backend/scripts/backfill_relevance_tier.py if we want to
        # retroactively stamp legacy docs.
        new_query = (
            db.collection("jobs")
            .where("posted_at", ">=", twenty_four_hours_ago)
            .where("relevance_tier", "in", [1, 2])
            .order_by("posted_at", direction="DESCENDING")
            .limit(400)
        )
        raw = []
        for d in new_query.stream():
            j = d.to_dict()
            if _is_international_job(j) or _is_excluded_job(j):
                continue
            jid = j.get("job_id", d.id)
            if cached_scores:
                j["match_score"] = cached_scores.get(jid)
                j["match_reason"] = (cached_reasons or {}).get(jid)
                j["ranked"] = j["match_score"] is not None
            else:
                j["match_score"] = None
                j["match_reason"] = None
                j["ranked"] = False
            raw.append(j)

        _lap("new_matches_stream")

        # Collapse title variants ("Teller (Full Time)" + "Teller (Part Time)" → one),
        # then cap per company so a single batch poster can't fill the feed.
        deduped = _dedup_by_title_company(raw)
        # US-only feed (2026-07-15 pre-launch): Sid flagged Thailand /
        # Kuala Lumpur / Bulgaria jobs surfacing. Drop us_priority=3 outright
        # (confirmed non-US). Legacy jobs without us_priority stamped default
        # to keeping — they came through _is_international_job's keyword
        # filter above, so anything that reached this point that's ALSO
        # missing us_priority is very likely US-adjacent.
        # Users in different US cities keep seeing all US jobs (LA user sees
        # NYC jobs) — the filter is country-level only, not city-level.
        deduped = [j for j in deduped if j.get("us_priority") != 3]
        # Sort remaining US-first (us_priority=1) then remote (=2), then
        # legacy-unstamped (defaults to 3 in sort but 3s just filtered above,
        # so this effectively puts stamped-US before legacy-unknown). Within
        # each band, most recent posted_at wins.
        def _feed_sort_key(j: dict):
            pa = j.get("posted_at")
            if hasattr(pa, "timestamp"):
                ts = pa.timestamp()
            elif isinstance(pa, (int, float)):
                ts = float(pa)
            else:
                ts = 0.0
            return (j.get("us_priority") or 3, -ts)
        deduped.sort(key=_feed_sort_key)
        new_matches = cap_per_company(deduped, max_per_company=8)[:150]
        # Persist new_matches to cache (fire-and-forget)
        try:
            user_ref.update({
                "jobFeedCache.new_matches_cache": {
                    "jobs": _serialize_jobs(new_matches),
                    "cached_at": now,
                }
            })
        except Exception:
            pass
        return new_matches, False

    # Display slice: cache stores up to 1000 ranked job_ids (rich inventory for
    # rotation), but each response only hydrates MAX_DISPLAY_TOP_JOBS of them.
    # The slice now starts at `offset` so successive refreshes surface
    # different jobs from the same ranked pool.
    MAX_DISPLAY_TOP_JOBS = 300

    def _load_top_jobs_from_cache(cached_ids, cached_scores, cached_reasons, offset: int = 0):
        """Hydrate top_jobs from cached job IDs starting at `offset`.

        Defensively wraps when offset is past the end of the cached list,
        so a stale persisted offset (e.g. left over from a longer cache that
        has since been rebuilt smaller) still surfaces real jobs instead of
        returning an empty list. The caller's _advance_feed_offset normally
        prevents this, so the wrap here is belt-and-suspenders.
        """
        top_jobs = []
        if cached_ids:
            start = max(0, offset)
            if start >= len(cached_ids):
                start = 0
            window = cached_ids[start : start + MAX_DISPLAY_TOP_JOBS]
            for i in range(0, len(window), 100):
                chunk = window[i:i + 100]
                refs = [db.collection("jobs").document(jid) for jid in chunk]
                docs = db.get_all(refs)
                for d in docs:
                    if d.exists:
                        j = d.to_dict()
                        jid = j.get("job_id", d.id)
                        j["match_score"] = cached_scores.get(jid)
                        j["match_reason"] = cached_reasons.get(jid)
                        j["ranked"] = j["match_score"] is not None
                        top_jobs.append(j)
            top_jobs.sort(key=lambda j: j.get("match_score") or 0, reverse=True)
        return top_jobs

    # Resolve the hydration offset before the cache paths execute. Reads the
    # stored offset, and if this is a refresh and the cache exists, advances
    # by STRIDE (with wrap-to-zero on overflow) and persists the new value.
    # Non-refresh requests keep the current offset, so a deep-linked reload
    # of the same page shows the same slice the user last saw.
    feed_offset = int(profile.get("jobFeedOffset") or 0)
    feed_wrapped = False
    cached_ids_len_for_offset = len(cache.get("job_ids") or [])
    if refresh and cached_ids_len_for_offset > 0:
        feed_offset, feed_wrapped = _advance_feed_offset(
            feed_offset, cached_ids_len_for_offset,
        )
        try:
            user_ref.update({"jobFeedOffset": feed_offset})
        except Exception:
            logger.exception("[JobsFeed] failed to persist jobFeedOffset for uid=%s", uid)

    if cache_valid:
        cached_ids = cache.get("job_ids", [])
        cached_scores = cache.get("scores", {})
        cached_reasons = cache.get("reasons", {})
        top_jobs = _enrich(_load_top_jobs_from_cache(
            cached_ids, cached_scores, cached_reasons, offset=feed_offset,
        ))
        new_matches_raw, nm_from_cache = _fetch_new_matches(cached_scores, cached_reasons)
        new_matches = _enrich(new_matches_raw)
        new_matches, top_jobs, gated_info = _apply_gates(new_matches, top_jobs)

        return jsonify({
            "new_matches": _serialize_jobs(new_matches) if not nm_from_cache else new_matches,
            "top_jobs": _serialize_jobs(top_jobs),
            "new_matches_count": len(new_matches),
            "top_jobs_count": len(top_jobs),
            "ranked": True,
            "no_resume": False,
            "cached": True,
            "feed_offset": feed_offset,
            "feed_wrapped": feed_wrapped,
            "summary": _get_pipeline_summary(),
            "gated": gated_info,
        })

    if not cache_valid and cache_stale_ok:
        cached_ids = cache.get("job_ids", [])
        cached_scores = cache.get("scores", {})
        cached_reasons = cache.get("reasons", {})
        top_jobs = _enrich(_load_top_jobs_from_cache(
            cached_ids, cached_scores, cached_reasons, offset=feed_offset,
        ))
        new_matches_raw, nm_from_cache = _fetch_new_matches(cached_scores, cached_reasons)
        new_matches = _enrich(new_matches_raw)

        # Trigger background re-rank if not already in progress
        if uid not in _ranking_in_progress:
            _ranking_in_progress.add(uid)
            _ranking_pool.submit(_background_rerank, uid)
            logger.info(f"Triggered background re-rank for {uid}")

        new_matches, top_jobs, gated_info = _apply_gates(new_matches, top_jobs)
        return jsonify({
            "new_matches": _serialize_jobs(new_matches) if not nm_from_cache else new_matches,
            "top_jobs": _serialize_jobs(top_jobs),
            "new_matches_count": len(new_matches),
            "top_jobs_count": len(top_jobs),
            "ranked": True,
            "no_resume": False,
            "cached": True,
            "stale": True,
            "feed_offset": feed_offset,
            "feed_wrapped": feed_wrapped,
            "summary": _get_pipeline_summary(),
            "gated": gated_info,
        })

    # No resume — return unranked jobs by recency
    has_resume = bool(profile.get("resumeParsed") or profile.get("resumeText"))
    if not has_resume:
        new_matches_raw, nm_from_cache = _fetch_new_matches()
        new_matches = _enrich(new_matches_raw)
        # 500 is a generous buffer over the 300-job cap_per_company(10)[:300]
        # slice; was 1000 (over-fetch — extra docs streamed then discarded).
        top_query = (
            db.collection("jobs")
            .order_by("posted_at", direction="DESCENDING")
            .limit(500)
        )
        top_jobs = [d.to_dict() for d in top_query.stream()]
        _lap("top_query_stream")
        top_jobs = [j for j in top_jobs if not _is_international_job(j) and not _is_excluded_job(j)]
        top_jobs = cap_per_company(top_jobs, max_per_company=10)[:300]
        for j in top_jobs:
            j["match_score"] = None
            j["match_reason"] = None
            j["ranked"] = False
        top_jobs = _enrich(top_jobs)
        new_matches, top_jobs, gated_info = _apply_gates(new_matches, top_jobs)

        _serialized_new = _serialize_jobs(new_matches) if not nm_from_cache else new_matches
        _serialized_top = _serialize_jobs(top_jobs)
        _lap("serialize")
        return jsonify({
            "new_matches": _serialized_new,
            "top_jobs": _serialized_top,
            "new_matches_count": len(new_matches),
            "top_jobs_count": len(top_jobs),
            "ranked": False,
            "no_resume": True,
            "cached": False,
            "summary": _get_pipeline_summary(),
            "gated": gated_info,
        })

    # Has resume but no cache — return unranked jobs immediately, rank in background.
    # 500 is a generous buffer over the 300-job cap_per_company(10)[:300] slice;
    # was 1000 (streamed + deserialized then discarded). This is a transient
    # unranked placeholder — _background_rerank runs its own .limit(5000) scan
    # and the next load serves the real ranked cache, so the candidate pool
    # for ranking is untouched.
    top_query = (
        db.collection("jobs")
        .order_by("posted_at", direction="DESCENDING")
        .limit(500)
    )
    top_jobs = [d.to_dict() for d in top_query.stream()]
    _lap("top_query_stream")
    top_jobs = [j for j in top_jobs if not _is_international_job(j) and not _is_excluded_job(j)]
    top_jobs = cap_per_company(top_jobs, max_per_company=10)[:300]
    for j in top_jobs:
        j["match_score"] = None
        j["match_reason"] = None
        j["ranked"] = False
    top_jobs = _enrich(top_jobs)

    new_matches_raw, nm_from_cache = _fetch_new_matches()
    new_matches = _enrich(new_matches_raw)

    # Trigger background ranking so next load is fast
    if uid not in _ranking_in_progress:
        _ranking_in_progress.add(uid)
        t = threading.Thread(target=_background_rerank, args=(uid,), daemon=True)
        t.start()
        logger.info(f"Triggered background ranking for {uid} (first visit)")

    new_matches, top_jobs, gated_info = _apply_gates(new_matches, top_jobs)
    _serialized_new = _serialize_jobs(new_matches) if not nm_from_cache else new_matches
    _serialized_top = _serialize_jobs(top_jobs)
    _lap("serialize")
    return jsonify({
        "new_matches": _serialized_new,
        "top_jobs": _serialized_top,
        "new_matches_count": len(new_matches),
        "top_jobs_count": len(top_jobs),
        "ranked": False,
        "no_resume": False,
        "cached": False,
        "ranking_in_progress": True,
        "summary": _get_pipeline_summary(),
        "gated": gated_info,
    })


def _is_recent(posted_at, cutoff: datetime) -> bool:
    """Check if a posted_at value is after the cutoff."""
    if posted_at is None:
        return False
    if hasattr(posted_at, "timestamp"):
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    return posted_at >= cutoff


def _get_posted_at_ts(job: dict) -> datetime:
    """Get posted_at as a timezone-aware datetime for sorting."""
    val = job.get("posted_at")
    if val is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if hasattr(val, "timestamp") and val.tzinfo is None:
        return val.replace(tzinfo=timezone.utc)
    return val


def _background_rerank(uid: str):
    """Re-rank jobs in background thread and update cache."""
    try:
        from backend.app.extensions import get_db
        db = get_db()
        now = datetime.now(timezone.utc)

        user_ref = db.collection("users").document(uid)
        user_doc = user_ref.get()
        if not user_doc.exists:
            return
        profile = user_doc.to_dict()

        has_resume = bool(profile.get("resumeParsed") or profile.get("resumeText"))
        if not has_resume:
            return

        all_query = (
            db.collection("jobs")
            .order_by("posted_at", direction="DESCENDING")
            .limit(5000)
        )
        all_jobs = [doc.to_dict() for doc in all_query.stream()]

        # Filter out international and senior/irrelevant jobs
        all_jobs = [j for j in all_jobs if not _is_international_job(j) and not _is_excluded_job(j)]

        prefs_query = user_ref.collection("jobPreferences").limit(100)
        preferences = [doc.to_dict() for doc in prefs_query.stream()]

        # Phase 2: load dream/target/alumni signals so the rerank can boost
        # those companies. Cached for 5 min, so this is cheap on repeat
        # reranks within a session.
        try:
            from app.services.job_ranker_signals import load_user_signals
            user_signals = load_user_signals(uid)
        except Exception:
            user_signals = None
            logger.exception("[Rerank] load_user_signals failed for uid=%s", uid)

        # Try semantic embedding-based prefilter (text-embedding-3-small),
        # gated by feature flag for safe rollout. Falls back to deterministic
        # keyword scoring if embeddings unavailable or flag disabled.
        from backend.app.services import feature_flags
        candidates = []
        if feature_flags.is_enabled("embedding_ranker", uid=uid, default=False):
            from backend.app.utils.embedding_ranker import embedding_rank
            candidates = embedding_rank(all_jobs, profile, uid, top_n=1500)
            if candidates:
                logger.info(
                    "Embedding rank: top score %.1f, bottom %.1f (%d candidates)",
                    candidates[0].get("_embedding_score", 0),
                    candidates[-1].get("_embedding_score", 0),
                    len(candidates),
                )
            else:
                logger.info("Embedding rank returned empty, falling back to deterministic")
        if not candidates:
            candidates = prefilter_candidates(all_jobs, profile, top_n=1500)
        ranked = rank_with_gpt(candidates, profile)
        adjusted = apply_feedback_adjustments(ranked, preferences, user_signals=user_signals)

        # Deduplicate by title + company, cap per company, then take top N
        deduped = _dedup_by_title_company(adjusted)
        top_jobs = cap_per_company(deduped, max_per_company=10)[:1000]
        cache_data = {
            "job_ids": [j["job_id"] for j in top_jobs],
            "scores": {j["job_id"]: j.get("match_score") for j in top_jobs},
            "reasons": {j["job_id"]: j.get("match_reason") for j in top_jobs},
            "ranked_at": datetime.now(timezone.utc),
        }
        # Reset the refresh-rotation offset whenever we write a fresh ranked
        # list. The previous offset was keyed to the OLD job_ids; carrying it
        # forward would mean the user's next visit hydrates positions e.g.
        # 500-799 of the NEW ranking instead of the actual top picks. Zero
        # aligns the user with the top of the freshly-ranked pool.
        user_ref.update({"jobFeedCache": cache_data, "jobFeedOffset": 0})
        logger.info(f"Background re-rank complete for {uid}")
    except Exception as e:
        logger.warning(f"Background re-rank failed for {uid}: {e}")
    finally:
        _ranking_in_progress.discard(uid)


# ---------------------------------------------------------------------------
# GET /api/jobs/search
# ---------------------------------------------------------------------------
#
# Explicit search and filter over the full `jobs` collection. NOT personalized.
# Separate from /api/jobs/feed by design: the feed is a ranked, capped,
# per-user product surface; this route is a catalog query the user drives.
#
# Inputs (all query string):
#   q           free-text. Tokenized with build_search_terms; first token is
#               sent to Firestore as array_contains("search_terms", token),
#               remaining tokens are AND-applied in Python.
#   company     canonical brand string. Run through canonicalize_company so
#               "AWS" hits Amazon docs.
#   location    case-insensitive substring match against `location` field.
#   type        one of FULLTIME, PARTTIME, INTERNSHIP. Exact match.
#   seniority   "intern" / "entry" / "mid" / "senior". Post-filtered against
#               structured.title_meta.seniority when the title enricher has
#               populated it; jobs missing the field pass through (we'd rather
#               surface than hide on missing metadata).
#   posted_after  one of "24h", "7d", "30d". Applies a posted_at >= cutoff
#               range filter at the Firestore level (no post-filter). Unknown
#               or empty values fail open (no filter applied), so a typo
#               cannot silently empty the result set.
#   limit       default 50, max 100.
#   cursor      opaque base64 token from a previous response's next_cursor.
#               Pagination is posted_at-desc with job_id as a tiebreaker.
#
# Firestore composite indexes required (create before shipping):
#   1) jobs: search_terms (Arrays) ASC, posted_at DESC, __name__ ASC
#   2) jobs: company ASC, posted_at DESC, __name__ ASC
#   3) jobs: type ASC, posted_at DESC, __name__ ASC
# Without these, the queries below will fail with FAILED_PRECONDITION and the
# Firestore console will link to the index-create form.

# Conservative ceilings so a bad query can't blow up the worker.
_SEARCH_MAX_LIMIT = 100
_SEARCH_DEFAULT_LIMIT = 50
# How many docs we read from Firestore before applying post-filters. Post-filters
# can drop most of the page, so we read a multiple of the requested page size.
# Bounded so a stopword-heavy q like "the engineer" can't read forever.
_SEARCH_SCAN_MULTIPLIER = 4
_SEARCH_MAX_SCAN = 1000

# Accepted relative tokens for the posted_after query param. Map to a fixed
# timedelta; the resolved cutoff timestamp is now - delta. Kept small and
# explicit so a hostile client cannot pass e.g. "10000d" and force Firestore
# to scan the whole collection. Add new windows here by name.
_POSTED_AFTER_DELTAS: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d":  timedelta(days=7),
    "30d": timedelta(days=30),
}


def _parse_posted_after(raw: str | None) -> datetime | None:
    """Return the cutoff datetime for `posted_after`, or None if the param is
    absent / empty / unrecognized. Unknown values fail open (no filter) so a
    typo never empties the result set silently.
    """
    if not raw:
        return None
    delta = _POSTED_AFTER_DELTAS.get(raw.strip().lower())
    if delta is None:
        logger.warning("search: ignoring unknown posted_after value %r", raw)
        return None
    return datetime.now(timezone.utc) - delta


def _encode_cursor(posted_at, job_id: str) -> str | None:
    if posted_at is None or not job_id:
        return None
    if hasattr(posted_at, "isoformat"):
        ts = posted_at.isoformat()
    else:
        ts = str(posted_at)
    payload = json.dumps({"posted_at": ts, "job_id": job_id})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(token: str | None) -> tuple[datetime | None, str | None]:
    if not token:
        return None, None
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8"))
        ts_str = payload.get("posted_at")
        job_id = payload.get("job_id")
        ts = None
        if ts_str:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        return ts, job_id
    except Exception:
        logger.warning("invalid search cursor, ignoring", exc_info=True)
        return None, None


def _job_seniority(job: dict) -> str | None:
    """Pull the enriched seniority value if present. Falls back to None."""
    structured = job.get("structured")
    if isinstance(structured, dict):
        title_meta = structured.get("title_meta")
        if isinstance(title_meta, dict):
            val = title_meta.get("seniority")
            if isinstance(val, str):
                return val.lower()
    return None


def _matches_post_filters(
    job: dict,
    extra_tokens: list[str],
    location_q: str | None,
    job_type: str | None,
    seniority: str | None,
) -> bool:
    if extra_tokens:
        terms = job.get("search_terms") or []
        if not isinstance(terms, list):
            return False
        term_set = set(terms)
        if not all(t in term_set for t in extra_tokens):
            return False
    if location_q:
        loc = job.get("location") or ""
        if isinstance(loc, dict):
            loc = " ".join(str(v) for v in loc.values() if v)
        elif isinstance(loc, list):
            loc = " ".join(str(v) for v in loc)
        if location_q not in str(loc).lower():
            return False
    if job_type and (job.get("type") or "").upper() != job_type:
        return False
    if seniority:
        actual = _job_seniority(job)
        # Missing metadata passes through so users see more, not fewer, results.
        if actual is not None and actual != seniority:
            return False
    if _is_international_job(job) or _is_excluded_job(job):
        return False
    return True


@jobs_bp.route("/api/jobs/search", methods=["GET"])
@require_firebase_auth
def search_jobs():
    db = get_db()
    if not db:
        return jsonify({"error": "Database unavailable"}), 503

    raw_q = (request.args.get("q") or "").strip()
    company_in = (request.args.get("company") or "").strip()
    location_in = (request.args.get("location") or "").strip().lower()
    type_in = (request.args.get("type") or "").strip().upper()
    seniority_in = (request.args.get("seniority") or "").strip().lower()
    posted_after_in = (request.args.get("posted_after") or "").strip().lower()
    posted_after_ts = _parse_posted_after(posted_after_in)
    try:
        limit = int(request.args.get("limit") or _SEARCH_DEFAULT_LIMIT)
    except ValueError:
        limit = _SEARCH_DEFAULT_LIMIT
    limit = max(1, min(limit, _SEARCH_MAX_LIMIT))

    cursor_ts, cursor_id = _decode_cursor(request.args.get("cursor"))

    # Short-circuit: a degenerate "show me everything" request would force
    # Firestore into a bare `order_by(posted_at).order_by(__name__)` query,
    # which needs its own composite index AND is a wasteful scan of the
    # whole collection. If the user has not narrowed the query in any way,
    # return an explanatory empty payload instead. The frontend should not
    # call this route in that state, but we defend against it anyway.
    # posted_after counts as a narrowing filter even if it is the only one.
    if (not raw_q and not company_in and not location_in
            and not type_in and not seniority_in and posted_after_ts is None):
        return jsonify({
            "results": [],
            "count": 0,
            "scanned": 0,
            "next_cursor": None,
            "query": {
                "q": "", "tokens": [],
                "company": None, "location": None,
                "type": None, "seniority": None,
                "posted_after": None,
                "limit": limit,
            },
            "message": "Add a keyword or filter to search the catalog.",
        })

    # Tokenize the user's query. Pick the rarest-looking token (longest)
    # for the Firestore filter so we minimize the post-filter set.
    tokens = build_search_terms(raw_q, None, None)
    primary_token: str | None = None
    extra_tokens: list[str] = []
    if tokens:
        primary_token = max(tokens, key=len)
        extra_tokens = [t for t in tokens if t != primary_token]

    canonical_company = canonicalize_company(company_in) if company_in else ""

    # Pick the primary Firestore filter. Order of preference:
    #   1. search_terms (most selective when q is provided)
    #   2. company (exact match on the canonical brand)
    #   3. type
    # Anything else becomes a post-filter so we don't multiply the composite
    # index matrix.
    q = db.collection("jobs").order_by(
        "posted_at", direction="DESCENDING"
    ).order_by("__name__", direction="ASCENDING")

    if primary_token:
        q = q.where(filter=FieldFilter("search_terms", "array_contains", primary_token))
    elif canonical_company:
        q = q.where(filter=FieldFilter("company", "==", canonical_company))
    elif type_in:
        q = q.where(filter=FieldFilter("type", "==", type_in))

    # posted_after composes with the primary filter via the existing composite
    # indexes (search_terms / company / type each carry posted_at DESC plus
    # __name__ ASC), and standalone via the posted_at + __name__ index. No
    # new index is required to ship this. Firestore allows one range filter
    # per query; the range and the order_by are both on `posted_at`, which
    # is the allowed shape.
    if posted_after_ts is not None:
        q = q.where(filter=FieldFilter("posted_at", ">=", posted_after_ts))

    if cursor_ts is not None:
        q = q.start_after({"posted_at": cursor_ts, "__name__": cursor_id or ""})

    # Scan budget: read up to limit * multiplier so post-filters have headroom.
    # Capped so a stopword-only query can't read the whole collection.
    scan_budget = min(limit * _SEARCH_SCAN_MULTIPLIER, _SEARCH_MAX_SCAN)
    q = q.limit(scan_budget)

    # If primary_token already filters by search_terms, do not re-apply it
    # as a post-filter; the array_contains query already enforced it.
    post_company = canonical_company if (canonical_company and primary_token) else None
    post_type = type_in if (type_in and (primary_token or canonical_company)) else None

    results: list[dict] = []
    last_doc = None
    scanned = 0
    try:
        for snap in q.stream():
            scanned += 1
            last_doc = snap
            job = snap.to_dict() or {}
            if post_company and job.get("company") != post_company:
                continue
            if post_type and (job.get("type") or "").upper() != post_type:
                continue
            if not _matches_post_filters(
                job, extra_tokens, location_in or None, type_in or None, seniority_in or None,
            ):
                continue
            results.append(job)
            if len(results) >= limit:
                break
    except Exception:
        logger.exception(
            "search_jobs Firestore query failed (likely missing composite index). "
            "primary_token=%s company=%s type=%s",
            primary_token, canonical_company, type_in,
        )
        return jsonify({
            "error": "Search index unavailable. If this persists, the Firestore "
                     "composite index for this query has not been created. See "
                     "the route docstring for the required indexes.",
        }), 503

    # Build the cursor from the LAST DOC WE SCANNED, not the last result, so
    # the next page starts after the doc we stopped on rather than re-scanning
    # filtered-out docs.
    next_cursor = None
    if last_doc is not None and len(results) >= limit:
        last_dict = last_doc.to_dict() or {}
        next_cursor = _encode_cursor(last_dict.get("posted_at"), last_doc.id)

    return jsonify({
        "results": _serialize_jobs(results),
        "count": len(results),
        "scanned": scanned,
        "next_cursor": next_cursor,
        "query": {
            "q": raw_q,
            "tokens": tokens,
            "company": canonical_company or None,
            "location": location_in or None,
            "type": type_in or None,
            "seniority": seniority_in or None,
            "posted_after": posted_after_in or None,
            "limit": limit,
        },
    })


# ---------------------------------------------------------------------------
# GET /api/jobs/<job_id>
# ---------------------------------------------------------------------------

@jobs_bp.route("/api/jobs/<job_id>", methods=["GET"])
@require_firebase_auth
def get_job_detail(job_id: str):
    db = get_db()
    doc = db.collection("jobs").document(job_id).get()
    if not doc.exists:
        return jsonify({"error": "Job not found"}), 404
    job = doc.to_dict()
    # Serialize timestamps
    for ts_field in ("posted_at", "fetched_at", "expires_at"):
        val = job.get(ts_field)
        if val is not None and hasattr(val, "isoformat"):
            job[ts_field] = val.isoformat()
    return jsonify(job)


# ---------------------------------------------------------------------------
# GET /api/jobs/<job_id>/description
# ---------------------------------------------------------------------------

from backend.app.services.job_description import compose_from_structured as _compose_from_structured


def _describe(data: dict) -> str:
    """Best description text for the detail pane, from STORED data only — no
    network, instant. Prefers real prose (ingested, or captured in the
    background by the pipeline enricher), falling back to a bulleted summary
    composed from the enriched `structured` fields, else empty.

    Because the enricher fetches real prose ahead of time, most jobs return
    prose here with zero request-time scraping — so this scales to any number of
    concurrent users.
    """
    raw = (data.get("description_raw") or "").strip()
    if raw:
        return raw
    return _compose_from_structured(data.get("structured") or {})


@jobs_bp.route("/api/jobs/<job_id>/description", methods=["GET"])
@require_firebase_auth
def get_job_description(job_id: str):
    """Detail-pane description — STORED data only, never scrapes in the request.

    Real prose where we have it (the enricher captures it in the background),
    otherwise a bulleted summary from the structured fields, otherwise empty.
    No network call here, so browsing job to job stays instant at any scale.
    """
    db = get_db()
    doc = db.collection("jobs").document(job_id).get()
    if not doc.exists:
        return jsonify({"error": "Job not found"}), 404
    data = doc.to_dict() or {}
    return jsonify({"description": _describe(data) or None})


# ---------------------------------------------------------------------------
# POST /api/jobs/feedback
# ---------------------------------------------------------------------------

@jobs_bp.route("/api/jobs/feedback", methods=["POST"])
@require_firebase_auth
def post_feedback():
    uid = request.firebase_user["uid"]
    db = get_db()
    data = request.get_json(silent=True) or {}

    job_id = data.get("job_id")
    signal = data.get("signal")

    if not job_id:
        return jsonify({"error": "job_id is required"}), 400
    if signal not in ("positive", "negative"):
        return jsonify({"error": "signal must be 'positive' or 'negative'"}), 400

    pref_doc = {
        "job_id": job_id,
        "signal": signal,
        "company": data.get("company"),
        "category": data.get("category"),
        "created_at": datetime.now(timezone.utc),
    }
    db.collection("users").document(uid).collection("jobPreferences").document(job_id).set(pref_doc)

    # Invalidate cache
    db.collection("users").document(uid).update({"jobFeedCache.ranked_at": None})

    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# GET /api/jobs/filters
# ---------------------------------------------------------------------------

@jobs_bp.route("/api/jobs/filters", methods=["GET"])
@require_firebase_auth
def get_filters():
    now = datetime.now(timezone.utc)

    # Check module-level cache
    if (
        _filters_cache["data"] is not None
        and _filters_cache["cached_at"] is not None
        and (now - _filters_cache["cached_at"]).total_seconds() < FILTERS_CACHE_TTL
    ):
        return jsonify(_filters_cache["data"])

    db = get_db()
    types = set()
    categories = set()
    total = 0

    query = db.collection("jobs").limit(500)
    for doc in query.stream():
        d = doc.to_dict()
        total += 1
        if d.get("type"):
            types.add(d["type"])
        if d.get("category"):
            categories.add(d["category"])

    result = {
        "types": sorted(types),
        "categories": sorted(categories),
        "total_jobs": total,
    }
    _filters_cache["data"] = result
    _filters_cache["cached_at"] = now

    return jsonify(result)
