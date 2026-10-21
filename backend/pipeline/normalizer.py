"""
Normalize raw JSearch results into a consistent Firestore document schema.
Uses OpenAI gpt-4o-mini for salary extraction when structured data is missing.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Search tokenization
# ---------------------------------------------------------------------------
#
# search_terms is a flat lowercased token array stored on every job doc so the
# /api/jobs/search route can match against it with Firestore's array_contains
# operator. v1 indexes title + company + location. v2 may add description.
#
# Kept dumb on purpose: no stemming, no synonyms, no IDF. Company aliasing
# (Amazon Web Services -> Amazon) is the C2 normalizer's job; by the time we
# tokenize here, `company` is already the canonical brand.

_SEARCH_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Common words that match thousands of jobs and add noise. Keep this list
# short on purpose; we'd rather over-match than under-match.
_SEARCH_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "for", "in", "at", "to", "with",
    "on", "by", "is", "as",
})


def _stringify_for_tokens(value) -> str:
    """Coerce an arbitrary field value into a string the tokenizer can read.

    Strings pass through. Dicts flatten to a space-joined string of their
    values; the JSON-LD `@type` discriminator is excluded so its literal type
    tag (e.g. "PostalAddress") never enters search_terms. Lists and tuples
    flatten to space-joined elements. Anything else is stringified.

    Exists because ~28 percent of legacy Fantastic.jobs docs store `location`
    as a JSON-LD PostalAddress dict instead of a string. Without this, the
    tokenizer crashes on `.lower()` for those docs and the backfill skips
    them. Live writes already produce string locations, so this also
    future-proofs the tokenizer against any other source that lands a
    structured value in a tokenized field.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(
            str(v) for k, v in value.items() if v and k != "@type"
        )
    if isinstance(value, (list, tuple)):
        return " ".join(str(v) for v in value if v)
    return str(value)


def _tokenize_for_search(*texts) -> list[str]:
    """Lowercase, split on non-alphanumerics, drop stopwords and length-1
    tokens. Order-preserving dedup so the array stays small and stable for
    diff-noise when re-normalizing the same doc.
    """
    seen: set[str] = set()
    out: list[str] = []
    for text in texts:
        s = _stringify_for_tokens(text)
        if not s:
            continue
        for tok in _SEARCH_TOKEN_RE.findall(s.lower()):
            if len(tok) < 2 or tok in _SEARCH_STOPWORDS:
                continue
            if tok in seen:
                continue
            seen.add(tok)
            out.append(tok)
    return out


def build_search_terms(title: str | None, company: str | None, location: str | None) -> list[str]:
    """Public entry point. Backfill scripts import this so production and
    backfill produce byte-identical arrays.
    """
    return _tokenize_for_search(title or "", company or "", location or "")


# ---------------------------------------------------------------------------
# Company alias normalization
# ---------------------------------------------------------------------------
#
# Scrapers store whatever string the ATS surfaces, so the same brand reaches
# Firestore under many names: "Amazon" via Greenhouse, "Amazon Web Services"
# via Fantastic.jobs, "Amazon.com Services LLC" via Workday paperwork.
# `canonicalize_company` collapses those to one canonical brand string so a
# search for "amazon" hits every Amazon job, and aggregations (per-company
# rank, dedup, the cap_per_company step) stop double-counting.
#
# Rules:
#   - Lookup is case-insensitive and whitespace-stripped.
#   - The raw input is preserved on the doc as `company_raw` so analytics can
#     still see which legal entity posted the job.
#   - Unknown strings pass through unchanged. We deliberately do NOT do fuzzy
#     matching: "Open AI" -> "OpenAI" reads helpful, but "Open Doors" ->
#     "OpenAI" would be a disaster. Add aliases explicitly when you find them.
#
# The alias table is intentionally small. Grow it from observed search misses,
# not speculation.

