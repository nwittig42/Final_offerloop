"""
find_contacts MCP tool.

Runs the engine door (engine_search_contacts) + warmth_scoring. Result count
is clamped per tier in tier_caps.find_contacts_cap (anonymous + free signed-in
= 5, pro = 8, elite = 15). The schema permits up to 25 to avoid a migration
when caps are raised.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from app.mcp_server.budget import MCPBudget
from app.mcp_server.cache import MCPCache
from app.mcp_server.events import MCPEvents
from app.mcp_server.rate_limit import MCPRateLimit
from app.mcp_server.responses import build_paywall
from app.mcp_server.schemas import Contact, FindContactsInput, FindContactsOutput
from app.mcp_server.tier_caps import cap_message, find_contacts_cap

logger = logging.getLogger(__name__)


TOOL_NAME = "find_contacts"
CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days


# Cold result is ~1 PDL credit per record returned plus a small over-fetch
# buffer inside pdl_client. Five-record query averages 6 credits.
PDL_CREDITS_PER_QUERY = 6


# Offerloop credits charged to the signed-in caller per contact returned.
# MCP splits the website's bundled prompt_search rate (10 cr =
# CREDIT_COSTS['find_contact']) into two tool calls: find_contacts (5)
# + draft_outreach (5) = 10 total, matching the website's per-contact
# cost. Anonymous callers (uid=None) pay nothing — rate limits + the
# service-wide MCPBudget are their abuse-control surfaces.
CREDITS_PER_CONTACT = 5


def handle(
    *,
    args: dict,
    ip_hash: str,
    db: Any,
    user_ctx: dict | None = None,
) -> dict:
    """Run the find_contacts pipeline. Always returns a dict, never raises.

    Caller is responsible for serializing the dict to the MCP response.
    """
    started = time.monotonic()
    cache = MCPCache(db)
    limiter = MCPRateLimit(db)
    budget = MCPBudget(db)
    events = MCPEvents(db)

    # 1. Parse input.
    try:
        parsed = FindContactsInput.model_validate(args)
    except Exception as e:
        events.log(
            tool=TOOL_NAME, ip_hash=ip_hash, args_hash="",
            error=f"input_validation: {e}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return {"error": "invalid input", "details": str(e)}

    # 2. Handler-enforced tier cap (schema permits up to 25; handler clamps
    # per tier_caps.find_contacts_cap so we can raise tier caps without a
    # schema migration).
    requested = parsed.count
    tier_cap = find_contacts_cap(user_ctx)
    effective_count = min(requested, tier_cap)

    cache_args = parsed.model_dump()
    cache_args["count"] = effective_count  # cache by effective, not requested
    # Signed-in callers get the FullEnrich email pass; anonymous callers get
    # warehouse emails only. Key the cache on that so an anonymous, email-less
    # result never serves a signed-in caller who paid for the pass.
    cache_args["with_emails"] = bool(_signed_in_uid(user_ctx))
    args_hash = cache.key(TOOL_NAME, cache_args)

    # 3. Rate limit check.
    from app.mcp_server.tier_caps import rate_limit_identity
    rl = limiter.check_and_increment(
        rate_limit_identity(user_ctx, ip_hash), TOOL_NAME, user_ctx=user_ctx,
    )
    if not rl.ok:
        # Try cached fallback before paywall lands.
        cached_payload = cache.get(TOOL_NAME, cache_args)
        paywall = build_paywall(
            TOOL_NAME, ip_hash,
            hit_cap_type=rl.hit_cap_type or "day",
            retry_after_seconds=rl.retry_after_seconds,
        )
        out = _from_cache_or_empty(parsed.company, cached_payload, paywall)
        events.log(
            tool=TOOL_NAME, ip_hash=ip_hash, args_hash=args_hash,
            cache_hit=cached_payload is not None,
            paywall_shown=True,
            claim_token=_token_from_url(paywall.claim_url),
            result_count=len(out.contacts),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return out.model_dump()

    # 4. Cache lookup.
    cached_payload = cache.get(TOOL_NAME, cache_args)
    if cached_payload is not None:
        out = FindContactsOutput.model_validate({**cached_payload, "cached": True})
        if requested > effective_count:
            out.truncated_to = effective_count
            out.note = cap_message(user_ctx, effective_count)
        # Persist to My Network on cache hits too. The mcp_cache is shared
        # across users (no uid bucket), so a cache hit means we owe THIS
        # user's My Network the same write the cold-path user got. Dedup
        # in persist_contacts handles the case where the contact already
        # exists for this user.
        _maybe_persist_to_my_network(user_ctx, db, out.contacts)
        events.log(
            tool=TOOL_NAME, ip_hash=ip_hash, args_hash=args_hash,
            cache_hit=True,
            result_count=len(out.contacts),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return out.model_dump()

    # 4.5. Credit pre-check for signed-in callers. Charged at
    # CREDITS_PER_CONTACT (5) per result, matching the website's
    # prompt_search rate. Cache hits above don't deduct — mirrors the
    # website's pdl_cache 0-credit behavior. Anonymous callers (uid=None)
    # are gated by rate limits + MCPBudget only and skip this branch.
    credit_paywall = _credit_pre_check(user_ctx, effective_count, ip_hash)
    if credit_paywall is not None:
        out = FindContactsOutput(
            contacts=[],
            company=parsed.company,
            cached=False,
            note=credit_paywall.message,
            paywall=credit_paywall,
        )
        events.log(
            tool=TOOL_NAME, ip_hash=ip_hash, args_hash=args_hash,
            paywall_shown=True,
            claim_token=_token_from_url(credit_paywall.claim_url),
            result_count=0,
            duration_ms=int((time.monotonic() - started) * 1000),
            error="insufficient_credits",
        )
        return out.model_dump()

    # 5. Budget gate. If over-budget, return cached-only (we already
    # missed the cache above, so this becomes an empty result + CTA).
    if not budget.can_spend(PDL_CREDITS_PER_QUERY):
        paywall = build_paywall(
            TOOL_NAME, ip_hash,
            hit_cap_type="budget",
            retry_after_seconds=24 * 3600,
            message=(
                "Showing cached results only right now. Sign up free to keep "
                "querying live."
            ),
        )
        out = FindContactsOutput(
            contacts=[],
            company=parsed.company,
            cached=True,
            note="Daily live-query budget exhausted; no cached match for this query.",
            paywall=paywall,
        )
        events.log(
            tool=TOOL_NAME, ip_hash=ip_hash, args_hash=args_hash,
            paywall_shown=True,
            claim_token=_token_from_url(paywall.claim_url),
            result_count=0,
            duration_ms=int((time.monotonic() - started) * 1000),
            error="budget_exhausted",
        )
        return out.model_dump()

    # 6. Cold path: engine search (warehouse first, Coresignal for the rest),
    # then the bounded FullEnrich email pass for signed-in callers, then
    # warmth scoring.
    try:
        contacts = _run_pdl_search(parsed, effective_count, uid=_signed_in_uid(user_ctx))
        if _signed_in_uid(user_ctx):
            _fullenrich_pass(contacts, db)
        warmth = _score_warmth(parsed, contacts)
        # Stamp warmth metadata onto the raw PDL dicts so when we persist
        # to My Network the warmth fields land on the Firestore doc.
        for i, c in enumerate(contacts):
            w = warmth.get(i) or {}
            if w.get("score") is not None:
                c["warmth_score"] = w.get("score")
                c["warmth_tier"] = w.get("tier") or ""
                c["warmth_label"] = w.get("label") or ""
                c["warmth_signals"] = w.get("signals") or []
        out_contacts = _build_contacts(contacts, warmth)
    except Exception as e:
        logger.warning("[MCP find_contacts] cold path failed: %s", e, exc_info=True)
        events.log(
            tool=TOOL_NAME, ip_hash=ip_hash, args_hash=args_hash,
            error=f"pdl_or_warmth: {e}",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return {
            "contacts": [],
            "company": parsed.company,
            "cached": False,
            "note": "Live search failed. Please try again shortly.",
        }

    budget.spend(PDL_CREDITS_PER_QUERY)

    # 6.5. Deduct Offerloop credits for signed-in callers. Anonymous
    # stays free. Charged on actual contact count returned (not on the
    # requested or effective_count cap), so a search that returns 3 of
    # the requested 5 costs 15 credits, not 25.
    _deduct_credits_for_search(user_ctx, len(out_contacts))

    # 7. Persist to My Network for authed callers. Mirrors the website's
    # Find People flow so contacts discovered in Claude show up on
    # offerloop.ai with an mcpUnseen=true flag the UI uses to render
    # the first-time orange highlight. Same call as the cache-hit
    # branch above so persistence behavior is identical regardless of
    # whether PDL was hit or the cache served.
    _maybe_persist_to_my_network(user_ctx, db, contacts)

    out = FindContactsOutput(
        contacts=out_contacts,
        company=parsed.company,
        cached=False,
    )
    if requested > effective_count:
        out.truncated_to = effective_count
        out.note = cap_message(user_ctx, effective_count)

    # Only cache when the cold path actually produced contacts. Caching an
    # empty list poisons the bucket for CACHE_TTL_SECONDS — every subsequent
    # call with the same args (other users included; the find_contacts
    # cache key isn't uid-bucketed) hits the stale 0 until expiry. The
    # cofounder's pre-LLM-parser 0-result for Roblox + UC Berkeley sat in
    # mcp_cache for 7 days and made the deployed fix look broken.
    # pdl_client's own cache already follows this pattern.
    if out.contacts:
        cache.set(TOOL_NAME, cache_args, out.model_dump(), CACHE_TTL_SECONDS)

    events.log(
        tool=TOOL_NAME, ip_hash=ip_hash, args_hash=args_hash,
        cache_hit=False,
        result_count=len(out.contacts),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return out.model_dump()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _run_pdl_search(parsed: FindContactsInput, count: int, uid: Optional[str] = None) -> list[dict]:
    """Synthesize a parsed_prompt and walk the engine door.

    Routes through the same LLM-based prompt parser the website uses
    (parse_search_prompt_structured) so the search gets the rich,
    company-aware title_variations list, not just the single literal role
    string the MCP caller typed. Without this step, the MCP query landed only
    people whose title contained the exact phrase the user used; with it, we
    get the full role-family expansion the website's natural-language search
    already enjoys.

    Falls back to a manual parsed_prompt construction if the LLM parser
    is unavailable (no OPENAI_API_KEY, network error, low-confidence parse).
    """
    # The engine door (2026-09-07): warehouse first, Coresignal for the
    # remainder, spend mirrored, every vendor fill written back to the
    # warehouse. Until now this tool called Coresignal directly, so every MCP
    # search paid cold rates for people already on the shelf, and the people
    # it bought were never saved for the next student. The Hunter bridge
    # still covers a company-targeted miss, mirroring the website ladder.
    from app.services.engine_search import engine_search_contacts

    parsed_prompt = _build_parsed_prompt(parsed)
    user_profile = _synthesize_user_profile(parsed)

    # Signed-in callers: don't resurface people already in their network.
    # Anonymous callers have no network to exclude against. Fail open.
    exclude_keys = None
    if uid:
        try:
            from app.extensions import get_db
            from app.routes.runs import _build_exclusion_data_from_firestore
            exclude_keys = _build_exclusion_data_from_firestore(get_db(), uid)["identity_set"]
        except Exception as e:
            logger.warning("[MCP find_contacts] exclusion lookup failed (non-fatal): %s", e)

    contacts, _retry_level, _already_saved, _adjacency = engine_search_contacts(
        parsed_prompt,
        count,
        exclude_keys=exclude_keys,
        user_profile=user_profile,
        source="mcp_find_contacts",
    )
    if not contacts and (parsed_prompt.get("companies") or []):
        try:
            from app.services.hunter_person_search import search_people_via_hunter
            contacts, _retry_level, _already_saved, _hmeta = search_people_via_hunter(
                parsed_prompt, count, exclude_keys=exclude_keys, user_profile=user_profile
            )
        except Exception as e:
            logger.warning("[MCP find_contacts] hunter bridge failed: %s", e)
    return contacts or []


# Same bound the website Find uses (runs.py): one FullEnrich bulk job for
# whoever is still email-less, a bounded wait, misses stay email-less.
_FE_PASS_CAP = 6


def _fullenrich_pass(contacts: list[dict], db: Any) -> None:
    """Earn work emails for email-less results, warehouse-fed on a hit.

    Coresignal records carry no emails, so without this pass every MCP
    result came back with email=None and draft_outreach had nothing to send
    to. Signed-in callers only: they pay per contact, and the reseller
    agreement lets a verified address be served to later students from the
    warehouse at no re-charge. Never raises.
    """
    if not contacts:
        return
    missing = [
        c for c in contacts
        if not (c.get("WorkEmail") or c.get("Email") or "").strip()
    ][:_FE_PASS_CAP]
    if not missing:
        return
    # Hunter first (2026-09-07): seconds, in parallel, confidence 90+.
    # Whoever it finds skips the waterfall. Same ladder as every surface.
    try:
        from app.services import email_fast as _ef
        from concurrent.futures import ThreadPoolExecutor as _TPE
        def _hunt(c):
            return _ef.hunter_email(
                (c.get("FirstName") or "").strip(),
                (c.get("LastName") or "").strip(),
                (c.get("Company") or "").strip(),
            )
        with _TPE(max_workers=max(1, len(missing))) as ex:
            hunter_hits = list(ex.map(_hunt, missing))
        for c, h in zip(missing, hunter_hits):
            if h and h.get("email"):
                c["Email"] = h["email"]
                c["WorkEmail"] = h["email"]
                c["EmailSource"] = "hunter_finder"
                c["EmailVerified"] = False
                c["EmailConfidenceScore"] = int(h.get("confidence") or 0)
        logger.info("[MCP find_contacts] Hunter fast rung: %d/%d found",
                    sum(1 for h in hunter_hits if h), len(missing))
    except Exception:
        logger.warning("[MCP find_contacts] Hunter fast rung failed; everyone goes to FullEnrich")
    missing = [c for c in missing if not (c.get("Email") or "").strip()]
    if not missing:
        return
    try:
        from app.services import fullenrich_client
        people = [
            (str(i),
             (c.get("FirstName") or "").strip(),
             (c.get("LastName") or "").strip(),
             (c.get("Company") or "").strip(),
             (c.get("LinkedIn") or "").strip())
            for i, c in enumerate(missing)
        ]
        eid = fullenrich_client.start_bulk(people, db=db)
        if not eid:
            return
        wait = int(os.environ.get("MCP_FE_WAIT_SECONDS", "30"))
        ready, hits = fullenrich_client.fetch_bulk(eid, wait_seconds=wait, db=db)
        gained = []
        for i, hit in (hits or {}).items():
            c = missing[int(i)]
            c["Email"] = hit["email"]
            c["WorkEmail"] = hit["email"]
            c["EmailSource"] = "fullenrich_backfill"
            c["EmailVerified"] = True
            gained.append(c)
        logger.info("[MCP find_contacts] FullEnrich pass: %d/%d found (ready=%s)",
                    len(hits or {}), len(missing), ready)
        if gained:
            try:
                from app.services.firm_cache.writer import cache_pdl_contacts
                cache_pdl_contacts(gained, shape="app", async_write=True)
            except Exception:
                logger.warning("[MCP find_contacts] warehouse email feed failed")
    except Exception as e:
        logger.warning("[MCP find_contacts] FullEnrich pass failed: %s", e)


def _build_parsed_prompt(parsed: FindContactsInput) -> dict:
    """LLM-expand structured MCP inputs into the parsed_prompt shape the
    website builds, so PDL gets a rich title list at retry_level=0."""
    try:
        from app.services.prompt_parser import parse_search_prompt_structured
    except Exception as e:
        logger.warning("[MCP find_contacts] prompt_parser import failed: %s", e)
        return _build_parsed_prompt_manual(parsed)

    synthetic_prompt = _synthesize_prompt(parsed)
    try:
        llm_parsed = parse_search_prompt_structured(synthetic_prompt) or {}
    except Exception as e:
        logger.warning("[MCP find_contacts] LLM parser raised, falling back: %s", e)
        return _build_parsed_prompt_manual(parsed)

    if llm_parsed.get("error") or llm_parsed.get("confidence") == "low":
        logger.info(
            "[MCP find_contacts] LLM parser unusable (error=%s, confidence=%s) — "
            "falling back to manual prompt",
            llm_parsed.get("error"),
            llm_parsed.get("confidence"),
        )
        return _build_parsed_prompt_manual(parsed)

    # Hard-pin the MCP caller's typed inputs back onto the parsed dict.
    # The LLM is good at expanding titles but can drop or rewrite the
    # structured fields (e.g., normalize "UC Berkeley" to "Berkeley" in
    # ways the school_aliases path doesn't expect). We trust the caller's
    # company/school strings as ground truth; we only need the LLM for
    # title_variations + company_context.
    if (parsed.company or "").strip():
        llm_parsed["companies"] = [{
            "name": parsed.company,
            "matched_titles": (llm_parsed.get("companies") or [{}])[0].get("matched_titles", []),
        }]
    else:
        # Company-less search: make sure the LLM didn't hallucinate a company
        # (it would over-constrain PDL) and pin the caller's location.
        llm_parsed["companies"] = []
        loc = (getattr(parsed, "location", None) or "").strip()
        if loc and not llm_parsed.get("locations"):
            llm_parsed["locations"] = [loc]
    if parsed.school:
        llm_parsed["schools"] = [parsed.school]
    elif "schools" not in llm_parsed:
        llm_parsed["schools"] = []

    # Guarantee title_variations is non-empty — fall back to the manual
    # builder if the LLM somehow returned an empty list.
    if not llm_parsed.get("title_variations"):
        llm_parsed["title_variations"] = _build_title_variations(
            parsed.role, parsed.career_track,
        )

    return llm_parsed


def _synthesize_prompt(parsed: FindContactsInput) -> str:
    """Build a natural-language prompt that captures all of MCP's structured
    inputs in a form the LLM parser handles well. The phrasing mirrors how
    users actually search on the website's natural-language bar."""
    company = (parsed.company or "").strip()
    role = (parsed.role or "").strip()
    school = (parsed.school or "").strip()
    track = (parsed.career_track or "").strip()
    location = (getattr(parsed, "location", None) or "").strip()

    if company:
        if role and school:
            head = f"{role}s from {school} at {company}"
        elif role:
            head = f"{role}s at {company}"
        elif school:
            head = f"{school} alumni at {company}"
        else:
            head = f"people at {company}"
    else:
        # Company-less (industry-wide) search, e.g. "investment banking
        # analysts in Los Angeles that graduated from USC".
        if role and school:
            head = f"{role}s that graduated from {school}"
        elif role:
            head = f"{role}s"
        else:
            head = f"{school} alumni"

    if location:
        head = f"{head} in {location}"
    if track and track.lower() not in role.lower():
        head = f"{head} in {track}"
    return head


