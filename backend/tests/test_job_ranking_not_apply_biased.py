"""Auto-apply eligibility must rank jobs, never filter or reorder them.

Both surfaces used to privilege auto-applyable jobs (Greenhouse / Lever /
Ashby) over everything else:

  * `routes/jobs.py::_serialize_jobs` re-sorted every feed response so that
    eligible jobs came before ineligible ones, burying a well-matched
    Workday summer-analyst posting below a weak Greenhouse match.
  * `scout/tools.py::find_jobs` hard-filtered to eligible jobs, so Workday /
    Oracle / Oleeo / Goldman postings at target employers were invisible in
    Scout unless there was no eligible match at all.

These tests pin the corrected behaviour: quality decides order, eligibility
is only a tiebreaker, and nothing is ever dropped for being ineligible.
"""
import pytest

from app.services.scout.tools import job_rank_key


def _job(score, tier, eligible, job_id):
    return {"_score": score, "_tier": tier,
            "auto_apply_eligible": eligible, "job_id": job_id}


@pytest.mark.unit
def test_better_match_outranks_eligible_job():
    """A stronger token match wins even though it can't be auto-applied."""
    workday = _job(5, 1, False, "direct_workday_citi_1")
    greenhouse = _job(1, 1, True, "direct_greenhouse_acme_1")
    assert sorted([greenhouse, workday], key=job_rank_key)[0] is workday


@pytest.mark.unit
def test_early_career_tier_outranks_eligibility():
    """At equal match, a tier-1 early-career role beats an applyable tier-3."""
    summer_analyst = _job(3, 1, False, "direct_goldman_gs_1")
    senior_ish = _job(3, 3, True, "direct_lever_acme_1")
    assert sorted([senior_ish, summer_analyst], key=job_rank_key)[0] is summer_analyst


@pytest.mark.unit
def test_eligibility_breaks_ties_only():
    """Identical quality — then and only then prefer the applyable job."""
    eligible = _job(3, 1, True, "direct_ashby_acme_1")
    ineligible = _job(3, 1, False, "direct_oracle_jpmorgan_1")
    assert sorted([ineligible, eligible], key=job_rank_key)[0] is eligible


@pytest.mark.unit
def test_ineligible_jobs_are_never_dropped():
    """The old code returned ONLY eligible jobs whenever one existed."""
    jobs = [
        _job(5, 1, False, "direct_workday_citi_1"),
        _job(4, 1, False, "direct_oracle_jpmorgan_1"),
        _job(1, 3, True, "direct_greenhouse_acme_1"),
    ]
    ranked = sorted(jobs, key=job_rank_key)
    assert len(ranked) == 3, "no job may be filtered out for being ineligible"
    assert [j["job_id"] for j in ranked] == [
        "direct_workday_citi_1",
        "direct_oracle_jpmorgan_1",
        "direct_greenhouse_acme_1",
    ]


@pytest.mark.unit
def test_missing_fields_default_safely():
    """A doc with no relevance_tier/score must not raise or jump the queue."""
    bare = {"job_id": "x"}
    good = _job(2, 1, True, "y")
    assert job_rank_key(bare) == (0, 3, True)
    assert sorted([bare, good], key=job_rank_key)[0] is good


@pytest.mark.unit
def test_serialize_jobs_preserves_caller_ranking():
    """_serialize_jobs must not reorder; the caller's match ranking stands."""
    from backend.app.routes import jobs as jobs_route

    ordered = [
        {"job_id": "a", "source": "workday", "match_score": 99},
        {"job_id": "b", "source": "greenhouse", "match_score": 50},
        {"job_id": "c", "source": "oracle", "match_score": 10},
    ]
    out = jobs_route._serialize_jobs(ordered)
    assert [j["job_id"] for j in out] == ["a", "b", "c"]
