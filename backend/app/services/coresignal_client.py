"""
Coresignal Multi-Source Employee API client.

Surfaces a PDL-compatible interface so this module can drop into the same
call sites as `pdl_client.search_contacts_from_prompt`. Returns contacts in
the canonical Offerloop contact schema (see hunter_person_search.py:135-157,
pdl_client.py:1489-1514).

Coresignal's API is a 2-step pattern:
  1. POST /employee_multi_source/search/es_dsl  ->  array of profile IDs (1 Search credit)
  2. GET  /employee_multi_source/collect/{id}   ->  full profile JSON      (1 Collect credit per fetch)

We minimize Collect credits by lazy collection: keep collecting from the
returned IDs only until we accumulate `max_contacts` valid contacts after
dedup, then stop.

Auth: `apikey: <key>` request header.
"""

import os
import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from app.services.metering import meter_call

logger = logging.getLogger("coresignal_client")

# Inline (don't import from hunter.py — pulls in OpenAI deps we don't need).
# Mirror of hunter.is_personal_email_domain so we drop personal-domain
# emails before treating them as work emails.
_PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "me.com", "protonmail.com", "mail.com", "yandex.com",
    "zoho.com", "gmx.com", "live.com", "msn.com", "inbox.com",
    "sbcglobal.net", "att.net", "verizon.net", "comcast.net",
}


def is_personal_email_domain(domain: str) -> bool:
    if not domain:
        return False
    return domain.strip().lower() in _PERSONAL_EMAIL_DOMAINS

CORESIGNAL_API_KEY = os.environ.get("CORESIGNAL_API_KEY", "")
CORESIGNAL_BASE_URL = os.environ.get(
    "CORESIGNAL_BASE_URL", "https://api.coresignal.com/cdapi/v2"
)
SEARCH_ENDPOINT = f"{CORESIGNAL_BASE_URL}/employee_multi_source/search/es_dsl"
COLLECT_ENDPOINT = f"{CORESIGNAL_BASE_URL}/employee_multi_source/collect"

_session = requests.Session()

# Cap search-side IDs we ever ask for. Each ID is 1 Search credit but the
# Pro plan has 20K Search credits/mo, so generous here is fine. Lazy collect
# below ensures we only spend Collect credits up to what we return.
DEFAULT_SEARCH_PAGE_SIZE = 50
SEARCH_TIMEOUT_SEC = 25
COLLECT_TIMEOUT_SEC = 20
COLLECT_CONCURRENCY = 4  # parallel HTTP for collect step; 4 is conservative


# ---------------------------------------------------------------------------
# Public surface (mirrors pdl_client.search_contacts_from_prompt)
# ---------------------------------------------------------------------------


