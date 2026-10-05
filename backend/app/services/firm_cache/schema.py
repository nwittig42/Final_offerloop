"""firm_employees Firestore schema + normalizers.

Doc shape (one per LinkedIn profile):

    firm_employees/{linkedin_id}
      linkedin_id        str    slug from LinkedIn URL, doc ID (also stored).
                                 When the source contact has no LinkedIn URL
                                 (e.g. FullEnrich's /people/search rung, which
                                 never returns one), this is a synthesized key
                                 instead (see _synthesize_person_key) so the
                                 contact still enters the warehouse. Prefixed
                                 "synth:" to keep it visibly distinct from a
                                 real slug.
      linkedin_url       str    canonicalized https://www.linkedin.com/in/<slug>,
                                 or "" when linkedin_id is synthesized
      pdl_id             str    PDL's internal id, for provenance
      name               str    display "First Last"
      first_name         str
      last_name          str
      headline           str    raw title string (display)
      title              str    lowercase current title (filter)
      title_level        str    one of TITLE_LEVELS (indexed filter)
      company            str    slugified current company (indexed filter)
      company_display    str    original-case company name (display)
      schools            list   slugified school names (indexed array-contains)
      schools_display    list   original-case school names (display)
      joined_year        int    year current job started (indexed filter); None if unknown
      office_location    str    slugified "city_state" for US, else "" (indexed filter)
      city               str    original-case city (display)
      state              str    two-letter state (display)
      country_code       str    "US" only; non-US docs are not written
      work_email             str  verified work email, shared across EVERY
                                  surface (website, MCP, extension, mobile)
                                  per the FullEnrich reseller agreement's
                                  90-day re-serve window. Absent until a
                                  verified address is found; only set, never
                                  overwritten with a worse value.
      work_email_source      str  the EmailSource that verified it
      work_email_verified_at str  ISO 8601 UTC timestamp; warehouse_warm.py's
                                  nightly purge deletes all three past
                                  WAREHOUSE_EMAIL_TTL_DAYS (90)
      last_seen_at       ts     server timestamp of most recent write

Reads: reader.py uses indexed where() only — never in-memory filter.
Writes: writer.py upserts via SetOptions(merge=True), no delete path.

Normalizers here accept RAW PDL person dicts (before pdl_client's own
contact-shape flattening) so we retain education[] and experience[]
structure that the display contact dict throws away.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

FIRM_EMPLOYEES_COLLECTION = "firm_employees"

# Ordered least- to most-senior. Any title we can't classify goes to "other".
TITLE_LEVELS = (
    "analyst",       # analyst, business analyst, associate analyst
    "associate",     # associate, associate consultant, senior associate
    "consultant",    # consultant, senior consultant
    "manager",       # manager, engagement manager, project leader
    "principal",     # principal, associate principal
    "director",      # director, senior director, executive director
    "vp",            # vp, svp, evp
    "partner",       # partner, principal at law/PE contexts
    "cxo",           # ceo, cto, cfo, coo, cmo, chief anything
    "other",
)

# Rules are ordered specific -> generic. First match wins. Case-insensitive
# substring match on the raw title string.
#
# Known imperfection: MBB firms overload "Associate" and "Consultant" across
# very different seniority levels — Bain's "Senior Associate Consultant" is
# a 2-year mid-level role but classifies as `consultant` here because it
# contains that substring. Good enough for MVP student networking queries
# (they don't discriminate AC vs SAC vs C when reaching out); revisit if
# hit-rate data shows this bucketing hurts.
_TITLE_LEVEL_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("cxo",       ("chief ", " cxo", "ceo", "cto", "cfo", "coo", "cmo", "cro", "ciso")),
    ("partner",   ("partner", "managing director")),
    ("vp",        ("vice president", " vp", "svp", "evp", "senior vp")),
    ("director",  ("director",)),
    ("principal", ("principal",)),
    ("manager",   ("manager", "engagement lead", "project leader", "eng lead", "engineering lead")),
    ("consultant",("consultant",)),
    ("associate", ("associate",)),
    ("analyst",   ("analyst",)),
]


# ── Slugging helpers ──────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(value: str) -> str:
    """Lowercase, strip non-alnum → underscore-separated. Never returns
    leading/trailing separators. Used for indexed filter fields."""
    if not value:
        return ""
    s = _SLUG_RE.sub("_", value.lower()).strip("_")
    return s


def slugify_company(name: str) -> str:
    """Canonical company slug for filter queries.

    Drops common corporate suffixes so 'Bain & Company', 'Bain & Co.', and
    'Bain and Company' all key the same. Not exhaustive — good enough for
    the top ~200 firms students actually search.
    """
    if not name:
        return ""
    n = name.lower()
    # Strip corporate-form suffixes only. Do NOT strip "group" or "partners"
    # — those are often part of the brand ("Boston Consulting Group",
    # "PJT Partners") and stripping produces worse slugs, not better ones.
    for suffix in (
        " & company", " and company", " & co.", " & co", " and co.", " and co",
        ", inc.", ", inc", " inc.", " inc", ", llc", " llc",
        ", ltd.", " ltd.", " ltd", " gmbh", " sa", " plc",
    ):
        if n.endswith(suffix):
            n = n[: -len(suffix)].strip()
            break
    return _slug(n)


# Canonical school slugs for schools where PDL returns multiple variants.
# Both the full name and the abbreviation must map to the same canonical
# slug so that "USC alumni" queries match every person regardless of which
# name variant is on their profile. Not exhaustive — extend as query
# misses surface in the observability dashboard.
_SCHOOL_ALIASES: dict[str, str] = {
    # Target-market schools (highest priority)
    "usc": "usc",
    "university of southern california": "usc",
    "ucla": "ucla",
    "university of california los angeles": "ucla",
    "university of california, los angeles": "ucla",
    "university of michigan": "michigan",
    "michigan": "michigan",
    "university of michigan-ann arbor": "michigan",
    "nyu": "nyu",
    "new york university": "nyu",
    "georgetown": "georgetown",
    "georgetown university": "georgetown",
    "university of pennsylvania": "pennsylvania",
    "penn": "pennsylvania",
    "upenn": "pennsylvania",
    "u penn": "pennsylvania",
    # Common ivy + peer schools
    "harvard": "harvard",
    "harvard university": "harvard",
    "yale": "yale",
    "yale university": "yale",
    "princeton": "princeton",
    "princeton university": "princeton",
    "columbia": "columbia",
    "columbia university": "columbia",
    "columbia university in the city of new york": "columbia",
    "cornell": "cornell",
    "cornell university": "cornell",
    "brown": "brown",
    "brown university": "brown",
    "dartmouth": "dartmouth",
    "dartmouth college": "dartmouth",
    "stanford": "stanford",
    "stanford university": "stanford",
    "mit": "mit",
    "massachusetts institute of technology": "mit",
    "duke": "duke",
    "duke university": "duke",
    "northwestern": "northwestern",
    "northwestern university": "northwestern",
    "chicago": "chicago",
    "university of chicago": "chicago",
    # Big publics common at MBB/IB
    "uc berkeley": "berkeley",
    "berkeley": "berkeley",
    "university of california berkeley": "berkeley",
    "university of california, berkeley": "berkeley",
    "university of virginia": "virginia",
    "virginia": "virginia",
    "university of texas at austin": "texas",
    "ut austin": "texas",
    "university of north carolina at chapel hill": "unc",
    "unc chapel hill": "unc",
    "unc": "unc",
    # Business schools (grad programs matter for MBB targeting)
    "wharton": "wharton",
    "the wharton school": "wharton",
    "wharton school": "wharton",
    "harvard business school": "hbs",
    "hbs": "hbs",
    "stanford graduate school of business": "stanford_gsb",
    "stanford gsb": "stanford_gsb",
    "chicago booth": "booth",
    "booth": "booth",
    "the university of chicago booth school of business": "booth",
    "kellogg": "kellogg",
    "kellogg school of management": "kellogg",
    "columbia business school": "cbs",
    "mit sloan": "mit_sloan",
    "sloan": "mit_sloan",
}


def slugify_school(name: str) -> str:
    """Canonical school slug.

    First checks the alias table (handles known variants like
    'University of Southern California' == 'USC'). Falls back to a
    generic normalizer for unknown schools — strips 'the', 'university of',
    trailing 'university', then slugifies. Unknown schools still dedup
    against themselves; only cross-variant collision (long vs short name)
    requires the alias table.
    """
    if not name:
        return ""
    n = name.lower().strip()
    if n in _SCHOOL_ALIASES:
        return _SCHOOL_ALIASES[n]
    if n.startswith("the "):
        n = n[4:]
    if n.startswith("university of "):
        n = n[len("university of "):]
    if n.endswith(" university"):
        n = n[: -len(" university")]
    # Retry alias lookup after stripping (catches "The Wharton School" ->
    # "wharton school" -> in table)
    if n in _SCHOOL_ALIASES:
        return _SCHOOL_ALIASES[n]
    return _slug(n)


# ── LinkedIn URL canonicalization ────────────────────────────────────────

# Match the canonical shape used elsewhere in the codebase (apify_client.py).
# https://www.linkedin.com/in/<slug>  (lowercased slug, no query/fragment/locale)
_LINKEDIN_SLUG_RE = re.compile(r"linkedin\.com/in/([\w\-]+)", re.IGNORECASE)


def canonicalize_linkedin_url(url: str) -> tuple[str, str]:
    """Return (canonical_url, slug) or ("", "") if unrecognizable.

    Handles: missing protocol, missing www, m.linkedin.com, trailing slash,
    query params, fragment, trailing locale path (/en, /fr).
    """
    if not url:
        return "", ""
    u = url.strip().rstrip("/").split("?")[0].split("#")[0]
    u = re.sub(r"/[a-z]{2}$", "", u, flags=re.IGNORECASE)
    m = _LINKEDIN_SLUG_RE.search(u)
    if not m:
        return "", ""
    slug = m.group(1).lower()
    return f"https://www.linkedin.com/in/{slug}", slug


# ── Title level derivation ────────────────────────────────────────────────

def derive_title_level(title: str) -> str:
    """Bucket a raw job title into a coarse seniority level."""
    if not title:
        return "other"
    t = title.lower()
    for level, needles in _TITLE_LEVEL_RULES:
        for needle in needles:
            if needle in t:
                return level
    return "other"


# ── US location extraction ────────────────────────────────────────────────

# PDL sometimes returns country as "united states" or "us" or the region as
# a state name. Accept both.
_US_COUNTRY_TOKENS = {"united states", "us", "usa", "u.s.", "u.s.a.", "united states of america"}


def _is_us_person(person: dict) -> bool:
    """Return True if PDL person's location is in the US.

    Checks in priority order:
      location_country (string), location_country_code (2-letter),
      job_company_location_country. Missing signal → default False (skip
      caching — we don't want polluted non-US docs)."""
    country = (person.get("location_country") or "").strip().lower()
    if country in _US_COUNTRY_TOKENS:
        return True
    cc = (person.get("location_country_code") or "").strip().lower()
    if cc == "us":
        return True
    jc = (person.get("job_company_location_country") or "").strip().lower()
    if jc in _US_COUNTRY_TOKENS:
        return True
    return False


def _extract_city_state(person: dict) -> tuple[str, str, str]:
    """Return (city_display, state_display, office_location_slug).

    office_location_slug is the indexed field: "los_angeles_ca". Empty
    string for US-but-no-city (unlikely) — those still get country_code=US.
    """
    city = (person.get("location_locality") or "").strip()
    state = (person.get("location_region") or "").strip()
    # PDL sometimes returns full state name ("California") vs abbreviation
    # ("CA"). Store the display as-given; use its slug in the indexed field.
    if not city and not state:
        return "", "", ""
    if city and state:
        return city, state, _slug(f"{city}_{state}")
    return city, state, _slug(city or state)


# ── Education → schools[] ─────────────────────────────────────────────────

def _extract_schools(person: dict) -> tuple[list[str], list[str]]:
    """Return (schools_slugs, schools_display).

    PDL shape: person.education is list[dict], each dict has
    {school: {name: str, ...}, degrees: [...], start_date: {year: int}, ...}
    High school entries are dropped — students don't network based on high school.
    """
    edu_list = person.get("education") or []
    if not isinstance(edu_list, list):
        return [], []
    slugs: list[str] = []
    displays: list[str] = []
    seen: set[str] = set()
    for e in edu_list:
        if not isinstance(e, dict):
            continue
        school = e.get("school") or {}
        if not isinstance(school, dict):
            continue
        name = (school.get("name") or "").strip()
        if not name:
            continue
        if "high school" in name.lower():
            continue
        slug = slugify_school(name)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
        displays.append(name)
    return slugs, displays


# ── Current job → joined_year, title, company ─────────────────────────────

def _extract_current_job(person: dict) -> tuple[str, str, Optional[int]]:
    """Return (title_display, company_display, joined_year).

    Priority: person.job_title / person.job_company_name (already surfaces
    the current job in PDL), fall back to experience[0] if job_ fields empty.
    joined_year comes from job_start_date or experience[0].start_date.year.
    """
    title = (person.get("job_title") or "").strip()
    company = (person.get("job_company_name") or "").strip()
    joined_year: Optional[int] = None

    # job_start_date is a string like "2022-06" or "2022-06-01"
    jsd = person.get("job_start_date")
    if isinstance(jsd, str) and len(jsd) >= 4 and jsd[:4].isdigit():
        try:
            joined_year = int(jsd[:4])
        except ValueError:
            joined_year = None

    if not (title and company and joined_year):
        exp_list = person.get("experience") or []
        if isinstance(exp_list, list) and exp_list:
            first = exp_list[0] if isinstance(exp_list[0], dict) else {}
            if not title:
                title = (first.get("title") or {}).get("name", "") if isinstance(first.get("title"), dict) else ""
                title = title.strip()
            if not company:
                cd = first.get("company") or {}
                if isinstance(cd, dict):
                    company = (cd.get("name") or "").strip()
            if not joined_year:
                sd = first.get("start_date") or {}
                if isinstance(sd, dict):
                    y = sd.get("year")
                    if isinstance(y, int):
                        joined_year = y

    return title, company, joined_year


# ── App-shape (flattened) contact → firm_employees doc ───────────────────
#
# The JIT writer hooks into pdl_client.search_contacts_from_prompt at the
# cache-write site, where PDL raw persons have already been flattened into
# App-shape contact dicts (keys: FirstName, LastName, LinkedIn, Company,
# Title, College, EducationTop, City, State, pdlId, ...).
#
# Trade-offs of the App-shape entry point:
#   + Simplest hook site — one place, minimal invasive change to pdl_client
#   + Same shape the app already deals with everywhere
#   - joined_year is unavailable (start_date got dropped during flattening)
#   - Multi-school history requires parsing EducationTop (a "; "-joined
#     "SchoolName - Degree (start - end)" string). We parse it, so schools[]
#     still contains all schools — just no per-school degree info.
#
# If cache-hit quality suffers we can either add a raw-fields hook site
# earlier in pdl_client, or extend App-shape to carry raw education +
# start dates. Measure first.


def _parse_education_top(education_top: str) -> tuple[list[str], list[str]]:
    """Parse App-shape EducationTop string → (school_slugs, school_displays).

    Format: "Harvard University - MBA (2020 - 2022); USC - BA (2014 - 2018)"
    Drops high school entries. Empty string / "Not available" → ([], [])."""
    if not education_top or not isinstance(education_top, str):
        return [], []
    if education_top.strip().lower() == "not available":
        return [], []
    slugs: list[str] = []
    displays: list[str] = []
    seen: set[str] = set()
    for entry in education_top.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        # School name is everything before " - <degree>". If no " - ", the
        # whole entry is the school name.
        parts = entry.split(" - ", 1)
        name = parts[0].strip()
        if not name or "high school" in name.lower():
            continue
        slug = slugify_school(name)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
        displays.append(name)
    return slugs, displays


def _synthesize_person_key(email: str, first: str, last: str, company_slug: str) -> str:
    """Stable doc-id fallback for a contact with no LinkedIn URL. Prefer
    email (closer to a real identity than a name, and already lowercased/
    trimmed by the caller); fall back to name+company, same spirit as
    coresignal_client._synthesize_id's dedup key. "synth:" prefix keeps it
    visibly distinct from a real LinkedIn slug at a glance (in logs, in the
    Firestore console) and guarantees it can never collide with one."""
    seed = email or f"{first}_{last}_{company_slug}".strip("_").lower()
    if not seed:
        return ""
    return "synth:" + hashlib.sha1(seed.encode()).hexdigest()[:16]


def firm_employee_doc_from_app_contact(contact: dict) -> Optional[dict]:
    """Normalize an App-shape PDL contact (post-flattening) → firm_employees doc.

    Returns None if unusable (no US location / no company / nothing to key
    on at all). joined_year is always None — that data was dropped during
    flattening.

    A contact with no LinkedIn URL (2026-10-05: found FullEnrich's discovery
    rung always omits one, so every contact found that way was silently
    dropped here, never entering the warehouse at all) still gets a doc,
    keyed by _synthesize_person_key instead of a LinkedIn slug. Known
    limitation: if a LinkedIn URL is found for this same person later (a
    backfill, or a different provider), it lands as a SEPARATE doc under the
    real linkedin_id rather than merging into this one — not solved here.
    """
    if not isinstance(contact, dict):
        return None

    # LinkedIn — preferred dedup key, but not required (see
    # _synthesize_person_key below for the no-LinkedIn fallback).
    raw_linkedin = (
        contact.get("LinkedIn")
        or contact.get("linkedin_url")
        or contact.get("linkedinUrl")
        or ""
    )
    canon_url, slug = canonicalize_linkedin_url(raw_linkedin)

    # Company — required
    company_display = (contact.get("Company") or contact.get("company") or "").strip()
    if not company_display:
        return None
    company_slug = slugify_company(company_display)
    if not company_slug:
        return None

    # Location — the App-shape doesn't carry country code. Three cases:
    #   1) State is a valid US state → country_code="US"
    #   2) State is non-empty but clearly non-US → skip (out of MVP scope)
    #   3) State is empty (PDL data gap — very common for tech companies
    #      like Apple/Google that don't always surface location on employees)
    #      → accept but mark country_code="unknown". The person is still
    #      queryable by company/schools/title; office_location just stays
    #      empty. Better than dropping the cache entry entirely.
    state = (contact.get("State") or contact.get("state") or "").strip()
    city = (contact.get("City") or contact.get("city") or "").strip()
    if state and not _looks_like_us_state(state):
        return None
    country_code = "US" if _looks_like_us_state(state) else "unknown"

    # Schools — parse EducationTop for multi-school history. Fall back to
    # the single College field if EducationTop is empty.
    schools_slugs, schools_display = _parse_education_top(
        contact.get("EducationTop") or ""
    )
    if not schools_slugs:
        college = (contact.get("College") or "").strip()
        if college and "high school" not in college.lower():
            college_slug = slugify_school(college)
            if college_slug:
                schools_slugs = [college_slug]
                schools_display = [college]

    # Title
    title_display = (contact.get("Title") or contact.get("title") or "").strip()

    # Location slug (indexed)
    if city and state:
        office_slug = _slug(f"{city}_{state}")
    elif city or state:
        office_slug = _slug(city or state)
    else:
        office_slug = ""

    # Name
    first = (contact.get("FirstName") or contact.get("firstName") or "").strip()
    last = (contact.get("LastName") or contact.get("lastName") or "").strip()
    name = f"{first} {last}".strip()

    doc_id = slug
    if not doc_id:
        email = (contact.get("Email") or contact.get("WorkEmail") or "").strip().lower()
        if not email and not (first or last):
            # No LinkedIn, no email, no name — company alone is not a
            # person, and would collide every nameless contact at that
            # firm into one doc. Genuinely unusable, same as old behavior.
            return None
        doc_id = _synthesize_person_key(email, first, last, company_slug)
        if not doc_id:
            return None

    doc = {
        "linkedin_id":     doc_id,
        "linkedin_url":    canon_url,
        "pdl_id":          (contact.get("pdlId") or "").strip(),
        "name":            name,
        "first_name":      first,
        "last_name":       last,
        "headline":        title_display,
        "title":           title_display.lower(),
        "title_level":     derive_title_level(title_display),
        "company":         company_slug,
        "company_display": company_display,
        "schools":         schools_slugs,
        "schools_display": schools_display,
        "joined_year":     None,   # not available in App-shape; add later if needed
        "office_location": office_slug,
        "city":            city,
        "state":           state,
        "country_code":    country_code,
        # Photo (2026-08-26): PDL never had them, Coresignal always does, and
        # a deck card with a face is measured product value. Carried when the
        # source contact has one; never invented.
        "photo_url":       (contact.get("PhotoUrl") or contact.get("photo_url") or "").strip(),
        # last_seen_at is stamped by writer.py with SERVER_TIMESTAMP
    }

    # Verified work email (2026-10-02, porting the fix already live on the
    # website branch since 2026-09-07, ENG-xxx). Previously only ranker.py's
    # own inline logic wrote this shared field; every other mobile surface
    # (this normalizer) dropped a found email on the floor, same gap that
    # fix closed for the website, MCP server and extension. One shared field
    # name means an email found by ANY of those surfaces is now visible to
    # ALL of them, not just a per-branch, per-feature cache.
    #
    # Only a VERIFIED address is shared (never a guess), and the reseller
    # agreement caps how long we may re-serve one: 90 days, enforced by
    # warehouse_warm.py's nightly purge on work_email_verified_at, not by
    # this normalizer. merge=True on write means an empty value here never
    # blanks a fresher one already on the doc.
    verified_email = _verified_work_email(contact)
    if verified_email:
        doc["work_email"] = verified_email
        doc["work_email_source"] = (contact.get("EmailSource") or "verified").strip()
        doc["work_email_verified_at"] = _utc_now_iso()
    return doc


# Two-letter US state postal codes + DC. Anything else → treat as non-US
# and skip caching (safer to miss than to pollute cache with non-US docs).
_US_STATE_CODES = frozenset({
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
})
# Same list spelled out, for full-name PDL values ("California" not "CA")
_US_STATE_NAMES = frozenset({
    "alabama","alaska","arizona","arkansas","california","colorado","connecticut",
    "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa",
    "kansas","kentucky","louisiana","maine","maryland","massachusetts","michigan",
    "minnesota","mississippi","missouri","montana","nebraska","nevada","new hampshire",
    "new jersey","new mexico","new york","north carolina","north dakota","ohio",
    "oklahoma","oregon","pennsylvania","rhode island","south carolina","south dakota",
    "tennessee","texas","utah","vermont","virginia","washington","west virginia",
    "wisconsin","wyoming","district of columbia",
})


def _looks_like_us_state(state: str) -> bool:
    if not state:
        return False
    s = state.strip()
    if len(s) == 2 and s.upper() in _US_STATE_CODES:
        return True
    return s.lower() in _US_STATE_NAMES


# Email sources whose addresses are verified before they reach a contact
# dict. Anything else (pattern guesses, Hunter risky, catch-alls) stays
# per-user and never lands on the shared shelf. Ported verbatim from the
# website branch's firm_cache/schema.py so the two stay compatible; mobile's
# own EmailSource strings (e.g. "fullenrich_deliverable", "crustdata_v2_
# deliverable") don't literally match these, but every mobile rung already
# sets EmailVerified=True only on a confirmed address, which gates it too.
_VERIFIED_EMAIL_SOURCES = frozenset({
    "fullenrich_backfill", "fullenrich", "fullenrich_prefetch", "warehouse",
})


def _verified_work_email(contact: dict) -> str:
    email = (contact.get("WorkEmail") or contact.get("Email") or "").strip().lower()
    if not email or "@" not in email or email == "not available":
        return ""
    source = (contact.get("EmailSource") or "").strip().lower()
    if contact.get("EmailVerified") is True or source in _VERIFIED_EMAIL_SOURCES:
        return email
    return ""


def _utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ── Public entry point ────────────────────────────────────────────────────

def firm_employee_doc_from_pdl_person(person: dict) -> Optional[dict]:
    """Normalize a raw PDL person response into a firm_employees doc.

    Returns None if the person is unusable — writer.py silently skips these:
      - No LinkedIn URL (can't dedup)
      - Not US-located (out of MVP scope)
      - No current company (can't filter by firm)

    The returned dict is Firestore-ready except for `last_seen_at`, which
    the writer stamps with SERVER_TIMESTAMP.
    """
    if not isinstance(person, dict):
        return None

    # LinkedIn — dedup key. Try both the flat field and the profiles array.
    linkedin_url = ""
    for candidate in (person.get("linkedin_url"), person.get("linkedin_username")):
        if candidate:
            linkedin_url = candidate if candidate.startswith("http") else f"https://www.linkedin.com/in/{candidate}"
            break
    if not linkedin_url:
        for p in person.get("profiles") or []:
            if isinstance(p, dict) and "linkedin" in (p.get("network") or "").lower():
                linkedin_url = p.get("url") or ""
                if linkedin_url:
                    break
    canon_url, slug = canonicalize_linkedin_url(linkedin_url)
    if not slug:
        return None

    # US filter
    if not _is_us_person(person):
        return None

    # Current job
    title_display, company_display, joined_year = _extract_current_job(person)
    if not company_display:
        return None
    company_slug = slugify_company(company_display)
    if not company_slug:
        return None

    # Schools
    schools_slugs, schools_display = _extract_schools(person)

    # Location
    city, state, office_slug = _extract_city_state(person)

    # Name — prefer explicit first/last, fall back to full_name split.
    first = (person.get("first_name") or "").strip()
    last = (person.get("last_name") or "").strip()
    if not (first or last):
        full = (person.get("full_name") or "").strip()
        parts = full.split(" ", 1)
        first = parts[0] if parts else ""
        last = parts[1] if len(parts) > 1 else ""
    name = f"{first} {last}".strip()

    return {
        "linkedin_id":     slug,
        "linkedin_url":    canon_url,
        "pdl_id":          (person.get("id") or "").strip(),
        "name":            name,
        "first_name":      first,
        "last_name":       last,
        "headline":        title_display,
        "title":           title_display.lower(),
        "title_level":     derive_title_level(title_display),
        "company":         company_slug,
        "company_display": company_display,
        "schools":         schools_slugs,
        "schools_display": schools_display,
        "joined_year":     joined_year,
        "office_location": office_slug,
        "city":            city,
        "state":           state,
        "country_code":    "US",
        # last_seen_at is stamped by writer.py with SERVER_TIMESTAMP
    }
