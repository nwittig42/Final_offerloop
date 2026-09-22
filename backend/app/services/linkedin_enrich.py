"""Engine enrichment for one LinkedIn URL: the front door of meeting prep.

Replaces pdl_client.enrich_linkedin_profile (2026-08-30). That function hit
PDL /person/enrich, and with the retired key 401ing, EVERY coffee-chat prep
died at step 1 with "check the URL and try again", refund included. Coresignal
collects the same profile by its LinkedIn shorthand for 20 grant credits, with
the shared warehouse as a free fallback for people the engine has already met.

Output contract matches pdl_client.build_coffee_chat_data exactly, because the
whole prep pipeline (SERP research, hometown guess, AI sections, PDF) reads
that shape.
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
# treat it as stale and pay for a fresh collect instead.
WAREHOUSE_PROFILE_TTL_DAYS = 90


def _slug(linkedin_url: str) -> str:
    m = re.search(r"linkedin\.com/in/([^/?#]+)", (linkedin_url or ""), re.I)
    if m:
        return m.group(1).strip("/").lower()
    # Bare shorthand ("the-rylan-bohnett") is accepted as-is.
    v = (linkedin_url or "").strip().strip("/").lower()
    return "" if ("/" in v or " " in v or not v) else v


def _mirror_collect_spend(db) -> None:
    try:
        from google.cloud import firestore as _fs
        if db is not None:
            db.collection("meta").document("coresignalTestBudget").set(
                {"spent_estimate": _fs.Increment(20.0), "collect_count": _fs.Increment(1)},
                merge=True,
            )
        from app.services.metering import log_provider_spend
        log_provider_spend("coresignal", "member_collect", 1, returned=1)
    except Exception:
        pass


def _from_coresignal(prof: Dict[str, Any], linkedin_url: str) -> Dict[str, Any]:
    """Coresignal multi-source profile -> the coffee-chat contract."""
    first = (prof.get("first_name") or "").strip()
    last = (prof.get("last_name") or "").strip()
    if not (first or last):
        parts = (prof.get("full_name") or prof.get("name") or "").strip().split()
        first = parts[0] if parts else ""
        last = " ".join(parts[1:]) if len(parts) > 1 else ""

    experience_array = []
    for exp in prof.get("experience") or []:
        if not isinstance(exp, dict):
            continue
        experience_array.append({
            "title":      (exp.get("position_title") or exp.get("title") or "").strip(),
            "company":    (exp.get("company_name") or "").strip(),
            "start_date": exp.get("date_from") or exp.get("start_date") or "",
            "end_date":   exp.get("date_to") or exp.get("end_date") or "",
            "summary":    (exp.get("description") or exp.get("summary") or "")[:600],
        })

    education_array = []
    for edu in prof.get("education") or []:
        if not isinstance(edu, dict):
            continue
        education_array.append({
            "school":     (edu.get("institution_name") or "").strip(),
            "degree":     (edu.get("degree") or "").strip(),
            "major":      (edu.get("field_of_study") or edu.get("major") or "").strip(),
            "start_date": edu.get("date_from") or "",
            "end_date":   edu.get("date_to") or "",
            "gpa":        edu.get("gpa"),
        })

    skills = [s.get("name") if isinstance(s, dict) else str(s)
              for s in (prof.get("skills") or []) if s]
    skills = [s for s in skills if s]

    certifications = []
    for cert in prof.get("certifications") or []:
        if isinstance(cert, dict) and cert.get("name"):
            certifications.append({
                "name": cert.get("name", ""),
                "start_date": cert.get("date_from") or "",
                "end_date": cert.get("date_to") or "",
            })

    cur = experience_array[0] if experience_array else {}
    industry = (prof.get("industry")
                or ((prof.get("experience") or [{}])[0] or {}).get("company_industry")
                or "")

    city = (prof.get("location_city") or "").strip()
    state = (prof.get("location_state") or "").strip()
    country = (prof.get("location_country") or "").strip()
    location_display = (prof.get("location_full") or "").strip() or ", ".join(
        filter(None, [city, state, country])
    )

    return {
        "firstName": first,
        "lastName": last,
        "fullName": f"{first} {last}".strip(),
        "email": "",
        "linkedinUrl": linkedin_url,
        "githubUrl": "",
        "twitterUrl": "",
        "jobTitle": cur.get("title", "") or (prof.get("headline") or "").strip(),
        "company": cur.get("company", ""),
        "industry": industry,
        "jobCompanySize": "",
        "jobCompanyFounded": "",
        "jobCompanyLinkedinUrl": "",
        "city": city,
        "state": state,
        "country": country,
        "location": location_display,
        "experienceArray": experience_array,
        "educationArray": education_array,
        "skills": skills,
        "interests": [i for i in (prof.get("interests") or []) if i],
        "certifications": certifications,
        "languages": [l.get("name") if isinstance(l, dict) else str(l)
                      for l in (prof.get("languages") or []) if l],
        "summary": (prof.get("summary") or prof.get("description") or "").strip(),
        "yearsExperience": prof.get("total_experience_duration_years"),
        "linkedinConnections": prof.get("connections_count"),
        "photoUrl": (prof.get("picture_url") or "").strip(),
        "workExperience": [
            f"{experience_array[0]['title']} at {experience_array[0]['company']}"
            if experience_array else ""
        ],
        "education": (
            f"{education_array[0]['degree']} at {education_array[0]['school']}"
            if education_array else ""
        ),
    }


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


def enrich_linkedin_profile_engine(linkedin_url: str, db=None) -> Optional[Dict[str, Any]]:
    """Coresignal collect by shorthand, warehouse fallback, None on a true miss."""
    slug = _slug(linkedin_url)
    if not slug:
        return None
    with _cache_lock:
        if slug in _cache:
            return _cache[slug]

    data: Optional[Dict[str, Any]] = None
    try:
        from app.services.coresignal_client import _collect_profile, CORESIGNAL_API_KEY
        if CORESIGNAL_API_KEY:
            prof = _collect_profile(slug)  # the endpoint accepts the shorthand
            if prof:
                data = _from_coresignal(prof, linkedin_url)
                _mirror_collect_spend(db)
                # Feed the warehouse so the next surface meets them free.
                try:
                    from app.services.firm_cache.writer import cache_pdl_contacts
                    cache_pdl_contacts([{
                        "FirstName": data["firstName"], "LastName": data["lastName"],
                        "LinkedIn": linkedin_url, "Title": data["jobTitle"],
                        "Company": data["company"], "City": data["city"],
                        "State": data["state"], "College": (data["educationArray"][0]["school"]
                                                            if data["educationArray"] else ""),
                        "PhotoUrl": data["photoUrl"], "Email": "",
                    }], shape="app", async_write=True)
                except Exception:
                    pass
    except Exception:
        logging.getLogger(__name__).exception("engine linkedin enrich failed for %s", slug)

    if data is None and db is not None:
        try:
            snap = db.collection("firm_employees").document(slug).get()
            if getattr(snap, "exists", False):
                data = _from_warehouse(snap.to_dict() or {}, linkedin_url)
        except Exception:
            pass

    if data is not None:
        with _cache_lock:
            _cache[slug] = data
    return data


def warehouse_only_profile(
    linkedin_url: str, db, *, max_age_days: int = WAREHOUSE_PROFILE_TTL_DAYS
) -> Optional[Dict[str, Any]]:
    """The thin shelf record for one LinkedIn URL. No vendor call, $0.

    enrich_linkedin_profile_engine() above always pays for a fresh Coresignal
    collect first, even for someone another student already found — right
    for meeting prep, which needs the full Coresignal shape (experience,
    education detail, skills, summary) for the AI brief. Callers that only
    read the thin shape (name, title, company, city/state, one school,
    photo — the extension's import flow is the big one) should try this
    FIRST and fall back to the engine only on a miss or a stale record, so a
    repeat lookup of someone already on the shelf costs nothing.
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
