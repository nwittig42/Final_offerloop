"""firm_cache.reader — JIT cache read path.

Called from pdl_client.search_contacts_from_prompt BEFORE PDL. Queries
firm_employees Firestore collection using indexed filters, returns contacts
in App-shape (same dict layout the app already deals with) so the caller
can slot cache hits + PDL fills into one result list.

Design invariants:
  1. NEVER break a search. All exceptions caught, empty list returned on error.
  2. Use indexed where() only — never in-memory filter on unindexed fields.
     Firestore charges per-doc-read; a stray full-collection scan on
     firm_employees could pull tens of thousands of docs.
  3. Off by default. ENABLE_FIRM_CACHE_LOOKUP env flag gates every read so
     Phase 3 can ship dark and be enabled independently of write.
  4. Most cache docs still have no email (nothing has enriched them yet):
     Email="" and EmailSource="cache_no_email", same as before. A doc DOES
     carry a shared work_email once reveal-time enrichment on ANY surface
     (website, MCP, extension, mobile) finds and verifies one, served back
     here as long as it's within WAREHOUSE_EMAIL_TTL_DAYS (see
     firm_cache/schema.py's _verified_work_email for what counts as
     verified, and warehouse_warm.py for the nightly purge past the TTL).

Query strategy (highest specificity first):
    parsed has companies + schools → (company, schools[])          index
    parsed has companies only      → (company, title_level)        index
    parsed has schools only        → (schools[], title_level)      index
    parsed has neither             → skip (too broad — full scan risk)

Firestore query limits reminder:
  - one array-contains per query
  - multiple equality filters OK
  - can't combine array-contains + range on different fields
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from app.extensions import get_db
from app.services.firm_cache.schema import (
    FIRM_EMPLOYEES_COLLECTION,
    derive_title_level,
    slugify_company,
    slugify_school,
)
from app.services.pdl_client import US_STATE_ABBREVIATIONS

logger = logging.getLogger(__name__)

# Shared verified work emails live on the doc for this long (the app's
# reveal step and the nightly purge use the same number). Same env knob as
# ranker.py and the website branch's firm_cache/reader.py.
_EMAIL_TTL_DAYS = int(os.environ.get("WAREHOUSE_EMAIL_TTL_DAYS", "90"))

_FLAG_ENV = "ENABLE_FIRM_CACHE_LOOKUP"

# Firestore max docs per query. Well under the 10k billing wall — even
# aggressive queries against a big firm (say McKinsey with 15k US docs
# after cache warms) will still fit in one page.
DEFAULT_QUERY_CAP = 500


def _flag_enabled() -> bool:
    raw = os.environ.get(_FLAG_ENV, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# ── Parse extraction helpers ─────────────────────────────────────────────


def _extract_company_slugs(parsed: dict) -> list[str]:
    """Pull company names from parsed prompt, slugify to firm_employees keys.
    Handles both list-of-strings and list-of-dicts shapes."""
    raw = parsed.get("companies") or []
    slugs: list[str] = []
    for c in raw:
        name = c.get("name") if isinstance(c, dict) else c
        if not name:
            continue
        slug = slugify_company(str(name))
        if slug and slug not in slugs:
            slugs.append(slug)
    return slugs


def _extract_school_slugs(parsed: dict) -> list[str]:
    raw = parsed.get("schools") or []
    slugs: list[str] = []
    for s in raw:
        name = s.get("name") if isinstance(s, dict) else s
        if not name:
            continue
        slug = slugify_school(str(name))
        if slug and slug not in slugs:
            slugs.append(slug)
    return slugs


def _extract_locations(parsed: dict) -> list[str]:
    """Pull free-text location strings straight from the parsed prompt, no
    slugifying — matched against doc city/state in _matches_location_filter."""
    raw = parsed.get("locations") or []
    return [str(l).strip() for l in raw if l and str(l).strip()]


def _extract_title_levels(parsed: dict) -> list[str]:
    """Derive title_level bucket set from parsed title_variations.
    Empty list means 'no title filter — accept any level'."""
    raw = parsed.get("title_variations") or []
    levels: set[str] = set()
    for t in raw:
        if not t:
            continue
        levels.add(derive_title_level(str(t)))
    # 'other' isn't a useful positive filter — it matches everything unrecognized
    levels.discard("other")
    return sorted(levels)


# ── Firestore query dispatcher ───────────────────────────────────────────


def _query_by_company_and_school(db, company: str, school: str, cap: int) -> list[dict]:
    """WHERE company=X AND schools array-contains Y. Uses index
    (company ASC, schools ARRAY-CONTAINS)."""
    q = (
        db.collection(FIRM_EMPLOYEES_COLLECTION)
        .where("company", "==", company)
        .where("schools", "array_contains", school)
        .limit(cap)
    )
    return [_snap_to_dict(d) for d in q.stream()]


def _query_by_company(db, company: str, cap: int) -> list[dict]:
    """WHERE company=X. Uses default single-field index on company."""
    q = (
        db.collection(FIRM_EMPLOYEES_COLLECTION)
        .where("company", "==", company)
        .limit(cap)
    )
    return [_snap_to_dict(d) for d in q.stream()]


def _query_by_school(db, school: str, cap: int) -> list[dict]:
    """WHERE schools array-contains Y. Uses default single-field index on
    schools (Firestore auto-indexes array fields for array-contains)."""
    q = (
        db.collection(FIRM_EMPLOYEES_COLLECTION)
        .where("schools", "array_contains", school)
        .limit(cap)
    )
    return [_snap_to_dict(d) for d in q.stream()]


def _snap_to_dict(snap) -> dict:
    d = snap.to_dict() or {}
    d["_id"] = snap.id
    return d


# ── App-shape contact conversion ─────────────────────────────────────────


def _warehouse_email(doc: dict) -> str:
    """The doc's shared verified work email if it is still inside the TTL,
    else "". Timestamps arrive as ISO strings (ranker.py, schema.py) or as
    Firestore datetimes; anything unparseable counts as expired. Ported
    verbatim from the website branch's firm_cache/reader.py so both read the
    same field the same way."""
    email = (doc.get("work_email") or "").strip().lower()
    if not email or "@" not in email:
        return ""
    stamp = doc.get("work_email_verified_at")
    ts = None
    if isinstance(stamp, datetime):
        ts = stamp
    elif isinstance(stamp, str) and stamp:
        try:
            ts = datetime.fromisoformat(stamp.rstrip("Z"))
        except ValueError:
            ts = None
    if ts is None:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return email if 0 <= age <= _EMAIL_TTL_DAYS * 86400 else ""


def firm_employee_doc_to_app_contact(doc: dict) -> dict:
    """Convert a firm_employees doc back into the App-shape contact dict
    that runs.py + downstream email/draft/save code expects.

    Fields the cache doesn't carry get sane defaults:
      - Email: ""             (cache doesn't store PII we can recompute)
      - EmailSource: "cache_no_email"
      - EmailVerified: False
      - EmailConfidenceScore: 0
      - Phone, PersonalEmail, WorkEmail: ""
      - LinkedInConnections: 0
      - VolunteerHistory: "Not available"
      - DataVersion: "firm_cache"
    """
    company_display = doc.get("company_display") or ""
    title_display = doc.get("headline") or ""
    schools_display = doc.get("schools_display") or []
    wh_email = _warehouse_email(doc)
    return {
        "pdlId":              doc.get("pdl_id") or "",
        "FirstName":          doc.get("first_name") or "",
        "LastName":           doc.get("last_name") or "",
        "LinkedIn":           doc.get("linkedin_url") or "",
        "Email":              wh_email,
        "Title":              title_display,
        "Company":            company_display,
        "City":               doc.get("city") or "",
        "State":              doc.get("state") or "",
        "College":            schools_display[0] if schools_display else "",
        "Phone":              "",
        "PersonalEmail":      "",
        "WorkEmail":          wh_email,
        "SocialProfiles":     f"LinkedIn: {doc.get('linkedin_url') or ''}" if doc.get("linkedin_url") else "Not available",
        "EducationTop":       "; ".join(schools_display) if schools_display else "Not available",
        "VolunteerHistory":   "Not available",
        "WorkSummary":        f"Professional at {company_display}" if company_display else "",
        "Group":              f"{company_display} {title_display.split()[0] if title_display else 'Professional'} Team",
        "LinkedInConnections": 0,
        "DataVersion":        "firm_cache",
        # Professional tenure fact for the deck card ("joined this year" is a
        # real outreach hook). Allowlist extension approved by Rylan 2026-08-18.
        "JoinedYear":         doc.get("joined_year") or None,
        # The face. Stored on warehouse rows since 2026-08-26; without this
        # mapping every cache-hydrated person lost their photo on the way
        # out, which blanked ranked deck cards and the Inbox rows they saved.
        "PhotoUrl":           doc.get("photo_url") or "",
        "EmailSource":        "warehouse" if wh_email else "cache_no_email",
        "EmailVerified":      bool(wh_email),
        "EmailConfidenceScore": 95 if wh_email else 0,
        "IsCurrentlyAtTarget": True,  # We only cached them as current — safe default
        # Provenance / observability
        "source":             "firm_cache",
        "_cache_last_seen_at": doc.get("last_seen_at"),
    }


# ── Post-filter (in Python, on the small candidate set) ──────────────────


def _matches_title_filter(doc: dict, allowed_levels: list[str]) -> bool:
    if not allowed_levels:
        return True
    return (doc.get("title_level") or "other") in allowed_levels


def _matches_location_filter(doc: dict, locations: list[str]) -> bool:
    """True if doc's city/state satisfies at least one requested location
    (OR across requested locations, same semantics every live provider's own
    location clause uses). Empty `locations` means none was asked for, so
    everything passes, this only restricts location-bearing queries.

    Every live provider (Coresignal, FullEnrich, Crustdata) enforces company
    AND location together. Before this, the warehouse enforced company only,
    so a cache hit for "Stripe in San Diego" could silently hand back a
    Stripe employee from anywhere, the exact bug Hunter has, just reached
    through the cache instead of the Hunter fallback."""
    if not locations:
        return True
    doc_city = (doc.get("city") or "").strip().lower()
    doc_state_code = (doc.get("state") or "").strip().lower()
    doc_state_name = US_STATE_ABBREVIATIONS.get((doc.get("state") or "").strip().upper(), "").lower()
    for loc in locations:
        loc_l = loc.strip().lower()
        if not loc_l:
            continue
        if loc_l in ("united states", "usa", "u.s.", "us"):
            return True
        if loc_l == doc_city or loc_l == doc_state_code or loc_l == doc_state_name:
            return True
    return False


def _matches_exclude_keys(doc: dict, exclude_keys: set) -> bool:
    """True if this doc should be EXCLUDED (user already has this person)."""
    if not exclude_keys:
        return False
    # get_contact_identity() key is (first, last, company) lowercased.
    first = (doc.get("first_name") or "").lower().strip()
    last = (doc.get("last_name") or "").lower().strip()
    company = (doc.get("company_display") or "").lower().strip()
    return (first, last, company) in exclude_keys


# ── Public entry point ───────────────────────────────────────────────────


def search_firm_cache(
    parsed: dict,
    max_contacts: int,
    exclude_keys: Optional[set] = None,
) -> tuple[list[dict], int, list[dict], Optional[dict]]:
    """Query firm_employees. Return (contacts, retry_level_used, already_saved, metadata)
    matching pdl_client.search_contacts_from_prompt's shape for drop-in compat.

    Returns ([], 0, [], None) on flag off, empty parsed prompt, Firestore
    error, or no matches. Caller should treat empty result as cache-miss
    and fall through to PDL.

    max_contacts caps final returned list. Query fetches up to
    DEFAULT_QUERY_CAP (500) to give post-filters room to work.
    """
    if not _flag_enabled():
        return [], 0, [], None
    if not isinstance(parsed, dict) or max_contacts < 1:
        return [], 0, [], None

    exclude_keys = exclude_keys or set()
    companies = _extract_company_slugs(parsed)
    schools = _extract_school_slugs(parsed)
    title_levels = _extract_title_levels(parsed)
    locations = _extract_locations(parsed)

    # Route to best-matching indexed query. Skip if the prompt is too broad
    # (no company AND no school) — we don't want to full-scan firm_employees.
    if not companies and not schools:
        return [], 0, [], None

    # get_db() may raise RuntimeError if Firebase Admin isn't initialized
    # (unit tests, some scripts, cold-start edge cases). Cache misses must
    # never break the caller, so treat any error the same as no-data.
    try:
        db = get_db()
    except Exception as exc:  # noqa: BLE001
        logger.debug("firm_cache reader: get_db() failed: %s", exc)
        return [], 0, [], None
    if db is None:
        return [], 0, [], None

    candidates: list[dict] = []
    seen_ids: set[str] = set()

    try:
        # Strategy 1: company + school (most specific — uses (company, schools) index)
        if companies and schools:
            for co in companies:
                for sch in schools:
                    for doc in _query_by_company_and_school(db, co, sch, DEFAULT_QUERY_CAP):
                        _id = doc.get("_id")
                        if _id and _id not in seen_ids:
                            seen_ids.add(_id)
                            candidates.append(doc)
        # Strategy 2: company only
        elif companies:
            for co in companies:
                for doc in _query_by_company(db, co, DEFAULT_QUERY_CAP):
                    _id = doc.get("_id")
                    if _id and _id not in seen_ids:
                        seen_ids.add(_id)
                        candidates.append(doc)
        # Strategy 3: school only
        elif schools:
            for sch in schools:
                for doc in _query_by_school(db, sch, DEFAULT_QUERY_CAP):
                    _id = doc.get("_id")
                    if _id and _id not in seen_ids:
                        seen_ids.add(_id)
                        candidates.append(doc)
    except Exception as exc:  # noqa: BLE001 — cache reads must never break search
        logger.warning("firm_cache reader query failed (non-fatal): %s", exc)
        return [], 0, [], None

    if not candidates:
        return [], 0, [], None

    # Post-filter (in Python, on the fetched set — cheap; candidates count is
    # bounded by DEFAULT_QUERY_CAP per query).
    kept: list[dict] = []
    for doc in candidates:
        if _matches_exclude_keys(doc, exclude_keys):
            continue
        if not _matches_title_filter(doc, title_levels):
            continue
        if not _matches_location_filter(doc, locations):
            continue
        kept.append(doc)
        if len(kept) >= max_contacts:
            break

    if not kept:
        return [], 0, [], None

    contacts = [firm_employee_doc_to_app_contact(d) for d in kept]
    metadata = {
        "provider": "firm_cache",
        "cache_hits": len(contacts),
        "candidates_scanned": len(candidates),
    }
    return contacts, 0, [], metadata
