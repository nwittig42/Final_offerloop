"""Oleeo (tal.net) direct fetcher: no network; the module session is faked."""
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from backend.pipeline.direct import FetchError, oleeo

FIX = Path(__file__).parent / "fixtures" / "direct"
BOARD = (FIX / "oleeo_board_jefferies.html").read_text()
CAPTCHA = (FIX / "oleeo_board_evercore.html").read_text()  # real bot-challenge response
VACANCY = (FIX / "oleeo_vacancy_jefferies.html").read_text()
# Real BofA campus boards (2026-10-21): vacancy/1 "Global Programs", vacancy/2 "Campus Events".
BOFA_PROGRAMS = (FIX / "oleeo_board_bofa_campus_programs.html").read_text()
BOFA_EVENTS = (FIX / "oleeo_board_bofa_campus_events.html").read_text()
RECORD = {"name": "Jefferies", "slug": "jefferies", "analyst_entry": True,
          "url": "https://jefferies.tal.net/vx/lang-en-GB/mobile-0/appcentre-ext/brand-4/candidate/jobboard/vacancy/2/adv/"}
HOST = "https://jefferies.tal.net/vx/lang-en-GB/mobile-0/appcentre-1/brand-4/candidate/so/pm/1/pl/2/opp"


def _resp(text, status=200, ctype="text/html; charset=utf-8"):
    r = MagicMock()
    r.status_code = status
    r.text = text
    r.headers = {"Content-Type": ctype}
    if status >= 400:
        r.raise_for_status.side_effect = requests.HTTPError(str(status))
    return r


def _board(titles, next_href=None):
    links = "".join(
        f'<a class="subject" href="{HOST}/{i}-{t.replace(" ", "-")}/en-GB">\n  {t}  </a>'
        for i, t in titles)
    nxt = f'<span class="next_links"><a href="{next_href}">Next page</a></span>' if next_href else ""
    return f'<html><script id="wcn-page-config">{{}}</script>{links}{nxt}</html>'


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(oleeo, "_polite_sleep", lambda: None)
    monkeypatch.setattr(oleeo.slug_loader, "get_record", lambda a, s: dict(RECORD))
    sess = MagicMock()
    monkeypatch.setattr(oleeo, "_SESSION", sess)
    return sess


def _serve(env, board_pages, vacancy=VACANCY):
    pages = iter(board_pages)

    def get(url, **kw):
        if "/opp/" in url:
            return _resp(vacancy)
        return _resp(next(pages))
    env.get.side_effect = get


def test_board_links_and_titles(env):
    _serve(env, [BOARD, _board([])])
    jobs = oleeo.fetch("jefferies")
    # 50 anchors on the real board, 45 survive the candidate-title filter
    assert len(jobs) == 45
    assert jobs[0]["title"] == "2027 Corporate Technology Summer Internship – New York and Jersey City"
    assert jobs[0]["job_id"] == "direct_oleeo_jefferies_1991"
    assert jobs[0]["apply_url"].endswith("/opp/1991-2027-Corporate-Technology-Summer-Internship-New-York-and-Jersey-City/en-GB")
    assert jobs[0]["source"] == "oleeo" and jobs[0]["posted_at"] is None
    assert jobs[0]["analyst_entry"] is True
    assert "Class of 2029: Investment Banking Insight Day" not in {j["title"] for j in jobs}


def test_events_dropped(env):
    page = _board([(1, "Coffee Chat with Evercore"), (2, "Virtual Event: Women in Finance"),
                   (3, "Information Session"), (4, "Summer Analyst Program")])
    _serve(env, [page])
    jobs = oleeo.fetch("jefferies")
    assert [j["title"] for j in jobs] == ["Summer Analyst Program"]
    assert not any("Coffee Chat" in j["title"] for j in jobs)
    # only the survivor's vacancy page was fetched
    assert sum("/opp/" in c.args[0] for c in env.get.call_args_list) == 1


