"""
Nudge service: scan contacts, generate follow-up nudges, persist to Firestore.

Background daemon calls scan_and_generate_nudges() every 6 hours.
Each nudge includes AI-generated follow-up suggestion text and an optional
ready-to-send follow-up email draft (cherry-pick #1 from CEO review).
"""
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

from google.cloud.firestore_v1 import transactional

from app.extensions import get_db
from app.models.pipeline_stages import AWAITING_REPLY_STAGES
from app.services.openai_client import get_openai_client
from app.services.outbox_service import _parse_iso
from app.utils.users import (
    get_university_variants,
    get_user_name,
    get_user_school,
    get_user_major,
    get_user_career_track,
)

logger = logging.getLogger("nudge_service")

# Contacts in these stages are eligible for follow-up nudges
NUDGE_ELIGIBLE_STAGES = AWAITING_REPLY_STAGES | frozenset({"draft_created"})

# Default follow-up timing: 7 calendar days (CEO review decision)
DEFAULT_FOLLOWUP_DAYS = 7

# Max nudges per user per day (frequency cap)
MAX_NUDGES_PER_USER_PER_DAY = 3

# Distributed lock TTL (seconds) — prevents duplicate scans across Gunicorn workers
LOCK_TTL_SECONDS = 3600  # 1 hour

# Max contacts to process in parallel
MAX_WORKERS = 10

# Nudge TTL: auto-delete dismissed/acted nudges older than this
NUDGE_TTL_DAYS = 30


# ---------------------------------------------------------------------------
# Distributed lock (Firestore transaction — atomic, no TOCTOU race)
# ---------------------------------------------------------------------------

