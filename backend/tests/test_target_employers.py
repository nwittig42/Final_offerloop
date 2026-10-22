"""Target-employer CSVs (build_target_employers) and Simplify de-duplication.

No live HTTP: CSVs are fixtures in tmp_path and `requests.get` is patched.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


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


class _Snap:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return dict(self._data)


def _jobs_db(rows):
    """Fake Firestore: jobs.where(source in ...).select([...]).stream()."""
    db = MagicMock()
    db.collection.return_value.where.return_value.select.return_value.stream.return_value = [
        _Snap(r) for r in rows]
    return db


def _simplify(listings, db):
    from backend.pipeline import fetcher

    resp = MagicMock()
    resp.json.return_value = listings
    resp.raise_for_status.return_value = None
    with patch.object(fetcher.requests, "get", return_value=resp), \
            patch("backend.app.extensions.get_db", return_value=db), \
            patch.object(fetcher.time, "sleep"):
        return [j["job_id"] for j in fetcher.fetch_simplify()]


def test_simplify_skips_only_postings_the_direct_crawl_holds():
    """Per-posting de-dup: a listing on a crawled host survives unless the
    direct crawl holds that exact (normalized) apply URL."""
    db = _jobs_db([
        {"apply_url": "https://citi.wd5.myworkdayjobs.com/en-US/2/job/New-York/Summer-Analyst_123"},
        {"apply_url": "https://higher.gs.com/roles/170696"},
        {"apply_url": "https://higher.gs.com/roles/999", "expired": True},
    ])
    ids = _simplify([
        # same posting: locale segment, query string, case and trailing slash differ
        _listing("a1", "https://CITI.wd5.myworkdayjobs.com/2/job/New-York/Summer-Analyst_123/?utm_source=Simplify"),
        # same host, a posting the direct crawl doesn't produce
        _listing("b2", "https://citi.wd5.myworkdayjobs.com/2/job/New-York/Ops-Intern_456"),
        _listing("c3", "https://higher.gs.com/roles/170696#apply"),
        _listing("d4", "https://higher.gs.com/roles/555"),       # Goldman non-campus
        _listing("e5", "https://higher.gs.com/roles/999"),       # held but expired
    ], db)
    assert ids == ["simplify_b2", "simplify_d4", "simplify_e5"]
    where = db.collection.return_value.where
    f = where.call_args.kwargs["filter"]
    assert (f.field_path, f.op_string, f.value) == (
        "source", "in", ["workday", "oracle", "oleeo", "goldman", "manual"])
    where.return_value.select.assert_called_once_with(["apply_url", "expired"])


def test_simplify_no_dedup_when_firestore_read_fails(caplog):
    db = MagicMock()
    db.collection.return_value.where.return_value.select.return_value.stream.side_effect = (
        RuntimeError("boom"))
    with caplog.at_level("WARNING"):
        ids = _simplify([_listing("a1", "https://higher.gs.com/roles/1")], db)
    assert ids == ["simplify_a1"]
    assert any("no de-dup" in r.getMessage() for r in caplog.records)


def test_simplify_no_dedup_without_firestore(caplog):
    with caplog.at_level("WARNING"):
        ids = _simplify([_listing("a1", "https://higher.gs.com/roles/1")], None)
    assert ids == ["simplify_a1"]
    assert any("no de-dup" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("url,expected", [
    ("HTTPS://Citi.WD5.myworkdayjobs.com/en-US/2/job/X_1/?a=1#f",
     "https://citi.wd5.myworkdayjobs.com/2/job/X_1"),
    ("https://citi.wd5.myworkdayjobs.com/2/job/X_1", "https://citi.wd5.myworkdayjobs.com/2/job/X_1"),
    ("https://jefferies.tal.net/vx/xf-abc/candidate/so/pm/1/pl/2/opp/1991-SA/en-GB",
     "https://jefferies.tal.net/vx/candidate/so/pm/1/pl/2/opp/1991-SA/en-GB"),
    ("https://higher.gs.com/roles/1/", "https://higher.gs.com/roles/1"),
    ("https://example.com/", "https://example.com"),
    ("", ""),
])
def test_normalize_apply_url(url, expected):
    from backend.pipeline.direct import normalize_apply_url

    assert normalize_apply_url(url) == expected


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
