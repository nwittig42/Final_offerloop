"""
PDL (People Data Labs) client service - search, enrichment, and caching
"""
import requests
import json
import hashlib
import re
import time
from functools import lru_cache
from datetime import datetime
import requests.exceptions
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

from app.config import (
    PEOPLE_DATA_LABS_API_KEY, PDL_BASE_URL, PDL_METRO_AREAS,
    pdl_cache, CACHE_DURATION,
    ENABLE_INDUSTRY_EXPANSION,
)
from app.services.openai_client import get_openai_client
from app.services.metering import meter_call
from app.utils.retry import retry_with_backoff
from app.utils.role_taxonomy import (
    _TITLE_FAMILY_EXPANSIONS,
    _SENIORITY_ADJACENT_TITLES,
    _expand_titles_for_broadening,
    _expand_titles_seniority_adjacent,
)

# Create a session with connection pooling for better performance.
# Accept-Encoding: gzip is a free ~5× response-size reduction per PDL docs.
_session = requests.Session()
_session.headers.update({"Accept-Encoding": "gzip"})
_session_lock = Lock()

# Minimal fields needed for a contact card. data_include shrinks the response
# payload (bandwidth + parsing latency) but does NOT reduce per-record credit
# cost — credits are still charged per record returned.
# Email-source confidence ranking. Used by search_contacts_from_prompt to put
# verified emails ahead of best-guesses when max_contacts < len(candidates).
# Higher number = higher confidence.
EMAIL_SOURCE_RANK = {
    "pdl":                  100,  # PDL email already at current target domain
    "hunter_finder":         90,  # Hunter Email Finder score >= 80
    "neverbounce_verified":  85,  # Pattern + NeverBounce SMTP-confirmed valid
    "hunter_finder_risky":   70,  # Hunter Email Finder score 70-79
    "neverbounce_acceptall": 60,  # Pattern + NeverBounce says catch-all
    "pattern":               40,  # Hunter pattern synthesis, no SMTP verification
    "domain_generated":      30,  # Generic first.last@domain fallback
    "pdl_fallback":          20,  # PDL email at non-target domain
}
HIGH_CONFIDENCE_EMAIL_SOURCES = {"pdl", "hunter_finder", "neverbounce_verified"}


