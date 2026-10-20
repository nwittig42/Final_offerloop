"""Slot scoring and assignment for Find contacts.

score() ranks one candidate for one slot; fill_slots() picks the best
candidate per slot so no person fills two slots. Alumni status is per
student, so it is applied separately by mark_alumni().
"""
from __future__ import annotations

import re

from app.utils.users import get_university_variants

SLOT_LABELS = {
    "leader": "Leads this team · can vouch for you",
    "process": "Likely runs interviews",
    "recruiter": "Scheduling and logistics",
}
NAMED_LABEL = "Named in the posting"


def person_key(contact: dict) -> str:
    """LinkedIn URL when present, else first|last|company, lowercased."""
    linkedin = (contact.get("LinkedIn") or "").strip().lower().rstrip("/")
    if linkedin:
        return linkedin
    parts = (contact.get("FirstName"), contact.get("LastName"), contact.get("Company"))
    return "|".join((p or "").strip().lower() for p in parts)


def _has_phrase(text: str, phrase: str) -> bool:
    phrase = (phrase or "").strip()
    if not phrase:
        return False
    return re.search(r"\b" + re.escape(phrase.lower()) + r"\b", text) is not None


def title_matches(title: str, titles: list[str]) -> bool:
    """True when any of `titles` appears as a whole phrase in `title`."""
    text = (title or "").lower()
    return any(_has_phrase(text, t) for t in titles or [])


def score(candidate: dict, slot: str, facts: dict, specs: dict) -> float:
    spec = specs.get(slot) or {}
    title = (candidate.get("Title") or "").lower()
    total = 0.0
    total += 3 * sum(1 for kw in spec.get("keywords") or [] if _has_phrase(title, kw))
    if title_matches(title, spec.get("titles")):
        total += 2
    city = (facts.get("office_city") or "").strip().lower()
    if city and (candidate.get("City") or "").strip().lower() == city:
        total += 2
    if slot == "leader" and candidate.get("_coresignal_is_decision_maker"):
        total += 1
    return total


def _school_variants(name: str) -> set[str]:
    name = (name or "").strip()
    if not name:
        return set()
    # "University of Southern California - Marshall School of Business"
    base = re.split(r"\s+[-–—|]\s+", name)[0]
    return get_university_variants(name) | get_university_variants(base)


def mark_alumni(people: list[dict], student_school: str | None) -> list[dict]:
    """Copies of `people` with `alumni` set from their `college`."""
    student = _school_variants(student_school or "")
    return [dict(p, alumni=bool(student & _school_variants(p.get("college") or "")))
            for p in people]


def _person(candidate: dict, slot: str) -> dict:
    found_by = candidate.get("_found_by") or ""
    name = f"{candidate.get('FirstName') or ''} {candidate.get('LastName') or ''}".strip()
    return {
        "slot": slot,
        "label": NAMED_LABEL if found_by == "named" else SLOT_LABELS.get(slot, ""),
        "name": name,
        "title": candidate.get("Title") or "",
        "company": candidate.get("Company") or "",
        "city": candidate.get("City") or "",
        "linkedin": candidate.get("LinkedIn") or "",
        "photo": candidate.get("PhotoUrl") or "",
        "college": candidate.get("College") or "",
        "found_by": found_by,
        "person_key": person_key(candidate),
    }


def fill_slots(candidates: dict, facts: dict, specs: dict,
               student_school: str | None) -> list[dict]:
    """Best candidate per slot (named people first), one person per slot."""
    used: set[str] = set()
    people = []
    for slot in specs:
        pool = [c for c in candidates.get(slot) or [] if person_key(c) not in used]
        if not pool:
            continue
        best = max(pool, key=lambda c: (c.get("_found_by") == "named",
                                        score(c, slot, facts, specs)))
        used.add(person_key(best))
        people.append(_person(best, slot))
    return mark_alumni(people, student_school)
