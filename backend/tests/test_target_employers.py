"""Target-employer CSVs (build_target_employers) and Simplify de-duplication.

No live HTTP: CSVs are fixtures in tmp_path and `requests.get` is patched.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _write_fixture_csvs(tmp_path):
    (tmp_path / "workday.csv").write_text(
        "name,slug,url,analyst_entry\n"
        "Citi,citi,https://citi.wd5.myworkdayjobs.com/2,1\n"
        "Acme,acme,https://acme.wd1.myworkdayjobs.com/careers,0\n"
    )
    (tmp_path / "oracle.csv").write_text(
        "name,slug,url,analyst_entry\n"
        "JPMorgan Chase,jpmorgan,https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001,1\n"
    )
    (tmp_path / "oleeo.csv").write_text(
        "name,slug,url,analyst_entry\n"
        "Jefferies,jefferies,https://jefferies.tal.net/vx/lang-en-GB/mobile-0/appcentre-ext/brand-4/candidate/jobboard/vacancy/2/adv/,1\n"
    )
    (tmp_path / "hot_slugs.txt").write_text(
        "greenhouse:stripe\nworkday:citi  # Citi\noracle:jpmorgan\n"
    )


def test_crawled_hosts_from_csvs(tmp_path, monkeypatch):
    """Only hot-tier target boards count: cold `acme` and un-hot `jefferies`
    are not crawled on schedule, so their Simplify listings must be kept."""
    from backend.pipeline import direct, slug_loader

    _write_fixture_csvs(tmp_path)
    monkeypatch.setattr(slug_loader, "DATA_DIR", tmp_path)
    slug_loader.clear_cache()
    try:
        assert direct.crawled_hosts() == frozenset({
            "citi.wd5.myworkdayjobs.com",
            "jpmc.fa.oraclecloud.com",
        })
    finally:
        slug_loader.clear_cache()


def _listing(listing_id: str, url: str) -> dict:
    return {
        "id": listing_id,
        "title": "Summer Analyst Intern",
        "company_name": "Some Co",
        "active": True,
        "is_visible": True,
        "locations": ["New York, NY"],
        "url": url,
        "date_posted": 1760000000,
        "category": "Finance",
    }


def test_simplify_skips_crawled_hosts():
    from backend.pipeline import direct, fetcher

    resp = MagicMock()
    resp.json.return_value = [
        _listing("a1", "https://citi.wd5.myworkdayjobs.com/2/job/New-York/Summer-Analyst_123"),
        _listing("b2", "https://acme.wd1.myworkdayjobs.com/careers/job/NY/Intern_9"),
    ]
    resp.raise_for_status.return_value = None
    with patch.object(fetcher.requests, "get", return_value=resp), \
            patch.object(direct, "crawled_hosts",
                         return_value=frozenset({"citi.wd5.myworkdayjobs.com"})), \
            patch.object(fetcher.time, "sleep"):
        jobs = fetcher.fetch_simplify()
    assert [j["job_id"] for j in jobs] == ["simplify_b2"]


def test_script_slugify():
    from backend.scripts.build_target_employers import slugify

    assert slugify("J.P. Morgan Chase & Co.") == "jp-morgan-chase-co"


@pytest.mark.parametrize("name,expected", [
    ("The Walt Disney Company", 0),
    ("Stanley Black & Decker", 0),
    ("Ensemble Health Partners", 0),
    ("Barclays", 1),
    ("BlackRock", 1),
    ("PIMCO", 1),
    ("PJT Partners", 1),
    ("EY", 1),
    ("T. Rowe Price", 1),
    ("S&P Global", 1),
    ("Goldman Sachs", 1),
])
def test_analyst_entry_classifier(name, expected):
    from backend.scripts.build_target_employers import analyst_entry

    assert analyst_entry(name) == expected


def test_tenant_site_prefers_public():
    from backend.scripts.build_target_employers import simplify_workday_tenants

    def item(url, active=True):
        return {"url": url, "company_name": "Acme", "active": active}

    tenants = simplify_workday_tenants([
        item("https://acme.wd1.myworkdayjobs.com/acme_private/job/x_1"),
        item("https://acme.wd1.myworkdayjobs.com/acme_private/job/x_2"),
        item("https://acme.wd1.myworkdayjobs.com/AcmeCareers/job/x_3", active=False),
        item("https://solo.wd1.myworkdayjobs.com/invitation-only/job/x_4"),
    ])
    assert tenants["acme.wd1.myworkdayjobs.com"]["site"] == "AcmeCareers"
    assert tenants["acme.wd1.myworkdayjobs.com"]["public_site"] is True
    assert tenants["solo.wd1.myworkdayjobs.com"]["public_site"] is False