_COMPANY_ALIASES: dict[str, str] = {
    # Amazon family
    "amazon web services": "Amazon",
    "aws": "Amazon",
    "amazon.com services llc": "Amazon",
    "amazon.com services": "Amazon",
    "amazon.com": "Amazon",
    "amazon services llc": "Amazon",
    "amazon dev center": "Amazon",
    # Meta
    "facebook": "Meta",
    "facebook inc": "Meta",
    "facebook, inc.": "Meta",
    "meta platforms": "Meta",
    "meta platforms inc": "Meta",
    "instagram": "Meta",
    "whatsapp": "Meta",
    # Alphabet / Google
    "alphabet": "Google",
    "alphabet inc": "Google",
    "google llc": "Google",
    "google inc": "Google",
    "youtube": "Google",
    # Microsoft
    "microsoft corp": "Microsoft",
    "microsoft corporation": "Microsoft",
    "linkedin": "Microsoft",
    "github": "Microsoft",
    # Apple
    "apple inc": "Apple",
    "apple inc.": "Apple",
    # X / Twitter
    "twitter": "X",
    "x corp": "X",
    "x corp.": "X",
    # Other repeats observed in scraped data
    "salesforce.com": "Salesforce",
    "salesforce inc": "Salesforce",
    "ibm corp": "IBM",
    "international business machines": "IBM",
    "jpmorgan": "JPMorgan Chase",
    "jpmorgan chase": "JPMorgan Chase",
    "jp morgan": "JPMorgan Chase",
    "jp morgan chase": "JPMorgan Chase",
    "goldman sachs group": "Goldman Sachs",
    "morgan stanley & co": "Morgan Stanley",
    "deloitte consulting": "Deloitte",
    "deloitte llp": "Deloitte",
    "ey": "EY",
    "ernst & young": "EY",
    "kpmg llp": "KPMG",
    "pwc": "PwC",
    "pricewaterhousecoopers": "PwC",
    "mckinsey": "McKinsey & Company",
    "mckinsey and company": "McKinsey & Company",
    "bcg": "Boston Consulting Group",
    "bain & co": "Bain & Company",
    "bain and company": "Bain & Company",
    # Brands where jobhive / raw scrapes commonly land as all-lowercase or
    # otherwise mis-cased (2026-07-15 backfill audit). Explicit map here
    # forces the canonical display casing so search + companies index work.
    "openai": "OpenAI",
    "1password": "1Password",
    "cursor": "Cursor",
    "anthropic": "Anthropic",
    "notion": "Notion",
    "linear": "Linear",
    "ramp": "Ramp",
    "deel": "Deel",
    "replit": "Replit",
    "supabase": "Supabase",
    "vercel": "Vercel",
    "figma": "Figma",
    "canva": "Canva",
    "airbnb": "Airbnb",
    "stripe": "Stripe",
    "brex": "Brex",
    "mercury": "Mercury",
    "coinbase": "Coinbase",
    "robinhood": "Robinhood",
    "chime": "Chime",
    "plaid": "Plaid",
    "spotify": "Spotify",
    "netflix": "Netflix",
    "uber": "Uber",
    "doordash": "DoorDash",
    "doordashusa": "DoorDash",
    "instacart": "Instacart",
    "lyft": "Lyft",
    "pinterest": "Pinterest",
    "reddit": "Reddit",
    "discord": "Discord",
    "twitch": "Twitch",
    "snap": "Snap",
    "snapchat": "Snap",
    "shopify": "Shopify",
    "atlassian": "Atlassian",
    "asana": "Asana",
    "monday": "Monday.com",
    "clickup": "ClickUp",
    "miro": "Miro",
    "airtable": "Airtable",
    "webflow": "Webflow",
    "loom": "Loom",
    "zapier": "Zapier",
    "postman": "Postman",
    "gitlab": "GitLab",
    "cloudflare": "Cloudflare",
    "datadog": "Datadog",
    "elastic": "Elastic",
    "mongodb": "MongoDB",
    "databricks": "Databricks",
    "snowflake": "Snowflake",
    "hubspot": "HubSpot",
    "zendesk": "Zendesk",
    "twilio": "Twilio",
    "sendgrid": "SendGrid",
    "amplitude": "Amplitude",
    "mixpanel": "Mixpanel",
    "posthog": "PostHog",
    "segment": "Segment",
    "grammarly": "Grammarly",
    "duolingo": "Duolingo",
    "gusto": "Gusto",
    "rippling": "Rippling",
    "carta": "Carta",
    "faire": "Faire",
    "verkada": "Verkada",
    "scaleai": "Scale AI",
    "scale ai": "Scale AI",
    "anduril": "Anduril",
    "palantir": "Palantir",
    "palantirtech": "Palantir",
    "spacex": "SpaceX",
    "shieldai": "Shield AI",
    "shield ai": "Shield AI",
    "cohere": "Cohere",
    "perplexity": "Perplexity",
    "mistralai": "Mistral AI",
    "mistral ai": "Mistral AI",
    "togetherai": "Together AI",
    "together ai": "Together AI",
    "huggingface": "Hugging Face",
    "hugging face": "Hugging Face",
    "modal": "Modal",
    "replicate": "Replicate",
    "langchain": "LangChain",
    "pinecone": "Pinecone",
    "weaviate": "Weaviate",
    "wandb": "Weights & Biases",
    "weights and biases": "Weights & Biases",
    "runway": "Runway",
    "raycast": "Raycast",
    "clerk": "Clerk",
    "resend": "Resend",
    "beehiiv": "Beehiiv",
    "ghost": "Ghost",
    "substack": "Substack",
    "loops": "Loops",
    # QA additions from Rylan's 2026-07-15 sector-classifier spot-check.
    # Fix scrape-artifact concatenations that survived tail-strip, plus
    # missing parent→brand mappings.
    "paytm payments": "Paytm",
    "paytm payments bank": "Paytm",
    "monsterenergy": "Monster Energy",
    "monster energy": "Monster Energy",
    "michelscorporation": "Michels",
    "michels corporation": "Michels",
    "jdsports": "JD Sports",
    "jdsportsfr": "JD Sports",
    "jd sports": "JD Sports",
    "n2publishing": "N2 Publishing",
    "n2publishingglassdoor": "N2 Publishing",
    "n2 publishing": "N2 Publishing",
    # Companies mis-inferred by sector classifier — force the display
    # to the canonical brand form so re-classification (later --force
    # pass) picks the correct sector.
    "harvey ai": "Harvey AI",
    "harveyai": "Harvey AI",
    "omada ai": "Omada Health",
    "omadaai": "Omada Health",
    "omada health": "Omada Health",
    "omadahealth": "Omada Health",
    "horace mann": "Horace Mann",
    "horace mann agent opportunities": "Horace Mann",
    "morgan and morgan": "Morgan & Morgan",
    "morgan & morgan": "Morgan & Morgan",
    "morgan and morgan p a": "Morgan & Morgan",
    "morgan and morgan pa": "Morgan & Morgan",
    "chaos industries": "CHAOS Industries",
    "chaosindustries": "CHAOS Industries",
    "genius sports": "Genius Sports",
    "genius sports statistician network": "Genius Sports",
    "genius sports group": "Genius Sports",
    "betsson": "Betsson",
    "air apps": "Air Apps",
    "airapps": "Air Apps",
    "infuse": "INFUSE",
    "lyft": "Lyft",
}


