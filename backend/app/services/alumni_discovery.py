"""Auto-discover alumni contacts on the job board.

Powers the "Find alumni at {company}" CTA — turns the dead-end referral path
on jobs where the student has no saved contact into a working
(school × company × title) PDL discovery + draft flow.

Public surface:
  - `discover_alumni(uid, job, ...)`     — relaxation ladder + scoring + caching
  - `score_match_strength(...)`          — per-row strong/moderate/weak helper
  - `read_discovery_cache(uid, job_id)`  — 60-min trust-boundary read
  - `read_negative_cache(uid, company)`  — 7-day "already empty" check
  - `TIER_DISCOVERY_MAX`                  — tier → max contacts mapping

The endpoint layer (`app/routes/alumni_discovery_routes.py`) is responsible
for auth, tier, rate-limiting and translating the result dict into Flask
JSON responses. This module never touches `request` or `jsonify`.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from google.cloud.firestore import SERVER_TIMESTAMP

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

# Tier max contacts for discovery. Intentionally below tier search caps —
# discovery is a wedge, not a contact grinder.
TIER_DISCOVERY_MAX: dict[str, int] = {
    "free": 3,
    "pro": 5,
    "elite": 8,
}

# 60-min TTL chosen by eng review — students park the tab on the alum's
# LinkedIn for several minutes before drafting; 15min forced unnecessary
# re-charges mid-flow.
DISCOVERY_CACHE_TTL_SEC = 60 * 60

# 7-day negative cache: "we already checked, no alumni at this company".
NEGATIVE_CACHE_TTL_SEC = 7 * 24 * 60 * 60

# Hard wall-clock cap on the full relaxation ladder. The PDL helper already
# caps at ~9s per query; we give two rungs room plus overhead before bailing
# with a 504 at the route layer.
PDL_TIMEOUT_SEC = 30

# Rung labels surfaced in the response so the UI can label honestly
# (e.g. "without title filter" vs "recent hires, not alumni").
RUNG_SCHOOL_COMPANY_TITLE = "school+company+title"
RUNG_SCHOOL_COMPANY = "school+company"
RUNG_NO_ALUMNI_FALLBACK = "no-alumni-fallback"
RUNG_EMPTY = "empty"


def is_feature_enabled() -> bool:
    """Read the env flag at call time (not import time) so tests can flip it."""
    return os.getenv("DISCOVER_ALUMNI_ENABLED", "false").lower() == "true"


# ----------------------------------------------------------------------------
# Cache helpers
# ----------------------------------------------------------------------------

def _company_slug(company: str) -> str:
    """Stable filesystem-/Firestore-safe doc id for a company name."""
    norm = re.sub(r"[^a-z0-9]+", "-", (company or "").lower()).strip("-")
    if not norm:
        norm = "unknown"
    # Hash long names so doc IDs stay under the 1500-byte Firestore limit.
    if len(norm) > 80:
        norm = f"{norm[:60]}-{hashlib.sha1(norm.encode()).hexdigest()[:8]}"
    return norm


def _now() -> datetime:
    return datetime.now(timezone.utc)


def read_discovery_cache(uid: str, job_id: str) -> Optional[dict]:
    """Return the discovery-cache doc for (uid, job_id) or None if missing/expired."""
    if not uid or not job_id:
        return None
    try:
        from app.extensions import get_db
        db = get_db()
        if not db:
            return None
        snap = (
            db.collection("users").document(uid)
            .collection("discovery_cache").document(job_id).get()
        )
        if not snap.exists:
            return None
        doc = snap.to_dict() or {}
        expires_at = doc.get("expires_at")
        if expires_at is not None:
            try:
                if expires_at <= _now():
                    return None
            except Exception:
                pass
        return doc
    except Exception as e:
        logger.warning("discovery_cache read failed uid=%s job=%s: %s", uid, job_id, e)
        return None


def write_discovery_cache(uid: str, job_id: str, payload: dict) -> None:
    """Persist the discovery response for `/from-discovery` to read back."""
    if not uid or not job_id:
        return
    try:
        from app.extensions import get_db
        db = get_db()
        if not db:
            return
        created_at = _now()
        doc = {
            **payload,
            "created_at": SERVER_TIMESTAMP,
            "expires_at": created_at + timedelta(seconds=DISCOVERY_CACHE_TTL_SEC),
        }
        (
            db.collection("users").document(uid)
            .collection("discovery_cache").document(job_id).set(doc)
        )
    except Exception as e:
        logger.warning("discovery_cache write failed uid=%s job=%s: %s", uid, job_id, e)


def read_negative_cache(uid: str, company: str) -> Optional[dict]:
    """Return the negative-cache doc for (uid, company) or None if missing/expired."""
    if not uid or not company:
        return None
    slug = _company_slug(company)
    try:
        from app.extensions import get_db
        db = get_db()
        if not db:
            return None
        snap = (
            db.collection("users").document(uid)
            .collection("discovery_negative_cache").document(slug).get()
        )
        if not snap.exists:
            return None
        doc = snap.to_dict() or {}
        expires_at = doc.get("expires_at")
        if expires_at is not None:
            try:
                if expires_at <= _now():
                    return None
            except Exception:
                pass
        return doc
    except Exception as e:
        logger.warning("negative_cache read failed uid=%s company=%s: %s", uid, company, e)
        return None


def write_negative_cache(uid: str, company: str) -> None:
    if not uid or not company:
        return
    slug = _company_slug(company)
    try:
        from app.extensions import get_db
        db = get_db()
        if not db:
            return
        created_at = _now()
        (
            db.collection("users").document(uid)
            .collection("discovery_negative_cache").document(slug).set({
                "company": company,
                "company_slug": slug,
                "created_at": SERVER_TIMESTAMP,
                "expires_at": created_at + timedelta(seconds=NEGATIVE_CACHE_TTL_SEC),
            })
        )
    except Exception as e:
        logger.warning("negative_cache write failed uid=%s company=%s: %s", uid, company, e)


# ----------------------------------------------------------------------------
# Recruiter cache — trust boundary for /referral-draft/from-find-recruiter.
#
# Mirrors discovery_cache but feeds the "Find the Connection" rewire (June
# 2026): find_recruiter writes the validated recruiter list here, then the
# new /from-find-recruiter endpoint reads from it to persist a contact and
# draft an email. The boundary prevents a client from forging contact fields.
# ----------------------------------------------------------------------------

RECRUITER_CACHE_TTL_SEC = 60 * 60  # 60 min, same rationale as discovery_cache


def _generate_search_id(uid: str, company: str, job_title: str) -> str:
    """Stable per-(uid, company, jobTitle) id keyed within a 1-hour bucket
    so a repeat search within the cache window collides with the previous
    one (same searchId, same cache doc). Not for cross-user replay — the
    uid is folded in.
    """
    bucket = int(time.time() // RECRUITER_CACHE_TTL_SEC)
    raw = f"{uid}|{(company or '').lower().strip()}|{(job_title or '').lower().strip()}|{bucket}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def read_recruiter_cache(uid: str, search_id: str) -> Optional[dict]:
    """Return the recruiter-cache doc for (uid, search_id) or None if
    missing/expired. Used by /referral-draft/from-find-recruiter as the
    server-side trust boundary.
    """
    if not uid or not search_id:
        return None
    try:
        from app.extensions import get_db
        db = get_db()
        if not db:
            return None
        snap = (
            db.collection("users").document(uid)
            .collection("recruiter_cache").document(search_id).get()
        )
        if not snap.exists:
            return None
        doc = snap.to_dict() or {}
        expires_at = doc.get("expires_at")
        if expires_at is not None:
            try:
                if expires_at <= _now():
                    return None
            except Exception:
                pass
        return doc
    except Exception as e:
        logger.warning(
            "recruiter_cache read failed uid=%s search_id=%s: %s",
            uid, search_id, e,
        )
        return None


def write_recruiter_cache(uid: str, search_id: str, payload: dict) -> None:
    """Persist a find_recruiter response so /from-find-recruiter can read
    it back. Best-effort — never raises.
    """
    if not uid or not search_id:
        return
    try:
        from app.extensions import get_db
        db = get_db()
        if not db:
            return
        created_at = _now()
        doc = {
            **payload,
            "created_at": SERVER_TIMESTAMP,
            "expires_at": created_at + timedelta(seconds=RECRUITER_CACHE_TTL_SEC),
        }
        (
            db.collection("users").document(uid)
            .collection("recruiter_cache").document(search_id).set(doc)
        )
    except Exception as e:
        logger.warning(
            "recruiter_cache write failed uid=%s search_id=%s: %s",
            uid, search_id, e,
        )


def find_cached_recruiter(cache_doc: dict, recruiter_email: str) -> Optional[dict]:
    """Look up the raw recruiter dict in a cached find-recruiter doc by
    email. Mirrors `find_cached_contact` but matches on Email rather than
    a synthetic pdl_id — the SPA already has Email for each row.
    """
    if not cache_doc or not recruiter_email:
        return None
    needle = recruiter_email.lower().strip()
    for r in cache_doc.get("recruiters") or []:
        if (r.get("Email") or "").lower().strip() == needle:
            return r
    return None


def list_negative_cache_companies(uid: str) -> list[str]:
    """Return company names with a live negative-cache entry.

    Called once on JobBoardPage mount so the UI can branch CTA per job
    without N Firestore reads.
    """
    if not uid:
        return []
    try:
        from app.extensions import get_db
        db = get_db()
        if not db:
            return []
        now = _now()
        out: list[str] = []
        for snap in (
            db.collection("users").document(uid)
            .collection("discovery_negative_cache").stream()
        ):
            doc = snap.to_dict() or {}
            expires_at = doc.get("expires_at")
            if expires_at is not None:
                try:
                    if expires_at <= now:
                        continue
                except Exception:
                    pass
            company = doc.get("company")
            if company:
                out.append(company)
        return out
    except Exception as e:
        logger.warning("negative_cache list failed uid=%s: %s", uid, e)
        return []


# ----------------------------------------------------------------------------
# Match-strength scoring
# ----------------------------------------------------------------------------

# Coarse role families. Matched on the first 2 words of either title — good
# enough to separate "Software Eng" from "Recruiter" without dragging in a
# taxonomy. PDL's own title broadening is upstream of this.
_ROLE_FAMILIES = {
    "engineering": (
        "software", "engineer", "developer", "swe", "sde", "ml", "data",
        "backend", "frontend", "fullstack", "infra", "platform", "devops",
        "site reliability", "security",
    ),
    "product": ("product manager", "product", "pm", "tpm"),
    "design": ("design", "ux", "ui", "designer"),
    "consulting": ("consultant", "consulting", "associate", "analyst",
                   "strategy", "advisory"),
    "banking": ("analyst", "associate", "investment banking", "ib",
                "trader", "trading", "sales and trading"),
    "marketing": ("marketing", "growth", "brand", "content"),
    "operations": ("operations", "ops", "program manager", "chief of staff"),
    "research": ("research", "scientist", "phd"),
    "data": ("data scientist", "data analyst", "analytics"),
}


def _role_family(title: str) -> Optional[str]:
    t = (title or "").lower().strip()
    if not t:
        return None
    # Match longest tokens first across all families so that "investment
    # banking" wins over the generic "analyst" token in consulting.
    candidates: list[tuple[int, str, str]] = []
    for fam, tokens in _ROLE_FAMILIES.items():
        for tok in tokens:
            if tok in t:
                candidates.append((len(tok), fam, tok))
    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][1]


_YEAR_RE = re.compile(r"(20\d{2}|19\d{2})")


def _extract_latest_year(s: str) -> Optional[int]:
    if not s:
        return None
    years = [int(y) for y in _YEAR_RE.findall(s)]
    if not years:
        return None
    # "Present" → use current year as a high signal; otherwise take max year
    # that's not absurdly in the future.
    cap = _now().year + 1
    valid = [y for y in years if y <= cap]
    return max(valid) if valid else None


def score_match_strength(
    contact: dict,
    *,
    user_school: str,
    job_title: str,
    user_grad_year: Optional[int] = None,
) -> tuple[str, list[str]]:
    """Return (strength, reasons) for a discovered alum row.

    Strong   = same school + grad-year within 2y + role family match
    Moderate = at least one strong signal (school OR grad-year OR role family)
    Weak     = no signals matched
    """
    from app.services.pdl_client import _school_aliases  # local to avoid cycles

    reasons: list[str] = []
    score = 0

    # ---- School match (alias-aware) -------------------------------------
    contact_school = (
        contact.get("College")
        or contact.get("college")
        or ""
    )
    school_match = False
    if user_school and contact_school:
        user_aliases = {a.lower() for a in _school_aliases(user_school)}
        contact_aliases = {a.lower() for a in _school_aliases(contact_school)}
        if user_aliases & contact_aliases:
            school_match = True
            score += 2
            short = contact_school.replace(" University", "").strip() or contact_school
            reasons.append(f"{short} alum")

    # ---- Grad year proximity --------------------------------------------
    edu_top = contact.get("EducationTop") or contact.get("educationArray") or ""
    if isinstance(edu_top, list):
        # If it's the array shape, flatten years out.
        years = []
        for e in edu_top:
            if isinstance(e, dict):
                for k in ("end_date", "endYear", "end_year"):
                    v = e.get(k)
                    if v:
                        years.extend([int(y) for y in _YEAR_RE.findall(str(v))])
        contact_year = max(years) if years else None
    else:
        contact_year = _extract_latest_year(str(edu_top))

    if user_grad_year and contact_year and abs(int(user_grad_year) - int(contact_year)) <= 2:
        score += 1
        # Format short class year like "'22"
        short = f"'{str(contact_year)[-2:]}"
        reasons.append(f"{short} — same grad year")

    # ---- Role family ----------------------------------------------------
    job_family = _role_family(job_title or "")
    contact_family = _role_family(contact.get("Title") or "")
    if job_family and contact_family and job_family == contact_family:
        score += 1
        reasons.append(f"{job_family.title()} role family match")

    # School overlap is a NECESSARY condition for non-weak per spec:
    # "no school overlap → weak" (plan verification case 14). Year + role
    # family without school is still cold-outreach to a stranger.
    if not school_match:
        strength = "weak"
    elif score >= 4:
        strength = "strong"
    else:
        strength = "moderate"

    return strength, reasons[:3]


# ----------------------------------------------------------------------------
# Contact row shaping (used by both /discover-alumni and /from-discovery)
# ----------------------------------------------------------------------------

def _pdl_id_for(contact: dict) -> str:
    """Deterministic id we surface to the client. Uses identity hash so
    /from-discovery can re-look up the same contact in the cache without
    trusting client-supplied identity fields.
    """
    from app.services.pdl_client import get_contact_identity
    return hashlib.sha1(get_contact_identity(contact).encode()).hexdigest()[:16]


def _shape_row(
    contact: dict,
    *,
    user_school: str,
    job_title: str,
    user_grad_year: Optional[int],
    rung: str,
) -> dict:
    """Build the API row from a PDL contact dict."""
    strength, reasons = score_match_strength(
        contact,
        user_school=user_school,
        job_title=job_title,
        user_grad_year=user_grad_year,
    )
    email = contact.get("Email") or ""
    has_email = bool(email) and email != "Not available"
    # Relationship classifier in referral_email.py treats shared-school as
    # "moderate"; preserve that signal for the UI badge below the row.
    relationship = "moderate" if strength in ("strong", "moderate") else "weak"
    return {
        "pdl_id": _pdl_id_for(contact),
        "first_name": contact.get("FirstName", ""),
        "last_name": contact.get("LastName", ""),
        "title": contact.get("Title", ""),
        "company": contact.get("Company", ""),
        "school": contact.get("College", ""),
        "linkedin_url": contact.get("LinkedIn", ""),
        "email": email if has_email else "",
        "email_available": has_email,
        "relationship": relationship,
        "match_strength": strength,
        "match_reasons": reasons,
        "matched_on": rung.split("+"),
    }


# ----------------------------------------------------------------------------
# Profile resolution
# ----------------------------------------------------------------------------

def _resolve_user_profile(uid: str) -> dict:
    """Load the user doc once. Returns {} on any failure so callers can
    surface a clean 'no_school' code rather than 500."""
    try:
        from app.extensions import get_db
        db = get_db()
        if not db:
            return {}
        snap = db.collection("users").document(uid).get()
        if not snap.exists:
            return {}
        return snap.to_dict() or {}
    except Exception as e:
        logger.warning("user profile read failed uid=%s: %s", uid, e)
        return {}


def _user_school_from_profile(profile: dict) -> str:
    if not profile:
        return ""
    # Same precedence as `referral_email._load_user_profile`: resumeParsed →
    # top-level → academics. Keeps signals aligned across features.
    rp = profile.get("resumeParsed") or {}
    edu = rp.get("education") or {}
    candidates = [
        edu.get("school"),
        profile.get("university"),
        profile.get("school"),
        (profile.get("academics") or {}).get("university"),
        (profile.get("academics") or {}).get("school"),
    ]
    for c in candidates:
        if c and isinstance(c, str) and c.strip():
            return c.strip()
    return ""


def _user_grad_year_from_profile(profile: dict) -> Optional[int]:
    if not profile:
        return None
    rp = profile.get("resumeParsed") or {}
    edu = rp.get("education") or {}
    candidates = [
        edu.get("graduationYear"),
        edu.get("endYear"),
        profile.get("graduationYear"),
        (profile.get("academics") or {}).get("graduationYear"),
    ]
    for c in candidates:
        try:
            y = int(str(c))
            if 1900 < y < 2100:
                return y
        except (TypeError, ValueError):
            continue
    return None


# ----------------------------------------------------------------------------
# Relaxation ladder
# ----------------------------------------------------------------------------

def _build_parsed_prompt(school: str, company: str, title: str) -> dict:
    return {
        "schools": [school] if school else [],
        "companies": [company] if company else [],
        "title_variations": [title] if title else [],
        "locations": [],
        "industries": [],
    }


def _normalize_title(raw_title: str) -> str:
    """Strip seniority modifiers via PDL enrichment; fall back to raw on error.

    `enrich_job_title_with_pdl` hits PDL's `/job_title/enrich` which is free
    for cleaning. Don't fail discovery if it's down — just use the raw title.
    """
    if not raw_title:
        return ""
    try:
        from app.services.pdl_client import enrich_job_title_with_pdl
        result = enrich_job_title_with_pdl(raw_title) or {}
        cleaned = result.get("cleaned_name")
        if cleaned and isinstance(cleaned, str):
            return cleaned
    except Exception:
        pass
    return raw_title


def _run_search_with_timeout(
    parsed: dict,
    *,
    max_contacts: int,
    exclude_keys: set,
    user_profile: dict,
) -> tuple[list[dict], bool, bool, bool]:
    """Run `search_contacts_from_prompt` with a hard 30s wall-clock cap.

    Provider is FullEnrich (Rylan 2026-09-14: the PDL subscription is
    retired). Returns (contacts, cache_hit, timed_out, partial). `partial`
    is reserved for the mid-flight credit exhaustion case; production
    callers don't surface it yet, but the tuple is shaped so tests can wire
    it in and the route can plumb it without further changes.
    """
    from app.extensions import get_db
    from app.services.fullenrich_client import enabled as _fullenrich_enabled
    from app.services.fullenrich_search import search_contacts_from_prompt

    db = get_db()
    if not _fullenrich_enabled(db):
        logger.warning("alumni_discovery: FullEnrich unavailable (no key or budget exhausted)")
        return [], False, False, False

    started = time.time()
    contacts: list[dict] = []
    timed_out = False
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(
            search_contacts_from_prompt,
            parsed,
            max_contacts,
            exclude_keys,
            user_profile,
            db,
        )
        try:
            result = fut.result(timeout=PDL_TIMEOUT_SEC)
            contacts = list(result[0] or []) if isinstance(result, tuple) else list(result or [])
        except FuturesTimeout:
            timed_out = True
            # Best-effort shutdown — we can't truly kill the thread but the
            # request handler will return 504 immediately.
    elapsed = time.time() - started
    # Sub-second turnaround is almost always a cache short-circuit (a real
    # rung's roundtrip floor is ~1.5s) — soft signal, not load-bearing.
    cache_hit = (not timed_out) and elapsed < 1.0 and bool(contacts)
    return contacts, cache_hit, timed_out, False


# ----------------------------------------------------------------------------
# Public orchestrator
# ----------------------------------------------------------------------------

def discover_alumni(
    uid: str,
    job: dict,
    *,
    tier: str,
    allow_drop_title: bool = False,
    allow_no_school_fallback: bool = False,
) -> dict:
    """Discover alumni for a job. Returns a fully-shaped response dict.

    Possible top-level shapes:
      success: {ok: True, contacts: [...], credits_used, cache_hit, rung, tier_max, partial}
      no_school: {ok: False, code: "no_school"}
      no_title:  {ok: False, code: "no_title"}
      timeout:   {ok: False, code: "pdl_timeout"}
      negative:  {ok: True, contacts: [], cache_hit: "negative", rung: "empty", ...}

    The route layer translates these into HTTP status codes.
    """
    if not uid or not isinstance(job, dict):
        return {"ok": False, "code": "bad_request"}

    company = (job.get("company") or "").strip()
    if not company:
        return {"ok": False, "code": "no_company"}

    job_id = (job.get("job_id") or job.get("id") or "").strip()
    if not job_id:
        return {"ok": False, "code": "no_job_id"}

    profile = _resolve_user_profile(uid)
    user_school = _user_school_from_profile(profile)
    user_grad_year = _user_grad_year_from_profile(profile)

    if not user_school:
        return {"ok": False, "code": "no_school"}

    raw_title = (
        (job.get("title") or "").strip()
        or (profile.get("careerTrack") or "").strip()
    )
    if not raw_title:
        return {"ok": False, "code": "no_title"}

    tier_max = TIER_DISCOVERY_MAX.get(tier, TIER_DISCOVERY_MAX["free"])

    # ---- Negative-cache fast path --------------------------------------
    neg = read_negative_cache(uid, company)
    if neg:
        logger.info(
            "alumni_discovery hit=negative uid=%s company=%s job_id=%s",
            uid, company, job_id,
        )
        return {
            "ok": True,
            "contacts": [],
            "credits_used": 0,
            "cache_hit": "negative",
            "rung": RUNG_EMPTY,
            "tier_max": tier_max,
            "partial": False,
        }

    # ---- Build exclusion set (defense in depth) ------------------------
    exclude_keys: set = set()
    try:
        from app.routes.runs import _get_cached_exclusion_list, _build_exclusion_data_from_firestore
        from app.extensions import get_db
        excl = _get_cached_exclusion_list(uid)
        if not excl:
            db = get_db()
            if db:
                excl = _build_exclusion_data_from_firestore(db, uid)
        if excl:
            exclude_keys = excl.get("identity_set") or set()
    except Exception as e:
        # Exclusion is defense-in-depth; PDL-side dedup still works without it.
        logger.warning("exclusion list lookup failed uid=%s: %s", uid, e)

    normalized_title = _normalize_title(raw_title)

    # ---- Relaxation ladder ---------------------------------------------
    rungs: list[tuple[str, dict]] = [
        (RUNG_SCHOOL_COMPANY_TITLE, _build_parsed_prompt(user_school, company, normalized_title)),
    ]
    if allow_drop_title:
        rungs.append((RUNG_SCHOOL_COMPANY, _build_parsed_prompt(user_school, company, "")))
    if allow_no_school_fallback:
        # No-school fallback is "recent hires at company in the right role",
        # NOT alumni. Surfaced as a separate explicit rung.
        rungs.append((RUNG_NO_ALUMNI_FALLBACK, _build_parsed_prompt("", company, normalized_title)))

    started = time.time()
    contacts: list[dict] = []
    rung_fired = RUNG_EMPTY
    cache_hit = False
    partial = False

    for rung_label, parsed in rungs:
        rung_started = time.time()
        try:
            rung_contacts, rung_cache_hit, timed_out, rung_partial = _run_search_with_timeout(
                parsed,
                max_contacts=tier_max,
                exclude_keys=exclude_keys,
                user_profile=profile,
            )
        except Exception as e:
            logger.exception(
                "alumni_discovery rung=%s failed uid=%s: %s", rung_label, uid, e,
            )
            rung_contacts, rung_cache_hit, timed_out, rung_partial = [], False, False, False

        rung_latency = int((time.time() - rung_started) * 1000)
        logger.info(
            "alumni_discovery rung=%s uid=%s company=%s school=%s "
            "result_count=%d cache_hit=%s latency_ms=%d",
            rung_label, uid, company, user_school,
            len(rung_contacts), rung_cache_hit, rung_latency,
        )

        if timed_out:
            return {"ok": False, "code": "pdl_timeout"}

        if rung_contacts:
            contacts = rung_contacts
            rung_fired = rung_label
            cache_hit = rung_cache_hit
            partial = rung_partial
            break

        # Guard against the total budget — if rung 1 already consumed most
        # of the timeout, don't start rung 2.
        if time.time() - started > PDL_TIMEOUT_SEC - 2:
            logger.info(
                "alumni_discovery short-circuit uid=%s budget_exhausted_after=%s",
                uid, rung_label,
            )
            break

    # ---- Empty result → write negative cache ---------------------------
    if not contacts:
        write_negative_cache(uid, company)
        total_latency = int((time.time() - started) * 1000)
        logger.info(
            "alumni_discovery empty uid=%s company=%s school=%s latency_ms=%d",
            uid, company, user_school, total_latency,
        )
        return {
            "ok": True,
            "contacts": [],
            "credits_used": 0,
            "cache_hit": False,
            "rung": RUNG_EMPTY,
            "tier_max": tier_max,
            "partial": False,
        }

    # ---- Shape rows + write discovery cache ----------------------------
    rows = [
        _shape_row(
            c,
            user_school=user_school,
            job_title=raw_title,
            user_grad_year=user_grad_year,
            rung=rung_fired,
        )
        for c in contacts[:tier_max]
    ]
    # `meter_call` on `execute_pdl_search` charges 1 credit per record
    # returned from the live PDL call; on a cache hit it short-circuits to 0.
    credits_used = 0 if cache_hit else len(rows)

    # The discovery cache holds the FULL PascalCase contact dict (not just
    # the shaped row) so /from-discovery can hand it to
    # `extract_contact_from_pdl_person_enhanced` if needed and to dedup
    # against `users/{uid}/contacts`.
    cache_payload = {
        "job_id": job_id,
        "company": company,
        "rung": rung_fired,
        "contacts": contacts[:tier_max],   # raw PascalCase
        "rows": rows,                       # shaped rows the UI sees
    }
    write_discovery_cache(uid, job_id, cache_payload)

    total_latency = int((time.time() - started) * 1000)
    logger.info(
        "alumni_discovery success uid=%s company=%s school=%s rung=%s "
        "result_count=%d cache_hit=%s credits_used=%d latency_ms=%d",
        uid, company, user_school, rung_fired,
        len(rows), cache_hit, credits_used, total_latency,
    )

    return {
        "ok": True,
        "contacts": rows,
        "credits_used": credits_used,
        "cache_hit": cache_hit,
        "rung": rung_fired,
        "tier_max": tier_max,
        "partial": partial,
    }


# ----------------------------------------------------------------------------
# /from-discovery helpers
# ----------------------------------------------------------------------------

def find_cached_contact(cache_doc: dict, pdl_id: str) -> Optional[dict]:
    """Look up the raw PascalCase contact in a cached discovery doc.

    Trust boundary: the route layer rejects 410 if cache_doc is None or this
    returns None. Prevents a client from forging contact fields and poisoning
    `users/{uid}/contacts/` or the LLM prompt.
    """
    if not cache_doc or not pdl_id:
        return None
    for c in cache_doc.get("contacts") or []:
        if _pdl_id_for(c) == pdl_id:
            return c
    return None


def persist_discovered_contact(
    uid: str,
    pdl_contact: dict,
    *,
    job_id: str,
    company: str,
    matched_on: list[str],
) -> tuple[str, bool]:
    """Persist a PDL discovery into `users/{uid}/contacts/`.

    Returns (contact_id, was_new). Uses a Firestore transaction so two
    parallel tabs racing on the same alum end up with one doc.
    """
    from app.extensions import get_db
    from app.services.pdl_client import get_contact_identity
    from google.cloud import firestore as gcf

    db = get_db()
    if not db:
        raise RuntimeError("firestore_unavailable")

    identity_key = get_contact_identity(pdl_contact)
    contacts_col = db.collection("users").document(uid).collection("contacts")
    new_ref = contacts_col.document()  # pre-allocated id; only used if insert wins

    @gcf.transactional
    def _txn(transaction):
        query = contacts_col.where("identity_key", "==", identity_key).limit(1)
        existing = list(query.get(transaction=transaction))
        if existing:
            return existing[0].id, False
        transaction.set(new_ref, _build_contact_doc(
            pdl_contact,
            identity_key=identity_key,
            job_id=job_id,
            company=company,
            matched_on=matched_on,
        ))
        return new_ref.id, True

    return _txn(db.transaction())


def persist_find_recruiter_contact(
    uid: str,
    pdl_recruiter: dict,
    *,
    search_id: str,
    company: str,
    job_title: str,
) -> tuple[str, bool]:
    """Persist a recruiter from `/find-recruiter` into `users/{uid}/contacts/`.

    Mirrors `persist_discovered_contact` but stamps `source: "find_recruiter"`
    and a different `discoveredVia` envelope. Same txn dedup so parallel
    tabs collapse to one doc.
    """
    from app.extensions import get_db
    from app.services.pdl_client import get_contact_identity
    from google.cloud import firestore as gcf

    db = get_db()
    if not db:
        raise RuntimeError("firestore_unavailable")

    identity_key = get_contact_identity(pdl_recruiter)
    contacts_col = db.collection("users").document(uid).collection("contacts")
    new_ref = contacts_col.document()

    @gcf.transactional
    def _txn(transaction):
        query = contacts_col.where("identity_key", "==", identity_key).limit(1)
        existing = list(query.get(transaction=transaction))
        if existing:
            return existing[0].id, False
        doc = _build_contact_doc(
            pdl_recruiter,
            identity_key=identity_key,
            job_id=search_id,
            company=company,
            matched_on=[],
        )
        # Stamp this contact as coming from find-recruiter, not alumni
        # discovery — the contact-list UI + analytics need to tell them apart.
        doc["source"] = "find_recruiter"
        doc["discoveredVia"] = {
            "search_id": search_id,
            "company": company,
            "job_title": job_title,
            "discovered_at": doc["discoveredVia"]["discovered_at"],
        }
        transaction.set(new_ref, doc)
        return new_ref.id, True

    return _txn(db.transaction())


def _build_contact_doc(
    pdl_contact: dict,
    *,
    identity_key: str,
    job_id: str,
    company: str,
    matched_on: list[str],
) -> dict:
    """Shape a PascalCase PDL contact into the camelCase Firestore contact doc.

    Includes `source: "pdl_discovery"` and a `discoveredVia` envelope so
    the contact-list UI and observability can attribute the contact back
    to the discovery flow.
    """
    first = pdl_contact.get("FirstName", "") or ""
    last = pdl_contact.get("LastName", "") or ""
    email = pdl_contact.get("Email", "") or ""
    if email == "Not available":
        email = ""
    return {
        # Identity (camelCase — matches the rest of the contacts collection)
        "firstName": first,
        "lastName": last,
        "fullName": f"{first} {last}".strip(),
        "email": email,
        "linkedinUrl": pdl_contact.get("LinkedIn", "") or "",
        # Current role
        "jobTitle": pdl_contact.get("Title", "") or "",
        "company": pdl_contact.get("Company", "") or "",
        "city": pdl_contact.get("City", "") or "",
        "state": pdl_contact.get("State", "") or "",
        # School — store under both keys; relationship classifier in
        # referral_email.py reads contact.get("college") or contact.get("College").
        "college": pdl_contact.get("College", "") or "",
        "College": pdl_contact.get("College", "") or "",
        "educationTop": pdl_contact.get("EducationTop", "") or "",
        # Provenance
        "source": "pdl_discovery",
        "discoveredVia": {
            "job_id": job_id,
            "company": company,
            "matched_on": matched_on,
            "discovered_at": SERVER_TIMESTAMP,
        },
        "identity_key": identity_key,
        "createdAt": SERVER_TIMESTAMP,
        "updatedAt": SERVER_TIMESTAMP,
        # Email confidence — mirrors PDL contact shape used elsewhere
        "emailSource": pdl_contact.get("EmailSource", "") or "",
        "emailVerified": bool(pdl_contact.get("EmailVerified")),
    }


__all__ = [
    "TIER_DISCOVERY_MAX",
    "DISCOVERY_CACHE_TTL_SEC",
    "NEGATIVE_CACHE_TTL_SEC",
    "PDL_TIMEOUT_SEC",
    "RUNG_SCHOOL_COMPANY_TITLE",
    "RUNG_SCHOOL_COMPANY",
    "RUNG_NO_ALUMNI_FALLBACK",
    "RUNG_EMPTY",
    "is_feature_enabled",
    "discover_alumni",
    "score_match_strength",
    "read_discovery_cache",
    "write_discovery_cache",
    "read_negative_cache",
    "write_negative_cache",
    "list_negative_cache_companies",
    "find_cached_contact",
    "persist_discovered_contact",
]
