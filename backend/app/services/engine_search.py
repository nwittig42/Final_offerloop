"""engine_search — the one door every people-search surface walks through.

Warehouse first, FullEnrich search for the remainder. Coresignal was
canceled 2026-10-29; the fallback rung that used to sit after FullEnrich
is gone, so a FullEnrich-returns-zero response now surfaces as an honest
empty result (same contract as "no matches").

Shipped 2026-10-28 as PR #232 when FullEnrich replaced Coresignal as the
primary vendor: FullEnrich's /people/search covers discovery at
$0.0145/row (vs Coresignal's $0.08/collect), and `fullenrich_search.py`
is already a PDL-compatible 4-tuple wrapper, so flipping the primary
cascaded to every surface routed through this module.

Original rationale (still true): /api/prompt-search was the only surface
reading firm_employees before paying a vendor; every other surface either
paid cold rates on every call or pointed at the retired PDL key.

Contract: same 4-tuple shape as pdl_client.search_contacts_from_prompt,
so it is a drop-in at every call site:
    (contacts, retry_level_used, already_saved_contacts, metadata)

Rules this module enforces so callers don't have to:
  1. firm_employees is read BEFORE any vendor call. A full cache hit costs
     zero credits and skips the vendor entirely.
  2. Cache hits are added to the vendor exclude set so the remainder call
     can't re-buy people we already have.
  3. `is_configured()` reports whether the vendor rung can run, so caller
     gates don't bind to a specific env var.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Set, Tuple

SearchResult = Tuple[List[Dict[str, Any]], int, List[Dict[str, Any]], Optional[Dict[str, Any]]]


def is_configured() -> bool:
    """True if engine_search_contacts has a vendor rung wired up.

    Returns False only when FullEnrich's API key is absent. Callers that
    require vendor discovery (recruiter finder, hiring-manager finder)
    should gate on this instead of checking a specific vendor's env var
    themselves.

    Does NOT verify budget headroom — a FullEnrich-out-of-budget state is
    still "configured", just temporarily exhausted. The engine returns an
    empty result in that case, same as any other no-match.
    """
    return bool((os.getenv("FULLENRICH_API_KEY") or "").strip())


def engine_search_contacts(
    parsed: Dict[str, Any],
    max_contacts: int,
    exclude_keys: Optional[Set] = None,
    user_profile: Optional[Dict[str, Any]] = None,
    allow_vendor: bool = True,
    source: str = "engine",
) -> SearchResult:
    """Warehouse → FullEnrich search. Never raises; empty on miss.

    allow_vendor=False serves warehouse hits only and never spends a
    credit; the public no-login widgets run in this mode. `source` labels
    the surface in the hit-rate and demand metrics.
    """
    exclude_keys = set(exclude_keys or set())

    try:
        from app.services.warehouse_metrics import log_demand
        log_demand(parsed, source)
    except Exception:
        pass

    # Rung 1: the warehouse. Free, and every prior vendor fill lives here.
    cache_hits: List[Dict[str, Any]] = []
    cache_meta: Optional[Dict[str, Any]] = None
    try:
        from app.services.firm_cache import search_firm_cache
        from app.services.firm_cache.reader import _flag_enabled as _lookup_enabled
        if _lookup_enabled():
            cache_hits, _, _, cache_meta = search_firm_cache(
                parsed, max_contacts, exclude_keys=exclude_keys
            )
            if cache_hits:
                print(f"[EngineSearch] warehouse hit: {len(cache_hits)}/{max_contacts} free")
    except Exception as e:
        print(f"[EngineSearch] warehouse lookup failed (non-fatal): {e}")
        cache_hits = []
        cache_meta = None

    if len(cache_hits) >= max_contacts or not allow_vendor:
        meta = dict(cache_meta or {})
        meta.setdefault("provider", "firm_cache")
        meta["firm_cache_hits"] = len(cache_hits)
        try:
            from app.services.warehouse_metrics import log_search_mix
            log_search_mix(source, max_contacts, len(cache_hits), 0)
        except Exception:
            pass
        return cache_hits[:max_contacts], 0, [], meta

    # Rung 2: FullEnrich search for the remainder, cache hits excluded.
    # Gated by FULLENRICH_SEARCH_DISCOVERY=1 plus healthy monthly budget
    # (fullenrich_client.enabled). A disabled rung returns [] honestly —
    # no fallback vendor, no silent 401.
    remainder: List[Dict[str, Any]] = []
    retry_level = 0
    saved: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}

    vendor_exclude = set(exclude_keys)
    for ch in cache_hits:
        fn = (ch.get("FirstName") or "").strip().lower()
        ln = (ch.get("LastName") or "").strip().lower()
        co = (ch.get("Company") or "").strip().lower()
        if fn and ln and co:
            vendor_exclude.add((fn, ln, co))

    try:
        from app.extensions import get_db as _get_db
        from app.services import fullenrich_search
        _fe_db = _get_db()
        if fullenrich_search.discovery_enabled(_fe_db):
            fe_contacts, fe_retry, fe_saved, fe_meta = fullenrich_search.search_contacts_from_prompt(
                parsed, max_contacts - len(cache_hits),
                exclude_keys=vendor_exclude, user_profile=user_profile, db=_fe_db,
            )
            # fullenrich_search ignores exclude_keys internally — enforce the
            # exclude set here so already-saved people don't leak back onto
            # the deck. Matches the (fn, ln, co) tuple shape used elsewhere,
            # with LinkedIn URL as an additional axis.
            filtered: List[Dict[str, Any]] = []
            for c in fe_contacts or []:
                fn = (c.get("FirstName") or "").strip().lower()
                ln = (c.get("LastName") or "").strip().lower()
                co = (c.get("Company") or "").strip().lower()
                li = (c.get("LinkedIn") or "").strip().lower()
                if fn and ln and co and (fn, ln, co) in vendor_exclude:
                    continue
                if li and li in vendor_exclude:
                    continue
                filtered.append(c)
            remainder = filtered
            retry_level = fe_retry
            saved = fe_saved or []
            meta = fe_meta or {}
            meta.setdefault("provider", "fullenrich_search")
    except Exception as fe_err:
        print(f"[EngineSearch] fullenrich rung failed ({fe_err!r})")
        remainder, meta = [], {}

    # Every vendor fill becomes a warehouse asset for the next surface.
    if remainder:
        try:
            from app.services.firm_cache.writer import cache_pdl_contacts
            cache_pdl_contacts(remainder, shape="app", async_write=True)
        except Exception:
            pass

    contacts = cache_hits + (remainder or [])
    if cache_hits:
        meta = dict(meta or {})
        meta["firm_cache_hits"] = len(cache_hits)
        meta["provider_fills"] = len(remainder or [])
    try:
        from app.services.warehouse_metrics import log_search_mix
        log_search_mix(source, max_contacts, len(cache_hits), len(remainder or []))
    except Exception:
        pass
    return contacts, retry_level, saved, (meta or None)