def _email_quality_score(contact: dict) -> int:
    """Composite email-confidence score used for sorting candidates.

    Combines:
      - source rank (dominant — see EMAIL_SOURCE_RANK)
      - verified bonus (+5 if EmailVerified is True)
      - underlying confidence score (Hunter Finder 0-100, capped at +4)
    """
    src = contact.get("EmailSource")
    base = EMAIL_SOURCE_RANK.get(src, 0)
    verified_bonus = 5 if contact.get("EmailVerified") else 0
    raw_score = int(contact.get("EmailConfidenceScore") or 0)
    score_bonus = min(raw_score // 20, 4)
    return base + verified_bonus + score_bonus


PDL_DATA_INCLUDE = ",".join([
    "id", "full_name", "first_name", "last_name",
    "job_title", "job_title_role", "job_title_levels", "job_title_sub_role",
    "job_company_name", "job_company_website",
    "job_company_location_locality", "job_company_location_region",
    "job_company_location_country", "job_company_location_metro",
    "location_locality", "location_region", "location_country", "location_metro",
    # `experience` is REQUIRED — extract_contact_from_pdl_person_enhanced reads
    # Title/Company from experience[0] (current job). Omitting it caused the
    # frontend Company/Role columns to render as "-".
    "experience",
    "education", "profiles", "emails",
    "linkedin_url", "linkedin_username",
    "work_email", "personal_emails", "recommended_personal_email",
    "industry",
])


"""
Enhanced alumni filtering for PDL client - ensures contacts actually attended the school as degree students
"""

def _school_aliases(raw: str) -> list[str]:
    """
    Generate comprehensive aliases for ANY school (not just USC/Stanford)
    Returns list of normalized variations that PDL might use
    """
    if not raw:
        return []

    # Clean and normalize input. Strip commas because PDL listings often
    # write "University of California, Berkeley" but the school_map keys
    # are stored without commas — without this normalization, both the
    # exact-key lookup and the substring check below miss.
    s = " ".join(str(raw).lower().replace(",", " ").split())
    aliases = {s}
    
    # Remove common words to get core name
    core = s
    for remove in [" university", " college", " school", " institute", " of technology", ", the"]:
        if remove in core:
            core = core.replace(remove, "").strip()
    
    if core and core != s:
        aliases.add(core)
    
    # Add "University of X" variants
    if not s.startswith("university of") and core:
        aliases.add(f"university of {core}")
    
    # Add variations with/without "the"
    if s.startswith("the "):
        aliases.add(s[4:])  # Remove "the"
    else:
        aliases.add(f"the {s}")
    
    # Comprehensive school-specific aliases map
    school_map = {
        # California schools
        "usc": ["university of southern california", "usc viterbi", "viterbi school of engineering", "southern california"],
        "university of southern california": ["usc", "usc viterbi", "viterbi school of engineering", "southern california"],
        "southern california": ["usc", "university of southern california", "usc viterbi"],
        
        # UC system. Every campus has both a "uc <campus>" key AND a
        # "university of california <campus>" key so a free-typed input
        # like "UC Davis" or a website-dropdown selection like "University
        # of California, Davis" both hit a school_map entry directly (not
        # just via substring fallback). Without these, the alias set
        # collapses to the literal input + the generic "the X" / "university
        # of X" expansions, missing variants like "UC Davis" → "Cal", which
        # leaves most PDL profiles unmatched.
        "ucla": ["university of california los angeles", "uc los angeles", "cal los angeles"],
        "uc los angeles": ["ucla", "university of california los angeles", "cal los angeles"],
        "university of california los angeles": ["ucla", "uc los angeles", "cal los angeles"],

        "berkeley": ["uc berkeley", "university of california berkeley", "cal", "california berkeley"],
        "uc berkeley": ["berkeley", "university of california berkeley", "cal", "california berkeley"],
        "university of california berkeley": ["berkeley", "uc berkeley", "cal", "california berkeley"],

        "ucsd": ["uc san diego", "university of california san diego", "california san diego"],
        "uc san diego": ["ucsd", "university of california san diego", "california san diego"],
        "university of california san diego": ["ucsd", "uc san diego", "california san diego"],

        "uci": ["uc irvine", "university of california irvine", "california irvine"],
        "uc irvine": ["uci", "university of california irvine", "california irvine"],
        "university of california irvine": ["uci", "uc irvine", "california irvine"],

        "ucsb": ["uc santa barbara", "university of california santa barbara", "california santa barbara"],
        "uc santa barbara": ["ucsb", "university of california santa barbara", "california santa barbara"],
        "university of california santa barbara": ["ucsb", "uc santa barbara", "california santa barbara"],

        "ucd": ["uc davis", "university of california davis", "california davis"],
        "uc davis": ["ucd", "university of california davis", "california davis"],
        "university of california davis": ["ucd", "uc davis", "california davis"],
        "davis": ["uc davis", "university of california davis", "ucd"],

        "ucsc": ["uc santa cruz", "university of california santa cruz", "california santa cruz"],
        "uc santa cruz": ["ucsc", "university of california santa cruz", "california santa cruz"],
        "university of california santa cruz": ["ucsc", "uc santa cruz", "california santa cruz"],

        "ucr": ["uc riverside", "university of california riverside", "california riverside"],
        "uc riverside": ["ucr", "university of california riverside", "california riverside"],
        "university of california riverside": ["ucr", "uc riverside", "california riverside"],

        "ucm": ["uc merced", "university of california merced", "california merced"],
        "uc merced": ["ucm", "university of california merced", "california merced"],
        "university of california merced": ["ucm", "uc merced", "california merced"],

        "ucsf": ["uc san francisco", "university of california san francisco"],
        "uc san francisco": ["ucsf", "university of california san francisco"],
        "university of california san francisco": ["ucsf", "uc san francisco"],
        
        "stanford": ["stanford university", "leland stanford junior university"],
        "stanford university": ["stanford", "leland stanford junior university"],
        
        "caltech": ["california institute of technology", "cal tech"],
        
        # Ivy League
        "harvard": ["harvard university", "harvard college"],
        "harvard university": ["harvard", "harvard college"],
        
        "yale": ["yale university"],
        "yale university": ["yale"],
        
        "princeton": ["princeton university"],
        "princeton university": ["princeton"],
        
        "columbia": ["columbia university", "columbia university in the city of new york"],
        "columbia university": ["columbia", "columbia university in the city of new york"],
        
        "penn": ["university of pennsylvania", "upenn", "wharton"],
        "university of pennsylvania": ["penn", "upenn", "wharton"],
        
        "brown": ["brown university"],
        "brown university": ["brown"],
        
        "dartmouth": ["dartmouth college", "dartmouth university"],
        "dartmouth college": ["dartmouth", "dartmouth university"],
        
        "cornell": ["cornell university"],
        "cornell university": ["cornell"],
        
        # Other top schools
        "mit": ["massachusetts institute of technology", "m.i.t."],
        "massachusetts institute of technology": ["mit", "m.i.t."],
        
        "nyu": ["new york university", "new york u"],
        "new york university": ["nyu", "new york u"],
        
        "duke": ["duke university"],
        "duke university": ["duke"],
        
        "northwestern": ["northwestern university"],
        "northwestern university": ["northwestern"],
        
        "chicago": ["university of chicago", "uchicago", "u of chicago"],
        "university of chicago": ["chicago", "uchicago", "u of chicago"],
        
        "michigan": ["university of michigan", "umich", "u of michigan", "michigan ann arbor"],
        "university of michigan": ["michigan", "umich", "u of michigan", "michigan ann arbor"],
        
        "virginia": ["university of virginia", "uva", "u of virginia"],
        "university of virginia": ["virginia", "uva", "u of virginia"],
        
        "notre dame": ["university of notre dame", "the university of notre dame"],
        "university of notre dame": ["notre dame", "the university of notre dame"],
        
        "carnegie mellon": ["carnegie mellon university", "cmu"],
        "carnegie mellon university": ["carnegie mellon", "cmu"],
        
        "georgia tech": ["georgia institute of technology", "georgia tech"],
        "georgia institute of technology": ["georgia tech"],
        
        "texas": ["university of texas", "ut austin", "texas austin"],
        "university of texas": ["texas", "ut austin", "texas austin"],
        
        "washington": ["university of washington", "uw", "washington seattle"],
        "university of washington": ["washington", "uw", "washington seattle"],
        
        "wisconsin": ["university of wisconsin", "uw madison", "wisconsin madison"],
        "university of wisconsin": ["wisconsin", "uw madison", "wisconsin madison"],
        
        "illinois": ["university of illinois", "uiuc", "illinois urbana champaign"],
        "university of illinois": ["illinois", "uiuc", "illinois urbana champaign"],
        
        # Add more as needed
    }
    
    # Check if any key matches and add expansions
    for key, expansions in school_map.items():
        if key in s or any(key in alias for alias in list(aliases)):
            aliases.update(expansions)
            aliases.add(key)  # Also add the short form

    # PDL stores most UC campuses as "University of California, <Campus>"
    # WITH a comma. Empirical counts from PDL person/search:
    #   "university of california, berkeley" -> 576,014 records
    #   "university of california berkeley"  -> 0 records
    #   "university of california, davis"    -> 294,670 records
    # Our normalization above strips commas so the school_map lookup hits;
    # here we re-emit the comma variant so PDL's exact-match school filter
    # actually finds the records. Without this, Berkeley + Google returned
    # 0 across the entire retry ladder while Stanford + Google worked fine.
    extra_comma_forms = set()
    uc_prefix = "university of california "
    for a in aliases:
        if a.startswith(uc_prefix) and len(a) > len(uc_prefix):
            campus = a[len(uc_prefix):]
            extra_comma_forms.add(f"university of california, {campus}")
    aliases.update(extra_comma_forms)

    # Clean up and return sorted
    cleaned = {" ".join(a.split()).strip() for a in aliases if a and len(a.strip()) > 1}
    return sorted(cleaned)


def _school_name_matches(school_name: str, aliases: list[str]) -> bool:
    """
    P3 FIX: Word-boundary-aware school name matching.
    Prevents false positives like "columbia" matching "british columbia" or
    "washington" matching "george washington university".

    Rules:
    - If either side is a single word (e.g. "columbia"), require it to appear
      as a word boundary match (not embedded in another word like "british columbia").
    - Multi-word aliases (e.g. "columbia university") use substring matching
      since they're already specific enough.
    - Exact match always passes.
    """
    if not school_name:
        return False
    for alias in aliases:
        if not alias:
            continue
        # Exact match
        if alias == school_name:
            return True
        # For short/single-word aliases, use word-boundary regex to prevent false positives
        alias_words = alias.split()
        if len(alias_words) == 1 and len(alias) < 15:
            # Single word like "columbia" — require word boundary match
            pattern = r'\b' + re.escape(alias) + r'\b'
            if re.search(pattern, school_name):
                # Extra guard: if school_name has a geographic prefix that changes meaning, reject
                # e.g. "british columbia" contains "columbia" but is NOT Columbia University
                if school_name != alias and alias in school_name:
                    # Check if what's before/after the alias is a common geographic qualifier
                    before = school_name[:school_name.index(alias)].strip()
                    if before and before.split()[-1] in _GEOGRAPHIC_QUALIFIERS:
                        continue
                return True
        else:
            # Multi-word alias: substring is fine (already specific)
            if alias in school_name or school_name in alias:
                return True
    return False


# Common geographic words that change a school's identity when prepended
_GEOGRAPHIC_QUALIFIERS = {
    "british", "western", "eastern", "northern", "southern", "central",
    "george", "north", "south", "west", "east", "new", "old",
    "saint", "st", "fort", "mount", "mt", "san", "santa",
}


def contact_matches_school(contact: dict, aliases: list[str], strictness: str = "normal") -> bool:
    """
    Consolidated alumni filter. Checks if a contact attended one of the target schools.

    strictness levels:
      - "strict": Requires degree indicators (degrees, field_of_study, start+end dates).
                  Falls back to College field only with degree keywords.
      - "normal": Accepts if school appears in education with any metadata (dates, field, etc.)
                  or if school name is substantial (>5 chars). Trusts College/EducationTop fields.
      - "loose":  Any word-boundary match in any education field. Most permissive.

    Args:
        contact: Contact dict (PDL raw format or extracted contact format)
        aliases: Pre-computed school aliases from _school_aliases()
        strictness: "strict", "normal", or "loose"
    """
    if not aliases:
        return False

    # --- Check structured education array ---
    edu = contact.get("education") or []
    if isinstance(edu, list):
        for e in edu:
            if not isinstance(e, dict):
                continue
            school = e.get("school") or {}
            school_name = ""
            if isinstance(school, dict):
                school_name = (school.get("name") or "").lower()
            elif isinstance(school, str):
                school_name = school.lower()
            if not school_name or len(school_name) < 3:
                continue

            if not _school_name_matches(school_name, aliases):
                continue

            if strictness == "loose":
                return True

            # Check for education metadata that indicates a real enrollment
            has_degree_indicator = bool(
                e.get("degrees") or e.get("degree")
                or e.get("field_of_study") or e.get("major")
            )
            has_dates = bool(e.get("start_date") or e.get("end_date"))

            if strictness == "strict":
                if has_degree_indicator or (has_dates and e.get("start_date")):
                    return True
                if any(w in school_name for w in ["university", "college", "institute"]):
                    if e.get("start_date") or e.get("field_of_study") or e.get("major"):
                        return True
            else:  # "normal"
                if has_degree_indicator or has_dates or e.get("start_date") or len(school_name) > 5:
                    return True

    # --- Fallback: top-level College field ---
    college = (contact.get("College") or contact.get("college") or "").lower()
    if college and len(college) > 2:
        if _school_name_matches(college, aliases):
            if strictness == "strict":
                degree_keywords = ["bachelor", "master", "phd", "mba", "degree", "graduated", "alumni"]
                if any(kw in college for kw in degree_keywords):
                    return True
            else:
                return True

    # --- Fallback: EducationTop field ---
    if strictness != "strict":
        edu_top = (contact.get("EducationTop") or "").lower()
        if edu_top and len(edu_top) > 2:
            if _school_name_matches(edu_top, aliases):
                return True

    return False


# Backward-compatible aliases that delegate to the consolidated function
def _contact_has_school_as_primary_education(contact: dict, aliases: list[str]) -> bool:
    return contact_matches_school(contact, aliases, strictness="strict")

def _contact_has_school_as_primary_education_lenient(contact: dict, aliases: list[str]) -> bool:
    return contact_matches_school(contact, aliases, strictness="normal")
def normalize_company_for_identity(raw: str) -> str:
    """Collapse the ways one employer is written, for identity purposes only.

    Deliberately conservative: strips punctuation and legal suffixes only, so
    "Discord" and "Discord, Inc." hash to the same person, while two genuinely
    different employers still differ on company. Ported from mobile's
    pdl_client.py (2026-07-14 duplicate-contact fix)."""
    import re as _re
    c = (raw or "").lower().strip()
    c = _re.sub(r"[.,()]", " ", c)
    c = _re.sub(
        r"\b(inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|plc|gmbh|"
        r"holdings|holding|group)\b",
        " ",
        c,
    )
    c = _re.sub(r"\s+", " ", c).strip()
    return c


def _contact_hash(contact: dict) -> tuple:
    """Generate a tuple of key fields to identify a contact uniquely"""
    first = (contact.get("FirstName") or "").lower().strip()
    last = (contact.get("LastName") or "").lower().strip()
    email = (contact.get("Email") or "").lower().strip()
    company = normalize_company_for_identity(contact.get("Company") or "")

    # ALWAYS use (first, last, company) for identity
    # Email can be added later (PDL→Hunter), so it's not reliable for deduplication
    return (first, last, company)
def get_contact_identity(contact: dict) -> str:
    """Generate a unique identity string for a contact"""
    return "||".join(_contact_hash(contact))
def _fetch_verified_alumni_contacts(
    primary_title, similar_titles, cleaned_company,
    location_strategy, job_title_enrichment,
    max_contacts, college_alumni, excluded_keys=None  # ADD excluded_keys parameter
):
    """
    Fetch contacts in batches until we have enough verified alumni
    Guarantees to return the requested number if they exist
    """
    aliases = _school_aliases(college_alumni)
    if not aliases:
        print(f"⚠️ No aliases found for {college_alumni}")
        return []
    
    excluded_keys = excluded_keys or set()  # ADD this line
    verified_alumni = []
    all_fetched_contacts = []
    batch_size = max_contacts * 2  # Start with 2x the requested amount
    max_total_fetch = 200  # Increased to 200 to handle low alumni rates (e.g., 10% = 20 alumni from 200 contacts)
    total_fetched = 0
    attempts = 0
    max_attempts = 4
    
    print(f"🎓 Starting alumni search for {max_contacts} verified {college_alumni} graduates")
    print(f"   Excluding {len(excluded_keys)} previously seen contacts")  # ADD this line
    
    while len(verified_alumni) < max_contacts and attempts < max_attempts and total_fetched < max_total_fetch:
        attempts += 1
        
        # Calculate how many to fetch this round
        current_batch_size = min(batch_size, max_total_fetch - total_fetched)
        if current_batch_size <= 0:
            break
            
        print(f"\n📥 Attempt {attempts}: Fetching {current_batch_size} contacts...")
        
        # Fetch contacts using the appropriate strategy
        if location_strategy['strategy'] == 'metro_primary':
            batch_contacts = try_metro_search_optimized(
                primary_title, similar_titles, cleaned_company,
                location_strategy, current_batch_size,
                college_alumni=college_alumni,
                exclude_keys=excluded_keys  # ADD this parameter
            )
            
            # If not enough, try locality
            if len(batch_contacts) < current_batch_size:
                locality_contacts = try_locality_search_optimized(
                    primary_title, similar_titles, cleaned_company,
                    location_strategy, current_batch_size - len(batch_contacts),
                    college_alumni=college_alumni,
                    exclude_keys=excluded_keys  # ADD this parameter
                )
                batch_contacts.extend([c for c in locality_contacts if c not in batch_contacts])
        else:
            batch_contacts = try_locality_search_optimized(
                primary_title, similar_titles, cleaned_company,
                location_strategy, current_batch_size,
                college_alumni=college_alumni,
                exclude_keys=excluded_keys  # ADD this parameter
            )
            
            # If not enough, try broader search
            if len(batch_contacts) < current_batch_size:
                broader_contacts = try_job_title_levels_search_enhanced(
                    job_title_enrichment, cleaned_company,
                    location_strategy['city'], location_strategy['state'],
                    current_batch_size - len(batch_contacts),
                    college_alumni=college_alumni,
                    exclude_keys=excluded_keys  # ADD this parameter
                )
                batch_contacts.extend([c for c in broader_contacts if c not in batch_contacts])
        
        # Track what we've fetched (with duplicate prevention)
        for contact in batch_contacts:
            if contact not in all_fetched_contacts:
                all_fetched_contacts.append(contact)
        
        total_fetched += len(batch_contacts)
        
        # Apply strict alumni filter to new batch
        # IMPORTANT: Check alumni status FIRST, then exclude duplicates
        new_verified = 0
        for contact in batch_contacts:
            # First verify if they're alumni
            if _contact_has_school_as_primary_education_lenient(contact, aliases):
                # Only now check if we've already added them (by identity, not exclude_keys)
                contact_key = get_contact_identity(contact)
                
                # Skip if already in verified_alumni (duplicate check)
                already_added = any(get_contact_identity(v) == contact_key for v in verified_alumni)
                if already_added:
                    continue
                
                # Skip if in excluded_keys (user's contact library)
                if contact_key in excluded_keys:
                    continue
                
                # Add to verified alumni
                verified_alumni.append(contact)
                new_verified += 1
                
                # Log each verified alumni found
                name = f"{contact.get('FirstName', '')} {contact.get('LastName', '')}".strip()
                print(f"   ✓ Verified alumni #{len(verified_alumni)}: {name}")
                    
                if len(verified_alumni) >= max_contacts:
                    break
        
        print(f"   Found {new_verified} new verified alumni from {len(batch_contacts)} contacts")
        print(f"   Total verified: {len(verified_alumni)}/{max_contacts}")
        
        # If we're not finding many alumni, increase batch size for next attempt
        if new_verified < batch_size * 0.2:  # Less than 20% success rate
            batch_size = min(batch_size * 2, 25)  # Double the batch size, cap at 25
            print(f"   📈 Low alumni rate, increasing next batch size to {batch_size}")
    
    # Final summary
    print(f"\n🎓 Alumni search complete:")
    print(f"   Total contacts fetched: {total_fetched}")
    print(f"   Total verified {college_alumni} alumni: {len(verified_alumni)}")
    
    if len(verified_alumni) < max_contacts:
        print(f"   ⚠️ Only found {len(verified_alumni)} verified alumni (requested {max_contacts})")
        print(f"   Consider broadening search criteria or removing company/location filters")
    else:
        print(f"   ✅ Successfully found {max_contacts} verified alumni")
    
    return verified_alumni[:max_contacts]



def _fetch_contacts_standard(
    primary_title, similar_titles, cleaned_company,
    location_strategy, job_title_enrichment,
    max_contacts, exclude_keys=None
):
    """
    Standard contact fetching without alumni filter (original logic)
    DEPRECATED: Use _fetch_contacts_standard_parallel for better performance
    """
    excluded_keys = exclude_keys or set()
    contacts = []
    
    if location_strategy['strategy'] == 'metro_primary':
        contacts = try_metro_search_optimized(
            primary_title, similar_titles, cleaned_company,
            location_strategy, max_contacts,
            college_alumni=None,
            exclude_keys=excluded_keys  # ✅ PASS EXCLUDE_KEYS
        )
        
        if len(contacts) < max_contacts:
            print(f"Metro results insufficient ({len(contacts)}), adding locality results")
            locality_contacts = try_locality_search_optimized(
                primary_title, similar_titles, cleaned_company,
                location_strategy, max_contacts - len(contacts),
                college_alumni=None,
                exclude_keys=excluded_keys
            )
            contacts.extend([c for c in locality_contacts if c not in contacts])
    else:
        contacts = try_locality_search_optimized(
            primary_title, similar_titles, cleaned_company,
            location_strategy, max_contacts,
            college_alumni=None,
            exclude_keys=excluded_keys
        )
        
        if len(contacts) < max_contacts:
            print(f"Locality results insufficient ({len(contacts)}), trying broader search")
            broader_contacts = try_job_title_levels_search_enhanced(
                job_title_enrichment, cleaned_company,
                location_strategy['city'], location_strategy['state'],
                max_contacts - len(contacts),
                college_alumni=None,
                exclude_keys=excluded_keys  # ✅ PASS EXCLUDE_KEYS
            )
            contacts.extend([c for c in broader_contacts if c not in contacts])
    
    return contacts


def _fetch_contacts_standard_parallel(
    primary_title, similar_titles, cleaned_company,
    location_strategy, job_title_enrichment,
    max_contacts, exclude_keys=None
):
    """
    OPTIMIZED: Parallel contact fetching - runs multiple search strategies simultaneously
    This significantly reduces search time by trying all strategies in parallel
    """
    excluded_keys = exclude_keys or set()
    contacts = []
    seen_identities = set()
    
    import time
    search_start = time.time()
    
    # Determine which searches to run based on location strategy
    searches_to_run = []
    
    if location_strategy['strategy'] == 'metro_primary':
        # Run metro and locality searches in parallel
        searches_to_run.append(('metro', lambda: try_metro_search_optimized(
            primary_title, similar_titles, cleaned_company,
            location_strategy, max_contacts,
            college_alumni=None,
            exclude_keys=excluded_keys
        )))
        searches_to_run.append(('locality', lambda: try_locality_search_optimized(
            primary_title, similar_titles, cleaned_company,
            location_strategy, max_contacts,
            college_alumni=None,
            exclude_keys=excluded_keys
        )))
    else:
        # Run locality search first
        searches_to_run.append(('locality', lambda: try_locality_search_optimized(
            primary_title, similar_titles, cleaned_company,
            location_strategy, max_contacts,
            college_alumni=None,
            exclude_keys=excluded_keys
        )))
    
    # Always prepare broader search as backup
    broader_search = lambda: try_job_title_levels_search_enhanced(
        job_title_enrichment, cleaned_company,
        location_strategy['city'], location_strategy['state'],
        max_contacts,
        college_alumni=None,
        exclude_keys=excluded_keys
    )
    
    # Run initial searches in parallel
    if searches_to_run:
        print(f"⚡ Running {len(searches_to_run)} search strategies in parallel...")
        with ThreadPoolExecutor(max_workers=len(searches_to_run)) as executor:
            futures = {executor.submit(search_func): name for name, search_func in searches_to_run}
            
            for future in as_completed(futures):
                search_name = futures[future]
                try:
                    results = future.result()
                    print(f"✅ {search_name} search returned {len(results)} contacts")
                    
                    # Deduplicate by identity
                    for contact in results:
                        contact_id = get_contact_identity(contact)
                        if contact_id not in seen_identities:
                            contacts.append(contact)
                            seen_identities.add(contact_id)
                            if len(contacts) >= max_contacts:
                                break
                except Exception as e:
                    print(f"❌ {search_name} search failed: {e}")
    
    # If we still need more contacts, try broader search
    if len(contacts) < max_contacts:
        needed = max_contacts - len(contacts)
        print(f"⚡ Running broader search for {needed} more contacts...")
        try:
            broader_contacts = broader_search()
            for contact in broader_contacts:
                contact_id = get_contact_identity(contact)
                if contact_id not in seen_identities:
                    contacts.append(contact)
                    seen_identities.add(contact_id)
                    if len(contacts) >= max_contacts:
                        break
        except Exception as e:
            print(f"❌ Broader search failed: {e}")
    
    search_time = time.time() - search_start
    print(f"⚡ Parallel search completed in {search_time:.2f}s - found {len(contacts)} contacts")
    
    return contacts[:max_contacts]

def _contact_has_school_alias(c: dict, aliases: list[str]) -> bool:
    """Backward-compatible loose alias check. Delegates to consolidated contact_matches_school."""
    return contact_matches_school(c, aliases, strictness="loose")


def apply_strict_alumni_filter(contacts: list, college_alumni: str, use_strict: bool = True) -> list:
    """Apply alumni filtering using the consolidated contact_matches_school function."""
    if not college_alumni:
        return contacts
    aliases = _school_aliases(college_alumni)
    if not aliases:
        return contacts
    strictness = "strict" if use_strict else "loose"
    filtered = [c for c in contacts if contact_matches_school(c, aliases, strictness=strictness)]
    if len(filtered) < len(contacts):
        print(f"🎓 Alumni filter ({strictness}): {len(contacts)} → {len(filtered)} contacts")
    return filtered


# Enhanced PDL query builder for alumni search
def build_enhanced_alumni_query(aliases: list[str]) -> dict:
    """
    Build a more sophisticated alumni query that prioritizes actual degree holders
    
    This query uses boosting to rank actual alumni higher while still catching edge cases
    """
    should_clauses = []
    
    for alias in aliases:
        # High boost for full school name in education
        should_clauses.append({
            "match_phrase": {
                "education.school.name": {
                    "query": alias,
                    "boost": 3.0  # High priority
                }
            }
        })
        
        # Medium boost for school + degree indicators
        degree_terms = ["bachelor", "bs", "ba", "master", "ms", "ma", "mba", "phd", "degree"]
        for degree in degree_terms:
            should_clauses.append({
                "bool": {
                    "must": [
                        {"match_phrase": {"education.school.name": alias}},
                        {"match": {"education.degrees": degree}}
                    ],
                    "boost": 5.0  # Very high priority for confirmed degrees
                }
            })
        
        # Lower boost for school mentions without degree confirmation
        should_clauses.append({
            "match": {
                "education.summary": {
                    "query": alias,
                    "boost": 0.5  # Low priority
                }
            }
        })
    
    return {"bool": {"should": should_clauses}}


# Integration point for your existing code
def search_contacts_with_smart_location_strategy_enhanced(
    job_title, company, location, max_contacts=8, college_alumni=None, 
    use_strict_alumni_filter=True
):
    """
    Enhanced version of your existing search function with better alumni filtering
    
    This is a wrapper that would call your existing function and apply enhanced filtering
    """
    # Import your existing function (adjust import as needed)
    from app.services.pdl_client import search_contacts_with_smart_location_strategy
    
    # Call existing search function
    contacts = search_contacts_with_smart_location_strategy(
        job_title, company, location, max_contacts=max_contacts * 2,  # Get extra to account for filtering
        college_alumni=college_alumni  # Still pass for query-level filtering
    )
    
    # Apply enhanced post-processing filter
    if college_alumni and use_strict_alumni_filter:
        contacts = apply_strict_alumni_filter(contacts, college_alumni, use_strict=True)
    
    # Return up to max_contacts
    return contacts[:max_contacts]


@lru_cache(maxsize=1000)
def cached_enrich_job_title(job_title):
    """Cache job title enrichments to avoid repeated API calls"""
    return enrich_job_title_with_pdl(job_title)


@lru_cache(maxsize=1000)
def cached_clean_company(company):
    """Cache company cleaning to avoid repeated API calls"""
    return clean_company_name(company) if company else ''


@lru_cache(maxsize=1000)
def cached_clean_location(location):
    """Cache location cleaning to avoid repeated API calls"""
    return clean_location_name(location) if location else ''


_clean_company_cache: dict[str, str] = {}
_clean_company_cache_lock = Lock()

# Curated map of user-supplied company short-names/acronyms → primary domain.
# PRIMARY hook for PDL Person Search: match_phrase on job_company_website is
# orders of magnitude more reliable than match_phrase on job_company_name,
# because PDL stores job_company_name under unguessable canonicals
# (e.g. BCG is "boston consulting group (bcg)" — parens and all — and the
# plain string "bcg" matches 776 unrelated low-quality records).
#
# Probed empirically 2026-06-23. PDL's Company Cleaner / Enrichment can NOT
# be trusted for acronym resolution: /v5/company/enrich?name=BCG returns an
# unrelated 35-person NZ IT consultancy. /v5/company/clean?name=BCG returns
# name=None.
#
# Keys are lowercased; only unambiguous keys included. "MS" is intentionally
# Morgan Stanley (Microsoft users type "microsoft" or "msft"). Ambiguous keys
# like "CS" and "DB" are deliberately omitted — better to miss than wrong.
COMPANY_DOMAIN_MAP = {
    # Consulting
    "bcg": "bcg.com",
    "boston consulting group": "bcg.com",
    "mckinsey": "mckinsey.com",
    "mckinsey & company": "mckinsey.com",
    "bain": "bain.com",
    "bain & company": "bain.com",
    "bain and company": "bain.com",
    "deloitte": "deloitte.com",
    "pwc": "pwc.com",
    "ey": "ey.com",
    "ernst & young": "ey.com",
    "kpmg": "kpmg.com",
    "accenture": "accenture.com",
    "oliver wyman": "oliverwyman.com",
    "l.e.k.": "lek.com",
    "lek": "lek.com",
    "alvarez & marsal": "alvarezandmarsal.com",
    # Banking — bulge bracket + boutiques
    "jpm": "jpmorgan.com",
    "jp morgan": "jpmorgan.com",
    "jpmorgan": "jpmorgan.com",
    "jpmorgan chase": "jpmorgan.com",
    "gs": "goldmansachs.com",
    "goldman": "goldmansachs.com",
    "goldman sachs": "goldmansachs.com",
    "ms": "morganstanley.com",
    "morgan stanley": "morganstanley.com",
    "bofa": "bofa.com",
    "baml": "bofa.com",
    "bank of america": "bofa.com",
    "citi": "citi.com",
    "citigroup": "citi.com",
    "barclays": "barclays.com",
    "ubs": "ubs.com",
    "deutsche bank": "db.com",
    "jefferies": "jefferies.com",
    "houlihan lokey": "hl.com",
    "lazard": "lazard.com",
    "evercore": "evercore.com",
    "moelis": "moelis.com",
    "rothschild": "rothschild.com",
    "guggenheim": "guggenheimpartners.com",
    "centerview": "centerview.com",
    "piper sandler": "psc.com",
    "raymond james": "raymondjames.com",
    "pjt": "pjtpartners.com",
    "pjt partners": "pjtpartners.com",
    "perella weinberg": "pwpartners.com",
    "wells fargo": "wellsfargo.com",
    "rbc": "rbc.com",
    "nomura": "nomura.com",
    # Private equity / hedge funds
    "kkr": "kkr.com",
    "bx": "blackstone.com",
    "blackstone": "blackstone.com",
    "apollo": "apollo.com",
    "carlyle": "carlyle.com",
    "tpg": "tpg.com",
    "bain capital": "baincapital.com",
    "brookfield": "brookfield.com",
    "de shaw": "deshaw.com",
    "d.e. shaw": "deshaw.com",
    "citadel": "citadel.com",
    "point72": "point72.com",
    "p72": "point72.com",
    "millennium": "mlp.com",
    "two sigma": "twosigma.com",
    "bridgewater": "bridgewater.com",
    "jane street": "janestreet.com",
    "jump trading": "jumptrading.com",
    # Tech
    "google": "google.com",
    "alphabet": "google.com",
    "meta": "meta.com",
    "facebook": "meta.com",
    "fb": "meta.com",
    "amazon": "amazon.com",
    "aws": "amazon.com",
    "microsoft": "microsoft.com",
    "msft": "microsoft.com",
    "apple": "apple.com",
    "netflix": "netflix.com",
    "spotify": "spotify.com",
    "tesla": "tesla.com",
    "nvidia": "nvidia.com",
    "openai": "openai.com",
    "anthropic": "anthropic.com",
    "stripe": "stripe.com",
    "palantir": "palantir.com",
    "databricks": "databricks.com",
    "snowflake": "snowflake.com",
    "airbnb": "airbnb.com",
    "uber": "uber.com",
    "lyft": "lyft.com",
    "doordash": "doordash.com",
    "robinhood": "robinhood.com",
    "coinbase": "coinbase.com",
    "shopify": "shopify.com",
    "figma": "figma.com",
    "notion": "notion.so",
    "canva": "canva.com",
    "ibm": "ibm.com",
    "oracle": "oracle.com",
    "salesforce": "salesforce.com",
    "adobe": "adobe.com",
    # Automotive
    "bmw": "bmwgroup.com",
    "bmw group": "bmwgroup.com",
    "mercedes": "mercedes-benz.com",
    "mercedes-benz": "mercedes-benz.com",
    "mercedes benz": "mercedes-benz.com",
    "rivian": "rivian.com",
    "ford": "ford.com",
    "gm": "gm.com",
    "general motors": "gm.com",
    "toyota": "toyota.com",
    "hyundai": "hyundai.com",
    "honda": "honda.co.jp",
    "koenigsegg": "koenigsegg.com",
    # Defense / aerospace
    "northrop": "northropgrumman.com",
    "northrop grumman": "northropgrumman.com",
    "raytheon": "raytheon.com",
    "rtx": "rtx.com",
    "lockheed": "lockheedmartin.com",
    "lockheed martin": "lockheedmartin.com",
    "boeing": "boeing.com",
    "general dynamics": "gd.com",
    "l3harris": "l3harris.com",
    "ti": "ti.com",
    "texas instruments": "ti.com",
}

# Long-tail extension — student-relevant companies added 2026-07-10 to close
# the silent zero-hit gap (e.g. "Find 3 Spotify SWEs" returned zero because
# job_company_name canonicals mismatch). Values are the primary domain PDL
# indexes those companies' people under in `job_company_website`.
COMPANY_DOMAIN_MAP.update({
    # -------- Consulting (extended) --------
    "strategy&": "strategyand.pwc.com",
    "strategy and": "strategyand.pwc.com",
    "monitor deloitte": "deloitte.com",
    "deloitte consulting": "deloitte.com",
    "parthenon": "parthenon.ey.com",
    "ey-parthenon": "parthenon.ey.com",
    "kearney": "kearney.com",
    "a.t. kearney": "kearney.com",
    "at kearney": "kearney.com",
    "roland berger": "rolandberger.com",
    "arthur d little": "adlittle.com",
    "arthur d. little": "adlittle.com",
    "zs": "zs.com",
    "zs associates": "zs.com",
    "simon-kucher": "simon-kucher.com",
    "simon kucher": "simon-kucher.com",
    "analysis group": "analysisgroup.com",
    "cornerstone research": "cornerstone.com",
    "charles river associates": "crai.com",
    "cra": "crai.com",
    "compass lexecon": "compasslexecon.com",
    "nera": "nera.com",
    "brattle": "brattle.com",
    "bridgespan": "bridgespan.org",
    "dalberg": "dalberg.com",
    "cambridge associates": "cambridgeassociates.com",
    "mercer": "mercer.com",
    "aon": "aon.com",
    "willis towers watson": "wtwco.com",
    "wtw": "wtwco.com",
    "milliman": "milliman.com",
    "egon zehnder": "egonzehnder.com",
    "spencer stuart": "spencerstuart.com",
    "korn ferry": "kornferry.com",
    "russell reynolds": "russellreynolds.com",
    "fti consulting": "fticonsulting.com",
    "fti": "fticonsulting.com",
    "alixpartners": "alixpartners.com",
    "alix partners": "alixpartners.com",
    "berkeley research group": "thinkbrg.com",
    "brg": "thinkbrg.com",
    "huron consulting": "huronconsultinggroup.com",
    "huron": "huronconsultinggroup.com",
    "west monroe": "westmonroe.com",
    "north highland": "northhighland.com",
    "slalom": "slalom.com",
    "protiviti": "protiviti.com",
    "publicis sapient": "publicissapient.com",
    "quantumblack": "mckinsey.com",
    "gartner": "gartner.com",
    "forrester": "forrester.com",
    "guidepoint": "guidepoint.com",
    "third bridge": "thirdbridge.com",
    "tegus": "tegus.com",
    "alphasights": "alphasights.com",

    # -------- Banking (extended) --------
    "hsbc": "hsbc.com",
    "bnp paribas": "bnpparibas.com",
    "credit agricole": "credit-agricole.com",
    "societe generale": "societegenerale.com",
    "socgen": "societegenerale.com",
    "natixis": "natixis.com",
    "standard chartered": "sc.com",
    "ing": "ing.com",
    "santander": "santander.com",
    "bbva": "bbva.com",
    "bmo": "bmo.com",
    "bmo capital markets": "bmocm.com",
    "scotiabank": "scotiabank.com",
    "scotia": "scotiabank.com",
    "td bank": "td.com",
    "td securities": "tdsecurities.com",
    "cibc": "cibc.com",
    "mizuho": "mizuhogroup.com",
    "mufg": "mufg.jp",
    "smbc": "smbc.co.jp",
    "daiwa": "daiwa.jp",
    "macquarie": "macquarie.com",
    "pnc": "pnc.com",
    "truist": "truist.com",
    "us bank": "usbank.com",
    "u.s. bank": "usbank.com",
    "keybank": "key.com",
    "keybanc": "kbcm.com",
    "keybanc capital markets": "kbcm.com",
    "regions bank": "regions.com",
    "regions": "regions.com",
    "fifth third": "53.com",
    "comerica": "comerica.com",
    "m&t bank": "mtb.com",
    "huntington": "huntington.com",
    "citizens bank": "citizensbank.com",
    "citizens": "citizensbank.com",
    "capital one": "capitalone.com",
    "capitalone": "capitalone.com",
    "chase": "chase.com",
    "american express": "aexp.com",
    "amex": "aexp.com",
    "william blair": "williamblair.com",
    "baird": "rwbaird.com",
    "robert w baird": "rwbaird.com",
    "stifel": "stifel.com",
    "cowen": "cowen.com",
    "oppenheimer": "opco.com",
    "cantor fitzgerald": "cantor.com",
    "btig": "btig.com",
    "kbw": "kbw.com",
    "keefe bruyette & woods": "kbw.com",
    "wolfe research": "wolferesearch.com",
    "bernstein": "bernsteinresearch.com",
    "sanford bernstein": "bernsteinresearch.com",
    "redburn": "redburn-atlantic.com",
    "liontree": "liontree.com",
    "qatalyst": "qatalyst.com",
    "ft partners": "ftpartners.com",
    "financial technology partners": "ftpartners.com",
    "greenhill": "greenhill.com",
    "solomon partners": "solomonpartners.com",
    "ducera": "ducerapartners.com",
    "loop capital": "loopcapital.com",
    "siebert williams shank": "siebertwilliams.com",
    "financo": "financo.com",
    "union square advisors": "unionsq.com",
    "kroll": "kroll.com",
    "duff & phelps": "kroll.com",

    # -------- Private Equity --------
    "warburg pincus": "warburgpincus.com",
    "advent international": "adventinternational.com",
    "advent": "adventinternational.com",
    "cvc": "cvc.com",
    "cvc capital partners": "cvc.com",
    "eqt": "eqtgroup.com",
    "eqt partners": "eqtgroup.com",
    "vista equity partners": "vistaequitypartners.com",
    "vista": "vistaequitypartners.com",
    "thoma bravo": "thomabravo.com",
    "silver lake": "silverlake.com",
    "insight partners": "insightpartners.com",
    "general atlantic": "generalatlantic.com",
    "providence equity": "provequity.com",
    "hellman & friedman": "hf.com",
    "leonard green": "leonardgreen.com",
    "leonard green & partners": "leonardgreen.com",
    "roark capital": "roarkcapital.com",
    "ares management": "aresmgmt.com",
    "ares": "aresmgmt.com",
    "sixth street": "sixthstreet.com",
    "oaktree": "oaktreecapital.com",
    "oaktree capital": "oaktreecapital.com",
    "golden gate capital": "goldengatecap.com",
    "clearlake": "clearlake.com",
    "clearlake capital": "clearlake.com",
    "berkshire partners": "berkshirepartners.com",
    "ta associates": "ta.com",
    "summit partners": "summitpartners.com",
    "gtcr": "gtcr.com",
    "new mountain": "newmountaincapital.com",
    "charlesbank": "charlesbank.com",
    "hg": "hgcapital.com",
    "hg capital": "hgcapital.com",
    "nordic capital": "nordiccapital.com",
    "bridgepoint": "bridgepoint.eu",
    "pai partners": "paipartners.com",
    "cinven": "cinven.com",
    "ontario teachers": "otpp.com",
    "cdpq": "cdpq.com",
    "gic": "gic.com.sg",
    "temasek": "temasek.com.sg",
    "mubadala": "mubadala.com",
    "adia": "adia.ae",
    "pif": "pif.gov.sa",

    # -------- Hedge funds / Prop trading --------
    "aqr": "aqr.com",
    "aqr capital": "aqr.com",
    "renaissance technologies": "rentec.com",
    "rentec": "rentec.com",
    "man group": "man.com",
    "sculptor": "sculptor.com",
    "elliott management": "elliottmgmt.com",
    "elliott": "elliottmgmt.com",
    "third point": "thirdpoint.com",
    "valueact": "valueactcapital.com",
    "pershing square": "pershingsquareholdings.com",
    "baupost": "baupost.com",
    "farallon": "faralloncapital.com",
    "balyasny": "bam.com",
    "verition": "verition.com",
    "exodus point": "exoduspoint.com",
    "exoduspoint": "exoduspoint.com",
    "schonfeld": "schonfeld.com",
    "hbk": "hbk.com",
    "hbk capital": "hbk.com",
    "squarepoint": "squarepoint-capital.com",
    "hudson river trading": "hudsonrivertrading.com",
    "hrt": "hudsonrivertrading.com",
    "optiver": "optiver.com",
    "imc trading": "imc.com",
    "imc": "imc.com",
    "drw": "drw.com",
    "susquehanna": "sig.com",
    "sig": "sig.com",
    "susquehanna international group": "sig.com",
    "akuna capital": "akunacapital.com",
    "akuna": "akunacapital.com",
    "cts": "ctc.com",
    "chicago trading company": "ctc.com",
    "wolverine trading": "wolve.com",
    "peak6": "peak6.com",
    "geneva trading": "genevatrading.com",
    "belvedere trading": "belvederetrading.com",
    "old mission": "oldmissioncapital.com",
    "flow traders": "flowtraders.com",
    "virtu financial": "virtu.com",

    # -------- Asset management --------
    "blackrock": "blackrock.com",
    "vanguard": "vanguard.com",
    "fidelity": "fidelity.com",
    "fidelity investments": "fidelity.com",
    "t. rowe price": "troweprice.com",
    "t rowe price": "troweprice.com",
    "state street": "statestreet.com",
    "state street global advisors": "ssga.com",
    "ssga": "ssga.com",
    "bny mellon": "bnymellon.com",
    "bank of new york mellon": "bnymellon.com",
    "invesco": "invesco.com",
    "franklin templeton": "franklintempleton.com",
    "pimco": "pimco.com",
    "pgim": "pgim.com",
    "nuveen": "nuveen.com",
    "neuberger berman": "nb.com",
    "wellington management": "wellington.com",
    "wellington": "wellington.com",
    "capital group": "capitalgroup.com",
    "american century": "americancentury.com",
    "mfs": "mfs.com",
    "federated hermes": "federatedhermes.com",
    "lord abbett": "lordabbett.com",
    "dodge & cox": "dodgeandcox.com",
    "northern trust": "northerntrust.com",
    "ariel investments": "arielinvestments.com",
    "voya": "voya.com",
    "voya financial": "voya.com",
    "manulife": "manulife.com",
    "john hancock": "johnhancock.com",
    "tiaa": "tiaa.org",
    "prudential": "prudential.com",
    "metlife": "metlife.com",
    "aig": "aig.com",
    "chubb": "chubb.com",
    "travelers": "travelers.com",
    "the travelers": "travelers.com",
    "hartford": "thehartford.com",
    "progressive": "progressive.com",
    "allstate": "allstate.com",
    "geico": "geico.com",
    "state farm": "statefarm.com",
    "nationwide": "nationwide.com",
    "liberty mutual": "libertymutual.com",
    "aflac": "aflac.com",
    "charles schwab": "schwab.com",
    "schwab": "schwab.com",
    "e*trade": "etrade.com",
    "etrade": "etrade.com",

    # -------- Venture Capital --------
    "a16z": "a16z.com",
    "andreessen horowitz": "a16z.com",
    "sequoia": "sequoiacap.com",
    "sequoia capital": "sequoiacap.com",
    "benchmark": "benchmark.com",
    "kleiner perkins": "kleinerperkins.com",
    "accel": "accel.com",
    "greylock": "greylock.com",
    "founders fund": "foundersfund.com",
    "lightspeed": "lsvp.com",
    "lightspeed venture partners": "lsvp.com",
    "bessemer": "bvp.com",
    "bessemer venture partners": "bvp.com",
    "bvp": "bvp.com",
    "nea": "nea.com",
    "new enterprise associates": "nea.com",
    "ivp": "ivp.com",
    "institutional venture partners": "ivp.com",
    "tcv": "tcv.com",
    "coatue": "coatue.com",
    "tiger global": "tigerglobal.com",
    "dst global": "dst-global.com",
    "usv": "usv.com",
    "union square ventures": "usv.com",
    "first round": "firstround.com",
    "first round capital": "firstround.com",
    "y combinator": "ycombinator.com",
    "ycombinator": "ycombinator.com",
    "yc": "ycombinator.com",
    "ribbit capital": "ribbitcap.com",
    "ribbit": "ribbitcap.com",
    "iconiq": "iconiqcapital.com",
    "iconiq capital": "iconiqcapital.com",
    "general catalyst": "generalcatalyst.com",
    "battery ventures": "battery.com",
    "battery": "battery.com",
    "ggv capital": "ggvc.com",
    "ggv": "ggvc.com",
    "redpoint": "redpoint.com",
    "redpoint ventures": "redpoint.com",
    "emergence capital": "emcap.com",
    "menlo ventures": "menlovc.com",
    "norwest": "nvp.com",
    "norwest venture partners": "nvp.com",
    "khosla ventures": "khoslaventures.com",
    "felicis": "felicis.com",
    "thrive capital": "thrivecap.com",
    "lux capital": "luxcapital.com",
    "spark capital": "sparkcapital.com",
    "boldstart": "boldstart.vc",
    "upfront": "upfront.com",
    "upfront ventures": "upfront.com",
    "precursor": "precursorvc.com",
    "initialized": "initialized.com",
    "uncork capital": "uncorkcapital.com",
    "floodgate": "floodgate.com",
    "cowboy ventures": "cowboy.vc",
    "8vc": "8vc.com",

    # -------- Big Tech (extended) --------
    "linkedin": "linkedin.com",
    "youtube": "youtube.com",
    "waymo": "waymo.com",
    "deepmind": "deepmind.com",
    "google cloud": "google.com",
    "google deepmind": "deepmind.com",
    "azure": "microsoft.com",
    "github": "github.com",
    "aws": "amazon.com",
    "whatsapp": "whatsapp.com",
    "instagram": "instagram.com",
    "reality labs": "meta.com",

    # -------- Consumer Social / Media --------
    "x": "x.com",
    "twitter": "x.com",
    "snap": "snap.com",
    "snapchat": "snap.com",
    "pinterest": "pinterest.com",
    "reddit": "reddit.com",
    "tiktok": "tiktok.com",
    "bytedance": "bytedance.com",
    "discord": "discord.com",
    "twitch": "twitch.tv",
    "roblox": "roblox.com",
    "electronic arts": "ea.com",
    "ea": "ea.com",
    "activision": "activision.com",
    "activision blizzard": "activisionblizzard.com",
    "blizzard": "blizzard.com",
    "blizzard entertainment": "blizzard.com",
    "riot games": "riotgames.com",
    "riot": "riotgames.com",
    "epic games": "epicgames.com",
    "unity": "unity.com",
    "unity technologies": "unity.com",
    "duolingo": "duolingo.com",
    "yelp": "yelp.com",
    "nextdoor": "nextdoor.com",
    "quora": "quora.com",
    "substack": "substack.com",
    "medium": "medium.com",
    "automattic": "automattic.com",
    "wordpress": "wordpress.com",
    "wix": "wix.com",
    "squarespace": "squarespace.com",
    "godaddy": "godaddy.com",
    "bumble": "bumble.com",
    "hinge": "hinge.co",
    "tinder": "tinder.com",
    "match group": "matchgroup.com",
    "match": "matchgroup.com",
    "grindr": "grindr.com",

    # -------- Consumer Commerce / Travel --------
    "instacart": "instacart.com",
    "grubhub": "grubhub.com",
    "postmates": "postmates.com",
    "gopuff": "gopuff.com",
    "opentable": "opentable.com",
    "resy": "resy.com",
    "booking": "booking.com",
    "booking holdings": "bookingholdings.com",
    "priceline": "priceline.com",
    "kayak": "kayak.com",
    "expedia": "expedia.com",
    "expedia group": "expediagroup.com",
    "vrbo": "vrbo.com",
    "hopper": "hopper.com",
    "trivago": "trivago.com",
    "tripadvisor": "tripadvisor.com",
    "hotels.com": "hotels.com",
    "skyscanner": "skyscanner.net",
    "etsy": "etsy.com",
    "wayfair": "wayfair.com",
    "stockx": "stockx.com",
    "goat": "goat.com",
    "poshmark": "poshmark.com",
    "depop": "depop.com",
    "thredup": "thredup.com",
    "rent the runway": "renttherunway.com",
    "peloton": "onepeloton.com",
    "classpass": "classpass.com",
    "chewy": "chewy.com",
    "1stdibs": "1stdibs.com",
    "faire": "faire.com",
    "kickstarter": "kickstarter.com",
    "indiegogo": "indiegogo.com",
    "gofundme": "gofundme.com",
    "patreon": "patreon.com",
    "onlyfans": "onlyfans.com",
    "bird": "bird.co",
    "lime": "li.me",
    "turo": "turo.com",

    # -------- Fintech --------
    "square": "squareup.com",
    "block": "block.xyz",
    "plaid": "plaid.com",
    "affirm": "affirm.com",
    "klarna": "klarna.com",
    "chime": "chime.com",
    "sofi": "sofi.com",
    "marqeta": "marqeta.com",
    "toast": "toasttab.com",
    "bill.com": "bill.com",
    "bill": "bill.com",
    "ramp": "ramp.com",
    "brex": "brex.com",
    "mercury": "mercury.com",
    "wise": "wise.com",
    "transferwise": "wise.com",
    "remitly": "remitly.com",
    "payoneer": "payoneer.com",
    "adyen": "adyen.com",
    "checkout.com": "checkout.com",
    "rapyd": "rapyd.net",
    "nubank": "nubank.com.br",
    "revolut": "revolut.com",
    "monzo": "monzo.com",
    "n26": "n26.com",
    "kraken": "kraken.com",
    "gemini": "gemini.com",
    "binance": "binance.com",
    "circle": "circle.com",
    "ripple": "ripple.com",
    "fireblocks": "fireblocks.com",
    "anchorage": "anchorage.com",
    "bitgo": "bitgo.com",
    "moonpay": "moonpay.com",
    "alchemy": "alchemy.com",
    "consensys": "consensys.io",
    "chainalysis": "chainalysis.com",
    "paypal": "paypal.com",
    "venmo": "venmo.com",
    "zelle": "zellepay.com",
    "cash app": "cash.app",
    "cashapp": "cash.app",
    "opendoor": "opendoor.com",
    "rocket mortgage": "rocketmortgage.com",
    "rocket companies": "rocketcompanies.com",

    # -------- SaaS / Enterprise Software --------
    "servicenow": "servicenow.com",
    "workday": "workday.com",
    "atlassian": "atlassian.com",
    "slack": "slack.com",
    "zoom": "zoom.us",
    "datadog": "datadoghq.com",
    "mongodb": "mongodb.com",
    "elastic": "elastic.co",
    "confluent": "confluent.io",
    "cloudflare": "cloudflare.com",
    "fastly": "fastly.com",
    "vercel": "vercel.com",
    "netlify": "netlify.com",
    "hashicorp": "hashicorp.com",
    "gitlab": "gitlab.com",
    "github": "github.com",
    "miro": "miro.com",
    "asana": "asana.com",
    "monday.com": "monday.com",
    "monday": "monday.com",
    "airtable": "airtable.com",
    "retool": "retool.com",
    "zapier": "zapier.com",
    "twilio": "twilio.com",
    "sendgrid": "sendgrid.com",
    "segment": "segment.com",
    "amplitude": "amplitude.com",
    "mixpanel": "mixpanel.com",
    "braze": "braze.com",
    "iterable": "iterable.com",
    "klaviyo": "klaviyo.com",
    "attentive": "attentivemobile.com",
    "hubspot": "hubspot.com",
    "zendesk": "zendesk.com",
    "freshworks": "freshworks.com",
    "intercom": "intercom.com",
    "drift": "drift.com",
    "gainsight": "gainsight.com",
    "front": "front.com",
    "superhuman": "superhuman.com",
    "loom": "loom.com",
    "calendly": "calendly.com",
    "docusign": "docusign.com",
    "autodesk": "autodesk.com",
    "grammarly": "grammarly.com",
    "descript": "descript.com",
    "linear": "linear.app",
    "coda": "coda.io",
    "pitch": "pitch.com",

    # -------- AI / ML --------
    "xai": "x.ai",
    "character ai": "character.ai",
    "character.ai": "character.ai",
    "cohere": "cohere.com",
    "mistral": "mistral.ai",
    "mistral ai": "mistral.ai",
    "perplexity": "perplexity.ai",
    "adept": "adept.ai",
    "inflection ai": "inflection.ai",
    "inflection": "inflection.ai",
    "hugging face": "huggingface.co",
    "huggingface": "huggingface.co",
    "scale ai": "scale.com",
    "scale": "scale.com",
    "weights & biases": "wandb.com",
    "wandb": "wandb.com",
    "modal": "modal.com",
    "replicate": "replicate.com",
    "together ai": "together.ai",
    "groq": "groq.com",
    "cerebras": "cerebras.net",
    "sambanova": "sambanova.ai",
    "mosaicml": "mosaicml.com",
    "midjourney": "midjourney.com",
    "stability ai": "stability.ai",
    "stability": "stability.ai",
    "runway": "runwayml.com",
    "runwayml": "runwayml.com",
    "pika": "pika.art",
    "luma ai": "lumalabs.ai",
    "luma": "lumalabs.ai",
    "elevenlabs": "elevenlabs.io",
    "suno": "suno.com",
    "udio": "udio.com",
    "ideogram": "ideogram.ai",
    "langchain": "langchain.com",
    "llamaindex": "llamaindex.ai",
    "pinecone": "pinecone.io",
    "weaviate": "weaviate.io",

    # -------- Cybersecurity --------
    "crowdstrike": "crowdstrike.com",
    "palo alto networks": "paloaltonetworks.com",
    "palo alto": "paloaltonetworks.com",
    "fortinet": "fortinet.com",
    "zscaler": "zscaler.com",
    "okta": "okta.com",
    "1password": "1password.com",
    "snyk": "snyk.io",
    "wiz": "wiz.io",
    "sentinelone": "sentinelone.com",
    "rapid7": "rapid7.com",
    "tenable": "tenable.com",
    "proofpoint": "proofpoint.com",
    "splunk": "splunk.com",
    "netskope": "netskope.com",
    "cybereason": "cybereason.com",
    "arctic wolf": "arcticwolf.com",
    "trellix": "trellix.com",
    "trend micro": "trendmicro.com",
    "check point": "checkpoint.com",
    "sophos": "sophos.com",
    "bitdefender": "bitdefender.com",
    "malwarebytes": "malwarebytes.com",
    "hackerone": "hackerone.com",
    "bugcrowd": "bugcrowd.com",
    "cato networks": "catonetworks.com",
    "illumio": "illumio.com",
    "orca security": "orca.security",
    "aqua security": "aquasec.com",
    "sysdig": "sysdig.com",
    "darktrace": "darktrace.com",
    "recorded future": "recordedfuture.com",
    "mandiant": "mandiant.com",

    # -------- Dev infra / Data --------
    "redis": "redis.io",
    "fivetran": "fivetran.com",
    "dbt labs": "getdbt.com",
    "dbt": "getdbt.com",
    "airbyte": "airbyte.com",
    "prefect": "prefect.io",
    "astronomer": "astronomer.io",
    "grafana labs": "grafana.com",
    "grafana": "grafana.com",
    "new relic": "newrelic.com",
    "chronosphere": "chronosphere.io",
    "launchdarkly": "launchdarkly.com",
    "pagerduty": "pagerduty.com",
    "sentry": "sentry.io",
    "digitalocean": "digitalocean.com",
    "linode": "linode.com",
    "akamai": "akamai.com",
    "heroku": "heroku.com",
    "render": "render.com",
    "fly.io": "fly.io",
    "railway": "railway.app",
    "supabase": "supabase.com",
    "firebase": "firebase.google.com",
    "planetscale": "planetscale.com",
    "neon": "neon.tech",
    "cockroachdb": "cockroachlabs.com",
    "cockroach labs": "cockroachlabs.com",

    # -------- Semiconductors / Hardware --------
    "intel": "intel.com",
    "amd": "amd.com",
    "qualcomm": "qualcomm.com",
    "broadcom": "broadcom.com",
    "marvell": "marvell.com",
    "mediatek": "mediatek.com",
    "micron": "micron.com",
    "western digital": "westerndigital.com",
    "seagate": "seagate.com",
    "tsmc": "tsmc.com",
    "asml": "asml.com",
    "applied materials": "appliedmaterials.com",
    "lam research": "lamresearch.com",
    "kla": "kla.com",
    "analog devices": "analog.com",
    "microchip": "microchip.com",
    "on semi": "onsemi.com",
    "onsemi": "onsemi.com",
    "globalfoundries": "gf.com",
    "arm": "arm.com",
    "arm holdings": "arm.com",
    "cisco": "cisco.com",
    "hpe": "hpe.com",
    "hewlett packard enterprise": "hpe.com",
    "hp": "hp.com",
    "dell": "dell.com",
    "lenovo": "lenovo.com",
    "samsung": "samsung.com",
    "sony": "sony.com",
    "lg": "lg.com",
    "logitech": "logitech.com",
    "sonos": "sonos.com",
    "bose": "bose.com",

    # -------- Autonomous / Robotics / Space --------
    "spacex": "spacex.com",
    "cruise": "getcruise.com",
    "anduril": "anduril.com",
    "shield ai": "shield.ai",
    "skydio": "skydio.com",
    "zipline": "flyzipline.com",
    "nuro": "nuro.ai",
    "aurora": "aurora.tech",
    "aurora innovation": "aurora.tech",
    "motional": "motional.com",
    "applied intuition": "appliedintuition.com",
    "blue origin": "blueorigin.com",
    "rocket lab": "rocketlabusa.com",
    "firefly aerospace": "fireflyspace.com",
    "relativity space": "relativityspace.com",
    "axiom space": "axiomspace.com",
    "sierra space": "sierraspace.com",
    "planet labs": "planet.com",
    "maxar": "maxar.com",
    "starlink": "starlink.com",

    # -------- Media / Entertainment --------
    "disney": "disney.com",
    "walt disney": "disney.com",
    "the walt disney company": "disney.com",
    "marvel": "marvel.com",
    "marvel studios": "marvel.com",
    "pixar": "pixar.com",
    "lucasfilm": "lucasfilm.com",
    "warner bros discovery": "wbd.com",
    "warner bros. discovery": "wbd.com",
    "wbd": "wbd.com",
    "warner bros": "warnerbros.com",
    "warner brothers": "warnerbros.com",
    "paramount": "paramount.com",
    "paramount global": "paramount.com",
    "comcast": "comcast.com",
    "nbcuniversal": "nbcuni.com",
    "nbc": "nbc.com",
    "fox": "foxcorporation.com",
    "fox corporation": "foxcorporation.com",
    "sony pictures": "sonypictures.com",
    "universal music": "universalmusic.com",
    "umg": "universalmusic.com",
    "warner music": "wmg.com",
    "warner music group": "wmg.com",
    "sony music": "sonymusic.com",
    "live nation": "livenationentertainment.com",
    "ticketmaster": "ticketmaster.com",
    "amc theatres": "amctheatres.com",
    "regal cinemas": "regmovies.com",
    "cinemark": "cinemark.com",
    "imax": "imax.com",
    "hulu": "hulu.com",
    "max": "max.com",
    "hbo": "hbo.com",
    "roku": "roku.com",
    "sirius xm": "siriusxm.com",
    "siriusxm": "siriusxm.com",
    "iheartmedia": "iheartmedia.com",
    "audible": "audible.com",
    "wondery": "wondery.com",
    "soundcloud": "soundcloud.com",
    "tidal": "tidal.com",
    "deezer": "deezer.com",

    # -------- News / Publishing --------
    "new york times": "nytimes.com",
    "nyt": "nytimes.com",
    "the new york times": "nytimes.com",
    "wall street journal": "wsj.com",
    "wsj": "wsj.com",
    "news corp": "newscorp.com",
    "washington post": "washpost.com",
    "bloomberg": "bloomberg.net",
    "reuters": "thomsonreuters.com",
    "thomson reuters": "thomsonreuters.com",
    "dow jones": "dowjones.com",
    "financial times": "ft.com",
    "ft": "ft.com",
    "the economist": "economist.com",
    "time": "time.com",
    "time magazine": "time.com",
    "conde nast": "condenast.com",
    "condé nast": "condenast.com",
    "vogue": "condenast.com",
    "gq": "condenast.com",
    "the new yorker": "condenast.com",
    "vanity fair": "condenast.com",
    "wired": "wired.com",
    "hearst": "hearst.com",
    "dotdash meredith": "dotdashmeredith.com",
    "vox media": "voxmedia.com",
    "buzzfeed": "buzzfeed.com",
    "vice": "vice.com",
    "business insider": "insider.com",
    "semafor": "semafor.com",
    "puck": "puck.news",
    "the athletic": "theathletic.com",
    "politico": "politico.com",
    "axios": "axios.com",
    "the information": "theinformation.com",
    "forbes": "forbes.com",
    "fortune": "fortune.com",
    "the atlantic": "theatlantic.com",
    "rolling stone": "rollingstone.com",
    "billboard": "billboard.com",
    "variety": "variety.com",
    "hollywood reporter": "hollywoodreporter.com",

    # -------- Sports Leagues --------
    "mlb": "mlb.com",
    "nba": "nba.com",
    "nfl": "nfl.com",
    "nhl": "nhl.com",
    "mls": "mlssoccer.com",
    "pga tour": "pgatour.com",
    "pga": "pga.com",
    "wnba": "wnba.com",
    "wwe": "wwe.com",
    "ufc": "ufc.com",
    "formula 1": "formula1.com",
    "f1": "formula1.com",
    "nascar": "nascar.com",
    "fanatics": "fanatics.com",
    "espn": "espn.com",

    # -------- Consumer / CPG --------
    "procter & gamble": "pg.com",
    "procter and gamble": "pg.com",
    "p&g": "pg.com",
    "unilever": "unilever.com",
    "nestle": "nestle.com",
    "nestlé": "nestle.com",
    "coca-cola": "coca-cola.com",
    "coca cola": "coca-cola.com",
    "pepsico": "pepsico.com",
    "kraft heinz": "kraftheinzcompany.com",
    "general mills": "generalmills.com",
    "kellogg's": "kellogg.com",
    "kelloggs": "kellogg.com",
    "kellogg": "kellogg.com",
    "mondelez": "mondelezinternational.com",
    "mondelēz": "mondelezinternational.com",
    "colgate-palmolive": "colgate.com",
    "colgate": "colgate.com",
    "kimberly-clark": "kimberly-clark.com",
    "kimberly clark": "kimberly-clark.com",
    "clorox": "thecloroxcompany.com",
    "church & dwight": "churchdwight.com",
    "estee lauder": "elcompanies.com",
    "estée lauder": "elcompanies.com",
    "l'oreal": "loreal.com",
    "loreal": "loreal.com",
    "l'oréal": "loreal.com",
    "coty": "coty.com",
    "sephora": "sephora.com",
    "ulta": "ulta.com",
    "ulta beauty": "ulta.com",
    "e.l.f.": "elfbeauty.com",
    "elf beauty": "elfbeauty.com",
    "glossier": "glossier.com",
    "rare beauty": "rarebeauty.com",
    "fenty beauty": "fentybeauty.com",
    "anastasia beverly hills": "anastasiabeverlyhills.com",

    # -------- Luxury / Fashion --------
    "lvmh": "lvmh.com",
    "louis vuitton": "louisvuitton.com",
    "dior": "dior.com",
    "christian dior": "dior.com",
    "fendi": "fendi.com",
    "celine": "celine.com",
    "givenchy": "givenchy.com",
    "kenzo": "kenzo.com",
    "marc jacobs": "marcjacobs.com",
    "tiffany": "tiffany.com",
    "tiffany & co": "tiffany.com",
    "bulgari": "bulgari.com",
    "tag heuer": "tagheuer.com",
    "kering": "kering.com",
    "gucci": "gucci.com",
    "yves saint laurent": "ysl.com",
    "saint laurent": "ysl.com",
    "ysl": "ysl.com",
    "bottega veneta": "bottegaveneta.com",
    "balenciaga": "balenciaga.com",
    "alexander mcqueen": "alexandermcqueen.com",
    "richemont": "richemont.com",
    "cartier": "cartier.com",
    "van cleef & arpels": "vancleefarpels.com",
    "iwc": "iwc.com",
    "vacheron constantin": "vacheron-constantin.com",
    "panerai": "panerai.com",
    "hermes": "hermes.com",
    "hermès": "hermes.com",
    "chanel": "chanel.com",
    "prada": "prada.com",
    "burberry": "burberry.com",
    "rolex": "rolex.com",
    "patek philippe": "patek.com",
    "audemars piguet": "audemarspiguet.com",
    "omega": "omegawatches.com",
    "swatch group": "swatchgroup.com",
    "chopard": "chopard.com",
    "ralph lauren": "ralphlauren.com",
    "tommy hilfiger": "tommy.com",
    "calvin klein": "calvinklein.com",
    "pvh": "pvh.com",
    "levi's": "levi.com",
    "levis": "levi.com",
    "levi strauss": "levi.com",
    "gap": "gap.com",
    "gap inc": "gapinc.com",
    "old navy": "oldnavy.com",
    "banana republic": "bananarepublic.com",
    "j.crew": "jcrew.com",
    "j crew": "jcrew.com",
    "madewell": "madewell.com",
    "everlane": "everlane.com",
    "reformation": "thereformation.com",
    "aritzia": "aritzia.com",
    "alo yoga": "aloyoga.com",
    "shein": "shein.com",
    "temu": "temu.com",
    "zara": "zara.com",
    "inditex": "inditex.com",
    "h&m": "hm.com",
    "hm": "hm.com",
    "uniqlo": "uniqlo.com",
    "fast retailing": "fastretailing.com",

    # -------- Athletic / Athleisure --------
    "nike": "nike.com",
    "adidas": "adidas.com",
    "puma": "puma.com",
    "under armour": "underarmour.com",
    "lululemon": "lululemon.com",
    "athleta": "athleta.com",
    "vans": "vans.com",
    "the north face": "thenorthface.com",
    "north face": "thenorthface.com",
    "vf corporation": "vfc.com",
    "vf corp": "vfc.com",
    "new balance": "newbalance.com",
    "asics": "asics.com",
    "brooks running": "brooksrunning.com",
    "hoka": "hoka.com",
    "ugg": "ugg.com",
    "deckers": "deckers.com",
    "on running": "on.com",
    "allbirds": "allbirds.com",
    "skechers": "skechers.com",

    # -------- Food / Beverage --------
    "mcdonald's": "mcdonalds.com",
    "mcdonalds": "mcdonalds.com",
    "burger king": "bk.com",
    "wendy's": "wendys.com",
    "wendys": "wendys.com",
    "subway": "subway.com",
    "chick-fil-a": "chick-fil-a.com",
    "chick fil a": "chick-fil-a.com",
    "kfc": "kfc.com",
    "taco bell": "tacobell.com",
    "pizza hut": "pizzahut.com",
    "domino's": "dominos.com",
    "dominos": "dominos.com",
    "papa john's": "papajohns.com",
    "papa johns": "papajohns.com",
    "little caesars": "littlecaesars.com",
    "yum brands": "yum.com",
    "yum! brands": "yum.com",
    "restaurant brands international": "rbi.com",
    "rbi": "rbi.com",
    "chipotle": "chipotle.com",
    "panera": "panerabread.com",
    "panera bread": "panerabread.com",
    "sweetgreen": "sweetgreen.com",
    "cava": "cava.com",
    "shake shack": "shakeshack.com",
    "five guys": "fiveguys.com",
    "wingstop": "wingstop.com",
    "buffalo wild wings": "buffalowildwings.com",
    "darden": "darden.com",
    "darden restaurants": "darden.com",
    "olive garden": "olivegarden.com",
    "longhorn steakhouse": "longhornsteakhouse.com",
    "cheesecake factory": "thecheesecakefactory.com",
    "texas roadhouse": "texasroadhouse.com",
    "raising cane's": "raisingcanes.com",
    "raising canes": "raisingcanes.com",
    "in-n-out": "in-n-out.com",
    "in n out": "in-n-out.com",
    "whataburger": "whataburger.com",
    "starbucks": "starbucks.com",
    "dunkin'": "dunkinbrands.com",
    "dunkin": "dunkinbrands.com",
    "inspire brands": "inspirebrands.com",
    "krispy kreme": "krispykreme.com",
    "keurig dr pepper": "keurigdrpepper.com",
    "monster beverage": "monsterbevcorp.com",
    "monster energy": "monsterenergy.com",
    "celsius": "celsius.com",
    "red bull": "redbull.com",
    "liquid death": "liquiddeath.com",
    "white claw": "whiteclaw.com",
    "boston beer": "bostonbeer.com",
    "sam adams": "samueladams.com",
    "diageo": "diageo.com",
    "constellation brands": "cbrands.com",
    "anheuser-busch": "anheuser-busch.com",
    "anheuser busch": "anheuser-busch.com",
    "ab inbev": "ab-inbev.com",
    "molson coors": "molsoncoors.com",
    "heineken": "heineken.com",
    "carlsberg": "carlsberg.com",

    # -------- Retail --------
    "walmart": "walmart.com",
    "target": "target.com",
    "costco": "costco.com",
    "sam's club": "samsclub.com",
    "bj's wholesale": "bjs.com",
    "home depot": "homedepot.com",
    "lowe's": "lowes.com",
    "lowes": "lowes.com",
    "best buy": "bestbuy.com",
    "kroger": "kroger.com",
    "albertsons": "albertsons.com",
    "publix": "publix.com",
    "whole foods": "wholefoodsmarket.com",
    "trader joe's": "traderjoes.com",
    "trader joes": "traderjoes.com",
    "aldi": "aldi.us",
    "wegmans": "wegmans.com",
    "heb": "heb.com",
    "h-e-b": "heb.com",
    "sprouts": "sprouts.com",
    "erewhon": "erewhonmarket.com",
    "ahold delhaize": "aholddelhaize.com",
    "tjx": "tjx.com",
    "tjx companies": "tjx.com",
    "tj maxx": "tjmaxx.com",
    "marshalls": "marshalls.com",
    "homegoods": "homegoods.com",
    "ross stores": "rossstores.com",
    "burlington": "burlingtonstores.com",
    "nordstrom": "nordstrom.com",
    "macy's": "macys.com",
    "macys": "macys.com",
    "kohl's": "kohls.com",
    "kohls": "kohls.com",
    "saks fifth avenue": "saksfifthavenue.com",
    "saks": "saksfifthavenue.com",
    "neiman marcus": "neimanmarcus.com",
    "dillard's": "dillards.com",
    "dollar tree": "dollartree.com",
    "dollar general": "dollargeneral.com",
    "five below": "fivebelow.com",
    "ikea": "ikea.com",
    "williams-sonoma": "williams-sonoma.com",
    "williams sonoma": "williams-sonoma.com",
    "restoration hardware": "rh.com",
    "rh": "rh.com",
    "crate & barrel": "crateandbarrel.com",
    "pottery barn": "potterybarn.com",
    "west elm": "westelm.com",
    "cvs": "cvs.com",
    "cvs health": "cvshealth.com",
    "walgreens": "walgreens.com",
    "rite aid": "riteaid.com",
    "petco": "petco.com",
    "petsmart": "petsmart.com",
    "gamestop": "gamestop.com",
    "barnes & noble": "barnesandnoble.com",
    "michaels": "michaels.com",
    "hobby lobby": "hobbylobby.com",
    "joann": "joann.com",
    "tractor supply": "tractorsupply.com",
    "autozone": "autozone.com",
    "o'reilly auto parts": "oreillyauto.com",
    "advance auto parts": "advanceautoparts.com",

    # -------- Pharma --------
    "johnson & johnson": "jnj.com",
    "johnson and johnson": "jnj.com",
    "j&j": "jnj.com",
    "jnj": "jnj.com",
    "pfizer": "pfizer.com",
    "merck": "merck.com",
    "abbvie": "abbvie.com",
    "bristol-myers squibb": "bms.com",
    "bms": "bms.com",
    "bristol myers squibb": "bms.com",
    "eli lilly": "lilly.com",
    "lilly": "lilly.com",
    "amgen": "amgen.com",
    "gilead": "gilead.com",
    "biogen": "biogen.com",
    "regeneron": "regeneron.com",
    "vertex": "vrtx.com",
    "vertex pharmaceuticals": "vrtx.com",
    "moderna": "modernatx.com",
    "biontech": "biontech.de",
    "novartis": "novartis.com",
    "roche": "roche.com",
    "genentech": "gene.com",
    "sanofi": "sanofi.com",
    "gsk": "gsk.com",
    "glaxosmithkline": "gsk.com",
    "astrazeneca": "astrazeneca.com",
    "bayer": "bayer.com",
    "boehringer ingelheim": "boehringer-ingelheim.com",
    "novo nordisk": "novonordisk.com",
    "takeda": "takeda.com",
    "teva": "tevapharm.com",
    "zoetis": "zoetis.com",

    # -------- Payers / Providers --------
    "unitedhealth": "unitedhealthgroup.com",
    "unitedhealth group": "unitedhealthgroup.com",
    "united healthcare": "uhc.com",
    "optum": "optum.com",
    "elevance": "elevancehealth.com",
    "elevance health": "elevancehealth.com",
    "anthem": "anthem.com",
    "aetna": "aetna.com",
    "cigna": "cigna.com",
    "humana": "humana.com",
    "centene": "centene.com",
    "molina": "molinahealthcare.com",
    "kaiser permanente": "kp.org",
    "kaiser": "kp.org",
    "hca healthcare": "hcahealthcare.com",
    "hca": "hcahealthcare.com",
    "tenet healthcare": "tenethealth.com",
    "commonspirit": "commonspirit.org",
    "ascension": "ascension.org",
    "providence": "providence.org",
    "northwell health": "northwell.edu",
    "mass general brigham": "massgeneralbrigham.org",
    "cleveland clinic": "clevelandclinic.org",
    "mayo clinic": "mayoclinic.org",
    "johns hopkins medicine": "hopkinsmedicine.org",
    "stanford health care": "stanfordhealthcare.org",
    "ucla health": "uclahealth.org",
    "ucsf health": "ucsfhealth.org",
    "keck medicine": "keckmedicine.org",
    "nyu langone": "nyulangone.org",
    "cedars-sinai": "cedars-sinai.org",
    "cedars sinai": "cedars-sinai.org",
    "memorial sloan kettering": "mskcc.org",
    "msk": "mskcc.org",
    "md anderson": "mdanderson.org",
    "dana-farber": "dana-farber.org",

    # -------- Medtech --------
    "medtronic": "medtronic.com",
    "abbott": "abbott.com",
    "abbott laboratories": "abbott.com",
    "boston scientific": "bostonscientific.com",
    "stryker": "stryker.com",
    "zimmer biomet": "zimmerbiomet.com",
    "edwards lifesciences": "edwards.com",
    "intuitive surgical": "intuitive.com",
    "intuitive": "intuitive.com",
    "dexcom": "dexcom.com",
    "insulet": "insulet.com",
    "resmed": "resmed.com",
    "baxter": "baxter.com",
    "becton dickinson": "bd.com",
    "bd": "bd.com",
    "danaher": "danaher.com",
    "thermo fisher": "thermofisher.com",
    "thermo fisher scientific": "thermofisher.com",
    "agilent": "agilent.com",
    "waters": "waters.com",
    "perkinelmer": "perkinelmer.com",
    "revvity": "revvity.com",
    "illumina": "illumina.com",
    "10x genomics": "10xgenomics.com",
    "guardant health": "guardanthealth.com",
    "grail": "grail.com",
    "natera": "natera.com",
    "exact sciences": "exactsciences.com",
    "ginkgo bioworks": "ginkgobioworks.com",
    "tempus": "tempus.com",

    # -------- Digital health --------
    "teladoc": "teladochealth.com",
    "doximity": "doximity.com",
    "hims & hers": "forhims.com",
    "hims and hers": "forhims.com",
    "ro": "ro.co",
    "hinge health": "hingehealth.com",
    "lyra health": "lyrahealth.com",
    "spring health": "springhealth.com",
    "one medical": "onemedical.com",
    "oscar health": "hioscar.com",
    "included health": "includedhealth.com",
    "modern health": "modernhealth.com",
    "cerebral": "cerebral.com",
    "talkspace": "talkspace.com",
    "betterhelp": "betterhelp.com",
    "iqvia": "iqvia.com",
    "icon plc": "iconplc.com",
    "labcorp": "labcorp.com",
    "quest diagnostics": "questdiagnostics.com",

    # -------- Autos --------
    "nissan": "nissan-global.com",
    "kia": "kia.com",
    "volkswagen": "volkswagenag.com",
    "vw": "volkswagenag.com",
    "audi": "audi.com",
    "porsche": "porsche.com",
    "bentley": "bentleymotors.com",
    "bugatti": "bugatti.com",
    "lamborghini": "lamborghini.com",
    "seat": "seat.com",
    "skoda": "skoda-auto.com",
    "volvo cars": "volvocars.com",
    "volvo group": "volvogroup.com",
    "jaguar": "jaguar.com",
    "land rover": "landrover.com",
    "jaguar land rover": "jaguarlandrover.com",
    "jlr": "jaguarlandrover.com",
    "aston martin": "astonmartin.com",
    "lotus": "lotuscars.com",
    "mclaren": "mclaren.com",
    "mclaren automotive": "cars.mclaren.com",
    "ferrari": "ferrari.com",
    "maserati": "maserati.com",
    "stellantis": "stellantis.com",
    "chrysler": "chrysler.com",
    "jeep": "jeep.com",
    "dodge": "dodge.com",
    "ram": "ramtrucks.com",
    "fiat": "fiatusa.com",
    "peugeot": "peugeot.com",
    "renault": "renaultgroup.com",
    "subaru": "subaru.com",
    "mazda": "mazda.com",
    "suzuki": "suzuki.com",
    "lucid": "lucidmotors.com",
    "lucid motors": "lucidmotors.com",
    "fisker": "fiskerinc.com",
    "polestar": "polestar.com",
    "nio": "nio.com",
    "xpeng": "xpeng.com",
    "li auto": "lixiang.com",
    "byd": "byd.com",
    "geely": "geelyauto.com",
    "chery": "cheryinternational.com",
    "tata motors": "tatamotors.com",
    "mahindra": "mahindra.com",
    "bosch": "bosch.com",
    "continental": "continental.com",
    "denso": "denso.com",
    "aptiv": "aptiv.com",
    "magna": "magna.com",
    "magna international": "magna.com",
    "zf": "zf.com",
    "michelin": "michelin.com",
    "bridgestone": "bridgestone.com",
    "goodyear": "goodyear.com",
    "pirelli": "pirelli.com",

    # -------- Aerospace / Defense --------
    "airbus": "airbus.com",
    "embraer": "embraer.com",
    "bombardier": "bombardier.com",
    "textron": "textron.com",
    "bell textron": "bellflight.com",
    "gulfstream": "gulfstream.com",
    "dassault": "dassault-aviation.com",
    "dassault systemes": "3ds.com",
    "rolls-royce": "rolls-royce.com",
    "rolls royce": "rolls-royce.com",
    "pratt & whitney": "prattwhitney.com",
    "ge aerospace": "geaerospace.com",
    "safran": "safran-group.com",
    "honeywell": "honeywell.com",
    "collins aerospace": "collinsaerospace.com",
    "leonardo": "leonardo.com",
    "bae systems": "baesystems.com",
    "thales": "thalesgroup.com",
    "saab": "saab.com",
    "elbit systems": "elbitsystems.com",
    "aerovironment": "avinc.com",
    "kratos": "kratosdefense.com",
    "booz allen hamilton": "boozallen.com",
    "booz allen": "boozallen.com",
    "leidos": "leidos.com",
    "saic": "saic.com",
    "peraton": "peraton.com",
    "mantech": "mantech.com",
    "caci": "caci.com",
    "kbr": "kbr.com",
    "parsons": "parsons.com",
    "aecom": "aecom.com",
    "mitre": "mitre.org",
    "rand corporation": "rand.org",
    "the aerospace corporation": "aero.org",
    "nasa": "nasa.gov",
    "jpl": "jpl.nasa.gov",
    "darpa": "darpa.mil",

    # -------- Energy --------
    "exxonmobil": "exxonmobil.com",
    "exxon mobil": "exxonmobil.com",
    "exxon": "exxonmobil.com",
    "chevron": "chevron.com",
    "shell": "shell.com",
    "bp": "bp.com",
    "totalenergies": "totalenergies.com",
    "conocophillips": "conocophillips.com",
    "occidental petroleum": "oxy.com",
    "oxy": "oxy.com",
    "marathon petroleum": "marathonpetroleum.com",
    "marathon oil": "marathonoil.com",
    "valero": "valero.com",
    "valero energy": "valero.com",
    "phillips 66": "phillips66.com",
    "hess corporation": "hess.com",
    "hess": "hess.com",
    "eog resources": "eogresources.com",
    "eog": "eogresources.com",
    "devon energy": "devonenergy.com",
    "pioneer natural resources": "pxd.com",
    "diamondback energy": "diamondbackenergy.com",
    "kinder morgan": "kindermorgan.com",
    "enterprise products partners": "enterpriseproducts.com",
    "energy transfer": "energytransfer.com",
    "williams companies": "co.williams.com",
    "cheniere": "cheniere.com",
    "cheniere energy": "cheniere.com",
    "enbridge": "enbridge.com",
    "tc energy": "tcenergy.com",
    "halliburton": "halliburton.com",
    "schlumberger": "slb.com",
    "slb": "slb.com",
    "baker hughes": "bakerhughes.com",
    "nov": "nov.com",
    "saudi aramco": "aramco.com",
    "aramco": "aramco.com",
    "petrobras": "petrobras.com.br",
    "equinor": "equinor.com",
    "eni": "eni.com",
    "repsol": "repsol.com",
    "nextera energy": "nexteraenergy.com",
    "nextera": "nexteraenergy.com",
    "first solar": "firstsolar.com",
    "sunpower": "sunpower.com",
    "sunrun": "sunrun.com",
    "enphase": "enphase.com",
    "solaredge": "solaredge.com",
    "vestas": "vestas.com",
    "orsted": "orsted.com",
    "ørsted": "orsted.com",
    "iberdrola": "iberdrola.com",
    "ge vernova": "gevernova.com",
    "siemens gamesa": "siemensgamesa.com",
    "chargepoint": "chargepoint.com",
    "evgo": "evgo.com",
    "blink charging": "blinkcharging.com",
    "fluence energy": "fluenceenergy.com",
    "bloom energy": "bloomenergy.com",
    "plug power": "plugpower.com",
    "quantumscape": "quantumscape.com",
    "duke energy": "duke-energy.com",
    "southern company": "southerncompany.com",
    "dominion energy": "dominionenergy.com",
    "exelon": "exeloncorp.com",
    "nrg energy": "nrg.com",
    "aep": "aep.com",
    "american electric power": "aep.com",
    "xcel energy": "xcelenergy.com",
    "sempra": "sempra.com",
    "sempra energy": "sempra.com",
    "edison international": "edisoninvestor.com",
    "pg&e": "pge.com",
    "pge": "pge.com",
    "consolidated edison": "coned.com",
    "coned": "coned.com",
    "firstenergy": "firstenergycorp.com",
    "entergy": "entergy.com",
    "eversource": "eversource.com",
    "pseg": "pseg.com",

    # -------- Industrials / Materials --------
    "ge": "ge.com",
    "general electric": "ge.com",
    "ge healthcare": "gehealthcare.com",
    "3m": "3m.com",
    "emerson electric": "emerson.com",
    "emerson": "emerson.com",
    "rockwell automation": "rockwellautomation.com",
    "abb": "global.abb",
    "siemens": "siemens.com",
    "schneider electric": "se.com",
    "eaton": "eaton.com",
    "vertiv": "vertiv.com",
    "hubbell": "hubbell.com",
    "roper technologies": "ropertech.com",
    "fortive": "fortive.com",
    "ametek": "ametek.com",
    "dover corporation": "dovercorporation.com",
    "illinois tool works": "itw.com",
    "itw": "itw.com",
    "parker hannifin": "parker.com",
    "trane technologies": "tranetechnologies.com",
    "carrier": "corporate.carrier.com",
    "otis": "otis.com",
    "xylem": "xylem.com",
    "pentair": "pentair.com",
    "ingersoll rand": "irco.com",
    "cummins": "cummins.com",
    "john deere": "deere.com",
    "deere": "deere.com",
    "caterpillar": "caterpillar.com",
    "cat": "caterpillar.com",
    "paccar": "paccar.com",
    "agco": "agcocorp.com",
    "cnh industrial": "cnhindustrial.com",
    "terex": "terex.com",
    "oshkosh": "oshkoshcorp.com",
    "komatsu": "komatsu.com",
    "fanuc": "fanuc.com",
    "yaskawa": "yaskawa.com",
    "dow": "dow.com",
    "dupont": "dupont.com",
    "lyondellbasell": "lyondellbasell.com",
    "corteva": "corteva.com",
    "ppg industries": "ppg.com",
    "ppg": "ppg.com",
    "sherwin-williams": "sherwin-williams.com",
    "sherwin williams": "sherwin-williams.com",
    "rpm international": "rpminc.com",
    "axalta": "axalta.com",
    "ecolab": "ecolab.com",
    "iff": "iff.com",
    "givaudan": "givaudan.com",
    "symrise": "symrise.com",
    "air liquide": "airliquide.com",
    "linde": "linde.com",
    "air products": "airproducts.com",
    "celanese": "celanese.com",
    "eastman chemical": "eastman.com",
    "basf": "basf.com",
    "nucor": "nucor.com",
    "steel dynamics": "steeldynamics.com",
    "us steel": "ussteel.com",
    "united states steel": "ussteel.com",
    "cleveland-cliffs": "clevelandcliffs.com",
    "arcelormittal": "arcelormittal.com",
    "posco": "posco.com",
    "alcoa": "alcoa.com",
    "freeport-mcmoran": "fcx.com",
    "freeport mcmoran": "fcx.com",
    "newmont": "newmont.com",
    "barrick gold": "barrick.com",
    "rio tinto": "riotinto.com",
    "bhp": "bhp.com",
    "vale": "vale.com",
    "anglo american": "angloamerican.com",
    "glencore": "glencore.com",

    # -------- Real estate --------
    "cbre": "cbre.com",
    "jll": "jll.com",
    "jones lang lasalle": "jll.com",
    "cushman & wakefield": "cushmanwakefield.com",
    "cushman wakefield": "cushmanwakefield.com",
    "colliers": "colliers.com",
    "newmark": "nmrk.com",
    "marcus & millichap": "marcusmillichap.com",
    "eastdil secured": "eastdilsecured.com",
    "compass": "compass.com",
    "redfin": "redfin.com",
    "zillow": "zillow.com",
    "anywhere real estate": "anywhere.re",
    "coldwell banker": "coldwellbanker.com",
    "century 21": "century21.com",
    "sotheby's international realty": "sothebysrealty.com",
    "berkshire hathaway homeservices": "bhhs.com",
    "re/max": "remax.com",
    "remax": "remax.com",
    "keller williams": "kw.com",
    "exp realty": "exprealty.com",
    "simon property group": "simon.com",
    "simon": "simon.com",
    "prologis": "prologis.com",
    "american tower": "americantower.com",
    "crown castle": "crowncastle.com",
    "equinix": "equinix.com",
    "digital realty": "digitalrealty.com",
    "public storage": "publicstorage.com",
    "avalonbay": "avalonbay.com",
    "avalonbay communities": "avalonbay.com",
    "equity residential": "equityresidential.com",
    "camden": "camdenliving.com",
    "essex property trust": "essex.com",
    "udr": "udr.com",
    "boston properties": "bxp.com",
    "bxp": "bxp.com",
    "vornado": "vno.com",
    "sl green": "slgreen.com",
    "kilroy realty": "kilroyrealty.com",
    "kimco realty": "kimcorealty.com",
    "regency centers": "regencycenters.com",
    "federal realty": "federalrealty.com",
    "realty income": "realtyincome.com",
    "iron mountain": "ironmountain.com",
    "vici properties": "viciproperties.com",
    "weyerhaeuser": "weyerhaeuser.com",
    "invitation homes": "invitationhomes.com",
    "cadre": "cadre.com",
    "roofstock": "roofstock.com",
    "opendoor": "opendoor.com",
    "better.com": "better.com",
    "rocket mortgage": "rocketmortgage.com",
    "loandepot": "loandepot.com",
    "uwm": "uwm.com",
    "united wholesale mortgage": "uwm.com",
    "pennymac": "pennymac.com",
    "guaranteed rate": "rate.com",

    # -------- Telecom / Networking --------
    "verizon": "verizon.com",
    "at&t": "att.com",
    "att": "att.com",
    "t-mobile": "t-mobile.com",
    "tmobile": "t-mobile.com",
    "vodafone": "vodafone.com",
    "bt group": "bt.com",
    "deutsche telekom": "telekom.com",
    "orange": "orange.com",
    "telefonica": "telefonica.com",
    "charter communications": "charter.com",
    "spectrum": "spectrum.com",
    "cox communications": "cox.com",
    "altice": "alticeusa.com",
    "frontier communications": "frontier.com",
    "lumen": "lumen.com",
    "lumen technologies": "lumen.com",
    "dish network": "dish.com",
    "ericsson": "ericsson.com",
    "nokia": "nokia.com",
    "corning": "corning.com",
})


# -----------------------------------------------------------------------------
# Dynamic fallback for names not in COMPANY_DOMAIN_MAP
#
# When a student types a company name we haven't hand-mapped (e.g. some
# mid-cap tech firm), fall back to hunter.get_smart_company_domain (which
# already does map → OpenAI → simple-domain heuristic). Then VERIFY the
# candidate returns real PDL hits before caching — Hunter can hand back a
# valid-looking but wrong domain, and locking that in would poison the
# cache. Verified domains persist to Firestore so we don't burn a PDL
# probe credit on every worker cold-start.
#
# Guardrail: skip inputs that look like acronyms (all-uppercase or len<4).
# PDL Enrichment demonstrably resolves acronyms wrong (BCG → NZ IT firm),
# and simple-domain heuristics produce "aig.com"-style false confidence
# in a whole class of wrong domains. Acronyms MUST be hand-added.
# -----------------------------------------------------------------------------

_dynamic_domain_cache: dict[str, str | None] = {}
_dynamic_cache_lock = Lock()


def _validate_domain_returns_hits(domain: str) -> bool:
    """1-record PDL probe: does job_company_website=domain match any US person?

    Cheapest possible sanity check that the resolved domain corresponds to
    a real, indexed company on PDL. Costs ~1 credit per probe (once per
    unique unmapped company name, then cached forever in Firestore).
    """
    if not domain or not PEOPLE_DATA_LABS_API_KEY:
        return False
    try:
        payload = {
            "query": {
                "bool": {
                    "must": [
                        {"match_phrase": {"job_company_website": domain}},
                        {"term": {"location_country": "united states"}},
                    ]
                }
            },
            "size": 1,
        }
        r = requests.post(
            f"{PDL_BASE_URL}/person/search",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Api-Key": PEOPLE_DATA_LABS_API_KEY,
            },
            json=payload,
            timeout=8,
        )
        if r.status_code != 200:
            return False
        return bool((r.json() or {}).get("data"))
    except Exception:
        return False


