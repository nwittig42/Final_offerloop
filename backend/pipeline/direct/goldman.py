"""Goldman Sachs direct fetcher (public GraphQL behind higher.gs.com).

One POST query lists every CAMPUS-experience role (summer analyst / new
analyst / summer associate programs) with its description, so there are no
per-posting detail calls. Keeps US roles with a candidate title. Any failure,
or a page set that doesn't add up to the reported totalCount, raises
FetchError; a partial list is never returned.
"""
from __future__ import annotations

import html
import logging
import re

import requests
from requests.adapters import HTTPAdapter

from backend.pipeline import slug_loader
from backend.pipeline.fetcher import (
    POOL_SIZE, REQUEST_TIMEOUT, USER_AGENT, _polite_sleep, _strip_html)
from backend.pipeline.direct import FetchError, is_candidate_title, make_job_id

logger = logging.getLogger(__name__)

API_URL = "https://api-higher.gs.com/gateway/api/v1/graphql"
ROLE_URL = "https://higher.gs.com/roles/{}"
EXPERIENCES = ["CAMPUS"]
PAGE_SIZE = 100
MAX_PAGES = 20
_BAD_ID_CHARS = re.compile(r"[^A-Za-z0-9-]")

QUERY = (
    "query GetRoles($searchQueryInput: RoleSearchQueryInput!) {"
    " roleSearch(searchQueryInput: $searchQueryInput) { totalCount items {"
    " roleId corporateTitle jobTitle division descriptionHtml lastPostedDate"
    " externalSource { sourceId } locations { primary city state country } } } }"
)


def _build_session() -> requests.Session:
    session = requests.Session()
    session.mount("https://", HTTPAdapter(pool_connections=1, pool_maxsize=POOL_SIZE.get("goldman", 4)))
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Origin": "https://higher.gs.com",
    })
    return session


_SESSION = _build_session()


def _page(page_number: int) -> dict:
    body = {
        "operationName": "GetRoles",
        "query": QUERY,
        "variables": {"searchQueryInput": {
            "page": {"pageSize": PAGE_SIZE, "pageNumber": page_number},
            "sort": {"sortStrategy": "POSTED_DATE", "sortOrder": "DESC"},
            "filters": [],
            "experiences": EXPERIENCES,
            "searchTerm": "",
        }},
    }
    _polite_sleep()
    try:
        resp = _SESSION.post(API_URL, json=body, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise FetchError(f"Goldman request failed: {exc}") from exc
    if not isinstance(data, dict) or data.get("errors"):
        raise FetchError(f"Goldman GraphQL error: {str((data or {}).get('errors'))[:200]}")
    try:
        result = data["data"]["roleSearch"]
        total, items = result["totalCount"], result["items"]
    except (KeyError, TypeError) as exc:
        raise FetchError("Goldman returned unexpected JSON") from exc
    if not isinstance(items, list) or not isinstance(total, int):
        raise FetchError("Goldman returned unexpected JSON")
    return {"total": total, "items": items}


def _us_location(loc_list) -> str:
    """'City, ST, United States' for the primary US location, else ''."""
    locs = [l for l in (loc_list or []) if isinstance(l, dict)]
    locs.sort(key=lambda l: not l.get("primary"))
    for loc in locs[:1]:
        if loc.get("country") == "United States":
            parts = [loc.get("city"), loc.get("state"), "United States"]
            return ", ".join(p for p in parts if p)
    return ""


def fetch(slug: str) -> list[dict]:
    rec = slug_loader.get_record("goldman", slug)
    if not rec:
        raise FetchError(f"no goldman record for slug {slug!r}")
    analyst_entry = bool(rec.get("analyst_entry"))

    rows: list[dict] = []
    total = None
    for page_number in range(MAX_PAGES):
        page = _page(page_number)
        total = page["total"]
        rows.extend(r for r in page["items"] if isinstance(r, dict))
        if len(rows) >= total or not page["items"]:
            break
    if total is None or len(rows) != total:
        raise FetchError(f"Goldman [{slug}] listed {len(rows)} of {total} roles")

    jobs: list[dict] = []
    seen_ids: set[str] = set()
    for row in rows:
        title = row.get("jobTitle") or ""
        location = _us_location(row.get("locations"))
        if not location or not is_candidate_title(title, analyst_entry):
            continue
        source_id = str((row.get("externalSource") or {}).get("sourceId") or "")
        if not source_id:
            raise FetchError(f"Goldman [{slug}] role {row.get('roleId')!r} has no sourceId")
        try:
            job_id = make_job_id("goldman", slug, _BAD_ID_CHARS.sub("-", source_id))
        except ValueError as exc:
            raise FetchError(str(exc)) from exc
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)
        jobs.append({
            "job_id": job_id,
            "source": "goldman",
            "board_platform": "goldman",
            "board_slug": slug,
            "title": title,
            "company": rec["name"],
            "employer_logo": None,
            "location": location,
            "remote": False,
            "description_raw": html.unescape(_strip_html(row.get("descriptionHtml") or ""))[:8000],
            "apply_url": ROLE_URL.format(source_id),
            "posted_at": row.get("lastPostedDate"),
            "salary_min": None,
            "salary_max": None,
            "salary_period": None,
            "department": (row.get("division") or "").strip(),
            "team": "",
            "analyst_entry": analyst_entry,
        })

    logger.info("  Goldman [%s] -> %d jobs (of %d campus roles)", slug, len(jobs), total)
    return jobs
