"""
Job ranking utilities — pure Python, no Flask imports.
Deterministic pre-filtering, GPT-based ranking, and feedback adjustments.
"""
import re as _re
from datetime import datetime
from typing import Optional


# ---------------------------------------------------------------------------
# Data normalization helpers
# ---------------------------------------------------------------------------

def flatten_skills(skills_field) -> list[str]:
    if isinstance(skills_field, list):
        return [s for s in skills_field if isinstance(s, str)]
    if isinstance(skills_field, dict):
        flat = []
        for v in skills_field.values():
            if isinstance(v, list):
                flat.extend([s for s in v if isinstance(s, str)])
        return flat
    return []


def flatten_experience_title(title_field) -> str:
    if isinstance(title_field, str):
        return title_field
    if isinstance(title_field, dict):
        return title_field.get("name", "")
    return ""


# ---------------------------------------------------------------------------
# Field inference from careerTrack / major
# ---------------------------------------------------------------------------

# Explicit careerTrack → field key (same keys as MAJOR_FIELD_MAP values)
# These feed into FIELD_CATEGORY_MAP for matching against job categories.
CAREER_TRACK_MAP = {
    "investment banking": "finance",
    "finance": "finance",
    "banking": "finance",
    "consulting": "consulting",
    "management consulting": "consulting",
    "software engineering": "tech",
    "software": "tech",
    "engineering": "tech",
    "product management": "consulting",  # PM maps to consulting field (has product_management category)
    "product": "consulting",
    "data science": "data",
    "data analytics": "data",
    "data": "data",
    "marketing": "marketing",
    "growth": "marketing",
    "venture capital": "venture_capital",
    "private equity": "venture_capital",
    "private equity / vc": "venture_capital",
    "vc": "venture_capital",
    "pe": "venture_capital",
    "sales & trading": "finance",
    "corporate finance / fp&a": "finance",
}

MAJOR_FIELD_MAP = {
    "finance": "finance", "economics": "finance", "accounting": "finance",
    "investment": "finance", "computer science": "tech", "data science": "data",
    "information systems": "tech", "software": "tech", "electrical engineering": "tech",
    "marketing": "marketing", "business administration": "consulting",
    "management": "consulting", "statistics": "data", "mathematics": "data",
    "communications": "marketing", "psychology": "consulting",
    "real estate": "real_estate", "urban planning": "real_estate",
    "venture": "venture_capital", "entrepreneurship": "venture_capital",
}

FIELD_CATEGORY_MAP = {
    "finance":    ["finance_banking", "consulting"],
    "tech":       ["software_engineering", "data_science", "product_management"],
    "data":       ["data_science", "software_engineering", "product_management"],
    "marketing":  ["marketing_growth", "product_management"],
    "consulting": ["consulting", "finance_banking", "product_management"],
    "real_estate": ["real_estate", "finance_banking"],
    "venture_capital": ["venture_capital", "finance_banking", "consulting"],
}