def _firestore_safe_key(key: str) -> str:
    return key.replace("/", "_").replace(".", "_")[:1400]


def _dynamic_domain_for_company(name: str) -> str | None:
    raw = (name or "").strip()
    if not raw or len(raw) < 4:
        return None
    # Acronym guard: all-caps inputs (BCG, KKR, PJT) need hand mapping.
    # `.upper()` catches "BCG"; letters-only check avoids tripping on
    # normal names that happen to be short (Nike, Etsy — already mapped
    # in the static block, so the guard only bites unmapped acronyms).
    if raw.isupper():
        return None
    key = raw.lower()

    with _dynamic_cache_lock:
        if key in _dynamic_domain_cache:
            return _dynamic_domain_cache[key]

    db = None
    try:
        from app.extensions import get_db
        db = get_db()
    except Exception:
        db = None

    doc_id = _firestore_safe_key(key)
    if db is not None:
        try:
            doc = db.collection("pdlCompanyDomains").document(doc_id).get()
            if doc.exists:
                cached = (doc.to_dict() or {}).get("domain")
                with _dynamic_cache_lock:
                    _dynamic_domain_cache[key] = cached
                return cached
        except Exception as e:
            print(f"[dynamic_domain] Firestore read failed for {key!r}: {e}")

    candidate = None
    try:
        from app.services.hunter import get_smart_company_domain
        candidate = get_smart_company_domain(raw)
    except Exception as e:
        print(f"[dynamic_domain] Hunter resolver failed for {key!r}: {e}")

    verified = candidate if (candidate and _validate_domain_returns_hits(candidate)) else None
    if verified:
        print(f"[dynamic_domain] {key!r} → {verified} (verified via PDL probe)")
    else:
        print(f"[dynamic_domain] {key!r} → no verified domain (candidate={candidate!r})")

    with _dynamic_cache_lock:
        _dynamic_domain_cache[key] = verified

    if db is not None:
        try:
            db.collection("pdlCompanyDomains").document(doc_id).set({
                "input_name": raw,
                "domain": verified,
                "candidate": candidate,
                "verified_at": datetime.utcnow(),
            })
        except Exception as e:
            print(f"[dynamic_domain] Firestore write failed for {key!r}: {e}")

    return verified