# Legal / geo suffixes stripped from company names during canonicalization.
# Order matters — longer patterns first so "united states" gets stripped
# before falling to bare "us". All applied case-insensitively at token
# boundaries. Kept broad on purpose: we'd rather over-strip and produce
# tight canonicals than leave variants split ("Stripe" vs "Stripe Inc.").
_LEGAL_SUFFIX_RE = re.compile(
    r"[,\s]+("
    r"incorporated|corporation|holdings|technologies|technology|"
    r"international|group|labs|systems|solutions|services|company|co|"
    r"llc|llp|ltd|limited|inc|corp|pbc|plc|gmbh|sa|nv|"
    r"united\s+states|usa|us|"
    r"\(us\)|\(usa\)"
    r")\.?\s*$",
    re.I,
)

# Trailing region tags like "- US", "— USA", " (US)" applied before suffix strip.
_GEO_TAG_RE = re.compile(r"\s*[-–—]\s*(us|usa|united\s+states)\s*$", re.I)

# Aggressive whitespace + punctuation collapse. Preserves ampersands
# (Bain & Company) but drops commas, dots, parens.
_PUNCT_COLLAPSE_RE = re.compile(r"[.,;:()\[\]{}]+")
_MULTI_SPACE_RE = re.compile(r"\s+")


def _strip_suffixes(name: str) -> str:
    """Peel legal + geo suffixes off a company name. Iterates in case a name
    stacks multiple ("Foo Inc. Ltd." → "Foo")."""
    prev = None
    current = name
    while prev != current:
        prev = current
        current = _GEO_TAG_RE.sub("", current)
        current = _LEGAL_SUFFIX_RE.sub("", current).strip()
    return current


# Concatenated-suffix strip (applied to the space-less lookup key). Handles
# names like "Doordashusa" or "StripeInc" where the source system mashed the
# suffix on without a space. Only stripped from the key used for matching,
# never from the display form.
#
# Also strips common scrape artifacts (`fr`, `de`, `uk`, `glassdoor`) that
# leak from source systems' localized subdomains and aggregator IDs — per
# Rylan's QA (2026-07-15): jdsportsfr → jdsports, n2publishingglassdoor →
# n2publishing.
_KEY_TAIL_STRIP_RE = re.compile(
    r"(usa|us|inc|corp|llc|llp|ltd|holdings|technologies|group|co|"
    r"fr|de|uk|es|it|nl|jp|ca|au|"
    r"glassdoor|indeed|linkedin)$",
    re.I,
)


