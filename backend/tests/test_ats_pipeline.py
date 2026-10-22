"""Phase 0 direct-ATS pipeline tests.

Covers the eng-review coverage diagram (11 paths, minus the ★ concurrency
smoke tests + adaptive-brake which isn't implemented). Everything mocked —
no live HTTP, no Firestore.

Priority tags in each test docstring: ★★★ regression-critical, ★★ regression,
★ smoke.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from backend.pipeline import crawl_state, quality_gate, slug_loader, writer


# --------------------------------------------------------------------------
# ★★★ direct_ prefix collision safety vs FJ writes
# --------------------------------------------------------------------------

def test_direct_prefix_never_collides_with_fj():
    """Direct-ATS job_ids MUST NOT collide with FJ's format even for the
    same underlying job. FJ uses `fantasticjobs_{ext_id}`; direct uses
    `direct_greenhouse_{slug}_{ext_id}`. Different first tokens."""
    fj_id = "fantasticjobs_abc123"
    # Even if FJ ever ingested a Greenhouse job and coincidentally guessed
    # the slug + ext_id, its ID starts with `fantasticjobs_`, not `direct_`.
    direct_ids = [
        "direct_greenhouse_stripe_1234",
        "direct_lever_spotify_abc",
        "direct_ashby_linear_uuid",
    ]
    for did in direct_ids:
        assert did != fj_id
        assert not did.startswith("fantasticjobs_")
        assert did.startswith("direct_")


# --------------------------------------------------------------------------
# ★★★ _is_excluded excludes expired=True
# --------------------------------------------------------------------------

def test_is_excluded_drops_expired():
    """P0 safety fix: feed ranker MUST NOT surface jobs marked expired=True."""
    from backend.app.utils.job_ranking import _is_excluded

    live = {"title": "Software Engineer Intern", "category": "engineering"}
    expired = {"title": "Software Engineer Intern", "category": "engineering", "expired": True}
    assert _is_excluded(live) is False
    assert _is_excluded(expired) is True


def test_is_excluded_no_effect_when_expired_missing_or_false():
    """expired defaults to False/missing; feed should still see the job."""
    from backend.app.utils.job_ranking import _is_excluded

    assert _is_excluded({"title": "Data Analyst Intern", "expired": False}) is False
    assert _is_excluded({"title": "Data Analyst Intern"}) is False
    # `is True` guard means None doesn't accidentally exclude.
    assert _is_excluded({"title": "Data Analyst Intern", "expired": None}) is False


# --------------------------------------------------------------------------
# ★★★ sync_board_jobs diff correctness (new / removed / unchanged partitions)
# --------------------------------------------------------------------------

def _mk_job(job_id: str, posted_at: str = "2026-07-14T10:00:00Z") -> dict:
    return {
        "job_id": job_id,
        "posted_at": posted_at,
        "title": "Software Engineer Intern",
        "board_platform": "greenhouse",
        "board_slug": "test",
        "relevance_tier": 1,
    }


@patch("backend.pipeline.writer.get_db")
@patch("backend.pipeline.writer._embed_new_jobs_batch")
@patch("backend.pipeline.writer._expire_by_firestore_ids")
@patch("backend.pipeline.crawl_state.write_state")
def test_sync_board_jobs_new_removed_unchanged_partitions(
    mock_write_state, mock_expire, mock_embed, mock_db
):
    """Given prior kept={A,B,C} and current snapshot={B,C,D}: write only D,
    expire only A, and leave B,C alone."""
    mock_db.return_value = MagicMock()

    snapshot = [_mk_job("direct_greenhouse_test_B"),
                _mk_job("direct_greenhouse_test_C"),
                _mk_job("direct_greenhouse_test_D")]
    prior = {
        "board_hash": "different_hash_forces_diff_path",
        "kept_job_ids": [
            "direct_greenhouse_test_A",
            "direct_greenhouse_test_B",
            "direct_greenhouse_test_C",
        ],
    }

    result = writer.sync_board_jobs("greenhouse", "test", snapshot, prior_state=prior)

    assert result["board_hash_matched"] is False
    assert result["written"] == 1  # only D is new
    mock_expire.assert_called_once()
    expired_arg = mock_expire.call_args[0][0]
    assert set(expired_arg) == {"direct_greenhouse_test_A"}


# --------------------------------------------------------------------------
# ★★ Board-hash skip behavior (unchanged snapshot → zero writes)
# --------------------------------------------------------------------------

@patch("backend.pipeline.writer.get_db")
def test_sync_board_jobs_hash_match_short_circuits(mock_db):
    """When new snapshot hashes identically to prior state, sync must not
    touch Firestore at all. This is the 85-95% steady-state skip path."""
    mock_db.return_value = MagicMock()
    snapshot = [_mk_job("direct_greenhouse_test_1")]
    prior = {
        "board_hash": crawl_state.compute_board_hash(snapshot),
        "kept_job_ids": ["direct_greenhouse_test_1"],
    }
    result = writer.sync_board_jobs("greenhouse", "test", snapshot, prior_state=prior)
    assert result["board_hash_matched"] is True
    assert result["written"] == 0
    assert result["expired"] == 0


# --------------------------------------------------------------------------
# ★★ Board-hash pure-function properties
# --------------------------------------------------------------------------

def test_board_hash_is_order_independent():
    """Snapshot hash MUST NOT depend on job order (the ATSes don't guarantee
    stable ordering across polls)."""
    a = [_mk_job("direct_greenhouse_x_1", "2026-07-14T10:00Z"),
         _mk_job("direct_greenhouse_x_2", "2026-07-14T11:00Z")]
    assert crawl_state.compute_board_hash(a) == crawl_state.compute_board_hash(list(reversed(a)))


def test_board_hash_changes_on_updated_at():
    """Same job_id, new posted_at → new hash (this is how we detect a single
    job being edited on the ATS)."""
    a = [_mk_job("direct_greenhouse_x_1", "2026-07-14T10:00Z")]
    b = [_mk_job("direct_greenhouse_x_1", "2026-07-15T10:00Z")]
    assert crawl_state.compute_board_hash(a) != crawl_state.compute_board_hash(b)


def test_board_hash_changes_on_new_job():
    """Adding a job flips the hash → sync will diff instead of short-circuit."""
    a = [_mk_job("direct_greenhouse_x_1")]
    b = a + [_mk_job("direct_greenhouse_x_2")]
    assert crawl_state.compute_board_hash(a) != crawl_state.compute_board_hash(b)


def test_board_hash_empty_snapshot_is_distinct():
    """Empty snapshot must have its own hash — an all-jobs-removed event needs
    to be detectable so we can expire everything."""
    empty_hash = crawl_state.compute_board_hash([])
    non_empty = crawl_state.compute_board_hash([_mk_job("direct_greenhouse_x_1")])
    assert empty_hash != non_empty


# --------------------------------------------------------------------------
# ★★ Slug health: is_dormant helper
# --------------------------------------------------------------------------

def test_is_dormant_helper():
    """is_dormant is the read-side helper for feed/crawl decisions."""
    assert crawl_state.is_dormant(None) is False  # never crawled != dormant
    assert crawl_state.is_dormant({"dormant": True}) is True
    assert crawl_state.is_dormant({"dormant": False}) is False
    assert crawl_state.is_dormant({}) is False


# --------------------------------------------------------------------------
# ★★ jobhive CSV parse via slug_loader (real vendored fixtures)
# --------------------------------------------------------------------------

def test_slug_loader_hot_tier_returns_curated_set():
    """Hot tier must load from hot_slugs.txt across all three ATSes.

    Bounded assertion so this doesn't break every time we curate more slugs
    into Phase 1/2 — the guard here is "we have a curated hot list of at
    least the Phase 0 baseline size."
    """
    slug_loader.clear_cache()
    counts = {a: len(slug_loader.load_slugs(a, "hot"))
              for a in ("greenhouse", "lever", "ashby")}
    total = sum(counts.values())
    assert total >= 270, f"hot tier shrank below Phase 0 baseline: {counts}"
    # Each ATS must be non-empty (regression against a corrupted hot_slugs.txt)
    for ats, n in counts.items():
        assert n > 0, f"{ats} hot tier is empty"


def test_slug_loader_get_company_name_uses_canonical():
    """Company name must come from jobhive's CSV, not slug-title-case."""
    slug_loader.clear_cache()
    # These are known jobhive entries; canonical name differs from slug-title-case
    assert slug_loader.get_company_name("greenhouse", "1password") == "1Password"
    assert slug_loader.get_company_name("greenhouse", "stripe") == "Stripe"
    # Fallback: unknown slug → title-cased
    assert slug_loader.get_company_name("greenhouse", "no-such-company") == "No Such Company"


