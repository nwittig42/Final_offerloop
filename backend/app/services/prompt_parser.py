"""
Prompt parser service - converts natural language prompts into structured search filters.

Single canonical parser (parse_search_prompt_structured) with adapter functions
for legacy callers that expect different output shapes.
"""
import json
import hashlib
import os
import threading
import time
from typing import Dict, Any, List
from app.services.openai_client import get_openai_client

# Default timeout for OpenAI prompt parsing (keep prompt search fast)
PROMPT_PARSE_TIMEOUT = 10.0
EXPANSION_TIMEOUT = 8.0

# Shared empty result template
_EMPTY_STRUCTURED = {
    "confidence": "low",
    "original_prompt": "",
    "company_context": "",
    "companies": [],
    "locations": [],
    "schools": [],
    "industries": [],
    "title_variations": [],
}

# Manual TTL cache for parsed prompts — same pattern as company_search.py
_parse_cache: dict[str, tuple[float, dict]] = {}
_parse_cache_lock = threading.Lock()
_PARSE_CACHE_TTL = 3600   # 1 hour
_PARSE_CACHE_MAX = 500

# Cache for industry/title expansion (separate keyspace; same TTL/limits)
_expand_cache: dict[str, tuple[float, dict]] = {}
_expand_cache_lock = threading.Lock()
_EXPAND_CACHE_TTL = 3600
_EXPAND_CACHE_MAX = 500


def _load_pdl_industry_taxonomy() -> frozenset:
    """Load PDL canonical industry enum from JSON. Frozen at import."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "pdl_industries.json"
    )
    try:
        with open(path, "r", encoding="utf-8") as f:
            return frozenset(s.lower().strip() for s in json.load(f) if s)
    except Exception as e:
        print(f"[PromptParser] Failed to load PDL industry taxonomy: {e}")
        return frozenset()


PDL_INDUSTRY_TAXONOMY = _load_pdl_industry_taxonomy()


def _make_empty(prompt: str, error: str = "") -> Dict[str, Any]:
    """Return a valid empty structured result."""
    out = dict(_EMPTY_STRUCTURED)
    out["original_prompt"] = prompt
    if error:
        out["error"] = error
    return out


def parse_search_prompt_structured(prompt: str) -> Dict[str, Any]:
    """
    Parse a natural language search prompt into structured params with company-specific
    job titles. Uses OpenAI with structured JSON output.

    This is the SINGLE canonical parser. All other parse functions delegate to this one.

    Returns:
        Dict with: original_prompt, company_context, companies [{name, matched_titles}],
        locations, schools, seniority_levels, industries, confidence ("high"|"low"),
        title_variations (flat list). On error: includes "error" key.
    """
    client = get_openai_client()
    if not client:
        return _make_empty(prompt, "OpenAI client not available")

    # --- Cache lookup ---
    cache_key = hashlib.md5(prompt.lower().strip().encode()).hexdigest()
    with _parse_cache_lock:
        if cache_key in _parse_cache:
            ts, cached_result = _parse_cache[cache_key]
            if time.time() - ts < _PARSE_CACHE_TTL:
                print(f"[PromptParser] Cache hit for prompt (MD5: {cache_key})")
                return cached_result
            else:
                del _parse_cache[cache_key]

    print("[PromptParser] Cache miss — calling OpenAI")

    system_prompt = """You are a contact search query parser. Extract structured search parameters from a user's natural language prompt for finding professional contacts.

RULES:
1. Extract companies, locations, schools, and seniority when mentioned.
2. For EACH company mentioned, generate the REAL job titles that actual employees at that company have on LinkedIn — not job posting titles, not generic titles. Use company-specific naming:
   - Amazon: use "SDE", "Software Development Engineer", not "Software Engineer".
   - Google: use "Software Engineer", "Product Manager", not "SDE".
   - Meta: use "Software Engineer" with team suffixes where relevant, "Product Manager".
   - Finance: "Investment Banking Analyst", "Investment Banking Associate" — match the seniority the user specified.
