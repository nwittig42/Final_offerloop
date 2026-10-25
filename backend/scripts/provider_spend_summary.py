"""Read-only summary of vendor spend from the `provider_calls` ledger.

Answers "which people-search vendor are we actually paying, and how much?"
from data rather than from code comments, which disagree: engine_search.py
calls PDL "the retired PDL key" while runs.py still says "PDL is primary".

Both clients are metered (`@meter_call("pdl", ...)` in pdl_client,
`@meter_call("coresignal", ...)` in coresignal_client), so this ledger is the
arbiter.

Also prints remaining PDL title-enrichment headroom. That pool is a FIXED
50,000 credits (not monthly) with a circuit breaker at 90%, so the question
before a large job crawl is how much of the pool is left — not a per-call price.

    PYTHONPATH=$PWD python -m backend.scripts.provider_spend_summary [--days=30]

Writes nothing. Safe to run against PROD.
"""
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.getcwd())

from backend.pipeline.main import _bootstrap_app  # noqa: E402
from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402


def main() -> None:
    days = 30
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            days = int(arg.split("=", 1)[1])

    _bootstrap_app()
    from backend.app.extensions import get_db

    db = get_db()
    since = datetime.now(timezone.utc) - timedelta(days=days)

    print(f"=== provider_calls, last {days} days ===")
    rows = (db.collection("provider_calls")
              .where(filter=FieldFilter("timestamp", ">=", since))
              .select(["provider", "endpoint", "credits_charged",
                       "est_cost_usd", "cache_hit", "status"])
              .stream())

    agg = defaultdict(lambda: {"calls": 0, "credits": 0.0, "usd": 0.0,
                               "cache_hits": 0, "errors": 0})
    total = 0
    for snap in rows:
        d = snap.to_dict() or {}
        prov = d.get("provider") or "?"
        key = (prov, d.get("endpoint") or "?")
        a = agg[key]
        a["calls"] += 1
        a["credits"] += float(d.get("credits_charged") or 0)
        a["usd"] += float(d.get("est_cost_usd") or 0)
        if d.get("cache_hit"):
            a["cache_hits"] += 1
        if d.get("status") and d["status"] != "ok":
            a["errors"] += 1
        total += 1

    if not total:
        print(f"  no rows in the last {days} days")
    else:
        print(f"  {'provider/endpoint':<34}{'calls':>8}{'credits':>10}"
              f"{'est $':>10}{'cached':>8}{'errors':>8}")
        by_provider = defaultdict(float)
        for (prov, ep), a in sorted(agg.items(), key=lambda kv: -kv[1]["usd"]):
            print(f"  {prov + '/' + ep:<34}{a['calls']:>8,}{a['credits']:>10,.0f}"
                  f"{a['usd']:>10,.2f}{a['cache_hits']:>8,}{a['errors']:>8,}")
            by_provider[prov] += a["usd"]
        print(f"\n  {'— by provider —':<34}")
        for prov, usd in sorted(by_provider.items(), key=lambda kv: -kv[1]):
            share = 100.0 * usd / sum(by_provider.values()) if by_provider else 0
            print(f"  {prov:<34}{'':>8}{'':>10}{usd:>10,.2f}  ({share:.0f}% of spend)")
        print(f"\n  total rows: {total:,}")

    # PDL title-enrichment pool — fixed 50k, breaker at 45k.
    print("\n=== PDL title-enrichment pool (job pipeline) ===")
    try:
        from app.services.pdl_title_cache import (
            PDL_TOTAL_BUDGET, TOTAL_BUDGET_CIRCUIT_BREAKER)
        snap = db.collection("pdl_usage").document("title_enrich").get()
        used = int((snap.to_dict() or {}).get("credits_used") or 0) if snap.exists else 0
        print(f"  used:            {used:,}")
        print(f"  breaker at:      {TOTAL_BUDGET_CIRCUIT_BREAKER:,} (90% of pool)")
        print(f"  total pool:      {PDL_TOTAL_BUDGET:,}")
        print(f"  headroom:        {max(0, TOTAL_BUDGET_CIRCUIT_BREAKER - used):,}"
              f" before enrichment stops")
        if not snap.exists:
            print("  (no usage doc yet — title enrichment may never have run)")
    except Exception as e:
        print(f"  could not read pool usage: {type(e).__name__}: {e}")

    # Cached unique titles — cost scales with unique titles, not jobs.
    try:
        n = db.collection("job_title_enrichments").count(alias="c").get()[0][0].value
        print(f"  unique titles already cached: {int(n):,} (0 credits to re-use)")
    except Exception as e:
        print(f"  could not count title cache: {type(e).__name__}")


if __name__ == "__main__":
    main()
