"""Oracle Recruiting (Candidate Experience) direct fetcher (public REST JSON).

Board record URL: https://{host}/hcmUI/CandidateExperience/{lang}/sites/{site}.
Searches each early-career term, keeps US rows with a candidate title, then
fetches the detail for each survivor. Any failure raises FetchError.
"""
from __future__ import annotations

import html
import logging
import re
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from backend.pipeline import slug_loader
from backend.pipeline.fetcher import REQUEST_TIMEOUT, USER_AGENT, _polite_sleep, _strip_html
from backend.pipeline.direct import FetchError, is_candidate_title, make_job_id

logger = logging.getLogger(__name__)

SEARCH_TERMS = ("intern", "internship", "analyst", "new grad", "early career", "summer")
PAGE_SIZE = 25
MAX_PER_TERM = 100
_SITE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_BAD_ID_CHARS = re.compile(r"[^A-Za-z0-9-]")
_DEPT_KEYS = ("Department", "Organization", "JobFamily", "JobFunction")


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
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


_SESSION = _build_session(4)


def _parse_board_url(url: str) -> tuple[str, str]:
    """(host, site) from a record URL."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    site = parts[parts.index("sites") + 1] if "sites" in parts[:-1] else ""
    if not host.endswith(".oraclecloud.com") or not _SITE_RE.match(site):
        raise FetchError(f"unparseable Oracle url: {url!r}")
    return host, site


def _json(resp) -> dict:
    try:
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise FetchError(f"Oracle request failed: {exc}") from exc
    if not isinstance(data, dict):
        raise FetchError("Oracle returned unexpected JSON")
    return data


def _first_item(data: dict, what: str) -> dict:
    items = data.get("items")
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise FetchError(f"Oracle {what} missing items")
    return items[0]


def _department(row: dict) -> str:
    for key in _DEPT_KEYS:
        val = row.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def fetch(slug: str) -> list[dict]:
    rec = slug_loader.get_record("oracle", slug)
    if not rec:
        raise FetchError(f"no oracle record for slug {slug!r}")
    host, site = _parse_board_url(rec["url"])
    base = f"https://{host}/hcmRestApi/resources/latest"
    analyst_entry = bool(rec.get("analyst_entry"))

    rows: dict[str, dict] = {}  # Id -> list row, first seen wins
    for term in SEARCH_TERMS:
        offset = 0
        while offset < MAX_PER_TERM:
            _polite_sleep()
            url = (f"{base}/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList"
                   f"&finder=findReqs;siteNumber={site},keyword={quote(term)},"
                   f"limit={PAGE_SIZE},offset={offset}")
            try:
                resp = _SESSION.get(url, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                raise FetchError(f"Oracle [{slug}] list failed: {exc}") from exc
            page = _first_item(_json(resp), "list").get("requisitionList") or []
            if not isinstance(page, list):
                raise FetchError(f"Oracle [{slug}] list malformed")
            for row in page:
                rid = str(row.get("Id") or "")
                if (rid and rid not in rows and row.get("PrimaryLocationCountry") == "US"
                        and is_candidate_title(row.get("Title") or "", analyst_entry)):
                    rows[rid] = row
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        else:
            logger.warning("Oracle [%s] term %r hit the %d-result cap; results may be truncated",
                           slug, term, MAX_PER_TERM)

    jobs: list[dict] = []
    seen_ids: set[str] = set()
    for rid, row in rows.items():
        _polite_sleep()
        url = (f"{base}/recruitingCEJobRequisitionDetails?expand=all&onlyData=true"
               f'&finder=ById;Id="{rid}",siteNumber={site}')
        try:
            resp = _SESSION.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise FetchError(f"Oracle [{slug}] detail {rid} failed: {exc}") from exc
        info = _first_item(_json(resp), f"detail {rid}")
        ext_id = _BAD_ID_CHARS.sub("-", rid)
        try:
            job_id = make_job_id("oracle", slug, ext_id)
        except ValueError as exc:
            raise FetchError(str(exc)) from exc
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)

        desc = "".join(info.get(k) or "" for k in (
            "ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr"))
        location = (row.get("PrimaryLocation") or info.get("PrimaryLocation") or "").strip()
        jobs.append({
            "job_id": job_id,
            "source": "oracle",
            "board_platform": "oracle",
            "board_slug": slug,
            "title": row.get("Title") or "",
            "company": rec["name"],
            "employer_logo": None,
            "location": location,
            "remote": "remote" in location.lower(),
            # strip tags first, then unescape, so escaped text like &lt;b&gt; survives
            "description_raw": html.unescape(_strip_html(desc))[:8000],
            "apply_url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{rid}",
            "posted_at": row.get("PostedDate"),
            "salary_min": None,
            "salary_max": None,
            "salary_period": None,
            "department": _department(row),
            "team": "",
            "analyst_entry": analyst_entry,
        })

    logger.info("  Oracle [%s] -> %d jobs", slug, len(jobs))
    return jobs