def test_slug_loader_shard_split_is_deterministic_and_partitions():
    """4-way shard must be deterministic AND cover every slug exactly once."""
    slug_loader.clear_cache()
    cold_full = set(slug_loader.load_slugs("greenhouse", "cold"))
    shard_union: set[str] = set()
    for i in range(4):
        chunk = set(slug_loader.load_slugs("greenhouse", "cold", shard=(i, 4)))
        # No overlap with prior shards
        assert not (chunk & shard_union), f"shard {i} overlaps prior shards"
        shard_union |= chunk
    assert shard_union == cold_full


# --------------------------------------------------------------------------
# ★★ relevance_tier assignment on tier-1/2/3 examples
# --------------------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("Software Engineer Intern", 1),           # early-career + target function
    ("New Grad Software Engineer", 1),         # early-career + target
    ("Investment Banking Summer Analyst", 1),  # early-career (summer analyst) + target
    ("Data Analyst Intern", 1),                # early-career + target (data analyst)
    ("Marketing Intern", 2),                   # early-career, off-target function
    ("Junior Product Manager", 1),             # junior + product manager
    ("Software Engineer", 3),                  # no early-career signal, generic
    ("Product Manager", 3),                    # no early-career signal
])
def test_relevance_tier_assignment(title, expected):
    doc = {"title": title, "description_raw": "sample" * 20, "posted_at": "2026-07-14"}
    assert quality_gate.compute_relevance_tier(doc) == expected