def _build_parsed_prompt_manual(parsed: FindContactsInput) -> dict:
    """Original (pre-LLM-routing) parsed_prompt construction. Used as the
    fallback when the LLM parser is unavailable. Title list stays narrow
    so the retry ladder is the only broadening mechanism in that case."""
    company = (parsed.company or "").strip()
    location = (getattr(parsed, "location", None) or "").strip()
    return {
        "companies": [{"name": company}] if company else [],
        "schools": [parsed.school] if parsed.school else [],
        "title_variations": _build_title_variations(parsed.role, parsed.career_track),
        "locations": [location] if location else [],
        "industries": [],
    }


def _build_title_variations(role: Optional[str], career_track: Optional[str]) -> list[str]:
    variations: list[str] = []
    if role:
        variations.append(role)
    if career_track and career_track not in variations:
        variations.append(career_track)
    return variations


def _synthesize_user_profile(parsed: FindContactsInput) -> dict:
    """Minimal Firestore-user-doc shape that warmth scoring + post-filters
    can read. Anonymous v1: we only have school + role + career_track from
    the MCP call, no resume, no goals.
    """
    profile: dict = {}
    if parsed.school:
        profile["academics"] = {"university": parsed.school}
        profile["professionalInfo"] = {"university": parsed.school}
    if parsed.career_track:
        profile.setdefault("goals", {})["careerTrack"] = parsed.career_track
    return profile


