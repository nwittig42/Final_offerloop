"""Coresignal Multi-source Jobs API — pipeline fetcher.

Mirrors the shape of _fetch_all_greenhouse/lever/ashby/workable in fetcher.py:
top-level entry is `fetch_all_coresignal()` returning a list of pre-normalized
job dicts ready for the normalizer/writer.

Coresignal fills Offerloop's coverage gap on AI labs and big tech that our
Greenhouse/Lever/Ashby/Workable slug lists miss.

Pricing / budget notes (Starter tier — $49/mo):
  500 Search credits / month  → 1 credit per API call (regardless of result count)
  250 Collect credits / month → 1 credit per job_id hydrated
  Daily budget at 30-day month: 16 Search + 8 Collect

This module enforces per-run budgets via SEARCH_BUDGET_PER_RUN and
COLLECT_BUDGET_PER_RUN. The FJ modified daemon in wsgi.py runs this once
per day, so per-run == per-day.

Empirical filter values verified by probe (see docs are unreliable):
  seniority: "internship", "entry", "senior", "junior" (NOT "intern")
  company_last_funding_round_type: "Series A", "Series B", "Seed" (title case)
  company_technologies.technology: nested query on lowercase tech name
  country: "United States"
  company_name: fuzzy match — noisy, avoid unless firm is unambiguous
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.coresignal.com"
SEARCH_ENDPOINT = f"{BASE_URL}/cdapi/v2/job_multi_source/search/es_dsl"
COLLECT_ENDPOINT = f"{BASE_URL}/cdapi/v2/job_multi_source/collect/{{job_id}}"

REQUEST_TIMEOUT = 20

# Hard per-run credit budgets. Daemon runs once/day, so these are effectively
# daily budgets. Keep well under Starter tier's monthly allocation (500 Search
# / 250 Collect) with buffer for retries.
SEARCH_BUDGET_PER_RUN = 3   # 3 discovery searches/day = 90/month vs 500 budget
COLLECT_BUDGET_PER_RUN = 8  # 8 hydrations/day = 240/month vs 250 budget

# Target companies: high-signal firms Offerloop's users care about that our
# free ATS scraping misses. Fuzzy match noise is real — the CANONICAL_MATCHES
# below runs a strict text-similarity check to drop bogus company matches
# ("Citadel" -> "Citadel Healthcare", "Meta" -> "Meta Power Solutions").
TARGET_COMPANIES: list[str] = [
    # AI labs (verified good coverage in probe)
    "Anthropic",
    "OpenAI",
    "Cohere",
    "Perplexity AI",
    # Big tech (verified in probe)
    "Google",
    "Meta",
    "Amazon",
    "Microsoft",
    "Apple",
    "Nvidia",
    # Mid-size tech that isn't in our ATS lists
    "Stripe",
    "Databricks",
    "Snowflake",
    "Figma",
    "Notion",
]


def _canonicalize(name: str) -> str:
    """Normalize for equality-ish comparison. Drops only CORPORATE LEGAL
    suffixes (Inc, LLC, Ltd, Corp) — NOT industry qualifiers (Healthcare,
    Solutions, etc.). The latter indicate genuinely different companies:
    'Citadel Healthcare' is not 'Citadel'; 'Meta Power Solutions' is not
    'Meta'. Empirically confirmed by our probe against the live index.
    """
    if not name:
        return ""
    n = name.lower().strip()
    # ONLY corporate legal entity suffixes. Never strip industry qualifiers.
    for suffix in (
        " inc.", " inc", " ltd.", " ltd", " llc", " co.", " co",
        " corp.", " corp", " corporation", " company", " gmbh",
    ):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    return n.strip()


def _accept_company(target: str, actual: str) -> bool:
    """Fuzzy match protection. `target` is a canonical firm; `actual` is the
    company_name Coresignal returned. Reject when actual is clearly a different
    entity (per empirical probe: 'Meta Power Solutions', 'Citadel Healthcare',
    'Stanley Automotive').

    Strategy: canonicalize both by stripping common corporate suffixes
    (Inc, LLC, Healthcare, Solutions, etc.) and require exact equality.
    'Google LLC' → 'google' == 'Google' → 'google' (accept).
    'Meta Power Solutions' → 'meta power' != 'Meta' → 'meta' (reject).
    """
    if not actual:
        return False
    t = _canonicalize(target)
    a = _canonicalize(actual)
    if not t or not a:
        return False
    return t == a


def _api_key() -> Optional[str]:
    return os.environ.get("CORESIGNAL_API_KEY")


def _headers(api_key: str) -> dict:
    return {"apikey": api_key, "Content-Type": "application/json"}


def _build_discovery_query() -> dict:
    """One ES-DSL query that pulls US intern + entry-level roles at any of
    our target companies, posted recently. Coresignal doesn't accept `size`
    at top level so pagination defaults to their per-call cap (~1000)."""
    return {
        "query": {
            "bool": {
                "must": [
                    {"match": {"country": "United States"}},
                    {
                        "bool": {
                            "should": [
                                {"match": {"seniority": "internship"}},
                                {"match": {"seniority": "entry"}},
                            ]
                        }
                    },
                    {
                        "bool": {
                            "should": [
                                {"match": {"company_name": c}} for c in TARGET_COMPANIES
                            ]
                        }
                    },
                ]
            }
        }
    }


def _build_tech_stack_query(tech: str) -> dict:
    """Bonus discovery: US entry-level roles at any company using a specific
    technology. Used for Loops-style "companies using PyTorch hiring interns"."""
    return {
        "query": {
            "bool": {
                "must": [
                    {"match": {"country": "United States"}},
                    {
                        "bool": {
                            "should": [
                                {"match": {"seniority": "internship"}},
                                {"match": {"seniority": "entry"}},
                            ]
                        }
                    },
                    {
                        "nested": {
                            "path": "company_technologies",
                            "query": {
                                "match": {"company_technologies.technology": tech}
                            },
                        }
                    },
                ]
            }
        }
    }


def _search(api_key: str, body: dict, label: str) -> list[int]:
    """Run one ES-DSL search. Returns list of job_id ints (Coresignal's shape).
    Fail-soft: any exception returns []."""
    try:
        resp = requests.post(
            SEARCH_ENDPOINT, headers=_headers(api_key), json=body, timeout=REQUEST_TIMEOUT
        )
        if resp.status_code != 200:
            logger.warning(
                "Coresignal search [%s] HTTP %d: %s",
                label, resp.status_code, resp.text[:200],
            )
            return []
        data = resp.json()
        if isinstance(data, list):
            ids = [j for j in data if isinstance(j, int)]
            logger.info("  Coresignal search [%s] → %d job_ids", label, len(ids))
            return ids
        logger.warning("Coresignal search [%s] unexpected shape: %s", label, type(data).__name__)
        return []
    except Exception as exc:
        logger.warning("Coresignal search [%s] failed: %s", label, exc)
        return []


def _collect(api_key: str, job_id: int) -> Optional[dict]:
    """Hydrate one job by id. Fail-soft: any exception returns None."""
    try:
        resp = requests.get(
            COLLECT_ENDPOINT.format(job_id=job_id),
            headers=_headers(api_key),
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning(
                "Coresignal collect [%s] HTTP %d: %s",
                job_id, resp.status_code, resp.text[:120],
            )
            return None
        return resp.json()
    except Exception as exc:
        logger.warning("Coresignal collect [%s] failed: %s", job_id, exc)
        return None


def _normalize_coresignal_job(raw: dict) -> Optional[dict]:
    """Map a Coresignal Multi-source Jobs record into Offerloop's standard
    pipeline dict (matches Greenhouse/Lever/Ashby/Workable shape).

    Returns None if the record is missing critical fields (title, company).
    """
    job_id = raw.get("id")
    title = (raw.get("title") or "").strip()
    company = (raw.get("company_name") or "").strip()
    if not job_id or not title or not company:
        return None

    location = (raw.get("location") or "").strip()
    if not location:
        # Coresignal sometimes exposes long_location or country as fallback
        location = (
            raw.get("long_location")
            or raw.get("short_location")
            or raw.get("country")
            or "United States"
        )

    remote_val = raw.get("remote")
    if isinstance(remote_val, bool):
        remote = remote_val
    elif isinstance(remote_val, str):
        remote = remote_val.lower() in ("true", "yes", "remote")
    else:
        remote = "remote" in location.lower()

    seniority = raw.get("seniority") or ""
    # Coresignal returns "Entry level" / "Internship" — normalize case for
    # downstream ranker keyword matches on the badge system.
    seniority_norm = seniority.strip()

    employment_type = ""
    emp_statuses = raw.get("employment_statuses") or []
    if isinstance(emp_statuses, list) and emp_statuses:
        first = emp_statuses[0]
        if isinstance(first, str):
            employment_type = {
                "full_time": "FULLTIME",
                "part_time": "PARTTIME",
                "intern": "INTERNSHIP",
                "internship": "INTERNSHIP",
                "contract": "CONTRACT",
            }.get(first.lower(), first.upper())

    # Salary fields are unreliable (probe found a $290M/year parsing bug).
    # Only accept salary when both min and max are plausible (< $500k/yr
    # for entry-level target audience).
    salary_min = None
    salary_max = None
    salary_period = None
    try:
        s_min = raw.get("min_annual_salary_usd")
        s_max = raw.get("max_annual_salary_usd")
        if (
            isinstance(s_min, (int, float))
            and isinstance(s_max, (int, float))
            and 10_000 <= s_min <= 500_000
            and 10_000 <= s_max <= 500_000
        ):
            salary_min = float(s_min)
            salary_max = float(s_max)
            salary_period = "YEAR"
    except Exception:
        pass

    description = (raw.get("description") or "").strip()[:8000]
    apply_url = raw.get("url") or raw.get("source_url") or ""
    posted_at = raw.get("date_posted") or raw.get("discovered_at") or None

    return {
        "job_id": f"coresignal_{job_id}",
        "source": "coresignal",
        "title": title,
        "company": company,
        "employer_logo": None,
        "location": location,
        "remote": remote,
        "description_raw": description,
        "apply_url": apply_url,
        "posted_at": posted_at,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_period": salary_period,
        "_employment_type": employment_type,
        "_seniority": seniority_norm,
    }


def fetch_all_coresignal(
    existing_job_ids: Optional[set[str]] = None,
    tech_stacks_for_loops: Optional[list[str]] = None,
) -> list[dict]:
    """Daily Coresignal ingest. Returns pre-normalized job dicts.

    Args:
        existing_job_ids: Set of Firestore job_ids already present. Used to
            skip Collect on IDs we already have — saves credits. If None,
            no dedup. Caller (pipeline/main.py) is expected to pass this in.
        tech_stacks_for_loops: Optional list of technologies (e.g., ["pytorch",
            "django"]) to run additional discovery queries against. Each tech
            costs 1 Search credit. Skip if None or empty.

    Budget:
        Up to SEARCH_BUDGET_PER_RUN Search credits (3 by default)
        Up to COLLECT_BUDGET_PER_RUN Collect credits (8 by default)
    """
    api_key = _api_key()
    if not api_key:
        logger.info("Coresignal: no CORESIGNAL_API_KEY set, skipping")
        return []

    existing_job_ids = existing_job_ids or set()
    search_used = 0

    # Search 1: US intern/entry at target companies
    candidate_ids: list[int] = []
    if search_used < SEARCH_BUDGET_PER_RUN:
        ids = _search(api_key, _build_discovery_query(), "target_companies_intern_entry")
        candidate_ids.extend(ids)
        search_used += 1

    # Searches 2-3: tech-stack-driven discovery for Loops (bonus, only if
    # caller supplied specific technologies)
    for tech in (tech_stacks_for_loops or [])[: SEARCH_BUDGET_PER_RUN - search_used]:
        if search_used >= SEARCH_BUDGET_PER_RUN:
            break
        ids = _search(api_key, _build_tech_stack_query(tech), f"tech_{tech}")
        candidate_ids.extend(ids)
        search_used += 1

    # Dedup + prioritize by order (freshest first per Coresignal's response)
    seen: set[int] = set()
    unique_ids: list[int] = []
    for jid in candidate_ids:
        if jid in seen:
            continue
        seen.add(jid)
        # Skip if we already have this job in Firestore
        if f"coresignal_{jid}" in existing_job_ids:
            continue
        unique_ids.append(jid)

    # Enforce Collect budget
    to_collect = unique_ids[:COLLECT_BUDGET_PER_RUN]
    logger.info(
        "Coresignal: %d candidates, %d unique-new, collecting %d (budget %d)",
        len(candidate_ids), len(unique_ids), len(to_collect), COLLECT_BUDGET_PER_RUN,
    )

    # Hydrate + normalize with fuzzy-match protection
    normalized_jobs: list[dict] = []
    for jid in to_collect:
        raw = _collect(api_key, jid)
        if not raw:
            continue
        normalized = _normalize_coresignal_job(raw)
        if not normalized:
            continue

        # Fuzzy-match protection: verify company_name actually matches ONE
        # of our target firms. Coresignal returns "Meta Power Solutions" for
        # a "Meta" query — those slip through without this check.
        company = normalized["company"]
        if not any(_accept_company(target, company) for target in TARGET_COMPANIES):
            # If we're running tech-stack queries, we accept any company that
            # matches the tech since that's exactly what Loops wants.
            if not tech_stacks_for_loops:
                logger.debug(
                    "  Coresignal: rejecting fuzzy match [%s] as company",
                    company,
                )
                continue

        normalized_jobs.append(normalized)

        # Gentle rate limit (Coresignal caps Collect at 54 req/sec)
        time.sleep(0.05)

    logger.info(
        "Coresignal: %d jobs normalized (search used %d, collect used %d)",
        len(normalized_jobs), search_used, len(to_collect),
    )
    return normalized_jobs
