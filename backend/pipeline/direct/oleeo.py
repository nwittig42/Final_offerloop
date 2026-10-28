"""Oleeo (tal.net) direct fetcher (public HTML job boards).

Board record URL: the job-board listing page, e.g.
https://evercore.tal.net/vx/mobile-0/appcentre-ext/brand-4/candidate/jobboard/vacancy/1/adv/
Walks up to MAX_PAGES listing pages, keeps vacancies with a candidate title
(events are dropped by is_candidate_title), then fetches each vacancy page.
Hitting the MAX_PAGES cap marks the snapshot partial (`Snapshot.partial`), so
the crawl expires nothing on that board.

A vacancy page returning 403/404/410 skips that vacancy (it is no longer
viewable) unless too many do (`check_skip_ceiling`).

Failure rule (FetchError): transport error, any other non-2xx, a non-HTML response, a
bot-challenge page, a page that is not a tal.net page at all, or a vacancy
page from which no description can be extracted. A genuine tal.net board with
zero vacancy links is NOT an error (boards can be empty).
"""
from __future__ import annotations

import html
import logging
import os
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from backend.pipeline import slug_loader
from backend.pipeline.fetcher import REQUEST_TIMEOUT, USER_AGENT
from backend.pipeline.direct import (
    SKIPPABLE_STATUSES, FetchError, Snapshot, check_skip_ceiling, is_candidate_title,
    make_job_id)

logger = logging.getLogger(__name__)

MAX_PAGES = 5
MIN_DESCRIPTION_CHARS = 100
# From https://bankcampuscareers.tal.net/robots.txt — `Crawl-delay: 10`. The
# same file Disallows ClaudeBot, GPTBot, Bytespider, ImageSiftBot and
# SemrushBot entirely; `*` is allowed on the jobboard path we use, with this
# delay. Overridable for tests.
TAL_NET_CRAWL_DELAY_S = float(os.getenv("OLEEO_CRAWL_DELAY_S", "10"))
_XF_SEGMENT = re.compile(r"/xf-[^/]+(?=/)")
_VACANCY_RE = re.compile(
    r'<a\s[^>]*?href="([^"]*/opp/(\d+)-[^"]*)"[^>]*>(.*?)</a>', re.S | re.I)
_NEXT_RE = re.compile(
    r'class="[^"]*next_links[^"]*"[^>]*>.*?<a\s[^>]*?href="([^"]+)"', re.S | re.I)
_DROP_BLOCKS = re.compile(r"<(script|style|noscript)\b.*?</\1\s*>|<!--.*?-->", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_LABEL_RE = re.compile(r'class="[^"]*hform_lbl_text[^"]*"[^>]*>(.*?)</span>', re.S | re.I)
# Region label values that mean "US" (allowlist): any other Region value
# (EMEA, APAC, Latin America, Middle East, ...) is skipped on purpose.
US_REGIONS = re.compile(r"\b(americas|north america|us|usa|united states)\b", re.I)
# Unambiguously non-US places named in a title. Used twice: as a PRE-FETCH
# skip on the listing title, and as the fallback when a vacancy page carries no
# Region label.
#
# Only unambiguous names belong here, because the pre-fetch skip bypasses the
# authoritative Region check — and dropping a US job is the expensive, silent
# error (cf. OFF-54). Deliberately excluded: "Dublin" (Ohio and California),
# "Mexico" (Mexico, Missouri — and "New Mexico" is a whole US state).
# "Latin America" / "Brazil" / "Japan" are safe as region/country words.
NON_US_TITLE_PLACES = re.compile(
    r"\b(london|tokyo|hong\s+kong|singapore|sydney|paris|frankfurt|mumbai|"
    r"toronto|seoul|shanghai|jakarta|kuala\s+lumpur|stockholm|"
    r"latin\s+america|brazil|japan|canada)\b", re.I)
_LOCATION_KEYS = ("location", "region")
_DEPT_KEYS = ("division", "business area", "business unit", "group", "department")


def _build_session(pool_size: int = 4) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=retry)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html"})
    return session


_SESSION = _build_session(4)


def _text(fragment: str) -> str:
    """Visible text: drop script/style/comments, strip tags, then unescape."""
    stripped = _TAG_RE.sub(" ", _DROP_BLOCKS.sub(" ", fragment))
    return re.sub(r"\s+", " ", html.unescape(stripped).replace("\xa0", " ")).strip()


