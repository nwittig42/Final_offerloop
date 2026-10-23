"""
Quality gate for ingested jobs. Drops postings that are wrong for the
Offerloop student audience BEFORE they reach Firestore — keeps the index
small, keeps the ranker honest, and saves enrichment spend.

All rules are deterministic and run on the normalized job dict (the same
shape `_normalize_board_job` produces). The AI-derived fields are used
when present (Fantastic.jobs source) and gracefully skipped otherwise.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


# Company-name patterns that flag staffing / recruiting / RPO firms.
_STAFFING_PATTERNS = re.compile(
    r"\b("
    r"staffing|recruiting|recruiter[s]?|talent\s+solutions|placement\s+agency|"
    r"manpower|adecco|randstad|robert\s+half|kforce|aerotek|"
    r"insight\s+global|teksystems|allegis|kelly\s+services|"
    r"experis|modis|mondo|tek\s+systems"
    r")\b",
    re.I,
)

# Obvious scams / MLM / influencer-marketing fake-intern patterns.
_SCAM_PATTERNS = re.compile(
    r"\b("
    r"commission\s+only|100%\s+commission|"
    r"multi[-\s]?level\s+marketing|MLM|"
    r"brand\s+ambassador|independent\s+distributor|"
    r"earn\s+up\s+to\s+\$|earn\s+\$\d|"
    r"work\s+from\s+home!!!|hiring\s+immediately!!!|"
    r"no\s+experience\s+necessary"
    r")\b",
    re.I,
)

# Senior / leadership titles. Treated as wrong-for-student UNLESS the title
# also carries an Intern / New Grad / Early Career / Co-op marker (which
# happens occasionally for sponsored rotations).
_SENIOR_TITLE = re.compile(
    r"\b(Senior|Staff|Principal|Lead|Director|Head\s+of|"
    r"Vice\s+President|VP|Chief|Manager\s+II|Manager\s+III)\b",
    re.I,
)
_EARLY_CAREER_OVERRIDE = re.compile(
    r"\b(Intern|Internship|Co-?op|'New\s+Grad'|'Entry[-\s]Level'|"
    r"University\s+Graduate|Early\s+Career|Summer\s+Analyst|Summer\s+Associate)\b",
    re.I,
)

# Inconsistency: posting is tagged INTERN (or has Intern in title) but
# description requires 3+/5+ years experience.
_YOE_INCONSISTENT = re.compile(
    r"\b(3\+|4\+|5\+|6\+|7\+|8\+|10\+)\s*(?:years?|yrs?)\b", re.I,
)

# Internships requiring permanent work authorization → exclude for the
# visa-needing slice of users. We capture this as a per-job flag rather
# than a hard drop, but if combined with INTERN + 0-2 it's a strong
# signal of mismatch for the international student persona.
_REQUIRES_WORK_AUTH = re.compile(
    r"\b(must\s+have\s+permanent\s+work\s+authorization|"
    r"no\s+sponsorship|"
    r"will\s+not\s+sponsor|"
    r"unable\s+to\s+sponsor)\b",
    re.I,
)


_MIN_DESCRIPTION_CHARS = 50
# Widened 2026-07-14 from 60 → 90 days to keep more of the historical
# early-career window indexed. Big-co posting cycles run 30-45 days and many
# fall/spring intern reqs stay open 60+ days before closing.
_MAX_AGE_DAYS = 90

# Sources whose crawl diffs the full board every run and expires jobs that
# disappear, so they don't need the age drop. Bank programs often stay open
# well past 90 days.
BOARD_DIFF_SOURCES = frozenset({"workday", "oracle", "oleeo", "goldman", "manual"})

# Recruiting events posted as jobs (coffee chats, info sessions, ...).
_EVENT_TITLE = re.compile(
    r"coffee chat|info(rmation)? session|webinar|insight day|open day|"
    r"networking event|virtual event",
    re.I,
)
_ANALYST_TITLE = re.compile(r"\banalyst\b", re.I)


# ---------------------------------------------------------------------------
# Relevance tier + cold-tier positive allowlist (Phase 0 — 2026-07-14)
# ---------------------------------------------------------------------------
# When we scale from 270 → ~10K slugs the cold tier will surface a lot of
# non-target postings (blue-collar, clinical, senior IC). Two mechanisms
# handle this without polluting the feed:
#   - Cold-tier positive allowlist: at ingest, only keep titles that read as
#     early-career. Filters at the door.
#   - Relevance tier: every KEPT posting gets tier 1|2|3. Feed queries filter
#     to tier in [1,2]; enrichment only fires on tier 1.

# Explicit early-career vocabulary. "internship" also covers "intern".
_EARLY_CAREER_TITLE = re.compile(
    r"\b("
    r"intern|internship|co[-\s]?op|new\s+grad|new[-\s]graduate|recent\s+graduate|"
    r"early[-\s]career|university\s+graduate|entry[-\s]level|"
    r"junior|jr\.?|associate|"
    r"apprentice|trainee|fellow(ship)?|"
    r"summer\s+(analyst|associate|intern)|graduate\s+program|"
    r"rotational\s+program|rotational|campus\s+(hire|recruit)|college\s+hire|"
    r"level\s+i\b|l1\b|grad\s+role"
    r")\b",
    re.I,
)

# Titles Offerloop users actively want (SWE, IB, consulting, PM, data,
# design, product marketing). Union of common variants — kept broad because
# tier 1 is meant to be inclusive of anything a target-audience student would
# realistically apply to.
_TARGET_FUNCTION_TITLE = re.compile(
    r"\b("
    r"software\s+engineer(ing)?|swe|sde|software\s+developer|"
    r"backend|frontend|full[-\s]stack|mobile\s+engineer|ios|android|"
    r"platform\s+engineer|infrastructure\s+engineer|devops|sre|site\s+reliability|"
    r"data\s+(scientist|engineer|analyst)|ml\s+engineer|mle|machine\s+learning|"
    r"applied\s+scientist|research\s+scientist|research\s+engineer|"
    r"security\s+(engineer|analyst)|"
    r"product\s+(manager|analyst|designer)|associate\s+product\s+manager|apm|"
    r"ux\s+designer|ui\s+designer|product\s+design|"
    r"solutions\s+engineer|forward\s+deployed|technical\s+account\s+manager|"
    r"customer\s+success|customer\s+engineer|"
    r"account\s+executive|sales\s+development|sdr|bdr|business\s+development|"
    r"investment\s+bank(ing)?|equity\s+research|credit\s+research|"
    r"sales\s*(and|&)\s*trading|"
    # Finance and professional-services functions. Without these, the
    # entry-level titles our students actually recruit for ("2027 Summer
    # Analyst Program", "Global Markets Summer Analyst", "Asset Management
    # Summer Analyst", "Corporate Banking Analyst Program") matched NO target
    # function, graded tier 2, and so ranked below every tech internship and
    # skipped enrichment. Previously only the literal phrases "investment
    # banking" and "sales and trading" qualified — a tiny slice of how banks,
    # asset managers and the Big 4 actually title early-career roles.
    # Functions only, never a bare "analyst": that would sweep almost
    # everything into tier 1 and defeat the point of tiering.
    r"capital\s+markets|global\s+markets|fixed\s+income|"
    r"(asset|investment|wealth|portfolio)\s+management|"
    r"(private|corporate|commercial)\s+bank(ing)?|"
    r"private\s+equity|venture\s+capital|"
    r"m&a|mergers\s+and\s+acquisitions|restructuring|"
    r"underwrit(ing|er)|actuarial|risk\s+management|"
    r"audit|assurance|deal\s+advisory|transaction\s+(services|advisory)|"
    r"trader|trading\s+(analyst|associate|intern)|"
    # "Summer Analyst" / "Summer Associate" are levels, not functions, but
    # they are finance- and consulting-specific by convention (tech says
    # "intern"), and they are how the flagship programs are titled when the
    # posting names no desk at all: "2027 Summer Analyst Program". The
    # deadline extractor already treats these as a first-class category
    # (`year_coded_analyst`), so counting them as on-target is consistent.
    r"summer\s+(analyst|associate)|"
    r"consultant|consulting|"
    r"quant(itative)?|research\s+analyst|"
    r"business\s+analyst|financial\s+analyst|risk\s+analyst|"
    r"product\s+marketing|growth\s+marketing|marketing\s+analyst|"
    r"strategy\s+(analyst|associate)|operations\s+(analyst|associate)"
    r")\b",
    re.I,
)

# Categorical exclusions for the cold tier only — blue-collar, clinical,
# skilled trades. Hot-tier curated companies rarely post these so we don't
# apply this filter there.
_COLD_TIER_BLOCKLIST_TITLE = re.compile(
    r"\b("
    r"nurse|nursing|rn|lpn|cna|"
    r"medical\s+assistant|dental\s+(assistant|hygienist)|"
    r"physical\s+therap(ist|y)|occupational\s+therap(ist|y)|"
    r"home\s+(health|care)\s+aide|caregiver|"
    r"cdl|truck\s+driver|delivery\s+driver|forklift|"
    r"warehouse|welder|machinist|electrician|plumber|hvac|"
    r"phlebotom(ist|y)|radiolog(ist|y)|sonographer|"
    r"security\s+guard|janitor|custodian|"
    r"cashier|barista|line\s+cook|server|bartender|"
    r"pharmacist|pharmacy\s+technician|"
    r"esthetician|cosmetologist|hair\s+stylist"
    r")\b",
    re.I,
)


def _matches_cold_allowlist(doc: dict) -> bool:
    """Cold-tier admission: admit any title not in the blue-collar / clinical
    blocklist.

    Loosened 2026-07-15 (pre-launch volume sprint) — dropped the requirement
    that titles match either the early-career vocab OR the target-function
    list. The senior-title exclusion in evaluate() step 4 already drops
    Senior/Staff/Lead at the drop-list stage, so what survives here is
    mid/junior IC across the full title space. Pre-launch = maximum pool
    size wins over selectivity. Post-launch, if feed quality suffers,
    revert this to the previous "target-function OR early-career" gate.
    """
    title = doc.get("title") or ""
    if _COLD_TIER_BLOCKLIST_TITLE.search(title):
        return False
    return True


# "Sr", "Sr.", "Snr" or "Sen." as a word. Target-board titles only; not part
# of _SENIOR_TITLE, so legacy gating and tiering are unchanged.
_SENIOR_ABBREV_TITLE = re.compile(r"\b(?:sr|snr)\b|\bsen\.", re.I)


def is_candidate_title(title: str, analyst_entry: bool) -> bool:
    """True if the title reads as early-career for a direct target-employer board.

    A plain "Analyst" title counts only on boards flagged `analyst_entry`
    (banks where Analyst is the entry-level rank). Senior titles and
    recruiting events never count.
    """
    title = title or ""
    if not (
        _EARLY_CAREER_TITLE.search(title)
        or (analyst_entry and _ANALYST_TITLE.search(title))
    ):
        return False
    return not (_SENIOR_TITLE.search(title) or _SENIOR_ABBREV_TITLE.search(title)
                or _EVENT_TITLE.search(title))


def compute_relevance_tier(doc: dict) -> int:
    """Assign a 1-3 tier to a kept posting. Higher = worse fit for feed.

    Tier 1 — Early-career title + in target function OR clear intern/new-grad
             marker. Feed default. Also gates enrichment spend.
    Tier 2 — Early-career signal but outside target functions (e.g. entry-
             level marketing at a tech co). Reachable via feed but never
             enriched.
    Tier 3 — Neither. Kept only for ranking flexibility; hidden from feed by
             default via tier filter.
    """
    title = doc.get("title") or ""
    is_early = bool(_EARLY_CAREER_TITLE.search(title)) or (
        bool(doc.get("analyst_entry")) and is_candidate_title(title, True)
    )
    in_target = bool(_TARGET_FUNCTION_TITLE.search(title))

    if is_early and in_target:
        return 1
    if is_early:
        return 2
    # Titles without early-career marker but tagged INTERN/internship in the
    # employment-type field are treated as tier 1 (common at Ashby/Lever).
    if _is_intern_role(doc) and in_target:
        return 1
    if _is_intern_role(doc):
        return 2
    return 3


def _is_too_old(posted_at) -> bool:
    if posted_at is None:
        return False
    if isinstance(posted_at, str):
        try:
            posted_at = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        except ValueError:
            return False
    if not isinstance(posted_at, datetime):
        return False
    if posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - posted_at > timedelta(days=_MAX_AGE_DAYS)


def _is_intern_role(doc: dict) -> bool:
    """True if this posting targets interns (used to evaluate YOE inconsistency)."""
    emp = (doc.get("ai_employment_type") or "").upper()
    if emp == "INTERN":
        return True
    job_type = (doc.get("type") or "").upper()
    if job_type == "INTERNSHIP":
        return True
    title = (doc.get("title") or "").lower()
    return any(kw in title for kw in ("intern", "co-op", "coop", "summer analyst"))


def evaluate(doc: dict, *, mode: str = "hot") -> tuple[bool, str | None]:
    """Return (keep, reason_if_dropped).

    Pure function — easy to unit test without Firestore or HTTP.

    Args:
        doc: normalized job dict
        mode: "hot" — original drop-list only (FJ + hot-tier direct-ATS).
              "cold" — additionally require the cold-tier positive allowlist
              (early-career vocabulary AND not blue-collar/clinical). Prevents
              10K-slug cold tier from flooding the feed with junk.
    """
    title = doc.get("title") or ""
    company = doc.get("company") or ""
    desc = doc.get("description_raw") or ""

    # 1. LinkedIn agency flag (FJ provides this when include_li=true)
    if doc.get("linkedin_org_recruitment_agency") is True:
        return False, "linkedin_recruitment_agency"

    # 2. Company-name staffing patterns
    if _STAFFING_PATTERNS.search(company):
        return False, "staffing_company_name"

    # 3. Scam / MLM patterns
    if _SCAM_PATTERNS.search(title) or _SCAM_PATTERNS.search(desc):
        return False, "scam_pattern"

    # 4. Senior titles are KEPT (2026-07-21 multi-audience change): the
    # per-user seniority gate in job_board.apply_hard_gate_seniority now
    # filters bidirectionally by career stage, so senior/executive roles
    # must exist in the pool for experienced users. They land in relevance
    # tier 3 via compute_relevance_tier, so student-default feed queries
    # and enrichment spend are unaffected.

    # 5. Intern-tagged role that requires 3+ years experience
    if _is_intern_role(doc) and _YOE_INCONSISTENT.search(desc):
        return False, "intern_yoe_inconsistent"

    # 6. Description too short to be a real posting
    if len(desc.strip()) < _MIN_DESCRIPTION_CHARS:
        # Allow short descriptions from Simplify (which intentionally stores
        # description="" and links out) — those entries are still useful.
        if doc.get("source") != "simplify":
            return False, "description_too_short"

    # 7. Stale posting (>90 days old). Board-diff sources are exempt: their
    #    crawl expires a job once it leaves the board.
    if doc.get("source") not in BOARD_DIFF_SOURCES and _is_too_old(doc.get("posted_at")):
        return False, "too_old"

    # 8. Cold-tier only: must match early-career positive allowlist.
    #    Hot tier (curated ~270 co's + FJ) skips this — those are known-good.
    if mode == "cold" and not _matches_cold_allowlist(doc):
        return False, "cold_tier_allowlist_miss"

    return True, None


def apply(docs: list[dict], *, mode: str = "hot") -> tuple[list[dict], dict]:
    """Filter a batch of normalized job docs. Returns (kept, drop_counts_by_reason).

    Mutates kept docs in-place to attach:
      - `relevance_tier` (1/2/3) — feed filter + enrichment gate
      - `us_priority`    (1/2/3) — US > remote > non-US ordering for the feed
    """
    from backend.pipeline.normalizer import infer_us_priority

    kept: list[dict] = []
    drops: dict[str, int] = {}
    for doc in docs:
        ok, reason = evaluate(doc, mode=mode)
        if not ok:
            drops[reason] = drops.get(reason, 0) + 1
            continue
        doc["relevance_tier"] = compute_relevance_tier(doc)
        doc["us_priority"] = infer_us_priority(doc)
        kept.append(doc)
    if drops:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(drops.items()))
        logger.info("Quality gate (mode=%s) dropped %d jobs (%s)",
                    mode, sum(drops.values()), breakdown)
    if kept:
        tier_counts: dict[int, int] = {}
        for d in kept:
            tier_counts[d["relevance_tier"]] = tier_counts.get(d["relevance_tier"], 0) + 1
        tier_breakdown = ", ".join(f"tier{k}={v}" for k, v in sorted(tier_counts.items()))
        logger.info("Quality gate (mode=%s) kept %d jobs (%s)", mode, len(kept), tier_breakdown)
    return kept, drops
