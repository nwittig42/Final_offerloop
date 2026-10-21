"""Hand-maintained jobs for employer sites we can't crawl.

Reads backend/pipeline/data/manual_jobs.csv (company,title,location,apply_url,
department,analyst_entry) and returns one raw job per row. The only slug is
"all". Deleting a row expires that job on the next crawl through the normal
board diff. No network access.
"""
from __future__ import annotations

import csv
import hashlib
import logging
from pathlib import Path

from backend.pipeline.direct import FetchError, make_job_id

logger = logging.getLogger(__name__)

CSV_PATH = Path(__file__).resolve().parent.parent / "data" / "manual_jobs.csv"
_REQUIRED = ("company", "title", "location", "apply_url")


def fetch(slug: str) -> list[dict]:
    if slug != "all":
        raise FetchError(f"manual only supports slug 'all', got {slug!r}")
    try:
        with CSV_PATH.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError as exc:
        raise FetchError(f"cannot read {CSV_PATH}: {exc}") from exc

    jobs: list[dict] = []
    seen: set[str] = set()
    for n, row in enumerate(rows, start=2):
        row = {k: (v or "").strip() for k, v in row.items() if k}
        missing = [c for c in _REQUIRED if not row.get(c)]
        if missing:
            raise FetchError(f"manual_jobs.csv line {n}: missing {', '.join(missing)}")
        url = row["apply_url"]
        try:
            job_id = make_job_id("manual", "all", hashlib.sha1(url.encode()).hexdigest()[:12])
        except ValueError as exc:
            raise FetchError(str(exc)) from exc
        if job_id in seen:
            continue
        seen.add(job_id)
        jobs.append({
            "job_id": job_id,
            "source": "manual",
            "board_platform": "manual",
            "board_slug": "all",
            "title": row["title"],
            "company": row["company"],
            "employer_logo": None,
            "location": row["location"],
            "remote": False,
            "description_raw": (
                f"{row['title']} at {row['company']}. "
                "Apply on the company's careers site."),
            "apply_url": url,
            "posted_at": None,
            "salary_min": None,
            "salary_max": None,
            "salary_period": None,
            "department": row.get("department", ""),
            "team": "",
            "analyst_entry": row.get("analyst_entry") == "1",
        })
    logger.info("  manual [all] -> %d jobs", len(jobs))
    return jobs