def _safe_str(val) -> str:
    """Coerce a profile field to a safe string. Lists join on ', '; dicts/None/other return ''."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        return ", ".join(v for v in val if isinstance(v, str))
    return ""


def _safe_str_list(val) -> list[str]:
    """Coerce a profile field to a list of strings. Skips non-strings silently."""
    if val is None:
        return []
    if isinstance(val, str):
        return [val] if val else []
    if isinstance(val, list):
        return [v for v in val if isinstance(v, str) and v]
    return []


def infer_field(profile: dict) -> Optional[str]:
    # 1. Explicit careerTrack (highest priority).
    #    Read BOTH paths. AccountSettings — the only surface that can still set
    #    a career track — writes `careerTrack` flat at the top level and never
    #    writes a `goals` map, and the current onboarding flow writes
    #    `goals.careerTrack` as "" (career tracks were removed from onboarding;
    #    see OnboardingFlow's completion payload). Reading goals-only meant the
    #    user's explicitly chosen track never applied and this always fell
    #    through to the resume's major.
    career_track = _safe_str(
        (profile.get("goals") or {}).get("careerTrack")
        or profile.get("careerTrack")
    ).lower().strip()
    if career_track:
        category = CAREER_TRACK_MAP.get(career_track)
        if category:
            return category

    # 2. Fallback: infer from major
    education = (profile.get("resumeParsed") or {}).get("education", {}) or {}
    major = _safe_str(education.get("major") or profile.get("major")).lower().strip()
    for key, field in MAJOR_FIELD_MAP.items():
        if key in major:
            return field
    return None


def infer_preferred_type(profile: dict) -> Optional[str]:
    # 1. Explicit jobTypes from onboarding (highest priority)
    job_types = (profile.get("location") or {}).get("jobTypes") or []
    if job_types:
        has_intern = "Internship" in job_types
        has_ft = "Full-Time" in job_types
        if has_intern and has_ft:
            return None  # no preference — user wants both
        if has_intern:
            return "INTERNSHIP"
        if has_ft:
            return "FULLTIME"

    # 2. Fallback: infer from graduation year
    education = (profile.get("resumeParsed") or {}).get("education", {}) or {}
    grad_year = education.get("graduation_year") or profile.get("graduationYear")
    if not grad_year:
        return None
    try:
        return "FULLTIME" if int(grad_year) - datetime.now().year <= 1 else "INTERNSHIP"
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Category / title exclusions — filter out irrelevant blue-collar jobs
# ---------------------------------------------------------------------------

EXCLUDED_CATEGORIES = frozenset([
    "manufacturing", "construction", "healthcare", "retail",
    "food_service", "transportation", "agriculture",
])

EXCLUDED_TITLE_KEYWORDS = [
    "assembly", "manufacturing", "warehouse", "forklift", "cdl",
    "nursing", "medical assistant", "dental", "hvac", "electrician",
    "plumber", "truck driver", "cashier", "barista",
    "it support", "help desk", "desktop support",
    "technical support specialist", "field technician",
    "field service", "maintenance technician",
]

# Always excluded — clearly not entry-level regardless of graduation year
SENIOR_TITLE_KEYWORDS = [
    "sr. ", "sr ", "senior ", "lead ", "principal ", "staff ",
    "director", "vp ", "vice president", "head of",
    "managing director", "partner",
]

# "Partner" is excluded UNLESS it's one of these entry-level roles
_PARTNER_EXCEPTIONS = ("channel partner", "partner manager", "partner success")

# "Manager" is excluded UNLESS preceded by "product" or "program"
_MANAGER_EXCEPTIONS = ("product manager", "program manager")

NON_US_LOCATION_KEYWORDS = [
    # Countries — expanded 2026-07-15 to match normalizer._EXCLUDED_COUNTRIES.
    # Sid explicitly flagged Kuala Lumpur/Bulgaria slipping through the feed;
    # the earlier list was missing those + ~30 other common non-US countries.
    # Europe
    "united kingdom", "uk", "germany", "france", "netherlands", "ireland",
    "poland", "spain", "italy", "sweden", "denmark", "finland", "norway",
    "belgium", "switzerland", "austria", "portugal", "greece",
    "czech republic", "czechia", "hungary", "romania", "bulgaria",
    "ukraine", "belarus", "estonia", "latvia", "lithuania", "slovakia",
    "slovenia", "croatia",
    # Asia-Pacific
    "india", "china", "japan", "singapore", "malaysia", "thailand",
    "philippines", "vietnam", "indonesia", "pakistan", "bangladesh",
    "sri lanka", "south korea", "korea", "taiwan", "hong kong",
    "australia", "new zealand",
    # Americas (non-US)
    "canada", "brazil", "mexico", "argentina", "colombia", "chile",
    "peru", "ecuador", "venezuela", "uruguay", "costa rica", "panama",
    # Middle East / Africa
    "israel", "uae", "united arab emirates", "saudi arabia", "turkey",
    "egypt", "south africa", "nigeria", "kenya", "morocco", "ghana",
    # Indian cities
    "bengaluru", "bangalore", "mumbai", "delhi", "hyderabad",
    "chennai", "pune", "kolkata", "ahmedabad", "noida", "gurgaon",
    # Other international cities
    "toronto", "vancouver", "montreal", "london", "manchester",
    "berlin", "amsterdam", "paris", "sydney", "melbourne",
    "tokyo", "beijing", "shanghai", "seoul", "taipei", "bangkok",
    "kuala lumpur", "jakarta", "manila", "ho chi minh",
    "mexico city", "sao paulo", "buenos aires", "santiago",
    "dublin", "warsaw", "prague", "budapest", "sofia", "bucharest",
    "tel aviv", "dubai", "istanbul", "cairo", "cape town", "lagos",
]

# "uk" needs word-boundary matching to avoid false positives on substrings
_UK_RE = _re.compile(r"\buk\b", _re.IGNORECASE)


def _get_grad_year(profile: dict) -> Optional[int]:
    """Extract graduation year from profile as int, or None."""
    education = (profile.get("resumeParsed") or {}).get("education", {}) or {}
    raw = education.get("graduation_year") or profile.get("graduationYear")
    if not raw:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def _is_excluded(job: dict) -> bool:
    """Return True if a job should be excluded from ranking entirely."""
    if job.get("expired") is True:
        return True
    if job.get("category") in EXCLUDED_CATEGORIES:
        return True
    title_lower = (job.get("title") or "").lower()
    if any(kw in title_lower for kw in EXCLUDED_TITLE_KEYWORDS):
        return True
    # Always exclude senior-level titles (not entry-level)
    for kw in SENIOR_TITLE_KEYWORDS:
        if kw in title_lower:
            # "partner" has entry-level exceptions (e.g. "partner manager")
            if kw == "partner" and any(exc in title_lower for exc in _PARTNER_EXCEPTIONS):
                continue
            return True
    # Exclude "manager" unless it's "product manager" or "program manager"
    if "manager" in title_lower and not any(exc in title_lower for exc in _MANAGER_EXCEPTIONS):
        return True
    return False


def _normalize_location(loc) -> str:
    """Coerce location to a string — handles dict, str, or None."""
    if not loc:
        return ""
    if isinstance(loc, str):
        return loc
    if isinstance(loc, dict):
        parts = [loc.get("addressLocality"), loc.get("addressRegion"), loc.get("addressCountry")]
        return ", ".join(str(p) for p in parts if p)
    return str(loc)


def _is_non_us(job: dict) -> bool:
    """Return True if job is based in a non-US location.

    Keeps jobs where location is purely "Remote" or "Remote - USA" etc.
    Excludes jobs like "Remote, Singapore" where the primary location is international.
    """
    location_lower = _normalize_location(job.get("location")).lower()
    # Check keyword list AND word-boundary regex for short codes like "uk"
    has_international = (
        any(kw in location_lower for kw in NON_US_LOCATION_KEYWORDS)
        or bool(_UK_RE.search(location_lower))
    )
    if not has_international:
        return False
    # Pure remote with no international qualifier → keep
    if job.get("remote_derived") and location_lower.strip() in ("remote", "remote - usa", "remote - us"):
        return False
    return True


# ---------------------------------------------------------------------------
# Deterministic pre-filter
# ---------------------------------------------------------------------------

def deterministic_score(job: dict, profile: dict) -> float:
    score = 0.0
    user_field = infer_field(profile)
    preferred_type = infer_preferred_type(profile)
    user_skills = set(s.lower() for s in flatten_skills(
        (profile.get("resumeParsed") or {}).get("skills", [])
    ))

    # Field alignment (careerTrack or major → job category)
    if user_field and job.get("category") in FIELD_CATEGORY_MAP.get(user_field, []):
        score += 40
    if preferred_type and job.get("type") == preferred_type:
        score += 30
    elif not preferred_type:
        score += 15
    if user_skills:
        # Phase 1: prefer Firecrawl-structured requirements list over the noisy
        # description blob when available (higher signal: requirements is a
        # short, canonical list rather than 8000 chars of fluff).
        structured = job.get("structured") or {}
        reqs = structured.get("requirements") or []
        if isinstance(reqs, list) and reqs:
            reqs_text = " ".join(r for r in reqs if isinstance(r, str)).lower()
            nice = structured.get("nice_to_have") or []
            if isinstance(nice, list):
                reqs_text += " " + " ".join(n for n in nice if isinstance(n, str)).lower()
            score += min(sum(1 for s in user_skills if s in reqs_text) * 6, 30)
        else:
            desc_lower = job.get("description_raw", "").lower()
            score += min(sum(1 for s in user_skills if s in desc_lower) * 4, 20)

    # Graduation year fit
    education = (profile.get("resumeParsed") or {}).get("education", {}) or {}
    grad_year = education.get("graduation_year") or profile.get("graduationYear")
    if grad_year:
        try:
            years_left = int(grad_year) - datetime.now().year
            if years_left <= 1 and job.get("type") == "FULLTIME":
                score += 10
            elif years_left >= 2 and job.get("type") == "INTERNSHIP":
                score += 10
            else:
                score += 4
        except (ValueError, TypeError):
            pass

    # Dream company bonus. Read both paths — AccountSettings writes
    # `dreamCompanies` flat at the top level and never writes a `goals` map, so
    # goals-only meant this bonus could not fire for anyone. job_ranker_signals
    # already reads both, which is why the +25 boost worked while this +15 did
    # not.
    dream_companies = _safe_str_list(
        (profile.get("goals") or {}).get("dreamCompanies")
        or profile.get("dreamCompanies")
    )
    if dream_companies:
        job_company = _safe_str(job.get("company")).lower().strip()
        if job_company and any(dc.lower().strip() in job_company or job_company in dc.lower().strip()
                               for dc in dream_companies):
            score += 15

    # Location preference bonus. preferredLocation is a LIST for multi-city
    # users (AccountSettings assigns careerInfo.preferredLocations to it), and
    # _safe_str joins lists on ", " — so ["New York", "Chicago"] became the
    # string "new york, chicago", which is never a substring of a real job
    # location. Match any one of the preferred locations instead.
    pref_locations = [p.strip().lower() for p in
                      _safe_str_list((profile.get("location") or {}).get("preferredLocation"))
                      if p.strip()]
    job_location = _normalize_location(job.get("location")).lower()
    if job.get("remote_derived") or "remote" in job_location:
        score += 5
    if any(p in job_location for p in pref_locations if p not in ("", "remote")):
        score += 10

    return score


def prefilter_candidates(jobs: list[dict], profile: dict, top_n: int = 30) -> list[dict]:
    import logging
    _logger = logging.getLogger(__name__)

    # Exclude irrelevant categories, titles, senior roles, and non-US jobs
    excluded_count = 0
    non_us_count = 0
    eligible = []
    for j in jobs:
        if _is_excluded(j):
            excluded_count += 1
        elif _is_non_us(j):
            non_us_count += 1
        else:
            eligible.append(j)
    _logger.info("Excluded %d senior/irrelevant jobs, %d non-US jobs from %d total", excluded_count, non_us_count, len(jobs))

    # Score each job defensively — one bad job (malformed field) shouldn't tank the rerank
    scored_raw = []
    score_errors = 0
    for job in eligible:
        try:
            scored_raw.append((job, deterministic_score(job, profile)))
        except Exception as e:
            score_errors += 1
            if score_errors <= 3:
                _logger.warning("deterministic_score failed for job_id=%s: %s", job.get("job_id"), e)
    if score_errors:
        _logger.warning("Skipped %d jobs with scoring errors", score_errors)
    scored = sorted(scored_raw, key=lambda x: x[1], reverse=True)

    # Apply minimum score threshold to avoid sending junk to GPT
    MIN_RESULTS = 20
    filtered = [(job, s) for job, s in scored if s >= 15]
    if len(filtered) < MIN_RESULTS:
        filtered = [(job, s) for job, s in scored if s >= 10]
    if len(filtered) < MIN_RESULTS:
        filtered = scored[:MIN_RESULTS]

    return [job for job, _ in filtered[:top_n]]


# ---------------------------------------------------------------------------
# GPT ranking
# ---------------------------------------------------------------------------

def _mark_unranked(jobs: list[dict]) -> list[dict]:
    """Mark all jobs as unranked and return them."""
    for job in jobs:
        job["match_score"] = None
        job["match_reason"] = None
        job["ranked"] = False
    return jobs


GPT_RANK_COUNT = 20  # Send only top N to GPT; rest get deterministic scores


# Generic/uninformative phrases the GPT model falls back to when it can't find
# a real signal. We replace these with data-derived reasons in post-processing.
_BANNED_REASON_SUBSTRINGS = (
    "your skill", "your profile", "your background", "your resume",
    "matches your", "fits your", "aligns with your", "matched your",
    "strong match", "good match", "great match", "great fit",
    "perfect fit", "good fit", "well-suited", "well suited",
    "matched by skills and profile", "matches skills and profile",
)


def _is_generic_reason(reason) -> bool:
    """Return True if the reason is too vague to be useful to the user."""
    if not isinstance(reason, str):
        return True
    r = reason.lower().strip()
    if len(r) < 8:
        return True
    return any(p in r for p in _BANNED_REASON_SUBSTRINGS)


def _derive_reason(job: dict, profile: dict) -> str:
    """Build a specific match reason from actual job + profile data.

    Used both for jobs in the deterministic-fallback tier (positions 21-50,
    not sent to GPT) AND as the replacement when GPT returns a banned/generic
    phrase. Always tries to mention a real attribute: team, level, a specific
    matched skill, or the user's major.
    """
    structured = job.get("structured") or {}

    # 1) specific skill match against the structured requirements
    try:
        user_skills_list = flatten_skills(
            (profile.get("resumeParsed") or {}).get("skills", [])
        )
    except Exception:
        user_skills_list = []
    user_skills = {s.lower() for s in user_skills_list if isinstance(s, str) and len(s) > 2}
    reqs = structured.get("requirements") or []
    if user_skills and isinstance(reqs, list):
        for req in reqs:
            if not isinstance(req, str):
                continue
            req_lower = req.lower()
            for skill in user_skills:
                if skill in req_lower:
                    return f"Requires {skill} — on your resume"

    # 2) the job's team/department
    team = structured.get("team")
    if isinstance(team, str) and team.strip():
        return f"{team.strip()} role · matches your interests"

    # 3) experience level + type ("Entry-level internship")
    level = structured.get("experience_level")
    typ = job.get("type") or ""
    if isinstance(level, str) and level.strip():
        type_label = {
            "INTERNSHIP": "internship",
            "FULLTIME": "full-time role",
            "PARTTIME": "part-time role",
        }.get(typ, "role")
        return f"{level.strip()} {type_label}"

    # 4) major
    edu = (profile.get("resumeParsed") or {}).get("education") or {}
    major = (edu.get("major") if isinstance(edu, dict) else None) or profile.get("major")
    if isinstance(major, str) and major.strip():
        return f"{major.strip()} fit"

    # 5) last resort — at least cite a concrete substring from the title
    title = job.get("title") or ""
    if isinstance(title, str) and title.strip():
        first_word = title.strip().split()[0]
        return f"{first_word} role"

    return "Recent posting"


def rank_with_gpt(jobs: list[dict], profile: dict) -> list[dict]:
    from backend.app.services.openai_client import client
    from openai import RateLimitError
    import json
    import re
    import time
    import logging

    logger = logging.getLogger(__name__)

    # Split: top 20 go to GPT, remainder get deterministic scores
    gpt_jobs = jobs[:GPT_RANK_COUNT]
    fallback_jobs = jobs[GPT_RANK_COUNT:]

    education = (profile.get("resumeParsed") or {}).get("education", {}) or {}
    skills = flatten_skills((profile.get("resumeParsed") or {}).get("skills", []))
    experience = (profile.get("resumeParsed") or {}).get("experience", []) or []

    exp_lines = ", ".join([
        f'{flatten_experience_title(e.get("title", ""))} at {e.get("company", "")}'
        for e in experience[:3]
        if flatten_experience_title(e.get("title", "")) and e.get("company")
    ]) or "None listed"

    # Same dual-path read as deterministic_score / infer_field: these live at
    # the top level for anyone who set them in AccountSettings, so goals-only
    # meant the GPT reranker was never told the user's track or dream companies.
    goals = profile.get("goals") or {}
    career_track = goals.get("careerTrack") or profile.get("careerTrack") or ""
    dream_companies = (goals.get("dreamCompanies")
                       or profile.get("dreamCompanies") or [])
    pref_location = (profile.get("location") or {}).get("preferredLocation", "")

    profile_str = f"""STUDENT PROFILE:
