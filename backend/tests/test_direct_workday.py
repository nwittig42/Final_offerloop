"""Workday direct fetcher: no network; the module session is faked."""
import copy
import json
import random
import threading
import time
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
    """items: (title, path) or (title, path, locationsText)."""
    return {"total": len(items), "jobPostings": [
        {"title": it[0], "externalPath": it[1],
         "locationsText": it[2] if len(it) > 2 else "X"} for it in items]}


def _detail_for(url):
    """DETAIL with a jobReqId taken from the posting path's trailing number."""
    d = copy.deepcopy(DETAIL)
    d["jobPostingInfo"]["jobReqId"] = url.rsplit("_", 1)[-1]
    return d


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
    # The fixture's postings are all non-US by locationsText and would be
    # pre-filtered; mark them ambiguous so the detail mapping is exercised.
    lst = copy.deepcopy(LIST)
    for item in lst["jobPostings"]:
        item["locationsText"] = "2 Locations"
    env.post.side_effect = [_resp(lst)] + [_resp(_page())] * 5
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


def test_list_prefilter_skips_non_us_before_detail(env):
    env.post.side_effect = [_resp(_page(
        ("Summer Analyst", "/job/w/Summer-Analyst_1", "Warsaw, Poland"),
        ("Summer Analyst", "/job/n/Summer-Analyst_2", "New York, NY")))] + [_resp(_page())] * 5
    env.get.side_effect = lambda url, **kw: _resp(_detail_for(url))
    jobs = workday.fetch("citi")
    assert env.get.call_count == 1
    assert env.get.call_args.args[0].endswith("/job/n/Summer-Analyst_2")
    assert [j["job_id"] for j in jobs] == ["direct_workday_citi_2"]


def test_list_prefilter_keeps_ambiguous_locations(env):
    env.post.side_effect = [_resp(_page(
        ("Summer Analyst", "/job/a/Summer-Analyst_1", "2 Locations"),
        ("Summer Analyst", "/job/b/Summer-Analyst_2", "Singapore  Singapore")))] + [_resp(_page())] * 5
    env.get.side_effect = lambda url, **kw: _resp(_detail_for(url))
    workday.fetch("citi")
    assert [c.args[0].rsplit("/", 1)[-1] for c in env.get.call_args_list] == ["Summer-Analyst_1"]


def _three_postings(env):
    env.post.side_effect = [_resp(_page(
        *[(f"Summer Analyst {i}", f"/job/x/Summer-Analyst_{i}") for i in (1, 2, 3)]))] + [_resp(_page())] * 5


@pytest.mark.parametrize("status", [403, 404, 410])
def test_detail_403_skips_posting(env, status, caplog):
    _three_postings(env)
    env.get.side_effect = lambda url, **kw: (
        _resp(status=status) if url.endswith("_2") else _resp(_detail_for(url)))
    jobs = workday.fetch("citi")
    assert [j["job_id"] for j in jobs] == ["direct_workday_citi_1", "direct_workday_citi_3"]
    assert any("skipp" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)


def test_detail_500_still_raises(env):
    _three_postings(env)
    env.get.side_effect = lambda url, **kw: (
        _resp(status=500) if url.endswith("_2") else _resp(_detail_for(url)))
    with pytest.raises(FetchError):
        workday.fetch("citi")


def _ten_postings(env, failing):
    env.post.side_effect = [_resp(_page(
        *[(f"Summer Analyst {i}", f"/job/x/Summer-Analyst_{i}") for i in range(10)]))] + [_resp(_page())] * 5
    env.get.side_effect = lambda url, **kw: (
        _resp(status=404) if int(url.rsplit("_", 1)[-1]) in failing else _resp(_detail_for(url)))


def test_many_detail_failures_raise(env):
    _ten_postings(env, failing={1, 3, 5, 7})
    with pytest.raises(FetchError):
        workday.fetch("citi")


def test_three_detail_failures_tolerated(env):
    # 30% of the board but only 3 postings: the ceiling needs both > 10% and > 3.
    _ten_postings(env, failing={1, 3, 5})
    assert len(workday.fetch("citi")) == 7


def test_details_fetched_concurrently_in_stable_order(env):
    paths = [f"/job/x/Summer-Analyst_{i}" for i in (7, 3, 9, 1, 5, 2, 8, 4)]
    lock = threading.Lock()
    state = {"now": 0, "max": 0}

    def get(url, **kw):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(random.uniform(0, 0.02))
        with lock:
            state["now"] -= 1
        return _resp(_detail_for(url))

    orders = []
    for _ in range(3):
        env.post.side_effect = [_resp(_page(
            *[(f"Summer Analyst {p[-1]}", p) for p in paths]))] + [_resp(_page())] * 5
        env.get.side_effect = get
        orders.append([j["job_id"] for j in workday.fetch("citi")])
    expected = [f"direct_workday_citi_{p.rsplit('_', 1)[-1]}" for p in paths]
    assert orders == [expected] * 3
    assert 2 <= state["max"] <= workday.DETAIL_WORKERS