def _acquire_lock(db) -> bool:
    """
    Attempt to acquire a distributed lock for the nudge scanner.
    Uses a Firestore transaction to prevent TOCTOU race between workers.
    Returns True if lock acquired, False if another worker holds it.
    """
    lock_ref = db.collection("system").document("nudge_scanner_lock")

    @transactional
    def _try_acquire(transaction):
        lock_snap = lock_ref.get(transaction=transaction)
        now = time.time()

        if lock_snap.exists:
            lock_data = lock_snap.to_dict() or {}
            acquired_at = lock_data.get("acquiredAt", 0)
            if now - acquired_at < LOCK_TTL_SECONDS:
                return False  # Lock held by another worker

        transaction.set(lock_ref, {
            "acquiredAt": now,
            "acquiredBy": f"worker-{os.getpid()}",
            "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        })
        return True

    try:
        transaction = db.transaction()
        acquired = _try_acquire(transaction)
        if not acquired:
            logger.info("Nudge scanner lock held by another worker, skipping")
        return acquired
    except Exception as e:
        logger.error("Failed to acquire nudge scanner lock: %s", e)
        return False


def _release_lock(db):
    """Release the distributed lock."""
    try:
        db.collection("system").document("nudge_scanner_lock").delete()
    except Exception as e:
        logger.warning("Failed to release nudge scanner lock: %s", e)


def _update_healthcheck(
    db,
    nudges_generated: int,
    users_scanned: int,
    contacts_scanned: int,
    errors: int,
    duration_ms: int,
):
    """
    Write healthcheck doc consumed by the daemon watchdog in wsgi.py.
    Field names follow docs/designs/tracker-daemon-contract.md.
    """
    try:
        db.collection("system").document("nudge_scanner").set({
            "lastSuccessAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "lastDurationMs": int(duration_ms),
            "contactsScanned": int(contacts_scanned),
            "nudgesGenerated": int(nudges_generated),
            "errorCount": int(errors),
            # Kept for operator visibility — not part of the contract.
            "usersScanned": int(users_scanned),
        })
    except Exception as e:
        logger.warning("Failed to update healthcheck: %s", e)


# ---------------------------------------------------------------------------
# Contact scanning (server-side Firestore filters)
# ---------------------------------------------------------------------------

def _get_eligible_contacts(db, uid: str, followup_days: int = DEFAULT_FOLLOWUP_DAYS):
    """
    Query contacts eligible for follow-up nudges using Firestore server-side
    filters where possible, with client-side fallback for complex predicates.
    """
    contacts_ref = db.collection("users").document(uid).collection("contacts")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=followup_days)
    max_age = now - timedelta(days=30)  # 30-day initial rollout cutoff

    cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")
    max_age_iso = max_age.isoformat().replace("+00:00", "Z")

    eligible = []
    try:
        # Server-side filter: emailGeneratedAt within the nudge window
        # This reduces reads from all contacts to only those with emails in range
        query = (
            contacts_ref
            .where("emailGeneratedAt", ">=", max_age_iso)
            .where("emailGeneratedAt", "<=", cutoff_iso)
        )
        docs = list(query.stream())
    except Exception:
        # Fallback if composite index doesn't exist yet — scan all contacts
        logger.warning("Firestore index missing for emailGeneratedAt range query on uid=%s, falling back to full scan", uid)
        docs = list(contacts_ref.stream())

    for doc in docs:
        data = doc.to_dict() or {}
        stage = data.get("pipelineStage") or ""
        if stage not in NUDGE_ELIGIBLE_STAGES:
            continue
        if data.get("archivedAt"):
            continue

        # Check snooze
        snoozed = data.get("snoozedUntil")
        if snoozed:
            try:
                if datetime.fromisoformat(snoozed.replace("Z", "+00:00")) > now:
                    continue
            except (ValueError, TypeError):
                pass

        sent_at = _parse_iso(data.get("emailGeneratedAt") or data.get("emailSentAt"))
        if not sent_at:
            continue
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        if sent_at > cutoff:
            continue
        if sent_at < max_age:
            continue

        # Skip if already has a recent pending nudge
        if data.get("lastNudgeAt"):
            last_nudge = _parse_iso(data["lastNudgeAt"])
            if last_nudge:
                if last_nudge.tzinfo is None:
                    last_nudge = last_nudge.replace(tzinfo=timezone.utc)
                if last_nudge > cutoff:
                    continue

        eligible.append({"id": doc.id, **data})

    return eligible


# ---------------------------------------------------------------------------
# AI nudge generation (GPT-4o-mini)
# ---------------------------------------------------------------------------

def _generate_nudge_text(contact: dict, user_data: dict, news_hook: str = "") -> dict | None:
    """
    Generate nudge suggestion text and a follow-up email draft using GPT-4o-mini.
    Returns {"suggestion": str, "followUpDraft": str} or None on failure.
    """
    client = get_openai_client()
    if not client:
        logger.warning("OpenAI client not available, skipping nudge generation")
        return None

    contact_name = (
        f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip()
        or contact.get("name", "Unknown")
    )
    company = contact.get("company") or "their company"
    title = contact.get("jobTitle") or ""
    college = contact.get("college") or ""
    days_elapsed = DEFAULT_FOLLOWUP_DAYS
    sent_at = _parse_iso(contact.get("emailGeneratedAt") or contact.get("emailSentAt"))
    if sent_at:
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        days_elapsed = (datetime.now(timezone.utc) - sent_at).days

    user_name = get_user_name(user_data)
    user_school = get_user_school(user_data)
    user_major = get_user_major(user_data)
    user_career_track = get_user_career_track(user_data)

    # Detect genuinely shared facts using variant-aware matching
    shared_facts = []
    contact_only_facts = []

    user_uni_variants = get_university_variants(user_school)
    contact_uni_variants = get_university_variants(college) if college else set()
    if user_uni_variants and contact_uni_variants and (user_uni_variants & contact_uni_variants):
        shared_facts.append(f"Same university: {user_school}")
    elif college:
        contact_only_facts.append(f"Attended {college}")

    shared_section = ""
    if shared_facts:
        shared_section = "\nSHARED BACKGROUND (you may reference as common ground):\n" + "\n".join(f"- {f}" for f in shared_facts)
    if contact_only_facts:
        shared_section += "\nCONTACT-ONLY FACTS (reference as their background, NOT as shared):\n" + "\n".join(f"- {f}" for f in contact_only_facts)

    prompt = f"""You are helping a college student follow up on a networking email they sent {days_elapsed} days ago that hasn't received a reply.

STUDENT (the sender — these are the ONLY facts about the student):
- Name: {user_name}
- University: {user_school or 'not specified'}
- Major: {user_major or 'not specified'}
- Target industry: {user_career_track or 'not specified'}

CONTACT (the recipient):
- Name: {contact_name}
- Role: {title} at {company}
- University: {college or 'not specified'}
{shared_section}

{"RECENT NEWS HOOK (use this to make the follow-up timely and specific):\n- " + news_hook + chr(10) if news_hook else ""}RULES:
- NEVER say "as a fellow X" or "you both attended X" unless X appears in SHARED BACKGROUND above.
- If there is no shared background, reference the CONTACT's role or company as the reason to follow up.
- Do not invent facts about the student that are not listed above.
- If a RECENT NEWS HOOK is provided, reference it naturally in the follow-up (e.g. "I saw {company} just...").

Generate two things:
1. SUGGESTION: A brief 1-2 sentence nudge explaining why they should follow up and what angle to take (20-40 words).
2. DRAFT: A short follow-up email (60-100 words) that's warm, specific, and references the original outreach without being pushy. No subject line needed. Sign off with just the student's first name.

Format your response exactly as:
SUGGESTION: [your suggestion]
DRAFT: [your email draft]"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You write concise, genuine networking follow-up suggestions for college students. Be specific, not generic."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=300,
            temperature=0.7,
        )
        text = response.choices[0].message.content or ""

        # Parse the response
        suggestion = ""
        draft = ""
        if "SUGGESTION:" in text and "DRAFT:" in text:
            parts = text.split("DRAFT:")
            suggestion = parts[0].replace("SUGGESTION:", "").strip()
            draft = parts[1].strip()
        else:
            # Fallback: use the whole response as suggestion
            suggestion = text.strip()[:200]

        if not suggestion:
            return None

        return {
            "suggestion": suggestion,
            "followUpDraft": draft or "",
        }
    except Exception as e:
        logger.error("GPT-4o-mini nudge generation failed for contact %s: %s",
                      contact.get("id"), e)
        return None


def _generate_template_nudge(contact: dict) -> dict:
    """Fallback template nudge when LLM is unavailable."""
    contact_name = (
        f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip()
        or contact.get("name", "Unknown")
    )
    company = contact.get("company") or "their company"
    days_elapsed = DEFAULT_FOLLOWUP_DAYS
    sent_at = _parse_iso(contact.get("emailGeneratedAt") or contact.get("emailSentAt"))
    if sent_at:
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        days_elapsed = (datetime.now(timezone.utc) - sent_at).days

    return {
        "suggestion": (
            f"You emailed {contact_name} at {company} {days_elapsed} days ago. "
            f"A brief, friendly follow-up can significantly increase your reply rate."
        ),
        "followUpDraft": "",
    }


# ---------------------------------------------------------------------------
# Nudge persistence
# ---------------------------------------------------------------------------

def _create_nudge(db, uid: str, contact: dict, nudge_text: dict) -> str | None:
    """
    Write a nudge document to users/{uid}/nudges/ and update the contact's lastNudgeAt.
    Returns the nudge document ID, or None on failure.
    """
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    contact_id = contact.get("id") or contact.get("doc_id", "")
    contact_name = (
        f"{contact.get('firstName', '')} {contact.get('lastName', '')}".strip()
        or contact.get("name", "Unknown")
    )

    nudge_data = {
        "contactId": contact_id,
        "contactName": contact_name,
        "company": contact.get("company") or "",
        "type": "follow_up",
        "generatedMessage": nudge_text.get("suggestion", ""),
        "followUpDraft": nudge_text.get("followUpDraft", ""),
        "createdAt": now_iso,
        "status": "pending",
        "actedOn": False,
        "followUpOutcome": None,
    }

    try:
        # Check for existing pending nudge for this contact (dedup)
        nudges_ref = db.collection("users").document(uid).collection("nudges")
        existing = list(
            nudges_ref
            .where("contactId", "==", contact_id)
            .where("status", "==", "pending")
            .limit(1)
            .stream()
        )
        if existing:
            logger.debug("Pending nudge already exists for uid=%s contact=%s, skipping", uid, contact_id)
            return None

        # Create nudge document
        _, nudge_ref = nudges_ref.add(nudge_data)
        nudge_id = nudge_ref.id

        # Update contact's lastNudgeAt
        db.collection("users").document(uid).collection("contacts").document(contact_id).update({
            "lastNudgeAt": now_iso,
        })

        return nudge_id
    except Exception as e:
        logger.error("Failed to create nudge for uid=%s contact=%s: %s", uid, contact_id, e)
        return None


# ---------------------------------------------------------------------------
# Nudge cleanup (TTL for old dismissed/acted nudges)
# ---------------------------------------------------------------------------

def _cleanup_old_nudges(db, uid: str):
    """Delete nudges older than NUDGE_TTL_DAYS that are dismissed or acted_on.

    Uses a single-field equality query (no composite index needed) and filters
    by createdAt in memory. Bounded by a fetch limit per status to cap cost.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=NUDGE_TTL_DAYS)
    cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")
    nudges_ref = db.collection("users").document(uid).collection("nudges")
    deleted = 0
    try:
        for status in ("dismissed", "acted_on"):
            # Single-field equality — no composite index required.
            candidates = list(
                nudges_ref
                .where("status", "==", status)
                .limit(200)
                .stream()
            )
            for doc in candidates:
                data = doc.to_dict() or {}
                created_at = data.get("createdAt") or ""
                if isinstance(created_at, str) and created_at and created_at <= cutoff_iso:
                    doc.reference.delete()
                    deleted += 1
                    if deleted >= 50:
                        break
            if deleted >= 50:
                break
    except Exception as e:
        logger.warning("Nudge cleanup failed for uid=%s: %s", uid, e)
    if deleted:
        logger.debug("Cleaned up %d old nudges for uid=%s", deleted, uid)


