"""warehouse_metrics — the two numbers that run the warehouse.

Built 2026-08-31, when every people-search surface started routing through
one door. Two daily Firestore docs, both written fire-and-forget:

1. meta/warehouseHitRate/daily/{YYYY-MM-DD}
   searches, requested, warehouse_hits, vendor_fills, full_hits.
   warehouse_hits / (warehouse_hits + vendor_fills) IS the data margin:
   at 0% every contact is a cold vendor buy, at 90% data cost is ~12% of
   revenue. Watch it weekly.

2. meta/warehouseDemand/daily/{YYYY-MM-DD}
   {firms: {slug: n}, title_levels: {bucket: n}, schools: {slug: n}}.
   What people actually search, clustered. The nightly warming reads the
   recent days of this and pre-digs the top firms, so the cache stocks
   real demand instead of only onboarding dreamCompanies.

Both writers swallow every exception: metrics must never break a search.
"""

from __future__ import annotations

import datetime
import re
import threading
from typing import Any, Dict, Optional


def _today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _safe_key(value: str) -> str:
    """Firestore field-path-safe slug: lowercase, alnum + hyphen only."""
    v = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return v[:80]


def _spawn(fn) -> None:
    threading.Thread(target=fn, daemon=True).start()


def log_search_mix(source: str, requested: int,
                   warehouse_hits: int, vendor_fills: int) -> None:
    """One search's serve mix into the daily hit-rate doc."""
    def _write():
        try:
            from google.cloud import firestore as _fs
            from app.extensions import get_db
            db = get_db()
            if db is None:
                return
            doc = (db.collection("meta").document("warehouseHitRate")
                     .collection("daily").document(_today()))
            doc.set({
                "searches": _fs.Increment(1),
                "requested": _fs.Increment(max(0, int(requested))),
                "warehouse_hits": _fs.Increment(max(0, int(warehouse_hits))),
                "vendor_fills": _fs.Increment(max(0, int(vendor_fills))),
                "full_hits": _fs.Increment(
                    1 if warehouse_hits >= requested and requested > 0 else 0),
                f"by_source.{_safe_key(source) or 'unknown'}": _fs.Increment(1),
            }, merge=True)
        except Exception:
            pass
    _spawn(_write)


def log_demand(parsed: Optional[Dict[str, Any]], source: str) -> None:
    """One search's targets into the daily demand doc (firms, title
    levels, schools). Feeds the nightly warming's ranking."""
    if not isinstance(parsed, dict):
        return

    def _write():
        try:
            from google.cloud import firestore as _fs
            from app.extensions import get_db
            db = get_db()
            if db is None:
                return
            # Firms + schools now key by the firm_cache canonical slugs
            # (underscores, corporate-form suffix stripping, school alias
            # table) so the demand map and firm_employees docs share a
            # vocabulary. Previously keyed by _safe_key (hyphens), which
            # produced "bain-company" in demand vs "bain" in
            # firm_employees — the warehouse_warm._demand_ranked_firms
            # consumer could never match multi-word firms. by_source and
            # title_levels still use _safe_key: those are surface labels
            # and bucket names, not firm_cache lookups.
            from app.services.firm_cache.schema import (
                derive_title_level,
                slugify_company,
                slugify_school,
            )
            updates: Dict[str, Any] = {}
            for c in (parsed.get("companies") or [])[:5]:
                name = c.get("name") if isinstance(c, dict) else c
                key = slugify_company(str(name or ""))
                if key:
                    updates[f"firms.{key}"] = _fs.Increment(1)
            try:
                levels = {derive_title_level(str(t))
                          for t in (parsed.get("title_variations") or [])[:8] if t}
                for lv in levels:
                    if lv and lv != "other":
                        updates[f"title_levels.{_safe_key(lv)}"] = _fs.Increment(1)
            except Exception:
                pass
            for s in (parsed.get("schools") or [])[:3]:
                name = s.get("name") if isinstance(s, dict) else s
                key = slugify_school(str(name or ""))
                if key:
                    updates[f"schools.{key}"] = _fs.Increment(1)
            if not updates:
                return
            updates["searches"] = _fs.Increment(1)
            updates[f"by_source.{_safe_key(source) or 'unknown'}"] = _fs.Increment(1)
            (db.collection("meta").document("warehouseDemand")
               .collection("daily").document(_today())).set(updates, merge=True)
        except Exception:
            pass
    _spawn(_write)


def recent_demand_firms(days: int = 7) -> Dict[str, int]:
    """Summed firm demand over the last `days` daily docs, keyed by the
    display-ish slug. For the nightly warming's ranking. Empty on error."""
    out: Dict[str, int] = {}
    try:
        from app.extensions import get_db
        db = get_db()
        if db is None:
            return out
        today = datetime.datetime.now(datetime.timezone.utc)
        for i in range(max(1, days)):
            day = (today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            snap = (db.collection("meta").document("warehouseDemand")
                      .collection("daily").document(day).get())
            if not getattr(snap, "exists", False):
                continue
            firms = (snap.to_dict() or {}).get("firms") or {}
            for slug, n in firms.items():
                try:
                    out[slug] = out.get(slug, 0) + int(n)
                except Exception:
                    continue
    except Exception:
        return out
    return out