def _score_warmth(parsed: FindContactsInput, contacts: list[dict]) -> dict:
    """Run warmth scoring against the synthesized user profile."""
    if not contacts or not parsed.school:
        return {}
    from app.utils.warmth_scoring import score_contacts_for_email
    user_profile = _synthesize_user_profile(parsed)
    try:
        return score_contacts_for_email(user_profile, contacts) or {}
    except Exception as e:
        logger.warning("[MCP find_contacts] warmth scoring failed: %s", e)
        return {}


def _build_contacts(raw_contacts: list[dict], warmth: dict) -> list[Contact]:
    out: list[Contact] = []
    for idx, c in enumerate(raw_contacts):
        w = warmth.get(idx) or {}
        signals = w.get("signals") or []
        matched_school = _matched_school_from_signals(signals)
        out.append(Contact(
            name=_compose_name(c),
            title=c.get("Title") or c.get("JobTitle") or None,
            company=c.get("Company") or c.get("company") or None,
            linkedin_url=c.get("LinkedIn") or c.get("linkedin_url") or None,
            education=_compose_education(c, matched_school=matched_school),
            recent_career_move=_compose_recent_move(c),
            personalization_hook=w.get("label") or _fallback_hook(c, w),
            relationship_type=_relationship_type_from_signals(signals),
            warmth=w.get("tier"),
            email=_pick_recipient_email(c),
        ))
    return out


