"""
Fetch job listings from Greenhouse, Lever, Ashby, and Fantastic.jobs APIs.
Outputs a list of pre-normalized job dicts ready for the normalizer/writer.
"""
import html
import logging
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from backend.pipeline import slug_loader

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15

# ---------------------------------------------------------------------------
# Politeness scaffolding (Phase 0)
# ---------------------------------------------------------------------------
# Per-platform requests.Session with a Retry adapter, custom User-Agent, and
# per-request jitter. Identifies us to Greenhouse / Lever / Ashby so we stay
# off greylists as slug count scales from 270 → ~10K.
#
# Pool sizing matches ThreadPoolExecutor `max_workers` for each platform so
# urllib3 doesn't complain about a saturated connection pool.

USER_AGENT = "OfferloopJobBot/1.0 (+https://offerloop.ai; contact@offerloop.ai)"

_JITTER_MS = (50, 300)

POOL_SIZE = {
    "greenhouse": 24,
    "lever": 16,
    "ashby": 12,
}


def _polite_sleep() -> None:
    """Add small random jitter before each ATS request to avoid burst patterns."""
    lo, hi = _JITTER_MS
    time.sleep(random.uniform(lo, hi) / 1000.0)


def _build_session(pool_size: int) -> requests.Session:
    """Session with retry-on-throttle + per-host connection pool sized for concurrency."""
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        pool_connections=pool_size,
        pool_maxsize=pool_size,
        max_retries=retry,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


_SESSION_GREENHOUSE = _build_session(POOL_SIZE["greenhouse"])
_SESSION_LEVER = _build_session(POOL_SIZE["lever"])
_SESSION_ASHBY = _build_session(POOL_SIZE["ashby"])

# ---------------------------------------------------------------------------
# Slug source (Phase 0 — 2026-07-14)
# ---------------------------------------------------------------------------
# The ~270 curated slugs that used to live inline here were migrated to
# backend/pipeline/data/ats_companies/hot_slugs.txt. slug_loader reads that
# file and the vendored jobhive CSVs; module-level bindings preserve backward
# compat for any caller that imported *_SLUGS from this module.

GREENHOUSE_SLUGS = slug_loader.load_slugs("greenhouse", tier="hot")
LEVER_SLUGS = slug_loader.load_slugs("lever", tier="hot")
ASHBY_SLUGS = slug_loader.load_slugs("ashby", tier="hot")

# ---------------------------------------------------------------------------
# HTML stripping helper
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = _TAG_RE.sub(" ", html)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Greenhouse fetcher
# ---------------------------------------------------------------------------

def _fetch_greenhouse(slug: str) -> list[dict]:
    # Migrated 2026-07-14 from boards.greenhouse.io to boards-api.greenhouse.io
    # per Greenhouse's canonical documented endpoint. Same response schema.
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    _polite_sleep()
    try:
        resp = _SESSION_GREENHOUSE.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Greenhouse [%s] failed: %s", slug, exc)
        return []

    jobs = []
    for job in data.get("jobs", []):
        title = job.get("title", "")
        location_name = ""
        if job.get("location"):
            location_name = job["location"].get("name", "")

        # Greenhouse returns content with HTML entities already encoded
        # (`&lt;p&gt;...` instead of `<p>...`). Unescape first so _strip_html
        # can see the real tags and remove them; otherwise the entities
        # survive into Firestore and render as visible `&lt;p&gt;` in the SPA.
        content = _strip_html(html.unescape(job.get("content", "")))[:8000]

        jobs.append({
            "job_id": f"direct_greenhouse_{slug}_{job['id']}",
            "source": "greenhouse",
            "board_platform": "greenhouse",
            "board_slug": slug,
            "title": title,
            "company": slug_loader.get_company_name("greenhouse", slug),
            "employer_logo": None,
            "location": location_name or "United States",
            "remote": "remote" in location_name.lower(),
            "description_raw": content,
            "apply_url": job.get("absolute_url", ""),
            "posted_at": job.get("updated_at"),
            "salary_min": None,
            "salary_max": None,
            "salary_period": None,
        })

    logger.info("  Greenhouse [%s] → %d jobs", slug, len(jobs))
    return jobs


def _fetch_all_greenhouse() -> list[dict]:
    results = []
    companies_with_jobs = 0
    with ThreadPoolExecutor(max_workers=POOL_SIZE["greenhouse"]) as pool:
        futures = {pool.submit(_fetch_greenhouse, slug): slug for slug in GREENHOUSE_SLUGS}
        for future in as_completed(futures):
            jobs = future.result()
            if jobs:
                companies_with_jobs += 1
            results.extend(jobs)
    logger.info("Greenhouse: %d jobs from %d companies", len(results), companies_with_jobs)
    return results


# ---------------------------------------------------------------------------
# Lever fetcher
# ---------------------------------------------------------------------------