- Major: {education.get("major") or profile.get("major", "Not specified")}
- Graduation: {education.get("graduation_year") or profile.get("graduationYear", "Not specified")}
- University: {education.get("school") or profile.get("university", "Not specified")}
- Career goal: {career_track or "Not specified"}
- Dream companies: {", ".join(dream_companies) or "None listed"}
- Preferred location: {pref_location or "Not specified"}
- Skills: {", ".join(skills[:15]) or "None listed"}
- Experience: {exp_lines}"""

    jobs_str = "JOBS:\n"
    for job in gpt_jobs:
        title = (job.get("title") or "")[:60]
        company = (job.get("company") or "")[:30]
        location = (_normalize_location(job.get("location")) or "")[:30]
        # Phase 1: prefer the canonical requirements list from Firecrawl over
        # the noisy 100-char description excerpt. Tighter, higher-signal context
        # for GPT-4o-mini.
        structured = job.get("structured") or {}
        reqs = structured.get("requirements") or []
        if isinstance(reqs, list) and reqs:
            req_summary = "; ".join(str(r) for r in reqs[:3] if r)[:200]
            level = structured.get("experience_level") or ""
            level_tag = f" | {level}" if level else ""
            jobs_str += (
                f'[{job["job_id"]}] {title} @ {company} | {location}\n'
                f'  {job.get("type")}{level_tag} | requires: {req_summary}\n'
            )
        else:
            desc = (job.get("description_raw") or "")[:100]
            jobs_str += (
                f'[{job["job_id"]}] {title} @ {company} | {location}\n'
                f'  {job.get("type")} | {desc}\n'
            )

    system_prompt = """You are a job matching assistant for college students.
