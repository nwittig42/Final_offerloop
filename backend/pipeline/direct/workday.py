"""Workday direct fetcher (public CXS JSON API).

Board record URL: https://{tenant}.{wdN}.myworkdayjobs.com[/{locale}]/{site}.
Searches each early-career term, drops non-candidate titles and postings whose
list `locationsText` is clearly non-US, then fetches the detail for each
survivor (DETAIL_WORKERS at a time, results in list order). A detail call
returning 403/404/410 skips that posting (it is no longer viewable) unless
too many do (`check_skip_ceiling`); any other failure raises FetchError.
"""
from __future__ import annotations

import html
import logging
import re
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from backend.pipeline import slug_loader
from backend.pipeline.fetcher import (
    POOL_SIZE, REQUEST_TIMEOUT, USER_AGENT, _polite_sleep, _strip_html)
from backend.pipeline.direct import (
    DETAIL_WORKERS, SKIPPABLE_STATUSES, FetchError, check_skip_ceiling, is_candidate_title,
    make_job_id, map_in_order)
from backend.pipeline.normalizer import _is_non_us_non_remote

logger = logging.getLogger(__name__)

SEARCH_TERMS = ("intern", "internship", "analyst", "new grad", "early career", "summer")
PAGE_SIZE = 20
MAX_PER_TERM = 100
_LOCALE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
_BAD_ID_CHARS = re.compile(r"[^A-Za-z0-9-]")


def _build_session(pool_size: int = 4, hosts: int = 4) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503),
        allowed_methods=frozenset(["GET", "POST"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(pool_connections=hosts, pool_maxsize=pool_size, max_retries=retry)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


# Shared by every board thread: up to BOARDS boards (one host each) with
# DETAIL_WORKERS detail calls in flight per board. Size each host's pool for
# all of them in case two boards share a tenant host.
_BOARDS = POOL_SIZE.get("workday", 4)
_SESSION = _build_session(pool_size=_BOARDS * DETAIL_WORKERS, hosts=_BOARDS * 2)


def _parse_board_url(url: str) -> tuple[str, str, str]:
    """(host, tenant, site) from a record URL, ignoring an optional locale."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    if parts and _LOCALE_RE.match(parts[0]):
        parts = parts[1:]
    if not host.endswith(".myworkdayjobs.com") or not parts:
        raise FetchError(f"unparseable Workday url: {url!r}")
    return host, host.split(".")[0], parts[0]


def _json(resp) -> dict:
    try:
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise FetchError(f"Workday request failed: {exc}") from exc
    if not isinstance(data, dict):
        raise FetchError("Workday returned unexpected JSON")
    return data


def fetch(slug: str) -> list[dict]:
    rec = slug_loader.get_record("workday", slug)
    if not rec:
        raise FetchError(f"no workday record for slug {slug!r}")
    host, tenant, site = _parse_board_url(rec["url"])
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    analyst_entry = bool(rec.get("analyst_entry"))

    paths: dict[str, str] = {}  # externalPath -> title, first seen wins
    non_us: set[str] = set()
    for term in SEARCH_TERMS:
        offset = 0
        while offset < MAX_PER_TERM:
            _polite_sleep()
            try:
                resp = _SESSION.post(
                    f"{base}/jobs",
                    json={"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": term},
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as exc:
                raise FetchError(f"Workday [{slug}] list failed: {exc}") from exc
            items = _json(resp).get("jobPostings") or []
            for item in items:
                path, title = item.get("externalPath"), item.get("title") or ""
                if not path or path in paths or not is_candidate_title(title, analyst_entry):
                    continue
                # Clearly non-US by the list's location text: no detail call.
                # Ambiguous text ("2 Locations") goes on to the detail step.
                if _is_non_us_non_remote({"location": item.get("locationsText") or ""}):
                    non_us.add(path)
                    continue
                paths[path] = title
            if len(items) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

    def detail(path: str) -> dict | None:
        """jobPostingInfo for one posting, or None when it 403/404/410s."""
        _polite_sleep()
        try:
            resp = _SESSION.get(f"{base}{path}", timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise FetchError(f"Workday [{slug}] detail {path} failed: {exc}") from exc
        if resp.status_code in SKIPPABLE_STATUSES:
            logger.warning("Workday [%s] detail %s returned %s; skipping posting",
                           slug, path, resp.status_code)
            return None
        info = _json(resp).get("jobPostingInfo")
        if not isinstance(info, dict) or not info.get("jobReqId"):
            raise FetchError(f"Workday [{slug}] detail {path} missing jobPostingInfo")
        return info

    path_list = list(paths)
    infos = map_in_order(detail, path_list)
    skipped = sum(info is None for info in infos)
    check_skip_ceiling(f"Workday [{slug}]", skipped, len(path_list))

    jobs: list[dict] = []
    seen_ids: set[str] = set()
    for path, info in zip(path_list, infos):
        if info is None:
            continue
        list_title = paths[path]
        ext_id = _BAD_ID_CHARS.sub("-", str(info["jobReqId"]))
        try:
            job_id = make_job_id("workday", slug, ext_id)
        except ValueError as exc:
            raise FetchError(str(exc)) from exc
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)

        country = ((info.get("country") or {}).get("descriptor") or "").strip()
        loc = (info.get("location") or "").strip()
        location = f"{loc}, {country}" if loc and country else (loc or country)
        jobs.append({
            "job_id": job_id,
            "source": "workday",
            "board_platform": "workday",
            "board_slug": slug,
            "title": info.get("title") or list_title,
            "company": rec["name"],
            "employer_logo": None,
            "location": location,
            "remote": "remote" in location.lower(),
            "description_raw": _strip_html(html.unescape(info.get("jobDescription") or ""))[:8000],
            "apply_url": info.get("externalUrl", ""),
            "posted_at": info.get("startDate"),
            "salary_min": None,
            "salary_max": None,
            "salary_period": None,
            "department": "",
            "team": "",
            "analyst_entry": analyst_entry,
        })

    logger.info("  Workday [%s] -> %d jobs (%d non-US skipped from list, %d gone)",
                slug, len(jobs), len(non_us), skipped)
    return jobs
