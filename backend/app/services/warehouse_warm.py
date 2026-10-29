"""warehouse_warm — demand-ranked firm seeds for sparse-profile decks.

Rebuilt 2026-10-29. This module previously existed and was imported by
person_ranker.py (`from app.services.warehouse_warm import _demand_ranked_firms`)
as the sparse-profile fallback — a user with no dreamCompanyNames should
have their first deck seeded with the firms most students target. The
file was deleted at some point; the import error was wrapped in
try/except so the fallback silently returned [] and sparse-profile users
got empty decks for weeks.

Current scope is minimal: one function that reads the daily
meta/warehouseDemand/daily/* docs that warehouse_metrics.log_demand
writes, aggregates firm search volume over a short window, and returns a
list of (display, firm_cache_slug, score) tuples sorted by score. A
future rebuild of the nightly pre-scraping job (see auto-memory
project-firm-employee-cache for context) can extend this module without
renaming anything, since the import path is already baked into
person_ranker.py and the "nightly warm" comments.

Slug coherence note: log_demand keyed the firms map with `_safe_key`
(hyphens) while firm_cache docs are keyed with `slugify_company`
(underscores, with corporate-suffix stripping). This function
canonicalizes to the firm_cache form so the slugs it returns actually
match firm_employees.company. A companion change in warehouse_metrics
aligns log_demand to use slugify_company directly going forward.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

from app.services.firm_cache.schema import slugify_company

logger = logging.getLogger(__name__)

# Days of search demand to aggregate. Short enough that cohorts shifting
# with recruiting season show up; long enough to smooth quiet weekends.
DEMAND_WINDOW_DAYS = 14

# Sanity cap on firms considered across the window. The direct caller
# (person_ranker) truncates further to the top 12 for the deck.
MAX_FIRMS = 50


def _demand_ranked_firms(db) -> List[Tuple[str, str, int]]:
    """[(display, firm_cache_slug, score)], highest score first.

    `display` is a best-effort Title-Case reconstruction of the firm name
    (demand slugs don't preserve the original casing or ampersands, so
    "Bain & Company" round-trips as "Bain Company" — see the slug
    coherence note in the module docstring).

    `firm_cache_slug` is the slug that matches firm_employees.company,
    so callers can query firm_employees directly without re-slugifying.

    `score` is the aggregated search count over DEMAND_WINDOW_DAYS.

    Returns [] on missing docs, Firestore errors, or no demand data. The
    caller (person_ranker) treats an empty return as "no seeds" and
    returns an empty deck.
    """
    if db is None:
        return []
    try:
        today = datetime.now(timezone.utc).date()
        agg: dict[str, int] = {}        # firm_cache_slug -> summed count
        display: dict[str, str] = {}    # firm_cache_slug -> reconstructed display

        for offset in range(DEMAND_WINDOW_DAYS):
            day = (today - timedelta(days=offset)).isoformat()
            try:
                snap = (db.collection("meta").document("warehouseDemand")
                          .collection("daily").document(day).get())
            except Exception:
                # One bad day shouldn't take out the aggregation.
                continue
            if not getattr(snap, "exists", False):
                continue
            firms = (snap.to_dict() or {}).get("firms") or {}
            if not isinstance(firms, dict):
                continue
            for raw_key, count in firms.items():
                try:
                    n = int(count or 0)
                except (TypeError, ValueError):
                    continue
                if n <= 0:
                    continue
                # Reconstruct a plausible display name from the key,
                # then route through slugify_company so legacy hyphen-
                # slugged entries and new underscore-slugged entries
                # merge into the same firm_cache slug.
                reconstructed = (raw_key or "").replace("-", " ").replace("_", " ").strip()
                fc_slug = slugify_company(reconstructed) or raw_key
                if not fc_slug:
                    continue
                agg[fc_slug] = agg.get(fc_slug, 0) + n
                display.setdefault(fc_slug, (reconstructed.title() or raw_key))

        if not agg:
            return []

        ranked = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:MAX_FIRMS]
        return [(display[slug], slug, count) for slug, count in ranked]
    except Exception:
        logger.exception("_demand_ranked_firms: aggregation failed")
        return []