Rank jobs by fit: 1) Field alignment with major 2) Job type fit 3) Skills match 4) Seniority fit.

match_reason rules (CRITICAL):
- Max 12 words. Concrete and specific to THIS job.
- MUST cite either (a) a specific skill from their resume, (b) their major, OR
  (c) a specific requirement from the job posting that they meet.
- BANNED phrases (do not use, ever): "your skills", "your profile",
  "matches your", "fits your", "aligns with your background", "strong match",
  "good match", "great fit", "perfect fit".
- BAD examples (too vague):
    * "Matches your skills and profile"
    * "Strong match for your background"
    * "Good fit for your resume"
- GOOD examples (specific):
    * "Python + scikit-learn role — matches your ML coursework"
    * "Econ major fits this banking analyst posting"
    * "Tableau experience matches their BI stack"
    * "Entry-level data science — aligns with DSCI 351"

Return ONLY a JSON array: [{"job_id":"...","match_score":85,"match_reason":"..."}]
Include every job_id. Order by match_score descending."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": profile_str + "\n\n" + jobs_str},
    ]

    def _call_gpt():
        return client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=8000,
            temperature=0.3,
        )

    try:
        try:
            response = _call_gpt()
        except RateLimitError:
            logger.warning("GPT ranking hit 429 — retrying in 10s")
            time.sleep(10)
            response = _call_gpt()

        raw = re.sub(
            r"```(?:json)?", "",
            response.choices[0].message.content.strip()
        ).strip().rstrip("`").strip()
        ranking_map = {
            item["job_id"]: {
                "match_score": int(item.get("match_score", 50)),
                "match_reason": item.get("match_reason", "")
            }
            for item in json.loads(raw) if "job_id" in item
        }

        # Apply GPT scores to top 20. Post-process reasons: replace any
        # generic/banned phrase the model fell back to with a data-derived
        # specific reason so the SPA never shows "Matched your skills and
        # profile"-style filler.
        ranked = []
        scrubbed = 0
        for job in gpt_jobs:
            if job["job_id"] in ranking_map:
                gpt_reason = ranking_map[job["job_id"]]["match_reason"]
                if _is_generic_reason(gpt_reason):
                    job["match_reason"] = _derive_reason(job, profile)
                    scrubbed += 1
                else:
                    job["match_reason"] = gpt_reason
                job["match_score"] = ranking_map[job["job_id"]]["match_score"]
                job["ranked"] = True
            else:
                job["match_score"] = None
                job["match_reason"] = None
                job["ranked"] = False
            ranked.append(job)
        if scrubbed:
            logger.info("Scrubbed %d generic GPT reasons → derived", scrubbed)

        # Apply deterministic scores (scaled to 0-100) for jobs 21-50.
        # Reasons are derived from real data, not a generic string.
        if fallback_jobs:
            det_scored = [(j, deterministic_score(j, profile)) for j in fallback_jobs]
            max_det = max((s for _, s in det_scored), default=1) or 1
            for job, det_s in det_scored:
                job["match_score"] = int((det_s / max_det) * 49)
                job["match_reason"] = _derive_reason(job, profile)
                job["ranked"] = True
                ranked.append(job)

        return sorted(ranked, key=lambda j: j.get("match_score") or 0, reverse=True)
    except RateLimitError:
        logger.warning("GPT ranking hit 429 twice — returning unranked")
        return _mark_unranked(jobs)
    except Exception as e:
        logger.warning(f"GPT ranking failed: {e}")
        return _mark_unranked(jobs)