def _domain_for_company(name):
    """Map a user-supplied company name/acronym to its primary domain, if known.

    Static map first (fast, verified, zero cost). On miss, dynamic
    resolver piggybacks on Hunter's smart domain lookup and validates
    with a 1-record PDL probe before caching to Firestore.
    """
    if not name:
        return None
    key = str(name).strip().lower()
    if not key:
        return None
    static = COMPANY_DOMAIN_MAP.get(key)
    if static:
        return static
    return _dynamic_domain_for_company(name)


def clean_company_name(company):
    """Clean company name using PDL Cleaner API for better matching.
    Results are cached in-process to avoid redundant API calls.
    """
    if not company or not company.strip():
        return company
    key = company.strip().lower()
    with _clean_company_cache_lock:
        if key in _clean_company_cache:
            return _clean_company_cache[key]
    try:
        response = _session.get(
            f"{PDL_BASE_URL}/company/clean",
            params={
                'api_key': PEOPLE_DATA_LABS_API_KEY,
                'name': company
            },
            timeout=10
        )
        if response.status_code == 200:
            clean_data = response.json()
            if clean_data.get('status') == 200 and clean_data.get('name'):
                cleaned_name = clean_data['name']
                with _clean_company_cache_lock:
                    _clean_company_cache[key] = cleaned_name
                return cleaned_name
    except Exception as e:
        print(f"Company cleaning failed: {e}")
    with _clean_company_cache_lock:
        _clean_company_cache[key] = company
    return company


# US State abbreviations mapping
US_STATE_ABBREVIATIONS = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
    'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
    'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
    'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
    'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
    'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah',
    'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia',
    'WI': 'Wisconsin', 'WY': 'Wyoming', 'DC': 'District of Columbia'
}

def _expand_us_state_abbreviation(location: str) -> str:
    """
    Expand US state abbreviations in location strings to avoid confusion with country codes.
    Example: "Research Park, CA" -> "Research Park, California"
    Handles formats like:
    - "City, ST" -> "City, State"
    - "City, ST 12345" -> "City, State"
    - "City ST" -> "City, State"
    """
    if not location:
        return location
    
    # Most common pattern: "City, ST" or "City, ST ZIP"
    # Match ", ST" or ", ST ZIP" at the end
    comma_pattern = r',\s*([A-Z]{2})(?:\s+\d{5})?$'
    
    def replace_comma_state(match):
        state_abbr = match.group(1).upper()
        if state_abbr in US_STATE_ABBREVIATIONS:
            return f', {US_STATE_ABBREVIATIONS[state_abbr]}'
        return match.group(0)
    
    expanded = re.sub(comma_pattern, replace_comma_state, location, flags=re.IGNORECASE)
    
    # If no comma pattern matched, try "City ST" (without comma)
    if expanded == location:
        # Match "City ST" or "City ST ZIP" at the end
        no_comma_pattern = r'^(.+?)\s+([A-Z]{2})(?:\s+\d{5})?$'
        
        def replace_no_comma_state(match):
            city = match.group(1).strip()
            state_abbr = match.group(2).upper()
            if state_abbr in US_STATE_ABBREVIATIONS:
                return f'{city}, {US_STATE_ABBREVIATIONS[state_abbr]}'
            return match.group(0)
        
        expanded = re.sub(no_comma_pattern, replace_no_comma_state, location, flags=re.IGNORECASE)
    
    return expanded if expanded != location else location


def _fix_canada_misinterpretation(original_location: str, cleaned_location: str) -> str:
    """
    Fix cases where PDL incorrectly interprets US state "CA" (California) as Canada.
    If the original location contains a US state abbreviation and the cleaned result is "canada",
    try to correct it by expanding the state abbreviation.
    """
    if not original_location or not cleaned_location:
        return cleaned_location
    
    cleaned_lower = cleaned_location.lower()
    
    # If PDL returned "canada" but the original had a US state abbreviation, fix it
    if cleaned_lower in ['canada', 'canadian']:
        # Check if original location has a US state abbreviation
        # Look for US state abbreviations in the original location
        state_pattern = r',\s*([A-Z]{2})(?:\s+\d{5})?$|\s+([A-Z]{2})(?:\s+\d{5})?$'
        match = re.search(state_pattern, original_location, re.IGNORECASE)
        
        if match:
            state_abbr = (match.group(1) or match.group(2)).upper()
            if state_abbr in US_STATE_ABBREVIATIONS:
                # This was likely a US location, not Canada
                # Expand the state abbreviation and return a more appropriate location
                state_full = US_STATE_ABBREVIATIONS[state_abbr]
                # Try to extract city name if present
                city_match = re.match(r'^([^,]+)', original_location)
                if city_match:
                    city = city_match.group(1).strip()
                    return f"{city}, {state_full}"
                else:
                    return state_full
    
    return cleaned_location


def expand_location_state_only(location: str) -> str:
    """
    Expand US state abbreviations in location strings WITHOUT calling PDL API.
    This preserves the original city name and only expands state abbreviations.
    
    Use this for cases where you want to preserve city names and avoid PDL's
    autocorrection/fuzzy matching of city names.
    
    Example: "Research Park, CA" -> "Research Park, California"
    """
    if not location:
        return location
    
    expanded = _expand_us_state_abbreviation(location)
    if expanded != location:
        print(f"Expanded US state abbreviation: '{location}' -> '{expanded}'")
    
    return expanded


def clean_location_name(location, use_pdl_api=True):
    """Clean location name using PDL Cleaner API for better matching
    OPTIMIZED: Uses connection pooling for better performance
    
    FIXED: Properly handles US state abbreviations (e.g., "CA" = California, not Canada)
    
    Args:
        location: Location string to clean
        use_pdl_api: If False, only expands state abbreviations without calling PDL API.
                     This preserves city names and avoids autocorrection.
    """
    if not location:
        return location
    
    # Pre-process: Expand US state abbreviations to avoid confusion with country codes
    # Example: "Research Park, CA" -> "Research Park, California"
    expanded_location = _expand_us_state_abbreviation(location)
    if expanded_location != location:
        print(f"Expanded US state abbreviation: '{location}' -> '{expanded_location}'")
    
    # If PDL API is disabled, just return the expanded location
    if not use_pdl_api:
        return expanded_location
    
    try:
        print(f"Cleaning location: {location}")
        
        # Use session for connection pooling
        with _session_lock:
            response = _session.get(
                f"{PDL_BASE_URL}/location/clean",
                params={
                    'api_key': PEOPLE_DATA_LABS_API_KEY,
                    'location': expanded_location
                },
                timeout=10
            )
        
        if response.status_code == 200:
            clean_data = response.json()
            if clean_data.get('status') == 200 and clean_data.get('name'):
                cleaned_location = clean_data['name']
                
                # Post-process: Fix cases where PDL incorrectly returns "canada" for US locations
                fixed_location = _fix_canada_misinterpretation(location, cleaned_location)
                
                if fixed_location != cleaned_location:
                    print(f"Fixed Canada misinterpretation: '{cleaned_location}' -> '{fixed_location}'")
                
                print(f"Cleaned location: '{location}' -> '{fixed_location}'")
                return fixed_location
    
    except Exception as e:
        print(f"Location cleaning failed: {e}")
    
    return expanded_location


def enrich_job_title_with_pdl(job_title):
    """Use PDL Job Title Enrichment API to get standardized job titles
    OPTIMIZED: Uses connection pooling for better performance
    """
    try:
        print(f"Enriching job title: {job_title}")
        
        # Use session for connection pooling
        with _session_lock:
            response = _session.get(
                f"{PDL_BASE_URL}/job_title/enrich",
                params={
                    'api_key': PEOPLE_DATA_LABS_API_KEY,
                    'job_title': job_title
                },
                timeout=10
            )
        
        if response.status_code == 200:
            enrich_data = response.json()
            if enrich_data.get('status') == 200 and enrich_data.get('data'):
                enriched_data = enrich_data['data']
                
                # Extract useful enrichment data
                result = {
                    'cleaned_name': enriched_data.get('cleaned_name', job_title),
                    'similar_titles': enriched_data.get('similar_job_titles', []),
                    'levels': enriched_data.get('job_title_levels', []),
                    'categories': enriched_data.get('job_title_categories', [])
                }
                
                print(f"Job title enrichment successful: {result}")
                return result
    
    except Exception as e:
        print(f"Job title enrichment failed: {e}")
    
    return {
        'cleaned_name': job_title,
        'similar_titles': [],
        'levels': [],
        'categories': []
    }


def get_autocomplete_suggestions(query, data_type='job_title'):
    """Enhanced autocomplete with proper PDL field mapping"""
    try:
        print(f"Getting autocomplete suggestions for {data_type}: {query}")
        
        # Map your frontend field names to PDL's supported field names
        pdl_field_mapping = {
            'job_title': 'title',  # This is the key fix
            'company': 'company',
            'location': 'location',
            'school': 'school',
            'skill': 'skill',
            'industry': 'industry',
            'role': 'role',
            'sub_role': 'sub_role'
        }
        
        # Get the correct PDL field name
        pdl_field = pdl_field_mapping.get(data_type, data_type)
        
        print(f"Mapping {data_type} -> {pdl_field} for PDL API")
        
        response = requests.get(
            f"{PDL_BASE_URL}/autocomplete",
            params={
                'api_key': PEOPLE_DATA_LABS_API_KEY,
                'field': pdl_field,  # Use the mapped field name
                'text': query,
                'size': 10
            },
            timeout=15
        )
        
        print(f"PDL autocomplete response: {response.status_code}")
        
        if response.status_code == 200:
            auto_data = response.json()
            if auto_data.get('status') == 200 and auto_data.get('data'):
                suggestions = auto_data['data']
                print(f"Autocomplete suggestions: {suggestions}")
                return suggestions
            else:
                print(f"PDL autocomplete no data: {auto_data}")
                return []
        
        elif response.status_code == 400:
            try:
                error_data = response.json()
                print(f"PDL autocomplete error 400: {error_data}")
                if isinstance(error_data, dict) and 'error' in error_data:
                    msg = error_data['error'].get('message', '')
                    if 'Supported fields are' in msg:
                        print(f"Available fields: {msg}")
            except Exception:
                pass
            return []
        elif response.status_code == 402:
            print("PDL API: Payment required for autocomplete")
            return []
        elif response.status_code == 429:
            print("PDL API rate limited for autocomplete")
            return []
        else:
            print(f"PDL autocomplete error {response.status_code}: {response.text}")
            return []
    
    except requests.exceptions.Timeout:
        print(f"Autocomplete timeout for {data_type}: {query}")
        return []
    except Exception as e:
        print(f"Autocomplete exception for {data_type}: {e}")
        return []


def es_title_block(primary_title: str, similar_titles: list[str] | None):
    """Build Elasticsearch-style title block query with validation"""
    titles = [t.strip().lower() for t in ([primary_title] + (similar_titles or [])) if t and t.strip()]
    
    # ✅ CRITICAL: Don't create empty should clauses
    if not titles:
        print("⚠️ WARNING: No valid titles provided, using fallback query")
        # Fallback to a broad professional query
        return {"exists": {"field": "job_title"}}
    
    # PDL doesn't support minimum_should_match at the top level
    # The "should" clause with at least one match is sufficient
    return {
        "bool": {
            "should": (
                [{"match_phrase": {"job_title": t}} for t in titles] +   # exact phrase
                [{"match": {"job_title": t}} for t in titles]            # token match
            )
        }
    }


def es_title_block_from_enrichment(primary_title: str, similar_titles: list[str] | None):
    """Reuse the already-implemented helper"""
    return es_title_block(primary_title, similar_titles or [])


def determine_location_strategy(location_input):
    """Determine whether to use metro or locality search based on input location"""
    try:
        # Handle empty/None location
        if not location_input or not isinstance(location_input, str) or not location_input.strip():
            return {
                'strategy': 'no_location',
                'metro_location': None,
                'city': None,
                'state': None,
                'original_input': '',
                'matched_metro': None
            }
        
        location_lower = location_input.lower().strip()
        
        # Check for country-only searches (United States, USA, US)
        us_aliases = ['united states', 'usa', 'us', 'united states of america']
        if location_lower in us_aliases:
            return {
                'strategy': 'country_only',
                'metro_location': None,
                'city': None,
                'state': None,
                'original_input': location_input,
                'matched_metro': None
            }
        
        # Parse input location
        if ',' in location_lower:
            parts = [part.strip() for part in location_lower.split(',')]
            city = parts[0]
            state = parts[1] if len(parts) > 1 else None
        else:
            city = location_lower
            state = None
        
        # Check if this location maps to a PDL metro area
        metro_key = None
        metro_location = None
        
        # Direct match check
        if city in PDL_METRO_AREAS:
            metro_key = city
            metro_location = PDL_METRO_AREAS[city]
        
        # Also check full location string
        elif location_lower in PDL_METRO_AREAS:
            metro_key = location_lower
            metro_location = PDL_METRO_AREAS[location_lower]
        
        # Check for partial matches (e.g., "san francisco, ca" matches "san francisco")
        else:
            for metro_name in PDL_METRO_AREAS:
                if metro_name in city or city in metro_name:
                    metro_key = metro_name
                    metro_location = PDL_METRO_AREAS[metro_name]
                    break
        
        if metro_location:
            return {
                'strategy': 'metro_primary',
                'metro_location': metro_location,
                'city': city,
                'state': state,
                'original_input': location_input,
                'matched_metro': metro_key
            }
        else:
            return {
                'strategy': 'locality_primary',
                'metro_location': None,
                'city': city,
                'state': state,
                'original_input': location_input,
                'matched_metro': None
            }
            
    except Exception as e:
        print(f"Error determining location strategy: {e}")
        return {
            'strategy': 'locality_primary',
            'metro_location': None,
            'city': location_input,
            'state': None,
            'original_input': location_input,
            'matched_metro': None
        }


def determine_job_level(job_title):
    """Determine job level from job title for JOB_TITLE_LEVELS search"""
    job_title_lower = job_title.lower()
    
    if any(word in job_title_lower for word in ['intern', 'internship']):
        return 'intern'
    elif any(word in job_title_lower for word in ['entry', 'junior', 'associate', 'coordinator']):
        return 'entry'
    elif any(word in job_title_lower for word in ['senior', 'lead', 'principal']):
        return 'senior'
    elif any(word in job_title_lower for word in ['manager', 'director', 'head']):
        return 'manager'
    elif any(word in job_title_lower for word in ['vp', 'vice president', 'executive', 'chief']):
        return 'executive'
    else:
        return 'mid'  # Default to mid-level


