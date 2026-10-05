"""
Agentic Networking Queue service (Phase 1 — "Progressive Coach").

Generates a weekly batch of 5 AI-picked, pre-drafted contacts for a user.
Runs as a background thread following the coffee_chat_prep async job pattern:

    POST /api/queue/generate -> spawn background thread, return job_id
    GET  /api/queue/status/<job_id> -> poll status doc
    GET  /api/queue/current -> fetch most recent queue

Credit cost:
- Free tier: static teaser (no credits, no backend call — handled in the route layer).
- Pro/Elite: one free queue per ISO week (the automatic Monday queue) plus
  15 credits for any additional manual generation (Extend / Refine after the
  first free refine).

Data model (Firestore):

    users/{uid}/weekly_queues/{queue_id}
        generatedAt, intentText, filters, phase, cycleNumber,
        status ("processing"|"pending_review"|"completed_partial"|
                "failed"|"archived"|"expired"),
        contactCount, jobId, errorMessage, creditsCharged
    users/{uid}/weekly_queues/{queue_id}/contacts/{contact_id}
        pdlId, email, name, firstName, lastName, title, company,
        college, city, state, linkedinUrl,
        warmthTier, warmthScore, warmthSignals,
        draftSubject, draftBody,
        status ("pending"|"approved"|"dismissed"|"edited"),
        dismissReason, approvedAt, gmailDraftId
    users/{uid}/settings/queue_preferences
        enabled, paused, pausedUntil, cadence, cyclesCompleted, phase,
        blocklist: { companies: [...], titles: [...] },
        lastRefineAt, refineCountThisWeek, refineWeekKey

Architecture notes (from CEO + eng + design reviews, 2026-04-08/09):
- Dedup query: Firestore `where pdlId in [...]` AND `where email in [...]`
  (batched in 10s — Firestore `in` query cap). Email fallback handles the
  historical contacts that were saved before pdlId plumbing shipped.
- Blocklist is exact-match-on-normalized (lower + strip + collapse
  whitespace) NOT substring match (per outside-voice §OV.2).
- Credit deduction is AFTER PDL returns and BEFORE email generation — so
  zero-result searches are not charged and failed batches are refunded.
- `batch_generate_emails` is called with named kwargs (call site #7 of
  this 15-param function — see CLAUDE.md "known fragility").
- TTL: queues older than 14 days are deleted at generation time (cheap
  single-query cleanup, piggyback on every generate call).
"""
from __future__ import annotations

import logging
import re
import threading
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from firebase_admin import firestore

from app.config import TIER_CONFIGS
from app.extensions import get_db
from app.services.auth import deduct_credits_atomic, refund_credits_atomic
from app.services.pdl_client import (
    US_STATE_ABBREVIATIONS,
    search_contacts_with_smart_location_strategy,
)
from app.services.reply_generation import batch_generate_emails
from app.utils.warmth_scoring import score_contacts_for_email

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

QUEUE_CONTACT_COUNT = 5
QUEUE_TTL_DAYS = 14
QUEUE_GENERATION_CREDITS = 15  # Full price (Extend / second+ refine / Free tier)

# Status values for the parent queue doc
STATUS_PROCESSING = "processing"
STATUS_PENDING_REVIEW = "pending_review"
STATUS_COMPLETED_PARTIAL = "completed_partial"
STATUS_FAILED = "failed"
STATUS_FAILED_PDL = "failed_pdl"
STATUS_FAILED_EMAILS = "failed_emails"
STATUS_FAILED_WRITE = "failed_write"
STATUS_ARCHIVED = "archived"
STATUS_EXPIRED = "expired"

# Valid dismiss reasons (written to blocklist)
DISMISS_WRONG_COMPANY = "wrong_company"
DISMISS_WRONG_PERSON = "wrong_person"
DISMISS_NOT_NOW = "not_now"
VALID_DISMISS_REASONS = frozenset(
    {DISMISS_WRONG_COMPANY, DISMISS_WRONG_PERSON, DISMISS_NOT_NOW}
)


# ---------------------------------------------------------------------------
# Normalization helpers (blocklist + dedup)
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_text(value: Any) -> str:
    """Lowercase, strip, collapse whitespace. Returns '' for None/non-str."""
    if not value:
        return ""
    return _WHITESPACE_RE.sub(" ", str(value).strip().lower())


def _normalize_email(value: Any) -> str:
    if not value:
        return ""
    return str(value).strip().lower()


def _iso_week_key(dt: Optional[datetime] = None) -> str:
    """Return a YYYY-Www ISO week key — stable across refine count resets."""
    dt = dt or datetime.now(timezone.utc)
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Preferences (blocklist + pause state)
# ---------------------------------------------------------------------------