# ---------------------------------------------------------------------------
# Feedback adjustments
# ---------------------------------------------------------------------------

def apply_feedback_adjustments(
    ranked_jobs: list[dict],
    preferences: list[dict],
    user_signals=None,
) -> list[dict]:
    """Apply user feedback (liked/disliked categories) and Phase 2 signal
    boosts to the ranked job list.

    The category-level liked/disliked counters are unchanged. Phase 2 adds:
      - dream/target company hit  → up to +25
      - alumni at company         → +20
      - saved-company affinity    → +10

    These boosts are added to match_score with a 100 cap so the UI display
    stays in the familiar 0-100 range, but tie-breaking happens via the
    `_signal_boost` field (which is uncapped) so two saturated jobs still
    sort by signal strength.
    """
    from collections import Counter

    liked = Counter()
    disliked = Counter()
    hidden = set()

    for p in preferences:
        cat = p.get("category", "")
        if p.get("signal") == "positive":
            liked[cat] += 1
        elif p.get("signal") == "negative":
            disliked[cat] += 1
            hidden.add(p.get("job_id", ""))

    adjusted = []
    for job in ranked_jobs:
        if job["job_id"] in hidden:
            continue

        signal_boost = 0
        if user_signals is not None:
            try:
                signal_boost, _ = user_signals.boost(job)
            except Exception:
                signal_boost = 0

        if job.get("ranked") and job.get("match_score") is not None:
            score = job["match_score"]
            cat = job.get("category", "")
            score += min(liked.get(cat, 0) * 5, 15)
            score -= min(disliked.get(cat, 0) * 8, 24)
            score += signal_boost
            job["match_score"] = max(0, min(100, score))

        # Stored separately so the secondary sort can use the uncapped value.
        job["_signal_boost"] = signal_boost
        adjusted.append(job)

    # Primary sort: match_score (capped). Secondary sort: uncapped signal
    # boost so a saturated dream-company hit still outranks a saturated
    # non-dream hit.
    return sorted(
        adjusted,
        key=lambda j: (j.get("match_score") or 0, j.get("_signal_boost", 0)),
        reverse=True,
    )