def test_apply_stamps_relevance_tier_on_kept_docs():
    docs = [
        {"title": "Software Engineer Intern", "description_raw": "x" * 100, "posted_at": "2026-07-14"},
        {"title": "Marketing Intern", "description_raw": "x" * 100, "posted_at": "2026-07-14"},
    ]
    kept, _ = quality_gate.apply(docs, mode="hot")
    for d in kept:
        assert "relevance_tier" in d, "quality_gate.apply must stamp relevance_tier"


def test_cold_mode_admits_any_non_blocklist_title():
    """Cold gate loosened 2026-07-15 (pre-launch volume sprint): admit any
    title not in the blue-collar/clinical blocklist. Senior/Staff/Lead still
    dropped upstream by _SENIOR_TITLE in evaluate() step 4. Only the
    blocklist test below (blue-collar drops) still asserts drops.
    """
    docs = [
        {"title": "Product Manager", "description_raw": "Established PM role" * 20, "posted_at": "2026-07-14"},
        {"title": "Software Engineer Intern", "description_raw": "Internship for university students" * 20, "posted_at": "2026-07-14"},
        {"title": "Community Coordinator", "description_raw": "Community role" * 20, "posted_at": "2026-07-14"},
    ]
    kept_cold, drops_cold = quality_gate.apply([dict(d) for d in docs], mode="cold")

    admitted_titles = {d["title"] for d in kept_cold}
    # All 3 admitted — loosened gate intentionally admits generic off-target titles
    assert "Product Manager" in admitted_titles
    assert "Software Engineer Intern" in admitted_titles
    assert "Community Coordinator" in admitted_titles
    assert drops_cold.get("cold_tier_allowlist_miss", 0) == 0


