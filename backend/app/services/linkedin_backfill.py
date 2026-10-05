"""Backfill a LinkedIn URL + photo for a contact that arrived without one.

FullEnrich's discovery rung never returns a LinkedIn URL or a photo, so those
contacts show initials and no profile link even when a real public profile
exists. This looks the person up on Coresignal by exact first/last name plus
current company: one search (1 credit) and at most one collect (~20 credits).

Precision over recall: a wrong-person LinkedIn is worse than none, so we only
accept a single search hit whose name AND current employer both match. Misses
are negative-cached, so repeat reveals never re-spend on the same person.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from app.services import coresignal_client
from app.services.pdl_client import get_contact_identity, normalize_company_for_identity

logger = logging.getLogger(__name__)

NEGATIVE_TTL_DAYS = int(os.getenv("LINKEDIN_BACKFILL_NEGATIVE_TTL_DAYS", "60"))
SEARCH_CREDITS = 1.0
COLLECT_CREDITS = 20.0


def _budget_ok(db) -> bool:
    """Same ceiling the People deck's Coresignal rung respects."""
    budget = float(os.getenv("CORESIGNAL_TEST_BUDGET", "7400"))
    snap = db.collection("meta").document("coresignalTestBudget").get()
    spent = float((snap.to_dict() or {}).get("spent_estimate", 0.0)) if snap.exists else 0.0
    return spent + SEARCH_CREDITS + COLLECT_CREDITS <= budget


def _spend(db, credits: float, collects: int) -> None:
    from google.cloud import firestore as _fs
    db.collection("meta").document("coresignalTestBudget").set(
        {"spent_estimate": _fs.Increment(credits), "collect_count": _fs.Increment(collects)},
        merge=True,
    )


def _lookup_ref(db, identity: str):
    import hashlib
    key = hashlib.sha1(identity.encode()).hexdigest()[:20]
    return db.collection("meta").document("linkedinBackfill").collection("lookups").document(key)


def _negative_fresh(record: Dict[str, Any]) -> bool:
    checked = record.get("checked_at")
    if not isinstance(checked, datetime):
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - checked < timedelta(days=NEGATIVE_TTL_DAYS)


def _profile_matches(prof: Dict[str, Any], first: str, last: str, company: str) -> bool:
    if prof.get("is_deleted") == 1:
        return False
    if (prof.get("first_name") or "").strip().lower() != first.lower():
        return False
    if (prof.get("last_name") or "").strip().lower() != last.lower():
        return False
    want = normalize_company_for_identity(company)
    for exp in prof.get("experience") or []:
        if not isinstance(exp, dict):
            continue
        if exp.get("active_experience") != 1:
            continue
        if normalize_company_for_identity(exp.get("company_name") or "") == want:
            return True
    return False


def find_linkedin_for_contact(db, contact: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Return {"linkedin_url", "photo_url"} for a confident match, else None.

    Never raises: a backfill failure must not break a reveal.
    """
    try:
        if contact.get("LinkedIn") or not coresignal_client.CORESIGNAL_API_KEY:
            return None
        first = (contact.get("FirstName") or "").strip()
        last = (contact.get("LastName") or "").strip()
        company = (contact.get("Company") or "").strip()
        if not (first and last and company):
            return None

        identity = get_contact_identity({"FirstName": first, "LastName": last, "Company": company})
        ref = _lookup_ref(db, identity)
        snap = ref.get()
        if snap.exists:
            record = snap.to_dict() or {}
            if record.get("status") == "hit" and record.get("linkedin_url"):
                return {"linkedin_url": record["linkedin_url"], "photo_url": record.get("photo_url", "")}
            if record.get("status") == "miss" and _negative_fresh(record):
                return None

        if not _budget_ok(db):
            logger.warning("linkedin backfill skipped: coresignal budget ceiling reached")
            return None

        query = {"query": {"bool": {"must": [
            {"match": {"first_name": first}},
            {"match": {"last_name": last}},
            {"nested": {"path": "experience", "query": {"bool": {"must": [
                {"term": {"experience.active_experience": 1}},
                {"match_phrase": {"experience.company_name": company}},
            ]}}}},
        ]}}}
        ids = coresignal_client._search_ids(query, page_size=3)
        _spend(db, SEARCH_CREDITS, 0)

        result = None
        if ids:
            prof = coresignal_client._collect_profile(ids[0])
            _spend(db, COLLECT_CREDITS, 1)
            if prof and _profile_matches(prof, first, last, company):
                url = (prof.get("linkedin_url") or "").strip()
                if url:
                    result = {"linkedin_url": url, "photo_url": (prof.get("picture_url") or "").strip()}

        ref.set({
            "status": "hit" if result else "miss",
            "identity": identity,
            "linkedin_url": result["linkedin_url"] if result else "",
            "photo_url": result["photo_url"] if result else "",
            "checked_at": datetime.now(timezone.utc),
        })
        return result
    except Exception:
        logger.exception("linkedin backfill failed (non-fatal)")
        return None