# ---------------------------------------------------------------------------
# Stuck-Student Intervention (Sprint 3B)
# ---------------------------------------------------------------------------

# Copy variations by subtype
STUCK_STUDENT_COPY = {
    "new_no_emails": "Ready to send your first email? Here are 3 great people to start with.",
    "new_no_replies": "First emails are tough — let's try a different angle. Here are 3 new contacts.",
    "established_inactive": "Let's get you back on track — here are 3 contacts worth reaching out to this week.",
}


def _check_student_activity(db, uid: str, user_data: dict):
    """
    Age-differentiated stuck-student detection.
    Creates a stuck_student nudge with suggestions if trigger conditions met.
    Deduplicates by synthetic contactId per subtype.
    """
    created_at_raw = user_data.get("createdAt")
    if not created_at_raw:
        return

    try:
        # createdAt may be a Firestore timestamp object or an ISO string
        if hasattr(created_at_raw, 'timestamp'):
            created_at = created_at_raw.replace(tzinfo=timezone.utc) if created_at_raw.tzinfo is None else created_at_raw
        else:
            created_at = datetime.fromisoformat(str(created_at_raw).replace("Z", "+00:00"))
    except Exception:
        return

    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    days_since_signup = (now - created_at).days

    # Gather activity data
    contacts_ref = db.collection("users").document(uid).collection("contacts")
    try:
        all_contacts = list(contacts_ref.limit(200).stream())
    except Exception:
        return

    emails_sent_count = 0
    has_reply = False
    last_activity = created_at

    for doc in all_contacts:
        data = doc.to_dict() or {}
        email_at = data.get("emailGeneratedAt") or data.get("emailSentAt")
        if email_at:
            emails_sent_count += 1
            try:
                email_dt = datetime.fromisoformat(email_at.replace("Z", "+00:00"))
                if email_dt.tzinfo is None:
                    email_dt = email_dt.replace(tzinfo=timezone.utc)
                if email_dt > last_activity:
                    last_activity = email_dt
            except (ValueError, TypeError):
                pass
        if data.get("replyReceivedAt"):
            has_reply = True

    # Determine which subtype triggers
    subtype = None

    if days_since_signup <= 14:
        # New user triggers
        if emails_sent_count == 0 and days_since_signup >= 3:
            subtype = "new_no_emails"
        elif emails_sent_count > 0 and not has_reply and days_since_signup >= 5:
            subtype = "new_no_replies"
    else:
        # Established user trigger: 7 days of inactivity
        days_inactive = (now - last_activity).days
        if days_inactive >= 7:
            subtype = "established_inactive"

    if not subtype:
        return

    # Generate suggestions using GPT-4o-mini
    suggestions = _generate_stuck_suggestions(user_data, subtype)
    copy_text = STUCK_STUDENT_COPY.get(subtype, "")

    # Create nudge with synthetic contactId for dedup
    nudge_data = {
        "contactId": f"__stuck_student__{subtype}",
        "contactName": "",
        "company": "",
        "type": "stuck_student",
        "subtype": subtype,
        "generatedMessage": copy_text,
        "followUpDraft": "",
        "suggestions": suggestions,
        "createdAt": now.isoformat().replace("+00:00", "Z"),
        "status": "pending",
        "actedOn": False,
        "followUpOutcome": None,
    }

    # Use _create_nudge's dedup logic via the synthetic contactId
    _create_nudge_raw(db, uid, nudge_data)