def _canonical_key(name: str) -> str:
    """Lowercased normalized key for alias-map lookup + jobhive matching.
    Collapses `DoorDash`, `Doordashusa`, `DoorDash Inc.` → `doordash`.

    Two passes: (1) strip suffixes at word boundaries (handles "DoorDash Inc"),
    (2) strip common tails from the concatenated form (handles "Doordashusa").
    """
    if not name:
        return ""
    s = name.strip()
    s = _strip_suffixes(s)
    s = _PUNCT_COLLAPSE_RE.sub(" ", s)
    s = _MULTI_SPACE_RE.sub(" ", s).strip()
    key = s.lower().replace(" ", "")
    # Second pass: strip common tail suffixes off the concatenated form.
    # Iterate up to 3 rounds in case of stacked tails ("fooinc" → "foo",
    # "foousainc" → "foousa" → "foo"). Preserve minimum 3-char stem to avoid
    # over-stripping short names like "USI".
    for _ in range(3):
        stripped = _KEY_TAIL_STRIP_RE.sub("", key)
        if stripped == key or len(stripped) < 3:
            break
        key = stripped
    return key


# Reverse-lookup cache populated on first call: canonical_key → display name
# pulled from vendored jobhive CSVs. Jobhive ships proper-cased names
# ("Stripe", "1Password"), so we borrow those as the canonical display form.
_JOBHIVE_DISPLAY_CACHE: dict[str, str] | None = None


def _build_jobhive_display_cache() -> dict[str, str]:
    """Load canonical display names from vendored jobhive CSVs. Keyed by
    _canonical_key(name). First one wins if multiple entries collide (rare).
    """
    global _JOBHIVE_DISPLAY_CACHE
    if _JOBHIVE_DISPLAY_CACHE is not None:
        return _JOBHIVE_DISPLAY_CACHE
    from pathlib import Path
    import csv
    cache: dict[str, str] = {}
    data_dir = Path(__file__).parent / "data" / "ats_companies"
    for ats in ("greenhouse", "lever", "ashby"):
        path = data_dir / f"{ats}.csv"
        if not path.exists():
            continue
        try:
            with path.open() as f:
                for row in csv.DictReader(f):
                    display_raw = (row.get("name") or "").strip()
                    if not display_raw:
                        continue
                    # Skip all-lowercase jobhive entries (e.g. "cursor",
                    # "openai") — they're not canonical display forms.
                    # The alias map above handles those explicitly; falling
                    # through here lets the input's own casing win instead.
                    if not any(c.isupper() for c in display_raw):
                        continue
                    # Strip legal/geo suffixes off the display name too — jobhive
                    # sometimes stores "DoorDash USA" for slug doordashusa; we
                    # want the clean brand "DoorDash" as the canonical form.
                    display = _strip_suffixes(_PUNCT_COLLAPSE_RE.sub(" ", display_raw))
                    display = _MULTI_SPACE_RE.sub(" ", display).strip() or display_raw
                    key = _canonical_key(display)
                    if key and key not in cache:
                        cache[key] = display
        except Exception as e:
            logger.warning("jobhive display cache load failed for %s: %s", ats, e)
    _JOBHIVE_DISPLAY_CACHE = cache
    return cache


