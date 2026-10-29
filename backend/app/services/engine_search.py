"""engine_search — the one door every people-search surface walks through.

Warehouse first, FullEnrich search for the remainder, Coresignal only as
a cold-start fallback. Rebuilt 2026-10-28 after the vendor audit: Coresignal
was ~2x the per-search cost of FullEnrich and the two signals we uniquely
paid Coresignal for (profile_score, is_decision_maker) weren't read anywhere
in the app. FullEnrich's /people/search covers discovery at $0.0145/row,
and `fullenrich_search.py` is already a PDL-compatible 4-tuple wrapper, so
flipping the primary is a one-file change here that cascades to every
surface that routes through this module. Coresignal stays wired as a
fallback for the rare case FullEnrich returns zero on a well-formed prompt
(e.g. an outage) and for the single-URL enrichment path that FullEnrich
doesn't cover; dropping it entirely is a follow-up PR.

Original rationale (still true): /api/prompt-search was the only surface
reading firm_employees before paying a vendor; every other surface either
paid cold rates on every call or pointed at the retired PDL key.

Contract: same 4-tuple as pdl_client/coresignal_client
search_contacts_from_prompt, so it is a drop-in at every call site:
    (contacts, retry_level_used, already_saved_contacts, metadata)

Rules this module enforces so callers don't have to:
  1. firm_employees is read BEFORE any vendor call. A full cache hit costs
     zero credits and skips the vendor entirely.
  2. Cache hits are added to the vendor exclude set so the remainder call
     can't re-buy people we already have.
  3. Coresignal collects (fallback path only) are mirrored to the
     meta/coresignalTestBudget tank and the provider spend log.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

SearchResult = Tuple[List[Dict[str, Any]], int, List[Dict[str, Any]], Optional[Dict[str, Any]]]


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
    """Warehouse → FullEnrich → Coresignal search. Never raises; empty on miss.

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

    # Rung 2: FullEnrich search for the remainder, cache hits excluded.
    # Primary vendor as of 2026-10-28 — ~half the per-search cost of Coresignal
    # and `fullenrich_search.search_contacts_from_prompt` returns the same
    # 4-tuple. Gated by FULLENRICH_SEARCH_DISCOVERY=1 plus a healthy monthly
    # budget (fullenrich_client.enabled). A disabled rung falls to Rung 3.
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

    # Rung 3: Coresignal fallback — only if FullEnrich returned nothing.
    # Covers the FullEnrich-is-disabled, budget-exhausted, or outage cases.
    # Dropping this rung entirely is tracked as a follow-up once FullEnrich
    # discovery has a clean week of hit-rate data.
    if not remainder:
        try:
            from app.services import coresignal_client
            if getattr(coresignal_client, "CORESIGNAL_API_KEY", ""):
                cs_contacts, cs_retry, cs_saved, cs_meta = coresignal_client.search_contacts_from_prompt(
                    parsed, max_contacts - len(cache_hits),
                    exclude_keys=vendor_exclude, user_profile=user_profile,
                )
                remainder = cs_contacts or []
                retry_level = cs_retry
                saved = cs_saved or []
                meta = cs_meta or {}
                meta.setdefault("provider", "coresignal")
                meta["fallback_from"] = "fullenrich_search"
                _mirror_coresignal_spend(int(meta.get("collected_count") or 0))
        except Exception as cs_err:
            print(f"[EngineSearch] coresignal fallback failed ({cs_err!r})")
            remainder, meta = remainder or [], meta or {}

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