def _generate_stuck_suggestions(user_data: dict, subtype: str) -> list:
    """Generate 3 contact search suggestions using GPT-4o-mini."""
    client = get_openai_client()
    if not client:
        return _fallback_suggestions(user_data)

    professional = user_data.get("professionalInfo") or {}
    goals = user_data.get("goals") or {}
    career_track = (
        professional.get("careerTrack")
        or user_data.get("careerTrack")
        or goals.get("careerTrack")
        or ""
    )
    dream_companies = goals.get("dreamCompanies") or user_data.get("dreamCompanies") or []
    if isinstance(dream_companies, str):
        dream_companies = [c.strip() for c in dream_companies.split(",") if c.strip()]

    prompt = f"""Suggest 3 specific people to reach out to for a college student networking in {career_track or 'their target industry'}.

Dream companies: {', '.join(dream_companies[:3]) if dream_companies else 'not specified'}
Subtype: {subtype}

Return ONLY valid JSON array with exactly 3 objects:
[{{"title": "VP of Product", "company": "Google", "reason": "Strong alumni connection, recently promoted"}}]

Be specific about titles and companies. If dream companies are specified, use those. Otherwise suggest well-known firms in the target industry."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You suggest specific networking targets for college students. Return only valid JSON."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=300,
            temperature=0.7,
        )
        import json
        text = response.choices[0].message.content or "[]"
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        suggestions = json.loads(text)
        if isinstance(suggestions, list) and len(suggestions) >= 1:
            return suggestions[:3]
    except Exception as e:
        logger.warning("Stuck student suggestion generation failed for uid: %s", e)

    return _fallback_suggestions(user_data)


def _fallback_suggestions(user_data: dict) -> list:
    """Deterministic fallback suggestions when LLM is unavailable."""
    professional = user_data.get("professionalInfo") or {}
    goals = user_data.get("goals") or {}
    dream_companies = goals.get("dreamCompanies") or user_data.get("dreamCompanies") or []
    if isinstance(dream_companies, str):
        dream_companies = [c.strip() for c in dream_companies.split(",") if c.strip()]

    if dream_companies:
        return [
            {"title": "Analyst", "company": dream_companies[0], "reason": "Your top dream company"},
            {"title": "Associate", "company": dream_companies[1] if len(dream_companies) > 1 else dream_companies[0], "reason": "Great for learning about the industry"},
            {"title": "VP", "company": dream_companies[0], "reason": "Senior perspective on recruiting"},
        ]

    return [
        {"title": "Analyst", "company": "a top firm", "reason": "Great starting point for networking"},
        {"title": "Associate", "company": "a target company", "reason": "Recently went through recruiting"},
        {"title": "VP", "company": "a dream firm", "reason": "Can refer you to the right people"},
    ]


def _create_nudge_raw(db, uid: str, nudge_data: dict) -> str | None:
    """
    Write a nudge document with dedup check on contactId.
    Similar to _create_nudge but accepts pre-built nudge_data dict.
    """
    contact_id = nudge_data.get("contactId", "")
    try:
        nudges_ref = db.collection("users").document(uid).collection("nudges")
        existing = list(
            nudges_ref
            .where("contactId", "==", contact_id)
            .where("status", "==", "pending")
            .limit(1)
            .stream()
        )
        if existing:
            return None

        _, nudge_ref = nudges_ref.add(nudge_data)
        return nudge_ref.id
    except Exception as e:
        logger.error("Failed to create stuck_student nudge for uid=%s: %s", uid, e)
        return None


# ---------------------------------------------------------------------------
# Main scan orchestrator
# ---------------------------------------------------------------------------

def scan_and_generate_nudges():
    """
    Main entry point called by the daemon thread.
    Scans all users, finds eligible contacts, generates nudges.
    Uses distributed lock to prevent duplicate scans across workers.
    """
    if os.environ.get("NUDGES_ENABLED", "true").lower() == "false":
        logger.info("Nudges disabled via NUDGES_ENABLED=false, skipping scan")
        return

    db = get_db()

    if not _acquire_lock(db):
        return

    try:
        _run_scan(db)
    finally:
        _release_lock(db)


def _run_scan(db):
    """Execute the nudge scan (called after lock is acquired)."""
    logger.info("Nudge scan starting")
    scan_started = time.time()
    users_scanned = 0
    contacts_scanned = 0
    total_nudges = 0
    total_errors = 0

    for user_doc in db.collection("users").stream():
        uid = user_doc.id
        user_data = user_doc.to_dict() or {}

        # Respect user's nudge opt-out preference
        if user_data.get("nudgesEnabled") is False:
            users_scanned += 1
            continue

        # Read user-configured preferences with defaults
        followup_days = user_data.get("nudgeFollowUpDays", DEFAULT_FOLLOWUP_DAYS)
        max_per_day = user_data.get("nudgeMaxPerDay", MAX_NUDGES_PER_USER_PER_DAY)

        try:
            # Stuck-student intervention check (Sprint 3B)
            _check_student_activity(db, uid, user_data)

            eligible = _get_eligible_contacts(db, uid, followup_days=followup_days)
            contacts_scanned += len(eligible)
            if not eligible:
                users_scanned += 1
                # Still run cleanup even if no eligible contacts
                _cleanup_old_nudges(db, uid)
                continue

            # Respect frequency cap using proper datetime comparison
            today_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            today_start_iso = today_start.isoformat().replace("+00:00", "Z")
            nudges_today = 0
            try:
                nudges_ref = db.collection("users").document(uid).collection("nudges")
                today_nudges = list(
                    nudges_ref
                    .where("createdAt", ">=", today_start_iso)
                    .limit(max_per_day + 1)
                    .stream()
                )
                nudges_today = len(today_nudges)
            except Exception as e:
                logger.warning("Failed to count today's nudges for uid=%s: %s", uid, e)

            remaining = max_per_day - nudges_today
            if remaining <= 0:
                users_scanned += 1
                _cleanup_old_nudges(db, uid)
                continue

            contacts_to_nudge = eligible[:remaining]

            # Generate nudges in parallel using ThreadPoolExecutor
            def process_contact(contact):
                nudge_text = _generate_nudge_text(contact, user_data)
                if not nudge_text:
                    nudge_text = _generate_template_nudge(contact)
                return contact, nudge_text

            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(contacts_to_nudge))) as executor:
                futures = {
                    executor.submit(process_contact, c): c
                    for c in contacts_to_nudge
                }
                for future in as_completed(futures):
                    try:
                        contact, nudge_text = future.result()
                        nudge_id = _create_nudge(db, uid, contact, nudge_text)
                        if nudge_id:
                            total_nudges += 1
                    except Exception as e:
                        total_errors += 1
                        logger.error("Nudge generation failed for uid=%s: %s", uid, e)

            # Cleanup old nudges after processing
            _cleanup_old_nudges(db, uid)
            users_scanned += 1

        except Exception as e:
            total_errors += 1
            users_scanned += 1
            logger.error("Error processing user uid=%s: %s", uid, e)

    duration_ms = int((time.time() - scan_started) * 1000)
    _update_healthcheck(
        db,
        nudges_generated=total_nudges,
        users_scanned=users_scanned,
        contacts_scanned=contacts_scanned,
        errors=total_errors,
        duration_ms=duration_ms,
    )
    logger.info(
        "Nudge scan complete: users=%d contacts=%d nudges=%d errors=%d duration=%dms",
        users_scanned, contacts_scanned, total_nudges, total_errors, duration_ms,
    )


# ---------------------------------------------------------------------------
# Nudge invalidation (called from gmail_webhook on reply detection)
# ---------------------------------------------------------------------------

def dismiss_pending_nudges_for_contact(db, uid: str, contact_id: str):
    """
    When a reply is detected, dismiss any pending nudges for that contact.
    Prevents "follow up with Sarah" appearing alongside "Sarah replied!".
    """
    try:
        nudges_ref = db.collection("users").document(uid).collection("nudges")
        pending = list(
            nudges_ref
            .where("contactId", "==", contact_id)
            .where("status", "==", "pending")
            .stream()
        )
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        for doc in pending:
            doc.reference.update({
                "status": "dismissed",
                "dismissedAt": now_iso,
                "dismissReason": "reply_received",
            })
        if pending:
            logger.info("Dismissed %d pending nudge(s) for uid=%s contact=%s",
                         len(pending), uid, contact_id)
    except Exception as e:
        logger.warning("Failed to dismiss nudges for uid=%s contact=%s: %s",
                        uid, contact_id, e)