def _pick_recipient_email(c: dict) -> Optional[str]:
    """Best-available email for this contact, mirroring gmail_client.
    _select_recipient_email's WorkEmail > Email > PersonalEmail preference.

    Low-confidence emails (PDL `EmailSource` in {pattern, domain_generated,
    pdl_fallback, hunter_finder_risky, neverbounce_acceptall}) are NOT
    filtered here — gmail_client.create_gmail_draft_for_user has its own
    guard that skips low-confidence drafts. Surfacing the email anyway
    lets Claude tell the user "I have an email but it's a guess" instead
    of silently omitting it.
    """
    work = (c.get("WorkEmail") or "").strip()
    if work and work != "Not available" and "@" in work:
        return work
    primary = (c.get("Email") or "").strip()
    if primary and "@" in primary and not primary.endswith("@domain.com"):
        return primary
    personal = (c.get("PersonalEmail") or "").strip()
    if personal and personal != "Not available" and "@" in personal:
        return personal
    return None


def _compose_name(c: dict) -> str:
    first = (c.get("FirstName") or "").strip()
    last = (c.get("LastName") or "").strip()
    name = " ".join(p for p in (first, last) if p).strip()
    return name or (c.get("full_name") or c.get("Name") or "Unknown")


def _compose_education(c: dict, *, matched_school: Optional[str] = None) -> Optional[str]:
    """Return the contact's displayed education.

    When warmth scoring fires the same_university signal, `matched_school`
    is set to the school that triggered the match. In that case, surface
    THAT school's entry (with degree + year context from EducationTop)
    rather than the contact's `College` field. PDL sets `College` to the
    chronological-first non-high-school entry (typically undergrad), so
    for a contact who did undergrad at BYU and a master's at USC, the
    raw `College` says BYU but the alumni connection that warmth detected
    is the USC degree. Without this override, the displayed education
    contradicts the relationship_type=alumni tag.

    When no same_university signal fired, fall back to the existing
    `College`-only display.
    """
    if matched_school:
        override = _format_matched_education_entry(c, matched_school)
        if override:
            return override

    college = (c.get("College") or "").strip()
    if not college:
        return None
    return college


