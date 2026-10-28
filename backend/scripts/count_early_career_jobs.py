"""Read-only census of the jobs collection: how much early-career supply do we have?

Answers "do we have enough internships and entry-level roles?" with counts
instead of estimates. Uses Firestore aggregation COUNT queries, so it bills
~1 read per 1,000 documents counted and never reads document bodies (apart
from the small FIELD SHAPE sample at the end).

    PYTHONPATH=$PWD python -m backend.scripts.count_early_career_jobs

Writes nothing. Safe to run against PROD.

Note on `expired`: it is NOT written on every path. `writer.write_jobs` sets
`expired: False` explicitly, but `writer.sync_board_jobs` (the direct-ATS
crawl, which produces most jobs) batch-sets the normalized doc without it.
A Firestore equality filter only matches documents where the field EXISTS, so
`where("expired","==",False)` silently misses every crawled job. We therefore
derive live = total - (expired == True) instead of filtering on False.
"""
import os
import sys

sys.path.insert(0, os.getcwd())

from backend.pipeline.main import _bootstrap_app  # noqa: E402
from google.cloud.firestore_v1.base_query import FieldFilter  # noqa: E402

SOURCES = ("greenhouse", "lever", "ashby", "simplify", "fantasticjobs",
           "workday", "oracle", "oleeo", "goldman", "manual")


def _print_sources(count, fmt, dead, tier, intern) -> None:
    """Per-source supply. Answers "are we actually getting Workday jobs?"

    The target platforms (workday/oracle/oleeo/goldman/manual) read 0 until
    the OFF-48 branch lands and its daily crawl runs — they are built but have
    never written a job. fantasticjobs also reads 0: the paid 7d sweep is
    skipped by default in main.py.
    """
    print("\n--- where supply comes from (all docs, by source) ---")
    print(f"  {'source':<14}{'total':>9}{'t1':>8}{'t2':>8}{'live t1':>9}")
    rows = []
    for src in SOURCES:
        f = FieldFilter("source", "==", src)
        t1 = count(f, tier(1))
        t1_dead = count(f, tier(1), dead)
        live_t1 = (t1 - t1_dead) if (t1 is not None and t1_dead is not None) else None
        rows.append((src, count(f), t1, count(f, tier(2)), live_t1))
    for src, s_all, s1, s2, s_live1 in sorted(
            rows, key=lambda r: -((r[2] or 0) + (r[3] or 0))):
        flag = "   <- not running yet" if (s_all or 0) == 0 else ""
        print(f"  {src:<14}{fmt(s_all):>9}{fmt(s1):>8}{fmt(s2):>8}{fmt(s_live1):>9}{flag}")


def _print_sample(col, dead, tier, limit=30) -> None:
    """Show the newest tier-1 jobs as a student would meet them.

    Counts say how MANY usable jobs exist; they say nothing about whether a
    student opening the board sees Goldman or sees DoorDash. This is the only
    view that answers that, so it reads documents rather than aggregating.
    """
    from collections import Counter
    print(f"\n--- newest {limit} live tier-1 jobs (what the feed opens on) ---")
    q = (col.where(filter=tier(1))
            .order_by("posted_at", direction="DESCENDING")
            .limit(limit * 3)
            .select(["title", "company", "source", "location", "expired"]))
    rows = []
    for snap in q.stream():
        d = snap.to_dict() or {}
        if d.get("expired") is True:
            continue
        rows.append(d)
        if len(rows) >= limit:
            break
    for d in rows:
        print(f"  {(d.get('company') or '?')[:26]:<26} "
              f"{(d.get('title') or '?')[:58]:<58} "
              f"{(d.get('source') or '?')}")
    if rows:
        print("\n  companies in this sample:",
              ", ".join(f"{c}×{n}" for c, n in
                        Counter((r.get("company") or "?") for r in rows).most_common(12)))