def _parse_pdl_date(s):
    """Parse a PDL date string. Returns timezone-aware UTC datetime or None.

    Per docs.peopledatalabs.com, PDL uses ISO-8601 UTC for top-level
    timestamps (`job_last_updated`, `job_last_changed`,
    `experience[].last_updated`), e.g. ``2024-03-15T21:32:10Z``. Experience
    start/end dates can be partial (``YYYY-MM`` or ``YYYY-MM-DD``).
    """
    if not s or not isinstance(s, str):
        return None
    from datetime import datetime, timezone
    s = s.strip()
    # ISO-8601 with optional Z. Python 3.11+ parses 'Z' natively; older
    # versions need it swapped for '+00:00'.
    try:
        normalized = s[:-1] + "+00:00" if s.endswith("Z") else s
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        pass
    # Partial-date fallback for experience start_date / end_date.
    for fmt in ("%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


# Phase 2.1: PDL emails older than this fall back to Hunter/NeverBounce instead
# of being trusted blindly. People change jobs without PDL noticing; stale
# `emails[]` entries are the #1 source of Loop bounces.
_PDL_EMAIL_MAX_AGE_DAYS = 180


def _pdl_email_is_fresh(person: dict, chosen_email: str = None, max_age_days: int = _PDL_EMAIL_MAX_AGE_DAYS) -> bool:
    """Return True only when PDL has recent evidence the person is still at the
    job the email belongs to.

    Uses only documented PDL fields (per peopledatalabs.com schema):
      - `person.job_last_updated` — primary signal, ISO-8601 UTC.
      - `experience[].last_updated` on a current job (`is_current=True`) — fallback.

    Per-email `first_seen` / `last_seen` are NOT in the public schema, so we
    don't depend on them even when the response happens to include them.

    Defaults to False on missing/unparseable dates — re-verifying via Hunter
    is cheaper than sending to a dead address and burning a contact slot.
    """
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    when = _parse_pdl_date(person.get("job_last_updated"))
    if when and when >= cutoff:
        return True

    for job in person.get("experience") or []:
        if not isinstance(job, dict):
            continue
        if not job.get("is_current"):
            continue
        when = _parse_pdl_date(job.get("last_updated"))
        if when and when >= cutoff:
            return True
        # Only the first current-job entry matters; PDL puts it at experience[0].
        break

    return False


def _choose_best_email(emails: list[dict], recommended: str | None = None) -> str | None:
    """Choose the best email from a list of emails"""
    def is_valid(addr: str) -> bool:
        # Handle case where addr might be a boolean or other non-string type
        if not isinstance(addr, str):
            return False
        if not addr or '@' not in addr: 
            return False
        bad = ["example.com", "test.com", "domain.com", "noreply@"]
        return not any(b in addr.lower() for b in bad)
    
    items = []
    for e in emails or []:
        addr = (e.get("address") or "").strip()
        et = (e.get("type") or "").lower()
        if is_valid(addr):
            items.append((et, addr))
    
    for et, a in items:
        if et in ("work","professional"): 
            return a
    for et, a in items:
        if et == "personal": 
            return a
    
    # Handle case where recommended might be a boolean
    if isinstance(recommended, str) and is_valid(recommended): 
        return recommended
        
    return items[0][1] if items else None


def extract_contact_from_pdl_person_enhanced(person, target_company=None, pre_verified_email=None):
    """
    Enhanced contact extraction with relaxed, sensible email acceptance.
    
    Args:
        person: PDL person data dictionary
        target_company: Target company name for email lookup (required for correct domain extraction)
        pre_verified_email: Optional pre-verified email result from batch verification (✅ TASK 2: avoids redundant Hunter calls)
    """
    import time
    extract_start = time.time()
    try:
        print(f"DEBUG: ⏱️  Starting contact extraction")
        
        # Basic identity
        first_name = person.get('first_name', '')
        last_name = person.get('last_name', '')
        if not first_name or not last_name:
            print(f"DEBUG: Missing name")
            return None

        print(f"DEBUG: Name found - {first_name} {last_name}")

        # Experience
        experience = person.get('experience', []) or []
        if not isinstance(experience, list):
            experience = []
            
        work_experience_details, current_job = [], None
        if experience:
            current_job = experience[0]
            for i, job in enumerate(experience[:5]):
                if not isinstance(job, dict):
                    continue
                company_info = job.get('company') or {}
                title_info = job.get('title') or {}
                company_name = company_info.get('name', '') if isinstance(company_info, dict) else ''
                job_title = title_info.get('name', '') if isinstance(title_info, dict) else ''
                start_date = job.get('start_date') or {}
                end_date = job.get('end_date') or {}

                def fmt(d, default_end=False):
                    if not isinstance(d, dict):
                        return ""
                    y = d.get('year')
                    m = d.get('month')
                    if y:
                        return f"{m or 1}/{y}" if m else f"{y}"
                    return "Present" if default_end else ""

                start_str = fmt(start_date)
                end_str = fmt(end_date, default_end=(i == 0))
                if company_name and job_title:
                    duration = f"{start_str} - {end_str}" if start_str else "Date unknown"
                    work_experience_details.append(f"{job_title} at {company_name} ({duration})")

        company_name = ''
        job_title = ''
        if current_job and isinstance(current_job, dict):
            company_info = current_job.get('company') or {}
            title_info = current_job.get('title') or {}
            company_name = company_info.get('name', '') if isinstance(company_info, dict) else ''
            job_title = title_info.get('name', '') if isinstance(title_info, dict) else ''

        # Location
        location_info = person.get('location') or {}
        city = location_info.get('locality', '') if isinstance(location_info, dict) else ''
        state = location_info.get('region', '') if isinstance(location_info, dict) else ''

        # Email selection - VERIFY PDL EMAILS WITH HUNTER USING TARGET COMPANY DOMAIN
        emails = person.get('emails') or []
        if not isinstance(emails, list):
            emails = []
            
        recommended = person.get('recommended_personal_email') or ''
        if not isinstance(recommended, str):
            recommended = ''
            
        pdl_email = _choose_best_email(emails, recommended)
        
        # ✅ USE TARGET COMPANY DOMAIN INSTEAD OF PDL EMAIL DOMAIN
        # Always use target company domain for email lookup (not PDL email domain which may be from old job)
        from app.services.hunter import get_verified_email, get_company_domain
        
        # Get target company domain
        if target_company:
            target_domain = get_company_domain(target_company)
            print(f"[ContactExtraction] Target company: {target_company}")
            print(f"[ContactExtraction] Target company domain: {target_domain}")
        else:
            target_domain = None
            print(f"[ContactExtraction] ⚠️ No target company provided, using PDL company as fallback")
        
        # Get PDL email domain for comparison
        pdl_domain = None
        if pdl_email and "@" in pdl_email:
            pdl_domain = pdl_email.split("@")[1].lower().strip()
            print(f"[ContactExtraction] PDL email domain: {pdl_domain}")
        
        # Log domain comparison
        if target_domain and pdl_domain:
            if pdl_domain != target_domain.lower():
                print(f"[ContactExtraction] ⚠️ PDL email is from old job ({pdl_domain}), using target domain ({target_domain})")
            else:
                print(f"[ContactExtraction] ✅ PDL email matches target domain")
        
        # ✅ TASK 2: Use pre-verified email if available (from batch verification), otherwise verify individually
        if pre_verified_email and pre_verified_email.get('email'):
            # Skip Hunter verification - use batch result
            best_email = pre_verified_email.get('email')
            email_source = pre_verified_email.get('source', 'hunter_batch')
            email_verified = pre_verified_email.get('verified', False)
            email_confidence_score = int(pre_verified_email.get('score') or 0)
            print(f"[ContactExtraction] ✅ Using pre-verified email from batch: {best_email} (verified: {email_verified}, source: {email_source}, score: {email_confidence_score})")
        else:
            # Fall back to individual verification (existing code)
            company_for_email_lookup = target_company if target_company else company_name

            # ✅ VERIFY PDL EMAIL WITH HUNTER BEFORE USING IT
            # This ensures we don't use outdated/invalid PDL emails
            # Uses target company domain for correct email lookup
            email_verify_start = time.time()
            verified_email_result = get_verified_email(
                pdl_email=pdl_email if pdl_email and pdl_email != "Not available" else None,
                first_name=first_name,
                last_name=last_name,
                company=company_for_email_lookup,  # Use target company, not PDL person's company
                person_data=person  # Pass full person data for context
            )
            email_verify_time = time.time() - email_verify_start
            print(f"DEBUG: ⏱️  Email verification: {email_verify_time:.2f}s for {first_name} {last_name}")

            best_email = verified_email_result.get('email')
            email_source = verified_email_result.get('email_source', 'pdl')
            email_verified = verified_email_result.get('email_verified', False)
            # get_verified_email returns 'score' or 'confidence' depending on the
            # path; default to 0 so the field is always an int downstream.
            email_confidence_score = int(
                verified_email_result.get('score')
                or verified_email_result.get('confidence')
                or 0
            )
        
        # ✅ INCLUDE contacts even without emails (Hunter.io will enrich them)
        if not best_email or best_email == "Not available":
            best_email = "Not available"  # Mark as unavailable but include the contact

        # Phone
        phone_numbers = person.get('phone_numbers') or []
        if not isinstance(phone_numbers, list):
            phone_numbers = []
        phone = phone_numbers[0] if phone_numbers else ''

        # LinkedIn
        profiles = person.get('profiles') or []
        if not isinstance(profiles, list):
            profiles = []
            
        linkedin_url = ''
        for p in profiles:
            if isinstance(p, dict) and 'linkedin' in (p.get('network') or '').lower():
                linkedin_url = p.get('url', '') or ''
                break

        # Education
        education = person.get('education') or []
        if not isinstance(education, list):
            education = []
            
        education_details, college_name = [], ""
        for edu in education:
            if not isinstance(edu, dict):
                continue
            school_info = edu.get('school') or {}
            school_name = school_info.get('name', '') if isinstance(school_info, dict) else ''
            degrees = edu.get('degrees') or []
            
            if not isinstance(degrees, list):
                degrees = []
                
            degree = degrees[0] if degrees else ''
            start_date = edu.get('start_date') or {}
            end_date = edu.get('end_date') or {}
            syear = start_date.get('year') if isinstance(start_date, dict) else None
            eyear = end_date.get('year') if isinstance(end_date, dict) else None

            if school_name:
                entry = school_name
                if degree:
                    entry += f" - {degree}"
                if syear or eyear:
                    entry += f" ({syear or '?'} - {eyear or 'Present'})"
                education_details.append(entry)
                if not college_name and 'high school' not in school_name.lower():
                    college_name = school_name
        education_history = '; '.join(education_details) if education_details else 'Not available'

        # Volunteer
        volunteer_work = []
        interests = person.get('interests') or []
        if not isinstance(interests, list):
            interests = []
            
        for interest in interests:
            if isinstance(interest, str):
                if any(k in interest.lower() for k in ['volunteer', 'charity', 'nonprofit', 'community', 'mentor']):
                    volunteer_work.append(interest)
                elif len(volunteer_work) < 3:
                    volunteer_work.append(f"{interest} enthusiast")

        summary = person.get('summary') or ''
        if isinstance(summary, str):
            vk = ['volunteer', 'charity', 'nonprofit', 'community service', 'mentor', 'coach']
            for k in vk:
                if k in summary.lower():
                    for sentence in summary.split('.'):
                        if k in sentence.lower():
                            volunteer_work.append(sentence.strip())
                            break
        volunteer_history = '; '.join(volunteer_work[:5]) if volunteer_work else 'Not available'

        # Safe email extraction for WorkEmail (use verified email if it's a work email)
        # Use verified email if it's from target company (work email), otherwise use PDL work email
        work_email = 'Not available'
        if best_email and best_email != "Not available":
            # Check if verified email is a work email (from target company domain)
            if target_domain and "@" in best_email:
                email_domain = best_email.split("@")[1].lower().strip()
                if target_domain.lower() in email_domain:
                    work_email = best_email  # Verified work email from target company
                elif email_source == 'hunter.io':
                    work_email = best_email  # Hunter found email (likely work email)
                else:
                    # Personal email - don't use as work email
                    work_email = 'Not available'
            elif email_source == 'hunter.io':
                work_email = best_email  # Hunter found email
            else:
                # Check if it's a work email from PDL
                for e in emails:
                    if isinstance(e, dict):
                        email_addr = e.get('address', '')
                        email_type = (e.get('type') or '').lower()
                        if email_type in ('work', 'professional') and email_addr == best_email:
                            work_email = best_email
                            break
        
        # Extract personal emails for fallback
        personal_emails_list = []
        for e in emails:
            if isinstance(e, dict):
                email_addr = e.get('address', '')
                email_type = (e.get('type') or '').lower()
                if email_type not in ('work', 'professional') and email_addr and "@" in email_addr:
                    personal_emails_list.append(email_addr)
        
        # Add recommended personal email if available
        if recommended and recommended not in personal_emails_list:
            personal_emails_list.append(recommended)

        # Check if currently employed at target company
        is_currently_at_target = False
        if target_company and experience:
            # Check if first job (current job) is at target company and has no end_date
            first_job = experience[0] if isinstance(experience, list) and len(experience) > 0 else None
            if first_job and isinstance(first_job, dict):
                first_job_company = first_job.get('company', {})
                if isinstance(first_job_company, dict):
                    first_job_company_name = first_job_company.get('name', '')
                    first_job_company_website = (first_job_company.get('website') or '').lower().strip()
                    first_job_end_date = first_job.get('end_date')
                    target_domain = _domain_for_company(target_company)

                    is_current_job = (not first_job_end_date
                                      or (isinstance(first_job_end_date, dict) and not first_job_end_date.get('year')))

                    # Domain-based match is authoritative for mapped firms — PDL stores
                    # names under unguessable canonicals, but website is normalized.
                    if target_domain and first_job_company_website == target_domain:
                        if is_current_job:
                            is_currently_at_target = True
                            print(f"[ContactExtraction] ✅ Currently at target company ({target_company}) via website={first_job_company_website}")
                        else:
                            print(f"[ContactExtraction] ⚠️ Previously at target ({target_company}), end_date: {first_job_end_date}")
                    else:
                        # Fall back to name comparison for unmapped firms.
                        cleaned_target = clean_company_name(target_company).lower().strip()
                        cleaned_first_job = clean_company_name(first_job_company_name).lower().strip() if first_job_company_name else ''
                        if cleaned_first_job and cleaned_target:
                            if (cleaned_first_job == cleaned_target
                                    or (cleaned_target in cleaned_first_job and len(cleaned_target) >= 3)
                                    or (cleaned_first_job in cleaned_target and len(cleaned_first_job) >= 3)):
                                if is_current_job:
                                    is_currently_at_target = True
                                    print(f"[ContactExtraction] ✅ Currently at target company ({target_company}) - first job: {first_job_company_name}")
                                else:
                                    print(f"[ContactExtraction] ⚠️ Previously at target company ({target_company}), but left (end_date: {first_job_end_date})")
                            else:
                                print(f"[ContactExtraction] ⚠️ Not at target company - first job: {first_job_company_name} (cleaned: {cleaned_first_job}), target: {target_company} (cleaned: {cleaned_target})")

        # Store minimal experience data for anchor detection (first 2 jobs with dates)
        experience_for_anchors = []
        if experience and isinstance(experience, list):
            for i, job in enumerate(experience[:2]):  # Only need first 2 for transition detection
                if isinstance(job, dict):
                    job_data = {
                        'company': job.get('company', {}),
                        'title': job.get('title', {}),
                        'start_date': job.get('start_date', {}),
                        'end_date': job.get('end_date', {})
                    }
                    experience_for_anchors.append(job_data)
        
        contact = {
            'pdlId': person.get('id', '') or '',
            'FirstName': first_name,
            'LastName': last_name,
            'LinkedIn': linkedin_url,
            'Email': best_email or "Not available",
            'Title': job_title,
            'Company': company_name,
            'City': city,
            'State': state,
            'College': college_name,
            'Phone': phone,
            'PersonalEmail': recommended if isinstance(recommended, str) else '',
            'WorkEmail': work_email,
            'SocialProfiles': f'LinkedIn: {linkedin_url}' if linkedin_url else 'Not available',
            'EducationTop': education_history,
            'VolunteerHistory': volunteer_history,
            'WorkSummary': '; '.join(work_experience_details[:3]) if work_experience_details else f"Professional at {company_name}",
            'Group': f"{company_name} {job_title.split()[0] if job_title else 'Professional'} Team",
            'LinkedInConnections': person.get('linkedin_connections', 0),
            'DataVersion': person.get('dataset_version', 'Unknown'),
            'EmailSource': email_source,  # Track email source (pdl, hunter_finder, pattern, neverbounce_verified, ...)
            'EmailVerified': email_verified,  # Track if email was verified
            'EmailConfidenceScore': email_confidence_score,  # 0-100 confidence score from the providing source
            'IsCurrentlyAtTarget': is_currently_at_target,  # Track if currently at target company
            'experience': experience_for_anchors  # Store minimal experience for anchor detection
        }

        extract_time = time.time() - extract_start
        print(f"DEBUG: ⏱️  Contact extraction successful: {extract_time:.2f}s total for {first_name} {last_name}")
        return contact
        
    except Exception as e:
        extract_time = time.time() - extract_start
        print(f"DEBUG: ⏱️  Failed to extract enhanced contact ({extract_time:.2f}s): {e}")
        import traceback
        traceback.print_exc()
        return None


def add_pdl_enrichment_fields_optimized(contact, person_data):
    """Add enrichment fields based on your product specifications"""
    try:
        # Work summary using experience array (36.8% fill rate)
        experience = person_data.get('experience', [])
        if isinstance(experience, list) and experience:
            current_job = experience[0]
            if isinstance(current_job, dict):
                title_info = current_job.get('title', {})
                company_info = current_job.get('company', {})
                
                title = title_info.get('name', contact.get('Title', '')) if isinstance(title_info, dict) else contact.get('Title', '')
                company = company_info.get('name', contact.get('Company', '')) if isinstance(company_info, dict) else contact.get('Company', '')
                
                work_summary = f"Current {title} at {company}"
                
                # Add years of experience if available (17.5% fill rate)
                years_exp = person_data.get('inferred_years_experience')
                if years_exp:
                    work_summary += f" ({years_exp} years experience)"
                
                if len(experience) > 1:
                    prev_job = experience[1]
                    if isinstance(prev_job, dict):
                        prev_company_info = prev_job.get('company', {})
                        if isinstance(prev_company_info, dict):
                            prev_company = prev_company_info.get('name', '')
                            if prev_company:
                                work_summary += f". Previously at {prev_company}"
                
                contact['WorkSummary'] = work_summary
        else:
            contact['WorkSummary'] = f"Professional at {contact.get('Company', 'current company')}"
        
        # Volunteer History from interests (4.2% fill rate)
        interests = person_data.get('interests', [])
        if isinstance(interests, list) and interests:
            volunteer_activities = []
            for interest in interests[:3]:  # Top 3 interests
                if isinstance(interest, str):
                    volunteer_activities.append(f"{interest} enthusiast")
            
            contact['VolunteerHistory'] = '; '.join(volunteer_activities) if volunteer_activities else 'Not available'
        else:
            contact['VolunteerHistory'] = 'Not available'
        
        # Group/Department (as per your spec)
        contact['Group'] = f"{contact.get('Company', 'Company')} {contact.get('Title', '').split()[0] if contact.get('Title') else 'Professional'} Team"
        
    except Exception as e:
        print(f"Error adding enrichment fields: {e}")


@retry_with_backoff(
    max_retries=3,
    initial_delay=1.0,
    max_delay=60.0,
    retryable_exceptions=(
        requests.exceptions.RequestException,
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
    ),
)
@meter_call("pdl", "person_search")
def execute_pdl_search(headers, url, query_obj, desired_limit, search_type, page_size=50, verbose=False, skip_count=0, target_company=None):
    """
    Execute PDL search with pagination
    
    Args:
        headers: HTTP headers for PDL API
        url: PDL API endpoint URL
        query_obj: PDL query object
        desired_limit: Maximum number of contacts to return
        search_type: Type of search (for logging)
        page_size: Number of results per page
        verbose: Enable verbose logging
        skip_count: Number of results to skip from the beginning (for getting different people)
        target_company: Target company name for email domain extraction (required for correct domain)
    
    Note: Ensures desired_limit and page_size are integers to avoid float slicing errors.
    """
    # ✅ Ensure integer values to avoid float slicing errors
    desired_limit = int(desired_limit)
    page_size = int(page_size)
    import time
    
    search_total_start = time.time()
    pdl_api_time = 0.0
    extract_time = 0.0
    
    # ---- Page 1
    fetch_size = page_size
    # ✅ CRITICAL: Cap at 100 (PDL's max size limit) and ensure integer
    fetch_size = int(min(100, fetch_size))
    # data_include trims response payload to the fields we actually use.
    # Does NOT reduce per-record credit cost, just bandwidth + parse latency.
    body = {"query": query_obj, "size": fetch_size, "data_include": PDL_DATA_INCLUDE}
    
    # ✅ ADD DEBUG LOGGING
    print(f"\n=== PDL {search_type} DEBUG ===")
    print(f"Query being sent:")
    print(json.dumps(body, indent=2, ensure_ascii=False))
    print("=" * 50)
    
    if verbose:
        print(f"\n=== PDL {search_type} PAGE 1 BODY ===")
        print(json.dumps(body, ensure_ascii=False))

    # Use session for connection pooling
    pdl_api_start = time.time()
    with _session_lock:
        r = _session.post(url, headers=headers, json=body, timeout=30)
    pdl_api_time += time.time() - pdl_api_start
    
    # ✅ HANDLE 404 GRACEFULLY - Return (empty, 404) so prompt-search caller can retry with relaxed query
    if r.status_code == 404:
        print(f"\n❌ PDL 404 ERROR - No records found matching query")
        print(f"Response: {r.text}")
        print(f"\n🔍 DIAGNOSIS:")
        print(f"   This usually means:")
        print(f"   1. The job title doesn't exist in PDL database")
        print(f"   2. The location filter is too restrictive")
        print(f"   3. The combination of filters yields zero results")
        print(f"\n💡 SUGGESTIONS:")
        print(f"   - Try a broader job title (e.g., 'engineer' instead of 'senior software engineer')")
        print(f"   - Remove the company filter")
        print(f"   - Try a different location or just state instead of city")
        return ([], 404)
    
    # ✅ HANDLE OTHER ERRORS - raise_for_status will handle 4xx/5xx (except 404)
    if r.status_code != 200:
        print(f"\n❌ PDL ERROR {r.status_code}:")
        print(f"Response: {r.text[:1000]}")
        r.raise_for_status()  # Raise exception for retry logic to catch
    
    j = r.json()

    data   = j.get("data", []) or []
    total  = j.get("total")
    scroll = j.get("scroll_token")

    if verbose:
        print(f"{search_type} page 1: got {len(data)}; total={total}; scroll_token={scroll}")
    
    # Stop early if we already have enough
    if len(data) >= desired_limit or not scroll:
        # OPTIMIZATION: Batch pre-fetch domains for unique companies (when no target_company specified)
        # This reduces duplicate OpenAI calls during extraction
        if not target_company and len(data) > 0:
            batch_domain_start = time.time()
            print(f"[PDL Extract] 🔍 Pre-fetching domains for unique companies...")
            unique_companies = set()
            for person in data[:desired_limit]:
                experience = person.get('experience', []) or []
                if experience and isinstance(experience, list) and len(experience) > 0:
                    company_info = experience[0].get('company') or {}
                    if isinstance(company_info, dict):
                        company_name = company_info.get('name', '').strip()
                        if company_name and company_name.lower() not in ['', 'n/a', 'none', 'unknown']:
                            unique_companies.add(company_name)
            
            if unique_companies:
                from app.services.hunter import get_smart_company_domain
                # Batch lookup domains in parallel (pre-populate cache)
                with ThreadPoolExecutor(max_workers=min(5, len(unique_companies))) as domain_executor:
                    domain_futures = {
                        domain_executor.submit(get_smart_company_domain, company): company 
                        for company in unique_companies
                    }
                    # Wait for all domain lookups to complete (populates cache)
                    for future in as_completed(domain_futures):
                        company = domain_futures[future]
                        try:
                            domain = future.result()
                            if domain:
                                print(f"[PDL Extract] ✅ Pre-fetched domain: {company} → {domain}")
                        except Exception as e:
                            print(f"[PDL Extract] ⚠️ Failed to pre-fetch domain for {company}: {e}")
                batch_domain_time = time.time() - batch_domain_start
                print(f"[PDL Extract] ⏱️  Domain pre-fetch: {batch_domain_time:.2f}s for {len(unique_companies)} companies")
        
        # TRANSFORM THE DATA BEFORE RETURNING - PARALLEL EXTRACTION
        extract_start = time.time()
        extracted_contacts = []
        
        # ✅ ISSUE 1 FIX: Filter duplicates BEFORE extraction to avoid processing same contacts multiple times
        seen_identity_keys = set()
        unique_persons = []
        for person in data[:desired_limit]:
            first_name = (person.get('first_name', '') or '').lower().strip()
            last_name = (person.get('last_name', '') or '').lower().strip()
            # Get company from experience
            company_name = ''
            experience = person.get('experience', []) or []
            if experience and isinstance(experience, list) and len(experience) > 0:
                company_info = experience[0].get('company') or {}
                if isinstance(company_info, dict):
                    company_name = (company_info.get('name', '') or '').lower().strip()
            
            identity_key = f"{first_name}_{last_name}_{company_name}"
            if identity_key not in seen_identity_keys:
                seen_identity_keys.add(identity_key)
                unique_persons.append(person)
        
        duplicate_count = len(data[:desired_limit]) - len(unique_persons)
        if duplicate_count > 0:
            print(f"[PDL Extract] 🔍 Filtered {duplicate_count} duplicate contacts before extraction")
        
        # ✅ TASK 2: Batch verify emails BEFORE individual extraction to reduce Hunter API calls
        batch_email_results = {}
        person_to_batch_index = {}  # Map unique_persons index -> contacts_for_batch index

        # ⚡ Short-circuit: skip batch verification if all contacts already have valid PDL work emails
        # BUT: never short-circuit when there's a target company — PDL emails may be from old jobs
        # AND: require recent PDL evidence (Phase 2.1) — stale work emails point at the previous job.
        _PERSONAL_DOMAINS = {'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'icloud.com', 'protonmail.com', 'me.com', 'live.com', 'msn.com', 'aol.com'}
        _all_have_pdl_email = True
        _pdl_emails_by_idx = {}
        _stale_count = 0
        if not target_company:
            for _sc_idx, _sc_person in enumerate(unique_persons):
                _sc_emails = _sc_person.get('emails') or []
                _sc_recommended = _sc_person.get('recommended_personal_email') or ''
                _sc_email = _choose_best_email(_sc_emails, _sc_recommended) if _sc_emails or _sc_recommended else None
                if (not _sc_email or _sc_email == "Not available" or '@' not in _sc_email
                        or _sc_email.split('@')[1].lower().strip() in _PERSONAL_DOMAINS):
                    _all_have_pdl_email = False
                    break
                if not _pdl_email_is_fresh(_sc_person, _sc_email):
                    _stale_count += 1
                    _all_have_pdl_email = False
                    break
                _pdl_emails_by_idx[_sc_idx] = _sc_email
            if _stale_count:
                print(f"[BatchEmailVerification] PDL email staleness detected — falling back to Hunter verification (max_age={_PDL_EMAIL_MAX_AGE_DAYS}d)")
        else:
            _all_have_pdl_email = False  # Force batch verification for target company searches

        if _all_have_pdl_email and _pdl_emails_by_idx:
            print(f"[BatchEmailVerification] ⚡ Skipped — all {len(_pdl_emails_by_idx)} contacts have valid PDL emails")
            for _sc_idx, _sc_email in _pdl_emails_by_idx.items():
                batch_email_results[_sc_idx] = {'email': _sc_email, 'verified': False, 'source': 'pdl'}

        elif len(unique_persons) > 1:
            print(f"[BatchEmailVerification] ⚡ Running — {len(unique_persons)} contacts need verification")
            try:
                from app.services.hunter import batch_verify_emails_for_contacts

                # Prepare contacts for batch verification (simpler format)
                contacts_for_batch = []
                batch_index = 0
                for person_idx, person in enumerate(unique_persons):
                    first_name = (person.get('first_name', '') or '').strip()
                    last_name = (person.get('last_name', '') or '').strip()
                    if not first_name or not last_name:
                        continue
                    
                    # Get company from experience
                    company_name = ''
                    experience = person.get('experience', []) or []
                    if experience and isinstance(experience, list) and len(experience) > 0:
                        company_info = experience[0].get('company') or {}
                        if isinstance(company_info, dict):
                            company_name = (company_info.get('name', '') or '').strip()
                    
                    # Get PDL email
                    emails = person.get('emails') or []
                    recommended = person.get('recommended_personal_email') or ''
                    pdl_email = _choose_best_email(emails, recommended) if emails or recommended else None
                    
                    contacts_for_batch.append({
                        'first_name': first_name,
                        'last_name': last_name,
                        'company': company_name,
                        'pdl_email': pdl_email if pdl_email and pdl_email != "Not available" else None,
                    })
                    person_to_batch_index[person_idx] = batch_index
                    batch_index += 1
                
                if contacts_for_batch:
                    print(f"[PDL Extract] 📧 Batch verifying emails for {len(contacts_for_batch)} contacts...")
                    batch_results = batch_verify_emails_for_contacts(contacts_for_batch, target_company=target_company)
                    # Map batch results back to unique_persons indices
                    for person_idx, batch_idx in person_to_batch_index.items():
                        if batch_idx in batch_results:
                            batch_email_results[person_idx] = batch_results[batch_idx]
                    print(f"[PDL Extract] ✅ Batch verification complete: {len([r for r in batch_email_results.values() if r.get('email')])} emails verified")
            except Exception as batch_error:
                print(f"[PDL Extract] ⚠️ Batch email verification failed, falling back to individual verification: {batch_error}")
                import traceback
                traceback.print_exc()
                batch_email_results = {}
        
        # Use parallel extraction to speed up email verification
        # ✅ ISSUE 5 FIX: Increased from 5 to 10 workers for faster extraction
        max_workers = min(10, len(unique_persons))
        persons_to_extract = unique_persons
        
        if max_workers > 1 and len(persons_to_extract) > 1:
            print(f"[PDL Extract] ⚡ Parallel extraction with {max_workers} workers for {len(persons_to_extract)} contacts...")
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all extraction tasks with pre-verified email results
                future_to_person = {
                    executor.submit(
                        extract_contact_from_pdl_person_enhanced, 
                        person, 
                        target_company,
                        batch_email_results.get(i, {})  # Pass pre-verified email result
                    ): (i, person)
                    for i, person in enumerate(persons_to_extract)
                }
                
                # Collect results as they complete, with early stopping for contacts with emails
                contacts_with_emails = 0
                max_contacts_with_emails = desired_limit * 2  # Get up to 2x to have verified options
                for future in as_completed(future_to_person):
                    i, person = future_to_person[future]
                    try:
                        contact = future.result()
                        if contact:  # Only add if extraction was successful
                            extracted_contacts.append(contact)
                            # Check if contact has any email (verified or not)
                            if contact.get('Email') or contact.get('WorkEmail') or contact.get('PersonalEmail'):
                                contacts_with_emails += 1
                            
                            # Early stop if we have enough contacts with emails (verified filtering happens later)
                            if contacts_with_emails >= max_contacts_with_emails:
                                # Cancel remaining futures to save processing time
                                remaining = len(future_to_person) - len(extracted_contacts)
                                if remaining > 0:
                                    print(f"[PDL Extract] ⚡ Early stopping: Have {contacts_with_emails} contacts with emails, cancelling {remaining} remaining extractions...")
                                    for remaining_future in future_to_person:
                                        if remaining_future != future and not remaining_future.done():
                                            remaining_future.cancel()
                                break
                    except Exception as e:
                        name = f"{person.get('first_name', '')} {person.get('last_name', '')}".strip()
                        print(f"[PDL Extract] ❌ Error extracting {name}: {e}")
        else:
            # Fallback to sequential for small batches
            for i, person in enumerate(persons_to_extract):
                try:
                    contact = extract_contact_from_pdl_person_enhanced(person, target_company=target_company)
                    if contact:  # Only add if extraction was successful
                        extracted_contacts.append(contact)
                except Exception as e:
                    name = f"{person.get('first_name', '')} {person.get('last_name', '')}".strip() or f"person {i+1}"
                    print(f"[PDL Extract] ❌ Error extracting {name}: {e}")
        
        extract_time = time.time() - extract_start
        total_time = time.time() - search_total_start
        
        print(f"\n[PDL {search_type}] ⏱️  TIMING BREAKDOWN:")
        print(f"[PDL {search_type}]   ├── PDL API calls: {pdl_api_time:.2f}s ({pdl_api_time/total_time*100:.1f}%)")
        print(f"[PDL {search_type}]   └── Contact extraction: {extract_time:.2f}s ({extract_time/total_time*100:.1f}%)")
        print(f"[PDL {search_type}] ⏱️  Total: {total_time:.2f}s for {len(extracted_contacts)}/{len(data[:desired_limit])} contacts")
        if len(data[:desired_limit]) > 0:
            avg_extract = extract_time / len(data[:desired_limit])
            print(f"[PDL {search_type}] ⏱️  Avg extraction per contact: {avg_extract:.2f}s")
        
        return (extracted_contacts, 200)

    # ---- Page 2+
    while scroll and len(data) < desired_limit:
        body2 = {"scroll_token": scroll, "size": int(page_size), "data_include": PDL_DATA_INCLUDE}
        if verbose:
            print(f"\n=== PDL {search_type} NEXT PAGE BODY ===")
            print(json.dumps(body2, ensure_ascii=False))

        # Use session for connection pooling
        pdl_api_start = time.time()
        with _session_lock:
            r2 = _session.post(url, headers=headers, json=body2, timeout=30)
        pdl_api_time += time.time() - pdl_api_start

        # Be robust to cluster quirk: require query/sql
        if r2.status_code == 400 and "Either `query` or `sql` must be provided" in (r2.text or ""):
            if verbose:
                print(f"{search_type} retrying with query+scroll_token due to 400…")
            body2_fallback = {"query": query_obj, "scroll_token": scroll, "size": int(page_size), "data_include": PDL_DATA_INCLUDE}
            pdl_api_start = time.time()
            with _session_lock:
                r2 = _session.post(url, headers=headers, json=body2_fallback, timeout=30)
            pdl_api_time += time.time() - pdl_api_start

        if r2.status_code != 200:
            if verbose:
                print(f"{search_type} next page status={r2.status_code} err={r2.text}")
            break

        j2 = r2.json()
        batch  = j2.get("data", []) or []
        scroll = j2.get("scroll_token")
        data.extend(batch)

        if verbose:
            print(f"{search_type} next page: got {len(batch)}, total so far={len(data)}, next scroll={scroll}")

    # OPTIMIZATION: Batch pre-fetch domains for unique companies (when no target_company specified)
    # This reduces duplicate OpenAI calls during extraction
    if not target_company and len(data) > 0:
        batch_domain_start = time.time()
        print(f"[PDL Extract] 🔍 Pre-fetching domains for unique companies...")
        unique_companies = set()
        for person in data[:desired_limit]:
            experience = person.get('experience', []) or []
            if experience and isinstance(experience, list) and len(experience) > 0:
                company_info = experience[0].get('company') or {}
                if isinstance(company_info, dict):
                    company_name = company_info.get('name', '').strip()
                    if company_name and company_name.lower() not in ['', 'n/a', 'none', 'unknown']:
                        unique_companies.add(company_name)
        
        if unique_companies:
            from app.services.hunter import get_smart_company_domain
            # Batch lookup domains in parallel (pre-populate cache)
            with ThreadPoolExecutor(max_workers=min(5, len(unique_companies))) as domain_executor:
                domain_futures = {
                    domain_executor.submit(get_smart_company_domain, company): company 
                    for company in unique_companies
                }
                # Wait for all domain lookups to complete (populates cache)
                for future in as_completed(domain_futures):
                    company = domain_futures[future]
                    try:
                        domain = future.result()
                        if domain:
                            print(f"[PDL Extract] ✅ Pre-fetched domain: {company} → {domain}")
                    except Exception as e:
                        print(f"[PDL Extract] ⚠️ Failed to pre-fetch domain for {company}: {e}")
            batch_domain_time = time.time() - batch_domain_start
            print(f"[PDL Extract] ⏱️  Domain pre-fetch: {batch_domain_time:.2f}s for {len(unique_companies)} companies")

    # TRANSFORM ALL THE DATA BEFORE RETURNING - PARALLEL EXTRACTION
    extract_start = time.time()
    extracted_contacts = []
    
    # ✅ ISSUE 1 FIX: Filter duplicates BEFORE extraction to avoid processing same contacts multiple times
    seen_identity_keys = set()
    unique_persons = []
    for person in data[:desired_limit]:
        first_name = (person.get('first_name', '') or '').lower().strip()
        last_name = (person.get('last_name', '') or '').lower().strip()
        # Get company from experience
        company_name = ''
        experience = person.get('experience', []) or []
        if experience and isinstance(experience, list) and len(experience) > 0:
            company_info = experience[0].get('company') or {}
            if isinstance(company_info, dict):
                company_name = (company_info.get('name', '') or '').lower().strip()
        
        identity_key = f"{first_name}_{last_name}_{company_name}"
        if identity_key not in seen_identity_keys:
            seen_identity_keys.add(identity_key)
            unique_persons.append(person)
    
    duplicate_count = len(data[:desired_limit]) - len(unique_persons)
    if duplicate_count > 0:
        print(f"[PDL Extract] 🔍 Filtered {duplicate_count} duplicate contacts before extraction")
    
    # ✅ TASK 2: Batch verify emails BEFORE individual extraction to reduce Hunter API calls
    batch_email_results = {}
    person_to_batch_index = {}  # Map unique_persons index -> contacts_for_batch index
    if len(unique_persons) > 1:
        try:
            from app.services.hunter import batch_verify_emails_for_contacts
            
            # Prepare contacts for batch verification (simpler format)
            contacts_for_batch = []
            batch_index = 0
            for person_idx, person in enumerate(unique_persons):
                first_name = (person.get('first_name', '') or '').strip()
                last_name = (person.get('last_name', '') or '').strip()
                if not first_name or not last_name:
                    continue
                
                # Get company from experience
                company_name = ''
                experience = person.get('experience', []) or []
                if experience and isinstance(experience, list) and len(experience) > 0:
                    company_info = experience[0].get('company') or {}
                    if isinstance(company_info, dict):
                        company_name = (company_info.get('name', '') or '').strip()
                
                # Get PDL email
                emails = person.get('emails') or []
                recommended = person.get('recommended_personal_email') or ''
                pdl_email = _choose_best_email(emails, recommended) if emails or recommended else None
                
                contacts_for_batch.append({
                    'first_name': first_name,
                    'last_name': last_name,
                    'company': company_name,
                    'pdl_email': pdl_email if pdl_email and pdl_email != "Not available" else None,
                })
                person_to_batch_index[person_idx] = batch_index
                batch_index += 1
            
            if contacts_for_batch:
                print(f"[PDL Extract] 📧 Batch verifying emails for {len(contacts_for_batch)} contacts...")
                batch_results = batch_verify_emails_for_contacts(contacts_for_batch, target_company=target_company)
                # Map batch results back to unique_persons indices
                for person_idx, batch_idx in person_to_batch_index.items():
                    if batch_idx in batch_results:
                        batch_email_results[person_idx] = batch_results[batch_idx]
                print(f"[PDL Extract] ✅ Batch verification complete: {len([r for r in batch_email_results.values() if r.get('email')])} emails verified")
        except Exception as batch_error:
            print(f"[PDL Extract] ⚠️ Batch email verification failed, falling back to individual verification: {batch_error}")
            import traceback
            traceback.print_exc()
            batch_email_results = {}
    
    # Use parallel extraction to speed up email verification
    # ✅ ISSUE 5 FIX: Increased from 5 to 10 workers for faster extraction
    max_workers = min(10, len(unique_persons))
    persons_to_extract = unique_persons
    
    if max_workers > 1 and len(persons_to_extract) > 1:
        print(f"[PDL Extract] ⚡ Parallel extraction with {max_workers} workers for {len(persons_to_extract)} contacts...")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all extraction tasks with pre-verified email results
            future_to_person = {
                executor.submit(
                    extract_contact_from_pdl_person_enhanced, 
                    person, 
                    target_company,
                    batch_email_results.get(i, {})  # Pass pre-verified email result
                ): (i, person)
                for i, person in enumerate(persons_to_extract)
            }
            
            # Collect results as they complete, with early stopping for contacts with emails
            contacts_with_emails = 0
            max_contacts_with_emails = desired_limit * 2  # Get up to 2x to have verified options
            for future in as_completed(future_to_person):
                i, person = future_to_person[future]
                try:
                    contact = future.result()
                    if contact:  # Only add if extraction was successful
                        extracted_contacts.append(contact)
                        # Check if contact has any email (verified or not)
                        if contact.get('Email') or contact.get('WorkEmail') or contact.get('PersonalEmail'):
                            contacts_with_emails += 1
                        
                        # Early stop if we have enough contacts with emails (verified filtering happens later)
                        if contacts_with_emails >= max_contacts_with_emails:
                            # Cancel remaining futures to save processing time
                            remaining = len(future_to_person) - len(extracted_contacts)
                            if remaining > 0:
                                print(f"[PDL Extract] ⚡ Early stopping: Have {contacts_with_emails} contacts with emails, cancelling {remaining} remaining extractions...")
                                for remaining_future in future_to_person:
                                    if remaining_future != future and not remaining_future.done():
                                        remaining_future.cancel()
                            break
                except Exception as e:
                    name = f"{person.get('first_name', '')} {person.get('last_name', '')}".strip()
                    print(f"[PDL Extract] ❌ Error extracting {name}: {e}")
    else:
        # Fallback to sequential for small batches
        for i, person in enumerate(persons_to_extract):
            try:
                # Pass pre-verified email result if available
                pre_verified = batch_email_results.get(i, {})
                contact = extract_contact_from_pdl_person_enhanced(person, target_company=target_company, pre_verified_email=pre_verified)
                if contact:  # Only add if extraction was successful
                    extracted_contacts.append(contact)
            except Exception as e:
                name = f"{person.get('first_name', '')} {person.get('last_name', '')}".strip() or f"person {i+1}"
                print(f"[PDL Extract] ❌ Error extracting {name}: {e}")
    
    extract_time = time.time() - extract_start
    total_time = time.time() - search_total_start
    
    print(f"\n[PDL {search_type}] ⏱️  TIMING BREAKDOWN:")
    print(f"[PDL {search_type}]   ├── PDL API calls: {pdl_api_time:.2f}s ({pdl_api_time/total_time*100:.1f}%)")
    print(f"[PDL {search_type}]   └── Contact extraction: {extract_time:.2f}s ({extract_time/total_time*100:.1f}%)")
    print(f"[PDL {search_type}] ⏱️  Total: {total_time:.2f}s for {len(extracted_contacts)}/{len(data[:desired_limit])} contacts")
    if len(data[:desired_limit]) > 0:
        avg_extract = extract_time / len(data[:desired_limit])
        print(f"[PDL {search_type}] ⏱️  Avg extraction per contact: {avg_extract:.2f}s")
    
    return (extracted_contacts, 200)


def try_metro_search_optimized(clean_title, similar_titles, company, location_strategy, max_contacts=8, college_alumni=None, exclude_keys=None):
    """Metro search with complete validation and exclusion filtering"""
    
    # ✅ Handle exclusion keys
    excluded_keys = exclude_keys or set()
    
    # Validate inputs
    if not clean_title or not clean_title.strip():
        print("❌ No valid job title")
        return []
    
    strategy = location_strategy.get("strategy", "locality_primary")
    metro_location = (location_strategy.get("metro_location") or "").lower()
    city = (location_strategy.get("city") or "").lower()
    state = (location_strategy.get("state") or "").lower()
    
    # Handle country-only search (United States, USA, US)
    if strategy == 'country_only':
        # Build title block with validation
        title_block = es_title_block_from_enrichment(clean_title, similar_titles)
        
        # Validate title block isn't empty
        if not title_block.get("bool", {}).get("should") and not title_block.get("exists"):
            print("❌ Empty title block")
            return []
        
        # Only filter by country for country-only searches
        location_must = [{"term": {"location_country": "united states"}}]
        loc_block = {"bool": {"must": location_must}}
    elif not metro_location and not city:
        print("❌ No valid location")
        return []
    else:
        # Build title block with validation
        title_block = es_title_block_from_enrichment(clean_title, similar_titles)
        
        # Validate title block isn't empty
        if not title_block.get("bool", {}).get("should") and not title_block.get("exists"):
            print("❌ Empty title block")
            return []
        
        # Build location filter
        location_must = []
        
        # Use match instead of term for more flexible location matching
        if metro_location and city:
            location_must.append({
                "bool": {
                    "should": [
                        {"match": {"location_metro": metro_location}},
                        {"match": {"location_locality": city}}
                    ]
                }
            })
        elif metro_location:
            location_must.append({"match": {"location_metro": metro_location}})
        elif city:
            location_must.append({"match": {"location_locality": city}})
        
        # Make state optional - use should instead of must
        if state:
            location_must.append({
                "bool": {
                    "should": [
                        {"match": {"location_region": state}},
                        {"match": {"location_locality": state}}  # Sometimes state is in city field
                    ]
                }
            })
        
        location_must.append({"term": {"location_country": "united states"}})
        
        loc_block = {"bool": {"must": location_must}}

    # Build final query - only include blocks that exist
    must = [title_block]
    if loc_block:
        must.append(loc_block)
    
    # Add optional filters - use both match_phrase and match for flexibility
    if company and company.strip():
        company_lower = company.lower().strip()
        must.append({
            "bool": {
                "should": [
                    {"match_phrase": {"job_company_name": company_lower}},
                    {"match": {"job_company_name": company_lower}}
                ]
            }
        })
        # NOTE: job_company_name already refers to current position in PDL's ES API

    # ✅ ADD EDUCATION FILTER TO QUERY - use both match_phrase and match
    if college_alumni:
        aliases = _school_aliases(college_alumni)
        if aliases:
            # Use both match_phrase (exact) and match (flexible) for better coverage
            education_clauses = []
            for a in aliases:
                education_clauses.append({"match_phrase": {"education.school.name": a}})
                education_clauses.append({"match": {"education.school.name": a}})
            must.append({
                "bool": {
                    "should": education_clauses
                }
            })

    # P0 FIX: Only return contacts that have email addresses
    must.append({"exists": {"field": "emails"}})

    # ✅ ALL FILTERS IN MUST CLAUSE - PDL only returns people matching ALL criteria:
    #   1. Job title (title_block)
    #   2. Location (loc_block)
    #   3. Company (if provided)
    #   4. Education/school (if provided)
    #   5. Has email (always)
    # This is efficient - PDL filters at query time, not post-processing
    query_obj = {"bool": {"must": must}}

    # Execute with proper error handling
    try:
        PDL_URL = f"{PDL_BASE_URL}/person/search"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Api-Key": PEOPLE_DATA_LABS_API_KEY,
        }

        # ✅ Use max_contacts directly - caller already applies 2.5x multiplier
        # Only add small buffer for post-filtering (alumni filtering, exclusion keys)
        if college_alumni:
            # Alumni filter in query means PDL returns mostly alumni
            fetch_limit = max_contacts * 1.5  # Small buffer for variation
        else:
            # No alumni filter - minimal buffer for exclusion keys
            fetch_limit = max_contacts * 1.5  # Minimal buffer for exclusion keys
        
        # Cap fetch_limit to prevent over-fetching (max 2.5x or 50, whichever is smaller)
        fetch_limit = int(min(fetch_limit, max_contacts * 2.5, 50))
        
        page_size = int(min(100, max(1, fetch_limit)))
        
        raw_contacts, _ = execute_pdl_search(
            headers=headers,
            url=PDL_URL,
            query_obj=query_obj,
            desired_limit=int(fetch_limit),
            search_type=f"metro_{location_strategy.get('matched_metro','unknown')}",
            page_size=page_size,
            verbose=False,
            target_company=company  # Pass target company for correct domain extraction
        )
        
        # ✅ FILTER OUT EXCLUDED CONTACTS
        if excluded_keys:
            filtered_contacts = []
            skipped_count = 0
            
            for contact in raw_contacts:
                contact_key = get_contact_identity(contact)
                if contact_key in excluded_keys:
                    skipped_count += 1
                    continue
                filtered_contacts.append(contact)
                if len(filtered_contacts) >= max_contacts:
                    break
            
            print(f"🔍 Metro search filtering:")
            print(f"   - Raw results from PDL: {len(raw_contacts)}")
            print(f"   - Excluded (already seen): {skipped_count}")
            print(f"   - Unique new contacts: {len(filtered_contacts)}")
            
            return filtered_contacts[:max_contacts]
        else:
            return raw_contacts[:max_contacts]
            
    except Exception as e:
        print(f"Metro search failed: {e}")
        return []

def try_locality_search_optimized(clean_title, similar_titles, company, location_strategy, max_contacts=8, college_alumni=None, exclude_keys=None):
    """
    Locality-focused version (used when metro results are thin).
    STRICT LOCATION: city AND state AND country, or country-only for United States
    """
    # ✅ Handle exclusion keys
    excluded_keys = exclude_keys or set()
    
    title_block = es_title_block_from_enrichment(clean_title, similar_titles)
    
    strategy = location_strategy.get("strategy", "locality_primary")
    
    # Handle country-only search (United States, USA, US)
    if strategy == 'country_only':
        # Only filter by country for country-only searches
        location_must = [{"term": {"location_country": "united states"}}]
        loc_block = {"bool": {"must": location_must}}
    else:
        # BUILD STRICT LOCATION FILTER
        city = (location_strategy.get("city") or "").lower()
        state = (location_strategy.get("state") or "").lower()
        
        location_must = []
        
        # Use match for more flexible city matching
        if city:
            location_must.append({"match": {"location_locality": city}})
        
        # Make state optional - use should for flexibility
        if state:
            location_must.append({
                "bool": {
                    "should": [
                        {"match": {"location_region": state}},
                        {"match": {"location_locality": state}}  # Sometimes state is in city field
                    ]
                }
            })
        
        # Always require USA
        location_must.append({"term": {"location_country": "united states"}})
        
        loc_block = {"bool": {"must": location_must}}

    must = [title_block, loc_block]
    if company:
        # Use both match_phrase and match for flexibility
        company_lower = company.lower().strip()
        must.append({
            "bool": {
                "should": [
                    {"match_phrase": {"job_company_name": company_lower}},
                    {"match": {"job_company_name": company_lower}}
                ]
            }
        })
        # NOTE: job_company_name already refers to current position in PDL's ES API

    # ✅ NO EDUCATION FILTER IN QUERY - alumni filtering happens post-fetch via _contact_has_school_as_primary_education_lenient()
    # This makes the PDL query broader (title + company + location) and more likely to return results
    # Python post-filtering then verifies alumni status

    # P0 FIX: Only return contacts that have email addresses
    must.append({"exists": {"field": "emails"}})

    # ✅ ALL FILTERS IN MUST CLAUSE - PDL only returns people matching ALL criteria:
    #   1. Job title (title_block)
    #   2. Location (loc_block)
    #   3. Company (if provided)
    #   4. Has email (always)
    # Education/school filtering happens post-fetch in Python
    query_obj = {"bool": {"must": must}}

    PDL_URL = f"{PDL_BASE_URL}/person/search"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Api-Key": PEOPLE_DATA_LABS_API_KEY,
    }

    # ✅ Use max_contacts directly - caller already applies 2.5x multiplier
    # Only add small buffer for post-filtering (alumni filtering, exclusion keys)
    if college_alumni:
        # Post-filtering means we need a small buffer
        fetch_limit = max_contacts * 2  # Small buffer for post-filtering
    else:
        fetch_limit = max_contacts * 1.5  # Minimal buffer for exclusion keys
    
    # Cap fetch_limit to prevent over-fetching (max 2.5x or 50, whichever is smaller)
    fetch_limit = int(min(fetch_limit, max_contacts * 2.5, 50))
    
    page_size = int(min(100, max(1, fetch_limit)))

    raw_contacts, _ = execute_pdl_search(
        headers=headers,
        url=PDL_URL,
        query_obj=query_obj,
        desired_limit=int(fetch_limit),
        search_type=f"locality_{location_strategy.get('city','unknown')}",
        page_size=page_size,
        verbose=False,
        target_company=company  # Pass target company for correct domain extraction
    )
    
    # ✅ FILTER OUT EXCLUDED CONTACTS
    if excluded_keys:
        filtered_contacts = []
        skipped_count = 0
        
        for contact in raw_contacts:
            contact_key = get_contact_identity(contact)
            if contact_key in excluded_keys:
                skipped_count += 1
                continue
            filtered_contacts.append(contact)
            if len(filtered_contacts) >= max_contacts:
                break
        
        print(f"🔍 Locality search filtering:")
        print(f"   - Raw results from PDL: {len(raw_contacts)}")
        print(f"   - Excluded (already seen): {skipped_count}")
        print(f"   - Unique new contacts: {len(filtered_contacts)}")
        
        return filtered_contacts[:max_contacts]
    else:
        return raw_contacts[:max_contacts]