3. Title variations should primarily use domain-qualified titles (e.g., "Investment Banking Analyst", "Software Engineer"). ALSO include the standalone generic version as a fallback at the end of the list (e.g., "Analyst", "Engineer", "Associate"). Many real LinkedIn profiles only have the generic title, so including both ensures broad coverage.
4. Include seniority only when combined with the domain: e.g. "investment banking associate", "software engineering manager".
5. RESPECT the seniority level the user specified. If they say "analysts", generate analyst-level titles ONLY — do NOT add senior titles like "Vice President", "Managing Director", "Partner", "Principal" unless the user explicitly asks for them. If they say "bankers" (generic), include all levels. If they say "associates", include associate-level only. Match the user's intent.
6. Include closely related roles: if user says "PM", include "Product Manager", "Technical Program Manager", "Program Manager".
7. Expand shorthand:
   - "FAANG" → Google, Apple, Meta, Amazon, Netflix
   - "Big 4" → Deloitte, PwC, EY, KPMG
   - "MBB" → McKinsey, Bain, BCG
8. Expand location shorthand: "NYC" → "New York", "SF" → "San Francisco", "LA" → "Los Angeles", "DC" → "Washington".
9. Set confidence to "low" ONLY when the prompt has no specifics at all — no job titles/roles, no companies, no schools, and no location. Examples of LOW confidence: "find me people", "help", "good contacts". Examples of HIGH confidence: "Software engineers from USC" (title + school), "Product managers in NYC" (title + location), "People at Google" (company), "USC alumni in consulting" (school + industry). If the prompt has at least one of: title_variations, companies, schools, or locations, set confidence to "high".
10. title_variations must be a flat, deduplicated list of ALL job titles across all companies for use in search.
11. When a company is specified, generate title variations that people ACTUALLY use at that company, not generic industry titles. Think about what the company does and what titles exist there. If the user's role description doesn't match what the company does, interpret the user's INTENT — they probably want to connect with people in a related function at that company. Examples:
   - "Investment bankers at Bain" → Bain is a consulting firm, not an investment bank. The user likely wants finance-related or client-facing roles at Bain. Generate titles like: "Consultant", "Associate Consultant", "Manager", "Senior Manager", "Partner".
   - "Investment banking analysts at Goldman Sachs" → Goldman IS an investment bank. User said "analysts" so generate analyst-level only: "Investment Banking Analyst", "Analyst", "Financial Analyst". Do NOT include "Vice President" or "Managing Director" — the user specified the seniority.
   - "Investment bankers at Goldman Sachs" → User said "bankers" (generic), so include all levels: "Investment Banking Analyst", "Investment Banking Associate", "Vice President", "Managing Director", "Analyst".
   - "Engineers at McKinsey" → McKinsey is a consulting firm that does have engineers. Generate: "Software Engineer", "Data Engineer", "Engineering Manager".
   Always think about what titles actually exist at the specified company before generating variations.

12. If the user mentions an industry (e.g. "consulting", "finance", "tech", "healthcare"), include it in the industries array. Use PDL-compatible industry labels when possible: "financial services", "technology", "management consulting", "health care", "real estate", "education", "media", "legal services", "accounting", "marketing and advertising".
13. For matched_titles in the companies array and for title_variations, always include BOTH domain-specific titles AND their generic fallbacks. Examples:
    - "Investment Banking Analyst" → also include "Analyst"
    - "Software Development Engineer" → also include "Software Engineer", "Engineer"
    - "Management Consultant" → also include "Consultant"
    - "Product Manager" → also include "PM"
    Place domain-specific titles first, generic fallbacks last.