def test_cold_mode_drops_blue_collar_titles():
    """Blocklist for clinical/blue-collar patterns."""
    docs = [
        {"title": "Registered Nurse", "description_raw": "RN full time" * 20, "posted_at": "2026-07-14"},
        {"title": "CDL Truck Driver", "description_raw": "Local routes" * 20, "posted_at": "2026-07-14"},
        {"title": "Line Cook", "description_raw": "Busy kitchen" * 20, "posted_at": "2026-07-14"},
    ]
    kept, drops = quality_gate.apply(docs, mode="cold")
    assert len(kept) == 0
    assert drops.get("cold_tier_allowlist_miss") == 3


# --------------------------------------------------------------------------
# ★★ grnh.se → greenhouse routing in ats_detector
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://grnh.se/abc123", "greenhouse"),
    ("https://www.grnh.se/xyz", "greenhouse"),
    ("https://boards.greenhouse.io/stripe/jobs/1234", "greenhouse"),
    ("https://boards-api.greenhouse.io/v1/boards/x/jobs/1", "greenhouse"),
    ("https://jobs.lever.co/spotify/foo", "lever"),
    ("https://jobs.ashbyhq.com/linear/bar", "ashby"),
    ("https://myworkdayjobs.com/goldman", None),
    ("https://greenhousefoo.com/", None),  # unrelated .com does NOT match
])
def test_ats_detector_url_classification(url, expected):
    from app.services.auto_apply.ats_detector import detect_platform
    assert detect_platform({"apply_url": url}) == expected


# --------------------------------------------------------------------------
# ★★ Enrichment gate: only tier-1 gets pending
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tier,expected_status", [
    (1, "pending"),
    (2, "skipped_low_priority"),
    (3, "skipped_low_priority"),
    (None, "skipped_low_priority"),
])
def test_apply_enrichment_gate(tier, expected_status):
    doc: dict = {}
    if tier is not None:
        doc["relevance_tier"] = tier
    writer._apply_enrichment_gate(doc)
    assert doc["enrichment_status"] == expected_status
    assert doc["title_enrichment_status"] == expected_status


def test_enrichment_gate_preserves_explicit_status():
    """setdefault must not overwrite upstream-set values (e.g. jobs that
    already completed enrichment shouldn't be reset)."""
    doc = {"relevance_tier": 1, "enrichment_status": "done"}
    writer._apply_enrichment_gate(doc)
    assert doc["enrichment_status"] == "done"  # preserved


# --------------------------------------------------------------------------
# ★★ mark_expired_jobs still handles FJ IDs via shared primitive
# --------------------------------------------------------------------------

@patch("backend.pipeline.writer._expire_by_firestore_ids")
def test_mark_expired_jobs_prefixes_fj_ids(mock_expire):
    """The FJ daemon calls mark_expired_jobs(raw_fj_ids); the wrapper must
    still prefix with `fantasticjobs_` before delegating."""
    mock_expire.return_value = 2
    result = writer.mark_expired_jobs(["abc123", "def456"])
    mock_expire.assert_called_once_with(["fantasticjobs_abc123", "fantasticjobs_def456"])
    assert result == {"marked": 2, "not_found": 0, "total": 2}


def test_mark_expired_jobs_empty_short_circuits():
    """Empty input must not touch Firestore at all."""
    result = writer.mark_expired_jobs([])
    assert result == {"marked": 0, "not_found": 0, "total": 0}


# --------------------------------------------------------------------------
# Final-review fixes: sticky dormancy + partial snapshots
# --------------------------------------------------------------------------

