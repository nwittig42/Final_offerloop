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


def test_crawled_apply_urls_reads_target_sources_only(monkeypatch):
    """Per-posting de-dup: we collect the apply URLs the target crawl holds.

    Replaces test_crawled_hosts_from_csvs. Host-level de-dup dropped any
    Simplify listing that merely lived on a crawled tenant — including
    postings past a search cap, Goldman roles outside the CAMPUS filter, and
    other career sites on the same host — so it is matched per posting now.
    """
    from backend.pipeline import direct

    captured = {}

    class _Snap:
        def __init__(self, url):
            self._url = url

        def to_dict(self):
            return {"apply_url": self._url}

    class _Query:
        def where(self, filter=None):
            captured["filter"] = filter
            return self

        def select(self, fields):
            captured["select"] = fields
            return self

        def stream(self):
            return iter([
                _Snap("https://citi.wd5.myworkdayjobs.com/en-US/2/job/Summer-Analyst_123"),
                _Snap("https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/9?x=1"),
                _Snap(""),  # a job with no apply url must not contribute ""
            ])

    class _DB:
        def collection(self, name):
            captured["collection"] = name
            return _Query()

    monkeypatch.setattr("backend.app.extensions.get_db", lambda: _DB())
    urls = direct.crawled_apply_urls()

    assert captured["collection"] == "jobs"
    # Projection only — never pull whole job bodies for a de-dup check.
    assert captured["select"] == ["apply_url"]
    # Locale segment and query string are normalized away so the Simplify
    # form of the same posting still matches.
    assert urls == frozenset({
        "https://citi.wd5.myworkdayjobs.com/2/job/Summer-Analyst_123",
        "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/9",
    })
    assert "" not in urls


def test_crawled_apply_urls_fails_open(monkeypatch):
    """A Firestore failure must disable de-dup, never drop listings.

    A duplicate job is visible and annoying; a missing job is invisible.
    """
    from backend.pipeline import direct

    def boom():
        raise RuntimeError("firestore down")

    monkeypatch.setattr("backend.app.extensions.get_db", boom)
    assert direct.crawled_apply_urls() == frozenset()


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


def test_simplify_skips_only_postings_the_direct_crawl_holds():
    """Only the exact duplicate is dropped; its neighbour on the same host stays.

    Under host-level de-dup both of these died because they share
    citi.wd5.myworkdayjobs.com, even though the direct crawl only produced the
    first. That is the bug this replaces.
    """
    from backend.pipeline import direct, fetcher

    held = "https://citi.wd5.myworkdayjobs.com/2/job/New-York/Summer-Analyst_123"
    resp = MagicMock()
    resp.json.return_value = [
        # same posting as `held`, but with a locale segment and a query string
        _listing("a1", "https://citi.wd5.myworkdayjobs.com/en-US/2/job/New-York/Summer-Analyst_123?src=simplify"),
        # a DIFFERENT posting on the same tenant — must survive
        _listing("b2", "https://citi.wd5.myworkdayjobs.com/2/job/Tampa/Operations-Analyst_999"),
        # a different tenant entirely — must survive
        _listing("c3", "https://acme.wd1.myworkdayjobs.com/careers/job/NY/Intern_9"),
    ]
    resp.raise_for_status.return_value = None
    with patch.object(fetcher.requests, "get", return_value=resp), \
            patch.object(direct, "crawled_apply_urls",
                         return_value=frozenset({held})), \
            patch.object(fetcher.time, "sleep"):
        jobs = fetcher.fetch_simplify()
    assert [j["job_id"] for j in jobs] == ["simplify_b2", "simplify_c3"]


def test_simplify_keeps_everything_when_dedup_unavailable():
    from backend.pipeline import direct, fetcher

    resp = MagicMock()
    resp.json.return_value = [
        _listing("a1", "https://citi.wd5.myworkdayjobs.com/2/job/Summer-Analyst_123"),
        _listing("b2", "https://acme.wd1.myworkdayjobs.com/careers/job/NY/Intern_9"),
    ]
    resp.raise_for_status.return_value = None
    with patch.object(fetcher.requests, "get", return_value=resp), \
            patch.object(direct, "crawled_apply_urls", return_value=frozenset()), \
            patch.object(fetcher.time, "sleep"):
        jobs = fetcher.fetch_simplify()
    assert [j["job_id"] for j in jobs] == ["simplify_a1", "simplify_b2"]


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