def try_job_title_levels_search_enhanced(job_title_enrichment, company, city, state, max_contacts, college_alumni=None, exclude_keys=None):
    """Enhanced job title levels search"""
    # ✅ Handle exclusion keys
    excluded_keys = exclude_keys or set()
    
    print("Enhanced job title levels search")

    must = []

    levels = job_title_enrichment.get('levels') or []
    if levels:
        must.append({"bool": {"should": [{"match": {"job_title_levels": lvl}} for lvl in levels]}})
    else:
        jl = determine_job_level(job_title_enrichment.get('cleaned_name', ''))
        if jl:
            must.append({"match": {"job_title_levels": jl}})

    # Also broaden titles
    must.append(es_title_block(job_title_enrichment.get('cleaned_name',''),
                               job_title_enrichment.get('similar_titles') or []))

    if company:
        # Use both match_phrase and match for flexibility
        company_lower = (company or "").lower().strip()
        must.append({
            "bool": {
                "should": [
                    {"match_phrase": {"job_company_name": company_lower}},
                    {"match": {"job_company_name": company_lower}}
                ]
            }
        })
        # NOTE: job_company_name already refers to current position in PDL's ES API

    location_must = []

    # Use match for more flexible city matching
    if city:
        location_must.append({"match": {"location_locality": city}})

    # Make state optional - use should for flexibility
    if state:
        location_must.append({
            "bool": {
                "should": [
                    {"match": {"location_region": state}},
                    {"match": {"location_locality": state}}  # Sometimes state is in city field
                ]
            }
        })

    # Always require USA
    location_must.append({"term": {"location_country": "united states"}})

    must.append({"bool": {"must": location_must}})

    # P0 FIX: Only return contacts that have email addresses
    must.append({"exists": {"field": "emails"}})

    # ✅ ALL FILTERS IN MUST CLAUSE - PDL only returns people matching ALL criteria:
    #   1. Job title (title_block)
    #   2. Location (loc_block)
    #   3. Company (if provided)
    #   4. Has email (always)
    # Education/school filtering happens post-fetch in Python
    query_obj = {"bool": {"must": must}}

    PDL_URL = f"{PDL_BASE_URL}/person/search"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Api-Key": PEOPLE_DATA_LABS_API_KEY,
    }

    # ✅ Use max_contacts directly - caller already applies 2.5x multiplier
    # Only add small buffer for post-filtering (alumni filtering, exclusion keys)
    if college_alumni:
        # Post-filtering means we need a small buffer
        fetch_limit = max_contacts * 2  # Small buffer for post-filtering
    else:
        fetch_limit = max_contacts * 1.5  # Minimal buffer for exclusion keys
    
    # Cap fetch_limit to prevent over-fetching (max 2.5x or 50, whichever is smaller)
    fetch_limit = int(min(fetch_limit, max_contacts * 2.5, 50))
    
    page_size = int(min(100, max(1, fetch_limit)))

    raw_contacts, _ = execute_pdl_search(
        headers=headers,
        url=PDL_URL,
        query_obj=query_obj,
        desired_limit=int(fetch_limit),
        search_type="job_levels_enhanced",
        page_size=page_size,
        verbose=False,
        target_company=company  # Pass target company for correct domain extraction
    )
    
    # ✅ FILTER OUT EXCLUDED CONTACTS
    if excluded_keys:
        filtered_contacts = []
        skipped_count = 0
        
        for contact in raw_contacts:
            contact_key = get_contact_identity(contact)
            if contact_key in excluded_keys:
                skipped_count += 1
                continue
            filtered_contacts.append(contact)
            if len(filtered_contacts) >= max_contacts:
                break
        
        print(f"🔍 Job levels search filtering:")
        print(f"   - Raw results from PDL: {len(raw_contacts)}")
        print(f"   - Excluded (already seen): {skipped_count}")
        print(f"   - Unique new contacts: {len(filtered_contacts)}")
        
        return filtered_contacts[:max_contacts]
    else:
        return raw_contacts[:max_contacts]