def canonicalize_company(raw_company: str | None) -> str:
    """Merge scraped company-name variants into ONE canonical display form.

    Fragmentation before this fix (verified against prod 2026-07-15):
      "OpenAI" (331) + "Openai" (310) + "openai" (0) → three fragmented keys
      "DoorDash" (1) + "Doordashusa" (271) → two fragmented keys

    Resolution priority:
      1. _COMPANY_ALIASES (hand-curated big-name mappings — parents to brands,
         acronyms to full names)
      2. Jobhive CSV display cache (canonical proper-cased names for every
         slug we crawl — "Stripe", "1Password", etc.)
      3. Cleaned original with legal/geo suffixes stripped (fallback for
         companies not in jobhive or the alias map)

    Never returns an empty string for a non-empty input.
    """
    if not raw_company:
        return ""
    cleaned = raw_company.strip()
    if not cleaned:
        return ""

    key = _canonical_key(cleaned)
    if not key:
        return cleaned

    # 1. Hand-curated aliases first (they carry parent→brand rewrites like
    # "Alphabet Inc" → "Google", which no downstream cache would guess).
    # Try three key forms so scrape variants match regardless of casing,
    # dashes, or concatenation:
    #   - raw lowercased ("horace mann - agent opportunities")
    #   - punctuation-collapsed ("horace mann agent opportunities")
    #   - fully-canonical key ("horacemannagentopportunities")
    _spaced = _PUNCT_COLLAPSE_RE.sub(" ", cleaned.replace("&", " and "))
    _spaced = re.sub(r"[-–—/]+", " ", _spaced)
    _spaced = _MULTI_SPACE_RE.sub(" ", _spaced).strip().lower()
    aliased = (
        _COMPANY_ALIASES.get(cleaned.lower())
        or _COMPANY_ALIASES.get(_spaced)
        or _COMPANY_ALIASES.get(key)
    )
    if aliased:
        return aliased

    # 2. Jobhive display cache (canonical brand casing for known slugs).
    display = _build_jobhive_display_cache().get(key)
    if display:
        return display

    # 3. Fallback: strip suffixes off the cleaned form. Preserves casing.
    stripped = _strip_suffixes(_PUNCT_COLLAPSE_RE.sub(" ", cleaned))
    stripped = _MULTI_SPACE_RE.sub(" ", stripped).strip()
    return stripped or cleaned

# ---------------------------------------------------------------------------
# Job type normalization
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "FULLTIME": "FULLTIME",
    "FULL_TIME": "FULLTIME",
    "PERMANENT": "FULLTIME",
    "CONTRACTOR": "FULLTIME",
    "PARTTIME": "PARTTIME",
    "PART_TIME": "PARTTIME",
    "INTERN": "INTERNSHIP",
    "INTERNSHIP": "INTERNSHIP",
}


_INTERNSHIP_KEYWORDS = (
    "intern", "internship", "co-op", "coop",
    "fellowship", "apprentice",
    "summer analyst", "summer associate",
    "winter analyst", "spring analyst",
)


def normalize_type(raw_type: str | None, title: str) -> str:
    title_lower = (title or "").lower()
    if any(kw in title_lower for kw in _INTERNSHIP_KEYWORDS):
        return "INTERNSHIP"
    if any(kw in title_lower for kw in ("part time", "part-time")):
        return "PARTTIME"
    if raw_type:
        mapped = _TYPE_MAP.get(raw_type.upper().strip())
        if mapped:
            return mapped
    return "FULLTIME"


# ---------------------------------------------------------------------------
# Salary helpers
# ---------------------------------------------------------------------------

def extract_salary_from_structured(job: dict) -> dict:
    """Pull salary from JSearch structured fields. Returns {} if both min/max are missing."""
    sal_min = job.get("job_min_salary")
    sal_max = job.get("job_max_salary")
    period = (job.get("job_salary_period") or "").upper().strip()

    if sal_min is None and sal_max is None:
        return {}

    return {
        "salary_min": float(sal_min) if sal_min is not None else None,
        "salary_max": float(sal_max) if sal_max is not None else None,
        "salary_period": period if period in ("HOUR", "YEAR") else None,
        "salary_extracted": False,
    }


_SALARY_KEYWORDS = ("salary", "$", "compensation", "pay range", "per hour", "per year", "annually", "stipend", "hourly")


def extract_salary_from_description(description: str) -> dict:
    """
    Salary extraction from description text.

    OpenAI extraction is disabled for now to keep pipeline runtime under
    2 minutes. Re-enable once a proper rate-limited queue is in place.
    Keyword detection is kept so we know which jobs *could* have salary
    data extracted later.
    """
    if not description or len(description.strip()) < 50:
        return {}

    desc_lower = description.lower()
    if not any(kw in desc_lower for kw in _SALARY_KEYWORDS):
        return {}

    # TODO: Re-enable OpenAI salary extraction with rate-limited queue
    return {}


_ANNUAL_MULTIPLIER = {
    "HOUR": 2080,
    "DAY": 260,
    "WEEK": 52,
    "MONTH": 12,
    "YEAR": 1,
}


def _salary_normalized_annual(sal_min, sal_max, period) -> int | None:
    """Convert to annual integer using period-appropriate multiplier."""
    val = sal_max or sal_min
    if val is None:
        return None
    multiplier = _ANNUAL_MULTIPLIER.get(period, 1)
    return int(val * multiplier)