def _matched_school_from_signals(signals: list[dict]) -> Optional[str]:
    """Return the matched school name when same_university signal is present.

    The detail payload format varies: warmth_scoring.py emits it as a string
    (just the school name), but defensively handle a dict shape too
    ({"university": ..., "university_short": ..., "mascot": ...}) since
    that's how detect_commonality builds it before assignment.
    """
    for s in signals:
        if (s.get("signal") or "").strip() != "same_university":
            continue
        d = s.get("detail")
        if isinstance(d, dict):
            uni = (d.get("university") or "").strip()
            if uni:
                return uni
        elif isinstance(d, str):
            uni = d.strip()
            if uni:
                return uni
    return None


def _format_matched_education_entry(c: dict, matched_school: str) -> Optional[str]:
    """Look up the matched school's entry in `EducationTop` to preserve
    degree + year context (e.g. "University of Southern California - MBA
    (2022 - 2024)").

    `EducationTop` is the pre-formatted education history that
    pdl_client._format_pdl_contact builds (line 1442-1445), of the form
    "School - Degree (start - end); School - Degree (start - end)".
    We split on '; ' and find the entry whose school portion matches
    `matched_school` via the same variant-set logic personalization.py
    uses (get_university_variants intersection). When EducationTop is
    missing or no entry matches, return just `matched_school` so the
    displayed value at least names the right school, even without degree
    context.
    """
    matched_school = (matched_school or "").strip()
    if not matched_school:
        return None

    edu_top = (c.get("EducationTop") or "").strip()
    if not edu_top or edu_top.lower() == "not available":
        return matched_school

    try:
        from app.utils.users import get_university_variants
        matched_variants = get_university_variants(matched_school)
    except Exception:
        matched_variants = set()

    matched_lower = matched_school.lower()

    for entry in edu_top.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        # Each entry is "School Name - Degree (start - end)"; pull off
        # the school portion before the degree separator.
        school_portion = entry.split(" - ", 1)[0].strip()
        if not school_portion:
            continue
        if matched_variants:
            if matched_variants & get_university_variants(school_portion):
                return entry
        elif matched_lower in school_portion.lower():
            return entry

    return matched_school


