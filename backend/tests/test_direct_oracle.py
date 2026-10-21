"""Oracle Recruiting direct fetcher: no network; the module session is faked."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from backend.pipeline.direct import FetchError, oracle

FIX = Path(__file__).parent / "fixtures" / "direct"
LIST = json.loads((FIX / "oracle_list.json").read_text())
DETAIL = json.loads((FIX / "oracle_detail.json").read_text())
RECORD = {"name": "JPMorgan Chase", "slug": "jpmorgan", "analyst_entry": True,
          "url": "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001"}
TERMS = ("intern", "internship", "analyst", "new grad", "early career", "summer")


def _resp(payload=None, status=200, text=None):
    r = MagicMock()
    r.status_code = status
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(str(status))
    if text is not None:
        r.json.side_effect = ValueError("not json")
    else:
        r.json.return_value = payload
    return r


def _page(rows):
    return {"items": [{"TotalJobsCount": len(rows), "requisitionList": rows}]}


def _row(i, title="Summer Analyst", country="US", **kw):
    return {"Id": str(i), "Title": title, "PostedDate": "2026-07-01",
            "PrimaryLocation": "Columbus, OH, United States",
            "PrimaryLocationCountry": country, "Department": None,
            "Organization": None, "JobFamily": None, "JobFunction": None, **kw}


def _detail(**kw):
    return {"items": [{"Id": "1", "ExternalDescriptionStr": "<p>Hi &amp; bye</p>",
                       "ExternalResponsibilitiesStr": None,
                       "ExternalQualificationsStr": None, **kw}]}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(oracle, "_polite_sleep", lambda: None)
    monkeypatch.setattr(oracle.slug_loader, "get_record", lambda a, s: dict(RECORD))
    sess = MagicMock()
    monkeypatch.setattr(oracle, "_SESSION", sess)
    return sess


def test_non_us_rows_skipped_without_detail(env):
    env.get.side_effect = lambda url, **kw: (
        _resp(_page([_row(1), _row(2, country="GB")])) if "findReqs" in url
        and "keyword=intern," in url else
        _resp(_page([])) if "findReqs" in url else _resp(_detail()))
    jobs = oracle.fetch("jpmorgan")
    detail_calls = [c for c in env.get.call_args_list if "ById" in c.args[0]]
    assert len(detail_calls) == 1 and 'Id="1"' in detail_calls[0].args[0]
    assert [j["job_id"] for j in jobs] == ["direct_oracle_jpmorgan_1"]
    first = env.get.call_args_list[0]
    assert first.args[0] == (
        "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        "?onlyData=true&expand=requisitionList"
        "&finder=findReqs;siteNumber=CX_1001,keyword=intern,limit=25,offset=0")
    keywords = [c.args[0].split("keyword=")[1].split(",")[0]
               for c in env.get.call_args_list if "findReqs" in c.args[0]]
    assert keywords == [t.replace(" ", "%20") for t in TERMS]


def test_detail_maps_fields(env):
    list_rows = LIST["items"][0]["requisitionList"]
    target = next(r for r in list_rows if r["Id"] == "210765547")
    assert any(r["PrimaryLocationCountry"] != "US" for r in list_rows)
    env.get.side_effect = lambda url, **kw: (
        _resp(LIST) if "findReqs" in url and "keyword=intern," in url else
        _resp(_page([])) if "findReqs" in url else _resp(DETAIL))
    jobs = oracle.fetch("jpmorgan")
    job = next(j for j in jobs if j["job_id"] == "direct_oracle_jpmorgan_210765547")
    assert job["title"] == target["Title"]
    assert job["location"] == "Jersey City, NJ, United States"
    assert job["posted_at"] == "2026-07-08"
    assert job["apply_url"] == (
        "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210765547")
    assert job["source"] == job["board_platform"] == "oracle"
    assert job["board_slug"] == "jpmorgan" and job["company"] == "JPMorgan Chase"
    assert job["department"] == "Seasonal Employee"  # list row JobFamily
    assert job["team"] == ""
    assert job["analyst_entry"] is True
    assert job["salary_min"] is None and job["employer_logo"] is None
    assert 0 < len(job["description_raw"]) <= 8000 and "<" not in job["description_raw"]
    assert all(j["location"].endswith("United States") for j in jobs)


def test_html_stripped_before_unescape(env):
    env.get.side_effect = lambda url, **kw: (
        _resp(_page([_row(1)])) if "findReqs" in url and "keyword=intern," in url else
        _resp(_page([])) if "findReqs" in url else
        _resp(_detail(ExternalDescriptionStr="a &lt;b&gt; c <p>x</p>",
                      ExternalResponsibilitiesStr="<li>r</li>")))
    jobs = oracle.fetch("jpmorgan")
    assert "<b>" in jobs[0]["description_raw"]  # literal text survives, tags did not
    assert "<p>" not in jobs[0]["description_raw"]
    assert "x" in jobs[0]["description_raw"] and "r" in jobs[0]["description_raw"]


@pytest.mark.parametrize("fields,expected", [
    ({"Department": "Dept", "Organization": "Org", "JobFamily": "Fam", "JobFunction": "Fn"}, "Dept"),
    ({"Department": "", "Organization": "Org", "JobFamily": "Fam", "JobFunction": "Fn"}, "Org"),
    ({"Department": None, "Organization": None, "JobFamily": "Fam", "JobFunction": "Fn"}, "Fam"),
    ({"Department": None, "Organization": None, "JobFamily": " ", "JobFunction": "Fn"}, "Fn"),
    ({"Department": None, "Organization": None, "JobFamily": None, "JobFunction": None}, ""),
])
def test_department_fallback_order(env, fields, expected):
    env.get.side_effect = lambda url, **kw: (
        _resp(_page([_row(1, **fields)])) if "findReqs" in url and "keyword=intern," in url else
        _resp(_page([])) if "findReqs" in url else _resp(_detail()))
    assert oracle.fetch("jpmorgan")[0]["department"] == expected


def test_paging_stops_short_page(env, caplog):
    full = _page([_row(i) for i in range(25)])
    # first term: 25, 25, then short page of 3
    pages = iter([full, full, _page([_row(100), _row(101), _row(102)])])

    def get2(url, **kw):
        if "ById" in url:
            return _resp(_detail())
        if "keyword=intern," in url:
            return _resp(next(pages))
        return _resp(_page([]))

    env.get.side_effect = get2
    oracle.fetch("jpmorgan")
    offsets = [c.args[0].rsplit("offset=", 1)[1] for c in env.get.call_args_list
               if "keyword=intern," in c.args[0]]
    assert offsets == ["0", "25", "50"]


def test_cap_at_100_warns(env, caplog):
    full = _page([_row(i) for i in range(25)])
    env.get.side_effect = lambda url, **kw: (
        _resp(_detail()) if "ById" in url else
        _resp(full) if "keyword=intern," in url else _resp(_page([])))
    with caplog.at_level("WARNING"):
        oracle.fetch("jpmorgan")
    offsets = [c.args[0].rsplit("offset=", 1)[1] for c in env.get.call_args_list
               if "keyword=intern," in c.args[0]]
    assert offsets == ["0", "25", "50", "75"]
    assert any("cap" in r.message.lower() for r in caplog.records)


def test_any_failure_raises(env):
    env.get.side_effect = None
    env.get.return_value = _resp(status=500)
    with pytest.raises(FetchError):
        oracle.fetch("jpmorgan")

    env.get.return_value = _resp(text="<html>")
    with pytest.raises(FetchError):
        oracle.fetch("jpmorgan")

    env.get.return_value = _resp({"items": []})
    with pytest.raises(FetchError):
        oracle.fetch("jpmorgan")

    env.get.return_value = requests.Timeout("slow")
    env.get.side_effect = requests.Timeout("slow")
    with pytest.raises(FetchError):
        oracle.fetch("jpmorgan")

    # detail failure for one job
    env.get.side_effect = lambda url, **kw: (
        _resp(_page([_row(1)])) if "findReqs" in url and "keyword=intern," in url else
        _resp(_page([])) if "findReqs" in url else _resp(status=404))
    with pytest.raises(FetchError):
        oracle.fetch("jpmorgan")

    env.get.side_effect = lambda url, **kw: (
        _resp(_page([_row(1)])) if "findReqs" in url and "keyword=intern," in url else
        _resp(_page([])) if "findReqs" in url else _resp({"items": []}))
    with pytest.raises(FetchError):
        oracle.fetch("jpmorgan")


def test_unknown_slug_raises(env, monkeypatch):
    monkeypatch.setattr(oracle.slug_loader, "get_record", lambda a, s: None)
    with pytest.raises(FetchError):
        oracle.fetch("nope")