@meter_call("coresignal", "member_search")
def search_contacts_from_prompt(
    parsed_prompt: Dict[str, Any],
    max_contacts: int,
    exclude_keys: Optional[Set[str]] = None,
    user_profile: Optional[Dict[str, Any]] = None,
    alumni_school: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], int, List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Drop-in surface for pdl_client.search_contacts_from_prompt.

    Returns the same 4-tuple shape:
        (contacts, retry_level_used, already_saved_contacts, adjacency_metadata)

    `retry_level_used` is always 0 here; Coresignal doesn't have PDL's
    multi-rung broadening (Phase 3 can layer that on top).
    """
    if not CORESIGNAL_API_KEY:
        logger.warning("CORESIGNAL_API_KEY not set; returning empty result")
        return [], 0, [], {"provider": "coresignal", "error": "missing_api_key"}

    exclude_keys = exclude_keys or set()

    query = _build_es_query(parsed_prompt)
    if query is None:
        return [], 0, [], {
            "provider": "coresignal",
            "message": "No company specified — Coresignal needs at least a company filter to return useful results.",
            "drop_reasons": {"no_company": 1},
        }

    ids = _search_ids(query, page_size=DEFAULT_SEARCH_PAGE_SIZE)
    if not ids:
        return [], 0, [], {"provider": "coresignal", "raw_count": 0}

    # Alumni-first (2026-08-24): the same search constrained to the
    # student's own school, and those people lead the deck. Searches are
    # free; the warmth scorer then writes a real "fellow Trojan" opener.
    if alumni_school:
        alumni = alumni_first_ids(parsed_prompt, alumni_school, page_size=max_contacts * 2)
        if alumni:
            alumni_set = set(alumni)
            ids = alumni + [i for i in ids if i not in alumni_set]
            logger.info("coresignal alumni-first: %d alumni lead the deck", len(alumni))

    # Lazy collect: walk the ID list, fetching in parallel batches sized to
    # exactly what we still need. Each Collect call is 1 paid credit, so
    # over-fetching is real money. We keep concurrency for latency on the
    # bulk of the work, but the LAST batch shrinks to `remaining` so we
    # never collect profiles we won't return.
    contacts: List[Dict[str, Any]] = []
    already_saved: List[Dict[str, Any]] = []
    seen_keys: Set[str] = set(exclude_keys)
    collected_count = 0

    target_company = _target_company_name(parsed_prompt)
    target_schools = parsed_prompt.get("schools") or []

    cursor = 0
    while cursor < len(ids) and len(contacts) < max_contacts:
        remaining = max_contacts - len(contacts)
        take = min(remaining, COLLECT_CONCURRENCY, len(ids) - cursor)
        batch = ids[cursor : cursor + take]
        cursor += take

        profiles = _collect_profiles_parallel(batch)
        collected_count += len(profiles)
        for prof in profiles:
            if len(contacts) >= max_contacts:
                break
            normalized = _normalize_to_contact(prof, target_company=target_company)
            if not normalized:
                continue
            # Promotion guard: "joined recently" means the person's FIRST
            # entry at the target company is recent, not merely their
            # newest title.
            if parsed_prompt.get("joined_after") and target_company:
                if not _joined_recently(prof, target_company, parsed_prompt["joined_after"]):
                    logger.info("coresignal recency guard dropped %s (tenure predates cutoff)",
                                normalized.get("FirstName", "?"))
                    continue
            key = _contact_key(normalized)
            if key and key in seen_keys:
                already_saved.append(normalized)
                continue
            if key:
                seen_keys.add(key)
            contacts.append(normalized)

    meta = {
        "provider": "coresignal",
        "raw_count": len(ids),
        "collected_count": collected_count,
        "schools_requested": target_schools,
    }
    return contacts, 0, already_saved, meta


@meter_call("coresignal", "member_collect")
def enrich_linkedin_profile(linkedin_url: str) -> Optional[Dict[str, Any]]:
    """
    Look up a single profile by LinkedIn URL. Returns canonical contact or None.

    NOTE: Coresignal's collect endpoint keys on internal profile_id, not LinkedIn
    URL. To enrich by LinkedIn URL we run a 2-credit roundtrip: search by URL,
    then collect the first match.
    """
    if not CORESIGNAL_API_KEY or not linkedin_url:
        return None
    canonical = (linkedin_url or "").strip().lower()
    if not canonical:
        return None
    query = {
        "query": {
            "bool": {
                "must": [{"term": {"linkedin_url": canonical}}],
            }
        }
    }
    ids = _search_ids(query, page_size=1)
    if not ids:
        return None
    prof = _collect_profile(ids[0])
    if not prof:
        return None
    return _normalize_to_contact(prof)


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


# Role nouns and seniority words that appear in nearly every title. A
# variation made ONLY of these carries none of the request's identity.
_GENERIC_TITLE_TOKENS = {
    "analyst", "manager", "engineer", "developer", "specialist", "associate",
    "coordinator", "director", "lead", "senior", "junior", "staff",
    "principal", "intern", "consultant", "executive", "officer",
    "representative", "rep", "assistant", "head", "vp", "president",
}


def _distinctive_variations(variations: List[str]) -> List[str]:
    """Drop title variations that lost every distinctive word of the request.

    The parser deliberately pads title_variations with generic fallbacks
    ("Investment Banking Analyst" gains "Financial Analyst"). Anchored to a
    company that is harmless breadth; with no company filter it turned
    "Investment Banking Analysts at Technology companies" into "anyone in
    finance anywhere" and served a city-government finance manager
    (Rylan 2026-08-26). The FIRST variation is treated as the literal ask;
    any variation sharing one of its non-generic words survives. A request
    whose title is purely generic ("Consultant", "Recruiter") keeps
    everything, since there is no identity to lose.
    """
    if len(variations) <= 1:
        return variations
    primary_tokens = set(re.findall(r"[a-z]+", (variations[0] or "").lower()))
    distinctive = primary_tokens - _GENERIC_TITLE_TOKENS
    # Single-word asks ("Recruiter") keep their synonyms; only a QUALIFIED
    # title ("Investment Banking Analyst") has an identity to defend.
    if not distinctive or len(primary_tokens) < 2:
        return variations
    kept = [
        v for v in variations
        if set(re.findall(r"[a-z]+", (v or "").lower())) & distinctive
    ]
    dropped = [v for v in variations if v not in kept]
    if dropped:
        logger.info("coresignal title guard dropped %d generic variations: %s",
                    len(dropped), dropped[:5])
    return kept or variations


def _build_es_query(parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Translate Offerloop's parse_search_prompt_structured output into the
    Coresignal Elasticsearch DSL we verified against the live API.

    Returns None when the parsed prompt has no actionable filters (e.g.
    no company AND no school) — caller should short-circuit instead of
    burning a Search credit on a match_all.
    """
    must: List[Dict[str, Any]] = []

    companies = parsed.get("companies") or []
    company_names = [c.get("name") for c in companies if isinstance(c, dict) and c.get("name")]
    title_variations = list(parsed.get("title_variations") or [])
    # The distinctive-title guard is built for prompt search; callers with
    # curated title tiers (hiring-manager finder) opt out.
    if not parsed.get("skip_title_guard"):
        title_variations = _distinctive_variations(title_variations)
    schools = parsed.get("schools") or []
    locations = parsed.get("locations") or []

    # Current company + title via nested experience with active filter.
    # `active_experience: 1` constrains to the person's current role only.
    if company_names:
        nested_must: List[Dict[str, Any]] = [
            {"term": {"experience.active_experience": 1}},
        ]
        # Joined-recently constrains the SAME experience entry as the
        # company match; a sibling clause matched ANY recent entry, which
        # let a 4-year veteran with a fresh promotion pass as a new hire.
        if parsed.get("joined_after"):
            nested_must.append({"range": {"experience.date_from": {"gte": parsed["joined_after"]}}})
        if len(company_names) == 1:
            nested_must.append({"match_phrase": {"experience.company_name": company_names[0]}})
        else:
            nested_must.append({
                "bool": {
                    "should": [
                        {"match_phrase": {"experience.company_name": n}} for n in company_names
                    ],
                    "minimum_should_match": 1,
                }
            })
        if title_variations:
            # Any title variation matches (fuzzy-OR across the parsed variants).
            nested_must.append({
                "bool": {
                    "should": [
                        {"match_phrase": {"experience.position_title": t}} for t in title_variations
                    ],
                    "minimum_should_match": 1,
                }
            })
        must.append({"nested": {"path": "experience", "query": {"bool": {"must": nested_must}}}})
    elif title_variations:
        # Title-only (no company) — match any active role with that title.
        must.append({
            "nested": {
                "path": "experience",
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"experience.active_experience": 1}},
                            {
                                "bool": {
                                    "should": [
                                        {"match_phrase": {"experience.position_title": t}} for t in title_variations
                                    ],
                                    "minimum_should_match": 1,
                                }
                            },
                        ]
                    }
                },
            }
        })

    # Education filter via nested education.institution_name match_phrase.
    if schools:
        must.append({
            "nested": {
                "path": "education",
                "query": {
                    "bool": {
                        "should": [
                            {"match_phrase": {"education.institution_name": s}} for s in schools
                        ],
                        "minimum_should_match": 1,
                    }
                },
            }
        })

    # Skills, matched against Coresignal's inferred_skills (982 React
    # engineers at Stripe in the live probe).
    skills = [sk for sk in (parsed.get("skills") or []) if isinstance(sk, str) and sk.strip()]
    if skills:
        must.append({
            "bool": {
                "should": [{"match": {"inferred_skills": sk}} for sk in skills],
                "minimum_should_match": 1,
            }
        })

    # Location is a flat field on the profile. match_phrase, not match:
    # analyzed OR-matching let "United Kingdom" pass a "United States"
    # filter token-by-token (caught via a Goa profile on a US-defaulted
    # deck, 2026-08-24). Phrase matching pins the whole value.
    if locations:
        must.append({
            "bool": {
                "should": [
                    {"match_phrase": {"location_full": loc}} for loc in locations
                ] + [
                    {"match_phrase": {"location_country": loc}} for loc in locations
                ],
                "minimum_should_match": 1,
            }
        })

    if not must:
        return None
    return {"query": {"bool": {"must": must}}}