def cap_per_company(jobs: list[dict], max_per_company: int = 3) -> list[dict]:
    """Limit results to max N jobs per company, keeping highest-scored."""
    from collections import defaultdict
    counts: dict[str, int] = defaultdict(int)
    result = []
    for job in jobs:
        company_key = (job.get("company") or "").lower().strip()
        if counts[company_key] < max_per_company:
            result.append(job)
            counts[company_key] += 1
    return result


# ---------------------------------------------------------------------------
# Phase 1: composite signals + natural bucket assignment
# ---------------------------------------------------------------------------
# Reads the active profile from job_ranking_config and composes four signals
# (relevance, landability, pipeline, discovery) into a single composite that
# replaces match_score. Default config weights are { relevance: 1.0, others:
# 0.0 } so the composite reduces to relevance exactly and match_score is
# numerically unchanged. Bucket tags are emitted unconditionally for
# telemetry but the renderer ignores them while render_mode is "legacy".
#
# Hard-drop on landability < hard_drop.landability_below replaces the old
# level + location gates (intent_gates.apply_intent_gates is patched to
# skip those when landability_below > 0).

import logging as _logging
_logger = _logging.getLogger(__name__)


def _normalize_company_slug(name) -> str:
    """Same shape as discovery._normalize_company. Duplicated to avoid an
    upstream import dependency on the discovery reader."""
    if not isinstance(name, str):
        return ""
    return _re.sub(r"[^a-z0-9]+", "", name.lower())


