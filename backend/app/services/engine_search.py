"""engine_search — the one door every people-search surface walks through.

Warehouse first, then a vendor rung for the remainder. The vendor choice
is tracked separately (see PR #232: FullEnrich primary, Coresignal
fallback). Built 2026-08-31 after an audit found that only
/api/prompt-search read the firm_employees cache before paying a vendor;
every other surface (the Chrome extension's four endpoints, the
hiring-manager finder, find-similar) either paid cold rates on every call
or pointed at the retired PDL key.

Contract: same 4-tuple as pdl_client/coresignal_client
search_contacts_from_prompt, so it is a drop-in at every call site:
    (contacts, retry_level_used, already_saved_contacts, metadata)
Each vendor's `meta["provider"]` is passed through so downstream telemetry
can attribute spend without caring which rung ran.

Rules this module enforces so callers don't have to:
  1. firm_employees is read BEFORE any vendor call. A full cache hit costs
     zero credits and skips the vendor entirely.
  2. Cache hits are added to the vendor exclude set so the remainder call
     can't re-buy people we already have.
  3. Vendor spend is mirrored to the provider spend log, so the alerter
     and dashboards see consistent numbers.
  4. `is_configured()` reports whether ANY vendor rung can run, so caller
     gates don't bind to one vendor's env var.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Set, Tuple

SearchResult = Tuple[List[Dict[str, Any]], int, List[Dict[str, Any]], Optional[Dict[str, Any]]]


def is_configured() -> bool:
    """True if engine_search_contacts has at least one vendor rung wired up.

    Returns False only when neither FullEnrich nor Coresignal has an API
    key present. Callers that require vendor discovery (recruiter finder,
    hiring-manager finder) should gate on this instead of checking a
    specific vendor's key — binding to one vendor's env var is what left
    those surfaces broken when the primary vendor was swapped.

    Does NOT verify budget headroom; a FullEnrich-out-of-budget state
    still passes because the engine falls back to Coresignal.
    """
    if (os.getenv("FULLENRICH_API_KEY") or "").strip():
        return True
    try:
        from app.services import coresignal_client
        if getattr(coresignal_client, "CORESIGNAL_API_KEY", ""):
            return True
    except Exception:
        pass
    return False


def _mirror_coresignal_spend(collected: int) -> None:
    if not collected:
        return
    try:
        from google.cloud import firestore as _fs
        from app.extensions import get_db as _get_db
        _db = _get_db()
        if _db is not None:
            _db.collection("meta").document("coresignalTestBudget").set(
                {"spent_estimate": _fs.Increment(20.0 * collected),
                 "collect_count": _fs.Increment(collected)}, merge=True)
        from app.services.metering import log_provider_spend
        log_provider_spend("coresignal", "member_collect", collected, returned=collected)
    except Exception:
        print("[EngineSearch] coresignal spend mirror failed")


def engine_search_contacts(
    parsed: Dict[str, Any],
    max_contacts: int,
    exclude_keys: Optional[Set] = None,
    user_profile: Optional[Dict[str, Any]] = None,
    allow_vendor: bool = True,
    source: str = "engine",
) -> SearchResult:
    """Warehouse-then-Coresignal search. Never raises; empty result on miss.

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

    # Rung 1: the warehouse. Free, and every prior collect lives here.
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

    # Rung 2: Coresignal for the remainder, cache hits excluded.
    remainder: List[Dict[str, Any]] = []
    retry_level = 0
    saved: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}
    try:
        from app.services import coresignal_client
        if getattr(coresignal_client, "CORESIGNAL_API_KEY", ""):
            vendor_exclude = set(exclude_keys)
            for ch in cache_hits:
                fn = (ch.get("FirstName") or "").strip().lower()
                ln = (ch.get("LastName") or "").strip().lower()
                co = (ch.get("Company") or "").strip().lower()
                if fn and ln and co:
                    vendor_exclude.add((fn, ln, co))
            remainder, retry_level, saved, meta = coresignal_client.search_contacts_from_prompt(
                parsed, max_contacts - len(cache_hits),
                exclude_keys=vendor_exclude, user_profile=user_profile,
            )
            meta = meta or {}
            meta.setdefault("provider", "coresignal")
            _mirror_coresignal_spend(int(meta.get("collected_count") or 0))
    except Exception as cs_err:
        print(f"[EngineSearch] coresignal rung failed ({cs_err!r})")
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