def main() -> None:
    # --sources-only prints just the per-source table. The full census runs
    # ~50 aggregation queries and the source table comes last, so a slow run
    # or an interrupted terminal loses exactly the breakdown you wanted.
    sources_only = "--sources-only" in sys.argv
    sample_only = "--sample" in sys.argv

    _bootstrap_app()
    from backend.app.extensions import get_db

    db = get_db()
    col = db.collection("jobs")

    def count(*filters):
        """COUNT aggregation. Returns int, or None if the query can't run
        (usually a missing composite index) so one failure doesn't kill the run."""
        q = col
        for f in filters:
            q = q.where(filter=f)
        try:
            return int(q.count(alias="c").get()[0][0].value)
        except Exception as e:
            print(f"    [query failed: {type(e).__name__}] "
                  f"{str(e).splitlines()[0][:150]}")
            return None

    def fmt(n):
        return "   n/a" if n is None else f"{n:,}"

    dead = FieldFilter("expired", "==", True)
    tier = lambda t: FieldFilter("relevance_tier", "==", t)  # noqa: E731
    intern = FieldFilter("employment_type", "==", "INTERNSHIP")

    if sample_only:
        _print_sample(col, dead, tier)
        return
    if sources_only:
        _print_sources(count, fmt, dead, tier, intern)
        return

    total = count()
    n_dead = count(dead)
    n_live = (total - n_dead) if (total is not None and n_dead is not None) else None
    print(f"TOTAL docs in `jobs`         : {fmt(total)}")
    print(f"  expired == True            : {fmt(n_dead)}")
    print(f"  LIVE (total - expired)     : {fmt(n_live)}")

    print("\n--- supply by relevance tier (all docs, then live) ---")
    print("  tier 1 = early-career AND target function (the feed default)")
    print("  tier 2 = early-career, outside target functions")
    print("  tier 3 = neither — HIDDEN from the feed by default\n")
    print(f"  {'tier':<8}{'all':>10}{'expired':>10}{'live':>10}")
    live_tiers = {}
    for t in (1, 2, 3):
        all_t = count(tier(t))
        dead_t = count(tier(t), dead)
        live_t = (all_t - dead_t) if (all_t is not None and dead_t is not None) else None
        live_tiers[t] = live_t
        print(f"  tier {t}  {fmt(all_t):>10}{fmt(dead_t):>10}{fmt(live_t):>10}")
    if live_tiers.get(1) is not None and live_tiers.get(2) is not None:
        early = live_tiers[1] + live_tiers[2]
        pct = (100.0 * early / n_live) if n_live else 0
        print(f"\n  EARLY-CAREER VISIBLE IN FEED (live t1+t2): {early:,}  "
              f"({pct:.1f}% of live)")

    print("\n--- internships ---")
    # `employment_type` is not written on the crawl path (same absent-field
    # problem as `expired`), so it always counts 0. Internships are identified
    # from the TITLE at tiering time (quality_gate._is_intern_role falls back to
    # "intern" / "co-op" / "summer analyst" in the title), so count the same way
    # via `search_terms`, which is tokenized from title + company + location.
    all_i = count(intern)
    print(f"  employment_type == INTERNSHIP : {fmt(all_i)}"
          f"   <- field not stored; ignore this number")
    print("  by title token (search_terms array_contains):")
    for term in ("intern", "internship", "interns", "co-op", "coop"):
        f = FieldFilter("search_terms", "array_contains", term)
        all_t = count(f)
        dead_t = count(f, dead)
        live_t = (all_t - dead_t) if (all_t is not None and dead_t is not None) else None
        print(f"    {term:<12} all={fmt(all_t):>8}  live={fmt(live_t):>8}")

    _print_sources(count, fmt, dead, tier, intern)

    # Field shape — the only place we read document bodies. Proves whether
    # `expired`, `relevance_tier` and `employment_type` are actually present,
    # which is what made the first version of this script report 0 live jobs.
    print("\n--- FIELD SHAPE (5 sample docs) ---")
    for snap in col.limit(5).stream():
        d = snap.to_dict() or {}
        print(f"  {snap.id[:46]:<46} "
              f"expired={d.get('expired')!r:<7} "
              f"tier={d.get('relevance_tier')!r:<5} "
              f"emp={d.get('employment_type')!r:<13} "
              f"src={d.get('source')!r}")
    missing = sum(1 for s in col.limit(50).stream()
                  if "expired" not in (s.to_dict() or {}))
    print(f"\n  docs missing `expired` entirely, of 50 sampled: {missing}/50")
    if missing:
        print("  → confirms the absent-field problem; live counts are derived, not filtered.")


if __name__ == "__main__":
    main()