def _fetch_lever(slug: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    _polite_sleep()
    try:
        resp = _SESSION_LEVER.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Lever [%s] failed: %s", slug, exc)
        return []

    if not isinstance(data, list):
        logger.warning("Lever [%s] unexpected response type: %s", slug, type(data).__name__)
        return []

    jobs = []
    for posting in data:
        title = posting.get("text", "")
        categories = posting.get("categories", {}) or {}
        location = categories.get("location", "") or ""
        workplace = (posting.get("workplaceType") or "").lower()

        created_ms = posting.get("createdAt")
        posted_at = None
        if created_ms:
            try:
                posted_at = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).isoformat()
            except (ValueError, OSError, TypeError):
                pass

        description = (posting.get("descriptionPlain") or "")[:8000]

        jobs.append({
            "job_id": f"direct_lever_{slug}_{posting['id']}",
            "source": "lever",
            "board_platform": "lever",
            "board_slug": slug,
            "title": title,
            "company": slug_loader.get_company_name("lever", slug),
            "employer_logo": None,
            "location": location or "United States",
            "remote": workplace == "remote",
            "description_raw": description,
            "apply_url": posting.get("hostedUrl", ""),
            "posted_at": posted_at,
            "salary_min": None,
            "salary_max": None,
            "salary_period": None,
        })

    logger.info("  Lever [%s] → %d jobs", slug, len(jobs))
    return jobs


def _fetch_all_lever() -> list[dict]:
    results = []
    companies_with_jobs = 0
    with ThreadPoolExecutor(max_workers=POOL_SIZE["lever"]) as pool:
        futures = {pool.submit(_fetch_lever, slug): slug for slug in LEVER_SLUGS}
        for future in as_completed(futures):
            jobs = future.result()
            if jobs:
                companies_with_jobs += 1
            results.extend(jobs)
    logger.info("Lever: %d jobs from %d companies", len(results), companies_with_jobs)
    return results


# ---------------------------------------------------------------------------
# Ashby fetcher
# ---------------------------------------------------------------------------

_ASHBY_TYPE_MAP = {
    "FullTime": "FULLTIME",
    "PartTime": "PARTTIME",
    "Intern": "INTERNSHIP",
}


def _parse_ashby_compensation(comp) -> tuple:
    """Extract (salary_min, salary_max, salary_period) from an Ashby
    compensation object.

    Honors only USD Salary components on YEAR or HOUR intervals; equity,
    non-USD currencies, and unsupported intervals (e.g. '1 MONTH') skip
    to None so existing downstream null-handling kicks in. Source data
    inspected 2026-06-02 on openai, ramp, notion live API.
    """
    if not isinstance(comp, dict):
        return None, None, None
    components = comp.get("summaryComponents") or []
    if not isinstance(components, list):
        return None, None, None
    for c in components:
        if not isinstance(c, dict):
            continue
        if c.get("compensationType") != "Salary":
            continue
        currency = c.get("currencyCode")
        if currency and currency != "USD":
            # Downstream display + normalizer assume USD; show "Not listed"
            # instead of mislabeling a foreign-currency number.
            continue
        interval = (c.get("interval") or "").upper().strip()
        if "YEAR" in interval:
            period = "YEAR"
        elif "HOUR" in interval:
            period = "HOUR"
        else:
            continue
        mn = c.get("minValue")
        mx = c.get("maxValue")
        return (
            float(mn) if isinstance(mn, (int, float)) else None,
            float(mx) if isinstance(mx, (int, float)) else None,
            period,
        )
    return None, None, None


