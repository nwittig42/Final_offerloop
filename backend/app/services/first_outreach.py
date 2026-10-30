"""One free first outreach for new accounts (onboarding v2 walkthrough).

The benefit: the walkthrough's first email reveal costs no credits. It is
only used up when the reveal finds a usable (charge-worthy) email; a miss
releases it so the student can try someone else.

Eligibility is decided here, from trusted data only:
  * FIRST_OUTREACH_FREE_SINCE (ISO date or datetime, UTC) must be set. Unset
    or unparseable means the benefit is off for everyone (fail closed).
  * The account's Firebase Auth creation time (Admin SDK, not the
    client-writable users/{uid}.createdAt) is on or after that cutoff.
  * No grant has been consumed yet.

State lives at users/{uid}/entitlements/firstOutreach, which Firestore rules
deny to clients (default deny), so only the backend can create or change it:
    {status: "reserved" | "consumed", candidateId, reservationId,
     reservedAt, consumedAt}

Concurrency: reserve() is a create-if-absent, so two simultaneous reveals
can't both get it. A reservation older than RESERVATION_TTL_SECONDS (a
request that crashed mid-lookup) can be taken over with a compare-and-set
on the doc's update_time. consume() and release() are compare-and-set too,
so they only ever act on their own reservation.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

RESERVATION_TTL_SECONDS = 600
CUTOFF_ENV = "FIRST_OUTREACH_FREE_SINCE"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def cutoff() -> Optional[datetime]:
    """The launch cutoff, or None when the benefit is off."""
    return _parse(os.environ.get(CUTOFF_ENV, ""))


def account_created_at(uid: str) -> Optional[datetime]:
    """Firebase Auth creation time (trusted). None on any lookup failure."""
    try:
        from firebase_admin import auth as fb_auth
        ms = fb_auth.get_user(uid).user_metadata.creation_timestamp
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc) if ms else None
    except Exception as exc:
        logger.warning("first_outreach: auth lookup failed uid=%s: %s", uid, exc)
        return None


def _ref(db, uid: str):
    return db.collection("users").document(uid).collection("entitlements").document("firstOutreach")


def _stale(data: dict, now: datetime) -> bool:
    reserved = _parse(data.get("reservedAt"))
    return reserved is None or (now - reserved).total_seconds() > RESERVATION_TTL_SECONDS


def account_qualifies(uid: str, created_at: Optional[Callable[[str], Optional[datetime]]] = None) -> bool:
    start = cutoff()
    if start is None:
        return False
    created = (created_at or account_created_at)(uid)
    return bool(created and created >= start)


def status(db, uid: str, created_at: Optional[Callable[[str], Optional[datetime]]] = None) -> dict:
    """{eligible, status} for the client. status: available | reserved | consumed | ineligible."""
    if not account_qualifies(uid, created_at):
        return {"eligible": False, "status": "ineligible"}
    snap = _ref(db, uid).get()
    if not getattr(snap, "exists", False):
        return {"eligible": True, "status": "available"}
    data = snap.to_dict() or {}
    if data.get("status") == "consumed":
        return {"eligible": False, "status": "consumed"}
    if _stale(data, _now()):
        return {"eligible": True, "status": "available"}
    return {"eligible": True, "status": "reserved"}


@dataclass
class Reservation:
    reservation_id: str
    update_time: object  # the doc's update_time after our write (for compare-and-set)


def reserve(db, uid: str, candidate_id: str,
            created_at: Optional[Callable[[str], Optional[datetime]]] = None):
    """Reserve the benefit for one reveal.

    Returns (Reservation, None) on success, or (None, reason) where reason is
    "unavailable" (not eligible, or already used) or "in_use" (another
    reveal holds a live reservation).
    """
    if not account_qualifies(uid, created_at):
        return None, "unavailable"
    from google.api_core import exceptions as gexc

    ref = _ref(db, uid)
    now = _now()
    rid = uuid.uuid4().hex
    doc = {
        "status": "reserved",
        "candidateId": candidate_id,
        "reservationId": rid,
        "reservedAt": _iso(now),
    }
    try:
        result = ref.create(doc)
        return Reservation(rid, getattr(result, "update_time", None)), None
    except gexc.Conflict:
        pass

    snap = ref.get()
    data = (snap.to_dict() or {}) if getattr(snap, "exists", False) else {}
    if data.get("status") == "consumed":
        return None, "unavailable"
    if not _stale(data, now):
        return None, "in_use"
    # Take over a reservation left behind by a crashed request.
    try:
        result = ref.update(doc, option=db.write_option(last_update_time=snap.update_time))
        return Reservation(rid, getattr(result, "update_time", None)), None
    except (gexc.FailedPrecondition, gexc.NotFound, gexc.Conflict):
        return None, "in_use"


def consume(db, uid: str, reservation: Reservation, candidate_id: str) -> bool:
    """Mark our reservation used (a usable email was found)."""
    from google.api_core import exceptions as gexc
    try:
        _ref(db, uid).update(
            {"status": "consumed", "candidateId": candidate_id, "consumedAt": _iso(_now())},
            option=db.write_option(last_update_time=reservation.update_time),
        )
        return True
    except (gexc.FailedPrecondition, gexc.NotFound) as exc:
        # Only after our reservation went stale and another request took it.
        logger.warning("first_outreach: consume lost the reservation uid=%s: %s", uid, exc)
        return False


def release(db, uid: str, reservation: Reservation) -> None:
    """Give the benefit back (no usable email). Never raises."""
    try:
        _ref(db, uid).delete(option=db.write_option(last_update_time=reservation.update_time))
    except Exception as exc:
        # Lost the reservation, or a Firestore blip: it goes stale and frees
        # itself after RESERVATION_TTL_SECONDS.
        logger.warning("first_outreach: release failed uid=%s: %s", uid, exc)
