"""Tiered people search for Find contacts.

Per slot, cheapest first:
  1. The warehouse (free, no vendor spend). Covers both "people the warehouse
     already knows at this company" and "people named in the posting if
     they're already on the shelf."
  2. FullEnrich /people/search for whatever is still empty — city filter
     first, no-city fallback for sparse markets.

A slot stops as soon as it has a candidate. Budget caps vendor spend
for the whole search in FullEnrich rows (≈ $0.0145 each).

Prior to 2026-10-30 this module ran a 4-round Coresignal escalation
(r1 keywords+city → r2 keywords → r3 city → r4 titles only) with a
two-step search+collect pattern unique to Coresignal. That pattern doesn't
translate to FullEnrich, which returns full contacts in one call and bills
per returned row instead of per two-step search+collect. The port
collapses the rounds to one FullEnrich search with a city-then-no-city
widening fallback.

Named-people boost regression: the Coresignal version could look up a
specific person by name+company in one call. FullEnrich's /people/search
doesn't filter on names, so the named-people step now relies on the
warehouse — if the posting names "Jane Doe at Deloitte" and she's on the
shelf, she lands. If not, she's silently skipped. A proper named-lookup
path (Perplexity → LinkedIn URL → Apify enrich) is a follow-up.
"""
from __future__ import annotations

import logging
import re

from app.services.engine_search import engine_search_contacts
from app.services.find_contacts.scoring import person_key, title_matches
from app.services.fullenrich_search import search_contacts_from_prompt

logger = logging.getLogger(__name__)

# Max rows pulled from each tier per slot.
WAREHOUSE_MAX = 5
FULLENRICH_PER_ROUND = 5

_RECRUITER_ROLE = re.compile(
    r"recruit|talent|human resources|\bhr\b|campus|university relations|early careers", re.I)
_LEADER_ROLE = re.compile(r"\bhead\b|\bchief\b|founder|\bc[a-z]?o\b", re.I)


class Budget:
    """Vendor spend left for one Find-contacts search, in FullEnrich rows.

    One row ≈ $0.0145, so a default budget of 10 rows ≈ $0.15 per search.
    Prior Coresignal-era `Budget(collects=6, fullenrich_calls=1)` mapped to
    roughly $0.55 of worst-case spend; FullEnrich-only is cheaper per unit
    so the row count can stay in the same ballpark without inflating cost.
    """

    def __init__(self, rows: int = 10):
        self.rows = rows

    def take(self, n: int) -> int:
        granted = max(0, min(n, self.rows))
        self.rows -= granted
        return granted


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


def _find_in_warehouse(company: str, first: str, last: str) -> dict | None:
    """Warehouse-only name lookup. Returns the first matching contact or None.

    Used for the named-people-from-posting boost. If the person is on the
    shelf we grab them for free; if not, we silently skip (no vendor spend
    on names because FullEnrich search can't filter by name)."""
    if not (company and first and last):
        return None
    parsed = {
        "companies": [{"name": company}],
        "title_variations": [],
        "locations": [],
        "skip_title_guard": True,
    }
    result = _safe(engine_search_contacts, None, parsed, 50,
                   allow_vendor=False, source="find_contacts_named")
    if not result:
        return None
    for c in (result[0] or []):
        fn = (c.get("FirstName") or "").strip().lower()
        ln = (c.get("LastName") or "").strip().lower()
        if fn == first.lower() and ln == last.lower():
            return c
    return None


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

    # 1. People named in the posting — warehouse only. See module docstring
    # on the named-lookup regression and the follow-up plan.
    for person in facts.get("named_people") or []:
        slot = named_slot(person.get("role"), specs)
        name = (person.get("name") or "").strip()
        if slot is None or out[slot] or not name:
            continue
        parts = name.split(" ", 1)
        first = parts[0]
        last = parts[1] if len(parts) > 1 else ""
        hit = _find_in_warehouse(company, first, last)
        if hit:
            add(slot, [hit], "named_warehouse")

    # 2. Warehouse by title: free.
    for slot in empty():
        titles = specs[slot].get("titles") or []
        if not titles:
            continue
        parsed = {"companies": [{"name": company}], "title_variations": titles,
                  "locations": [], "skip_title_guard": True}
        result = _safe(engine_search_contacts, None, parsed, WAREHOUSE_MAX,
                       allow_vendor=False, source="find_contacts")
        add(slot, (result or [[]])[0], "warehouse")

    # 3. FullEnrich — city filter first, no-city fallback. Widens by one
    # round only (FullEnrich returns rich contacts per call, so there's no
    # advantage to the 4-round Coresignal escalation we had before).
    for slot in empty():
        titles = specs[slot].get("titles") or []
        if not titles or budget.rows <= 0:
            continue
        rounds = []
        if city:
            rounds.append(("fullenrich_city", [city]))
        rounds.append(("fullenrich_nocity", []))
        for found_by, locations in rounds:
            take = budget.take(min(FULLENRICH_PER_ROUND, budget.rows))
            if take <= 0:
                break
            parsed = {"companies": [{"name": company}], "title_variations": titles,
                      "locations": locations}
            result = _safe(search_contacts_from_prompt, None, parsed, take)
            # Title-match first; FullEnrich titles are often abbreviated
            # ("VP, Payments"), so leftovers fall through if nothing matches.
            matched = []
            unmatched = []
            for contact in (result or [[]])[0]:
                if title_matches(contact.get("Title"), titles):
                    matched.append(contact)
                else:
                    unmatched.append(contact)
            add(slot, matched + unmatched, found_by)
            if out[slot]:
                break

    return out