def _format_salary_display(sal_min, sal_max, period, extracted: bool) -> str | None:
    prefix = "~" if extracted else ""
    if period == "HOUR":
        parts = []
        if sal_min is not None:
            parts.append(f"${int(sal_min)}")
        if sal_max is not None:
            parts.append(f"${int(sal_max)}")
        return f"{prefix}{('–').join(parts)}/hr" if parts else None
    # WEEK, MONTH, YEAR — annualize then display as $Xk/yr
    multiplier = _ANNUAL_MULTIPLIER.get(period, 1)
    parts = []
    if sal_min is not None:
        parts.append(f"${int(sal_min * multiplier / 1000)}k")
    if sal_max is not None:
        parts.append(f"${int(sal_max * multiplier / 1000)}k")
    return f"{prefix}{('–').join(parts)}/yr" if parts else None


# ---------------------------------------------------------------------------
# Skills helper
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


# ---------------------------------------------------------------------------
# Location helper
# ---------------------------------------------------------------------------

def _normalize_location(job: dict) -> tuple[str, bool]:
    remote = bool(job.get("job_is_remote"))
    city = job.get("job_city") or ""
    state = job.get("job_state") or ""
    if remote and not city:
        return "Remote", True
    parts = [p for p in (city, state) if p]
    loc = ", ".join(parts) if parts else "United States"
    return loc, remote


# ---------------------------------------------------------------------------
# Main normalize
# ---------------------------------------------------------------------------

# Advanced fields populated by the Fantastic.jobs fetcher when include_ai and
# include_li are on. Greenhouse/Lever/Ashby/Simplify don't supply these, so
# they pass through as None / empty and the student ranker treats them as
# "unknown" rather than penalizing.
_FJ_PASSTHROUGH_FIELDS = (
    "salary_currency",
    "ai_experience_level",
    "ai_employment_type",
    "ai_employment_types",
    "ai_work_arrangement",
    "ai_work_arrangement_office_days",
    "ai_visa_sponsorship",
    "ai_has_salary",
    "ai_keywords",
    "ai_key_skills",
    "ai_education_requirements",
    "ai_hiring_manager_name",
    "ai_hiring_manager_email",
    "ai_core_responsibilities",
    "ai_requirements_summary",
    "ai_taxonomies_a",
    "ai_taxonomy_primary",
    "ai_job_language",
    "linkedin_id",
    "linkedin_org_slug",
    "linkedin_org_industry",
    "linkedin_org_employees",
    "linkedin_org_size",
    "linkedin_org_specialties",
    "linkedin_org_followers",
    "linkedin_org_headquarters",
    "linkedin_org_recruitment_agency",
    "ats_platform",
    "ats_source_type",
    "ats_source_domain",
    "date_created",
    "date_validthrough",
)


def _normalize_board_job(raw: dict) -> dict | None:
    """Normalize a pre-structured job from a direct ATS board or Fantastic.jobs."""
    job_id = raw.get("job_id")
    title = raw.get("title")
    company_raw = raw.get("company")
    company = canonicalize_company(company_raw)

    if not job_id or not title or not company:
        return None

    now = datetime.now(timezone.utc)
    description = (raw.get("description_raw") or "")[:8000]
    job_type = normalize_type(None, title)

    # Posted date
    posted_str = raw.get("posted_at")
    try:
        posted_at = datetime.fromisoformat(posted_str.replace("Z", "+00:00")) if posted_str else now
    except (ValueError, AttributeError):
        posted_at = now

    # Salary — use structured fields if provided, else try AI extraction. For
    # Fantastic.jobs the fetcher already populates these from the ai_salary_*
    # fields when include_ai=true, so the description scan is a no-op.
    sal_min = raw.get("salary_min")
    sal_max = raw.get("salary_max")
    sal_period = raw.get("salary_period")
    sal_extracted = False

    if sal_min is None and sal_max is None and description:
        salary = extract_salary_from_description(description)
        if salary:
            sal_min = salary.get("salary_min")
            sal_max = salary.get("salary_max")
            sal_period = salary.get("salary_period")
            sal_extracted = salary.get("salary_extracted", False)

    has_salary = sal_min is not None or sal_max is not None

    location = raw.get("location") or "United States"
    doc = {
        "job_id": job_id,
        "source": raw.get("source", "unknown"),
        "title": title,
        "company": company,
        "company_raw": company_raw or company,
        "employer_logo": raw.get("employer_logo"),
        "location": location,
        "remote": bool(raw.get("remote")),
        "type": job_type,
        "type_raw": "",
        "category": raw.get("_category", "other"),
        "description_raw": description,
        "apply_url": raw.get("apply_url", ""),
        "salary_min": sal_min,
        "salary_max": sal_max,
        "salary_period": sal_period,
        "salary_display": _format_salary_display(sal_min, sal_max, sal_period, sal_extracted) if has_salary else None,
        "salary_normalized_annual": _salary_normalized_annual(sal_min, sal_max, sal_period) if has_salary else None,
        "salary_extracted": sal_extracted,
        "search_terms": build_search_terms(title, company, location),
        "posted_at": posted_at,
        "fetched_at": now,
        "expires_at": now + timedelta(days=14),
        "department": raw.get("department") or "",
        "team": raw.get("team") or "",
        "analyst_entry": bool(raw.get("analyst_entry")),
    }
    for field in _FJ_PASSTHROUGH_FIELDS:
        if field in raw:
            doc[field] = raw[field]
    return doc


