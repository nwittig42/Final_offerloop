"""Goldman Sachs direct fetcher: no network; the module session is faked."""
import copy
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from backend.pipeline.direct import FetchError, goldman

FIX = Path(__file__).parent / "fixtures" / "direct"
PAGE = json.loads((FIX / "goldman_campus.json").read_text())
RECORD = {"name": "Goldman Sachs", "slug": "goldman-sachs", "analyst_entry": True,
          "url": "https://higher.gs.com/results"}


def _resp(payload=None, status=200):
    r = MagicMock()
    r.status_code = status
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(str(status))
    r.json.return_value = payload
    return r


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(goldman, "_polite_sleep", lambda: None)
    monkeypatch.setattr(goldman.slug_loader, "get_record", lambda a, s: dict(RECORD))
    sess = MagicMock()
    monkeypatch.setattr(goldman, "_SESSION", sess)
    return sess


def test_keeps_us_campus_roles_and_maps_fields(env):
    env.post.return_value = _resp(copy.deepcopy(PAGE))
    jobs = goldman.fetch("goldman-sachs")
    assert [j["job_id"] for j in jobs] == [
        "direct_goldman_goldman-sachs_170696",
        "direct_goldman_goldman-sachs_170705",
        "direct_goldman_goldman-sachs_170707"]  # Warsaw / Frankfurt dropped
    j = jobs[0]
    assert j["source"] == "goldman" and j["board_platform"] == "goldman"
    assert j["location"] == "New York, NY, United States"
    assert j["apply_url"] == "https://higher.gs.com/roles/170696"
    assert j["title"].endswith("Summer Analyst") and j["company"] == "Goldman Sachs"
    assert j["posted_at"] == "2026-10-21T17:00:16.804Z"
    assert j["analyst_entry"] is True and j["department"] == "Executive Office Division"
    assert "About the program" in j["description_raw"] and "<p>" not in j["description_raw"]
    body = env.post.call_args.kwargs["json"]
    assert body["variables"]["searchQueryInput"]["experiences"] == ["CAMPUS"]
    assert env.post.call_args.args[0] == "https://api-higher.gs.com/gateway/api/v1/graphql"


def test_paginates_until_total(env, monkeypatch):
    monkeypatch.setattr(goldman, "PAGE_SIZE", 3)
    items = PAGE["data"]["roleSearch"]["items"]
    pages = [{"data": {"roleSearch": {"totalCount": 5, "items": items[:3]}}},
             {"data": {"roleSearch": {"totalCount": 5, "items": items[3:]}}}]
    env.post.side_effect = [_resp(p) for p in pages]
    assert len(goldman.fetch("goldman-sachs")) == 3
    nums = [c.kwargs["json"]["variables"]["searchQueryInput"]["page"]["pageNumber"]
            for c in env.post.call_args_list]
    assert nums == [0, 1]


def test_short_listing_raises(env):
    bad = copy.deepcopy(PAGE)
    bad["data"]["roleSearch"]["totalCount"] = 9
    bad["data"]["roleSearch"]["items"] = bad["data"]["roleSearch"]["items"][:2]
    env.post.side_effect = [_resp(bad), _resp({"data": {"roleSearch": {"totalCount": 9, "items": []}}})]
    with pytest.raises(FetchError, match="2 of 9"):
        goldman.fetch("goldman-sachs")


@pytest.mark.parametrize("resp", [
    _resp(status=500),
    _resp({"errors": [{"message": "boom"}]}),
    _resp({"data": None}),
])
def test_failures_raise(env, resp):
    env.post.return_value = resp
    with pytest.raises(FetchError):
        goldman.fetch("goldman-sachs")


def test_network_error_raises(env):
    env.post.side_effect = requests.ConnectionError("down")
    with pytest.raises(FetchError):
        goldman.fetch("goldman-sachs")


def test_event_titles_dropped(env):
    data = copy.deepcopy(PAGE)
    data["data"]["roleSearch"]["items"][0]["jobTitle"] = "2027 | Americas | New York | Info Session | Summer Analyst"
    env.post.return_value = _resp(data)
    ids = [j["job_id"] for j in goldman.fetch("goldman-sachs")]
    assert "direct_goldman_goldman-sachs_170696" not in ids and len(ids) == 2


def test_unknown_slug_raises(monkeypatch):
    monkeypatch.setattr(goldman.slug_loader, "get_record", lambda a, s: None)
    with pytest.raises(FetchError):
        goldman.fetch("nope")


def test_real_csv_row_loads():
    from backend.pipeline import slug_loader
    slug_loader.clear_cache()
    rec = slug_loader.get_record("goldman", "goldman-sachs")
    assert rec and rec["name"] == "Goldman Sachs" and rec["analyst_entry"] in ("1", 1, True)
    assert "goldman-sachs" in slug_loader.load_slugs("goldman", "hot")