def test_vacancy_fields(env):
    _serve(env, [_board([(1991, "Summer Analyst Program")])])
    job = oleeo.fetch("jefferies")[0]
    assert job["location"] == "Jersey City, New York"
    assert job["department"] == "Corporate"
    assert "generated inline style" not in job["description_raw"]
    assert "<" not in job["description_raw"] and len(job["description_raw"]) <= 8000
    assert "Jefferies" in job["description_raw"]


def test_location_falls_back_to_region_and_department_order():
    html = ('<div id="vac_desc"><p>' + "x" * 150 + '</p><span class="hform_lbl_text">Region</span><div class="form_value">EMEA</div>'
            '<span class="hform_lbl_text">Group</span><div class="form_value">Banking</div>'
            '<span class="hform_lbl_text">Division</span><div class="form_value">IBD</div>'
            '</div>')
    desc, loc, dept, region = oleeo._parse_vacancy(html, "t")
    assert (loc, dept, region) == ("EMEA", "IBD", "EMEA")


def _vac_with_region(region):
    return VACANCY.replace("\nAmericas\n", f"\n{region}\n")


@pytest.mark.parametrize("region", ["EMEA", "APAC"])
def test_non_us_region_vacancy_dropped(env, region):
    _serve(env, [_board([(1, "Summer Analyst Program London")])], vacancy=_vac_with_region(region))
    assert oleeo.fetch("jefferies") == []


def test_americas_vacancy_kept(env):
    _serve(env, [_board([(1991, "Summer Analyst Program")])])
    assert [j["job_id"] for j in oleeo.fetch("jefferies")] == ["direct_oleeo_jefferies_1991"]


def test_region_missing_falls_back_to_title_place(env):
    no_region = VACANCY.replace("Region", "Territory")
    _serve(env, [_board([(1, "Summer Analyst Program London"), (2, "Summer Analyst Program NYC")])],
           vacancy=no_region)
    assert [j["job_id"] for j in oleeo.fetch("jefferies")] == ["direct_oleeo_jefferies_2"]


def test_pagination_cap(env):
    env.get.side_effect = lambda url, **kw: (
        _resp(VACANCY) if "/opp/" in url else _resp(_board([], next_href="?start=50")))
    assert oleeo.fetch("jefferies") == []
    board_gets = [c.args[0] for c in env.get.call_args_list]
    assert len(board_gets) == 5
    assert board_gets[1].endswith("/vacancy/2/adv/?start=50")


def test_next_link_on_other_host_not_followed(env):
    _serve(env, [_board([], next_href="https://evil.example.com/?start=50")])
    oleeo.fetch("jefferies")
    assert env.get.call_count == 1


def test_empty_board_is_not_an_error(env):
    _serve(env, [_board([])])
    assert oleeo.fetch("jefferies") == []


def test_xf_segment_stripped_from_board_url(env, monkeypatch):
    rec = dict(RECORD, url=RECORD["url"].replace("/vx/", "/vx/xf-abc123/"))
    monkeypatch.setattr(oleeo.slug_loader, "get_record", lambda a, s: dict(rec))
    _serve(env, [_board([])])
    oleeo.fetch("jefferies")
    assert env.get.call_args.args[0] == RECORD["url"]


@pytest.mark.parametrize("case", [
    "no_record", "transport", "http_500", "not_html", "captcha", "not_tal_page",
    "vacancy_no_description", "vacancy_http_error", "bad_url"])
def test_any_failure_raises(env, monkeypatch, case):
    board = _board([(1, "Summer Analyst Program")])
    if case == "no_record":
        monkeypatch.setattr(oleeo.slug_loader, "get_record", lambda a, s: None)
    elif case == "bad_url":
        monkeypatch.setattr(oleeo.slug_loader, "get_record",
                            lambda a, s: dict(RECORD, url="https://example.com/x"))
    elif case == "transport":
        env.get.side_effect = requests.ConnectionError("boom")
    elif case == "http_500":
        env.get.side_effect = lambda url, **kw: _resp("err", status=500)
    elif case == "not_html":
        env.get.side_effect = lambda url, **kw: _resp('{"a": 1}', ctype="application/json")
    elif case == "captcha":
        env.get.side_effect = lambda url, **kw: _resp(CAPTCHA)
    elif case == "not_tal_page":
        env.get.side_effect = lambda url, **kw: _resp("<html><body>hello</body></html>")
    elif case == "vacancy_no_description":
        _serve(env, [board], vacancy='<html><script id="wcn-page-config"></script><body>Oops</body></html>')
    elif case == "vacancy_http_error":
        env.get.side_effect = lambda url, **kw: (
            _resp("x", status=500) if "/opp/" in url else _resp(board))
    with pytest.raises(FetchError):
        oleeo.fetch("jefferies")