def search_contacts_with_smart_location_strategy(
    job_title, company, location, max_contacts=8, college_alumni=None, exclude_keys=None
):

    """
    ENHANCED VERSION: Guarantees the requested number of verified alumni contacts
    
    When alumni filter is active, this function will:
    1. Fetch contacts in batches
    2. Filter for verified degree holders
    3. Continue fetching until we have enough verified alumni
    4. Return exactly the requested number
    
    PERFORMANCE OPTIMIZED: Parallelizes enrichment/cleaning operations
    """
    try:
        # ✅ ADD COMPREHENSIVE INPUT VALIDATION
        print(f"\n{'='*70}")
        print(f"🔍 PDL SEARCH STARTED (OPTIMIZED)")
        print(f"{'='*70}")
        print(f"📥 Input Parameters:")
        print(f"  ├─ job_title: '{job_title}'")
        print(f"  ├─ company: '{company}'")
        print(f"  ├─ location: '{location}'")
        print(f"  ├─ max_contacts: {max_contacts}")
        print(f"  ├─ college_alumni: '{college_alumni}'")
        print(f"  └─ exclude_keys: {len(exclude_keys) if exclude_keys else 0} contacts")
        
        # ✅ VALIDATE REQUIRED INPUTS
        # Note: job_title can be empty for prompt search (will search without job title filter)
        # if not job_title or not job_title.strip():
        #     print(f"❌ ERROR: job_title is required but was empty or None")
        #     return []
        
        # Location is required for general search (not prompt search)
        if not location or not location.strip():
            print(f"❌ ERROR: location is required but was empty or None")
            return []
        
        print(f"{'='*70}\n")
        
        print(f"Starting smart location search for {job_title} at {company} in {location}")
        if college_alumni:
            print(f"🎓 Alumni filter enabled: {college_alumni}")
        
        # ✅ OPTIMIZED: Parallelize enrichment and cleaning operations
        import time
        start_time = time.time()
        
        with ThreadPoolExecutor(max_workers=3) as executor:
            # Submit all enrichment/cleaning tasks in parallel
            future_title = executor.submit(cached_enrich_job_title, job_title)
            future_company = executor.submit(clean_company_name, company) if company else None
            future_location = executor.submit(clean_location_name, location)
            
            # Wait for all to complete
            job_title_enrichment = future_title.result()
            cleaned_company = future_company.result() if future_company else ''
            cleaned_location = future_location.result()
        
        enrichment_time = time.time() - start_time
        print(f"⚡ Parallel enrichment completed in {enrichment_time:.2f}s (vs ~3-5s sequential)")
        
        primary_title = job_title_enrichment.get('cleaned_name', job_title).lower()
        similar_titles = [t.lower() for t in job_title_enrichment.get('similar_titles', [])[:4]]
        
        # Analyze location strategy
        location_strategy = determine_location_strategy(cleaned_location)
        
        print(f"Location strategy: {location_strategy['strategy']}")
        if location_strategy['matched_metro']:
            print(f"Matched metro: {location_strategy['matched_metro']} -> {location_strategy['metro_location']}")
        
        # ✅ FIX #6: Pre-populate domain cache before parallel searches
        # This ensures both metro and locality searches share the same cache
        if cleaned_company:
            from app.services.hunter import get_smart_company_domain
            print(f"🔍 Pre-populating domain cache for company: {cleaned_company}")
            domain = get_smart_company_domain(cleaned_company)
            if domain:
                print(f"✅ Domain cached: {cleaned_company} → {domain}")
        
        # Step 4: CHANGED - If alumni filter is active, use batch fetching strategy
        if college_alumni:
            return _fetch_verified_alumni_contacts(
                primary_title, similar_titles, cleaned_company,
                location_strategy, job_title_enrichment,
                max_contacts, college_alumni, exclude_keys
            )
        
        # Step 5: For non-alumni searches, use optimized parallel search
        search_start = time.time()
        contacts = _fetch_contacts_standard_parallel(
            primary_title, similar_titles, cleaned_company,
            location_strategy, job_title_enrichment,
            max_contacts, exclude_keys
        )
        search_time = time.time() - search_start
        
        total_time = time.time() - start_time
        
        # LOG FINAL RESULTS WITH TIMING
        print(f"\n{'='*70}")
        print(f"⏱️  SEARCH TIMING SUMMARY")
        print(f"{'='*70}")
        print(f"⏱️  Enrichment: {enrichment_time:.2f}s ({enrichment_time/total_time*100:.1f}%)")
        print(f"⏱️  PDL search + extraction: {search_time:.2f}s ({search_time/total_time*100:.1f}%)")
        print(f"⏱️  Total time: {total_time:.2f}s")
        print(f"📊 Contacts found: {len(contacts)}/{max_contacts}")
        if len(contacts) > 0:
            avg_time_per_contact = search_time / len(contacts)
            print(f"⏱️  Avg time per contact: {avg_time_per_contact:.2f}s")
        
        if len(contacts) == 0:
            print(f"⚠️  WARNING: No contacts found with valid emails for {job_title} in {location}")
            print(f"Search parameters: title='{primary_title}', company='{cleaned_company}', location='{cleaned_location}'")
        else:
            print(f"✅ Smart location search completed: {len(contacts)} contacts found with valid emails")
        print(f"{'='*70}\n")
        
        return contacts[:max_contacts]
        
    except Exception as e:
        print(f"Smart location search failed: {e}")
        import traceback
        traceback.print_exc()
        return []



def search_contacts_with_pdl_optimized(job_title, company, location, max_contacts=8):
    """Updated main search function using smart location strategy"""
    return search_contacts_with_smart_location_strategy(job_title, company, location, max_contacts)


def search_contacts_with_pdl(job_title, company, location, max_contacts=8):
    """Wrapper function - redirect to optimized version for backward compatibility"""
    return search_contacts_with_pdl_optimized(job_title, company, location, max_contacts)


# Role-family + seniority-adjacent title expansions used by retry_level=1
# and retry_level=2 live in `app.utils.role_taxonomy` so the Perplexity
# job-search broadening flow can reuse the same dicts. Imported at the top
# of this module; behavior is byte-identical to the prior inline copy.


def build_query_from_prompt(parsed_prompt: dict, retry_level: int = 0,
                             exclude_pdl_ids: list | None = None) -> dict:
    """
    Build PDL Elasticsearch bool query from structured prompt parser output.
    Uses same patterns as es_title_block, try_metro_search_optimized (location, company, schools).
    Returns query_obj ready for execute_pdl_search.

    retry_level (rungs from tightest to broadest):
      0 = full query (title + location + company + school + industry)
      1 = simplify title via role-family expansion; keep everything else
      2 = drop title + industry; keep location + company + school
      3 = drop title + industry + location; keep company + school
      4 = DROP company; keep school + role-family expansion (catches the
          "international school × US firm" PDL coverage gap, e.g. Bocconi × Morgan
          Stanley returns 0 but Bocconi alums in IB more broadly returns plenty)
      5 = floor — school only (any reachable alum). Last resort before giving up.

    Company is dropped at level 4+; school filter is the only thing that survives
    to level 5. The user's intent is "find someone from my network" — at the floor
    we ensure they at least see SOMEONE from the school they care about.

    exclude_pdl_ids: optional list of PDL person ids to filter OUT via a must_not
    clause. Used by lazy-topup in search_contacts_from_prompt so broader retry
    attempts return NEW people instead of re-fetching the same names a stricter
    rung already returned.
    """
    must = []

    # ---- Title block ----
    title_variations = parsed_prompt.get("title_variations") or []
    titles = [t.strip().lower() for t in title_variations if t and str(t).strip()]

    if retry_level == 0:
        # Full: match_phrase for every title variation. For single-word tokens,
        # match_phrase is equivalent to a term lookup — so it already covers
        # "engineer" alone. We deliberately do NOT emit a bare
        # {"match": {"job_title": <single-token>}} clause: PDL silently returns
        # 0 hits for plain `match` queries on common single tokens like "data"
        # (confirmed via /tmp/pdl_diagnostic.py Q5a: 0 results despite the
        # match_phrase variant in Q3 returning 6).
        # Multi-word titles use match_phrase ONLY to avoid false positives —
        # e.g. "investment banking analyst" as a plain match would OR-tokenize
        # and match any VP with "investment" or "banking" anywhere in their profile.
        if titles:
            title_clauses = [{"match_phrase": {"job_title": t}} for t in titles]
            title_block = {"bool": {"should": title_clauses}}
            must.append(title_block)
        else:
            must.append({"exists": {"field": "job_title"}})
    elif retry_level == 1:
        # Retry 1 (title broadening rung): expand the role family via match_phrase
        # bool.should. This replaces the old plain `match` on `titles[0]`, which
        # silently returned 0 hits for common single tokens (PDL quirk — see
        # /tmp/pdl_diagnostic.py Q5a). Broadening from ["data scientist"] to
        # {data scientist, data analyst, data engineer, data science manager}
        # catches adjacent roles the user likely still wants to connect with.
        if titles:
            expanded_titles = _expand_titles_for_broadening(titles)
            title_clauses = [{"match_phrase": {"job_title": t}} for t in expanded_titles]
            title_block = {"bool": {"should": title_clauses}}
            must.append(title_block)
            if expanded_titles != [t.strip().lower() for t in titles if t and str(t).strip()]:
                print(
                    f"[build_query_from_prompt] Retry level 1 broadened titles: "
                    f"{titles!r} → {expanded_titles!r}"
                )
        else:
            must.append({"exists": {"field": "job_title"}})
    elif retry_level == 2:
        # Retry 2: seniority-adjacent title expansion — keeps intent but widens to
        # adjacent seniority levels (e.g. analyst → associate, senior analyst)
        if titles:
            adjacent_titles = _expand_titles_seniority_adjacent(titles)
            title_clauses = [{"match_phrase": {"job_title": t}} for t in adjacent_titles]
            title_block = {"bool": {"should": title_clauses}}
            must.append(title_block)
            if adjacent_titles != [t.strip().lower() for t in titles if t and str(t).strip()]:
                print(
                    f"[build_query_from_prompt] Retry level 2 seniority-adjacent titles: "
                    f"{titles!r} → {adjacent_titles!r}"
                )
        else:
            must.append({"exists": {"field": "job_title"}})
    elif retry_level == 3:
        # Retry 3: no title filter (any role at company + school)
        pass
    elif retry_level == 3:
        # Retry 3: no title, no location (handled below)
        pass
    elif retry_level == 4:
        # Retry 4: re-introduce broadened title (school × role family, no company)
        # Use case: international school × US firm where PDL coverage is thin.
        # Bocconi × Morgan Stanley returns 0; Bocconi alumni in IB roles broadly
        # is the right rescue path — keep their role intent, ditch the specific firm.
        if titles:
            expanded_titles = _expand_titles_for_broadening(titles)
            title_clauses = [{"match_phrase": {"job_title": t}} for t in expanded_titles]
            must.append({"bool": {"should": title_clauses}})
    else:
        # Retry 5: school-only floor — any reachable alum
        pass

    # ---- Location block (skip when retry_level >= 4) ----
    if retry_level < 4:
        locations = parsed_prompt.get("locations") or []
        location_str = (locations[0] if locations else "").strip() if locations else ""
        if location_str:
            cleaned_location = clean_location_name(location_str, use_pdl_api=False)
            location_strategy = determine_location_strategy(cleaned_location)
            strategy = location_strategy.get("strategy", "locality_primary")
            metro_location = (location_strategy.get("metro_location") or "").lower()
            city = (location_strategy.get("city") or "").lower()
            state = (location_strategy.get("state") or "").lower()

            if strategy == "country_only":
                location_must = [{"term": {"location_country": "united states"}}]
            else:
                # Flatten metro, city, and state into a single should block —
                # matching ANY of them is sufficient (many PDL profiles have metro but not region)
                location_should = []
                if metro_location:
                    location_should.append({"match": {"location_metro": metro_location}})
                if city:
                    location_should.append({"match": {"location_locality": city}})
                if state:
                    location_should.append({"match": {"location_region": state}})

                location_must = [{"term": {"location_country": "united states"}}]
                if location_should:
                    # PDL doesn't support minimum_should_match — bool.should defaults to "at least one"
                    location_must.append({"bool": {"should": location_should}})
            loc_block = {"bool": {"must": location_must}}
            must.append(loc_block)

    # ---- Company block: dropped at retry_level >= 4 ----
    # The "international school × US firm" PDL coverage gap is the main motivator —
    # at level 4 we try to find ANY alum of the school in the role family (no firm
    # constraint) so users get someone reachable instead of zero results.
    #
    # PDL stores job_company_name under unguessable canonicals (e.g. BCG is
    # "boston consulting group (bcg)", not "bcg"; plain "bcg" matches 776
    # unrelated low-quality records). When we know the firm's domain we route
    # via job_company_website — orders of magnitude more reliable. We do NOT
    # call clean_company_name here: it relies on PDL's Company Enrichment,
    # which returns garbage for acronyms (BCG → NZ IT consultancy).
    companies = parsed_prompt.get("companies") or []
    company_names = [c.get("name", "").strip() for c in companies if isinstance(c, dict) and c.get("name")]
    if retry_level < 4 and company_names:
        company_clauses = []
        for name in company_names:
            n = name.lower().strip()
            if not n:
                continue
            domain = _domain_for_company(name)
            if domain:
                print(f"[build_query_from_prompt] company={name!r} → website={domain} (mapped)")
                company_clauses.append({"match_phrase": {"job_company_website": domain}})
            else:
                print(f"[build_query_from_prompt] company={name!r} not in domain map, name match_phrase")
                # Unmapped: fall back to job_company_name. Multi-word phrases also
                # accept a relaxed match as fallback (catches "Goldman Sachs Group").
                if len(n.split()) == 1:
                    company_clauses.append({"match_phrase": {"job_company_name": n}})
                else:
                    company_clauses.append({
                        "bool": {
                            "should": [
                                {"match_phrase": {"job_company_name": n}},
                                {"match": {"job_company_name": n}},
                            ]
                        }
                    })
        if company_clauses:
            if len(company_clauses) == 1:
                must.append(company_clauses[0])
            else:
                must.append({"bool": {"should": company_clauses}})

    # Schools: flat match_phrase on education.school.name. PDL's ES dialect
    # does NOT support `nested` clauses (returns 400 "Query clause [path] not
    # allowed"), so we use the flat-dotted-field syntax that PDL accepts.
    # Alumni filtering still runs server-side; PDL handles the array semantics
    # internally. post_validation in search_contacts_from_prompt provides the
    # final strict alumni check via _school_aliases.
    schools = parsed_prompt.get("schools") or []
    if schools:
        education_clauses = []
        for school in schools:
            aliases = _school_aliases(school) if isinstance(school, str) else _school_aliases(
                school.get("name") if isinstance(school, dict) else ""
            )
            for a in aliases:
                education_clauses.append({"match_phrase": {"education.school.name": a}})
        if education_clauses:
            must.append({"bool": {"should": education_clauses}})

    # P3 FIX: Add industry filter when specified (e.g. "consulting", "financial services")
    # Drop industry at retry_level >= 1 — PDL taxonomy often doesn't match
    # the prompt parser's industry guesses, and combined with title + location + company
    # it over-constrains the query causing 0 results at level 0
    if retry_level < 1:
        industries = parsed_prompt.get("industries") or []
        if industries:
            industry_clauses = [{"match": {"industry": ind.lower().strip()}} for ind in industries if ind and ind.strip()]
            if industry_clauses:
                if len(industry_clauses) == 1:
                    must.append(industry_clauses[0])
                else:
                    must.append({"bool": {"should": industry_clauses}})

    # P0 FIX: Only return contacts that have email addresses
    must.append({"exists": {"field": "emails"}})

    bool_clause = {"must": must}

    # Lazy-topup support: exclude PDL ids already returned by an earlier (stricter)
    # retry rung so this rung surfaces NEW people instead of paging through the
    # same set. PDL caps `terms` clauses at ~1024 values — well above our use case
    # of <= PDL_BUDGET_CAP per search.
    if exclude_pdl_ids:
        clean_ids = [i for i in exclude_pdl_ids if i]
        if clean_ids:
            bool_clause["must_not"] = [{"terms": {"id": clean_ids}}]

    query_obj = {"bool": bool_clause}
    if retry_level > 0:
        print(f"[build_query_from_prompt] Retry level {retry_level} query:\n{json.dumps(query_obj, indent=2)}")
    else:
        print(f"[build_query_from_prompt] Full Elasticsearch query:\n{json.dumps(query_obj, indent=2)}")
    return query_obj


def _contact_matches_prompt_criteria(contact, parsed_prompt, target_company):
    """
    Post-validation: return True if contact matches user's company and school criteria.
    Returns (matches: bool, drop_reason: str | None).
    """
    name = f"{contact.get('FirstName', '')} {contact.get('LastName', '')}".strip()
    companies = parsed_prompt.get("companies") or []
    schools = parsed_prompt.get("schools") or []

    # Verbose debug: what we're checking
    contact_company = (contact.get("Company") or "").strip()
    is_current = contact.get("IsCurrentlyAtTarget", False)
    college = (contact.get("College") or "").strip()
    education_top = (contact.get("EducationTop") or "").strip()
    first_job = (contact.get("experience") or [None])[0] if contact.get("experience") else None
    if first_job and not isinstance(first_job, dict):
        first_job = None
    first_job_company = ((first_job.get("company", {}) or {}).get("name") or first_job.get("company_name") or "") if first_job else ""
    print(f"[PostFilter] Checking contact: {name}")
    print(f"[PostFilter] Target companies: {companies!r}, Contact company: {contact_company!r}, IsCurrentlyAtTarget: {is_current}, first_job_company: {first_job_company!r}")
    print(f"[PostFilter] Target schools: {schools!r}, Contact College: {college!r}, EducationTop: {education_top!r}")

    # Company check: if user specified a company, contact must be currently at target company.
    # For mapped firms the upstream PDL query already filtered by exact job_company_website,
    # so the name-based comparison would only produce false negatives (e.g. "MS" vs
    # "morgan stanley" fails the substring check). Skip name comparison for mapped firms.
    if companies and target_company:
        if not is_current:
            print(f"[PostFilter] Result for {name}: FAIL — not currently at target company (expected={target_company})")
            return False, "not_currently_at_target"
        if _domain_for_company(target_company):
            print(f"[PostFilter] Result for {name}: company check via website domain (mapped firm)")
        else:
            actual = contact_company
            if not actual:
                print(f"[PostFilter] Result for {name}: FAIL — company mismatch (expected={target_company}, got=no company)")
                return False, "company_mismatch"
            cleaned_expected = clean_company_name(target_company).lower().strip()
            cleaned_actual = clean_company_name(actual).lower().strip()
            if (cleaned_expected != cleaned_actual
                    and not (cleaned_expected in cleaned_actual and len(cleaned_expected) >= 3)
                    and not (cleaned_actual in cleaned_expected and len(cleaned_actual) >= 3)):
                print(f"[PostFilter] Result for {name}: FAIL — company mismatch (expected={target_company}, got={actual})")
                return False, "company_mismatch"

    # School check: if user specified schools, contact must have at least one matching school.
    # Delegate to contact_matches_school (strictness="loose") which uses word-boundary matching
    # (_school_name_matches) and the geographic-qualifier guard. Stage 4's match_phrase already
    # did the strict filter, so this is a post-fetch sanity check — "loose" is appropriate.
    if schools:
        alias_set = []
        for school in schools:
            alias_set.extend(_school_aliases(school))
        if not alias_set:
            print(f"[PostFilter] Result for {name}: PASS (no school aliases to check)")
            return True, None
        if not contact_matches_school(contact, alias_set, strictness="loose"):
            print(f"[PostFilter] Result for {name}: FAIL — school mismatch (expected one of {schools}, got College={contact.get('College')})")
            return False, "school_mismatch"
        # City-name guard: if the only evidence is a College field that is JUST a generic
        # city-level alias (e.g. College="New York" for school="New York University"), reject.
        # This preserves the distinction between city-name false positives and actual schools.
        _SCHOOL_WORDS = {"university", "college", "school", "institute", "academy"}
        college_lower = college.lower().strip()
        education_top_lower = education_top.lower().strip()
        edu = contact.get("education") or []
        has_structured_edu = isinstance(edu, list) and any(
            isinstance(e, dict) and ((e.get("school") or {}).get("name") if isinstance(e.get("school"), dict) else e.get("school"))
            for e in edu
        )
        has_school_word_in_college = any(sw in college_lower for sw in _SCHOOL_WORDS)
        has_school_word_in_edu_top = any(sw in education_top_lower for sw in _SCHOOL_WORDS)
        if college_lower and not has_school_word_in_college and not has_school_word_in_edu_top and not has_structured_edu:
            # Check if any aliases that could match the College field are "generic" — no school word and >5 chars.
            # Short aliases (≤5 chars like "usc", "mit", "nyu") are institutional acronyms and are safe.
            def _is_generic_alias(a: str) -> bool:
                al = a.lower().strip()
                if not al or len(al) <= 5:
                    return False
                return not any(sw in al for sw in _SCHOOL_WORDS)
            generic_matching = any(
                _is_generic_alias(a) and a.lower().strip() == college_lower
                for a in alias_set
            )
            if generic_matching:
                print(f"[PostFilter] Result for {name}: FAIL — school mismatch (College={college!r} is a generic/city-name alias without school keyword)")
                return False, "school_mismatch"

    print(f"[PostFilter] Result for {name}: PASS")
    return True, None


_MAJOR_ROLE_AFFINITY = {
    # Finance / Economics
    "finance": {"analyst", "associate", "banking", "finance", "trader", "financial", "investment", "advisory", "wealth"},
    "economics": {"analyst", "associate", "banking", "finance", "economist", "research", "advisory", "investment"},
    "accounting": {"accountant", "auditor", "tax", "accounting", "finance", "analyst", "advisory"},
    # Tech / Engineering
    "computer science": {"engineer", "developer", "sde", "software", "data", "ml", "machine learning", "devops", "infrastructure", "platform"},
    "computer engineering": {"engineer", "developer", "sde", "software", "hardware", "firmware", "embedded"},
    "electrical engineering": {"engineer", "hardware", "firmware", "embedded", "electrical"},
    "information systems": {"engineer", "developer", "analyst", "data", "it", "technology", "systems"},
    "data science": {"data", "scientist", "analyst", "ml", "machine learning", "research", "analytics"},
    # Business / Consulting
    "business": {"consultant", "manager", "strategy", "operations", "analyst", "associate", "business", "management"},
    "management": {"consultant", "manager", "strategy", "operations", "analyst", "associate", "management"},
    "business administration": {"consultant", "manager", "strategy", "operations", "analyst", "associate", "management"},
    # Marketing / Communications
    "marketing": {"marketing", "brand", "content", "communications", "social", "growth", "product"},
    "communications": {"communications", "marketing", "pr", "content", "media", "brand"},
    # Other
    "mathematics": {"analyst", "quant", "data", "research", "engineer", "quantitative", "actuary"},
    "statistics": {"analyst", "data", "scientist", "research", "statistician", "quantitative"},
    "psychology": {"research", "hr", "people", "ux", "product", "design", "behavioral"},
    "political science": {"policy", "analyst", "government", "consulting", "law", "public"},
}


def _compute_profile_rank_score(contact: dict, user_profile: dict | None) -> int:
    """
    Compute a soft ranking score for a contact based on the searching student's profile.
    Higher score = more relevant to this specific student.
    This is a SECONDARY sort — the user's explicit search query is always the primary filter.
    """
    if not user_profile:
        return 0

    score = 0
    academics = user_profile.get("academics") or {}
    professional_info = user_profile.get("professionalInfo") or {}
    resume_parsed = user_profile.get("resumeParsed") or {}

    # --- Location proximity ---
    raw_location = (
        user_profile.get("location")
        or user_profile.get("hometown")
        or professional_info.get("location")
        or ""
    )
    # location may be a dict (e.g. {"city": "LA", "state": "CA"}) or a string
    if isinstance(raw_location, dict):
        user_location = f"{raw_location.get('city', '')} {raw_location.get('state', '')}".lower().strip()
    else:
        user_location = str(raw_location).lower().strip()

    if user_location:
        contact_city = (contact.get("City") or "").lower().strip()
        contact_state = (contact.get("State") or "").lower().strip()
        contact_location = f"{contact_city} {contact_state}".strip()

        # Tokenize user location for flexible matching
        user_loc_parts = {p.strip() for p in user_location.replace(",", " ").split() if len(p.strip()) > 2}

        if contact_city and contact_city in user_location:
            score += 3  # Same city
        elif any(p in contact_location for p in user_loc_parts if len(p) > 3):
            score += 2  # Partial location match (e.g. same state)

    # --- Major/field → role affinity ---
    resume_education = resume_parsed.get("education") or {}
    if not isinstance(resume_education, dict):
        resume_education = {}
    user_major = (
        academics.get("major")
        or resume_education.get("major")
        or ""
    )
    if isinstance(user_major, str):
        user_major = user_major.lower().strip()
    else:
        user_major = ""

    if user_major:
        contact_title = (contact.get("Title") or "").lower()
        # Find best matching affinity set
        best_affinity = set()
        for major_key, keywords in _MAJOR_ROLE_AFFINITY.items():
            if major_key in user_major or user_major in major_key:
                best_affinity = keywords
                break

        if best_affinity and contact_title:
            title_words = set(contact_title.split())
            if title_words & best_affinity:
                score += 3  # Title aligns with student's major

    # --- LinkedIn presence (minor boost) ---
    if (contact.get("LinkedIn") or "").strip():
        score += 1

    return score