def _clean_url(url: str) -> str:
    """Drop the session-like `xf-…` path segment."""
    return _XF_SEGMENT.sub("", url)


class _Gone(Exception):
    """A vacancy page answered 403/404/410."""


def _get_html(url: str, what: str, skippable: bool = False) -> str:
    # tal.net's robots.txt declares `Crawl-delay: 10` and hard-blocks
    # ClaudeBot/GPTBot/Bytespider outright, so this is a tenant that means it.
    # fetcher._polite_sleep is 50-300ms jitter, which is two orders of
    # magnitude faster than asked. Honour the declared delay: these boards are
    # tens of vacancies, not thousands, so the wall-clock cost is minutes on a
    # daily crawl and it keeps us off a greylist we would not get back off.
    time.sleep(TAL_NET_CRAWL_DELAY_S)
    try:
        resp = _SESSION.get(url, timeout=REQUEST_TIMEOUT)
    except Exception as exc:
        raise FetchError(f"Oleeo {what} failed: {exc}") from exc
    # A bot challenge is often served with a 403: check for it before
    # treating the status as "posting gone".
    raw = resp.text if isinstance(getattr(resp, "text", None), str) else ""
    if "oleeoProtect" in raw or "Quick Check Needed" in raw:
        raise FetchError(f"Oleeo {what} blocked by a bot challenge")
    if skippable and resp.status_code in SKIPPABLE_STATUSES:
        raise _Gone(resp.status_code)
    try:
        resp.raise_for_status()
        ctype = (resp.headers.get("Content-Type") or "").lower()
        body = resp.text
    except Exception as exc:
        raise FetchError(f"Oleeo {what} failed: {exc}") from exc
    if "html" not in ctype or not isinstance(body, str) or "<" not in body:
        raise FetchError(f"Oleeo {what} returned non-HTML content ({ctype or 'no type'})")
    if "oleeoProtect" in body or "Quick Check Needed" in body:
        raise FetchError(f"Oleeo {what} blocked by a bot challenge")
    if "wcn-page-config" not in body:
        raise FetchError(f"Oleeo {what} is not a tal.net page")
    return body


def _vacancies(page: str, base: str, host: str) -> list[tuple[str, str, str]]:
    """(opp id, clean url, title) for each vacancy anchor on a board page."""
    out = []
    for href, opp, inner in _VACANCY_RE.findall(page):
        url = _clean_url(urljoin(base, html.unescape(href)))
        if (urlparse(url).hostname or "").lower() != host:
            continue
        title = _text(inner)
        if title:
            out.append((opp, url, title))
    return out


def _label_values(main: str) -> dict[str, str]:
    """Label -> value text; a value runs from its label to the next label."""
    labels = list(_LABEL_RE.finditer(main))
    pairs: dict[str, str] = {}
    for i, m in enumerate(labels):
        end = len(main)
        if i + 1 < len(labels):  # stop before the next label's opening tag
            end = max(main.rfind("<", 0, labels[i + 1].start()), m.end())
        key = re.sub(r"\(s\)$", "", _text(m.group(1)).lower()).strip()
        val = _text(main[m.end():end])
        if key and val and key not in pairs:
            pairs[key] = val
    return pairs


