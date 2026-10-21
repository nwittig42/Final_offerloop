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
from typing import Callable
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
]


class FetchError(Exception):
    """A board could not be read in full. Never return a partial list instead."""


LEGACY_PLATFORMS = ("greenhouse", "lever", "ashby")
TARGET_PLATFORMS = ("workday", "oracle", "oleeo")
ALL_PLATFORMS = LEGACY_PLATFORMS + TARGET_PLATFORMS

_SLUG_RE = re.compile(r"^[a-z0-9-]+$")
_EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9-]+$")


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
    """Hostnames of every board URL in the target-platform CSVs.

    Empty when the CSVs don't exist yet.
    """
    from backend.pipeline import slug_loader

    hosts: set[str] = set()
    for platform in TARGET_PLATFORMS:
        for rec in slug_loader.load_all_records(platform):
            host = (urlparse(rec.get("url") or "").hostname or "").lower()
            if host:
                hosts.add(host)
    return frozenset(hosts)