14. company_traits (OFF-58): when the user describes the KIND of company instead of (or as well as) naming one, e.g. "tech startups", "early-stage fintech", "seed-stage companies", "boutique investment banks", "big tech", "Fortune 500", "unicorns", "family offices", fill company_traits so the search can filter on the company itself. Otherwise set it to null. Fields (use null for anything the user didn't imply):
    - label: the user's own phrase, short, e.g. "tech startups"
    - min_employees / max_employees: integers. startup ~ 2-500, early-stage/seed ~ 2-100, boutique ~ 5-500, big/large/Fortune 500 ~ min 5000
    - company_types: LinkedIn company types, e.g. ["Privately Held"] for startups and boutiques, ["Public Company"] for public or Fortune 500
    - founded_after: year as an integer, e.g. 2012 for startups, 2018 for early-stage
    - funded_after: "YYYY-MM-DD" of the latest funding round, for venture-backed or recently funded companies, e.g. "2022-01-01"
    - industries: 2 to 6 LinkedIn industry names, e.g. "Software Development", "Technology, Information and Internet", "IT Services and IT Consulting", "Financial Services", "Investment Banking", "Venture Capital and Private Equity Principals", "Biotechnology Research", "Hospitals and Health Care". Leave empty when the industry is unspecified.
    Example: "USC alumni at tech startups in SF" -> schools ["University of Southern California"], locations ["San Francisco"], companies [], company_traits {"label": "tech startups", "min_employees": 2, "max_employees": 500, "company_types": ["Privately Held"], "founded_after": 2012, "funded_after": null, "industries": ["Software Development", "Technology, Information and Internet", "IT Services and IT Consulting"]}.

Return ONLY valid JSON with this exact structure (no markdown, no explanation):
{
  "original_prompt": "<user prompt>",
  "company_context": "<brief description of what the specified company/companies do, e.g. 'Bain & Company is a management consulting firm'. Empty string if no company specified.>",
  "companies": [{"name": "<company name>", "matched_titles": ["<title1>", "<title2>"]}],
  "locations": ["<location1>", "<location2>"],
  "schools": [],
  "industries": [],
  "confidence": "high" or "low",
  "title_variations": ["<title1>", "<title2>", ...],
  "company_traits": null or {"label": "", "min_employees": null, "max_employees": null, "company_types": [], "founded_after": null, "funded_after": null, "industries": []}
}"""

    user_prompt = f'Extract search parameters from this prompt:\n\n"{prompt}"'

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=1200,
            temperature=0.2,
            response_format={"type": "json_object"},
            timeout=PROMPT_PARSE_TIMEOUT,
        )
        result_text = (response.choices[0].message.content or "").strip()
        if not result_text:
            return _make_empty(prompt, "Empty response from OpenAI")
        parsed = json.loads(result_text)
        result = _validate_structured_parse(parsed, prompt)
        # --- Cache successful parse (don't cache errors) ---
        if "error" not in result:
            with _parse_cache_lock:
                if len(_parse_cache) >= _PARSE_CACHE_MAX:
                    oldest_key = min(_parse_cache, key=lambda k: _parse_cache[k][0])
                    del _parse_cache[oldest_key]
                _parse_cache[cache_key] = (time.time(), result)
        return result
    except json.JSONDecodeError as e:
        print(f"⚠️ Prompt parser JSON decode error: {e}")
        return _make_empty(prompt, "Failed to parse OpenAI response")
    except Exception as e:
        print(f"⚠️ Prompt parser error: {e}")
        return _make_empty(prompt, str(e))


def _validate_structured_parse(parsed: Dict, original_prompt: str) -> Dict[str, Any]:
    """Ensure all required keys and types; normalize lists and confidence."""
    out = {
        "original_prompt": str(parsed.get("original_prompt") or original_prompt),
        "company_context": str(parsed.get("company_context") or "").strip(),
        "companies": [],
        "locations": [],
        "schools": [],
        "seniority_levels": [],
        "industries": [],
        "confidence": "low",
        "title_variations": [],
    }
    if isinstance(parsed.get("companies"), list):
        for c in parsed["companies"]:
            if isinstance(c, dict) and c.get("name"):
                titles = c.get("matched_titles") or []
                out["companies"].append({
                    "name": str(c["name"]).strip(),
                    "matched_titles": [str(t).strip() for t in titles if t and str(t).strip()],
                })
    for key in ("locations", "schools", "industries", "title_variations"):
        val = parsed.get(key)
        if isinstance(val, list):
            out[key] = [str(x).strip() for x in val if x and str(x).strip()]
        elif isinstance(val, str) and val.strip():
            out[key] = [val.strip()]
    # Flatten title_variations from companies if not already populated
    if not out["title_variations"] and out["companies"]:
        seen = set()
        for c in out["companies"]:
            for t in c.get("matched_titles") or []:
                if t and t not in seen:
                    seen.add(t)
                    out["title_variations"].append(t)
    traits = _validate_company_traits(parsed.get("company_traits"))
    if traits:
        out["company_traits"] = traits
    # Override confidence: high if we have at least one search dimension
    has_specifics = bool(
        out["title_variations"] or out["companies"] or out["schools"] or out["locations"] or traits
    )
    if has_specifics:
        out["confidence"] = "high"
    else:
        conf = (parsed.get("confidence") or "").strip().lower()
        out["confidence"] = "high" if conf == "high" else "low"
    return out


def _int_or_none(v, lo: int, hi: int):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _validate_company_traits(raw) -> Dict[str, Any] | None:
    """Keep only well-formed traits; None when nothing usable is left."""
    if not isinstance(raw, dict):
        return None
    def strs(v):
        return [str(x).strip() for x in v if x and str(x).strip()][:8] if isinstance(v, list) else []
    funded = str(raw.get("funded_after") or "").strip()
    traits = {
        "label": str(raw.get("label") or "").strip()[:60],
        "min_employees": _int_or_none(raw.get("min_employees"), 1, 10_000_000),
        "max_employees": _int_or_none(raw.get("max_employees"), 1, 10_000_000),
        "company_types": strs(raw.get("company_types")),
        "founded_after": _int_or_none(raw.get("founded_after"), 1800, 2100),
        "funded_after": funded if len(funded) == 10 and funded[4] == "-" and funded[7] == "-" else None,
        "industries": strs(raw.get("industries")),
    }
    if not any(v for k, v in traits.items() if k != "label"):
        return None
    return traits


# ---------------------------------------------------------------------------
# Industry-aware semantic expansion
#
# Broadens a parsed prompt whose `industries` is non-empty into the set of
# related PDL taxonomy industries AND generates industry-aligned title
# variations. Strictly additive: original entries are preserved and dedup'd
# against the LLM's additions. The expanded industries list is hard-filtered
# against PDL_INDUSTRY_TAXONOMY (PDL industry is an exact-match enum — labels
# outside the enum match nothing and waste credits).
# ---------------------------------------------------------------------------


def _expansion_cache_key(parsed: Dict[str, Any]) -> str:
    industries = sorted(
        s.lower().strip()
        for s in (parsed.get("industries") or [])
        if s and str(s).strip()
    )
    companies = sorted(
        (c.get("name") or "").lower().strip()
        for c in (parsed.get("companies") or [])
        if isinstance(c, dict) and c.get("name")
    )
    original = (parsed.get("original_prompt") or "").lower().strip()
    raw = json.dumps(
        {"industries": industries, "companies": companies, "original": original},
        sort_keys=True,
    )
    return hashlib.md5(raw.encode()).hexdigest()


def expand_industries_and_titles(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """
    Broaden `industries` to related PDL taxonomy entries and add aligned
    `title_variations`. Soft-fails to the input dict on any error.

    Returns a NEW dict (caller can safely mutate either copy). No-op when
    `industries` is empty.
    """
    industries_in = [
        s.strip() for s in (parsed.get("industries") or []) if s and str(s).strip()
    ]
    if not industries_in:
        return parsed

    cache_key = _expansion_cache_key(parsed)
    with _expand_cache_lock:
        if cache_key in _expand_cache:
            ts, cached = _expand_cache[cache_key]
            if time.time() - ts < _EXPAND_CACHE_TTL:
                print(f"[ExpandIndustries] Cache hit (MD5: {cache_key})")
                return cached
            else:
                del _expand_cache[cache_key]

    client = get_openai_client()
    if not client:
        return parsed

    existing_titles = [
        str(t).strip()
        for t in (parsed.get("title_variations") or [])
        if t and str(t).strip()
    ]
    original_prompt = parsed.get("original_prompt") or ""

    system_prompt = (
        "You broaden a contact-search query so PDL returns more relevant people. "
        "Given the user's original prompt, the industries they mentioned, and their "
        "existing job titles, return JSON with TWO arrays:\n"
        "  - related_industries: PDL-canonical industry labels closely related to "
        "the user's stated industries (siblings + parents in the same sector). "
        "Use lowercase labels matching PDL's enum exactly (e.g. \"media production\", "
        "\"broadcast media\", \"online media\", \"entertainment\", \"motion pictures and film\" "
        "for \"media\"). Do NOT invent new labels. Aim for 4-8 entries.\n"
        "  - title_additions: 6-12 job titles common in those industries, aligned with "
        "the seniority and intent in the original prompt. Include domain-specific "
        "titles AND their generic fallbacks (e.g. \"Producer\", \"Editor\", \"Reporter\").\n"
        "Return ONLY valid JSON: "
        '{"related_industries": [...], "title_additions": [...]}'
    )
    user_prompt = (
        f"Original prompt: {original_prompt}\n"
        f"User industries: {industries_in}\n"
        f"User titles so far: {existing_titles}"
    )

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=600,
            temperature=0.2,
            response_format={"type": "json_object"},
            timeout=EXPANSION_TIMEOUT,
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            return parsed
        data = json.loads(text)
    except Exception as e:
        print(f"[ExpandIndustries] soft-fail: {e}")
        return parsed

    related = data.get("related_industries") or []
    additions = data.get("title_additions") or []
    if not isinstance(related, list):
        related = []
    if not isinstance(additions, list):
        additions = []

    # Merge industries: keep originals, append LLM additions, hard-filter against
    # PDL enum (exact-match). Originals stay even if not in enum — the caller
    # decides whether to drop them; we don't silently discard user intent.
    merged_industries: List[str] = []
    seen_ind = set()
    for ind in industries_in:
        key = ind.lower().strip()
        if key and key not in seen_ind:
            merged_industries.append(ind)
            seen_ind.add(key)
    for ind in related:
        if not isinstance(ind, str):
            continue
        key = ind.lower().strip()
        if not key or key in seen_ind:
            continue
        if PDL_INDUSTRY_TAXONOMY and key not in PDL_INDUSTRY_TAXONOMY:
            continue
        merged_industries.append(key)
        seen_ind.add(key)

    # Merge titles: keep originals first, append additions, dedupe case-insensitive.
    merged_titles: List[str] = []
    seen_titles = set()
    for t in existing_titles:
        key = t.lower().strip()
        if key and key not in seen_titles:
            merged_titles.append(t)
            seen_titles.add(key)
    for t in additions:
        if not isinstance(t, str):
            continue
        clean = t.strip()
        key = clean.lower()
        if not key or key in seen_titles:
            continue
        merged_titles.append(clean)
        seen_titles.add(key)

    out = dict(parsed)
    out["industries"] = merged_industries
    out["title_variations"] = merged_titles
    out["industry_expansion_applied"] = True

    with _expand_cache_lock:
        if len(_expand_cache) >= _EXPAND_CACHE_MAX:
            oldest = min(_expand_cache, key=lambda k: _expand_cache[k][0])
            del _expand_cache[oldest]
        _expand_cache[cache_key] = (time.time(), out)

    print(
        f"[ExpandIndustries] industries {len(industries_in)} -> "
        f"{len(merged_industries)}, titles {len(existing_titles)} -> {len(merged_titles)}"
    )
    return out


# ---------------------------------------------------------------------------
# Query classification — used by /api/prompt-search to decide whether the
# current provider (Hunter, while PDL credits are exhausted) can serve the
# query at all, and which user-requested filters it must skip.
# ---------------------------------------------------------------------------

def classify_query(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a routing decision for a parsed prompt while PDL is offline:

        {
          "has_company": bool,            # Hunter can attempt iff True
          "unsupported_filters": [str],   # filters Hunter cannot honor
        }

    Hunter Domain Search requires a company domain. Other filters
    (school, location-of-person, past-company) are reported back so the
    route can include them in the response payload and the frontend can
    render a "filter temporarily unavailable" disclaimer.
    """
    has_company = bool(parsed.get("companies"))
    unsupported: list[str] = []
    if parsed.get("schools"):
        unsupported.append("school")
    if parsed.get("locations"):
        unsupported.append("location")
    return {"has_company": has_company, "unsupported_filters": unsupported}


# ---------------------------------------------------------------------------
# Legacy adapter: used by /api/search/parse-prompt route (parse_prompt.py)
# Converts structured output → legacy flat-array format
# ---------------------------------------------------------------------------

def parse_search_prompt(prompt: str) -> Dict:
    """
    Adapter around the canonical parser. Returns the legacy flat-array format
    expected by the /api/search/parse-prompt endpoint.

    Returns: { company, roles, location, schools, industries, max_results, confidence }
    """
    structured = parse_search_prompt_structured(prompt)

    # If the canonical parser errored, fall back to keyword matching
    if structured.get("error") and structured.get("confidence") == "low":
        return _fallback_parse(prompt)

    # Reshape: structured → legacy format
    companies = [c["name"] for c in structured.get("companies", []) if c.get("name")]
    roles = list(structured.get("title_variations", []))

    # Confidence: map "high"/"low" → float
    conf = 0.9 if structured.get("confidence") == "high" else 0.2

    return {
        "company": companies,
        "roles": roles,
        "location": list(structured.get("locations", [])),
        "schools": list(structured.get("schools", [])),
        "industries": list(structured.get("industries", [])),
        "max_results": 15,
        "confidence": conf,
    }


# ---------------------------------------------------------------------------
# Keyword fallback (no LLM available)
# ---------------------------------------------------------------------------

def _fallback_parse(prompt: str) -> Dict:
    """Fallback parser using simple keyword matching when LLM is unavailable."""
    prompt_lower = prompt.lower()

    companies = []
    roles = []
    locations = []
    schools = []

    company_keywords = {
        "goldman sachs": ["goldman", "gs"],
        "morgan stanley": ["morgan stanley"],
        "jpmorgan": ["jpmorgan", "jpm", "jp morgan"],
        "mckinsey": ["mckinsey"],
        "bain": ["bain"],
        "bcg": ["bcg", "boston consulting"],
        "google": ["google"],
        "microsoft": ["microsoft", "msft"],
        "amazon": ["amazon"],
        "apple": ["apple"],
        "meta": ["meta", "facebook"],
    }
    for company, keywords in company_keywords.items():
        if any(kw in prompt_lower for kw in keywords):
            companies.append(company)

    if "investment banking" in prompt_lower or "ib " in prompt_lower:
        roles.append("Investment Banking Analyst")
    if "software engineer" in prompt_lower or "swe" in prompt_lower:
        roles.append("Software Engineer")
    if "product manager" in prompt_lower or "pm " in prompt_lower:
        roles.append("Product Manager")
    if "consultant" in prompt_lower:
        roles.append("Consultant")

    school_keywords = {
        "University of Southern California": ["usc", "southern california"],
        "New York University": ["nyu"],
        "Stanford University": ["stanford"],
        "Harvard University": ["harvard"],
        "MIT": ["mit"],
        "UC Berkeley": ["berkeley", "uc berkeley"],
    }
    for school, keywords in school_keywords.items():
        if any(kw in prompt_lower for kw in keywords):
            schools.append(school)

    location_map = {
        "New York": ["new york", "nyc"],
        "San Francisco": ["san francisco", "sf"],
        "Los Angeles": ["los angeles", "la"],
        "Chicago": ["chicago"],
        "Boston": ["boston"],
        "Seattle": ["seattle"],
        "Austin": ["austin"],
    }
    for loc, keywords in location_map.items():
        if any(kw in prompt_lower for kw in keywords):
            locations.append(loc)

    return {
        "company": companies,
        "roles": roles,
        "location": locations,
        "schools": schools,
        "industries": [],
        "max_results": 15,
        "confidence": 0.3,
    }
