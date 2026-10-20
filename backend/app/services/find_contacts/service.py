"""Find contacts for one job, saved once and shared by every student.

The result lives at jobs/{jobId}/contacts/result. The first caller takes a
60-second lock in a transaction and runs the search; anyone arriving while
the lock is live gets {"status": "searching"}. A crash leaves the lock to
expire, and the next caller retakes it. Alumni status is per student, so
the saved people carry `college` only and `alumni` is set on every read.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from firebase_admin import firestore

from app.services.find_contacts.playbooks import company_size, slot_specs
from app.services.find_contacts.posting_reader import read_posting
from app.services.find_contacts.scoring import fill_slots, mark_alumni
from app.services.find_contacts.search import Budget, find_slot_candidates
from app.services.fullenrich_client import find_work_email

logger = logging.getLogger(__name__)

LOCK_SECONDS = 60
_PRIVATE_FIELDS = ("alumni", "email", "email_status")


def _result_ref(db, job_id: str):
    return db.collection("jobs").document(job_id).collection("contacts").document("result")


def _as_utc(value) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _claim(db, ref) -> tuple[str, dict | None]:
    """("ready", doc) when saved, ("searching", None) when locked, else ("claimed", None)."""

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
        tx.set(ref, {"status": "searching",
                     "lock_until": now + timedelta(seconds=LOCK_SECONDS)}, merge=True)
        return "claimed", None

    return _txn(db.transaction())


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


def _posting_facts(job_ref, job: dict) -> dict:
    facts = job.get("posting_facts")
    if isinstance(facts, dict) and facts:
        return facts
    facts = read_posting(job)
    job_ref.set({"posting_facts": facts}, merge=True)
    return facts


def find_contacts_for_job(db, job_id: str, student_school: str | None) -> dict:
    job_ref = db.collection("jobs").document(job_id)
    snap = job_ref.get()
    job = (snap.to_dict() or {}) if snap.exists else None
    if job is None or job.get("expired") is True:
        return {"status": "not_found"}

    ref = _result_ref(db, job_id)
    state, saved = _claim(db, ref)
    if state == "ready":
        return _response(saved, student_school)
    if state == "searching":
        return {"status": "searching"}

    facts = _posting_facts(job_ref, job)
    specs = slot_specs(facts, company_size(facts, job))
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
    ref.set(doc)  # replaces the lock fields
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

    found = find_work_email(
        first_name=person.get("first_name") or "",
        last_name=person.get("last_name") or "",
        company=person.get("company") or "",
        linkedin_url=person.get("linkedin") or "",
        db=db,
    )
    if not found or not found.get("email"):
        return None
    result = {"email": found["email"], "status": found.get("status") or ""}

    # Re-read inside a transaction so concurrent reveals of other people
    # in the same doc are not overwritten.
    @firestore.transactional
    def _save(tx):
        snap = ref.get(transaction=tx)
        doc = snap.to_dict() if snap.exists else None
        if _find_person(doc, person_key) is None:
            return
        people = [dict(p, email=result["email"], email_status=result["status"])
                  if p.get("person_key") == person_key else p
                  for p in doc.get("people") or []]
        tx.set(ref, {"people": people}, merge=True)

    try:
        _save(db.transaction())
    except Exception:
        logger.exception("find_contacts: saving email failed job=%s", job_id)
    return result