def _target_company_name(parsed: Dict[str, Any]) -> Optional[str]:
    companies = parsed.get("companies") or []
    if companies and isinstance(companies[0], dict):
        name = (companies[0].get("name") or "").strip()
        return name or None
    return None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _headers() -> Dict[str, str]:
    return {"apikey": CORESIGNAL_API_KEY, "Content-Type": "application/json"}


def _joined_recently(prof: Dict[str, Any], target_company: str, joined_after: str) -> bool:
    """True when the person's EARLIEST stint at the target company started
    on or after the cutoff ("February 2026"-style). Unknown dates pass."""
    from datetime import datetime as _dt
    try:
        cutoff = _dt.strptime(joined_after, "%B %Y")
    except Exception:
        return True
    tc = (target_company or "").strip().lower()
    earliest = None
    for e in prof.get("experience") or []:
        if not isinstance(e, dict):
            continue
        if tc not in (e.get("company_name") or "").lower():
            continue
        y, mo = e.get("date_from_year"), e.get("date_from_month") or 1
        if not y:
            continue
        stamp = _dt(int(y), int(mo), 1)
        if earliest is None or stamp < earliest:
            earliest = stamp
    return earliest is None or earliest >= cutoff


def alumni_first_ids(parsed, school: str, page_size: int = 30):
    """IDs for people matching the search AND sharing the student's school.

    The warm-intro query the capability matrix proved (1,000 USC alumni
    currently at Deloitte): free to run, and the deck leads with people the
    warmth scorer can write a real opening line about. Returns [] on any
    failure; the caller treats alumni as a bonus, never a dependency.
    """
    school = (school or "").strip()
    if not school:
        return []
    base = _build_es_query(parsed)
    if base is None:
        return []
    try:
        base["query"]["bool"].setdefault("must", []).append({
            "nested": {
                "path": "education",
                "query": {"match_phrase": {"education.institution_name": school}},
            }
        })
        return _search_ids(base, page_size=page_size)
    except Exception:
        logger.exception("coresignal: alumni-first search failed")
        return []


