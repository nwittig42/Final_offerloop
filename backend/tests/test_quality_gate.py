"""
Unit tests for the pipeline quality gate.

Pure-function tests over normalized job dicts. No live API calls and no
Firestore dependency.
"""
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.quality_gate import evaluate, apply, compute_relevance_tier


def _base_job(**overrides):
    job = {
        "job_id": "fantasticjobs_1",
        "source": "fantasticjobs",
        "title": "Software Engineer Intern",
        "company": "Stripe",
        "description_raw": "Build payment systems. Pursuing a Bachelor's in CS. " * 5,
        "posted_at": datetime.now(timezone.utc) - timedelta(days=2),
        "type": "INTERNSHIP",
        "ai_employment_type": "INTERN",
    }
    job.update(overrides)
    return job


class TestQualityGate:

    def test_clean_intern_kept(self):
        ok, reason = evaluate(_base_job())
        assert ok and reason is None

    def test_recruitment_agency_dropped(self):
        ok, reason = evaluate(_base_job(linkedin_org_recruitment_agency=True))
        assert not ok and reason == "linkedin_recruitment_agency"

    def test_staffing_company_name_dropped(self):
        ok, reason = evaluate(_base_job(company="Insight Global Staffing"))
        assert not ok and reason == "staffing_company_name"

    def test_scam_in_description_dropped(self):
        ok, reason = evaluate(_base_job(
            description_raw="Earn up to $5000/week! 100% commission. Hiring immediately!!! " * 5
        ))
        assert not ok and reason == "scam_pattern"

    def test_senior_title_kept_for_experienced_users(self):
        # 2026-07-21 multi-audience change: senior titles stay in the pool
        # (relevance tier 3) so the per-user seniority gate can serve them
        # to experienced users. Students never see them — the bidirectional
        # gate in job_board.apply_hard_gate_seniority rejects them per-user.
        ok, reason = evaluate(_base_job(title="Senior Software Engineer"))
        assert ok and reason is None

    def test_senior_title_with_intern_override_kept(self):
        ok, _ = evaluate(_base_job(title="Senior Engineer Intern Program"))
        # "Intern" in title overrides Senior — rare but exists
        assert ok

    def test_intern_with_5_years_yoe_dropped(self):
        ok, reason = evaluate(_base_job(
            description_raw="We need 5+ years of experience in distributed systems. " * 5
        ))
        assert not ok and reason == "intern_yoe_inconsistent"

    def test_too_short_description_dropped(self):
        ok, reason = evaluate(_base_job(description_raw="apply now"))
        assert not ok and reason == "description_too_short"

    def test_simplify_short_description_kept(self):
        # Simplify intentionally stores empty descriptions; gate exempts that source
        ok, _ = evaluate(_base_job(source="simplify", description_raw=""))
        assert ok

    def test_stale_posting_dropped(self):
        old = datetime.now(timezone.utc) - timedelta(days=90)
        ok, reason = evaluate(_base_job(posted_at=old))
        assert not ok and reason == "too_old"

    def test_apply_returns_breakdown(self):
        jobs = [
            _base_job(job_id="a"),
            _base_job(job_id="b", title="Senior Director"),
            _base_job(job_id="c", company="Robert Half Staffing"),
        ]
        kept, drops = apply(jobs)
        # Senior titles are kept now (per-user gate handles them downstream).
        assert len(kept) == 2
        assert {j["job_id"] for j in kept} == {"a", "b"}
        assert drops.get("staffing_company_name") == 1