def fetch_discovery_scores_chunked(
    slugs: list, chunk_size: int = 100
) -> dict:
    """Batched read of company_signals via Firestore BatchGetDocuments.

    Uses Client.get_all(refs) not where("__name__", "in", chunk) so we
    do not hit the 10/30 cap on `in` queries. Chunks at 100 refs per call
    to stay safely under the ~4MB gRPC message size limit.

    Returns {slug: discovery_score}. Companies with no entry are absent
    from the result (caller treats them as None).
    """
    try:
        from app.extensions import get_db
    except Exception:
        return {}
    db = get_db()
    if db is None:
        return {}
    unique = sorted({s for s in slugs if isinstance(s, str) and s})
    if not unique:
        return {}
    out: dict = {}
    for i in range(0, len(unique), chunk_size):
        chunk = unique[i:i + chunk_size]
        refs = [db.collection("company_signals").document(s) for s in chunk]
        try:
            for snap in db.get_all(refs):
                if not snap.exists:
                    continue
                data = snap.to_dict() or {}
                score = data.get("discovery_score")
                if isinstance(score, int):
                    out[snap.id] = score
        except Exception as e:
            _logger.warning("discovery batch read failed: %s", e)
            continue
    return out


def score_signals_and_bucket(
    job: dict, intent: dict, profile: dict, relevance
) -> dict:
    """
    Compute four signals + natural bucket + composite for one job.

    Returns:
      {
        "signals":   {"relevance": int|None, "landability": int,
                      "pipeline": int, "discovery": int|None},
        "bucket":    "strong" | "reach" | "hidden",
        "composite": int | None,
        "drop":      bool,
      }
    """
    from app.utils.landability import score_landability
    from app.utils.discovery import get_discovery_score

    lab = score_landability(job, intent, profile)["score"]
    disc = get_discovery_score(job.get("company") or "")
    pipe = 0  # phase 1: pipeline signal stays zero

    signals = {
        "relevance":   int(relevance) if relevance is not None else None,
        "landability": int(lab),
        "pipeline":    pipe,
        "discovery":   int(disc) if disc is not None else None,
    }

    # Natural bucket assignment (config-driven thresholds)
    assign = profile.get("bucket_assignment", {}) or {}
    reach_below = int(assign.get("reach_when_landability_below", 45))
    hidden_disc_above = int(assign.get("hidden_when_discovery_above", 70))
    hidden_pipe_above = int(assign.get("hidden_when_pipeline_above", 50))

    if lab < reach_below:
        bucket = "reach"
    elif (
        disc is not None
        and disc >= hidden_disc_above
        and pipe >= hidden_pipe_above
    ):
        bucket = "hidden"
    else:
        bucket = "strong"

    # Composite = weighted avg of non-null signals using this bucket's weights
    weights = (profile.get("within_bucket_weights", {}) or {}).get(bucket, {}) or {}
    num = 0.0
    den = 0.0
    for k, v in signals.items():
        if v is None:
            continue
        w = float(weights.get(k, 0.0))
        if w <= 0:
            continue
        num += v * w
        den += w
    composite = int(round(num / den)) if den > 0 else None

    # Hard drops (gated implicitly by landability_below > 0 in the config)
    hd = profile.get("hard_drop", {}) or {}
    lab_floor = int(hd.get("landability_below", 0))
    rel_floor = int(hd.get("relevance_below", 0))

    drop = False
    if lab_floor > 0 and lab < lab_floor:
        drop = True
    if rel_floor > 0 and signals["relevance"] is not None and signals["relevance"] < rel_floor:
        drop = True

    return {
        "signals":   signals,
        "bucket":    bucket,
        "composite": composite,
        "drop":      drop,
    }


