"""Direct-source registry (backend.pipeline.direct) and crawl plumbing.

No live HTTP, no Firestore: fetchers, slug loading, crawl state and the
writer are all patched.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_no_circular_import():
    code = "import backend.pipeline.quality_gate\nimport backend.pipeline.direct\n"
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_job_id_rejects_underscore_and_slash():
    from backend.pipeline import direct

    with pytest.raises(ValueError):
        direct.make_job_id("workday", "citi", "26978_591")
    with pytest.raises(ValueError):
        direct.make_job_id("workday", "ci/ti", "1")
    assert (
        direct.make_job_id("oracle", "jpmorgan", "210772971")
        == "direct_oracle_jpmorgan_210772971"
    )


def test_cleanup_parses_new_platform_ids():
    from backend.pipeline import writer

    assert writer._ats_board("direct_oleeo_jefferies_1991") == ("oleeo", "jefferies")


def test_candidate_titles():
    from backend.pipeline.direct import is_candidate_title

    assert is_candidate_title("2027 Summer Analyst Program", False) is True
    assert is_candidate_title("Analyst - Healthcare", True) is True
    assert is_candidate_title("Analyst - Healthcare", False) is False
    assert is_candidate_title("Senior Analyst - FIG", True) is False
    assert is_candidate_title("2028 Summer Analyst Coffee Chats", False) is False


def _old_doc(source: str) -> dict:
    return {
        "job_id": f"direct_{source}_citi_1",
        "source": source,
        "title": "2027 Summer Analyst Program",
        "company": "Citi",
        "description_raw": "A real description of the summer analyst program. " * 3,
        "posted_at": datetime.now(timezone.utc) - timedelta(days=200),
    }


def test_board_diff_sources_skip_age_drop():
    from backend.pipeline import quality_gate

    assert quality_gate.evaluate(_old_doc("workday")) == (True, None)
    assert quality_gate.evaluate(_old_doc("greenhouse")) == (False, "too_old")


def test_analyst_entry_tier():
    from backend.pipeline.quality_gate import compute_relevance_tier

    assert compute_relevance_tier(
        {"title": "Investment Banking Analyst", "analyst_entry": True}
    ) == 1
    assert compute_relevance_tier({"title": "Investment Banking Analyst"}) == 3


def _raw(source: str, **extra) -> dict:
    raw = {
        "job_id": f"direct_{source}_jpmorgan_1",
        "source": source,
        "title": "Investment Banking Analyst",
        "company": "JPMorgan Chase",
        "location": "New York, NY, United States",
        "description_raw": "x" * 100,
        "apply_url": "https://example.com/1",
        "posted_at": "2026-10-01T00:00:00Z",
    }
    raw.update(extra)
    return raw


def test_normalizer_passes_team_fields():
    from backend.pipeline import normalizer

    doc = normalizer._normalize_board_job(
        _raw("workday", department="Investment Banking", team="M&A", analyst_entry=True)
    )
    assert doc["department"] == "Investment Banking"
    assert doc["team"] == "M&A"
    assert doc["analyst_entry"] is True

    bare = normalizer._normalize_board_job(_raw("greenhouse"))
    assert (bare["department"], bare["team"], bare["analyst_entry"]) == ("", "", False)

    with patch.object(normalizer, "_normalize_board_job", wraps=normalizer._normalize_board_job) as spy:
        for source in ("oracle", "oleeo"):
            assert normalizer.normalize_job(_raw(source)) is not None
        assert spy.call_count == 2


def test_target_platform_tiers_follow_hot_slugs(tmp_path, monkeypatch):
    """Target employers are hot via hot_slugs.txt; other CSV tenants are cold."""
    from backend.pipeline import slug_loader

    (tmp_path / "workday.csv").write_text(
        "name,slug,url,analyst_entry\n"
        "Citi,citi,https://citi.wd5.myworkdayjobs.com/2,1\n"
        "Acme,acme,https://acme.wd1.myworkdayjobs.com/careers,0\n"
    )
    (tmp_path / "hot_slugs.txt").write_text("workday:citi\n")
    monkeypatch.setattr(slug_loader, "DATA_DIR", tmp_path)
    slug_loader.clear_cache()
    try:
        assert slug_loader.load_slugs("workday", "hot") == ["citi"]
        assert slug_loader.load_slugs("workday", "cold") == ["acme"]
        assert slug_loader.load_slugs("workday", "all") == ["acme", "citi"]
        assert slug_loader.get_record("workday", "citi")["analyst_entry"] is True
        assert slug_loader.get_record("workday", "acme")["analyst_entry"] is False
    finally:
        slug_loader.clear_cache()


# --------------------------------------------------------------------------
# run_crawl_ats plumbing
# --------------------------------------------------------------------------

def _crawl_patches(fetchers: dict):
    """Patch everything run_crawl_ats touches outside the process."""
    from backend.pipeline import crawl_state, direct, slug_loader, writer

    calls: list[str] = []

    def fake_get_fetcher(platform):
        def _fetch(slug):
            calls.append(platform)
            return fetchers.get(platform, lambda s: [])(slug)
        return _fetch

    sync = MagicMock(return_value={
        "board_hash_matched": False, "snapshot_count": 0, "written": 0, "expired": 0,
    })
    mark_failure = MagicMock()
    patches = [
        patch.object(direct, "get_fetcher", side_effect=fake_get_fetcher),
        patch.object(slug_loader, "load_slugs", return_value=["acme"]),
        patch.object(slug_loader, "get_record", return_value=None),
        patch.object(crawl_state, "read_state_batch", return_value={}),
        patch.object(crawl_state, "mark_failure", mark_failure),
        patch.object(writer, "sync_board_jobs", sync),
    ]
    return calls, sync, mark_failure, patches


def _run(patches, **kwargs):
    from backend.pipeline.main import run_crawl_ats

    for p in patches:
        p.start()
    try:
        return run_crawl_ats(**kwargs)
    finally:
        for p in reversed(patches):
            p.stop()


def test_crawl_defaults_to_legacy_platforms():
    calls, sync, _, patches = _crawl_patches({})
    _run(patches)
    assert sorted(calls) == ["ashby", "greenhouse", "lever"]
    assert sync.call_count == 3


def test_crawl_platform_filter_and_dry_run():
    calls, sync, mark_failure, patches = _crawl_patches(
        {"workday": lambda slug: [_raw("workday", title="2027 Summer Analyst Program")]}
    )
    result = _run(patches, platforms=("workday",), dry_run=True)
    assert calls == ["workday"]
    sync.assert_not_called()
    mark_failure.assert_not_called()
    assert result["per_platform"]["workday"]["slugs_crawled"] == 1
    assert result["per_platform"]["workday"]["snapshot_count"] == 1


def test_fetch_error_marks_failure_without_sync():
    from backend.pipeline.direct import FetchError

    def boom(slug):
        raise FetchError("detail call failed")

    calls, sync, mark_failure, patches = _crawl_patches({"workday": boom})
    result = _run(patches, platforms=("workday",))
    mark_failure.assert_called_once()
    assert mark_failure.call_args.args[:2] == ("workday", "acme")
    sync.assert_not_called()
    assert result["slugs_failed"] == 1


@pytest.mark.parametrize("job_id,url", [
    ("direct_workday_citi_1", "https://citi.wd5.myworkdayjobs.com/en-US/2/job/1"),
    ("direct_oracle_jpmorgan_1", "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/job/1"),
    ("direct_oleeo_jefferies_1", "https://jefferies.tal.net/vx/candidate/apply/1"),
])
def test_target_platforms_never_auto_apply(job_id, url):
    from app.services.auto_apply.ats_detector import is_eligible

    assert is_eligible({"job_id": job_id, "apply_url": url}) is False


def test_totals_report_early_career():
    from backend.pipeline import quality_gate

    calls, sync, _, patches = _crawl_patches(
        {"workday": lambda slug: [
            _raw("workday", title="2027 Summer Analyst Program"),
            _raw("workday", title="Senior Vice President"),
        ]}
    )

    def fake_gate(docs, mode="hot"):
        for d, t in zip(docs, (1, 3)):
            d["relevance_tier"] = t
        return docs, []

    patches.append(patch.object(quality_gate, "apply", side_effect=fake_gate))
    result = _run(patches, platforms=("workday",), dry_run=True)
    assert result["tier1"] == 1 and result["tier3"] == 1
    assert result["early_career"] == 1
    assert result["per_platform"]["workday"]["early_career"] == 1