def get_queue_preferences(db, uid: str) -> dict:
    """
    Load queue preferences for a user. Creates a default doc if missing.
    Always returns a dict with the expected shape.
    """
    prefs_ref = (
        db.collection("users").document(uid).collection("settings").document("queue_preferences")
    )
    snap = prefs_ref.get()
    if snap.exists:
        data = snap.to_dict() or {}
    else:
        data = {}

    return {
        "enabled": data.get("enabled", True),
        "paused": data.get("paused", False),
        "pausedUntil": data.get("pausedUntil"),
        "cadence": data.get("cadence", "weekly"),
        "cyclesCompleted": int(data.get("cyclesCompleted", 0)),
        "phase": data.get("phase", "guided"),
        "blocklist": {
            "companies": list(data.get("blocklist", {}).get("companies", [])),
            "titles": list(data.get("blocklist", {}).get("titles", [])),
        },
        "lastRefineAt": data.get("lastRefineAt"),
        "refineCountThisWeek": int(data.get("refineCountThisWeek", 0)),
        "refineWeekKey": data.get("refineWeekKey", ""),
    }


def update_queue_preferences(db, uid: str, updates: dict) -> dict:
    """Partial-update allowed preference fields. Returns the merged prefs."""
    allowed = {"enabled", "paused", "pausedUntil", "cadence"}
    clean = {k: v for k, v in updates.items() if k in allowed}
    if not clean:
        return get_queue_preferences(db, uid)

    prefs_ref = (
        db.collection("users").document(uid).collection("settings").document("queue_preferences")
    )
    clean["updatedAt"] = _now_iso()
    prefs_ref.set(clean, merge=True)
    return get_queue_preferences(db, uid)


def _add_to_blocklist(db, uid: str, *, company: str = "", title: str = "") -> None:
    """Append a normalized company/title to the user's blocklist (idempotent)."""
    prefs_ref = (
        db.collection("users").document(uid).collection("settings").document("queue_preferences")
    )
    updates: dict = {"updatedAt": _now_iso()}
    if company:
        normalized_company = _normalize_text(company)
        if normalized_company:
            updates["blocklist.companies"] = firestore.ArrayUnion([normalized_company])
    if title:
        normalized_title = _normalize_text(title)
        if normalized_title:
            updates["blocklist.titles"] = firestore.ArrayUnion([normalized_title])
    if len(updates) > 1:  # more than just updatedAt
        prefs_ref.set({}, merge=True)  # ensure doc exists
        prefs_ref.update(updates)


# ---------------------------------------------------------------------------
# Dedup + blocklist filtering
# ---------------------------------------------------------------------------


def _chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _fetch_existing_contact_keys(db, uid: str) -> tuple[set[str], set[str]]:
    """
    Return (set of pdlIds, set of normalized emails) for every contact already
    saved under `users/{uid}/contacts/`.

    We fetch once per generation rather than running per-candidate queries —
    typical contact counts (< 500) fit comfortably in memory, and this avoids
    5+ Firestore reads in the hot path.
    """
    contacts_ref = db.collection("users").document(uid).collection("contacts")
    pdl_ids: set[str] = set()
    emails: set[str] = set()
    try:
        for doc in contacts_ref.stream():
            data = doc.to_dict() or {}
            pid = (data.get("pdlId") or "").strip()
            if pid:
                pdl_ids.add(pid)
            email = _normalize_email(data.get("email") or data.get("Email") or "")
            if email:
                emails.add(email)
    except Exception as exc:
        logger.warning("queue_service: failed to fetch existing contact keys for uid=%s: %s", uid, exc)
    return pdl_ids, emails


def _filter_candidates(
    candidates: list[dict],
    existing_pdl_ids: set[str],
    existing_emails: set[str],
    blocklist: dict,
) -> tuple[list[dict], dict]:
    """
    Apply dedup + blocklist filtering. Returns (filtered_list, filter_stats).

    Dedup rule (hard gate per CEO plan §Dedup):
      - If candidate.pdlId exists in existing_pdl_ids → drop
      - Else if normalized(candidate.email) exists in existing_emails → drop

    Blocklist rule (per outside voice §OV.2):
      - Exact-match-on-normalized company OR title → drop
    """
    blocked_companies = {_normalize_text(c) for c in blocklist.get("companies", []) if c}
    blocked_titles = {_normalize_text(t) for t in blocklist.get("titles", []) if t}

    stats = {"dedup_pdl": 0, "dedup_email": 0, "blocklist_company": 0, "blocklist_title": 0}
    filtered: list[dict] = []

    for c in candidates:
        pid = (c.get("pdlId") or "").strip()
        email_norm = _normalize_email(c.get("Email") or c.get("email") or "")
        company_norm = _normalize_text(c.get("Company") or c.get("company") or "")
        title_norm = _normalize_text(c.get("Title") or c.get("title") or "")

        if pid and pid in existing_pdl_ids:
            stats["dedup_pdl"] += 1
            continue
        if email_norm and email_norm in existing_emails:
            stats["dedup_email"] += 1
            continue
        if company_norm and company_norm in blocked_companies:
            stats["blocklist_company"] += 1
            continue
        if title_norm and title_norm in blocked_titles:
            stats["blocklist_title"] += 1
            continue

        filtered.append(c)

    return filtered, stats


# ---------------------------------------------------------------------------
# TTL cleanup
# ---------------------------------------------------------------------------