class TestFinanceTitlesReachTier1:
    """Bank / Big-4 early-career titles must grade tier 1, not tier 2.

    `_TARGET_FUNCTION_TITLE` was tech-weighted: for finance it matched only the
    literal phrases "investment banking" and "sales and trading". So the titles
    our students actually recruit for — "2027 Summer Analyst Program", "Global
    Markets Summer Analyst", "Asset Management Summer Analyst", "Audit
    Associate" — matched no target function and graded tier 2, which ranks them
    below every tech internship and skips enrichment entirely
    (writer._apply_enrichment_gate flags only tier 1 for Firecrawl + PDL).
    """

    TIER1_TITLES = [
        "Investment Banking Summer Analyst 2027",
        "2027 Summer Analyst Program",
        "Global Markets Summer Analyst 2027",
        "Asset Management Summer Analyst",
        "Equity Capital Markets Summer Analyst",
        "Corporate Banking Analyst Program",
        "Wealth Management Summer Analyst",
        "Private Bank Summer Analyst 2027",
        "Investment Management Summer Analyst",
        "2027 Early Careers: Analyst, Underwriting - Structured Credit",
        "Sales & Trading Summer Analyst",
        "Restructuring Summer Analyst",
        "Audit Associate - Fall 2027",
        "Assurance Intern 2027",
        "Actuarial Summer Intern",
        "Summer Associate - Consulting",
    ]

    # The regex must stay a FUNCTION list. Matching a bare "analyst" or
    # "associate" would sweep these in and make tiering meaningless.
    NOT_TIER1_TITLES = [
        "Warehouse Associate",
        "Retail Sales Associate",
        "Customer Support Associate",
        "Client Services Associate",
        "Teller",
        "Barista",
        "Summer Camp Counselor",
        "Groundskeeper",
    ]

    SENIOR_TITLES = [
        "Senior Analyst - FIG",
        "Vice President - Asset Management",
        "Managing Director, Global Markets",
        "Director of Audit",
    ]

    def test_finance_early_career_is_tier1(self):
        for title in self.TIER1_TITLES:
            doc = {"title": title, "analyst_entry": True}
            assert compute_relevance_tier(doc) == 1, f"{title!r} should be tier 1"

    def test_non_target_roles_stay_out_of_tier1(self):
        for title in self.NOT_TIER1_TITLES:
            doc = {"title": title, "analyst_entry": True}
            assert compute_relevance_tier(doc) != 1, f"{title!r} must not be tier 1"

    def test_senior_finance_titles_stay_tier3(self):
        for title in self.SENIOR_TITLES:
            doc = {"title": title, "analyst_entry": True}
            assert compute_relevance_tier(doc) == 3, f"{title!r} should be tier 3"

    def test_tech_titles_unaffected(self):
        for title in ("Software Engineer Intern", "Business Analyst Intern",
                      "Data Scientist Intern", "Product Manager Intern"):
            assert compute_relevance_tier({"title": title}) == 1, f"{title!r} regressed"


class TestSeniorRanksAreNotEarlyCareer:
    """A senior rank must disqualify tier 1 unless an intern/co-op marker overrides.

    `_SENIOR_TITLE` was only consulted by `is_candidate_title`, which just the
    direct-ATS fetchers call — so it never applied to Greenhouse, Lever, Ashby
    or Simplify, i.e. to essentially every job we hold. Because the bare word
    "associate" is in `_EARLY_CAREER_TITLE`, "Associate Director, Business
    Development" graded tier 1: "associate" made it early-career, "business
    development" made it on-target, and "Director" was never checked.

    Found live on 2026-10-22 in a sample of the 30 newest tier-1 jobs.
    """

    DEMOTED = [
        "Associate Director, Business Development",
        "Associate Principal, Consulting",
        "Associate Vice President, Asset Management",
        "Lead Data Scientist",
        "Head of Investment Banking",
        "Staff Software Engineer",
    ]

    # An unmistakable early-career marker outranks a senior-sounding word.
    STILL_TIER1 = [
        "Software Engineer Intern",
        "Senior Software Engineer Intern",   # intern wins over "Senior"
        "2027 Investment Banking Summer Analyst",
        "Summer Associate - Consulting",
        "Audit Associate - Fall 2027",
        "Business Analyst Intern",
    ]

    @pytest.mark.parametrize("title", DEMOTED)
    def test_senior_rank_is_not_tier1(self, title):
        assert compute_relevance_tier({"title": title}) != 1, \
            f"{title!r} is a senior role and must not reach the feed default"

    @pytest.mark.parametrize("title", STILL_TIER1)
    def test_early_career_override_wins(self, title):
        assert compute_relevance_tier({"title": title}) == 1, f"{title!r} regressed"

    @pytest.mark.parametrize("title,expected", [
        ("2027 Summer Analyst Program", 1),
        ("Global Markets Summer Analyst", 1),
        ("Senior Analyst - FIG", 3),
    ])
    def test_analyst_entry_path_unaffected(self, title, expected):
        doc = {"title": title, "analyst_entry": True}
        assert compute_relevance_tier(doc) == expected