def attach_signals_and_buckets(
    jobs: list, profile: dict, user_profile: dict,
    relevance_by_id=None,
) -> list:
    """
    Compute signals + bucket + composite for each job, drop hard-drops,
    attach signals/bucket/composite onto each surviving job dict.

    relevance_by_id: optional map {job_id: relevance_int|None}. When
    provided, used as the relevance signal. When omitted, each job's
    existing match_score is treated as relevance.

    match_score on the returned jobs is REPLACED by composite (which
    equals relevance exactly under phase-1 weights). When composite is
    None (e.g. no resume), match_score is left as-is.
    """
    from app.utils.intent_gates import build_user_intent
    from app.utils.discovery import prime_cache

    if not jobs:
        return []

    intent = build_user_intent(user_profile or {})

    # Pre-fetch discovery for unique companies in one BatchGetDocuments call
    slugs = [_normalize_company_slug(j.get("company") or "") for j in jobs]
    discovery_map = fetch_discovery_scores_chunked(slugs)
    if discovery_map:
        prime_cache(discovery_map)

    out = []
    for job in jobs:
        rel = (relevance_by_id or {}).get(job.get("job_id"))
        if rel is None and relevance_by_id is None:
            rel = job.get("match_score")
        result = score_signals_and_bucket(job, intent, profile, rel)
        if result["drop"]:
            continue
        job["signals"] = result["signals"]
        job["bucket"] = result["bucket"]
        if result["composite"] is not None:
            job["match_score"] = result["composite"]
        out.append(job)
    return out