def _compose_recent_move(c: dict) -> Optional[str]:
    # PDL contacts include experience array; the route-side formatter
    # already produces a "Company / Title" Most-Recent display. For v1 we
    # surface the primary job concisely without a dedicated formatter.
    title = (c.get("Title") or "").strip()
    company = (c.get("Company") or "").strip()
    if title and company:
        return f"{title} at {company}"
    return None


def _fallback_hook(c: dict, w: dict) -> Optional[str]:
    """When warmth scoring didn't produce a label, build a tiny role hook."""
    title = (c.get("Title") or "").strip()
    company = (c.get("Company") or "").strip()
    if title and company:
        return f"{title} at {company}"
    return None


_SIGNAL_TO_REL = {
    "dream_company": "dream_company",
    "same_university": "alumni",
    "same_major": "shared_major",
    "same_past_company": "shared_employer",
    "same_hometown": "shared_hometown",
    "recently_joined": "recent_transition",
    "career_transition": "career_path",
    "target_industry_match": "role_match",
}


def _relationship_type_from_signals(signals: list[dict]) -> Optional[str]:
    if not signals:
        return None
    for s in signals:
        kind = (s.get("signal") or "").strip()
        if kind in _SIGNAL_TO_REL:
            return _SIGNAL_TO_REL[kind]
    return None