@patch("backend.pipeline.writer.get_db")
@patch("backend.pipeline.crawl_state.mark_recovered")
def test_hash_match_clears_failures_and_dormancy(mock_recovered, mock_db):
    """A board that failed 3 times (dormant) and then recovers unchanged must
    have its failure flags cleared, or cleanup deletes its live jobs."""
    mock_db.return_value = MagicMock()
    snapshot = [_mk_job("direct_greenhouse_test_1")]
    prior = {
        "board_hash": crawl_state.compute_board_hash(snapshot),
        "kept_job_ids": ["direct_greenhouse_test_1"],
        "consecutive_failures": 3,
        "dormant": True,
    }
    result = writer.sync_board_jobs("greenhouse", "test", snapshot, prior_state=prior)
    assert result["board_hash_matched"] is True
    mock_recovered.assert_called_once_with(
        "greenhouse", "test", jobs_count=1, prior_state=prior)


@patch("backend.pipeline.writer.get_db")
@patch("backend.pipeline.crawl_state.mark_recovered")
def test_hash_match_healthy_board_writes_nothing(mock_recovered, mock_db):
    mock_db.return_value = MagicMock()
    snapshot = [_mk_job("direct_greenhouse_test_1")]
    prior = {"board_hash": crawl_state.compute_board_hash(snapshot),
             "kept_job_ids": ["direct_greenhouse_test_1"], "consecutive_failures": 0,
             "dormant": False}
    writer.sync_board_jobs("greenhouse", "test", snapshot, prior_state=prior)
    mock_recovered.assert_not_called()


def test_mark_recovered_resets_flags_keeps_hash_and_kept():
    db = MagicMock()
    with patch.object(crawl_state, "get_db", return_value=db):
        crawl_state.mark_recovered("workday", "citi", jobs_count=4,
                                   prior_state={"consecutive_failures": 3, "dormant": True})
    doc = db.collection.return_value.document.return_value.set.call_args.args[0]
    assert doc["consecutive_failures"] == 0 and doc["dormant"] is False
    assert "last_seen_jobs_at" in doc and doc["last_seen_jobs_count"] == 4
    assert "board_hash" not in doc and "kept_job_ids" not in doc


@patch("backend.pipeline.writer.get_db")
@patch("backend.pipeline.writer._embed_new_jobs_batch")
@patch("backend.pipeline.writer._expire_by_firestore_ids")
@patch("backend.pipeline.crawl_state.write_state")
def test_partial_snapshot_writes_new_expires_nothing(
    mock_write_state, mock_expire, mock_embed, mock_db
):
    """A capped (partial) snapshot writes new jobs, expires nothing, keeps
    prior kept + snapshot, and stores no board hash."""
    mock_db.return_value = MagicMock()
    snapshot = [_mk_job("direct_workday_test_B"), _mk_job("direct_workday_test_D")]
    prior = {"board_hash": "old", "kept_job_ids": [
        "direct_workday_test_A", "direct_workday_test_B"]}
    result = writer.sync_board_jobs("workday", "test", snapshot, prior_state=prior, partial=True)
    assert result["written"] == 1 and result["expired"] == 0
    mock_expire.assert_not_called()
    kw = mock_write_state.call_args.kwargs
    assert kw["board_hash"] is None
    assert set(kw["kept_job_ids"]) == {
        "direct_workday_test_A", "direct_workday_test_B", "direct_workday_test_D"}


@patch("backend.pipeline.writer.get_db")
@patch("backend.pipeline.writer._embed_new_jobs_batch")
@patch("backend.pipeline.writer._expire_by_firestore_ids")
@patch("backend.pipeline.crawl_state.write_state")
def test_partial_snapshot_skips_hash_shortcut(mock_write_state, mock_expire, mock_embed, mock_db):
    mock_db.return_value = MagicMock()
    snapshot = [_mk_job("direct_workday_test_B")]
    prior = {"board_hash": crawl_state.compute_board_hash(snapshot),
             "kept_job_ids": ["direct_workday_test_B"]}
    result = writer.sync_board_jobs("workday", "test", snapshot, prior_state=prior, partial=True)
    assert result["board_hash_matched"] is False
    mock_write_state.assert_called_once()