def _fetch_ashby(slug: str) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    _polite_sleep()
    try:
        resp = _SESSION_ASHBY.get(
            url,
            params={"includeCompensation": "true"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Ashby [%s] failed: %s", slug, exc)
        return []

    jobs = []
    for job in data.get("jobs", []):
        title = job.get("title", "")
        location = job.get("locationName") or "Remote"
        description = _strip_html(job.get("descriptionHtml") or "")[:8000]
        employment_type = job.get("employmentType") or ""
        sal_min, sal_max, sal_period = _parse_ashby_compensation(job.get("compensation"))

        jobs.append({
            "job_id": f"direct_ashby_{slug}_{job['id']}",
            "source": "ashby",
            "board_platform": "ashby",
            "board_slug": slug,
            "title": title,
            "company": slug_loader.get_company_name("ashby", slug),
            "employer_logo": None,
            "location": location,
            "remote": bool(job.get("isRemote")),
            "description_raw": description,
            "apply_url": job.get("jobUrl", ""),
            "posted_at": job.get("publishedAt"),
            "salary_min": sal_min,
            "salary_max": sal_max,
            "salary_period": sal_period,
            "_employment_type": _ASHBY_TYPE_MAP.get(employment_type, ""),
        })

    logger.info("  Ashby [%s] → %d jobs", slug, len(jobs))
    return jobs


def _fetch_all_ashby() -> list[dict]:
    results = []
    companies_with_jobs = 0
    with ThreadPoolExecutor(max_workers=POOL_SIZE["ashby"]) as pool:
        futures = {pool.submit(_fetch_ashby, slug): slug for slug in ASHBY_SLUGS}
        for future in as_completed(futures):
            jobs = future.result()
            if jobs:
                companies_with_jobs += 1
            results.extend(jobs)
    logger.info("Ashby: %d jobs from %d companies", len(results), companies_with_jobs)
    return results


# ---------------------------------------------------------------------------
# Fantastic.jobs (RapidAPI active-jobs-db) fetcher — category-based strategy
# ---------------------------------------------------------------------------

FANTASTICJOBS_BASE_URL = "https://active-jobs-db.p.rapidapi.com/active-ats-7d"
# Modified Jobs API (Ultra+): returns jobs modified in the last 24h matching
# the same filter params as /active-ats-7d. Limit caps at 500 vs 100. CRITICAL:
# does NOT consume Jobs credits — only 1 Request credit per call. This is the
# right endpoint for daily delta runs.
FANTASTICJOBS_MODIFIED_URL = "https://active-jobs-db.p.rapidapi.com/modified-ats-24h"
# Expired Jobs API (Ultra+): returns an array of job IDs flagged expired
# yesterday. No params accepted. Also does NOT consume Jobs credits.
FANTASTICJOBS_EXPIRED_URL = "https://active-jobs-db.p.rapidapi.com/active-ats-expired"
FANTASTICJOBS_HOST = "active-jobs-db.p.rapidapi.com"

# Always-on params for every Fantastic.jobs call. include_ai unlocks the
# AI-derived fields (salary, experience level, visa, taxonomies, etc.) and
# include_li unlocks the LinkedIn company fields. Both are free on the Ultra
# plan — they don't consume extra Jobs credits, only bandwidth.
FANTASTICJOBS_DEFAULT_PARAMS = {
    "include_ai": "true",
    "include_li": "true",
    "description_type": "text",
}

# Mutable module-level snapshot of the latest x-ratelimit-* values seen on a
# Fantastic.jobs response. Logged at the end of every fetch_fantasticjobs run
# so quota burn is visible without checking the RapidAPI dashboard.
_FJ_RATELIMIT_SNAPSHOT: dict = {}

# Student-cycle recipes. Each targets a real recruiting flow (SWE intern,
# IB Summer Analyst, MBB summer, etc.) instead of casting a market-wide net.
#
# All recipes share agency=false (set per-call below) + limit=100 + the
# always-on AI/LI params from FANTASTICJOBS_DEFAULT_PARAMS.
#
# advanced_title_filter syntax (from FJ docs):
#   '...' = phrase, & = AND, | = OR, ! = NOT, <-> = followed-by, :* = prefix
#   Cannot be combined with the simpler title_filter param.
#
# Employment-type enum:   FULL_TIME / PART_TIME / CONTRACTOR / INTERN / OTHER
# Experience-level enum:  0-2 / 2-5 / 5-10 / 10+
FANTASTICJOBS_CALLS = [
    # SWE / data / ML / PM internships
    ("tech_intern", {
        "advanced_title_filter": (
            "(Intern | Internship | 'Co-op' | Coop) & "
            "('Software Engineer' | 'Software Engineering' | 'Software Developer' | "
            "SWE | 'Data Scien':* | 'Machine Learning' | 'Product Manager' | "
            "'Product Management' | 'Technical Program Manager' | TPM | "
            "'Site Reliability' | 'Security Engineer' | Cybersecurity | "
            "'Backend Engineer' | 'Frontend Engineer' | 'Full Stack' | "
            "'iOS Engineer' | 'Android Engineer')"
        ),
        "ai_employment_type_filter": "INTERN",
        "ai_experience_level_filter": "0-2",
        "location_filter": "United States",
    }),
    # New-grad full-time tech (no Intern keyword)
    ("tech_new_grad", {
        "advanced_title_filter": (
            "('New Grad' | 'New Graduate' | 'University Graduate' | 'Entry Level' | "
            "'Entry-Level' | 'Early Career' | 'Associate Product Manager' | APM | "
            "Rotational | 'Class of 2026' | 'Class of 2027') & "
            "('Software Engineer' | 'Software Developer' | 'Data Scien':* | "
            "'Machine Learning' | 'Product Manager' | 'Backend Engineer' | "
            "'Frontend Engineer' | 'Full Stack' | 'Site Reliability')"
        ),
        "ai_employment_type_filter": "FULL_TIME",
        "ai_experience_level_filter": "0-2",
        "location_filter": "United States",
    }),
    # Co-op programs (Northeastern / Drexel cycle, also some big-tech rotations)
    ("tech_coop", {
        "advanced_title_filter": (
            "('Co-op' | Coop | Cooperative) & "
            "(Software | Engineer | Developer | Data | Product | Engineering)"
        ),
        "ai_experience_level_filter": "0-2",
        "location_filter": "United States",
    }),
    # IB Summer Analyst — bulge bracket + elite boutiques
    ("ib_summer_analyst", {
        "advanced_title_filter": (
            "('Summer Analyst' | 'Off-Cycle' | 'Off Cycle' | 'Summer Investment' | "
            "'Investment Banking Summer' | 'Global Markets Summer' | "
            "'Sales & Trading Summer' | 'Spring Week')"
        ),
        "organization_filter": (
            "Goldman Sachs,JPMorgan Chase & Co.,Morgan Stanley,Bank of America,"
            "Citi,Wells Fargo,Barclays,Deutsche Bank,UBS,Evercore,Lazard,"
            "PJT Partners,Moelis & Company,Centerview Partners,Houlihan Lokey,"
            "Rothschild & Co,Guggenheim Partners,Perella Weinberg Partners,"
            "Jefferies,RBC Capital Markets"
        ),
    }),
    # Quant trading / research interns at prop shops + HFs
    ("quant_intern", {
        "advanced_title_filter": (
            "(Intern | Internship | 'Summer Analyst') & "
            "('Quantitative Research' | 'Quantitative Analyst' | 'Quant Trader' | "
            "'Quant Developer' | 'Trading Intern' | 'Algorithmic Trading' | "
            "'Quantitative Strategy')"
        ),
        "organization_filter": (
            "Citadel,Citadel Securities,Jane Street,Two Sigma,Jump Trading,"
            "Hudson River Trading,IMC Trading,DRW,DE Shaw,AQR Capital,Point72,"
            "Five Rings,Akuna Capital,Belvedere Trading,Optiver,SIG,"
            "Susquehanna International Group"
        ),
    }),
    # Year-coded cohort postings — catches early-cycle postings before
    # they're tagged with experience_level (often appear Jul-Sep)
    ("year_coded_analyst", {
        "advanced_title_filter": (
            "('2026 Summer Analyst' | '2027 Summer Analyst' | "
            "'Summer 2026 Analyst' | 'Summer 2027 Analyst' | "
            "'2026 Analyst Program' | '2027 Analyst Program' | "
            "'2026 Summer Associate' | '2027 Summer Associate')"
        ),
        "location_filter": "United States",
    }),
    # Summer consulting interns at MBB + Big 4 + strategy boutiques
    ("consulting_summer", {
        "advanced_title_filter": (
            "(Intern | Internship | 'Summer Associate' | 'Summer Business Analyst' | "
            "'Summer Consultant' | 'Summer Advisory') & "
            "(Consulting | Strategy | Advisory | 'Business Analyst' | "
            "'Risk Consulting' | 'Technology Consulting')"
        ),
        "organization_filter": (
            "McKinsey & Company,Boston Consulting Group,Bain & Company,Deloitte,"
            "PwC,EY,KPMG,Strategy&,EY-Parthenon,Accenture,Oliver Wyman,"
            "L.E.K. Consulting,Roland Berger,Alvarez & Marsal,Capgemini,"
            "IBM Consulting,Charles River Associates,Analysis Group,"
            "Cornerstone Research,Kearney,ZS Associates"
        ),
    }),
    # Consulting new-grad full-time (BA / AC / Advisory Associate)
    ("consulting_new_grad", {
        "advanced_title_filter": (
            "('Business Analyst' | 'Associate Consultant' | 'Strategy Analyst' | "
            "'Management Consulting Analyst' | 'Advisory Associate' | "
            "'Technology Consulting Analyst' | 'Risk Advisory Associate')"
        ),
        "ai_employment_type_filter": "FULL_TIME",
        "ai_experience_level_filter": "0-2",
        "organization_filter": (
            "McKinsey & Company,Boston Consulting Group,Bain & Company,Deloitte,"
            "PwC,EY,KPMG,Accenture,Oliver Wyman,L.E.K. Consulting,Kearney,"
            "ZS Associates"
        ),
    }),
    # Visa-sponsoring internships — gold for USC/NYU/UPenn international students
    ("visa_sponsoring_intern", {
        "advanced_title_filter": (
            "(Intern | Internship | 'Co-op' | 'Summer Analyst' | 'Summer Associate')"
        ),
        "ai_employment_type_filter": "INTERN",
        "ai_visa_sponsorship_filter": "true",
        "location_filter": "United States",
    }),
    # Broad entry-level catch-all for marketing, design, ops, finance, data
    ("broad_entry_level", {
        "advanced_title_filter": (
            "(Intern | Internship | 'New Grad' | 'Entry Level' | 'Entry-Level') & "
            "(Marketing | Designer | UX | Growth | 'Operations Analyst' | "
            "'Product Designer' | 'Financial Analyst' | 'Data Analyst' | "
            "'Business Analyst' | 'Research Analyst' | 'Investment Analyst')"
        ),
        "ai_experience_level_filter": "0-2",
        "location_filter": "United States",
    }),
]


def _fantasticjobs_headers() -> dict:
    return {
        "x-rapidapi-key": os.getenv("RAPIDAPI_KEY"),
        "x-rapidapi-host": FANTASTICJOBS_HOST,
    }


def _extract_fj_location(job: dict) -> str:
    """Extract location from Fantastic.jobs job data."""
    locations = job.get("locations_derived") or []
    if locations and isinstance(locations[0], dict):
        parts = []
        city = locations[0].get("city")
        state = locations[0].get("state")
        if city:
            parts.append(city)
        if state:
            parts.append(state)
        if parts:
            return ", ".join(parts)
    raw_locs = job.get("locations_raw") or []
    if raw_locs and isinstance(raw_locs[0], dict):
        addr = raw_locs[0].get("address")
        if addr:
            return addr
    return ""


def _fj_first(value):
    """Return the first non-empty value from a list, or the value itself."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _fj_salary(job: dict) -> tuple[float | None, float | None, str | None, str | None]:
    """Extract salary from FJ AI fields. Returns (min, max, period, currency).

    Prefers explicit min/max range, falls back to ai_salary_value if only a
    single value is provided. Periods are normalized to HOUR/DAY/WEEK/MONTH/YEAR.
    """
    sal_min = job.get("ai_salary_minvalue")
    sal_max = job.get("ai_salary_maxvalue")
    single = job.get("ai_salary_value")
    if sal_min is None and sal_max is None and single is not None:
        sal_min = single
        sal_max = single
    period = (job.get("ai_salary_unittext") or "").upper().strip() or None
    if period and period not in ("HOUR", "DAY", "WEEK", "MONTH", "YEAR"):
        period = None
    currency = job.get("ai_salary_currency") or None
    try:
        sal_min = float(sal_min) if sal_min is not None else None
        sal_max = float(sal_max) if sal_max is not None else None
    except (TypeError, ValueError):
        sal_min = sal_max = None
    return sal_min, sal_max, period, currency


def _normalize_fj_job(job: dict) -> dict:
    """Convert a single Fantastic.jobs API result to our pre-normalized format.

    Captures the AI-derived and LinkedIn org fields exposed via include_ai=true
    and include_li=true. These let us skip downstream PDL title enrichment and
    OpenAI salary extraction for FJ-sourced jobs.
    """
    sal_min, sal_max, sal_period, sal_currency = _fj_salary(job)
    # FJ returns description in either `description_text` (when
    # description_type=text) or `description_html`. Older API responses used
    # `description`. Read in that priority order to stay backward-compatible
    # with any cached test fixtures.
    description = (
        job.get("description_text")
        or job.get("description")
        or job.get("description_html")
        or ""
    )

    # ai_employment_type can be a single string or an array; flatten to the
    # primary value while preserving the array for the ranker.
    emp_type_raw = job.get("ai_employment_type")
    emp_type_primary = _fj_first(emp_type_raw)

    taxonomies = job.get("ai_taxonomies_a") or []
    if not isinstance(taxonomies, list):
        taxonomies = [taxonomies]

    return {
        "job_id": f"fantasticjobs_{job['id']}",
        "source": "fantasticjobs",
        "title": job.get("title", ""),
        "company": job.get("organization", ""),
        "employer_logo": job.get("organization_logo"),
        "location": _extract_fj_location(job) or "United States",
        "remote": bool(job.get("remote_derived", False)),
        "description_raw": description[:8000],
        "apply_url": job.get("url", ""),
        "posted_at": job.get("date_posted"),
        "date_created": job.get("date_created"),
        "date_validthrough": job.get("date_validthrough"),
        # Salary — populated from AI fields. Downstream normalizer treats these
        # the same as structured salary from any other source.
        "salary_min": sal_min,
        "salary_max": sal_max,
        "salary_period": sal_period,
        "salary_currency": sal_currency,
        # AI-derived fields (FJ proprietary; consumed by the student ranker)
        "ai_experience_level": job.get("ai_experience_level"),
        "ai_employment_type": emp_type_primary,
        "ai_employment_types": emp_type_raw if isinstance(emp_type_raw, list) else None,
        "ai_work_arrangement": job.get("ai_work_arrangement"),
        "ai_work_arrangement_office_days": job.get("ai_work_arrangement_office_days"),
        "ai_visa_sponsorship": job.get("ai_visa_sponsorship"),
        "ai_has_salary": (sal_min is not None or sal_max is not None),
        "ai_keywords": job.get("ai_keywords") or [],
        "ai_key_skills": job.get("ai_key_skills") or [],
        "ai_education_requirements": job.get("ai_education_requirements") or [],
        "ai_hiring_manager_name": job.get("ai_hiring_manager_name"),
        "ai_hiring_manager_email": job.get("ai_hiring_manager_email_address"),
        "ai_core_responsibilities": job.get("ai_core_responsibilities"),
        "ai_requirements_summary": job.get("ai_requirements_summary"),
        "ai_taxonomies_a": taxonomies,
        "ai_taxonomy_primary": taxonomies[0] if taxonomies else None,
        "ai_job_language": job.get("ai_job_language"),
        # LinkedIn org fields
        "linkedin_id": job.get("linkedin_id"),
        "linkedin_org_slug": job.get("linkedin_org_slug"),
        "linkedin_org_industry": job.get("linkedin_org_industry"),
        "linkedin_org_employees": job.get("linkedin_org_employees"),
        "linkedin_org_size": job.get("linkedin_org_size"),
        "linkedin_org_specialties": job.get("linkedin_org_specialties") or [],
        "linkedin_org_followers": job.get("linkedin_org_followers"),
        "linkedin_org_headquarters": job.get("linkedin_org_headquarters"),
        "linkedin_org_recruitment_agency": job.get("linkedin_org_recruitment_agency_derived"),
        # ATS provenance
        "ats_platform": job.get("source"),
        "ats_source_type": job.get("source_type"),
        "ats_source_domain": job.get("source_domain"),
    }


def _capture_ratelimit(resp) -> None:
    """Snapshot x-ratelimit-* headers from a Fantastic.jobs response."""
    for key in (
        "x-ratelimit-jobs-limit",
        "x-ratelimit-jobs-remaining",
        "x-ratelimit-requests-limit",
        "x-ratelimit-requests-remaining",
        "x-ratelimit-jobs-reset",
    ):
        val = resp.headers.get(key)
        if val is not None:
            _FJ_RATELIMIT_SNAPSHOT[key] = val


def _fj_fetch_page(params: dict, label: str, url: str = FANTASTICJOBS_BASE_URL) -> list[dict]:
    """Fetch a single page from Fantastic.jobs with 429 retry. Returns raw job dicts.

    Defaults to /active-ats-7d; pass FANTASTICJOBS_MODIFIED_URL for delta pulls.
    """
    headers = _fantasticjobs_headers()
    # Merge always-on defaults; caller params win on key collisions.
    merged = {**FANTASTICJOBS_DEFAULT_PARAMS, **params}
    try:
        resp = requests.get(url, headers=headers, params=merged, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 429:
            logger.warning("Fantastic.jobs [%s] rate limited, waiting 60s...", label)
            time.sleep(60)
            resp = requests.get(url, headers=headers, params=merged, timeout=REQUEST_TIMEOUT)
        _capture_ratelimit(resp)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Fantastic.jobs [%s] failed: %s", label, exc)
        return []
    jobs_data = data if isinstance(data, list) else data.get("data", [])
    return [j for j in jobs_data if j.get("id") and j.get("title")]


def fetch_fantasticjobs(since_hours: int | None = None) -> list[dict]:
    """Fetch jobs from Fantastic.jobs using the student-cycle recipes.

    Runs against /active-ats-7d with include_ai=true and include_li=true.

    Args:
        since_hours: When set, adds a date_filter to each call so the API only
            returns jobs with date_posted within the last N hours. Use this for
            daily cron runs against the 7d endpoint to avoid re-pulling the same
            jobs every day and burning the 20k/mo Jobs quota.
            Recommended values:
              - Daily run: 36 (24h plus 12h overlap buffer for FJ indexing delay)
              - Weekly run: None (pull the full 7d window)
    """
    if not os.getenv("RAPIDAPI_KEY"):
        logger.info("Fantastic.jobs: skipped (no API key)")
        return []

    # Let Greenhouse/Lever/Ashby finish their burst first
    time.sleep(3)

    date_filter_value = None
    if since_hours:
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        date_filter_value = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
        logger.info("Fantastic.jobs: applying date_filter > %s (last %sh)",
                    date_filter_value, since_hours)

    results = []
    calls_with_results = 0

    for i, (label, extra_params) in enumerate(FANTASTICJOBS_CALLS):
        if i > 0:
            time.sleep(1.5)

        params = {"limit": "100", "agency": "false", **extra_params}
        if date_filter_value:
            params["date_filter"] = date_filter_value
        raw = _fj_fetch_page(params, label)
        jobs = [_normalize_fj_job(j) for j in raw]

        if jobs:
            calls_with_results += 1
        results.extend(jobs)
        logger.info("  Fantastic.jobs [%s] → %d jobs", label, len(jobs))

    # Deduplicate by job_id (categories overlap)
    seen = set()
    deduped = []
    for job in results:
        if job["job_id"] not in seen:
            seen.add(job["job_id"])
            deduped.append(job)

    logger.info(
        "Fantastic.jobs: %d unique jobs from %d category calls (%d raw before dedup)",
        len(deduped), calls_with_results, len(results),
    )
    if _FJ_RATELIMIT_SNAPSHOT:
        logger.info(
            "Fantastic.jobs quota — jobs %s/%s remaining, requests %s/%s remaining, reset in %ss",
            _FJ_RATELIMIT_SNAPSHOT.get("x-ratelimit-jobs-remaining", "?"),
            _FJ_RATELIMIT_SNAPSHOT.get("x-ratelimit-jobs-limit", "?"),
            _FJ_RATELIMIT_SNAPSHOT.get("x-ratelimit-requests-remaining", "?"),
            _FJ_RATELIMIT_SNAPSHOT.get("x-ratelimit-requests-limit", "?"),
            _FJ_RATELIMIT_SNAPSHOT.get("x-ratelimit-jobs-reset", "?"),
        )
    return deduped


def fetch_fantasticjobs_modified() -> list[dict]:
    """Fetch jobs modified in the last 24h via the Ultra Modified Jobs endpoint.

    Runs the same FANTASTICJOBS_CALLS student-cycle recipes against
    /modified-ats-24h with limit=500. Per FJ docs, this endpoint does NOT
    consume Jobs credits — only 1 Request credit per call — making it the
    right cron target for daily delta ingestion.

    Returns pre-normalized job dicts ready for the normalizer/writer.
    """
    if not os.getenv("RAPIDAPI_KEY"):
        logger.info("Fantastic.jobs (modified): skipped (no API key)")
        return []

    results = []
    calls_with_results = 0
    for i, (label, extra_params) in enumerate(FANTASTICJOBS_CALLS):
        if i > 0:
            time.sleep(1.5)
        params = {"limit": "500", "agency": "false", **extra_params}
        raw = _fj_fetch_page(params, f"modified:{label}", url=FANTASTICJOBS_MODIFIED_URL)
        jobs = [_normalize_fj_job(j) for j in raw]
        if jobs:
            calls_with_results += 1
        results.extend(jobs)
        logger.info("  Fantastic.jobs (modified) [%s] → %d jobs", label, len(jobs))

    # Dedup by job_id (categories can overlap)
    seen = set()
    deduped = []
    for job in results:
        if job["job_id"] not in seen:
            seen.add(job["job_id"])
            deduped.append(job)
    logger.info(
        "Fantastic.jobs (modified): %d unique jobs from %d category calls (%d raw)",
        len(deduped), calls_with_results, len(results),
    )
    if _FJ_RATELIMIT_SNAPSHOT:
        logger.info(
            "Fantastic.jobs quota — jobs %s/%s remaining, requests %s/%s remaining",
            _FJ_RATELIMIT_SNAPSHOT.get("x-ratelimit-jobs-remaining", "?"),
            _FJ_RATELIMIT_SNAPSHOT.get("x-ratelimit-jobs-limit", "?"),
            _FJ_RATELIMIT_SNAPSHOT.get("x-ratelimit-requests-remaining", "?"),
            _FJ_RATELIMIT_SNAPSHOT.get("x-ratelimit-requests-limit", "?"),
        )
    return deduped


def fetch_expired_job_ids() -> list[str]:
    """Pull the daily list of expired job IDs from the Ultra Expired Jobs endpoint.

    Returns raw FJ-side IDs (not yet prefixed with 'fantasticjobs_'). Per FJ
    docs, returns ~25k+ IDs per call and does NOT consume Jobs credits.

    Caller is responsible for translating these to Firestore job_ids
    (see backend/pipeline/writer.mark_expired_jobs).
    """
    if not os.getenv("RAPIDAPI_KEY"):
        logger.info("Fantastic.jobs (expired): skipped (no API key)")
        return []

    headers = _fantasticjobs_headers()
    try:
        resp = requests.get(FANTASTICJOBS_EXPIRED_URL, headers=headers, timeout=REQUEST_TIMEOUT)
        _capture_ratelimit(resp)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Fantastic.jobs (expired): fetch failed: %s", exc)
        return []

    # Response shape per docs is an array of IDs. Defensive: accept either
    # bare list or {"data": [...]} wrapping.
    if isinstance(data, dict):
        data = data.get("data") or data.get("ids") or []
    if not isinstance(data, list):
        logger.warning("Fantastic.jobs (expired): unexpected response shape: %s", type(data).__name__)
        return []

    # Each entry may be either a bare ID string or {"id": "..."} dict
    ids = []
    for item in data:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict) and item.get("id"):
            ids.append(str(item["id"]))
    logger.info("Fantastic.jobs (expired): %d expired job IDs returned", len(ids))
    return ids


# ---------------------------------------------------------------------------
# Simplify (GitHub-hosted internship listings)
# ---------------------------------------------------------------------------

SIMPLIFY_LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2025-Internships"
    "/dev/.github/scripts/listings.json"
)


_SIMPLIFY_SEARCH_FILTERS = [
    "software",
    "finance",
    "data",
    "marketing",
    "consulting",
    "product manager",
    "research",
]


def _simplify_item_to_job(item: dict) -> dict | None:
    """Convert a single Simplify listing to our pre-normalized format."""
    if not item.get("active") or not item.get("is_visible"):
        return None

    title = (item.get("title") or "").strip()
    company = (item.get("company_name") or "").strip()
    if not title or not company:
        return None

    locations = item.get("locations") or []
    location_str = ", ".join(locations) if locations else ""

    return {
        "job_id": f"simplify_{item['id']}",
        "source": "simplify",
        "title": title,
        "company": company,
        "employer_logo": None,
        "location": location_str,
        "remote": "remote" in location_str.lower(),
        "description_raw": "",
        "apply_url": item.get("url") or "",
        "posted_at": item.get("date_posted"),
        "job_employment_type": "INTERNSHIP",
        "_category": _simplify_category(item.get("category", "")),
        "_terms": item.get("terms", []),
        "_sponsorship": item.get("sponsorship", ""),
    }


def fetch_simplify() -> list[dict]:
    """Fetch active internship listings from the Simplify GitHub repo.

    Downloads the full JSON once, then applies keyword filters to surface
    targeted subsets (software, finance, data, etc.). Deduplicates by job_id.
    """
    try:
        resp = requests.get(SIMPLIFY_LISTINGS_URL, timeout=30)
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        logger.warning("Simplify: fetch failed: %s", e)
        return []

    # Convert all active/visible items
    all_valid: list[dict] = []
    for item in raw:
        job = _simplify_item_to_job(item)
        if job:
            all_valid.append(job)

    # Phase 1: Take up to 2,000 from the full list (equivalent to 20 pages × 100)
    main_batch = all_valid[:2000]
    logger.info("  Simplify [main] → %d jobs (of %d active)", len(main_batch), len(all_valid))

    seen: set[str] = {j["job_id"] for j in main_batch}
    results = list(main_batch)
    total_pages = len(main_batch) // 100 + (1 if len(main_batch) % 100 else 0)

    # Phase 2: Keyword-filtered passes to surface relevant jobs beyond the top 2,000
    for search_term in _SIMPLIFY_SEARCH_FILTERS:
        matched = []
        for job in all_valid:
            if job["job_id"] in seen:
                continue
            title_lower = job["title"].lower()
            company_lower = job["company"].lower()
            cat_lower = job.get("_category", "").lower()
            if search_term in title_lower or search_term in company_lower or search_term in cat_lower:
                matched.append(job)
                if len(matched) >= 300:  # 3 pages × 100
                    break

        for job in matched:
            seen.add(job["job_id"])
        results.extend(matched)
        pages = len(matched) // 100 + (1 if len(matched) % 100 else 0)
        total_pages += pages
        logger.info("  Simplify [search=%s] → %d jobs (%d pages)", search_term, len(matched), pages)
        time.sleep(1)

    logger.info("Simplify: %d internship jobs fetched (%d pages)", len(results), total_pages)
    return results


def _simplify_category(raw_cat: str) -> str:
    """Map Simplify category strings to our internal categories."""
    cat = raw_cat.lower()
    if "software" in cat:
        return "engineering"
    if "data" in cat:
        return "data"
    if "design" in cat:
        return "design"
    if "product" in cat or "pm" in cat:
        return "product"
    if "marketing" in cat:
        return "marketing"
    if "finance" in cat or "accounting" in cat:
        return "finance"
    if "hardware" in cat or "electrical" in cat or "mechanical" in cat:
        return "engineering"
    if "research" in cat or "science" in cat:
        return "research"
    if "sales" in cat or "business" in cat:
        return "business"
    return "other"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def fetch_jobs(skip_fantastic: bool = False, fj_since_hours: int | None = None) -> list[dict]:
    """Fetch jobs from all sources concurrently.

    Args:
        skip_fantastic: Skip the Fantastic.jobs source entirely.
        fj_since_hours: Forwarded to fetch_fantasticjobs as date_filter cutoff.
            None = pull full 7d window. 36 = last ~24h (recommended for daily cron).
    Returns a list of pre-normalized job dicts.
    """
    # Coresignal is fetched separately (it needs the set of existing Firestore
    # job_ids to dedup against so we don't burn Collect credits on jobs we
    # already have). Callers that want Coresignal in the batch should invoke
    # fetch_all_coresignal(existing_job_ids=...) from backend.pipeline.coresignal
    # in the same run — kept out of this concurrent fan-out to keep the credit
    # accounting explicit.
    with ThreadPoolExecutor(max_workers=5) as pool:
        gh_future = pool.submit(_fetch_all_greenhouse)
        lv_future = pool.submit(_fetch_all_lever)
        ab_future = pool.submit(_fetch_all_ashby)
        fj_future = None if skip_fantastic else pool.submit(fetch_fantasticjobs, fj_since_hours)
        si_future = pool.submit(fetch_simplify)

        greenhouse_jobs = gh_future.result()
        lever_jobs = lv_future.result()
        ashby_jobs = ab_future.result()
        fantasticjobs = [] if fj_future is None else fj_future.result()
        simplify_jobs = si_future.result()

    if skip_fantastic:
        logger.info("Skipped Fantastic.jobs (--skip-fantastic)")
    all_jobs = greenhouse_jobs + lever_jobs + ashby_jobs + fantasticjobs + simplify_jobs
    logger.info("Total: %d jobs fetched", len(all_jobs))
    return all_jobs
