"""Registry of direct job sources crawled by `main.run_crawl_ats`.

Legacy platforms (greenhouse / lever / ashby) use the fetchers in
`backend.pipeline.fetcher`. Target-employer platforms (workday / oracle /
oleeo) each live in their own module here (`direct/workday.py` etc.) and
expose `fetch(slug) -> list[dict]`, raising `FetchError` when a board can't
be read in full.

Import rules (avoid cycles):
  - `fetcher` is imported lazily inside `get_fetcher`, never at module level,
    because `fetcher` may import this package.
  - `quality_gate` is imported here; `quality_gate` must never import `direct`.
"""
from __future__ import annotations

import importlib
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, TypeVar
from urllib.parse import urlparse

from backend.pipeline.quality_gate import BOARD_DIFF_SOURCES, is_candidate_title

__all__ = [
    "FetchError",
    "LEGACY_PLATFORMS",
    "TARGET_PLATFORMS",
    "ALL_PLATFORMS",
    "BOARD_DIFF_SOURCES",
    "is_candidate_title",
    "get_fetcher",
    "make_job_id",
    "crawled_hosts",
    "DETAIL_WORKERS",
    "SKIPPABLE_STATUSES",
    "map_in_order",
    "check_skip_ceiling",
]

_T = TypeVar("_T")
_R = TypeVar("_R")

# Concurrent per-posting detail calls within one board (Workday / Oracle).
DETAIL_WORKERS = 4
# A per-posting detail/vacancy call with one of these statuses means the
# posting is gone or not publicly viewable: skip it (it will drop off the
# board) instead of failing the whole board. Any other failure still raises.
SKIPPABLE_STATUSES = frozenset({403, 404, 410})
# ...unless too many are skipped, which looks like the board blocking us.
_SKIP_CEILING_RATIO = 0.10
_SKIP_CEILING_MIN = 3


class FetchError(Exception):
    """A board could not be read in full. Never return a partial list instead."""


LEGACY_PLATFORMS = ("greenhouse", "lever", "ashby")
TARGET_PLATFORMS = ("workday", "oracle", "oleeo", "goldman")
ALL_PLATFORMS = LEGACY_PLATFORMS + TARGET_PLATFORMS

_SLUG_RE = re.compile(r"^[a-z0-9-]+$")
_EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9-]+$")


def map_in_order(fn: Callable[[_T], _R], items: Iterable[_T],
                 workers: int = DETAIL_WORKERS) -> list[_R]:
    """`[fn(x) for x in items]` run on `workers` threads, results in input
    order. The first exception is re-raised and calls not yet started are
    cancelled."""
    items = list(items)
    if not items:
        return []
    pool = ThreadPoolExecutor(max_workers=min(workers, len(items)))
    futures = [pool.submit(fn, item) for item in items]
    try:
        return [f.result() for f in futures]
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def check_skip_ceiling(what: str, skipped: int, candidates: int) -> None:
    """Raise FetchError when skipped postings exceed 10% of the board's
    candidates AND number more than 3 (the board is likely blocking us)."""
    if skipped > _SKIP_CEILING_MIN and skipped > _SKIP_CEILING_RATIO * candidates:
        raise FetchError(
            f"{what}: {skipped} of {candidates} postings returned 403/404/410; "
            "the board is likely blocking us")


def get_fetcher(platform: str) -> Callable[[str], list[dict]]:
    """The per-board fetch function for `platform`: `fetch(slug) -> list[dict]`."""
    if platform in LEGACY_PLATFORMS:
        from backend.pipeline import fetcher

        return getattr(fetcher, f"_fetch_{platform}")
    if platform in TARGET_PLATFORMS:
        module = importlib.import_module(f"backend.pipeline.direct.{platform}")
        return module.fetch
    raise ValueError(f"unknown direct platform: {platform!r}")


def make_job_id(platform: str, slug: str, external_id: str) -> str:
    """`direct_{platform}_{slug}_{external_id}`, validated so
    `writer._ats_board` can parse it back into (platform, slug)."""
    if platform not in ALL_PLATFORMS:
        raise ValueError(f"unknown direct platform: {platform!r}")
    if not _SLUG_RE.match(slug or ""):
        raise ValueError(f"bad slug {slug!r}: must match {_SLUG_RE.pattern}")
    external_id = str(external_id)
    if not _EXTERNAL_ID_RE.match(external_id):
        raise ValueError(
            f"bad external id {external_id!r}: must match {_EXTERNAL_ID_RE.pattern}"
        )
    return f"direct_{platform}_{slug}_{external_id}"


def crawled_hosts() -> frozenset[str]:
    """Hostnames of the HOT-tier target-platform boards (CSV rows whose
    `{platform}:{slug}` is in hot_slugs.txt), i.e. the boards the scheduled
    crawl actually visits. Simplify listings on these hosts are skipped as
    duplicates. Cold-tier rows are excluded: they are crawled only on manual
    dispatch, so their Simplify listings must be kept.

    Empty when the CSVs don't exist yet.
    """
    from backend.pipeline import slug_loader

    hosts: set[str] = set()
    for platform in TARGET_PLATFORMS:
        hot = set(slug_loader.load_slugs(platform, "hot"))
        for rec in slug_loader.load_all_records(platform):
            if rec.get("slug") not in hot:
                continue
            host = (urlparse(rec.get("url") or "").hostname or "").lower()
            if host:
                hosts.add(host)
    return frozenset(hosts)
