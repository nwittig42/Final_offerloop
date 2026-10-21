"""Workday direct fetcher: no network; the module session is faked."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from backend.pipeline.direct import FetchError, workday

FIX = Path(__file__).parent / "fixtures" / "direct"
LIST = json.loads((FIX / "workday_list.json").read_text())
DETAIL = json.loads((FIX / "workday_detail.json").read_text())
TERMS = ("intern", "internship", "analyst", "new grad", "early career", "summer")
RECORD = {"name": "Citi", "slug": "citi", "analyst_entry": True,
          "url": "https://citi.wd5.myworkdayjobs.com/2"}


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


def _page(*items):
    return {"total": len(items), "jobPostings": [
        {"title": t, "externalPath": p, "locationsText": "X"} for t, p in items]}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(workday, "_polite_sleep", lambda: None)
    monkeypatch.setattr(workday.slug_loader, "get_record", lambda a, s: dict(RECORD))
    sess = MagicMock()
    monkeypatch.setattr(workday, "_SESSION", sess)
    return sess


def test_searches_each_term(env):
    env.post.return_value = _resp(_page())
    assert workday.fetch("citi") == []
    assert env.post.call_count == 6
    for call, term in zip(env.post.call_args_list, TERMS):
        assert call.args[0] == "https://citi.wd5.myworkdayjobs.com/wday/cxs/citi/2/jobs"
        assert call.kwargs["json"] == {
            "appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term}


def test_locale_segment_ignored(env, monkeypatch):
    rec = dict(RECORD, slug="pwc", url="https://pwc.wd3.myworkdayjobs.com/en-US/Global_Campus_Careers")
    monkeypatch.setattr(workday.slug_loader, "get_record", lambda a, s: dict(rec))
    env.post.return_value = _resp(_page())
    workday.fetch("pwc")
    assert env.post.call_args_list[0].args[0] == (
        "https://pwc.wd3.myworkdayjobs.com/wday/cxs/pwc/Global_Campus_Careers/jobs")


def test_filters_titles_before_detail(env):
    env.post.side_effect = [_resp(_page(
        ("Vice President - Treasury", "/job/a/VP_1"),
        ("2027 Summer Analyst", "/job/b/Summer-Analyst_2")))] + [_resp(_page())] * 5
    env.get.return_value = _resp(DETAIL)
    jobs = workday.fetch("citi")
    assert env.get.call_count == 1
    assert env.get.call_args.args[0].endswith("/wday/cxs/citi/2/job/b/Summer-Analyst_2")
    assert len(jobs) == 1


def test_dedupes_external_path(env):
    env.post.return_value = _resp(_page(("2027 Summer Analyst", "/job/b/S_2")))
    env.get.return_value = _resp(DETAIL)
    assert len(workday.fetch("citi")) == 1
    assert env.get.call_count == 1


def test_pages_until_short(env):
    full = _page(*[(f"Summer Analyst {i}", f"/job/x/{i}") for i in range(20)])
    env.post.side_effect = [_resp(full), _resp(_page())] + [_resp(_page())] * 5
    env.get.return_value = _resp(DETAIL)
    workday.fetch("citi")
    offsets = [c.kwargs["json"]["offset"] for c in env.post.call_args_list[:2]]
    assert offsets == [0, 20]


def test_detail_maps_fields(env):
    env.post.side_effect = [_resp(LIST)] + [_resp(_page())] * 5
    env.get.return_value = _resp(DETAIL)
    jobs = workday.fetch("citi")
    info = DETAIL["jobPostingInfo"]
    job = next(j for j in jobs if j["job_id"] == "direct_workday_citi_26978591")
    assert job["location"].endswith(", Singapore")
    assert job["posted_at"] == "2026-07-23"
    assert job["apply_url"] == info["externalUrl"]
    assert job["source"] == job["board_platform"] == "workday"
    assert job["board_slug"] == "citi"
    assert job["company"] == "Citi"
    assert job["department"] == job["team"] == ""
    assert job["salary_min"] is None and job["employer_logo"] is None
    assert 0 < len(job["description_raw"]) <= 8000 and "<" not in job["description_raw"]


def test_any_failure_raises(env):
    env.post.return_value = _resp(status=500)
    with pytest.raises(FetchError):
        workday.fetch("citi")

    env.post.return_value = _resp(text="<html>")
    with pytest.raises(FetchError):
        workday.fetch("citi")

    env.post.return_value = _resp(_page(("Summer Analyst", "/job/b/S_2")))
    env.get.side_effect = requests.Timeout("slow")
    with pytest.raises(FetchError):
        workday.fetch("citi")


def test_unknown_slug_raises(env, monkeypatch):
    monkeypatch.setattr(workday.slug_loader, "get_record", lambda a, s: None)
    with pytest.raises(FetchError):
        workday.fetch("nope")
