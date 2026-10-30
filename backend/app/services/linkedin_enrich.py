"""Engine enrichment for one LinkedIn URL: the front door of meeting prep.

Backed by Apify (HarvestAPI's `linkedin-profile-scraper` actor) as of
2026-10-29, when Coresignal was canceled. Previously used Coresignal's
collect endpoint to pull a profile by its LinkedIn shorthand; the migration
was already partly done (PR #235 added `enrich_linkedin_profile_via_apify`
for the Chrome extension's thin-shape import path), and this file now
completes it for the rich-shape meeting-prep path too.

Output contract still matches pdl_client.build_coffee_chat_data exactly,
because the whole prep pipeline (SERP research, hometown guess, AI sections,
PDF) reads that shape. The Apify item is normalized through
pdl_client.build_coffee_chat_data_from_apify, which already produces the
right dict for the prep pipeline (it was built as a PDL fallback and the
shape is identical).

Warehouse fallback stays in place: when the shelf has a thin record for
this LinkedIn URL, we serve that instead of calling Apify. Repeat lookups
of a known person are free.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

_cache: Dict[str, Dict[str, Any]] = {}
_cache_lock = threading.Lock()

# How long a shelf record answers for a person before a caller that only
# needs the thin shape (name/title/company/location/school/photo) should
# treat it as stale and pay for a fresh vendor call instead.
WAREHOUSE_PROFILE_TTL_DAYS = 90


def _slug(linkedin_url: str) -> str:
    m = re.search(r"linkedin\.com/in/([^/?#]+)", (linkedin_url or ""), re.I)
    if m:
        return m.group(1).strip("/").lower()
    # Bare shorthand ("the-rylan-bohnett") is accepted as-is.
    v = (linkedin_url or "").strip().strip("/").lower()
    return "" if ("/" in v or " " in v or not v) else v


def _from_warehouse(fs: Dict[str, Any], linkedin_url: str) -> Dict[str, Any]:
    """Thin but honest: enough identity for research + a basic brief."""
    schools = fs.get("schools_display") or []
    return {
        "firstName": fs.get("first_name") or "",
        "lastName": fs.get("last_name") or "",
        "fullName": f"{fs.get('first_name') or ''} {fs.get('last_name') or ''}".strip(),
        "email": "",
        "linkedinUrl": linkedin_url,
        "githubUrl": "", "twitterUrl": "",
        "jobTitle": fs.get("headline") or "",
        "company": fs.get("company_display") or "",
        "industry": "",
        "jobCompanySize": "", "jobCompanyFounded": "", "jobCompanyLinkedinUrl": "",
        "city": fs.get("city") or "", "state": fs.get("state") or "", "country": "",
        "location": ", ".join(filter(None, [fs.get("city") or "", fs.get("state") or ""])),
        "experienceArray": [], "educationArray": [
            {"school": s, "degree": "", "major": "", "start_date": "", "end_date": "", "gpa": None}
            for s in schools
        ],
        "skills": [], "interests": [], "certifications": [], "languages": [],
        "summary": "", "yearsExperience": None, "linkedinConnections": None,
        "photoUrl": fs.get("photo_url") or "",
        "workExperience": [f"{fs.get('headline') or ''} at {fs.get('company_display') or ''}".strip(" at ")],
        "education": schools[0] if schools else "",
    }


def _feed_warehouse(data: Dict[str, Any], linkedin_url: str) -> None:
    """Thin App-shape write-back so the next lookup of this URL is free."""
    try:
        from app.services.firm_cache.writer import cache_pdl_contacts
        cache_pdl_contacts([{
            "FirstName": data.get("firstName", ""),
            "LastName": data.get("lastName", ""),
            "LinkedIn": linkedin_url,
            "Title": data.get("jobTitle", ""),
            "Company": data.get("company", ""),
            "City": data.get("city", ""),
            "State": data.get("state", ""),
            "College": (data.get("educationArray") or [{}])[0].get("school", "") if data.get("educationArray") else "",
            "PhotoUrl": data.get("photoUrl", ""),
            "Email": "",
        }], shape="app", async_write=True)
    except Exception:
        pass


def enrich_linkedin_profile_engine(linkedin_url: str, db=None) -> Optional[Dict[str, Any]]:
    """Warehouse-then-Apify LinkedIn enrichment. None on a true miss.

    Order:
      1. In-process memo cache (per-slug, process lifetime)
      2. firm_employees warehouse (free, name/title/company/school/photo)
      3. Apify HarvestAPI linkedin-profile-scraper (rich: adds experience,
         education, skills, summary, certifications)

    Warehouse hits don't carry the deeper fields (experienceArray, skills,
    etc.) — meeting prep uses whatever is there and leans on SERP research
    for the rest. A caller that strictly needs the deeper fields can pass a
    fresh URL that misses the warehouse to force an Apify call.
    """
    slug = _slug(linkedin_url)
    if not slug:
        return None
    with _cache_lock:
        if slug in _cache:
            return _cache[slug]

    data: Optional[Dict[str, Any]] = None

    # Warehouse first — cache_hit is free vendor spend.
    if db is not None:
        try:
            snap = db.collection("firm_employees").document(slug).get()
            if getattr(snap, "exists", False):
                data = _from_warehouse(snap.to_dict() or {}, linkedin_url)
        except Exception:
            logging.getLogger(__name__).debug("linkedin_enrich warehouse read failed for %s", slug)

    # Apify fallback — rich profile data for coffee chat prep.
    if data is None:
        try:
            from app.services.apify_client import enrich_user_linkedin_profile_via_apify
            from app.services.pdl_client import build_coffee_chat_data_from_apify
            envelope = enrich_user_linkedin_profile_via_apify(linkedin_url)
            if (envelope or {}).get("ok") and envelope.get("data"):
                data = build_coffee_chat_data_from_apify(envelope["data"])
                # The converter leaves linkedinUrl empty when the actor
                # didn't echo one back; the URL we were asked about IS truth.
                if not data.get("linkedinUrl"):
                    data["linkedinUrl"] = linkedin_url
                _feed_warehouse(data, linkedin_url)
        except Exception:
            logging.getLogger(__name__).exception("apify linkedin enrich failed for %s", slug)

    if data is not None:
        with _cache_lock:
            _cache[slug] = data
    return data


# Kept as an alias (and legacy import target) for callers that specifically
# want to signal "I need the Apify path." Pre-cancellation code used this
# name explicitly in linkedin_import.py (PR #235); now both functions do
# the same thing. Removing the alias is a trivial follow-up once no
# importer references it.
enrich_linkedin_profile_via_apify = enrich_linkedin_profile_engine


def warehouse_only_profile(
    linkedin_url: str, db, *, max_age_days: int = WAREHOUSE_PROFILE_TTL_DAYS
) -> Optional[Dict[str, Any]]:
    """The thin shelf record for one LinkedIn URL. No vendor call, $0.

    enrich_linkedin_profile_engine() above always tries the warehouse first
    and falls through to Apify on miss. Surfaces that STRICTLY want "free or
    nothing" (the Chrome extension's bulk-import flow, where paying per-URL
    isn't acceptable) should call this helper and skip to the next action
    on miss.
    """
    slug = _slug(linkedin_url)
    if not slug or db is None:
        return None
    try:
        snap = db.collection("firm_employees").document(slug).get()
        if not getattr(snap, "exists", False):
            return None
        fs = snap.to_dict() or {}
        last_seen = fs.get("last_seen_at")
        if not _fresh_enough(last_seen, max_age_days):
            return None
        return _from_warehouse(fs, linkedin_url)
    except Exception:
        logging.getLogger(__name__).exception("warehouse-only profile lookup failed for %s", slug)
        return None


def _fresh_enough(stamp: Any, max_age_days: int) -> bool:
    if not isinstance(stamp, datetime):
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp).days <= max_age_days