def _from_cache_or_empty(
    company: str,
    cached_payload: Optional[dict],
    paywall,
) -> FindContactsOutput:
    if cached_payload is not None:
        out = FindContactsOutput.model_validate({**cached_payload, "cached": True})
        out.paywall = paywall
        return out
    return FindContactsOutput(
        contacts=[],
        company=company,
        cached=False,
        note="You've hit a free tier limit. Sign up to keep searching.",
        paywall=paywall,
    )


def _token_from_url(url: str) -> Optional[str]:
    if not url or "token=" not in url:
        return None
    return url.split("token=", 1)[1].split("&", 1)[0]


# ── Credit gating + deduction (signed-in callers only) ─────────────────────


def _signed_in_uid(user_ctx) -> Optional[str]:
    """Return the Firestore uid for signed-in callers, else None.

    user_ctx['uid'] comes from the OAuth access token's `sub` claim
    (flask_mount.py:143). Anonymous bearer tokens carry sub=None — those
    skip credit logic and rely on rate limits + the service-wide
    MCPBudget for abuse control.
    """
    uid = (user_ctx or {}).get("uid") if user_ctx else None
    return uid if uid else None


def _credit_pre_check(user_ctx, count: int, ip_hash: str):
    """Return a paywall CTA if the signed-in caller can't afford this
    search at CREDITS_PER_CONTACT per result; None otherwise.

    Anonymous callers (no uid) skip this gate entirely. Defensive
    Firestore-unreachable / no-user-doc paths return None (no gate) so a
    transient infra issue can't lock signed-in users out — they'd still
    hit the deduction step which is fail-soft and logs.
    """
    uid = _signed_in_uid(user_ctx)
    if not uid or count <= 0:
        return None

    cost = count * CREDITS_PER_CONTACT
    try:
        from app.extensions import get_db
        from app.services.auth import check_and_reset_credits
        db = get_db()
        if db is None:
            return None
        user_ref = db.collection("users").document(uid)
        snap = user_ref.get()
        if not snap.exists:
            return None
        credits_available = check_and_reset_credits(user_ref, snap.to_dict() or {})
        if credits_available >= cost:
            return None
    except Exception as e:
        logger.warning("[MCP find_contacts] credit pre-check error: %s", e)
        return None

    return build_paywall(
        TOOL_NAME, ip_hash,
        hit_cap_type="credits",
        retry_after_seconds=0,
        message=(
            f"Not enough Offerloop credits for this search "
            f"(need {cost}, {CREDITS_PER_CONTACT} per contact x {count}). "
            "Top up at offerloop.ai or wait for your monthly reset."
        ),
    )


