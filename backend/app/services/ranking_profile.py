"""Ranking profile: everything we know about a student, in the shape the
people ranker scores against (OFF-41).

The deck used to read two fields, dreamCompanies and academics.school.
Students tell us far more than that, just not in those two fields:

  * Saved jobs name firms they are applying to.
  * Settings holds preferred roles and grad year.
  * Their own pipeline says which firms and role families actually turned
    into replies, meetings and connections, the outcome we are optimizing.
  * Left swipes say which firms they keep passing on.

build_ranking_profile() folds those into one dict. Pure functions do the
folding so they test without Firestore; load_ranking_profile() is the thin
reader. Every read is best-effort: a failed read leaves that signal empty
and never breaks the deck.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone
from typing import Iterable, Optional

from app.services.firm_cache.schema import slugify_company, slugify_school
from app.services.student_job_ranker import _title_role_families
from app.utils.users import get_user_school

logger = logging.getLogger(__name__)


# ── Tunables ──────────────────────────────────────────────────────────

# How strongly each source says "this firm is a target". Declared targets
# are the student's own words; a saved job is a softer signal.
TARGET_STRENGTH = {
    "declared": 1.0,
    "saved_job": 0.5,
}
MAX_TARGET_FIRMS = 10          # bounds the per-deck Firestore fan-out

# Pipeline stage → outcome weight. The product goal is a fostered
# connection, so a meeting or a connection outweighs everything else, and
# a reply outweighs a send.
STAGE_WEIGHT = {
    "new":              1.0,
    "draft_created":    1.0,
    "draft_deleted":    0.5,
    "email_sent":       2.0,
    "waiting_on_reply": 2.0,
    "no_response":      1.5,
    "replied":          4.0,
    "meeting_scheduled": 8.0,
    "connected":        8.0,
    "bounced":          0.0,
    "closed":           1.0,
}
AFFINITY_HALF_LIFE_DAYS = 60.0

# A firm becomes a negative after this many passes with no positive signal.
NEGATIVE_MIN_PASSES = 3

MAX_SAVED_JOBS = 50
MAX_PREFS = 500


# ── Pure folding helpers ──────────────────────────────────────────────


def _names(raw) -> list[str]:
    """Strings or {"name": ...} dicts, or a comma string → clean names."""
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [p for p in raw.split(",")]
    out: list[str] = []
    for item in raw:
        name = item.get("name") if isinstance(item, dict) else item
        name = str(name or "").strip()
        if name:
            out.append(name)
    return out


def collect_targets(
    profile: dict,
    saved_jobs: Iterable[dict] = (),
) -> dict:
    """Target firms keyed by company slug.

    Returns {slug: {"name": display, "strength": float, "sources": [..]}},
    insertion-ordered by strength (declared first), capped at
    MAX_TARGET_FIRMS. A firm named by several sources keeps its strongest
    strength and lists every source.
    """
    targets: dict[str, dict] = {}

    def _add(name: str, source: str) -> None:
        slug = slugify_company(name)
        if not slug:
            return
        strength = TARGET_STRENGTH[source]
        cur = targets.get(slug)
        if cur is None:
            targets[slug] = {"name": name, "strength": strength, "sources": [source]}
            return
        cur["strength"] = max(cur["strength"], strength)
        if source not in cur["sources"]:
            cur["sources"].append(source)

    goals = profile.get("goals") or {}
    for name in _names(goals.get("dreamCompanies") or profile.get("dreamCompanies")):
        _add(name, "declared")
    for job in saved_jobs:
        name = job.get("company")
        if isinstance(name, dict):
            name = name.get("name") or name.get("display_name")
        if name:
            _add(str(name).strip(), "saved_job")

    # Every firm the student declared stays; the cap only trims the
    # inferred ones (saved jobs) so the deck never loses a target
    # it used to read.
    ordered = sorted(targets.items(), key=lambda kv: -kv[1]["strength"])
    declared = [kv for kv in ordered if "declared" in kv[1]["sources"]]
    inferred = [kv for kv in ordered if "declared" not in kv[1]["sources"]]
    room = max(0, MAX_TARGET_FIRMS - len(declared))
    return dict(declared + inferred[:room])


def collect_target_roles(profile: dict) -> list[str]:
    """Role phrases the student is hunting for, deduped, original casing."""
    goals = profile.get("goals") or {}
    raw: list[str] = []
    for key in ("targetRoles", "target_roles", "preferredJobRolesOrTitles", "preferredJobRole"):
        raw += _names(goals.get(key) or profile.get(key))
    seen: set[str] = set()
    out: list[str] = []
    for r in raw:
        k = r.lower()
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out[:8]


def recruiting_stage(profile: dict, now: Optional[datetime] = None) -> str:
    """"explore" two or more years out, "recruit" inside a year of
    graduating (or just graduated), "" when grad year is unknown."""
    now = now or datetime.now(timezone.utc)
    academics = profile.get("academics") or {}
    raw = (academics.get("graduationYear") or profile.get("graduationYear")
           or profile.get("gradYear") or profile.get("classYear") or "")
    m = re.search(r"(20\d\d)", str(raw))
    if not m:
        return ""
    years_left = int(m.group(1)) - now.year
    if years_left >= 2:
        return "explore"
    if years_left >= -1:
        return "recruit"
    return ""


def _parse_ts(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _decay(ts: Optional[datetime], now: datetime) -> float:
    if ts is None:
        return 0.5   # undated rows count, but less than fresh ones
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    return math.pow(0.5, age_days / AFFINITY_HALF_LIFE_DAYS)


def affinity_from_contacts(
    contacts: Iterable[dict],
    now: Optional[datetime] = None,
) -> tuple[dict, dict]:
    """(company_affinity, role_family_affinity) from the student's own
    pipeline, weighted by how far each contact got and decayed by age."""
    now = now or datetime.now(timezone.utc)
    by_company: dict[str, float] = {}
    by_family: dict[str, float] = {}
    for c in contacts:
        stage = (c.get("pipelineStage") or "new").strip()
        weight = STAGE_WEIGHT.get(stage, 1.0)
        if weight <= 0:
            continue
        ts = _parse_ts(c.get("updatedAt") or c.get("createdAt") or c.get("savedAt"))
        w = weight * _decay(ts, now)
        company = slugify_company(c.get("company") or c.get("Company") or "")
        if company:
            by_company[company] = by_company.get(company, 0.0) + w
        title = c.get("jobTitle") or c.get("title") or c.get("Title") or ""
        for fam in _title_role_families(title):
            by_family[fam] = by_family.get(fam, 0.0) + w
    return by_company, by_family


def negatives_from_prefs(prefs: Iterable[dict], positive_companies: Iterable[str]) -> set[str]:
    """Firm slugs the student keeps passing on and has never engaged with."""
    positive = set(positive_companies)
    passes: dict[str, int] = {}
    for p in prefs:
        if p.get("signal") not in ("left", "skip"):
            continue
        slug = p.get("company") or ""
        if slug:
            passes[slug] = passes.get(slug, 0) + 1
    return {s for s, n in passes.items() if n >= NEGATIVE_MIN_PASSES and s not in positive}


def build_ranking_profile(
    profile: dict,
    *,
    saved_jobs: Iterable[dict] = (),
    contacts: Iterable[dict] = (),
    prefs: Iterable[dict] = (),
    now: Optional[datetime] = None,
) -> dict:
    contacts = list(contacts)
    school = get_user_school(profile) or ""
    school_slug = slugify_school(school) if school else ""
    company_aff, family_aff = affinity_from_contacts(contacts, now=now)
    return {
        "school":            school,
        "school_slugs":      [school_slug] if school_slug else [],
        "targets":           collect_targets(profile, saved_jobs),
        "target_roles":      collect_target_roles(profile),
        "stage":             recruiting_stage(profile, now=now),
        "company_affinity":  company_aff,
        "family_affinity":   family_aff,
        "negative_companies": negatives_from_prefs(prefs, company_aff.keys()),
    }


def empty_ranking_profile(profile: Optional[dict] = None) -> dict:
    return build_ranking_profile(profile or {})


# ── Firestore reader ──────────────────────────────────────────────────


def _stream(query, limit: int) -> list[dict]:
    try:
        return [s.to_dict() or {} for s in query.limit(limit).stream()]
    except Exception as exc:
        logger.debug("ranking_profile: read failed: %s", exc)
        return []


def load_ranking_profile(uid: str, db, profile: dict, contacts: Optional[list] = None) -> dict:
    """Read the student's saved jobs, swipes (and contacts unless
    the caller already streamed them) and build the profile. Never raises."""
    try:
        from concurrent.futures import ThreadPoolExecutor
        user_ref = db.collection("users").document(uid)
        reads = {
            "saved":  (user_ref.collection("savedJobs"), MAX_SAVED_JOBS),
            "prefs":  (user_ref.collection("peoplePreferences"), MAX_PREFS),
        }
        if contacts is None:
            reads["contacts"] = (user_ref.collection("contacts"), 2000)
        # Parallel so the deck pays one round trip, not three.
        with ThreadPoolExecutor(max_workers=len(reads)) as pool:
            futures = {k: pool.submit(_stream, q, n) for k, (q, n) in reads.items()}
            got = {k: f.result() for k, f in futures.items()}
        saved_jobs, prefs = got["saved"], got["prefs"]
        contacts = got.get("contacts", contacts)
    except Exception as exc:
        logger.warning("ranking_profile: load failed uid=%s: %s", uid, exc)
        return empty_ranking_profile(profile)
    try:
        return build_ranking_profile(
            profile, saved_jobs=saved_jobs, contacts=contacts, prefs=prefs,
        )
    except Exception:
        logger.exception("ranking_profile: build failed uid=%s", uid)
        return empty_ranking_profile(profile)
