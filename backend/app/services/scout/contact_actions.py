"""Scout execute actions: find contacts and pull company intel from chat.

Powers "find me 3 software engineers at Spotify" (and the one-beat chain
"...and email them") without leaving the panel. Both wrap the MCP tool
pipelines, which already carry the guardrails Scout needs:

- find_contacts: tier caps, shared result cache, credit deduction
  (5 credits per contact returned), PDL search + warmth scoring, and
  persistence into users/{uid}/contacts so My Network, the Inbox, and
  draft_outreach_emails all see the same saved contacts.
- get_company_intel: cached company overview + recruiting signals +
  alumni density, free.

Returns are compact envelopes the LLM reports verbatim; failures return a
structured `code` instead of raising.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Attribution string for MCP rate-limit/event logs. Authed callers are
# rate-limited by uid, so this is a label, not an identity.
_SCOUT_IP_HASH = "scout-chat"

_MAX_CONTACTS = 10


def _db():
    try:
        from app.extensions import get_db
        return get_db()
    except Exception:
        return None


def _user_ctx(uid: str, tier: Optional[str]) -> Dict[str, Any]:
    return {"uid": uid, "tier": (tier or "free").lower(), "scope": "scout"}


def find_contacts_for_chat(
    uid: str,
    tier: Optional[str],
    company: str,
    role: str = "",
    school: str = "",
    count: int = 5,
) -> Dict[str, Any]:
    """Run a live people search and save the results to My Network.

    Same pipeline as the Claude-MCP find_contacts tool: costs 5 credits per
    contact returned, clamped to the tier's per-search cap.
    """
    empty = {"count": 0, "contacts": []}
    if not uid:
        return {**empty, "error": "sign in required", "code": "AUTH_REQUIRED"}
    company = (company or "").strip()
    if not company:
        return {**empty, "error": "company required", "code": "BAD_REQUEST"}
    from app.services.industry_terms import is_industry_not_company, industry_rejection_message
    if is_industry_not_company(company):
        # An industry is a filter, never an employer (2026-07-09: PDL with
        # company="investment banking" returned shells and dead profiles).
        # The model relays the message and asks for a firm - never spends.
        return {**empty, "error": industry_rejection_message(company),
                "code": "INDUSTRY_NOT_COMPANY"}
    db = _db()
    if db is None:
        return {**empty, "error": "database unavailable", "code": "UNAVAILABLE"}

    try:
        count = max(1, min(int(count or 5), _MAX_CONTACTS))
    except (TypeError, ValueError):
        count = 5

    args: Dict[str, Any] = {"company": company, "count": count}
    if (role or "").strip():
        args["role"] = role.strip()
    if (school or "").strip():
        args["school"] = school.strip()

    # Unified engine (2026-08-27): the same ladder the People deck runs.
    # The old path imported app.mcp_server.tools.find_contacts, which only
    # exists on the WEBSITE branch — on the mobile services every Scout
    # people-find failed "contact search failed" while the hiring-manager
    # tool quietly served PDL-era, photo-less people. Scout now gets
    # Coresignal discovery (photos, warehouse feed, spend metering) and
    # FullEnrich waterfall emails with the verdict-aware gate.
    try:
        raw = _find_via_engine(db, uid, company, args.get("role") or "",
                               args.get("school") or "", count)
    except Exception as e:
        logger.exception("[ScoutContacts] engine find failed: %s", e)
        return {**empty, "error": "contact search failed", "code": "INTERNAL"}

    if not isinstance(raw, dict):
        return {**empty, "error": "contact search failed", "code": "INTERNAL"}
    if raw.get("error"):
        return {**empty, "error": str(raw.get("error")), "code": "BAD_REQUEST"}

    paywall = raw.get("paywall")
    contacts = [
        {
            "name": c.get("name") or "",
            "title": c.get("title") or "",
            "company": c.get("company") or company,
            "linkedin_url": c.get("linkedin_url") or "",
            "email": c.get("email") or "",
            "warmth": c.get("warmth") or "",
            "personalization_hook": c.get("personalization_hook") or "",
        }
        for c in (raw.get("contacts") or [])
        if isinstance(c, dict)
    ]
    if not contacts and paywall:
        # The pipeline's paywall on an authed caller means credits ran out
        # (rate/budget caps surface the same way; the note says which).
        return {
            **empty,
            "error": str(raw.get("note") or "not enough credits for this search"),
            "code": "INSUFFICIENT_CREDITS",
        }

    result: Dict[str, Any] = {
        "count": len(contacts),
        "contacts": contacts,
        "company": company,
        "saved_to_network": bool(contacts),
        "credits_charged": 5 * len(contacts),
    }
    if raw.get("note"):
        result["note"] = str(raw.get("note"))
    return result


def company_intel_for_chat(
    uid: str,
    tier: Optional[str],
    company: str,
    user_school: str = "",
    career_field: str = "",
) -> Dict[str, Any]:
    """Company overview + recruiting signals + alumni density, in chat. Free."""
    if not (company or "").strip():
        return {"error": "company required", "code": "BAD_REQUEST"}
    db = _db()
    if db is None:
        return {"error": "database unavailable", "code": "UNAVAILABLE"}

    args: Dict[str, Any] = {"company": company.strip()}
    if (user_school or "").strip():
        args["user_school"] = user_school.strip()
    if (career_field or "").strip():
        args["career_field"] = career_field.strip()

    try:
        from app.mcp_server.tools.get_company_intel import handle
        raw = handle(
            args=args, ip_hash=_SCOUT_IP_HASH, db=db,
            user_ctx=_user_ctx(uid, tier) if uid else None,
        )
    except Exception as e:
        logger.warning("[ScoutContacts] get_company_intel failed: %s", e)
        return {"error": "company research failed", "code": "INTERNAL"}

    if not isinstance(raw, dict):
        return {"error": "company research failed", "code": "INTERNAL"}
    if raw.get("error"):
        return {"error": str(raw.get("error")), "code": "BAD_REQUEST"}

    # Trim to what the model needs to answer well; drop paywall plumbing.
    return {
        "company": raw.get("company") or company,
        "overview": raw.get("overview") or {},
        "recent_news": (raw.get("recent_news") or [])[:5],
        "recruiting_signals": raw.get("recruiting_signals") or {},
        "divisions": (raw.get("divisions") or [])[:8],
        "alumni_at_your_school": raw.get("alumni_at_your_school"),
    }


def _find_via_engine(db, uid: str, company: str, role: str, school: str,
                     count: int) -> Dict[str, Any]:
    """Coresignal discovery + FullEnrich emails, shaped to the old MCP
    contract: {contacts: [...], note?} where each contact carries name,
    title, company, linkedin_url, email, warmth, personalization_hook.

    Charges 5 credits per contact RETURNED WITH AN EMAIL (an address-less
    person is not the product Scout promised), saves those to the user's
    network with their photo, and mirrors spend into the same meters the
    People deck feeds.
    """
    from google.cloud import firestore as _fs
    import os as _os
    from app.services import coresignal_client, fullenrich_client
    from app.services.firm_cache.writer import cache_pdl_contacts
    from app.services.people_deck import _seen_people_keys
    from app.utils.warmth_scoring import build_briefing_line, score_and_sort_contacts

    parsed = {
        "companies": [{"name": company, "matched_titles": [role] if role else []}],
        "title_variations": [role] if role else [],
        "schools": [school] if school else [],
        "locations": [],
    }

    # Warehouse read (2026-10-03): try the free cache before paying
    # Coresignal, same pattern as the People deck and the Feed right-swipe.
    # This find_contacts call has no location param, so there's no "wrong
    # city" risk here, it only ever matches on company/school/title.
    cache_hits: list = []
    try:
        from app.services.firm_cache import search_firm_cache
        from app.services.firm_cache.reader import _flag_enabled as _cache_lookup_enabled
        if _cache_lookup_enabled():
            cache_hits, _, _, _ = search_firm_cache(
                parsed, count, exclude_keys=_seen_people_keys(db, uid))
            if cache_hits:
                logger.info("[ScoutContacts] warehouse hit uid=%s: %d/%d free",
                            uid, len(cache_hits), count)
    except Exception:
        logger.exception("[ScoutContacts] warehouse lookup failed")
        cache_hits = []

    if len(cache_hits) >= count:
        contacts = list(cache_hits[:count])
    else:
        # Budget wall, same doc the People deck stops on. Default matches
        # people_deck.py's (7,400, the real live Coresignal balance as of
        # 2026-10-02) — this file used to default to a stray "20" instead.
        budget = float(_os.getenv("CORESIGNAL_TEST_BUDGET", "7400"))
        try:
            snap = db.collection("meta").document("coresignalTestBudget").get()
            spent = float((snap.to_dict() or {}).get("spent_estimate", 0.0)) if snap.exists else 0.0
        except Exception:
            spent = budget
        if not coresignal_client.CORESIGNAL_API_KEY or spent >= budget:
            if not cache_hits:
                return {"contacts": [], "note": "people search is at its budget wall right now"}
            contacts = list(cache_hits)
        else:
            contacts, _lvl, _saved, meta = coresignal_client.search_contacts_from_prompt(
                parsed, count - len(cache_hits), exclude_keys=_seen_people_keys(db, uid))
            collected = int((meta or {}).get("collected_count") or 0)
            if collected:
                try:
                    db.collection("meta").document("coresignalTestBudget").set(
                        {"spent_estimate": _fs.Increment(20.0 * collected),
                         "collect_count": _fs.Increment(collected)}, merge=True)
                    from app.services.metering import log_provider_spend
                    log_provider_spend("coresignal", "member_collect", collected, returned=collected)
                except Exception:
                    logger.exception("[ScoutContacts] spend mirror failed")
            try:
                cache_pdl_contacts(contacts, shape="app", async_write=True)
            except Exception:
                logger.exception("[ScoutContacts] warehouse feed failed")
            contacts = list(cache_hits) + (contacts or [])
    if not contacts:
        return {"contacts": []}

    # Warmth + briefing, the same scorer every other surface uses. It
    # returns the contacts SORTED by warmth and stamps warmth_score/tier/
    # label/signals onto each one in place.
    try:
        user_doc = (db.collection("users").document(uid).get().to_dict() or {})
        contacts = score_and_sort_contacts(user_doc, contacts) or contacts
        for c in contacts:
            c["briefing"] = build_briefing_line(c, c.get("warmth_signals") or [])
    except Exception:
        logger.exception("[ScoutContacts] warmth scoring failed")

    # Emails: one bulk waterfall, chat waits (a find in chat is a real ask,
    # and 60-90s with the model narrating beats a silent shrug).
    wanting = [
        (str(i), c.get("FirstName") or "", c.get("LastName") or "",
         c.get("Company") or company, (c.get("LinkedIn") or "").strip())
        for i, c in enumerate(contacts)
        if not (c.get("Email") or "").strip()
    ]
    found: Dict[str, Dict[str, Any]] = {}
    if wanting and fullenrich_client.enabled(db):
        eid = fullenrich_client.start_bulk(wanting, db=db)
        if eid:
            ready, results = fullenrich_client.fetch_bulk(eid, wait_seconds=90, db=db)
            if ready:
                found = results
    for i, c in enumerate(contacts):
        hit = found.get(str(i))
        if hit and fullenrich_client.sellable_gate(hit["email"], hit["status"]):
            c["Email"] = hit["email"]

    emailed = [c for c in contacts if (c.get("Email") or "").strip()]
    if emailed:
        try:
            from app.services.auth import deduct_credits_atomic
            deduct_credits_atomic(uid, 5 * len(emailed), "scout_find_contacts")
        except Exception:
            logger.exception("[ScoutContacts] charge failed")
        # Save to network (skip anyone already there by email).
        from datetime import datetime, timezone
        col = db.collection("users").document(uid).collection("contacts")
        for c in emailed:
            email = (c.get("Email") or "").strip().lower()
            try:
                if list(col.where("email", "==", email).limit(1).stream()):
                    continue
                col.add({
                    "firstName": (c.get("FirstName") or "").strip(),
                    "lastName": (c.get("LastName") or "").strip(),
                    "email": email,
                    "linkedinUrl": (c.get("LinkedIn") or "").strip(),
                    "company": (c.get("Company") or "").strip(),
                    "jobTitle": (c.get("Title") or "").strip(),
                    "college": (c.get("College") or "").strip(),
                    "photoUrl": (c.get("PhotoUrl") or "").strip(),
                    "status": "Not Contacted",
                    "userId": uid,
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                    "source": "scout_chat",
                    "emailVerified": True,
                })
            except Exception:
                logger.exception("[ScoutContacts] save failed for %s", email)

    out = []
    for c in emailed:
        out.append({
            "name": f"{(c.get('FirstName') or '').strip()} {(c.get('LastName') or '').strip()}".strip(),
            "title": (c.get("Title") or "").strip(),
            "company": (c.get("Company") or "").strip(),
            "linkedin_url": (c.get("LinkedIn") or "").strip(),
            "email": (c.get("Email") or "").strip(),
            "warmth": (c.get("warmth_label") or c.get("warmth_tier") or ""),
            "personalization_hook": (c.get("briefing") or "").strip(),
        })
    note = None
    misses = len(contacts) - len(emailed)
    if misses > 0:
        note = f"found {len(contacts)} matching people; {misses} had no reachable email and were not charged"
    result: Dict[str, Any] = {"contacts": out}
    if note:
        result["note"] = note
    return result