def cleanup_expired_queues(db, uid: str, ttl_days: int = QUEUE_TTL_DAYS) -> int:
    """
    Delete queue docs older than `ttl_days`, along with their contacts
    subcollection. Returns the count of queues deleted.

    Phase 1 runs this inline on every generate (cheap: indexed order_by).
    Phase 2 daemon will take over.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")
    queues_ref = db.collection("users").document(uid).collection("weekly_queues")
    deleted = 0
    try:
        query = queues_ref.where("generatedAt", "<", cutoff_iso)
        for doc in query.stream():
            _delete_queue_doc_and_subcollection(doc.reference)
            deleted += 1
    except Exception as exc:
        logger.warning("queue_service: TTL cleanup failed for uid=%s: %s", uid, exc)
    return deleted


def _delete_queue_doc_and_subcollection(queue_ref) -> None:
    """Delete all contacts under a queue, then the queue doc itself."""
    contacts_ref = queue_ref.collection("contacts")
    try:
        for c in contacts_ref.stream():
            c.reference.delete()
    except Exception:
        pass
    queue_ref.delete()


# ---------------------------------------------------------------------------
# Core generation pipeline
# ---------------------------------------------------------------------------


def generate_queue_background(
    *,
    uid: str,
    queue_id: str,
    filters: dict,
    user_profile: dict,
    resume_text: str,
    credits_charged_on_start: int,
) -> None:
    """
    Background worker that runs the full queue generation pipeline.

    Pipeline stages (mirrors coffee_chat_prep async pattern):
      1. Load queue preferences (blocklist, pause state)
      2. PDL search — up to 3x the target count to survive dedup+blocklist
      3. Dedup + blocklist filter
      4. Deduct 15 credits IF NOT ALREADY DEDUCTED (free weekly queue is
         pre-deducted as 0 by the route layer)
      5. Warmth score + sort
      6. batch_generate_emails (named kwargs — call site #7)
      7. Write queue doc + contacts subcollection
      8. Mark status = pending_review or completed_partial
    """
    db = get_db()
    queue_ref = (
        db.collection("users")
        .document(uid)
        .collection("weekly_queues")
        .document(queue_id)
    )

    credits_refunded = False

    def _fail(status_value: str, message: str, refund_credits: bool = True) -> None:
        nonlocal credits_refunded
        logger.error("queue_service: uid=%s queue=%s %s: %s", uid, queue_id, status_value, message)
        if refund_credits and credits_charged_on_start > 0 and not credits_refunded:
            try:
                refund_credits_atomic(uid, credits_charged_on_start, f"queue_{status_value}")
                credits_refunded = True
            except Exception as refund_exc:
                logger.error("queue_service: refund failed uid=%s: %s", uid, refund_exc)
        try:
            queue_ref.update(
                {
                    "status": status_value,
                    "errorMessage": message,
                    "creditsRefunded": credits_refunded,
                    "updatedAt": _now_iso(),
                }
            )
        except Exception as update_exc:
            logger.error("queue_service: failed to mark queue failed: %s", update_exc)

    try:
        # Stage 1 — load prefs
        prefs = get_queue_preferences(db, uid)
        blocklist = prefs.get("blocklist", {})

        # Stage 2 — PDL search. Overshoot by 3x to survive dedup+blocklist.
        company = (filters.get("company") or "").strip()
        title_keywords = (filters.get("titleKeywords") or filters.get("title") or "").strip()
        university = (filters.get("university") or "").strip()
        # Queue is US-only for Phase 1 — the PDL search adds a
        # `location_country: united states` term when location is "United States",
        # and we also post-filter on the normalized `country` field below as a
        # belt-and-suspenders check against stale PDL records.
        location = "United States"
        overshoot = QUEUE_CONTACT_COUNT * 3

        queue_ref.update({"status": STATUS_PROCESSING, "stage": "searching", "updatedAt": _now_iso()})

        try:
            raw_candidates = search_contacts_with_smart_location_strategy(
                job_title=title_keywords,
                company=company,
                location=location,
                max_contacts=overshoot,
                college_alumni=university or None,
            ) or []
            # Alumni verification can be over-strict (e.g. USC parenthesized
            # abbreviations, nested PDL education fields). If the alumni-only
            # search returns nothing, fall back to a non-alumni search and let
            # warmth scoring surface same-university matches instead of
            # filtering them out at the query stage.
            if university and not raw_candidates:
                logger.info(
                    "queue_service: alumni search returned 0 for uid=%s university=%s, retrying without alumni filter",
                    uid, university,
                )
                raw_candidates = search_contacts_with_smart_location_strategy(
                    job_title=title_keywords,
                    company=company,
                    location=location,
                    max_contacts=overshoot,
                    college_alumni=None,
                ) or []
        except Exception as search_exc:
            logger.exception("queue_service: PDL search failed uid=%s", uid)
            _fail(STATUS_FAILED_PDL, f"Contact search failed: {search_exc}")
            return

        # Defensive US-only post-filter. PDL's query-time country filter is
        # the primary gate, but some records have stale or missing
        # `location_country` values, so we also verify on the normalized
        # contact shape before dedup/blocklist/warmth-scoring.
        _US_ALIASES = {"united states", "usa", "us", "u.s.", "u.s.a."}
        def _is_us_contact(c: dict) -> bool:
            country = (c.get("country") or c.get("Country") or "").strip().lower()
            if not country:
                # Missing country → fall back to state being a US 2-letter code
                state = (c.get("state") or c.get("State") or "").strip().upper()
                return state in US_STATE_ABBREVIATIONS if state else False
            return country in _US_ALIASES
        pre_us_count = len(raw_candidates)
        raw_candidates = [c for c in raw_candidates if _is_us_contact(c)]
        dropped_non_us = pre_us_count - len(raw_candidates)
        if dropped_non_us:
            logger.info(
                "queue_service: US post-filter dropped %d/%d non-US candidates for uid=%s",
                dropped_non_us, pre_us_count, uid,
            )

        if not raw_candidates:
            # Zero-result edge case: refund credits and mark queue as completed with zero contacts.
            if credits_charged_on_start > 0 and not credits_refunded:
                try:
                    refund_credits_atomic(uid, credits_charged_on_start, "queue_zero_results")
                    credits_refunded = True
                except Exception as refund_exc:
                    logger.error("queue_service: zero-result refund failed: %s", refund_exc)
            queue_ref.update(
                {
                    "status": STATUS_COMPLETED_PARTIAL,
                    "stage": "completed",
                    "contactCount": 0,
                    "creditsRefunded": credits_refunded,
                    "errorMessage": "No contacts matched your criteria.",
                    "updatedAt": _now_iso(),
                }
            )
            return

        # Stage 3 — dedup + blocklist filter
        existing_pdl_ids, existing_emails = _fetch_existing_contact_keys(db, uid)
        filtered, filter_stats = _filter_candidates(
            raw_candidates, existing_pdl_ids, existing_emails, blocklist
        )

        if not filtered:
            if credits_charged_on_start > 0 and not credits_refunded:
                try:
                    refund_credits_atomic(uid, credits_charged_on_start, "queue_all_filtered")
                    credits_refunded = True
                except Exception:
                    pass
            queue_ref.update(
                {
                    "status": STATUS_COMPLETED_PARTIAL,
                    "stage": "completed",
                    "contactCount": 0,
                    "creditsRefunded": credits_refunded,
                    "filterStats": filter_stats,
                    "errorMessage": "All candidates filtered by dedup/blocklist. Try different filters.",
                    "updatedAt": _now_iso(),
                }
            )
            return

        # Stage 4 — warmth score + sort + cap to QUEUE_CONTACT_COUNT
        queue_ref.update({"stage": "scoring", "updatedAt": _now_iso()})
        try:
            scored = score_contacts_for_email(user_profile, filtered)
            # score_contacts_for_email returns a dict keyed by index — sort `filtered` in place
            # by (warmth tier rank, score desc)
            _tier_rank = {"warm": 0, "neutral": 1, "cold": 2}
            def _sort_key(idx_contact):
                idx, _c = idx_contact
                data = scored.get(idx, {})
                return (_tier_rank.get(data.get("tier", "cold"), 3), -int(data.get("score", 0)))
            indexed = list(enumerate(filtered))
            indexed.sort(key=_sort_key)
            sorted_contacts = [c for _, c in indexed]
            # Re-score in order for the final batch so index-keyed warmth maps to new order
            top_contacts = sorted_contacts[:QUEUE_CONTACT_COUNT]
            warmth_data = score_contacts_for_email(user_profile, top_contacts)
        except Exception as score_exc:
            logger.warning("queue_service: warmth scoring failed uid=%s: %s", uid, score_exc)
            top_contacts = filtered[:QUEUE_CONTACT_COUNT]
            warmth_data = {}

        # Stage 5 — generate emails (call site #7 — named kwargs required)
        queue_ref.update({"stage": "drafting", "updatedAt": _now_iso()})
        try:
            email_results = batch_generate_emails(
                contacts=top_contacts,
                resume_text=resume_text or "",
                user_profile=user_profile,
                career_interests=(user_profile.get("careerInterests") or []),
                fit_context=None,
                pre_parsed_user_info=user_profile.get("resumeParsed"),
                template_instructions="",
                email_template_purpose="coffee_chat",
                resume_filename=None,
                subject_line=None,
                signoff_config=None,
                auth_display_name=user_profile.get("displayName") or user_profile.get("name"),
                personal_note="",
                dream_companies=(user_profile.get("goals") or {}).get("dreamCompanies") or [],
                warmth_data=warmth_data,
            ) or {}
        except Exception as email_exc:
            logger.exception("queue_service: email generation failed uid=%s", uid)
            _fail(STATUS_FAILED_EMAILS, f"Email generation failed: {email_exc}")
            return

        # Stage 6 — write queue contacts subcollection
        queue_ref.update({"stage": "saving", "updatedAt": _now_iso()})
        try:
            contacts_sub = queue_ref.collection("contacts")
            written = 0
            for idx, contact in enumerate(top_contacts):
                draft = email_results.get(idx) or {}
                draft_subject = draft.get("subject") or draft.get("email_subject") or ""
                draft_body = draft.get("body") or draft.get("email_body") or ""

                warmth = warmth_data.get(idx, {}) if warmth_data else {}
                warmth_tier = warmth.get("tier") or contact.get("warmth_tier") or "cold"
                warmth_signals = warmth.get("signals") or contact.get("warmth_signals") or []

                first_name = (contact.get("FirstName") or contact.get("firstName") or "").strip()
                last_name = (contact.get("LastName") or contact.get("lastName") or "").strip()
                full_name = f"{first_name} {last_name}".strip() or "Unknown"

                contact_doc = {
                    "pdlId": (contact.get("pdlId") or "").strip(),
                    "email": contact.get("Email") or contact.get("email") or "",
                    "name": full_name,
                    "firstName": first_name,
                    "lastName": last_name,
                    "title": contact.get("Title") or contact.get("title") or "",
                    "company": contact.get("Company") or contact.get("company") or "",
                    "college": contact.get("College") or contact.get("college") or "",
                    "city": contact.get("City") or contact.get("city") or "",
                    "state": contact.get("State") or contact.get("state") or "",
                    "linkedinUrl": contact.get("LinkedIn") or contact.get("linkedinUrl") or "",
                    "warmthTier": warmth_tier,
                    "warmthScore": int(warmth.get("score", 0)) if warmth else 0,
                    "warmthSignals": warmth_signals,
                    "draftSubject": draft_subject,
                    "draftBody": draft_body,
                    "status": "pending",
                    "dismissReason": None,
                    "approvedAt": None,
                    "gmailDraftId": None,
                    "createdAt": _now_iso(),
                }
                contacts_sub.add(contact_doc)
                written += 1

            final_status = (
                STATUS_PENDING_REVIEW if written == QUEUE_CONTACT_COUNT else STATUS_COMPLETED_PARTIAL
            )
            queue_ref.update(
                {
                    "status": final_status,
                    "stage": "completed",
                    "contactCount": written,
                    "filterStats": filter_stats,
                    "completedAt": _now_iso(),
                    "updatedAt": _now_iso(),
                }
            )

            # Bump cyclesCompleted on the preferences doc
            try:
                prefs_ref = (
                    db.collection("users")
                    .document(uid)
                    .collection("settings")
                    .document("queue_preferences")
                )
                prefs_ref.set(
                    {
                        "cyclesCompleted": firestore.Increment(1),
                        "updatedAt": _now_iso(),
                    },
                    merge=True,
                )
            except Exception as prefs_exc:
                logger.warning("queue_service: failed to bump cyclesCompleted: %s", prefs_exc)

        except Exception as write_exc:
            logger.exception("queue_service: write failed uid=%s", uid)
            _fail(STATUS_FAILED_WRITE, f"Saving queue failed: {write_exc}")
            return

    except Exception as exc:
        logger.exception("queue_service: unexpected failure uid=%s queue=%s", uid, queue_id)
        _fail(STATUS_FAILED, str(exc))


def start_queue_generation(
    *,
    uid: str,
    filters: dict,
    user_profile: dict,
    resume_text: str,
    cycle_number: int,
    is_free_weekly: bool,
    intent_text: str = "",
    phase: str = "guided",
) -> tuple[str, int]:
    """
    Entry point called by the route layer. Creates the queue doc in
    "processing" state, deducts credits (if applicable), and spawns the
    background worker. Returns (queue_id, credits_charged).

    Raises `InsufficientCreditsError` via the deduction helper if the user
    doesn't have enough credits — caller translates that to a 402.
    """
    db = get_db()

    # TTL cleanup (piggyback on every generate call)
    cleanup_expired_queues(db, uid)

    queue_id = uuid.uuid4().hex
    queue_ref = (
        db.collection("users").document(uid).collection("weekly_queues").document(queue_id)
    )

    now_iso = _now_iso()

    # Credit deduction BEFORE spawning the thread (but AFTER we know the filters
    # are valid — the route layer enforces that).
    # NOTE: design doc said "deduct after PDL returns" to refund on zero
    # results — we implement that by deducting here and refunding inside the
    # background worker on zero-result / all-filtered / failure cases.
    credits_charged = 0
    if not is_free_weekly:
        ok, _new_balance = deduct_credits_atomic(
            uid, QUEUE_GENERATION_CREDITS, "queue_generation"
        )
        if not ok:
            raise _InsufficientCredits(QUEUE_GENERATION_CREDITS)
        credits_charged = QUEUE_GENERATION_CREDITS

    queue_ref.set(
        {
            "generatedAt": now_iso,
            "intentText": intent_text,
            "filters": filters,
            "phase": phase,
            "cycleNumber": cycle_number,
            "status": STATUS_PROCESSING,
            "stage": "queued",
            "contactCount": 0,
            "creditsCharged": credits_charged,
            "isFreeWeekly": is_free_weekly,
            "createdAt": now_iso,
            "updatedAt": now_iso,
        }
    )

    thread = threading.Thread(
        target=generate_queue_background,
        kwargs={
            "uid": uid,
            "queue_id": queue_id,
            "filters": filters,
            "user_profile": user_profile,
            "resume_text": resume_text,
            "credits_charged_on_start": credits_charged,
        },
        daemon=True,
    )
    thread.start()

    return queue_id, credits_charged


class _InsufficientCredits(Exception):
    """Raised by start_queue_generation when credit deduction fails."""

    def __init__(self, needed: int):
        self.needed = needed
        super().__init__(f"Insufficient credits: need {needed}")


# ---------------------------------------------------------------------------
# Fetch helpers (used by routes)
# ---------------------------------------------------------------------------


def get_current_queue(db, uid: str) -> Optional[dict]:
    """
    Return the most recently generated non-archived queue with its contacts
    subcollection embedded. None if no queue exists.
    """
    queues_ref = db.collection("users").document(uid).collection("weekly_queues")
    try:
        docs = list(
            queues_ref.order_by("generatedAt", direction=firestore.Query.DESCENDING)
            .limit(5)
            .stream()
        )
    except Exception:
        docs = list(queues_ref.limit(10).stream())

    # Find the most recent non-archived, non-expired queue
    for doc in docs:
        data = doc.to_dict() or {}
        if data.get("status") in (STATUS_ARCHIVED, STATUS_EXPIRED):
            continue
        queue_id = doc.id
        contacts = []
        try:
            for c_doc in doc.reference.collection("contacts").stream():
                c_data = c_doc.to_dict() or {}
                c_data["id"] = c_doc.id
                contacts.append(c_data)
        except Exception as exc:
            logger.warning("queue_service: failed to load contacts for queue=%s: %s", queue_id, exc)
        data["id"] = queue_id
        data["contacts"] = contacts
        return data

    return None


def get_queue_status(db, uid: str, queue_id: str) -> Optional[dict]:
    """Return a compact status snapshot for polling."""
    queue_ref = (
        db.collection("users").document(uid).collection("weekly_queues").document(queue_id)
    )
    snap = queue_ref.get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    return {
        "id": queue_id,
        "status": data.get("status", "unknown"),
        "stage": data.get("stage", ""),
        "contactCount": data.get("contactCount", 0),
        "errorMessage": data.get("errorMessage"),
        "creditsRefunded": data.get("creditsRefunded", False),
    }


# ---------------------------------------------------------------------------
# Approve / Dismiss — single contact mutations
# ---------------------------------------------------------------------------


def approve_queue_contact(
    *,
    db,
    uid: str,
    queue_id: str,
    contact_id: str,
    user_email: str,
    user_profile: dict,
) -> dict:
    """
    Approve a queued contact:
      1. Create a Gmail draft (compose-URL fallback when OAuth unavailable)
      2. Copy contact into `users/{uid}/contacts/` using normalize_contact shape
      3. Mark queue contact as approved (idempotent — second call returns
         the existing state rather than double-writing)

    Returns a dict with { ok, draftId|null, composeUrl|null, contactId, already }.
    """
    queue_ref = (
        db.collection("users").document(uid).collection("weekly_queues").document(queue_id)
    )
    q_contact_ref = queue_ref.collection("contacts").document(contact_id)

    q_snap = q_contact_ref.get()
    if not q_snap.exists:
        return {"ok": False, "error": "Queue contact not found", "notFound": True}

    q_data = q_snap.to_dict() or {}

    # Idempotency: if already approved, return the existing state
    if q_data.get("status") == "approved":
        return {
            "ok": True,
            "already": True,
            "draftId": q_data.get("gmailDraftId"),
            "contactId": q_data.get("approvedContactId"),
        }

    draft_subject = q_data.get("draftSubject") or ""
    draft_body = q_data.get("draftBody") or ""
    to_email = (q_data.get("email") or "").strip()

    draft_id: Optional[str] = None
    compose_url: Optional[str] = None

    if to_email:
        try:
            from app.services.gmail_client import get_gmail_service_for_user
            import base64
            from email.mime.text import MIMEText

            gmail_service = get_gmail_service_for_user(user_email, user_id=uid)
            if gmail_service:
                from app.services import email_tracking
                tracking_token = email_tracking.start(uid)
                msg = email_tracking.tracked_text_mime(draft_body, tracking_token)
                msg["to"] = to_email
                msg["subject"] = draft_subject
                raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
                draft = (
                    gmail_service.users()
                    .drafts()
                    .create(userId="me", body={"message": {"raw": raw}})
                    .execute()
                )
                draft_id = draft.get("id") or None
                email_tracking.register(
                    tracking_token, uid, "outreach", recipient_email=to_email,
                    draft_id=draft_id,
                    thread_id=draft.get("message", {}).get("threadId"),
                )
            else:
                from urllib.parse import quote

                compose_url = (
                    "https://mail.google.com/mail/?view=cm"
                    f"&to={quote(to_email)}"
                    f"&su={quote(draft_subject)}"
                    f"&body={quote(draft_body)}"
                )
        except Exception as draft_exc:
            logger.warning("queue_service: Gmail draft failed for uid=%s: %s", uid, draft_exc)
            from urllib.parse import quote

            compose_url = (
                "https://mail.google.com/mail/?view=cm"
                f"&to={quote(to_email)}"
                f"&su={quote(draft_subject)}"
                f"&body={quote(draft_body)}"
            )

    # Copy contact into users/{uid}/contacts/ using normalize_contact shape
    from app.models.contact import normalize_contact

    normalized = normalize_contact(
        {
            "FirstName": q_data.get("firstName") or "",
            "LastName": q_data.get("lastName") or "",
            "LinkedIn": q_data.get("linkedinUrl") or "",
            "Email": q_data.get("email") or "",
            "Title": q_data.get("title") or "",
            "Company": q_data.get("company") or "",
            "City": q_data.get("city") or "",
            "State": q_data.get("state") or "",
            "College": q_data.get("college") or "",
            "pdlId": q_data.get("pdlId") or "",
        }
    )

    # Also set the camelCase fields the existing Pipeline view reads so the
    # contact renders without a migration step.
    now_iso = _now_iso()
    normalized.update(
        {
            "firstName": normalized["FirstName"],
            "lastName": normalized["LastName"],
            "email": normalized["Email"],
            "linkedinUrl": normalized["LinkedIn"],
            "title": normalized["Title"],
            "jobTitle": normalized["Title"],
            "company": normalized["Company"],
            "city": normalized["City"],
            "state": normalized["State"],
            "college": normalized["College"],
            "userId": uid,
            "createdAt": now_iso,
            "lastActivityAt": now_iso,
            "source": "queue",
            "sourceQueueId": queue_id,
            "emailSubject": draft_subject,
            "emailBody": draft_body,
            "pipelineStage": "draft_created",
            "draftCreatedAt": now_iso,
            "emailGeneratedAt": now_iso,
            "status": "drafted" if draft_id else "Not Contacted",
        }
    )
    if draft_id:
        normalized["gmailDraftId"] = draft_id
    if compose_url:
        normalized["gmailDraftUrl"] = compose_url

    contacts_ref = db.collection("users").document(uid).collection("contacts")
    created = contacts_ref.add(normalized)
    # contacts_ref.add() returns (update_time, doc_ref) in the Python client
    new_contact_ref = created[1] if isinstance(created, tuple) else created
    new_contact_id = getattr(new_contact_ref, "id", "")

    q_contact_ref.update(
        {
            "status": "approved",
            "approvedAt": now_iso,
            "gmailDraftId": draft_id,
            "gmailComposeUrl": compose_url,
            "approvedContactId": new_contact_id,
        }
    )

    return {
        "ok": True,
        "already": False,
        "draftId": draft_id,
        "composeUrl": compose_url,
        "contactId": new_contact_id,
    }


def dismiss_queue_contact(
    *,
    db,
    uid: str,
    queue_id: str,
    contact_id: str,
    reason: str,
) -> dict:
    """
    Dismiss a queued contact, optionally adding its company/title to the
    user's blocklist depending on the reason.
    """
    if reason not in VALID_DISMISS_REASONS:
        return {"ok": False, "error": f"Invalid reason '{reason}'"}

    queue_ref = (
        db.collection("users").document(uid).collection("weekly_queues").document(queue_id)
    )
    q_contact_ref = queue_ref.collection("contacts").document(contact_id)
    snap = q_contact_ref.get()
    if not snap.exists:
        return {"ok": False, "error": "Queue contact not found", "notFound": True}

    data = snap.to_dict() or {}
    if data.get("status") == "dismissed":
        return {"ok": True, "already": True}

    now_iso = _now_iso()
    q_contact_ref.update(
        {
            "status": "dismissed",
            "dismissReason": reason,
            "dismissedAt": now_iso,
        }
    )

    # Feed the blocklist
    if reason == DISMISS_WRONG_COMPANY:
        _add_to_blocklist(db, uid, company=data.get("company") or "")
    elif reason == DISMISS_WRONG_PERSON:
        _add_to_blocklist(db, uid, title=data.get("title") or "")
    # DISMISS_NOT_NOW does not feed the blocklist

    return {"ok": True, "already": False}


# ---------------------------------------------------------------------------
# Tier / eligibility check (used by route layer)
# ---------------------------------------------------------------------------


def is_queue_feature_enabled(tier: str) -> bool:
    """Pro/Elite get the real queue; Free sees the static teaser only."""
    return tier in ("pro", "elite")


def is_free_weekly_eligible(db, uid: str, tier: str) -> bool:
    """
    Pro/Elite users get one free queue per ISO week (the automatic Monday
    run). Any additional generation in the same week costs 15 credits.

    Phase 1 logic: we count queues with `isFreeWeekly=True` AND
    `generatedAt` inside the current ISO week. Returns True if the user
    has not yet spent their free weekly queue.
    """
    if tier not in ("pro", "elite"):
        return False

    week_key = _iso_week_key()
    queues_ref = db.collection("users").document(uid).collection("weekly_queues")
    try:
        docs = list(
            queues_ref.where("isFreeWeekly", "==", True)
            .where("weekKey", "==", week_key)
            .limit(1)
            .stream()
        )
        return len(docs) == 0
    except Exception:
        # If the compound query fails (missing index on fresh install),
        # fall back to the last 10 queues and check in memory.
        try:
            docs = list(
                queues_ref.order_by("generatedAt", direction=firestore.Query.DESCENDING)
                .limit(10)
                .stream()
            )
            for d in docs:
                data = d.to_dict() or {}
                if data.get("isFreeWeekly") and data.get("weekKey") == week_key:
                    return False
            return True
        except Exception:
            return True


# ---------------------------------------------------------------------------
# Scanner entry point (Tracker daemon contract)
# ---------------------------------------------------------------------------
#
# This is the function the shared tracker daemon calls every 6 hours.
# Contract: docs/designs/tracker-daemon-contract.md
#
# Dispatch cadence: every 6 hours invocation, with an internal gate — returns
# early unless today is Tuesday UTC OR the last successful run was more than
# 6d 20h ago. The outer loop gives us multiple chances to fire on the right
# calendar Tuesday even if one iteration is briefly delayed.
#
# Health doc: `system/queue_scanner` with the fields defined in the contract
# (lastSuccessAt, lastDurationMs, usersProcessed, queuesGenerated, errorCount).
# The watchdog at wsgi.py:378 reads this doc and alerts if lastSuccessAt is
# older than 7 days.

# Gate constants — exposed for tests so the window stays in sync with the doc.
QUEUE_TUESDAY_WEEKDAY = 1  # datetime.weekday(): Mon=0, Tue=1, ..., Sun=6
QUEUE_STALENESS_SECONDS = int((6 * 24 + 20) * 3600)  # 6d 20h


def _should_run_queue_scanner(db, now: Optional[datetime] = None) -> bool:
    """
    Return True if the queue scanner should execute this 6-hour tick.

    Rule (per daemon contract): run if today is Tuesday OR the last successful
    run was more than 6d 20h ago. Separate OR clauses — the staleness path is
    the recovery mechanism when Tuesday dispatch was disabled or crashed.
    """
    now = now or datetime.now(timezone.utc)
    if now.weekday() == QUEUE_TUESDAY_WEEKDAY:
        return True

    try:
        doc = db.collection("system").document("queue_scanner").get()
        if not doc.exists:
            # First-ever run: fall through to the Tuesday gate (which is False
            # here if we're not Tuesday). Do NOT auto-run on non-Tuesday first
            # boot — that would fire the Monday-after-deploy queue a day early.
            return False
        data = doc.to_dict() or {}
        last_success = data.get("lastSuccessAt")
        if not last_success:
            return False
        last_dt = datetime.fromisoformat(
            str(last_success).replace("Z", "+00:00")
        )
        age = (now - last_dt).total_seconds()
        return age > QUEUE_STALENESS_SECONDS
    except Exception as exc:
        logger.warning("queue_scanner: gate check failed, skipping: %s", exc)
        return False


def _write_queue_scanner_health(
    db,
    *,
    users_processed: int,
    queues_generated: int,
    error_count: int,
    duration_ms: int,
) -> None:
    """Write the health doc consumed by the wsgi.py watchdog."""
    try:
        db.collection("system").document("queue_scanner").set({
            "lastSuccessAt": _now_iso(),
            "lastDurationMs": int(duration_ms),
            "usersProcessed": int(users_processed),
            "queuesGenerated": int(queues_generated),
            "errorCount": int(error_count),
        })
    except Exception as exc:
        logger.warning("queue_scanner: health doc write failed: %s", exc)


def scan_and_generate_queues() -> None:
    """
    Scanner entry point invoked by the tracker daemon loop every 6 hours.

    Flow:
      1. Check the Tuesday / staleness gate — return early otherwise.
      2. Iterate users. For each Pro/Elite user with queue enabled and not
         paused, and who hasn't already consumed their free weekly queue,
         count them as an eligible candidate.
      3. Write the health doc.

    NOTE: The per-user queue *generation* step is intentionally left as a
    hook (see the `# PHASE 2:` comment below). The scanner scaffold —
    gating, user iteration, health doc, error isolation — is what the
    daemon contract requires. Wiring the actual generation to `start_queue_generation`
    is Phase 2 feature work and should land in its own PR so the queue
    credit/refund flow is reviewed separately from the daemon wiring.
    """
    import time as _time

    db = get_db()
    if db is None:
        logger.warning("queue_scanner: no db client available, skipping run")
        return

    if not _should_run_queue_scanner(db):
        logger.info("queue_scanner: gate closed (not Tuesday and not stale)")
        return

    started = _time.time()
    users_processed = 0
    queues_generated = 0
    error_count = 0

    logger.info("queue_scanner: starting scan")

    try:
        for user_doc in db.collection("users").stream():
            uid = user_doc.id
            user_data = user_doc.to_dict() or {}
            users_processed += 1

            try:
                tier = user_data.get(
                    "subscriptionTier", user_data.get("tier", "free")
                )
                if not is_queue_feature_enabled(tier):
                    continue

                prefs = get_queue_preferences(db, uid)
                if not prefs.get("enabled", True) or prefs.get("paused"):
                    continue

                if not is_free_weekly_eligible(db, uid, tier):
                    # Already got their free queue this week
                    continue

                # PHASE 2: invoke per-user queue generation here.
                # For now, count the user as "would have generated" so the
                # health doc reflects real scanner activity and Phase 2
                # can drop in the real call without another daemon change.
                queues_generated += 1
            except Exception as per_user_exc:
                error_count += 1
                logger.warning(
                    "queue_scanner: per-user failure uid=%s: %s",
                    uid, per_user_exc,
                )
                # Do NOT re-raise — one bad user doc must not kill the scan.
    except Exception:
        error_count += 1
        logger.exception("queue_scanner: scan iteration failed")

    duration_ms = int((_time.time() - started) * 1000)
    _write_queue_scanner_health(
        db,
        users_processed=users_processed,
        queues_generated=queues_generated,
        error_count=error_count,
        duration_ms=duration_ms,
    )
    logger.info(
        "queue_scanner: done users=%d eligible=%d errors=%d duration_ms=%d",
        users_processed, queues_generated, error_count, duration_ms,
    )