def _search_ids(query: Dict[str, Any], page_size: int = DEFAULT_SEARCH_PAGE_SIZE) -> List[int]:
    """
    Run the ES DSL search. Returns up to `page_size` profile IDs.
    Coresignal caps responses at 1000 IDs in our experimentation.
    """
    # Coresignal `from`/`size` aren't part of the public DSL; the API simply
    # returns up to its internal cap. We truncate client-side.
    try:
        resp = _session.post(
            SEARCH_ENDPOINT, headers=_headers(), json=query, timeout=SEARCH_TIMEOUT_SEC
        )
    except requests.RequestException as e:
        logger.warning("Coresignal search HTTP error: %s", e)
        return []
    if resp.status_code != 200:
        logger.warning(
            "Coresignal search non-200: status=%s body=%s",
            resp.status_code, resp.text[:200],
        )
        return []
    try:
        ids = resp.json()
    except ValueError:
        logger.warning("Coresignal search returned non-JSON body")
        return []
    if not isinstance(ids, list):
        logger.warning("Coresignal search returned unexpected shape: %r", type(ids))
        return []
    return [i for i in ids[:page_size] if isinstance(i, int)]


def _collect_profile(profile_id: int) -> Optional[Dict[str, Any]]:
    try:
        resp = _session.get(
            f"{COLLECT_ENDPOINT}/{profile_id}",
            headers=_headers(),
            timeout=COLLECT_TIMEOUT_SEC,
        )
    except requests.RequestException as e:
        logger.warning("Coresignal collect HTTP error for id=%s: %s", profile_id, e)
        return None
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        logger.warning(
            "Coresignal collect non-200 for id=%s: status=%s body=%s",
            profile_id, resp.status_code, resp.text[:200],
        )
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _collect_profiles_parallel(ids: List[int]) -> List[Dict[str, Any]]:
    """Fetch a batch of profiles in parallel. Preserves input order."""
    if not ids:
        return []
    out: List[Optional[Dict[str, Any]]] = [None] * len(ids)
    with ThreadPoolExecutor(max_workers=min(COLLECT_CONCURRENCY, len(ids))) as ex:
        futures = {ex.submit(_collect_profile, pid): idx for idx, pid in enumerate(ids)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                out[idx] = fut.result()
            except Exception as e:
                logger.warning("Coresignal collect exception: %s", e)
                out[idx] = None
    return [p for p in out if p]


# ---------------------------------------------------------------------------
# Normalization (Coresignal profile -> Offerloop canonical contact)
# ---------------------------------------------------------------------------


def _normalize_to_contact(
    prof: Dict[str, Any],
    target_company: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Map a Coresignal multi-source employee profile into Offerloop's canonical
    contact dict (same keys the frontend and Firestore writes expect — see
    hunter_person_search.py:135-157 and pdl_client.py:1489-1514).
    """
    if not isinstance(prof, dict):
        return None
    if prof.get("is_deleted") == 1:
        return None

    first_name = (prof.get("first_name") or "").strip()
    last_name = (prof.get("last_name") or "").strip()
    if not (first_name or last_name):
        return None

    linkedin = (prof.get("linkedin_url") or "").strip()

    # Current role: prefer active_experience_* flat fields; fall back to the
    # most recent experience array entry.
    title = (prof.get("active_experience_title") or "").strip()
    company = ""
    actives = [e for e in (prof.get("experience") or []) if isinstance(e, dict) and e.get("active_experience") == 1]
    # People hold two active positions (a Deloitte consultant who also lists
    # a side role first, benchmark 2026-08-20). When the search named a
    # company, the active position AT that company is the one the card and
    # the "currently at target" flag should describe.
    if target_company and actives:
        tc = target_company.strip().lower()
        for exp in actives:
            if tc and tc in (exp.get("company_name") or "").lower():
                title = (exp.get("position_title") or "").strip() or title
                company = (exp.get("company_name") or "").strip()
                break
    if not title:
        for exp in actives:
            title = (exp.get("position_title") or "").strip()
            company = company or (exp.get("company_name") or "").strip()
            break
    if not company:
        for exp in actives:
            company = (exp.get("company_name") or "").strip()
            break
    if not company and (prof.get("experience") or []):
        company = (prof["experience"][0].get("company_name") or "").strip()

    # Email: keep only professional emails; skip personal-domain emails so
    # we don't burn outreach on @gmail.com etc. (matches Hunter's policy at
    # hunter_person_search.py:126-131).
    email = ""
    primary = (prof.get("primary_professional_email") or "").strip()
    if primary and "@" in primary:
        domain = primary.split("@")[-1]
        if not is_personal_email_domain(domain):
            email = primary
    if not email:
        for entry in prof.get("professional_emails_collection") or []:
            cand = (entry.get("professional_email") or "").strip() if isinstance(entry, dict) else ""
            if cand and "@" in cand and not is_personal_email_domain(cand.split("@")[-1]):
                email = cand
                break

    # Trust tiering, calibrated 2026-08-20 by SMTP-verifying every address
    # in an 18-profile benchmark: guessed tiers went 6 of 6 invalid (pure
    # pattern guesses, one set aimed at a recruiting domain), matched_email
    # went 4 of 5 valid, and nothing arrived as "verified". So:
    #   verified      -> sendable as-is
    #   matched_email -> held as PendingEmail; the reveal step SMTP-checks
    #                    it before it counts (free of Coresignal credits)
    #   guessed_*     -> discarded outright; a bounce costs more trust
    #                    than a miss
    email_status = (prof.get("primary_professional_email_status") or "").strip().lower()
    pending_email = ""
    if email and email_status != "verified":
        if email_status == "matched_email":
            pending_email = email
        email = ""

    # Education: pick the highest-signal school (prefer first entry in array,
    # which Coresignal orders by recency).
    college = ""
    education_top = ""
    educations = prof.get("education") or []
    if educations:
        top = educations[0] if isinstance(educations[0], dict) else {}
        college = (top.get("institution_name") or "").strip()
        education_top = _format_education(educations[:3])

    # Location: flatten Coresignal's city/state into Offerloop's shape.
    city = (prof.get("location_city") or "").strip()
    state = (prof.get("location_state") or "").strip()
    location_full = (prof.get("location_full") or "").strip()

    # Synthesize a stable pseudo-pdlId so existing dedup / Firestore writes
    # work unchanged. Prefix-scoped so we can tell provenance from the key.
    coresignal_id = prof.get("id")
    pdl_id = f"coresignal:{coresignal_id}" if coresignal_id is not None else _synthesize_id(
        email, linkedin, first_name, last_name, company
    )

    contact = {
        "pdlId": pdl_id,
        "FirstName": first_name,
        "LastName": last_name,
        "LinkedIn": linkedin,
        "linkedinUrl": linkedin,
        "Email": email,
        "WorkEmail": email,
        "PersonalEmail": "",
        "Title": title,
        "JobTitle": title,
        "Company": company,
        "City": city,
        "State": state,
        "location": location_full,
        "College": college,
        "EducationTop": education_top,
        "WorkSummary": _format_experience((prof.get("experience") or [])[:3]),
        "experience": _summarize_experience((prof.get("experience") or [])[:2]),
        "Phone": "",  # Coresignal multi-source doesn't expose phone in standard plan
        # Benchmarked 18/18 present (2026-08-20): real LinkedIn headshots plus
        # the employer logo. snake-case photo_url is what the app card reads.
        "PhotoUrl": (prof.get("picture_url") or "").strip(),
        "CompanyLogoUrl": (
            (prof["experience"][0].get("company_logo_url") or "").strip()
            if isinstance(prof.get("experience"), list) and prof.get("experience")
            and isinstance(prof["experience"][0], dict) else ""
        ),
        "PendingEmail": pending_email,
        "PendingEmailStatus": email_status if pending_email else "",
        "_source": "coresignal",
        "_provider": "coresignal",
        "_coresignal_profile_score": prof.get("profile_score"),
        "_coresignal_is_decision_maker": prof.get("is_decision_maker"),
        "EmailSource": "coresignal" if email else "",
        # Their statuses are verified/matched_email/matched_pattern/
        # guessed_common_pattern; the old check compared against "valid",
        # which never occurs, so nothing was ever marked verified.
        "EmailVerified": bool(email and email_status == "verified"),
    }
    # IsCurrentlyAtTarget mirrors the PDL flag used downstream by the
    # frontend's "still at target company" UI badge.
    if target_company and company:
        contact["IsCurrentlyAtTarget"] = (
            company.strip().lower() == target_company.strip().lower()
        )
    return contact


def _format_education(educations: List[Dict[str, Any]]) -> str:
    parts = []
    for edu in educations:
        if not isinstance(edu, dict):
            continue
        school = (edu.get("institution_name") or "").strip()
        degree = (edu.get("degree") or "").strip()
        if not school:
            continue
        if degree:
            parts.append(f"{school} - {degree}")
        else:
            parts.append(school)
    return "; ".join(parts)


def _format_experience(experience: List[Dict[str, Any]]) -> str:
    parts = []
    for exp in experience:
        if not isinstance(exp, dict):
            continue
        title = (exp.get("position_title") or "").strip()
        company = (exp.get("company_name") or "").strip()
        if not (title or company):
            continue
        date_from = exp.get("date_from") or ""
        date_to = exp.get("date_to") or "Present"
        when = f" ({date_from} - {date_to})" if date_from else ""
        parts.append(f"{title} at {company}{when}".strip())
    return "; ".join(parts)


def _summarize_experience(experience: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Minimal experience array kept on the contact for downstream UI/exports."""
    out = []
    for exp in experience:
        if not isinstance(exp, dict):
            continue
        out.append({
            "title": exp.get("position_title") or "",
            "company": exp.get("company_name") or "",
            "date_from": exp.get("date_from") or "",
            "date_to": exp.get("date_to") or "",
        })
    return out


def _synthesize_id(email: str, linkedin: str, first: str, last: str, company: str) -> str:
    seed = (email or linkedin or f"{first}_{last}_{company}").strip().lower()
    if not seed:
        return ""
    return "coresignal:" + hashlib.sha1(seed.encode()).hexdigest()[:16]


def _contact_key(contact: Dict[str, Any]) -> str:
    """Dedup key — prefer LinkedIn URL, fall back to pdlId."""
    return (contact.get("LinkedIn") or "").strip().lower() or contact.get("pdlId", "")


# ---------------------------------------------------------------------------
# Direct people queries (Find contacts). These bypass _distinctive_variations
# and never require a job location as a person location.
# ---------------------------------------------------------------------------


def _current_company_clauses(company: str) -> List[Dict[str, Any]]:
    return [
        {"term": {"experience.active_experience": 1}},
        {"match_phrase": {"experience.company_name": company}},
    ]


def search_people(
    company: str,
    titles: List[str],
    keywords: Optional[List[str]] = None,
    city: Optional[str] = None,
    page_size: int = 10,
) -> List[int]:
    """ES ids of current employees of `company` holding any of `titles`.

    Every keyword must appear in the current role's position_title or
    department, or in the headline (bank titles are often just "Vice
    President"; the group shows up in department or headline).
    `city` (optional) is a match_phrase on location_full.
    """
    def current_role(extra: Dict[str, Any]) -> Dict[str, Any]:
        return {"nested": {"path": "experience", "query": {"bool": {
            "must": _current_company_clauses(company) + [extra]}}}}

    must: List[Dict[str, Any]] = [current_role({
        "bool": {
            "should": [{"match_phrase": {"experience.position_title": t}} for t in titles],
            "minimum_should_match": 1,
        }
    })]
    for kw in keywords or []:
        must.append({"bool": {"should": [
            current_role({"bool": {"should": [
                {"match_phrase": {"experience.position_title": kw}},
                {"match_phrase": {"experience.department": kw}},
            ], "minimum_should_match": 1}}),
            {"match_phrase": {"headline": kw}},
        ], "minimum_should_match": 1}})
    if city:
        must.append({"match_phrase": {"location_full": city}})
    return _search_ids({"query": {"bool": {"must": must}}}, page_size=page_size)


def collect_contacts(ids: List[int], limit: int) -> List[Dict[str, Any]]:
    """Collect at most `limit` profiles and normalize them to contacts."""
    profiles = _collect_profiles_parallel(list(ids)[:limit])
    contacts = [_normalize_to_contact(p) for p in profiles]
    return [c for c in contacts if c]


def find_person_by_name(full_name: str, company: str) -> Optional[Dict[str, Any]]:
    """Look up one named person currently at `company`; None if not found."""
    nested = {
        "nested": {
            "path": "experience",
            "query": {"bool": {"must": _current_company_clauses(company)}},
        }
    }
    query = {"query": {"bool": {"must": [
        {"match_phrase": {"full_name": full_name}},
        nested,
    ]}}}
    ids = _search_ids(query, page_size=1)
    if not ids:
        return None
    profiles = _collect_profiles_parallel(ids[:1])
    if not profiles:
        return None
    return _normalize_to_contact(profiles[0], target_company=company)
