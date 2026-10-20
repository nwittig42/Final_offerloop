"""Tiered people search for Find contacts.

Per slot, cheapest first: people named in the posting (once, before any
slot search), the warehouse (free), Coresignal rounds r1..r4 (paid
collects), then one FullEnrich search for whatever is still empty. A slot
stops as soon as it has a candidate. Budget caps vendor spend for the
whole search.
"""
from __future__ import annotations

import logging
import re

from app.services.coresignal_client import (
    collect_contacts,
    find_person_by_name,
    search_people,
)
from app.services.engine_search import engine_search_contacts
from app.services.find_contacts.scoring import person_key, title_matches
from app.services.fullenrich_search import search_contacts_from_prompt

logger = logging.getLogger(__name__)

COLLECTS_PER_ROUND = 2
WAREHOUSE_MAX = 5
FULLENRICH_MAX = 5

_RECRUITER_ROLE = re.compile(
    r"recruit|talent|human resources|\bhr\b|campus|university relations|early careers", re.I)
_LEADER_ROLE = re.compile(r"\bhead\b|\bchief\b|founder|\bc[a-z]?o\b", re.I)


class Budget:
    """Vendor spend left for one search: Coresignal collects and FullEnrich calls."""

    def __init__(self, collects: int = 6, fullenrich_calls: int = 1):
        self.collects = collects
        self.fullenrich_calls = fullenrich_calls

    def take_collects(self, n: int) -> int:
        granted = max(0, min(n, self.collects))
        self.collects -= granted
        return granted

    def take_fullenrich(self) -> bool:
        if self.fullenrich_calls <= 0:
            return False
        self.fullenrich_calls -= 1
        return True


def named_slot(role: str, specs: dict) -> str | None:
    """Slot a person named in the posting fills, judged from their role."""
    role = role or ""
    if _RECRUITER_ROLE.search(role) and "recruiter" in specs:
        slot = "recruiter"
    elif _LEADER_ROLE.search(role) and not _RECRUITER_ROLE.search(role):
        slot = "leader"
    else:
        slot = "process"
    if slot in specs:
        return slot
    return "process" if "process" in specs else next(iter(specs), None)


def _safe(fn, default, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.exception("find_contacts search step %s failed", getattr(fn, "__name__", fn))
        return default


def _rounds(keywords: list[str], city: str | None) -> list[tuple[str, list[str], str | None]]:
    """R8: r1 keywords+city, r2 keywords, r3 city, r4 titles only; duplicates dropped."""
    out, seen = [], set()
    for name, kw, c in (("coresignal_r1", keywords, city), ("coresignal_r2", keywords, None),
                        ("coresignal_r3", [], city), ("coresignal_r4", [], None)):
        key = (tuple(kw), c)
        if key in seen:
            continue
        seen.add(key)
        out.append((name, list(kw), c))
    return out


def find_slot_candidates(facts: dict, specs: dict, budget: Budget) -> dict[str, list[dict]]:
    company = facts.get("company") or ""
    city = facts.get("office_city") or None
    out: dict[str, list[dict]] = {slot: [] for slot in specs}
    seen: set[str] = set()

    def add(slot: str, contacts: list[dict], found_by: str) -> None:
        for c in contacts or []:
            key = person_key(c)
            if not key or key in seen:
                continue
            seen.add(key)
            out[slot].append(dict(c, _found_by=found_by))

    def empty() -> list[str]:
        return [s for s in specs if not out[s]]

    # 1. People named in the posting. A hit costs one collect.
    for person in facts.get("named_people") or []:
        slot = named_slot(person.get("role"), specs)
        if slot is None or out[slot] or budget.collects <= 0 or not person.get("name"):
            continue
        hit = _safe(find_person_by_name, None, person["name"], company)
        if hit:
            budget.take_collects(1)
            add(slot, [hit], "named")

    # 2. Warehouse: free, no vendor spend.
    for slot in empty():
        titles = specs[slot].get("titles") or []
        if not titles:
            continue
        parsed = {"companies": [{"name": company}], "title_variations": titles,
                  "locations": [], "skip_title_guard": True}
        result = _safe(engine_search_contacts, None, parsed, WAREHOUSE_MAX,
                       allow_vendor=False, source="find_contacts")
        add(slot, (result or [[]])[0], "warehouse")

    # 3. Coresignal rounds, widening until the slot has someone.
    collected_ids: set[int] = set()
    for slot in empty():
        titles = specs[slot].get("titles") or []
        if not titles:
            continue
        for found_by, keywords, round_city in _rounds(specs[slot].get("keywords") or [], city):
            if budget.collects <= 0:
                break
            ids = _safe(search_people, [], company, titles, keywords=keywords, city=round_city)
            fresh = [i for i in ids if i not in collected_ids]
            if not fresh:
                continue
            n = budget.take_collects(min(COLLECTS_PER_ROUND, len(fresh)))
            batch = fresh[:n]
            collected_ids.update(batch)
            add(slot, _safe(collect_contacts, [], batch, n), found_by)
            if out[slot]:
                break

    # 4. One FullEnrich search for every slot still empty.
    still_empty = empty()
    if still_empty and budget.take_fullenrich():
        titles: list[str] = []
        for slot in still_empty:
            titles += [t for t in specs[slot].get("titles") or [] if t not in titles]
        parsed = {"companies": [{"name": company}], "title_variations": titles, "locations": []}
        result = _safe(search_contacts_from_prompt, None, parsed, FULLENRICH_MAX)
        for contact in (result or [[]])[0]:
            for slot in still_empty:
                if not out[slot] and title_matches(contact.get("Title"), specs[slot].get("titles")):
                    add(slot, [contact], "fullenrich")
                    break

    return out