def search_contacts_from_prompt(parsed_prompt: dict, max_contacts: int, exclude_keys=None, user_profile=None):
    """
    Run PDL person search from structured prompt output. Reuses execute_pdl_search,
    applies exclusion filtering and post-validation (company/school match), returns contacts.
    Post-validation runs AFTER execute_pdl_search and BEFORE contacts are returned (so before emails/drafts).
    """
    print(f"[PostFilter] search_contacts_from_prompt called (max_contacts={max_contacts}, parsed keys={list(parsed_prompt.keys())})")
    exclude_keys = exclude_keys or set()

    # Industry-aware semantic expansion: broaden `industries` to related PDL
    # taxonomy entries and add aligned title_variations. Runs BEFORE cache
    # lookup so expanded queries get their own cache entries. Skipped when:
    #   - parsed_prompt has no industries, OR
    #   - a specific company was named (e.g. "USC Data Scientist IBM"): the
    #     company filter already constrains the search to that firm, so
    #     broadening industry would only add noise. Expansion exists for
    #     school+industry prompts WITHOUT a company target.
    if (
        ENABLE_INDUSTRY_EXPANSION
        and parsed_prompt.get("industries")
        and not parsed_prompt.get("companies")
    ):
        parsed_prompt = expand_industries_and_titles(parsed_prompt)

    # ---- Cache check (Firestore, 30-day TTL) -------------------------------
    # Same query within 30 days = 0 PDL credits. Per-user exclude_keys is
    # applied AFTER read so one cache entry serves many users.
    try:
        from app.services import pdl_cache
        cached = pdl_cache.get(parsed_prompt, max_contacts)
        if cached and cached.get("results"):
            cached_results = pdl_cache.filter_excluded(cached["results"], exclude_keys)
            if cached_results:
                print(f"[PDL Cache] HIT — returning {len(cached_results)} cached contacts (0 credits)")
                # JIT firm-employee cache: also write to person-level cache on
                # query-cache HITs. This backfills firm_employees from historical
                # pdl_search_cache entries — otherwise the person cache never
                # captures anyone whose query hash matches an existing entry.
                # Cheap: Firestore upsert is idempotent, only touches last_seen_at
                # if the doc already exists.
                try:
                    from app.services.firm_cache import cache_pdl_contacts
                    from app.services.firm_cache.writer import _flag_enabled
                    _flag = _flag_enabled()
                    _in = len(cached["results"])
                    n = cache_pdl_contacts(cached["results"], shape="app")
                    print(f"[FirmCache] cache-hit path: flag={_flag} in={_in} queued={n}")
                except Exception as e:
                    print(f"[FirmCache] cache-hit write failed (non-fatal): {e}")
                return (
                    cached_results[:max_contacts],
                    int(cached.get("retry_level_used") or 0),
                    [],
                    cached.get("adjacency_metadata"),
                )
            else:
                print(f"[PDL Cache] HIT but all {len(cached['results'])} cached contacts excluded — falling through to PDL")
    except Exception as e:
        print(f"[PDL Cache] lookup failed (continuing to PDL): {e}")
    target_company = None
    companies = parsed_prompt.get("companies") or []
    if companies and isinstance(companies[0], dict):
        target_company = (companies[0].get("name") or "").strip() or None

    PDL_URL = f"{PDL_BASE_URL}/person/search"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Api-Key": PEOPLE_DATA_LABS_API_KEY,
    }
    # CREDIT-EFFICIENCY FIX (2026-05): PDL Person Search charges 1 credit
    # PER RECORD returned. Buffer scales gently with max_contacts so small
    # batches don't waste credits. Two reasons we over-fetch (need both):
    #   (1) Dedup against the user's exclude_keys (saved contacts).
    #   (2) Hard-filter for verified emails — some PDL candidates fall to
    #       pattern-synth / hunter_finder_risky and get dropped by the
    #       email confidence filter below.
    # At batchSize=1, the previous flat `min(exclude_count, 3)` was a 300%
    # over-fetch (4 PDL credits for 1 returned contact). Scaled buffer keeps
    # overhead bounded: mc=1 -> +1 (50% savings), mc=8 -> +2, mc=15 -> +3.
    exclude_count = len(exclude_keys or set())
    base_buffer = max(1, max_contacts // 4)
    dedup_buffer = min(exclude_count, base_buffer)
    buffer = max(base_buffer, dedup_buffer)
    fetch_limit = int(max_contacts + buffer)
    page_size = min(100, max(1, fetch_limit))

    raw_contacts = []
    status_code = 200
    retry_level_used = 0
    filtered = []
    already_saved = []
    already_saved_keys = set()
    post_filter_dropped = 0
    drop_reason_counts = {}  # Track why contacts were dropped across all attempts

    # Lazy-topup state (Phase 3 of email deliverability plan). Accumulate
    # filtered contacts ACROSS retry attempts instead of replacing the set
    # each rung. Break only when verified count meets the requested max OR
    # the PDL budget cap is reached, whichever comes first. Pre-topup
    # behavior was: any new contacts at a rung → stop. That silently dropped
    # 3 of 5 PDL records on hard searches (paid for, filtered out, not
    # returned). See docs/EMAIL_DELIVERABILITY_PLAN.md.
    cumulative_filtered: list = []
    cumulative_seen_keys: set = set()
    cumulative_seen_pdl_ids: list = []  # ordered for stable must_not clauses
    records_fetched_total = 0
    PDL_BUDGET_CAP_MULTIPLIER = 2.0
    pdl_budget_cap = int(max_contacts * PDL_BUDGET_CAP_MULTIPLIER) + buffer
    topup_triggered = False

    companies_from_prompt = parsed_prompt.get("companies") or []
    schools_from_prompt = parsed_prompt.get("schools") or []

    # Hard wall-time cap on the entire retry chain. PDL roundtrips run ~1.5-2.5s
    # each in practice, so 4s only fit 2 rungs and starved the most-useful broad
    # rungs (level 4 = drop company; level 5 = school floor). 9s lets the chain
    # reach level 4 reliably without making failed searches feel hung. Level 0
    # always runs; subsequent rungs only fire if budget remains.
    RETRY_WALL_TIME_BUDGET_SEC = 9.0
    retry_start_time = time.time()

    # Retry chain capped at level 3 (relax title + location, KEEP company).
    # Levels 4–5 drop the company filter, which combined with the email
    # extractor's target-company override would silently fabricate
    # `@target_company.com` emails for people who don't work there
    # (the Verkada-employee-emailed-as-@apple.com bug). Re-enable 4–5 only
    # after the extractor knows not to synthesize emails when company is
    # dropped from the query.
    for attempt in range(4):
        if attempt >= 1:
            elapsed = time.time() - retry_start_time
            if elapsed > RETRY_WALL_TIME_BUDGET_SEC:
                print(f"[PDL Retry] Wall-time cap hit at attempt {attempt} (elapsed={elapsed:.2f}s > budget={RETRY_WALL_TIME_BUDGET_SEC}s) — stopping retry chain")
                break
            if attempt == 1:
                print(f"[PDL Retry] Attempt 1: broaden title via role-family match_phrase expansion; keep company + school + location")
            elif attempt == 2:
                print(f"[PDL Retry] Attempt 2: drop title + industry filters (keep company + school + location)")
            elif attempt == 3:
                print(f"[PDL Retry] Attempt 3: drop title + industry + location (keep company + school only)")

            # Lazy-topup telemetry: mark only ONCE, on the first re-entry past
            # level 0. Tracks "did we have to broaden to top up verified count?"
            if not topup_triggered:
                topup_triggered = True
                try:
                    from app.utils.metrics_events import log_event
                    log_event(None, "pdl_topup_triggered", {
                        "max_contacts": max_contacts,
                        "verified_at_level_0": sum(
                            1 for c in cumulative_filtered
                            if c.get("EmailSource") in HIGH_CONFIDENCE_EMAIL_SOURCES
                        ),
                        "cumulative_at_level_0": len(cumulative_filtered),
                    })
                except Exception:
                    pass

        query_obj = build_query_from_prompt(
            parsed_prompt,
            retry_level=attempt,
            exclude_pdl_ids=cumulative_seen_pdl_ids,
        )
        raw_contacts, status_code = execute_pdl_search(
            headers=headers,
            url=PDL_URL,
            query_obj=query_obj,
            desired_limit=fetch_limit,
            search_type="prompt_search",
            page_size=page_size,
            verbose=False,
            target_company=target_company,
        )
        records_fetched_total += len(raw_contacts or [])

        if not raw_contacts:
            continue

        print(f"[PostFilter] Running post-validation on {len(raw_contacts)} contacts at retry_level={attempt} (parsed companies={companies_from_prompt!r}, schools={schools_from_prompt!r}, target_company={target_company!r})")

        attempt_filtered = []
        attempt_dropped = 0
        # At retry levels 4+ we intentionally dropped the company filter from
        # the query, so post-validation can't require a company match either —
        # otherwise everything would get dropped. Strip the company expectation
        # when evaluating contacts at those broader rungs.
        effective_parsed = parsed_prompt
        effective_target_company = target_company
        if attempt >= 4:
            effective_parsed = {**parsed_prompt, "companies": []}
            effective_target_company = None
        for contact in raw_contacts:
            key = get_contact_identity(contact)
            matches, drop_reason = _contact_matches_prompt_criteria(contact, effective_parsed, effective_target_company)
            if not matches:
                attempt_dropped += 1
                if drop_reason:
                    drop_reason_counts[drop_reason] = drop_reason_counts.get(drop_reason, 0) + 1
                continue
            if key in exclude_keys:
                if key not in already_saved_keys:
                    already_saved.append(contact)
                    already_saved_keys.add(key)
                continue
            attempt_filtered.append(contact)

        # Merge into cumulative (dedup by identity key), update pdlId exclusion list.
        for c in attempt_filtered:
            key = get_contact_identity(c)
            if key in cumulative_seen_keys:
                continue
            cumulative_seen_keys.add(key)
            cumulative_filtered.append(c)
            pid = c.get("pdlId")
            if pid:
                cumulative_seen_pdl_ids.append(pid)

        if attempt_filtered:
            retry_level_used = attempt
            post_filter_dropped += attempt_dropped
            if already_saved:
                print(f"🔍 Prompt search filtering: raw={len(raw_contacts)}, already_saved={len(already_saved)}, new_this_rung={len(attempt_filtered)}, cumulative={len(cumulative_filtered)} (attempt {attempt})")
            if attempt_dropped > 0:
                print(f"[PostFilter] Kept {len(attempt_filtered)} contacts at attempt {attempt} (dropped {attempt_dropped} non-matching)")

        # Break condition: enough verified contacts cumulated.
        verified_count = sum(
            1 for c in cumulative_filtered
            if c.get("EmailSource") in HIGH_CONFIDENCE_EMAIL_SOURCES
        )
        if verified_count >= max_contacts:
            print(f"[PDL Retry] Verified target met at attempt {attempt}: verified={verified_count} >= max_contacts={max_contacts} (records_fetched={records_fetched_total})")
            break

        # Budget cap: stop spending PDL credits past the configured ceiling.
        if records_fetched_total >= pdl_budget_cap:
            print(f"[PDL Retry] Budget cap hit at attempt {attempt}: records_fetched={records_fetched_total} >= cap={pdl_budget_cap}")
            try:
                from app.utils.metrics_events import log_event
                log_event(None, "pdl_budget_cap_hit", {
                    "records_fetched": records_fetched_total,
                    "budget_cap": pdl_budget_cap,
                    "verified_count": verified_count,
                    "max_contacts": max_contacts,
                })
            except Exception:
                pass
            break

        if not attempt_filtered:
            # No NEW contacts on this rung — broaden further.
            print(f"[PDL Retry] Attempt {attempt}: raw={len(raw_contacts)}, already_saved_cumulative={len(already_saved)}, new=0 — trying next rung")

    filtered = cumulative_filtered

    if topup_triggered:
        try:
            from app.utils.metrics_events import log_event
            log_event(None, "pdl_topup_records_fetched", {
                "records_fetched_total": records_fetched_total,
                "retry_level_reached": retry_level_used,
                "verified_count_final": sum(
                    1 for c in cumulative_filtered
                    if c.get("EmailSource") in HIGH_CONFIDENCE_EMAIL_SOURCES
                ),
                "cumulative_count_final": len(cumulative_filtered),
            })
        except Exception:
            pass

    # Build adjacency metadata to explain what happened
    adjacency_metadata = None
    if not filtered or drop_reason_counts:
        adjacency_metadata = {
            "drop_reasons": drop_reason_counts,
            "broadened": retry_level_used > 0,
            "broadening_level": retry_level_used,
        }
        # Build a human-readable message based on drop reasons
        if drop_reason_counts:
            total_dropped = sum(drop_reason_counts.values())
            top_reason = max(drop_reason_counts, key=drop_reason_counts.get)
            if top_reason == "not_currently_at_target" and target_company:
                adjacency_metadata["message"] = f"Found {total_dropped} people who previously worked at {target_company} but have since moved on. Try searching without the company filter or look for recruiters at {target_company} instead."
            elif top_reason == "company_mismatch" and target_company:
                adjacency_metadata["message"] = f"Found people in similar roles but not at {target_company}. Try broadening to related companies."
            elif top_reason == "school_mismatch":
                school_names = [s.get("name", s) if isinstance(s, dict) else s for s in schools_from_prompt[:2]]
                adjacency_metadata["message"] = f"Found {total_dropped} people matching your role/company criteria but from different schools. Try removing the school filter."
            else:
                adjacency_metadata["message"] = f"Found {total_dropped} people in the area but they didn't match all your criteria. Try simplifying your search."

    if not filtered and not already_saved:
        return [], 0, [], adjacency_metadata

    # ---- Cache write (best-effort) -----------------------------------------
    # Cache the FULL filtered set (pre-trim, pre-user-exclude) so the cache
    # is reusable across users. Excluded/already_saved contacts are not cached
    # (they're per-user state).
    try:
        from app.services import pdl_cache
        if filtered:
            pdl_cache.put(
                parsed_prompt,
                max_contacts,
                results=filtered,
                retry_level_used=retry_level_used,
                adjacency_metadata=adjacency_metadata,
            )
    except Exception as e:
        print(f"[PDL Cache] set failed (non-fatal): {e}")

    # ---- JIT firm-employee cache write (best-effort) -----------------------
    # Person-level cache complementing pdl_search_cache (query-level). Every
    # PDL search result gets upserted into firm_employees/{linkedin_id}, so
    # future searches that share people (different school / title / location
    # combinations against the same person) can be served without PDL.
    # Off by default via ENABLE_FIRM_CACHE_WRITE. Fires on a daemon thread —
    # no user-visible latency, never breaks the search.
    try:
        from app.services.firm_cache import cache_pdl_contacts
        from app.services.firm_cache.writer import _flag_enabled
        if filtered:
            _flag = _flag_enabled()
            n = cache_pdl_contacts(filtered, shape="app")
            print(f"[FirmCache] fresh-PDL path: flag={_flag} in={len(filtered)} queued={n}")
            # If flag is on but nothing queued, dump the first contact's key
            # fields so we can see why the normalizer rejected everything.
            if _flag and n == 0 and filtered:
                c = filtered[0]
                print(f"[FirmCache] sample dropped contact: "
                      f"LinkedIn={bool(c.get('LinkedIn'))} "
                      f"Company={c.get('Company')!r} "
                      f"State={c.get('State')!r} "
                      f"City={c.get('City')!r}")
    except Exception as e:
        print(f"[FirmCache] fresh-PDL write failed (non-fatal): {e}")

    # Compute profile-affinity scores ONCE for every candidate so they
    # actually participate in the sort below. Previously this was computed
    # AFTER the trim, which meant the sort key was effectively (0, linkedin).
    if user_profile:
        for c in filtered:
            c["_profile_rank"] = _compute_profile_rank_score(c, user_profile)
    else:
        for c in filtered:
            c["_profile_rank"] = 0

    # Composite sort: email confidence FIRST (the user's complaint — they want
    # verified emails to win when max_contacts < len(candidates)), then profile
    # affinity, then LinkedIn presence as a tiebreaker.
    def _sort_key(c):
        return (
            -_email_quality_score(c),
            -int(c.get("_profile_rank") or 0),
            0 if (c.get("LinkedIn") or "").strip() else 1,
        )

    filtered.sort(key=_sort_key)

    # Hard filter: when we have ANY verified-email contacts, return ONLY those.
    # Better to return fewer real emails than fill the list with best-guesses.
    # If we have zero verified, fall back to the full ranked list so the user
    # isn't shown an empty page — but flag email_quality='low' so the frontend
    # can warn them to verify before sending.
    adjacency_metadata = adjacency_metadata or {}
    verified_contacts = [c for c in filtered if c.get("EmailSource") in HIGH_CONFIDENCE_EMAIL_SOURCES]
    if verified_contacts:
        return_set = verified_contacts[:max_contacts]
        adjacency_metadata["email_quality"] = "high"
        print(
            f"[EmailQuality] {len(verified_contacts)}/{len(filtered)} candidates have verified emails — "
            f"returning {len(return_set)} verified."
        )
    else:
        return_set = filtered[:max_contacts]
        adjacency_metadata["email_quality"] = "low"
        adjacency_metadata.setdefault(
            "message",
            f"Found {len(filtered)} matching alumni but none have verified emails. "
            f"Showing best-guess emails — please verify before sending."
        )
        print(
            f"[EmailQuality] 0/{len(filtered)} candidates have verified emails — "
            f"returning {len(return_set)} best-guess contacts with low-quality flag."
        )

    # Debug visibility into the final ordering by email source
    if return_set:
        top_sources = [
            (c.get("EmailSource"), bool(c.get("EmailVerified")), int(c.get("EmailConfidenceScore") or 0))
            for c in return_set[:5]
        ]
        print(f"[EmailQuality] Top-{len(top_sources)} (source, verified, score): {top_sources}")

    # Clean up internal field before returning to the caller
    for c in filtered:
        c.pop("_profile_rank", None)

    return return_set, retry_level_used, already_saved, (adjacency_metadata or None)


def build_coffee_chat_data(pdl_person: dict, best_email: str) -> dict:
    """
    Build the full coffee_chat_data dict from a raw PDL person response.
    Pass through ALL useful fields — do not flatten or discard.
    """
    first = pdl_person.get("first_name", "") or ""
    last  = pdl_person.get("last_name", "")  or ""

    # ── Social profiles ──────────────────────────────────────────
    github_url  = ""
    twitter_url = ""
    linkedin_url = ""
    for profile in pdl_person.get("profiles", []):
        network = (profile.get("network") or "").lower()
        url     = profile.get("url") or profile.get("clean_url") or ""
        if network == "github":   github_url   = url
        if network == "twitter":  twitter_url  = url
        if network == "linkedin": linkedin_url = url

    # ── Experience array ─────────────────────────────────────────
    experience_array = []
    for exp in pdl_person.get("experience", []):
        start = exp.get("start_date") or ""
        end   = exp.get("end_date")   or ""
        is_current = exp.get("is_primary", False) or not end
        title_obj   = exp.get("title")   or {}
        company_obj = exp.get("company") or {}
        experience_array.append({
            "title":          title_obj.get("name", "") if isinstance(title_obj, dict) else str(title_obj),
            "company":        company_obj.get("name", "") if isinstance(company_obj, dict) else str(company_obj),
            "start_date":     start,
            "end_date":       end,
            "is_current":     is_current,
            "location_names": exp.get("location_names", []),
            "summary":        exp.get("summary", ""),
        })

    # Current role from first experience entry
    current_exp     = experience_array[0] if experience_array else {}
    current_title   = current_exp.get("title", "")
    current_company = current_exp.get("company", "") or pdl_person.get("job_company_name", "")

    # ── Education array ───────────────────────────────────────────
    education_array = []
    for edu in pdl_person.get("education", []):
        school_obj = edu.get("school") or {}
        education_array.append({
            "school":     school_obj.get("name", "") if isinstance(school_obj, dict) else str(school_obj),
            "degree":     edu.get("degrees", [""])[0] if edu.get("degrees") else "",
            "major":      edu.get("majors",  [""])[0] if edu.get("majors")  else "",
            "start_date": edu.get("start_date", ""),
            "end_date":   edu.get("end_date",   ""),
            "gpa":        edu.get("gpa"),
        })

    # ── Skills (ACTUALLY EXTRACT THESE) ─────────────────────────
    skills = []
    for s in pdl_person.get("skills", []):
        name = s.get("name") if isinstance(s, dict) else str(s)
        if name: skills.append(name)

    # ── Interests (REMOVE THE HARDCODED []) ─────────────────────
    interests = pdl_person.get("interests", [])
    if isinstance(interests, list):
        interests = [i for i in interests if i]

    # ── Certifications ────────────────────────────────────────────
    certifications = []
    for cert in pdl_person.get("certifications", []):
        if isinstance(cert, dict) and cert.get("name"):
            certifications.append({
                "name":       cert.get("name", ""),
                "start_date": cert.get("start_date", ""),
                "end_date":   cert.get("end_date", ""),
            })

    # ── Industry (USE REAL PDL FIELD, not fabricated) ───────────
    industry = (
        pdl_person.get("job_company_industry")
        or pdl_person.get("industry")
        or ""
    )

    loc = pdl_person.get("location") or {}
    city    = loc.get("locality") or pdl_person.get("location_locality") or ""
    state   = loc.get("region")   or pdl_person.get("location_region")   or ""
    country = loc.get("country")  or pdl_person.get("location_country")  or ""
    # PDL can return booleans for location fields — coerce to string, skip booleans
    city    = str(city) if city and not isinstance(city, bool) else ""
    state   = str(state) if state and not isinstance(state, bool) else ""
    country = str(country) if country and not isinstance(country, bool) else ""
    location_display = ", ".join(filter(None, [city, state, country]))

    return {
        # ── Identity
        "firstName":             first,
        "lastName":              last,
        "fullName":              f"{first} {last}".strip(),
        "email":                 best_email,
        "linkedinUrl":           linkedin_url,
        "githubUrl":             github_url,
        "twitterUrl":            twitter_url,

        # ── Current role
        "jobTitle":              current_title,
        "company":               current_company,
        "industry":              industry,
        "jobCompanySize":        pdl_person.get("job_company_size", ""),
        "jobCompanyFounded":     pdl_person.get("job_company_founded", ""),
        "jobCompanyLinkedinUrl": pdl_person.get("job_company_linkedin_url", ""),

        # ── Location
        "city":                  city,
        "state":                 state,
        "country":               country,
        "location":              location_display,

        # ── Rich arrays (NOT flattened)
        "experienceArray":       experience_array,
        "educationArray":        education_array,

        # ── Skills / interests (ACTUALLY POPULATED)
        "skills":                skills,
        "interests":             interests,
        "certifications":        certifications,
        "languages":             pdl_person.get("languages", []),

        # ── PDL summary (LinkedIn about section)
        "summary":               pdl_person.get("summary", ""),
        "yearsExperience":       pdl_person.get("inferred_years_experience"),
        "linkedinConnections":   pdl_person.get("linkedin_connections"),

        # ── Legacy flat strings for backward compat
        "workExperience": [
            f"{experience_array[0]['title']} at {experience_array[0]['company']}"
            if experience_array else ""
        ],
        "education": (
            f"{education_array[0]['degree']} at {education_array[0]['school']}"
            if education_array else ""
        ),
    }


def _apify_date_to_str(value) -> str:
    """Apify profile actors return dates as either ISO strings or {month, year}
    objects. Normalize to a YYYY-MM (or YYYY) string so the rest of the prep
    pipeline can treat PDL and Apify-derived dates the same way.
    """
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        year = value.get("year")
        month = value.get("month")
        if year and month:
            return f"{year}-{int(month):02d}"
        if year:
            return str(year)
    return ""


def build_coffee_chat_data_from_apify(apify_item: dict) -> dict:
    """Convert a `harvestapi/linkedin-profile-scraper` dataset item into the
    same coffee_chat_data shape produced by build_coffee_chat_data(). Used as
    a PDL fallback when PDL has no record of the profile (common for student
    / private accounts).

    Apify field names vary slightly by actor revision, so we try a few common
    variants per field and let unknowns fall through to empty rather than
    failing the whole prep.
    """
    item = apify_item or {}

    first = item.get("firstName") or ""
    last = item.get("lastName") or ""
    if not (first or last) and item.get("name"):
        parts = str(item["name"]).strip().split(" ", 1)
        first = parts[0]
        last = parts[1] if len(parts) > 1 else ""

    location_obj = item.get("location") or {}
    if isinstance(location_obj, str):
        city = ""
        country = ""
        location_display = location_obj
    else:
        city = location_obj.get("city") or location_obj.get("locality") or ""
        country = location_obj.get("country") or ""
        location_display = (
            location_obj.get("full")
            or location_obj.get("name")
            or ", ".join(filter(None, [city, location_obj.get("region", ""), country]))
        )

    current = item.get("currentPosition") or {}
    current_title = current.get("title") or item.get("headline") or ""
    current_company = current.get("companyName") or current.get("company") or ""

    experience_array = []
    for exp in item.get("experience", []) or []:
        if not isinstance(exp, dict):
            continue
        title = exp.get("title") or ""
        company = (
            exp.get("companyName")
            or exp.get("company")
            or (exp.get("companyObject") or {}).get("name")
            or ""
        )
        start = _apify_date_to_str(exp.get("startDate") or exp.get("start_date"))
        end = _apify_date_to_str(exp.get("endDate") or exp.get("end_date"))
        loc = exp.get("location") or ""
        experience_array.append({
            "title": title,
            "company": company,
            "start_date": start,
            "end_date": end,
            "is_current": not end,
            "location_names": [loc] if loc else [],
            "summary": exp.get("description") or exp.get("summary") or "",
        })

    if not current_title and experience_array:
        current_title = experience_array[0]["title"]
    if not current_company and experience_array:
        current_company = experience_array[0]["company"]

    education_array = []
    for edu in item.get("education", []) or []:
        if not isinstance(edu, dict):
            continue
        education_array.append({
            "school": edu.get("schoolName") or edu.get("school") or "",
            "degree": edu.get("degreeName") or edu.get("degree") or "",
            "major": edu.get("fieldOfStudy") or edu.get("major") or "",
            "start_date": _apify_date_to_str(edu.get("startDate") or edu.get("start_date")),
            "end_date": _apify_date_to_str(edu.get("endDate") or edu.get("end_date")),
            "gpa": edu.get("gpa"),
        })

    skills_obj = item.get("skills")
    if isinstance(skills_obj, dict):
        skills = skills_obj.get("allSkills") or skills_obj.get("topSkills") or []
    elif isinstance(skills_obj, list):
        skills = [s if isinstance(s, str) else (s.get("name") if isinstance(s, dict) else "") for s in skills_obj]
    else:
        skills = []
    skills = [s for s in skills if s]

    emails = item.get("emails") or []
    best_email = emails[0] if emails and isinstance(emails[0], str) else (item.get("email") or "")

    linkedin_url = item.get("profileUrl") or item.get("linkedinUrl") or ""

    return {
        "firstName": first,
        "lastName": last,
        "fullName": (item.get("name") or f"{first} {last}").strip(),
        "email": best_email,
        "linkedinUrl": linkedin_url,
        "githubUrl": "",
        "twitterUrl": "",

        "jobTitle": current_title,
        "company": current_company,
        "industry": item.get("industry") or "",
        "jobCompanySize": "",
        "jobCompanyFounded": "",
        "jobCompanyLinkedinUrl": current.get("companyUrl") or "",

        "city": city,
        "state": location_obj.get("region", "") if isinstance(location_obj, dict) else "",
        "country": country,
        "location": location_display,

        "experienceArray": experience_array,
        "educationArray": education_array,

        "skills": skills,
        "interests": [],
        "certifications": item.get("certifications") or [],
        "languages": item.get("languages") or [],

        "summary": item.get("summary") or item.get("about") or item.get("headline") or "",
        "yearsExperience": None,
        "linkedinConnections": item.get("connectionsCount") or item.get("followersCount"),

        "workExperience": [
            f"{experience_array[0]['title']} at {experience_array[0]['company']}"
            if experience_array else ""
        ],
        "education": (
            f"{education_array[0]['degree']} at {education_array[0]['school']}"
            if education_array else ""
        ),
    }


@meter_call("pdl", "person_enrich")
def enrich_by_name(first_name: str, last_name: str, company: str, min_likelihood: int = 4):
    """Look up a single person via /v5/person/enrich by name + company.

    Used by recruiter_finder when we know exactly who we want (e.g. a name
    extracted from a job posting). Cheaper than /person/search — 1 credit
    per HTTP 200, free on 404 misses.

    Returns the raw PDL person dict on HTTP 200, or None on 404 / no match.
    """
    if not PEOPLE_DATA_LABS_API_KEY:
        return None
    try:
        resp = requests.get(
            f"{PDL_BASE_URL}/person/enrich",
            params={
                "api_key": PEOPLE_DATA_LABS_API_KEY,
                "first_name": first_name,
                "last_name": last_name,
                "company": company,
                "min_likelihood": min_likelihood,
                "pretty": False,
            },
            timeout=20,
        )
        if resp.status_code == 200:
            body = resp.json() or {}
            if body.get("status") == 200 and body.get("data"):
                return body["data"]
        return None
    except Exception as e:
        print(f"[enrich_by_name] PDL enrich failed for {first_name} {last_name} @ {company}: {e}")
        return None


@meter_call("pdl", "person_enrich")
def _apify_fallback_enrich(linkedin_url: str):
    """Try the Apify HarvestAPI profile scraper when PDL has no record.
    Returns a coffee_chat_data dict on success or None on any failure.

    PDL's index lags ~6 months and misses many student / private accounts —
    Apify scrapes the live LinkedIn page so it catches what PDL can't.
    """
    try:
        from app.services.apify_client import enrich_user_linkedin_profile_via_apify
        envelope = enrich_user_linkedin_profile_via_apify(linkedin_url)
        if not envelope or not envelope.get("ok") or not envelope.get("data"):
            print(f"[Enrichment] Apify fallback returned no data (source={envelope.get('source') if envelope else 'none'})")
            return None
        coffee_chat_data = build_coffee_chat_data_from_apify(envelope["data"])
        print(f"[Enrichment] Apify fallback succeeded for {linkedin_url}")
        print(f"[CoffeeChat] skills={len(coffee_chat_data['skills'])}, interests={len(coffee_chat_data['interests'])}, industry={coffee_chat_data['industry']}")
        set_pdl_cache(linkedin_url, coffee_chat_data)
        return coffee_chat_data
    except Exception as e:
        print(f"[Enrichment] Apify fallback raised: {e}")
        return None


def enrich_linkedin_profile(linkedin_url):
    """Use PDL to enrich LinkedIn profile, with Apify as a fallback when PDL
    has no record of the profile (common for students / smaller accounts).
    """
    try:
        # Check cache first
        cached = get_cached_pdl_data(linkedin_url)
        if cached:
            print(f"Using cached data for: {linkedin_url}")
            return cached

        print(f"Enriching LinkedIn profile: {linkedin_url}")

        # Clean the LinkedIn URL - FIXED VERSION
        linkedin_url = linkedin_url.strip()

        # Remove protocol if present
        linkedin_url = linkedin_url.replace('https://', '').replace('http://', '')

        # Remove www. if present
        linkedin_url = linkedin_url.replace('www.', '')

        # If it's just the username (no linkedin.com), add the full path
        if not linkedin_url.startswith('linkedin.com'):
            linkedin_url = f'https://www.linkedin.com/in/{linkedin_url}'
        else:
            # If it already has linkedin.com, just add https://
            linkedin_url = f'https://{linkedin_url}'

        print(f"Cleaned URL: {linkedin_url}")

        # Use PDL Person Enrichment API
        response = requests.get(
            f"{PDL_BASE_URL}/person/enrich",
            params={
                'api_key': PEOPLE_DATA_LABS_API_KEY,
                'profile': linkedin_url,
                'pretty': True
            },
            timeout=30
        )

        print(f"PDL API response status: {response.status_code}")

        if response.status_code == 200:
            person_data = response.json()
            print(f"PDL response status: {person_data.get('status')}")

            if person_data.get('status') == 200 and person_data.get('data'):
                print(f"Successfully enriched profile")
                pdl_person = person_data['data']

                # Also extract via legacy function for backward compat with other features
                enriched = extract_contact_from_pdl_person_enhanced(pdl_person)
                best_email = (enriched or {}).get('Email', '') if enriched else ''

                # Build rich coffee chat data directly from raw PDL response
                coffee_chat_data = build_coffee_chat_data(pdl_person, best_email)

                print(f"[CoffeeChat] skills={len(coffee_chat_data['skills'])}, interests={len(coffee_chat_data['interests'])}, industry={coffee_chat_data['industry']}")

                print(f"Caching enriched data for: {linkedin_url}")
                set_pdl_cache(linkedin_url, coffee_chat_data)
                return coffee_chat_data
            else:
                print(f"PDL returned status {person_data.get('status')} - no data found, trying Apify fallback")
                if person_data.get('error'):
                    print(f"PDL error: {person_data.get('error')}")
                return _apify_fallback_enrich(linkedin_url)

        elif response.status_code == 404:
            print(f"LinkedIn profile not found in PDL database, trying Apify fallback")
            return _apify_fallback_enrich(linkedin_url)
        elif response.status_code == 402:
            print(f"PDL API: Payment required (out of credits), trying Apify fallback")
            return _apify_fallback_enrich(linkedin_url)
        elif response.status_code == 401:
            # Auth misconfiguration is an operator problem, not a coverage gap —
            # don't burn an Apify call covering for it.
            print(f"PDL API: Invalid API key")
            return None
        else:
            print(f"PDL enrichment failed with status {response.status_code}, trying Apify fallback")
            print(f"Response: {response.text[:500]}")
            return _apify_fallback_enrich(linkedin_url)

    except requests.exceptions.Timeout:
        print(f"PDL API timeout for {linkedin_url}, trying Apify fallback")
        return _apify_fallback_enrich(linkedin_url)
    except Exception as e:
        print(f"LinkedIn enrichment error: {e}")
        import traceback
        traceback.print_exc()
        return None


def get_pdl_cache_key(linkedin_url):
    """Generate cache key for LinkedIn URL"""
    return hashlib.md5(linkedin_url.encode()).hexdigest()


def get_cached_pdl_data(linkedin_url):
    """Get cached PDL data if available"""
    cache_key = get_pdl_cache_key(linkedin_url)
    if cache_key in pdl_cache:
        cached = pdl_cache[cache_key]
        # Don't expire - keep forever as requested
        print(f"Using cached PDL data for {linkedin_url}")
        return cached['data']
    return None


def set_pdl_cache(linkedin_url, data):
    """Cache PDL data permanently"""
    cache_key = get_pdl_cache_key(linkedin_url)
    pdl_cache[cache_key] = {
        'data': data,
        'timestamp': datetime.now()
    }