@pytest.mark.parametrize("status", [403, 404, 410])
def test_vacancy_404_skips_posting(env, status, caplog):
    board = _board([(1, "Summer Analyst Program A"), (2, "Summer Analyst Program B"),
                    (3, "Summer Analyst Program C")])
    env.get.side_effect = lambda url, **kw: (
        _resp("gone", status=status) if "/opp/2-" in url else
        _resp(VACANCY) if "/opp/" in url else _resp(board))
    jobs = oleeo.fetch("jefferies")
    assert [j["job_id"] for j in jobs] == ["direct_oleeo_jefferies_1", "direct_oleeo_jefferies_3"]
    assert any("skipp" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)


def test_many_vacancy_failures_raise(env):
    board = _board([(i, f"Summer Analyst Program {i}") for i in range(10)])
    env.get.side_effect = lambda url, **kw: (
        _resp("gone", status=404) if any(f"/opp/{i}-" in url for i in (1, 3, 5, 7)) else
        _resp(VACANCY) if "/opp/" in url else _resp(board))
    with pytest.raises(FetchError):
        oleeo.fetch("jefferies")


def test_board_page_404_still_raises(env):
    env.get.side_effect = lambda url, **kw: _resp("gone", status=404)
    with pytest.raises(FetchError):
        oleeo.fetch("jefferies")


BOFA_PROGRAMS_URL = ("https://bankcampuscareers.tal.net/vx/lang-en-GB/mobile-0/brand-4/"
                     "candidate/jobboard/vacancy/1/adv/")


def test_bofa_campus_events_board_has_no_candidates(env, monkeypatch):
    """vacancy/2 (the old CSV URL) is BofA's "Campus Events" board: only info
    sessions and roadshows, so 0 jobs was the correct reading of it."""
    rec = {"name": "Bank of America", "slug": "bofa-campus", "analyst_entry": True,
           "url": BOFA_PROGRAMS_URL.replace("/vacancy/1/", "/vacancy/2/")}
    monkeypatch.setattr(oleeo.slug_loader, "get_record", lambda a, s: dict(rec))
    _serve(env, [BOFA_EVENTS])
    assert "<title>Campus Events" in BOFA_EVENTS
    assert oleeo.fetch("bofa-campus") == []
    assert not any("/opp/" in c.args[0] for c in env.get.call_args_list)


def test_bofa_campus_programs_board(env, monkeypatch):
    rec = {"name": "Bank of America", "slug": "bofa-campus", "analyst_entry": True,
           "url": BOFA_PROGRAMS_URL}
    monkeypatch.setattr(oleeo.slug_loader, "get_record", lambda a, s: dict(rec))
    _serve(env, [BOFA_PROGRAMS])  # Jefferies vacancy page (Region: Americas) stands in for each opp
    jobs = oleeo.fetch("bofa-campus")
    titles = {j["title"] for j in jobs}
    # 46 programs on the board, 43 candidate titles (non-US ones are dropped
    # later by each real vacancy's Region label, faked here as Americas)
    assert len(jobs) == 43
    assert "Global Investment Banking Summer Associate Program - 2027" in titles
    assert all(j["job_id"].startswith("direct_oleeo_bofa-campus_") for j in jobs)


def test_bofa_campus_csv_points_at_programs_board():
    from backend.pipeline import slug_loader
    rec = slug_loader.get_record("oleeo", "bofa-campus")
    assert rec["url"] == BOFA_PROGRAMS_URL