def _normalize_jsearch_job(raw: dict) -> dict | None:
    """Convert a single raw JSearch result to normalized Firestore doc."""
    job_id = raw.get("job_id")
    title = raw.get("job_title")
    company_raw = raw.get("employer_name")
    company = canonicalize_company(company_raw)

    if not job_id or not title or not company:
        return None

    location, remote = _normalize_location(raw)
    raw_type = raw.get("job_employment_type") or ""
    job_type = normalize_type(raw_type, title)
    category = raw.get("_category", "other")
    description = (raw.get("job_description") or "")[:8000]

    now = datetime.now(timezone.utc)

    # Posted date
    posted_str = raw.get("job_posted_at_datetime_utc")
    try:
        posted_at = datetime.fromisoformat(posted_str.replace("Z", "+00:00")) if posted_str else now
    except (ValueError, AttributeError):
        posted_at = now

    # Salary
    salary = extract_salary_from_structured(raw)
    if not salary and description:
        salary = extract_salary_from_description(description)

    sal_min = salary.get("salary_min")
    sal_max = salary.get("salary_max")
    sal_period = salary.get("salary_period")
    sal_extracted = salary.get("salary_extracted", False)

    return {
        "job_id": job_id,
        "source": "jsearch",
        "title": title,
        "company": company,
        "company_raw": company_raw or company,
        "employer_logo": raw.get("employer_logo"),
        "location": location,
        "remote": remote,
        "type": job_type,
        "type_raw": raw_type,
        "category": category,
        "description_raw": description,
        "apply_url": raw.get("job_apply_link") or raw.get("job_google_link"),
        "salary_min": sal_min,
        "salary_max": sal_max,
        "salary_period": sal_period,
        "salary_display": _format_salary_display(sal_min, sal_max, sal_period, sal_extracted) if salary else None,
        "salary_normalized_annual": _salary_normalized_annual(sal_min, sal_max, sal_period) if salary else None,
        "salary_extracted": sal_extracted,
        "search_terms": build_search_terms(title, company, location),
        "posted_at": posted_at,
        "fetched_at": now,
        "expires_at": now + timedelta(days=14),
    }


def normalize_job(raw: dict) -> dict | None:
    """Normalize a raw job dict from any source. Auto-detects format."""
    if raw.get("source") in (
        "greenhouse", "lever", "workday", "oracle", "oleeo", "goldman", "manual", "ashby", "fantasticjobs", "simplify",
    ):
        return _normalize_board_job(raw)
    return _normalize_jsearch_job(raw)


# Countries to exclude — jobs with these in location are filtered out even if remote,
# since "Remote, Singapore" means the job is based internationally.
_EXCLUDED_COUNTRIES = {
    # Europe
    "united kingdom", "uk", "germany", "france", "netherlands", "ireland",
    "poland", "spain", "italy", "sweden", "denmark", "finland", "norway",
    "belgium", "switzerland", "austria", "portugal", "greece", "czech republic",
    "czechia", "hungary", "romania", "bulgaria", "ukraine", "belarus",
    "estonia", "latvia", "lithuania", "slovakia", "slovenia", "croatia",
    # Asia-Pacific
    "india", "china", "japan", "singapore", "malaysia", "thailand",
    "philippines", "vietnam", "indonesia", "pakistan", "bangladesh",
    "sri lanka", "south korea", "korea", "taiwan", "hong kong",
    "australia", "new zealand",
    # Americas (non-US)
    "canada", "brazil", "mexico", "argentina", "colombia", "chile",
    "peru", "ecuador", "venezuela", "uruguay", "costa rica", "panama",
    # Middle East / Africa
    "israel", "uae", "united arab emirates", "saudi arabia", "turkey", "egypt",
    "south africa", "nigeria", "kenya", "morocco", "ghana",
}


