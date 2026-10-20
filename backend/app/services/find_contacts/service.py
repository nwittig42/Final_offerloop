"""Find contacts for one job, saved once and shared by every student.

The result lives at jobs/{jobId}/contacts/result. The first caller takes a
lock (owner token + lock_until) in a transaction and runs the search; anyone
arriving while the lock is live gets {"status": "searching"}. The owner
refreshes the lock before each paid phase and saves only while it still
owns the lock, so a caller who lost it never spends or overwrites. A crash
leaves the lock to expire, and the next caller retakes it. Alumni status is per student, so
the saved people carry `college` only and `alumni` is set on every read.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from firebase_admin import firestore

from app.services.find_contacts.playbooks import company_size, slot_specs
from app.services.find_contacts.posting_reader import read_posting
from app.services.find_contacts.scoring import fill_slots, mark_alumni
from app.services.find_contacts.search import Budget, find_slot_candidates
from app.services.fullenrich_client import enabled as fullenrich_enabled
from app.services.fullenrich_client import find_work_email

logger = logging.getLogger(__name__)

LOCK_SECONDS = 180
NOT_FOUND = "not_found"
MISS_RETRY_AFTER = timedelta(days=7)
_PRIVATE_FIELDS = ("alumni", "email", "email_status", "email_checked_at")


def _result_ref(db, job_id: str):
    return db.collection("jobs").document(job_id).collection("contacts").document("result")


def _as_utc(value) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _claim(db, ref) -> tuple[str, dict | str | None]:
    """("ready", doc) when saved, ("searching", None) when locked, else ("claimed", token)."""

    @firestore.transactional
    def _txn(tx):
        snap = ref.get(transaction=tx)
        doc = snap.to_dict() if snap.exists else None
        if doc and doc.get("status") == "ready":
            return "ready", doc
        now = datetime.now(timezone.utc)
        lock_until = _as_utc((doc or {}).get("lock_until"))
        if lock_until and lock_until > now:
            return "searching", None
        token = uuid.uuid4().hex
        tx.set(ref, {"status": "searching", "lock_owner": token,
                     "lock_until": now + timedelta(seconds=LOCK_SECONDS)}, merge=True)
        return "claimed", token

    return _txn(db.transaction())


def _owns(doc: dict | None, token: str) -> bool:
    return bool(doc) and doc.get("status") != "ready" and doc.get("lock_owner") == token


def _refresh_lock(db, ref, token: str) -> bool:
    """Extend our lock before a paid phase; False when another caller owns it."""

    @firestore.transactional
    def _txn(tx):
        snap = ref.get(transaction=tx)
        if not _owns(snap.to_dict() if snap.exists else None, token):
            return False
        tx.set(ref, {"lock_until": datetime.now(timezone.utc)
                     + timedelta(seconds=LOCK_SECONDS)}, merge=True)
        return True

    return _txn(db.transaction())


def _save_if_owner(db, ref, token: str, doc: dict) -> dict | None:
    """Save `doc` (clearing the lock) when we still own the lock.

    Returns None on save, else the current doc (or {}) that won instead.
    """

    @firestore.transactional
    def _txn(tx):
        snap = ref.get(transaction=tx)
        current = snap.to_dict() if snap.exists else None
        if not _owns(current, token):
            return current or {}
        tx.set(ref, doc)  # full set drops lock_owner / lock_until
        return None

    return _txn(db.transaction())


def _current_state(current: dict, student_school: str | None) -> dict:
    if current.get("status") == "ready":
        return _response(current, student_school)
    return {"status": "searching"}


def _response(doc: dict, student_school: str | None) -> dict:
    people = [{k: v for k, v in p.items() if k not in _PRIVATE_FIELDS}
              for p in doc.get("people") or []]
    return {
        "status": "ready",
        "company": doc.get("company") or "",
        "team": doc.get("team") or "",
        "team_source": doc.get("team_source") or "",
        "people": mark_alumni(people, student_school),
    }


def _cached_facts(job: dict) -> dict | None:
    facts = job.get("posting_facts")
    return facts if isinstance(facts, dict) and facts else None


def _read_and_cache_facts(job_ref, job: dict, job_id: str) -> dict:
    facts = read_posting(job)
    try:
        job_ref.update({"posting_facts": facts})  # update: never resurrects a deleted job
    except Exception:
        logger.warning("find_contacts: caching posting_facts failed job=%s", job_id, exc_info=True)
    return facts


def find_contacts_for_job(db, job_id: str, student_school: str | None) -> dict:
    job_ref = db.collection("jobs").document(job_id)
    snap = job_ref.get()
    job = (snap.to_dict() or {}) if snap.exists else None
    if job is None or job.get("expired") is True:
        return {"status": "not_found"}

    ref = _result_ref(db, job_id)
    state, claimed = _claim(db, ref)
    if state == "ready":
        return _response(claimed, student_school)
    if state == "searching":
        return {"status": "searching"}
    token = claimed

    facts = _cached_facts(job)
    if facts is None:
        if not _refresh_lock(db, ref, token):
            return {"status": "searching"}
        facts = _read_and_cache_facts(job_ref, job, job_id)
    specs = slot_specs(facts, company_size(facts, job))
    if not _refresh_lock(db, ref, token):
        return {"status": "searching"}
    candidates = find_slot_candidates(facts, specs, Budget())
    people = [{k: v for k, v in p.items() if k != "alumni"}
              for p in fill_slots(candidates, facts, specs, None)]
    found = {p["slot"]: p["found_by"] for p in people}
    steps = {slot: found.get(slot) or "empty" for slot in specs}
    doc = {
        "status": "ready",
        "company": facts.get("company") or job.get("company") or "",
        "team": facts.get("team") or "",
        "team_source": facts.get("team_source") or "",
        "people": people,
        "steps": steps,
        "created_at": firestore.SERVER_TIMESTAMP,
    }
    lost_to = _save_if_owner(db, ref, token, doc)
    if lost_to is not None:
        logger.warning("find_contacts: lost lock, discarding result job=%s", job_id)
        return _current_state(lost_to, student_school)
    logger.info("find_contacts job=%s steps=%s", job_id, steps)
    return _response(doc, student_school)


def _find_person(doc: dict | None, person_key: str) -> dict | None:
    if not doc or doc.get("status") != "ready":
        return None
    return next((p for p in doc.get("people") or [] if p.get("person_key") == person_key), None)


def reveal_email(db, job_id: str, person_key: str) -> dict | None:
    """Saved email for one person, else one vendor lookup saved on that person."""
    ref = _result_ref(db, job_id)
    snap = ref.get()
    person = _find_person(snap.to_dict() if snap.exists else None, person_key)
    if person is None:
        return None
    if person.get("email"):
        return {"email": person["email"], "status": person.get("email_status") or ""}
    if person.get("email_status") == NOT_FOUND:
        checked = _as_utc(person.get("email_checked_at"))
        if checked and datetime.now(timezone.utc) - checked < MISS_RETRY_AFTER:
            return None  # recent miss; older ones are retried
    if not fullenrich_enabled(db):
        return None  # no key or spend cap reached: not a verdict, save nothing

    found = find_work_email(
        first_name=person.get("first_name") or "",
        last_name=person.get("last_name") or "",
        company=person.get("company") or "",
        linkedin_url=person.get("linkedin") or "",
        db=db,
    )
    if found and found.get("email"):
        result = {"email": found["email"], "status": found.get("status") or ""}
        fields = {"email": result["email"], "email_status": result["status"]}
    else:
        result = None  # save the miss so later clicks don't re-spend
        fields = {"email_status": NOT_FOUND, "email_checked_at": datetime.now(timezone.utc)}

    # Re-read inside a transaction so concurrent reveals of other people
    # in the same doc are not overwritten.
    @firestore.transactional
    def _save(tx):
        snap = ref.get(transaction=tx)
        doc = snap.to_dict() if snap.exists else None
        if _find_person(doc, person_key) is None:
            return
        people = [dict(p, **fields) if p.get("person_key") == person_key else p
                  for p in doc.get("people") or []]
        tx.set(ref, {"people": people}, merge=True)

    try:
        _save(db.transaction())
    except Exception:
        logger.exception("find_contacts: saving email failed job=%s", job_id)
    return result
