"""email_fast: one person's work email, fastest source first.

Every surface that looks up ONE hand-picked person (the extension's Find
Email, the website's URL paste, the deck reveal, the search-time email
pass) walks the same ladder:

  1. The shelf. A verified address any student found in the last 90 days
     lives on the shared firm_employees doc. Instant, vendor-free.
  2. Hunter's single database. Two to three seconds. Accepted at
     confidence 90 and up (the deck reveal's charge rule). Never written to
     the shelf: Hunter scores deliverability, it does not SMTP-verify, so
     these stay per user.
  3. FullEnrich's waterfall. Fifteen to ninety seconds, slowest on a miss,
     because it walks a dozen providers until one answers. Callers that can
     wait call it inline; callers that cannot (a person staring at a
     button) start it here and finish it later with poll_pending, so no
     request ever waits on the cascade.

Built 2026-09-07 after the extension's Find Email timed out at 90 seconds
on the cascade's miss path. Pure helpers, never raise, empty result on any
failure.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

WAREHOUSE_EMAIL_TTL_DAYS = int(os.environ.get("WAREHOUSE_EMAIL_TTL_DAYS", "90"))
HUNTER_MIN_CONFIDENCE = int(os.environ.get("HUNTER_FAST_MIN_CONFIDENCE", "90"))
HUNTER_TIMEOUT_SECONDS = float(os.environ.get("HUNTER_FAST_TIMEOUT_SECONDS", "4"))
# A background FullEnrich job older than this counts as a miss (and refunds).
PENDING_MAX_AGE_SECONDS = int(os.environ.get("PENDING_EMAIL_MAX_AGE_SECONDS", "420"))


# ── Small shared bits ───────────────────────────────────────────────────


def linkedin_slug(linkedin_url: str) -> str:
    try:
        from app.services.firm_cache.schema import canonicalize_linkedin_url
        _canon, slug = canonicalize_linkedin_url(linkedin_url or "")
        return slug or ""
    except Exception:
        return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(stamp: Any) -> Optional[float]:
    ts = None
    if isinstance(stamp, datetime):
        ts = stamp
    elif isinstance(stamp, str) and stamp:
        try:
            ts = datetime.fromisoformat(stamp.rstrip("Z"))
        except ValueError:
            ts = None
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


# ── Rung 1: the shelf ───────────────────────────────────────────────────


def warehouse_email(linkedin_url: str, db) -> Optional[Dict[str, Any]]:
    """The shared verified address for this person if it is inside the TTL."""
    slug = linkedin_slug(linkedin_url)
    if not slug or db is None:
        return None
    try:
        from app.services.firm_cache.schema import FIRM_EMPLOYEES_COLLECTION
        snap = db.collection(FIRM_EMPLOYEES_COLLECTION).document(slug).get()
        if not getattr(snap, "exists", False):
            return None
        d = snap.to_dict() or {}
        email = (d.get("work_email") or "").strip().lower()
        if not email or "@" not in email:
            return None
        age = _age_seconds(d.get("work_email_verified_at"))
        if age is None or age < 0 or age > WAREHOUSE_EMAIL_TTL_DAYS * 86400:
            return None
        return {"email": email, "source": "warehouse", "verified": True, "confidence": 100}
    except Exception:
        logger.exception("email_fast: warehouse read failed for %s", slug)
        return None


# ── Rung 2: Hunter, one database, seconds ───────────────────────────────


def hunter_email(first_name: str, last_name: str, company: str) -> Optional[Dict[str, Any]]:
    """Hunter Email Finder at confidence >= HUNTER_MIN_CONFIDENCE, else None."""
    first = (first_name or "").strip()
    last = (last_name or "").strip()
    company = (company or "").strip()
    if not (first and last and company):
        return None
    try:
        from app.services.hunter import (
            HUNTER_API_KEY, find_email_with_hunter, get_smart_company_domain,
        )
        if not HUNTER_API_KEY:
            return None
        domain = get_smart_company_domain(company)
        if not domain:
            return None
        email, score = find_email_with_hunter(
            first, last, domain, timeout=HUNTER_TIMEOUT_SECONDS, max_retries=0,
        )
        email = (email or "").strip().lower()
        score = int(score or 0)
        if not email or "@" not in email or score < HUNTER_MIN_CONFIDENCE:
            return None
        return {"email": email, "source": "hunter_finder", "verified": False, "confidence": score}
    except Exception:
        logger.exception("email_fast: hunter rung failed for %s %s at %s", first, last, company)
        return None


def fast_email(first_name: str, last_name: str, company: str, linkedin_url: str, db) -> Optional[Dict[str, Any]]:
    """Rungs 1 and 2 only: whatever can answer in seconds. None means the
    caller decides whether to wait on FullEnrich or start it in the background."""
    hit = warehouse_email(linkedin_url, db)
    if hit:
        return hit
    return hunter_email(first_name, last_name, company)


# ── Rung 3: FullEnrich in the background ────────────────────────────────


def start_pending(first_name: str, last_name: str, company: str, linkedin_url: str, db) -> Optional[Dict[str, Any]]:
    """Fire one FullEnrich job for this person and return the ticket the
    caller stores on the contact, or None when the job could not start."""
    pid = linkedin_slug(linkedin_url) or "p"
    try:
        from app.services import fullenrich_client
        job = fullenrich_client.start_bulk(
            [(pid, first_name or "", last_name or "", company or "", linkedin_url or "")], db=db,
        )
        if not job:
            return None
        return {"job": job, "pid": pid, "startedAt": _now_iso()}
    except Exception:
        logger.exception("email_fast: could not start the pending job for %s", pid)
        return None


def poll_pending(pending: Dict[str, Any], db) -> Tuple[str, Optional[Dict[str, Any]]]:
    """("found", hit) | ("miss", None) | ("pending", None). One read, no wait."""
    job = (pending or {}).get("job") or ""
    pid = str((pending or {}).get("pid") or "")
    if not job:
        return "miss", None
    try:
        from app.services import fullenrich_client
        ready, hits = fullenrich_client.fetch_bulk(job, wait_seconds=0, db=db)
    except Exception:
        logger.exception("email_fast: poll failed for job %s", job)
        ready, hits = False, {}
    if ready:
        hit = (hits or {}).get(pid)
        if hit and hit.get("email"):
            try:
                from app.services import fullenrich_client as _fec
                if not _fec.sellable_gate(hit["email"], hit.get("status") or ""):
                    return "miss", None
            except Exception:
                pass
            status = (hit.get("status") or "found").lower()
            return "found", {
                "email": hit["email"].strip().lower(),
                "source": f"fullenrich_{status}",
                "verified": True,
                "confidence": 0,
            }
        return "miss", None
    age = _age_seconds((pending or {}).get("startedAt"))
    if age is not None and age > PENDING_MAX_AGE_SECONDS:
        logger.info("email_fast: job %s aged out after %.0fs, counting as a miss", job, age)
        return "miss", None
    return "pending", None


# ── Writing a verified address where the next student can find it ───────


def write_email_to_shelf(linkedin_url: str, email: str, source: str, db) -> None:
    """Only VERIFIED addresses (FullEnrich tiers) reach the shared doc, and
    only when the person already has a doc there, so a bare email row never
    pollutes the company index."""
    slug = linkedin_slug(linkedin_url)
    email = (email or "").strip().lower()
    if not slug or not email or db is None or source == "warehouse":
        return
    if not (source or "").startswith("fullenrich"):
        return
    try:
        from app.services.firm_cache.schema import FIRM_EMPLOYEES_COLLECTION
        ref = db.collection(FIRM_EMPLOYEES_COLLECTION).document(slug)
        if not getattr(ref.get(), "exists", False):
            return
        ref.set({
            "work_email": email,
            "work_email_source": source,
            "work_email_verified_at": _now_iso(),
        }, merge=True)
    except Exception:
        logger.exception("email_fast: shelf write failed for %s", slug)