def _deduct_credits_for_search(user_ctx, contact_count: int) -> None:
    """Deduct CREDITS_PER_CONTACT per contact returned, for signed-in
    callers. No-op for anonymous or empty results. Logs but never raises
    on failure — the website's prompt_search route uses the same
    fail-soft pattern (routes/runs.py:824-833).
    """
    uid = _signed_in_uid(user_ctx)
    if not uid or contact_count <= 0:
        return
    amount = contact_count * CREDITS_PER_CONTACT
    try:
        from app.services.auth import deduct_credits_atomic
        success, remaining = deduct_credits_atomic(uid, amount, "mcp_find_contacts")
        if not success:
            logger.warning(
                "[MCP find_contacts] credit deduction underpaid for uid=%s: "
                "had %d, needed %d (contacts surfaced anyway)",
                uid, remaining, amount,
            )
    except Exception as e:
        logger.warning(
            "[MCP find_contacts] credit deduction error for uid=%s: %s",
            uid, e,
        )


# ── My Network persist (shared by cold-path + cache-hit branches) ───────────


def _maybe_persist_to_my_network(user_ctx, db, contacts) -> None:
    """Write the search's contacts to users/{uid}/contacts/ for authed
    callers. Idempotent via persist_contacts' dedup. Accepts EITHER raw
    PDL contact dicts (cold path) OR the MCP Contact shape from cached
    payloads (cache-hit path); _to_persist_dict normalizes."""
    uid = (user_ctx or {}).get("uid") if user_ctx else None
    if not uid or db is None or not contacts:
        return
    try:
        from app.mcp_server.persist import persist_contacts
        normalized = [_to_persist_dict(c) for c in contacts]
        persist_contacts(uid=uid, db=db, contacts=normalized, source="mcp")
    except Exception as e:
        # Persistence failure must never break the user-facing response.
        logger.warning("[MCP find_contacts] persist failed (non-fatal): %s", e)


def _to_persist_dict(c) -> dict:
    """Convert either a raw PDL contact dict OR an MCP Contact pydantic
    model OR an MCP Contact dict (from cache payload) into the PDL-shape
    dict persist_contacts expects."""
    # Pydantic Contact instance → dict.
    if hasattr(c, "model_dump") and not isinstance(c, dict):
        c = c.model_dump()

    # PDL shape detected — pass through; persist_contacts already handles
    # the FirstName/firstName / Email/WorkEmail/email lookup order.
    if c.get("FirstName") or c.get("firstName"):
        return c

    # MCP Contact shape: single `name` field, lowercase keys.
    name = (c.get("name") or "").strip()
    parts = name.split()
    first = parts[0] if parts else ""
    last = " ".join(parts[1:]) if len(parts) > 1 else ""
    return {
        "FirstName": first,
        "LastName": last,
        "Email": c.get("email") or "",
        "LinkedIn": c.get("linkedin_url") or "",
        "Company": c.get("company") or "",
        "Title": c.get("title") or "",
        "College": c.get("education") or "",
    }