def _is_non_us_non_remote(job: dict) -> bool:
    """Return True if the job is based in a non-US country.

    Excludes jobs like "Remote, Singapore" where the primary location is
    international. Keeps purely "Remote" or US-based locations.
    """
    loc = job.get("location") or ""
    if isinstance(loc, dict):
        loc = loc.get("name") or loc.get("city") or str(loc)
    elif isinstance(loc, list):
        loc = " ".join(str(x) for x in loc)
    location = str(loc).lower()
    for country in _EXCLUDED_COUNTRIES:
        if country in location:
            return True
    return False


# ---------------------------------------------------------------------------
# US-priority tagging (Phase 2 pre-launch: US-first feed ordering)
# ---------------------------------------------------------------------------
# Attaches an integer sort key to every ingested job so the feed can order
# US > remote > rest without dropping any postings. Client-side sort in
# jobs.py uses this field — no Firestore index or backfill required (missing
# field defaults to tier 3, safe).

_US_STATE_FULL = re.compile(
    r"\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|"
    r"delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|"
    r"kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|"
    r"minnesota|mississippi|missouri|montana|nebraska|nevada|"
    r"new\s+hampshire|new\s+jersey|new\s+mexico|new\s+york|"
    r"north\s+carolina|north\s+dakota|ohio|oklahoma|oregon|pennsylvania|"
    r"rhode\s+island|south\s+carolina|south\s+dakota|tennessee|texas|"
    r"utah|vermont|virginia|washington|west\s+virginia|wisconsin|wyoming)\b",
    re.I,
)

# State abbrevs — only match after a comma to avoid "or", "in", "me", etc. matching as words.
_US_STATE_ABBREV = re.compile(
    r",\s*(al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|"
    r"mi|mn|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|"
    r"vt|va|wa|wv|wi|wy|dc)\b",
    re.I,
)

_US_EXPLICIT = re.compile(
    r"\b(united\s+states|usa|u\.s\.a\.|u\.s\.)\b|\(us\)|\bus\s*only\b",
    re.I,
)


def _looks_like_us(location: str) -> bool:
    if not location:
        return False
    return bool(
        _US_EXPLICIT.search(location)
        or _US_STATE_FULL.search(location)
        or _US_STATE_ABBREV.search(location)
    )


def infer_us_priority(job: dict) -> int:
    """1 = US-based, 2 = remote (unclear country), 3 = clearly non-US.

    Feed orders by this ASC — US jobs always surface first, non-US jobs
    keep their spot at the tail. Never drops postings.
    """
    loc = job.get("location") or ""
    if isinstance(loc, dict):
        loc = loc.get("name") or loc.get("city") or str(loc)
    elif isinstance(loc, list):
        loc = " ".join(str(x) for x in loc)
    location = str(loc)
    country = str(job.get("country") or "").strip().lower()

    if country in ("united states", "us", "usa", "u.s.", "u.s.a."):
        return 1
    if _looks_like_us(location):
        return 1
    # Explicit non-US country tag OR location containing a non-US country name
    if country and country not in ("", "remote"):
        return 3
    if any(c in location.lower() for c in _EXCLUDED_COUNTRIES):
        return 3
    # No US signal, no explicit non-US signal → check remote
    if bool(job.get("remote")):
        return 2
    # Default: if location is empty/generic ("Remote"), treat as remote (2);
    # otherwise unknown → tier 3
    if location.strip().lower() in ("", "remote", "worldwide", "anywhere"):
        return 2
    return 3


def normalize_all(raw_jobs: list[dict]) -> list[dict]:
    """Normalize a batch of raw jobs.

    Stages, in order:
      1. Source-specific normalization (Greenhouse / Lever / Ashby / FJ / etc.)
      2. Drop non-US non-remote
      3. Quality gate (staffing agencies, senior titles, YOE-inconsistent
         interns, scams, stale postings — see pipeline/quality_gate.py)
    """
    from backend.pipeline.quality_gate import apply as apply_quality_gate

    normalized = []
    skipped = 0
    filtered_location = 0
    for raw in raw_jobs:
        doc = normalize_job(raw)
        if not doc:
            skipped += 1
            continue
        if _is_non_us_non_remote(doc):
            filtered_location += 1
            continue
        normalized.append(doc)
    logger.info(
        "Normalized %d jobs, skipped %d invalid, filtered %d non-US non-remote",
        len(normalized), skipped, filtered_location,
    )
    kept, _ = apply_quality_gate(normalized)
    return kept