def _first(pairs: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        if pairs.get(key):
            return pairs[key]
    return ""


def _parse_vacancy(page: str, what: str) -> tuple[str, str, str, str]:
    """(description, location, department, region) from a vacancy page."""
    start = page.find('id="vac_desc"')
    if start < 0:
        start = page.find('role="main"')
    if start < 0:
        raise FetchError(f"Oleeo {what} has no main content")
    end = page.find('id="branding-footer"', start)
    start = max(page.rfind("<", 0, start), 0)
    main = page[start:end if end > start else len(page)]
    description = _text(main)
    if len(description) < MIN_DESCRIPTION_CHARS:
        raise FetchError(f"Oleeo {what} has no extractable description")
    pairs = _label_values(main)
    return (description[:8000], _first(pairs, _LOCATION_KEYS), _first(pairs, _DEPT_KEYS),
            pairs.get("region", ""))


def _is_non_us(region: str, title: str) -> bool:
    if region.strip():
        return not US_REGIONS.search(region)
    return bool(NON_US_TITLE_PLACES.search(title))


def fetch(slug: str) -> list[dict]:
    rec = slug_loader.get_record("oleeo", slug)
    if not rec:
        raise FetchError(f"no oleeo record for slug {slug!r}")
    board_url = _clean_url(rec.get("url") or "")
    host = (urlparse(board_url).hostname or "").lower()
    if not host.endswith(".tal.net"):
        raise FetchError(f"unparseable Oleeo url: {board_url!r}")
    analyst_entry = bool(rec.get("analyst_entry"))

    found: dict[str, tuple[str, str]] = {}  # opp id -> (url, title), first seen wins
    prefiltered: set[str] = set()  # opp ids skipped as non-US before any vacancy fetch
    page_url: str | None = board_url
    partial = False
    for page_no in range(MAX_PAGES):
        page = _get_html(page_url, f"[{slug}] board page {page_no + 1}")
        for opp, url, title in _vacancies(page, page_url, host):
            if opp in found or not is_candidate_title(title, analyst_entry):
                continue
            # Skip titles that name a foreign city before paying for the
            # vacancy page. At the 10s crawl delay each skipped fetch is 10
            # seconds, and bofa-campus is currently 37 vacancies of which
            # every single one is non-US — over six minutes of the daily
            # crawl spent to return nothing. The Region label on the page
            # remains the authoritative check for everything else.
            if NON_US_TITLE_PLACES.search(title):
                # Record the id, not a counter: these boards list each vacancy
                # twice (desktop + mobile markup) and a skipped opp never
                # enters `found`, so a bare counter double-counts every one.
                prefiltered.add(opp)
                continue
            found[opp] = (url, title)
        nxt = _NEXT_RE.search(page)
        page_url = None
        if nxt:
            cand = _clean_url(urljoin(board_url, html.unescape(nxt.group(1))))
            if (urlparse(cand).hostname or "").lower() == host:
                page_url = cand
        if not page_url:
            break
    else:
        partial = True
        logger.warning("Oleeo [%s] hit the %d-page cap; snapshot is partial, "
                       "nothing will be expired", slug, MAX_PAGES)

    jobs: list[dict] = []
    skipped = 0
    gone = 0
    for opp, (url, title) in found.items():
        try:
            page = _get_html(url, f"[{slug}] vacancy {opp}", skippable=True)
        except _Gone as exc:
            gone += 1
            logger.warning("Oleeo [%s] vacancy %s returned %s; skipping posting", slug, opp, exc)
            check_skip_ceiling(f"Oleeo [{slug}]", gone, len(found))
            continue
        description, location, department, region = _parse_vacancy(
            page, f"[{slug}] vacancy {opp}")
        if _is_non_us(region, title):
            skipped += 1
            continue
        try:
            job_id = make_job_id("oleeo", slug, opp)
        except ValueError as exc:
            raise FetchError(str(exc)) from exc
        jobs.append({
            "job_id": job_id,
            "source": "oleeo",
            "board_platform": "oleeo",
            "board_slug": slug,
            "title": title,
            "company": rec["name"],
            "employer_logo": None,
            "location": location,
            "remote": "remote" in location.lower(),
            "description_raw": description,
            "apply_url": url,
            "posted_at": None,
            "salary_min": None,
            "salary_max": None,
            "salary_period": None,
            "department": department,
            "team": "",
            "analyst_entry": analyst_entry,
        })

    # 0 jobs is a legitimate steady state here, not a breakage. BofA's US
    # campus postings are seasonal: the 2027 US summer-analyst deadlines close
    # around Aug-Sept, so from autumn the board carries international roles
    # (Paris, Seoul, Tokyo, Brazil) plus US *events* until the next cycle
    # opens. Checked 2026-10-22: 37 vacancies, every one non-US. Do not
    # "fix" this by repointing the URL — vacancy/2 is the events board
    # (information sessions, careers roadshows) and vacancy/3 is empty, so
    # vacancy/1 is correct. All brand-N values serve the same board.
    logger.info("  Oleeo [%s] -> %d jobs (%d non-US by title pre-fetch, "
                "%d non-US by region, %d gone)%s",
                slug, len(jobs), len(prefiltered), skipped, gone,
                " [partial]" if partial else "")
    return Snapshot(jobs, partial=partial)
