"""iOS ranker — scored + diversified feed of people to reach out to.

Pure algorithm module. HTTP surface lives in routes/ranker.py. Rylan's
swipe-deck client hits that endpoint only; nothing in this file is
client-aware.

Data flow
---------
    uid
     → _load_user_context(uid, db)      user profile + resume in scorer shape
     → _already_known_keys(uid, db)     (first, last, company) exclude set
     → _fetch_candidates(user_ctx, db)  firm_employees query, App-shape dicts
     → _score_one(user_ctx, c)          {score, tier, reasons, briefing}
     → _diversify_people(scored, k)     MMR re-rank on company + role family
     → _to_response_dict(…)             trim to response contract (allowlist)

Sensitive-field posture
-----------------------
Three explicit allowlists sit between raw PDL data and Rylan's client:

  1. `firm_cache/schema.py:firm_employee_doc_from_pdl_person` — writer
     allowlist. Builds each firm_employees doc from a fixed 19-field set.
     sex / birth_date / birth_year never appear (grep-verified).
  2. `contacts.py:_RANKER_FIELDS` — Commit 3 write-path allowlist for
     the user's own contacts collection.
  3. `_to_response_dict` in this module — response-shape allowlist. Even
     if firm_employees schema drifts later, only fields named on the
     person-shape allowlist reach the client.

Known gaps (documented; not addressed here)
-------------------------------------------
* HometownSignal (extracted in Commit 2, persisted on user contacts in
  Commit 3) is NOT USED in scoring today. Two blockers:
    - firm_cache/schema.py's writer predates Commit 2 and does not
      persist hometownSignal on firm_employees docs. Candidates queried
      by this module therefore lack the verified hometown field.
    - warmth_scoring._score_shared_identity's `same_hometown` (+8)
      substring-matches user.hometown (from resume) against contact
      City/location. Policy-safe (see coffee_chat_prep.detect_commonality
      lines 76-86 — never derived from name or ethnicity) but weak.
  Wiring HometownSignal in requires: (i) extend firm_cache writer
  allowlist, (ii) update warmth_scoring.same_hometown to prefer
  contact.hometownSignal when present, (iii) optional backfill. Filed
  as a follow-up if ranking quality demands it.

* The write-path allowlist in contact_import.py and
  mcp_server/persist.py is not updated for the 9 typed ranker fields
  either — see Commit 3 message. Contacts written via those two paths
  won't carry ranker features on the user's own contacts collection.
  Not a person_ranker blocker (candidate source is firm_employees,
  not the user's contacts) but noted alongside for symmetry.

Reaching into private helpers
-----------------------------
This module imports leading-underscore helpers from personalization,
warmth_scoring, and student_job_ranker (e.g. `_detect_all_signals`,
`_upgrade_warmth_tier`, `_title_role_families`, `_normalize_company`,
`_build_user_comparison_data`). They are the shared implementations
used by the existing search / warmth / job flows; person_ranker uses
the same primitives so scoring is consistent across surfaces. If the
private-API coupling becomes painful, refactor those helpers to a
public shared module in a follow-up — do not fork the logic here.
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import re
from datetime import datetime, timezone
from typing import Optional

from google.cloud.firestore_v1.field_path import FieldPath

from app.services.firm_cache.schema import (
    FIRM_EMPLOYEES_COLLECTION,
    canonicalize_linkedin_url,
    slugify_company,
    slugify_school,
)
from app.services.firm_cache.reader import firm_employee_doc_to_app_contact
from app.services.ranking_profile import load_ranking_profile
from app.services.student_job_ranker import _title_role_families
from app.utils.personalization import (
    _LEAD_TYPE_RANK,
    _detect_all_signals,
    _normalize_company,
    _upgrade_warmth_tier,
    build_contact_profile,
    build_user_profile,
)
from app.utils.users import get_user_school
from app.utils.warmth_scoring import (
    _build_user_comparison_data,
    build_briefing_line,
    compute_warmth_score,
)

logger = logging.getLogger(__name__)


# ── Tunables ──────────────────────────────────────────────────────────
# Warmth signal weights live in warmth_scoring.py (SHARED_IDENTITY_CAP,
# per-signal points, TIER_THRESHOLDS). Lead-type priorities live in
# personalization.py (LEAD_TYPE_PRIORITY, _TIER_UPGRADES). REUSE those —
# do not redefine. Only pool + diversify tunables live here.

DEFAULT_CANDIDATE_POOL_CAP = 400   # firm_employees docs pulled per query
DEFAULT_RANKED_LIMIT       = 50    # cap on response length
MAX_RANKED_LIMIT           = 100   # HTTP-level ceiling

# MMR diversify penalties (ported from student_job_ranker.diversify).
LAMBDA_COMPANY     = 3.0           # squared penalty per repeat company
LAMBDA_ROLE_FAMILY = 0.5           # squared penalty per repeat role family

# Personalized ranking (OFF-41). v1 adds points from the student's ranking
# profile (ranking_profile.py) on top of warmth; v0 is warmth alone. The
# split is by uid hash so a student always sees the same ranker and the
# deck's events carry which one made the deck. PERSON_RANKER_V1_PERCENT=0
# turns v1 off, 100 turns it on for everyone.
V1_PERCENT_ENV = "PERSON_RANKER_V1_PERCENT"
V1_PERCENT_DEFAULT = 50

TARGET_FIRM_POINTS   = 12   # × source strength; declared firms and saved jobs
TARGET_ROLE_POINTS   = 10   # title matches a role the student is hunting
STAGE_FIT_POINTS     = 6    # the right kind of person for their year
COMPANY_AFFINITY_CAP = 12   # firms that already turned into replies/meetings
FAMILY_AFFINITY_CAP  = 8    # role families that did
NEGATIVE_FIRM_POINTS = 15   # firms they keep passing on

_JUNIOR_RE = re.compile(r"\b(analyst|associate|consultant|engineer|intern|developer|specialist)\b", re.I)


# ── Public entry point ────────────────────────────────────────────────


def rank_people_for_user(
    uid: str,
    db,
    limit: int = DEFAULT_RANKED_LIMIT,
    exclude_ids: set | None = None,
    company: str | None = None,
    role: str | None = None,
    focus: str | None = None,
    features_out: dict | None = None,
    max_limit: int = MAX_RANKED_LIMIT,
) -> list[dict]:
    """Return a diversified, ranked feed of people uid should reach out to.

    features_out, when given, is filled with {candidate_id: features} for
    every person returned, so the route can log what the ranker saw.
    max_limit raises the HTTP ceiling for the background deck build
    (people_deck_cache), which saves a longer list than one response holds.

    Response element shape:
      {
        "person": {
          "id":          <linkedin-slug or pdlId>,   # non-null by construction
          "firstName", "lastName",
          "linkedinUrl", "pdlId",
          "company", "title",
          "college", "city", "state"
        },
        "score":    int,     # raw warmth score, uncapped
        "tier":     str,     # "warm" | "neutral" | "cold" after lead-type upgrade
        "reasons":  list,    # ordered by personalization.LEAD_TYPE_PRIORITY
        "briefing": str      # single-sentence pitch (may be "")
      }

    Returns [] on:
      * sparse profile (no dreamCompanies AND no academics.school) —
        logged so we can track how often this fires
      * no firm_employees hits for the user's signals
      * unexpected Firestore / scorer errors (never raises — the deck
        contract requires an empty deck rather than a 500)
    """
    limit = max(1, min(int(limit or DEFAULT_RANKED_LIMIT), max(MAX_RANKED_LIMIT, int(max_limit))))

    user_ctx = _load_user_context(uid, db)
    if user_ctx is None:
        return []

    # Company deck (2026-09-07): the Chrome extension's Company tab and the
    # website's company pages ask for ONE firm. Same scorer, same warehouse,
    # same reasons; only the candidate source narrows to that firm. The
    # student's school still floats alumni to the top. Diversification is
    # skipped below (it soft-caps repeat companies, which here is all of
    # them).
    if company:
        _slug = slugify_company(company)
        if not _slug:
            return []
        user_ctx["dream_company_names"] = [company]
        user_ctx["dream_company_slugs"] = [_slug]
        user_ctx["company_filter"] = _slug

    # Role filter: what the student typed ("analyst", "recruiter") or a chip.
    # Feeds the warmth scorer's role_match so a matching title earns points,
    # and the final sort puts matching titles first, everyone else after.
    role = (role or "").strip()
    if role:
        user_ctx["role_filter"] = role
        try:
            user_ctx["comparison"]["search_titles"] = [role.lower()]
        except Exception:
            pass

    # Focus chips (2026-09-07): what a student is usually hunting for at a
    # company. "alumni" narrows the candidate pool to the student's school;
    # "recent" floats people who joined in the last two years (the
    # near-peers who just went through the process); "managers" floats the
    # people who hire. Each is an ordering, never a hard drop, except alumni.
    focus = (focus or "").strip().lower()
    if focus not in ("alumni", "recent", "managers"):
        focus = ""
    user_ctx["focus"] = focus

    variant = ranker_variant(uid)
    user_ctx["variant"] = variant
    if variant == "v1":
        rprofile = load_ranking_profile(uid, db, user_ctx["profile"])
        user_ctx["ranking_profile"] = rprofile
        if not company and rprofile["targets"]:
            # Saved jobs name firms too, and most students never fill
            # dreamCompanies in Settings. Source from both.
            user_ctx["dream_company_names"] = [t["name"] for t in rprofile["targets"].values()]
            user_ctx["dream_company_slugs"] = list(rprofile["targets"])

    if not user_ctx["dream_company_slugs"] and not user_ctx["school_slugs"]:
        # Brand-new accounts have neither (onboarding does not ask for target
        # companies yet), and every one of them opened an empty deck on day
        # one (Rylan 2026-09-03). Serve the firms most students target, the
        # same demand ranking the nightly warm stocks, so the first deck is
        # never blank. The scorer still ranks by the user's roles/industries.
        try:
            from app.services.warehouse_warm import _demand_ranked_firms
            seeds = [disp for disp, _slug, _score in _demand_ranked_firms(db)[:12]]
        except ImportError:
            # warehouse_warm (the nightly demand ranking) only exists on the
            # mobile backend. Without it every brand-new student opened an
            # empty deck; fall back to the firms students in their lane
            # most often network into.
            seeds = default_seed_firms(user_ctx["profile"])
        except Exception:
            logger.exception("person_ranker: seed-firm fallback failed uid=%s", uid)
            seeds = default_seed_firms(user_ctx["profile"])
        if not seeds:
            logger.info("person_ranker: sparse profile uid=%s and no seeds, returning empty deck", uid)
            return []
        user_ctx["dream_company_names"] = list(seeds)
        user_ctx["dream_company_slugs"] = [s for s in (slugify_company(n) for n in seeds) if s]
        user_ctx["seed_fallback"] = True
        logger.info(
            "person_ranker: sparse profile uid=%s, falling back to %d seed firms",
            uid, len(user_ctx["dream_company_slugs"]),
        )

    # Deck opens are demand too (2026-08-31): the nightly warm ranks firms
    # by what people search AND what decks are hungry for.
    try:
        from app.services.warehouse_metrics import log_demand
        log_demand({"companies": user_ctx["dream_company_slugs"],
                    "schools": user_ctx["school_slugs"]}, "people_deck")
    except Exception:
        pass

    exclude_keys = _already_known_keys(uid, db)
    swiped_ids = _swiped_person_ids(uid, db)

    candidates = _fetch_candidates(
        user_ctx,
        db,
        cap=DEFAULT_CANDIDATE_POOL_CAP,
        exclude_keys=exclude_keys,
        alumni_only=(user_ctx.get("focus") == "alumni"),
    )
    if not candidates:
        logger.info(
            "person_ranker: no firm_employees hits uid=%s "
            "dreamCompanies=%d schools=%d — returning empty deck",
            uid,
            len(user_ctx["dream_company_slugs"]),
            len(user_ctx["school_slugs"]),
        )
        return []

    own_id = own_candidate_id(user_ctx.get("profile") or {})
    scored: list[dict] = []
    for cand in candidates:
        _id = _candidate_id(cand)
        if own_id and _id == own_id:
            # Never recommend a user to themselves.
            continue
        if _id is None:
            # Drop: Rylan uses id as a render key; a null or dup breaks the deck.
            continue
        if _id in swiped_ids or (exclude_ids and _id in exclude_ids):
            # User has already swiped left/skip on this person in a
            # prior deck. peoplePreferences is the "don't show again"
            # source of truth.
            continue
        item = _score_one(user_ctx, cand)
        if item is None:
            continue
        # "score" stays the warmth score every client already draws (the
        # app turns it into a 1-10 badge). v1's personal points only decide
        # the ORDER, through rank_score, so cards look the same in both arms.
        item["rank_score"] = item["score"]
        item["features"] = {"warmth": item["score"]}
        if variant == "v1":
            pts, feats = _personal_points(user_ctx, cand, item)
            item["rank_score"] += pts
            item["features"].update(feats)
        item["id"] = _id
        item["candidate"] = cand
        scored.append(item)

    role_filter = user_ctx.get("role_filter") or ""
    focus = user_ctx.get("focus") or ""
    this_year = datetime.now(timezone.utc).year
    for item in scored:
        title = item["candidate"].get("Title") or ""
        item["_role_hit"] = _role_hit(title, role_filter) if role_filter else False
        joined = item["candidate"].get("JoinedYear")
        try:
            joined = int(joined) if joined else None
        except (TypeError, ValueError):
            joined = None
        item["_recent"] = bool(joined and joined >= this_year - 1)
        item["_manager"] = bool(_MANAGER_RE.search(title))
    if role_filter or focus:
        # Typed role first, then the focus, then warmth. Each is an
        # ordering, so a thin company still shows everyone it has.
        scored.sort(key=lambda x: (
            0 if x.get("_role_hit") else 1,
            0 if (focus == "recent" and x.get("_recent")) or (focus == "managers" and x.get("_manager")) else 1,
            -x["rank_score"],
        ))
    else:
        scored.sort(key=lambda x: x["rank_score"], reverse=True)

    if user_ctx.get("company_filter"):
        picked = scored[:limit]
    else:
        picked = _diversify_people(scored, k=limit)
    out = []
    for x in picked:
        if features_out is not None:
            features_out[x["id"]] = dict(x.get("features") or {}, rank_score=x["rank_score"], tier=x["tier"])
        d = _to_response_dict(x)
        if role_filter:
            d["role_match"] = bool(x.get("_role_hit"))
        if focus:
            d["focus_match"] = bool(
                (focus == "recent" and x.get("_recent"))
                or (focus == "managers" and x.get("_manager"))
                or focus == "alumni"
            )
        out.append(d)
    return out


# Titles that hire or run a team. Used by the "managers" focus.
_MANAGER_RE = re.compile(
    r"\b(manager|director|head of|head,|lead|vp|vice president|principal|partner|chief)\b",
    re.I,
)


def suggest_chips(user_ctx: Optional[dict]) -> list[dict]:
    """The chips the Company tab shows, predicted from the student's
    profile: their year decides the order, their school names the alumni
    chip, their saved roles become chips of their own.

    Each chip is {"label", "role"?: str, "focus"?: str}. Recruiters is always
    there (the most-tapped hunt). Never raises; a bare default on any
    failure.
    """
    default = [
        {"label": "Alumni", "focus": "alumni"},
        {"label": "Recent hires", "focus": "recent"},
        {"label": "Recruiters", "role": "recruiter"},
        {"label": "Hiring managers", "focus": "managers"},
    ]
    try:
        profile = (user_ctx or {}).get("profile") or {}
        academics = profile.get("academics") or {}
        goals = profile.get("goals") or {}

        school = (academics.get("university") or academics.get("school")
                  or profile.get("university") or profile.get("school") or "").strip()
        alumni_label = "Alumni"
        if school:
            short = school
            low = school.lower()
            if "southern california" in low:
                short = "USC"
            elif len(school) > 18:
                short = "Alumni"
            alumni_label = f"{short} alumni" if short != "Alumni" else "Alumni"

        grad_raw = (academics.get("graduationYear") or profile.get("graduationYear")
                    or profile.get("gradYear") or profile.get("classYear") or "")
        years_left = None
        try:
            m = re.search(r"(20\d\d)", str(grad_raw))
            if m:
                years_left = int(m.group(1)) - datetime.now(timezone.utc).year
        except Exception:
            years_left = None

        roles: list[str] = []
        for key in ("targetRoles", "target_roles", "careerInterests"):
            raw = goals.get(key) or profile.get(key) or []
            if isinstance(raw, str):
                raw = [raw]
            for r in raw:
                name = (r.get("name") if isinstance(r, dict) else r) or ""
                name = str(name).strip()
                if name and name.lower() not in {x.lower() for x in roles}:
                    roles.append(name)
        role_chips = [{"label": r[:28], "role": r} for r in roles[:2]]

        alumni = {"label": alumni_label, "focus": "alumni"}
        recent = {"label": "Recent hires", "focus": "recent"}
        recruiters = {"label": "Recruiters", "role": "recruiter"}
        managers = {"label": "Hiring managers", "focus": "managers"}

        if years_left is not None and years_left >= 2:
            # First and second years: warm intros and near-peers, not hiring.
            order = [alumni, recent, recruiters] + role_chips + [managers]
        elif years_left is not None and years_left >= 0:
            # Junior and senior: the people who just got in and the people who hire.
            order = [recent, alumni, managers, recruiters] + role_chips
        else:
            order = [alumni, managers, recruiters, recent] + role_chips
        return order[:6]
    except Exception:
        logger.exception("person_ranker: suggest_chips failed")
        return default


# Role words that mean the same hunt. "recruiter" is the chip students tap
# most; a title rarely says the word.
_ROLE_SYNONYMS = {
    "recruiter": ("recruit", "talent", "people operations", "people ops",
                  "human resources", "hr ", " hr", "campus", "university relations",
                  "early careers", "early career", "staffing"),
    "recruiters": ("recruit", "talent", "people operations", "people ops",
                   "human resources", "hr ", " hr", "campus", "university relations",
                   "early careers", "early career", "staffing"),
}

_ROLE_STOPWORDS = {"at", "of", "in", "the", "a", "an", "and", "or", "for", "to"}


def _role_hit(title: str, role: str) -> bool:
    """True when a title plausibly matches the typed role: a synonym family
    for the common chips, else any meaningful token overlap or substring."""
    t = f" {(title or '').lower().strip()} "
    r = (role or "").lower().strip()
    if not t.strip() or not r:
        return False
    for needle in _ROLE_SYNONYMS.get(r, ()):
        if needle in t:
            return True
    if r in t:
        return True
    r_tokens = {tok for tok in r.split() if tok not in _ROLE_STOPWORDS and len(tok) > 2}
    t_tokens = set(t.split())
    return bool(r_tokens & t_tokens)


# ── Loaders ───────────────────────────────────────────────────────────


def _load_user_context(uid: str, db) -> Optional[dict]:
    """Read user profile + resumeParsed. Build the dict both scorers
    accept. None on missing user doc (not sparse — genuinely missing)."""
    try:
        snap = db.collection("users").document(uid).get()
    except Exception as exc:
        logger.warning("person_ranker: user fetch failed for uid=%s: %s", uid, exc)
        return None
    if not snap.exists:
        return None
    profile = snap.to_dict() or {}
    resume_parsed = profile.get("resumeParsed") or {}

    # dreamCompanies live under goals.dreamCompanies or top-level (legacy).
    # Entries may be strings or {"name": ...} dicts.
    dream_companies_raw = (
        (profile.get("goals") or {}).get("dreamCompanies")
        or profile.get("dreamCompanies")
        or []
    )
    dream_company_names: list[str] = []
    dream_company_slugs: list[str] = []
    for c in dream_companies_raw:
        name = c.get("name") if isinstance(c, dict) else c
        if not name:
            continue
        name = str(name).strip()
        if not name:
            continue
        dream_company_names.append(name)
        slug = slugify_company(name)
        if slug and slug not in dream_company_slugs:
            dream_company_slugs.append(slug)

    # Web onboarding writes academics.university; the app writes
    # academics.school. Reading only the app's field left every web student
    # without an alumni candidate pool.
    school = get_user_school(profile)
    school_slugs: list[str] = []
    if school:
        slug = slugify_school(str(school))
        if slug:
            school_slugs.append(slug)

    comparison = _build_user_comparison_data(profile)
    normalized_user = build_user_profile(
        resume_parsed=resume_parsed,
        user_profile=profile,
        personal_note="",
        dream_companies=dream_company_names,
    )

    return {
        "uid":                 uid,
        "profile":             profile,
        "resume_parsed":       resume_parsed,
        "comparison":          comparison,
        "normalized_user":     normalized_user,
        "dream_company_slugs": dream_company_slugs,
        "school_slugs":        school_slugs,
    }


def _already_known_keys(uid: str, db) -> set:
    """(first_lower, last_lower, company_lower) tuples of the user's
    existing contacts — same dedupe key firm_cache.reader uses. Empty
    set on any Firestore error; never breaks the deck."""
    keys: set = set()
    try:
        contacts_ref = db.collection("users").document(uid).collection("contacts")
        for snap in contacts_ref.stream():
            d = snap.to_dict() or {}
            first = (d.get("firstName") or "").lower().strip()
            last = (d.get("lastName") or "").lower().strip()
            company = (d.get("company") or "").lower().strip()
            if first and last:
                keys.add((first, last, company))
    except Exception as exc:
        logger.debug("person_ranker: known-contacts fetch failed: %s", exc)
    return keys


def _swiped_person_ids(uid: str, db) -> set:
    """candidate_ids the user has already swiped LEFT or SKIP — excluded
    from future decks. Swipe-right is excluded elsewhere via
    _already_known_keys (right → saved to contacts → matched by
    (first, last, company)), so we intentionally do NOT read "right"
    signals here — that would double-suppress.

    peoplePreferences docs are keyed on the candidate_id we sent to the
    client (LinkedIn slug preferred, pdlId fallback), so lookup here
    matches the id we'll compute for fresh candidates in rank_people_for_user.

    Empty set on any Firestore error; never breaks the deck.
    """
    ids: set = set()
    try:
        prefs_ref = db.collection("users").document(uid).collection("peoplePreferences")
        for snap in prefs_ref.where("signal", "in", ["left", "skip"]).stream():
            ids.add(snap.id)
    except Exception as exc:
        logger.debug("person_ranker: swiped-ids fetch failed: %s", exc)
    return ids


# ── Candidate fetch ───────────────────────────────────────────────────


def _fetch_candidates(
    user_ctx: dict,
    db,
    cap: int,
    exclude_keys: Optional[set] = None,
    alumni_only: bool = False,
) -> list[dict]:
    """Query firm_employees for candidate people. Uses indexed where()
    only — never in-memory filter on unindexed fields. Excludes anyone
    already in the user's contacts collection.

    Query strategy (highest specificity first, mirrors firm_cache.reader):
      companies + schools → company AND schools array-contains  (composite index)
      companies only      → company                             (single-field index)
      schools only        → schools array-contains              (single-field index)
    """
    exclude_keys = exclude_keys or set()
    companies = user_ctx.get("dream_company_slugs") or []
    schools = user_ctx.get("school_slugs") or []
    if not companies and not schools:
        return []

    seen_ids: set[str] = set()
    docs: list[dict] = []

    def _take(snaps) -> None:
        for snap in snaps:
            _id = snap.id
            if _id in seen_ids:
                continue
            seen_ids.add(_id)
            d = snap.to_dict() or {}
            d["_id"] = _id
            docs.append(d)

    col = db.collection(FIRM_EMPLOYEES_COLLECTION)

    def _stream(query, n: int) -> None:
        """Up to n people from query, starting at a random document id and
        wrapping around. A bare where().limit(n) comes back in document-id
        order (the LinkedIn handle), so a firm with more than n people only
        ever showed its alphabetical head: Goldman's 107 stopped at "k"
        (2026-10-20). The random start keeps every person reachable."""
        try:
            by_id = query.order_by(FieldPath.document_id())
            start = col.document(_random_id_start())
            head = list(by_id.start_at({FieldPath.document_id(): start}).limit(n).stream())
            got = {s.id for s in head}
            if len(head) < n:
                # Wrap: the ids before the start point. Stop at the first
                # repeat, which means the whole set has been read.
                for snap in by_id.limit(n - len(head)).stream():
                    if snap.id in got:
                        break
                    head.append(snap)
            _take(head)
        except Exception as exc:
            logger.debug("person_ranker: firm_employees query failed: %s", exc)

    # Each firm gets a fair share of the pool. One firm at the full cap
    # used to fill the pool before the student's other targets were read.
    per_firm = max(60, cap // max(1, len(companies))) if companies else cap
    if companies and schools:
        for co in companies:
            for sch in schools:
                _stream(
                    col.where("company", "==", co)
                       .where("schools", "array_contains", sch),
                    cap,
                )
        if alumni_only:
            # The alumni chip: only people who share the student's school.
            return [firm_employee_doc_to_app_contact(d) for d in docs
                    if ((d.get("first_name") or "").lower().strip(),
                        (d.get("last_name") or "").lower().strip(),
                        (d.get("company_display") or "").lower().strip()) not in exclude_keys][:cap]
        # Alumni-at-target-firm is gold but rare. This used to be the ONLY
        # query when both were set, so a user with a school saved got NOBODY
        # at their target firms unless the warehouse happened to hold an alum
        # there (Rylan's Stripe well, 2026-08-30: 14 employees banked, zero
        # USC alumni, zero results). Top up with the firm's other people, the
        # warmth scorer still floats any alumni to the top.
        if len(docs) < cap:
            for co in companies:
                _stream(col.where("company", "==", co), per_firm)
        if len(docs) < cap and per_firm < cap:
            for co in companies:
                if len(docs) >= cap:
                    break
                _stream(col.where("company", "==", co), cap)
        if len(docs) < cap:
            for sch in schools:
                _stream(col.where("schools", "array_contains", sch), cap)
    elif companies:
        for co in companies:
            _stream(col.where("company", "==", co), per_firm)
        if len(docs) < cap and per_firm < cap:
            # Some targets were thin: let the deep ones fill the rest.
            for co in companies:
                if len(docs) >= cap:
                    break
                _stream(col.where("company", "==", co), cap)
    else:
        for sch in schools:
            _stream(col.where("schools", "array_contains", sch), cap)

    kept: list[dict] = []
    for doc in docs:
        first = (doc.get("first_name") or "").lower().strip()
        last = (doc.get("last_name") or "").lower().strip()
        company = (doc.get("company_display") or "").lower().strip()
        if (first, last, company) in exclude_keys:
            continue
        kept.append(doc)
        if len(kept) >= cap:
            break

    return [firm_employee_doc_to_app_contact(d) for d in kept]


_ID_CHARS = "0123456789abcdefghijklmnopqrstuvwxyz"


def _random_id_start() -> str:
    """A random point in document-id order. Ids are LinkedIn handles
    (lowercase letters, digits, dashes) or synth: keys."""
    return "".join(random.choice(_ID_CHARS) for _ in range(3))


# ── Scoring ───────────────────────────────────────────────────────────


def _score_one(user_ctx: dict, candidate: dict) -> dict:
    """Compose warmth_scoring + personalization signals for one candidate.

    Returns {score, tier, reasons, briefing}. On internal exception,
    returns a zero-score record rather than dropping the candidate —
    the deck should degrade gracefully on one bad record rather than gap.
    """
    try:
        warmth = compute_warmth_score(user_ctx["comparison"], candidate)
        base_score = int(warmth.get("score") or 0)
        base_tier = warmth.get("tier") or "cold"
        warmth_signals = warmth.get("signals") or []

        contact_profile = build_contact_profile(candidate)
        lead_signals = _detect_all_signals(user_ctx["normalized_user"], contact_profile) or []

        # personalization._detect_all_signals is NOT pre-sorted (per its
        # docstring). Sort by LEAD_TYPE_PRIORITY so the client sees the
        # strongest hook first.
        lead_signals = sorted(
            lead_signals,
            key=lambda s: _LEAD_TYPE_RANK.get(
                s.get("type", "") if isinstance(s, dict) else "",
                len(_LEAD_TYPE_RANK),
            ),
        )

        # Apply lead-type tier upgrades. _upgrade_warmth_tier never
        # downgrades and is monotone-capped, so accumulating is safe.
        tier = base_tier
        for sig in lead_signals:
            lt = sig.get("type") if isinstance(sig, dict) else None
            if lt:
                tier = _upgrade_warmth_tier(tier, lt)

        briefing = build_briefing_line(candidate, warmth_signals) or ""

        return {
            "score":    base_score,
            "tier":     tier,
            "reasons":  lead_signals,
            "briefing": briefing,
            "warmth_signals": [s.get("signal") for s in warmth_signals if isinstance(s, dict)],
        }
    except Exception as exc:
        logger.warning(
            "person_ranker: scoring failed for candidate %s: %s",
            candidate.get("pdlId") or candidate.get("LinkedIn") or "?",
            exc,
        )
        return {"score": 0, "tier": "cold", "reasons": [], "briefing": ""}


# ── Starter firms for a profile with no targets ───────────────────────

# Keyword → the firms students in that lane most often network into. Same
# lanes as the website feed (connect-grow-hire find/people/feedSeeds.ts).
_SEED_LANES = [
    (re.compile(r"invest(ment)? bank|\bib\b|banking|private equity|\bpe\b|hedge fund|asset management|sales (and|&) trading|capital markets|finance", re.I),
     ["Goldman Sachs", "JPMorgan Chase", "Morgan Stanley", "Evercore", "Blackstone", "Bank of America"]),
    (re.compile(r"consult|strategy|management consulting", re.I),
     ["McKinsey & Company", "Boston Consulting Group", "Bain & Company", "Deloitte", "Accenture"]),
    (re.compile(r"account|audit|\btax\b|\bcpa\b|assurance", re.I),
     ["Deloitte", "PwC", "EY", "KPMG"]),
    (re.compile(r"software|engineer|computer science|\bcs\b|tech|product manag|data scien|machine learning|\bai\b", re.I),
     ["Google", "Meta", "Apple", "Amazon", "Microsoft"]),
    (re.compile(r"market|brand|advertis|media|entertainment|consumer", re.I),
     ["Procter & Gamble", "Nike", "The Walt Disney Company", "Netflix", "L'Oréal"]),
    (re.compile(r"real estate", re.I),
     ["CBRE", "JLL", "Blackstone"]),
]
_SEED_DEFAULT_MIX = ["Goldman Sachs", "McKinsey & Company", "Google", "Deloitte",
                     "JPMorgan Chase", "Amazon", "Morgan Stanley", "Boston Consulting Group"]


def default_seed_firms(profile: Optional[dict], max_firms: int = 12) -> list[str]:
    """Firms to start a deck from when the student named none: their lane
    (track, industries, roles, major, resume titles) first, then a broad mix."""
    profile = profile or {}
    goals = profile.get("goals") or {}
    academics = profile.get("academics") or {}
    resume = profile.get("resumeParsed") or {}
    parts: list[str] = []
    for v in (goals.get("careerTrack"), profile.get("careerTrack"), profile.get("preferredJobRole"),
              academics.get("major"), profile.get("major")):
        if v:
            parts.append(str(v))
    for key in ("targetIndustries", "industriesOfInterest", "careerInterests", "preferredJobRolesOrTitles"):
        raw = goals.get(key) or profile.get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        parts += [str(x.get("name") if isinstance(x, dict) else x) for x in raw if x]
    for exp in (resume.get("experience") or [])[:4]:
        if isinstance(exp, dict) and exp.get("title"):
            parts.append(str(exp["title"]))
    text = " | ".join(parts)

    out: list[str] = []
    def _add(name: str) -> None:
        if name.lower() not in {x.lower() for x in out}:
            out.append(name)
    if text:
        for pat, firms in _SEED_LANES:
            if pat.search(text):
                for f in firms:
                    _add(f)
    for f in _SEED_DEFAULT_MIX:
        _add(f)
    return out[:max_firms]


# ── Personalization (v1) ──────────────────────────────────────────────


def ranker_variant(uid: str) -> str:
    """"v1" or "v0" for this student, stable across calls. The bucket is a
    hash of the uid, so the routes can recompute it for event logging
    without a lookup."""
    try:
        pct = int(os.getenv(V1_PERCENT_ENV, V1_PERCENT_DEFAULT))
    except (TypeError, ValueError):
        pct = V1_PERCENT_DEFAULT
    bucket = int(hashlib.sha1(f"person_ranker:{uid}".encode()).hexdigest(), 16) % 100
    return "v1" if bucket < pct else "v0"


def model_version_for(uid: str) -> str:
    return f"person_ranker_{ranker_variant(uid)}"


def _personal_points(user_ctx: dict, cand: dict, item: dict) -> tuple[int, dict]:
    """Points from the ranking profile on top of warmth, plus the features
    that earned them (logged with the deck so outcomes can be traced back
    to what the ranker saw)."""
    prof = user_ctx.get("ranking_profile") or {}
    title = cand.get("Title") or ""
    slug = slugify_company(cand.get("Company") or "")
    feats: dict = {}

    target = (prof.get("targets") or {}).get(slug)
    if target:
        pts = round(TARGET_FIRM_POINTS * float(target.get("strength") or 0))
        if "dream_company" in (item.get("warmth_signals") or []):
            # Warmth already paid +10 for a declared dream firm.
            pts = max(0, pts - 10)
        feats["target_firm"] = pts

    if not user_ctx.get("role_filter"):
        roles = prof.get("target_roles") or []
        if title and any(_role_hit(title, r) for r in roles):
            feats["target_role"] = TARGET_ROLE_POINTS

    joined = cand.get("JoinedYear")
    try:
        joined = int(joined) if joined else None
    except (TypeError, ValueError):
        joined = None
    recent = bool(joined and joined >= datetime.now(timezone.utc).year - 1)
    manager = bool(_MANAGER_RE.search(title))
    stage = prof.get("stage") or ""
    if stage == "recruit":
        # Inside a year of graduating: the people who hire and the people
        # who just went through the process.
        fit = _role_hit(title, "recruiter") or manager or recent
    elif stage == "explore":
        # Freshmen and sophomores: near-peers who will take a coffee chat.
        fit = recent or (bool(_JUNIOR_RE.search(title)) and not manager)
    else:
        fit = False
    if fit:
        feats["stage_fit"] = STAGE_FIT_POINTS

    co_w = (prof.get("company_affinity") or {}).get(slug, 0.0)
    if co_w:
        feats["company_affinity"] = min(COMPANY_AFFINITY_CAP, round(3 * co_w))
    fam_aff = prof.get("family_affinity") or {}
    fam_w = max((fam_aff.get(f, 0.0) for f in _title_role_families(title)), default=0.0)
    if fam_w:
        feats["family_affinity"] = min(FAMILY_AFFINITY_CAP, round(2 * fam_w))

    if slug and slug in (prof.get("negative_companies") or ()):
        feats["negative_firm"] = -NEGATIVE_FIRM_POINTS

    return sum(feats.values()), feats


# ── Diversify ─────────────────────────────────────────────────────────


def _diversify_people(scored: list[dict], k: int) -> list[dict]:
    """MMR re-rank ported from student_job_ranker.diversify (see that
    module for the original job-side version).

    Each round picks argmax of:
        adj = score
              - LAMBDA_COMPANY     * (company_count) ** 2
              - LAMBDA_ROLE_FAMILY * Σ(family_count ** 2 for f in families)

    Keys per candidate:
      * normalized company via personalization._normalize_company
      * title role families via student_job_ranker._title_role_families

    Soft-caps repeat companies at ~3-4 in the top ~20, so the swipe
    deck isn't eight identical Google SWE profiles in a row.
    """
    if not scored:
        return []
    remaining = list(scored)
    picked: list[dict] = []
    company_counts: dict[str, int] = {}
    family_counts: dict[str, int] = {}
    keys: dict[int, tuple] = {}
    for item in remaining:
        cand = item["candidate"]
        keys[id(item)] = (
            _normalize_company(cand.get("Company") or "") or "_unknown",
            _title_role_families(cand.get("Title") or ""),
        )

    while remaining and len(picked) < k:
        best_i = 0
        best_adj = float("-inf")
        for i, item in enumerate(remaining):
            co, fams = keys[id(item)]
            co_ct = company_counts.get(co, 0)
            fam_penalty = (
                sum(family_counts.get(f, 0) ** 2 for f in fams) if fams else 0
            )
            adj = (
                item.get("rank_score", item["score"])
                - LAMBDA_COMPANY * (co_ct ** 2)
                - LAMBDA_ROLE_FAMILY * fam_penalty
            )
            if adj > best_adj:
                best_adj = adj
                best_i = i

        chosen = remaining.pop(best_i)
        co, fams = keys[id(chosen)]
        company_counts[co] = company_counts.get(co, 0) + 1
        for f in fams:
            family_counts[f] = family_counts.get(f, 0) + 1
        picked.append(chosen)

    return picked


# ── ID + response ─────────────────────────────────────────────────────


def _candidate_id(candidate: dict) -> Optional[str]:
    """Stable id per candidate. LinkedIn slug preferred, pdlId fallback,
    None if both missing (caller drops the candidate — Rylan uses id as
    a render key, a null or duplicate breaks the deck)."""
    url = candidate.get("LinkedIn") or candidate.get("linkedin_url") or ""
    if url:
        _canon, slug = canonicalize_linkedin_url(url)
        if slug:
            return slug
    pdl_id = (candidate.get("pdlId") or candidate.get("pdl_id") or "").strip()
    return pdl_id or None


def own_candidate_id(user_doc: dict) -> Optional[str]:
    """The candidate id the user's own LinkedIn would get, so the deck can
    drop them. profile.linkedinUrl first, then top-level linkedinUrl."""
    user_doc = user_doc or {}
    url = (user_doc.get("profile") or {}).get("linkedinUrl") or user_doc.get("linkedinUrl") or ""
    if not isinstance(url, str) or not url.strip():
        return None
    return _candidate_id({"LinkedIn": url})


def _to_response_dict(scored_item: dict) -> dict:
    """Trim internal scored dict → response contract. EXPLICIT KEY
    ALLOWLIST — defense-in-depth against firm_employees schema drift.
    See module docstring 'Sensitive-field posture'.
    """
    cand = scored_item["candidate"]
    return {
        "person": {
            "id":          scored_item["id"],
            "firstName":   cand.get("FirstName") or "",
            "lastName":    cand.get("LastName") or "",
            "linkedinUrl": cand.get("LinkedIn") or "",
            "pdlId":       cand.get("pdlId") or "",
            "company":     cand.get("Company") or "",
            "title":       cand.get("Title") or "",
            "college":     cand.get("College") or "",
            # Allowlist extension (OFF-57): every school they attended, not
            # just the first. Public profile facts, nothing sensitive.
            "schools":     list(cand.get("Schools") or []),
            "city":        cand.get("City") or "",
            "state":       cand.get("State") or "",
            # Deliberate allowlist extension (Rylan 2026-08-18): the year they
            # joined their current company. A professional fact from the
            # current-job block, nothing in the sensitive class this list
            # exists to block, and the card's best outreach hook.
            "joinedYear":  cand.get("JoinedYear") or None,
            # Allowlist extension (2026-08-30): the profile photo. Display
            # sugar, not sensitive; the card falls back to initials when it
            # is empty or the LinkedIn hotlink dies. The app mapper reads
            # snake_case photo_url, matching the prompt deck's contract.
            "photo_url":   cand.get("PhotoUrl") or "",
        },
        "score":    scored_item["score"],
        "tier":     scored_item["tier"],
        "reasons":  scored_item["reasons"],
        "briefing": scored_item["briefing"],
    }
