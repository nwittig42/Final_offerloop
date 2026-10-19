"""
Outbox / Network Tracker service layer.

All business logic for the outbox feature lives here.
Routes call these functions; they should never touch Gmail or Firestore directly.
"""
import html
import logging
import re
from datetime import datetime, timedelta, timezone

from google.cloud.firestore_v1 import transactional

from app.extensions import get_db
from app.models.pipeline_stages import (
    AWAITING_REPLY_STAGES,
    EMAIL_OPENED,
    WAITING_ON_REPLY,
    stage_after_send_confirmed,
)
from app.services.gmail_client import (
    _load_user_gmail_creds,
    _gmail_service,
    get_full_thread_chain,
    sync_thread_message,
)

logger = logging.getLogger(__name__)

# Canonical pipeline stages
ALLOWED_PIPELINE_STAGES = frozenset({
    "new", "draft_created", "draft_deleted", "email_sent", "waiting_on_reply",
    "email_opened", "replied",
    "meeting_scheduled", "connected", "no_response", "bounced", "closed",
})

DONE_STAGES = frozenset({
    "connected", "meeting_scheduled", "no_response", "bounced", "closed",
})

# Stages that imply a reply was received (for reply rate calculation)
REPLIED_STAGES = frozenset({"replied", "meeting_scheduled", "connected"})

VALID_RESOLUTIONS = frozenset({
    "meeting_booked", "soft_no", "hard_no", "ghosted", "completed",
})

# How long before a sync is considered stale (seconds)
SYNC_LOCK_SECONDS = 60

# If a contact is still in draft_created this long after the draft was created,
# the Gmail webhook likely never matched a sent-message back to this contact
# (e.g. user sent from a different address, or reply-to mismatch). Surface a
# "needs manual sync" hint in the API so the UI can prompt the user.
STUCK_DRAFT_HOURS = 24

# Pipeline stages where the draft has been sent, so the "Open in Gmail" button
# must not point at a #draft/{id} URL (those 404 once the draft is gone).
# Prefer the thread URL instead.
POST_SEND_STAGES = AWAITING_REPLY_STAGES | frozenset({
    "replied",
    "meeting_scheduled", "connected", "no_response",
    "bounced", "closed",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_iso(s):
    """Parse ISO date string to naive UTC datetime. Returns None on failure."""
    if not s:
        return None
    try:
        s = s.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _get_contact_ref(uid, contact_id):
    db = get_db()
    return db.collection("users").document(uid).collection("contacts").document(contact_id)


def _get_contact(uid, contact_id):
    """Fetch a contact doc. Returns (ref, data) or raises ValueError."""
    ref = _get_contact_ref(uid, contact_id)
    doc = ref.get()
    if not doc.exists:
        raise ValueError("contact_not_found")
    return ref, doc.to_dict() or {}


def _contact_to_dict(contact_id, data):
    """Convert raw Firestore contact data to the API response shape."""
    first = (data.get("firstName") or "").strip()
    last = (data.get("lastName") or "").strip()
    name = f"{first} {last}".strip() or data.get("name") or data.get("email", "")

    gmail_draft_id = data.get("gmailDraftId")
    gmail_draft_url = data.get("gmailDraftUrl")
    gmail_message_id = data.get("gmailMessageId")
    gmail_thread_id = data.get("gmailThreadId")
    stage = data.get("pipelineStage")

    # Once the draft has been sent, #draft/{id} 404s. Prefer the thread URL.
    # Fall back to compose-URL only while the contact is still pre-send.
    if stage in POST_SEND_STAGES and gmail_thread_id:
        gmail_draft_url = f"https://mail.google.com/mail/u/0/#inbox/{gmail_thread_id}"
    elif gmail_draft_id and not gmail_draft_url:
        if gmail_message_id:
            gmail_draft_url = f"https://mail.google.com/mail/u/0/#drafts?compose={gmail_message_id}"
        else:
            gmail_draft_url = f"https://mail.google.com/mail/u/0/#draft/{gmail_draft_id}"
    elif gmail_draft_url and "#drafts/" in gmail_draft_url:
        gmail_draft_url = gmail_draft_url.replace("#drafts/", "#draft/")

    # Snippet fallback
    snippet = data.get("lastMessageSnippet")
    if not snippet:
        stage = data.get("pipelineStage")
        if stage == "new":
            snippet = "Ready to draft an email"
        elif not data.get("gmailThreadId"):
            snippet = data.get("emailBody") or "Draft is ready to send in Gmail"
        else:
            snippet = ""
    # Gmail API returns HTML-encoded snippets (&#39; &amp; etc.) — decode them
    if snippet:
        snippet = html.unescape(snippet)
    # A snippet is a preview line, not the document: the emailBody fallback
    # above was shipping FULL email bodies for every draft-stage contact,
    # inflating GET /outbox/threads to ~0.5MB per poll across a few hundred
    # contacts. Detail views fetch the real body via the messages endpoint.
    if snippet and len(snippet) > 160:
        snippet = snippet[:157].rstrip() + "…"

    # Flag drafts stuck in draft_created for > STUCK_DRAFT_HOURS with no thread.
    # These are contacts the Gmail webhook never matched to a sent message, so
    # the frontend should nudge the user to hit Refresh / reconnect Gmail.
    needs_manual_sync = False
    if data.get("pipelineStage") == "draft_created" and not data.get("gmailThreadId"):
        draft_created_at = _parse_iso(data.get("draftCreatedAt"))
        if draft_created_at:
            age_hours = (
                datetime.now(timezone.utc).replace(tzinfo=None) - draft_created_at
            ).total_seconds() / 3600
            if age_hours >= STUCK_DRAFT_HOURS:
                needs_manual_sync = True

    return {
        "id": contact_id,
        # Identity
        "name": name,
        "email": data.get("draftToEmail") or data.get("email") or "",
        "company": data.get("company") or "",
        "title": data.get("jobTitle") or "",
        # Hotlinked LinkedIn photo (expires in weeks-months); the app's
        # Avatar falls back to initials when it stops loading.
        "photoUrl": data.get("photoUrl") or "",
        "linkedinUrl": ("https://" + data.get("linkedinUrl") if data.get("linkedinUrl") and not data.get("linkedinUrl", "").startswith("http") else data.get("linkedinUrl")),
        # Outbox state
        "pipelineStage": data.get("pipelineStage"),
        "inOutbox": bool(data.get("inOutbox")),
        "hasUnreadReply": bool(data.get("hasUnreadReply")),
        # Gmail
        "gmailThreadId": data.get("gmailThreadId"),
        "gmailDraftId": gmail_draft_id,
        "gmailDraftUrl": gmail_draft_url,
        "emailSubject": data.get("emailSubject"),
        "draftToEmail": data.get("draftToEmail"),
        "lastMessageSnippet": snippet,
        "lastMessageFrom": data.get("lastMessageFrom"),
        # Timestamps
        "emailSentAt": data.get("emailSentAt"),
        "draftCreatedAt": data.get("draftCreatedAt"),
        "replyReceivedAt": data.get("replyReceivedAt"),
        "lastActivityAt": data.get("lastActivityAt") or data.get("draftCreatedAt") or data.get("createdAt") or "",
        # Open tracking (written by email_tracking.record_open)
        "emailOpenedAt": data.get("emailOpenedAt"),
        "emailLastOpenedAt": data.get("emailLastOpenedAt"),
        "emailOpenCount": data.get("emailOpenCount", 0),
        # Follow-up
        "followUpCount": data.get("followUpCount", 0),
        "nextFollowUpAt": data.get("nextFollowUpAt"),
        "messageCount": data.get("messageCount", 0),
        # Resolution
        "resolution": data.get("resolution"),
        "resolutionDetails": data.get("resolutionDetails"),
        "conversationSummary": data.get("conversationSummary"),
        # Archive/snooze
        "archivedAt": data.get("archivedAt"),
        "snoozedUntil": data.get("snoozedUntil"),
        "updatedAt": data.get("updatedAt") or "",
        # Sync metadata (kept for frontend sync-error display)
        "lastSyncError": data.get("lastSyncError"),
        "lastSyncAt": data.get("lastSyncAt"),
        "needsManualSync": needs_manual_sync,
        # Provenance — lets the tracker / My Network badge a Loop-sourced row
        # and lets approve-send verify Loop ownership. Empty string for manual
        # contacts (source is only set by the agent / queue / HM paths).
        "source": data.get("source") or "",
        "loopId": data.get("loopId") or "",
        # Legacy aliases for frontend compatibility (remove after Outbox.tsx migration)
        "contactName": name,
        "jobTitle": data.get("jobTitle") or "",
        "hasDraft": bool(data.get("gmailDraftId")),
        "status": data.get("pipelineStage") or "",
    }


# ---------------------------------------------------------------------------
# Hiring-manager outbox contact (shared by agent + manual Find HM paths)
# ---------------------------------------------------------------------------

def build_hm_outbox_contact_doc(
    *,
    uid,
    first_name,
    last_name,
    email,
    company,
    job_title,
    now_iso,
    today,
    source,
    linkedin_url="",
    email_subject="",
    email_body="",
    gmail_draft_id="",
    gmail_draft_url="",
    agent_cycle_id=None,
    discovered_via="",
    source_job_id="",
    loop_id="",
):
    """Build a users/{uid}/contacts/* doc for a hiring-manager outbox row.

    Single source of truth shared by the agent / Loop HM path
    (agent_actions.execute_find_hiring_managers) and the manual
    Find -> Hiring Managers path (job_board.find_hiring_manager_endpoint), so
    the two cannot drift again. Both log at DRAFT time, matching Find People.

    pipelineStage is "draft_created" when an email body exists, else
    "not_contacted". "not_contacted" is intentionally outside
    ALLOWED_PIPELINE_STAGES (the metrics histogram set): such contacts still
    surface in the outbox and bucket as active, they are simply excluded from
    the per-stage counts.

    draftToEmail is always the lowercased recipient address. The Gmail reply
    webhook matches inbound mail on draftToEmail, so setting it here is what
    lets a reply to a manually drafted HM attach back to this contact. The
    agent path historically omitted draftToEmail; unifying on this builder
    backfills it for agent-surfaced HMs too (named improvement, 2026-06).
    """
    doc = {
        "firstName": first_name,
        "lastName": last_name,
        "email": email,
        "company": company,
        "jobTitle": job_title,
        "source": source,
        "pipelineStage": "draft_created" if email_body else "not_contacted",
        "emailSubject": email_subject,
        "emailBody": email_body,
        "inOutbox": True,
        "createdAt": now_iso,
        "firstContactDate": today,
        "lastContactDate": today,
        "status": "Not Contacted",
        "isHiringManager": True,
        "userId": uid,
        "emailGeneratedAt": now_iso,
        "draftCreatedAt": now_iso,
        "draftStillExists": True,
        "lastActivityAt": now_iso,
        "hasUnreadReply": False,
        "linkedinUrl": linkedin_url,
        "draftToEmail": (email or "").strip().lower(),
    }
    # Agent / Loop provenance. Only the agent path sets these; the manual path
    # leaves them off so a manually found HM is not mislabeled as agent-sourced.
    if source == "agent":
        doc["agentCycleId"] = agent_cycle_id
        doc["discoveredVia"] = discovered_via
        doc["sourceJobId"] = source_job_id
        doc["loopId"] = loop_id
    # gmailDraftId/Url are added only when present. The agent path leaves these
    # empty here and sets them post-hoc after the Gmail draft call, so omitting
    # the empty keys keeps that path byte-identical.
    if gmail_draft_id:
        doc["gmailDraftId"] = gmail_draft_id
    if gmail_draft_url:
        doc["gmailDraftUrl"] = gmail_draft_url
    return doc


def upsert_hm_outbox_contact(
    db,
    uid,
    *,
    first_name,
    last_name,
    email,
    company,
    job_title,
    now_iso,
    today,
    linkedin_url="",
    email_subject="",
    email_body="",
    gmail_draft_id="",
    gmail_draft_url="",
):
    """Create or merge a manual hiring-manager outbox contact, keyed on the
    lowercased email. Returns the contact doc id (or None if no email).

    Dedup convention mirrors the Find People draft endpoint (emails.py): query
    contacts where email == lowercased address, merge if present else add. This
    keeps a person drafted to twice from creating duplicate outbox rows, and a
    later recruiters/* save from double-logging.

    Conservative merge: on an existing contact we refresh only draft-event and
    outbox-membership fields. We deliberately do NOT touch createdAt,
    firstContactDate, status, pipelineStage, or hasUnreadReply. This is where
    HM intentionally differs from the emails.py Find People re-draft path, which
    resets pipelineStage to "draft_created" on every draft: for HM we must not
    knock a contact already advanced to replied / sent / meeting_scheduled back
    to a fresh draft, nor clear an unread reply, just because the user generated
    another draft.
    """
    key_email = (email or "").strip().lower()
    if not key_email:
        return None

    contacts_ref = db.collection("users").document(uid).collection("contacts")
    existing = list(contacts_ref.where("email", "==", key_email).limit(1).stream())

    if existing:
        ref = existing[0].reference
        update = {
            "inOutbox": True,
            "isHiringManager": True,
            "emailSubject": email_subject,
            "emailBody": email_body,
            "draftToEmail": key_email,
            "draftCreatedAt": now_iso,
            "emailGeneratedAt": now_iso,
            "draftStillExists": True,
            "lastActivityAt": now_iso,
            "lastContactDate": today,
        }
        # Only refresh the draft link when we actually have one, so a retry that
        # failed to produce a draft cannot wipe an existing draft pointer.
        if gmail_draft_id:
            update["gmailDraftId"] = gmail_draft_id
        if gmail_draft_url:
            update["gmailDraftUrl"] = gmail_draft_url
        ref.set(update, merge=True)
        return existing[0].id

    doc = build_hm_outbox_contact_doc(
        uid=uid,
        first_name=first_name,
        last_name=last_name,
        email=key_email,
        company=company,
        job_title=job_title,
        now_iso=now_iso,
        today=today,
        source="manual_find_hm",
        linkedin_url=linkedin_url,
        email_subject=email_subject,
        email_body=email_body,
        gmail_draft_id=gmail_draft_id,
        gmail_draft_url=gmail_draft_url,
    )
    _, ref = contacts_ref.add(doc)
    return ref.id


# ---------------------------------------------------------------------------
# Core queries
# ---------------------------------------------------------------------------

def get_outbox_contacts(uid, include_archived=False):
    """
    Query outbox contacts using the inOutbox index.
    Returns list of dicts ready for API response.
    """
    db = get_db()
    contacts_ref = db.collection("users").document(uid).collection("contacts")

    query = contacts_ref.where("inOutbox", "==", True)
    docs = list(query.stream())

    results = []
    for doc in docs:
        data = doc.to_dict() or {}
        if not include_archived and data.get("archivedAt"):
            continue
        results.append(_contact_to_dict(doc.id, data))

    return results


def get_recent_outbox_contacts(uid, limit=50):
    """Bounded outbox query for suggestion engine. Returns most recent by updatedAt."""
    db = get_db()
    contacts_ref = db.collection("users").document(uid).collection("contacts")
    query = (contacts_ref
        .where("inOutbox", "==", True)
        .order_by("updatedAt", direction="DESCENDING")
        .limit(limit))
    docs = list(query.stream())
    return [_contact_to_dict(doc.id, doc.to_dict() or {}) for doc in docs
            if not (doc.to_dict() or {}).get("archivedAt")]


def get_outbox_stats(uid):
    """
    Compute outbox statistics from indexed query.
    Returns dict with stage counts, rates, and bucket counts.
    """
    contacts = get_outbox_contacts(uid, include_archived=False)
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    three_days_ago = now_utc - timedelta(days=3)
    seven_days_ago = now_utc - timedelta(days=7)

    by_stage = {}
    for stage in ALLOWED_PIPELINE_STAGES:
        by_stage[stage] = 0

    total = 0
    replied_count = 0
    eligible_for_reply_rate = 0
    response_hours = []
    needs_attention = 0
    waiting = 0
    done = 0
    this_week_sent = 0
    this_week_replied = 0

    for c in contacts:
        total += 1
        stage = c.get("pipelineStage") or ""

        if stage in ALLOWED_PIPELINE_STAGES:
            by_stage[stage] = by_stage.get(stage, 0) + 1

        # Bucket: done
        is_done = stage in DONE_STAGES or c.get("archivedAt")
        if is_done:
            done += 1
            # Still count for rates below
        else:
            # Bucket: needs_attention
            is_needs_attention = False
            # Snoozed contacts are suppressed from needs-attention
            snoozed_until = _parse_iso(c.get("snoozedUntil"))
            is_snoozed = snoozed_until and snoozed_until > now_utc
            if is_snoozed:
                pass  # skip needs-attention checks
            elif c.get("hasUnreadReply"):
                is_needs_attention = True
            elif stage == "draft_created":
                created_dt = _parse_iso(c.get("draftCreatedAt"))
                if created_dt and created_dt < three_days_ago:
                    is_needs_attention = True
            if not is_snoozed:
                next_fu = _parse_iso(c.get("nextFollowUpAt"))
                if next_fu and next_fu <= now_utc:
                    is_needs_attention = True

            if is_needs_attention:
                needs_attention += 1
            elif stage in AWAITING_REPLY_STAGES:
                waiting += 1
            else:
                # Remaining active contacts (e.g. "new", "replied" without unread, "draft_created" < 3 days)
                waiting += 1

        # Reply rate
        if stage in REPLIED_STAGES:
            replied_count += 1
        if stage in AWAITING_REPLY_STAGES or stage in ("replied", "meeting_scheduled", "connected", "no_response"):
            eligible_for_reply_rate += 1

        # This-week metrics
        sent_dt = _parse_iso(c.get("emailSentAt"))
        if sent_dt and sent_dt >= seven_days_ago:
            this_week_sent += 1

        if stage in ("replied", "meeting_scheduled", "connected"):
            activity_dt = _parse_iso(c.get("lastActivityAt"))
            if activity_dt and activity_dt >= seven_days_ago:
                this_week_replied += 1

        # Avg response time
        if stage in ("replied", "meeting_scheduled", "connected"):
            reply_at = _parse_iso(c.get("replyReceivedAt") or c.get("lastActivityAt"))
            if sent_dt and reply_at and reply_at >= sent_dt:
                delta_hours = (reply_at - sent_dt).total_seconds() / 3600.0
                response_hours.append(delta_hours)

    reply_rate = (replied_count / eligible_for_reply_rate) if eligible_for_reply_rate else 0.0
    avg_response_time_hours = round(sum(response_hours) / len(response_hours), 1) if response_hours else None
    # Meeting rate: of contacts that got any reply, what fraction led to a meeting?
    # Denominator = all contacts that reached replied/meeting_scheduled/connected stages
    meeting_denom = by_stage.get("replied", 0) + by_stage.get("meeting_scheduled", 0) + by_stage.get("connected", 0)
    # Numerator = meetings booked (meeting_scheduled) + connected (which implies a meeting happened)
    meeting_numer = by_stage.get("meeting_scheduled", 0) + by_stage.get("connected", 0)
    meeting_rate = (meeting_numer / meeting_denom) if meeting_denom else 0.0

    return {
        "total": total,
        "byStage": by_stage,
        "replyRate": round(reply_rate, 4),
        "avgResponseTimeHours": avg_response_time_hours,
        "meetingRate": round(meeting_rate, 4),
        "needsAttentionCount": needs_attention,
        "waitingCount": waiting,
        "doneCount": done,
        "thisWeekSent": this_week_sent,
        "thisWeekReplied": this_week_replied,
    }


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

def update_contact_stage(uid, contact_id, new_stage):
    """Update pipelineStage and related timestamps."""
    if new_stage not in ALLOWED_PIPELINE_STAGES:
        raise ValueError(f"Invalid stage: {new_stage}. Must be one of: {', '.join(sorted(ALLOWED_PIPELINE_STAGES))}")

    ref, data = _get_contact(uid, contact_id)
    prev_stage = data.get("pipelineStage") or ""
    updates = {
        "pipelineStage": new_stage,
        "updatedAt": _now_iso(),
    }
    if new_stage == "meeting_scheduled" and not data.get("meetingScheduledAt"):
        updates["meetingScheduledAt"] = _now_iso()
        # Auto-prep: trigger coffee chat prep when meeting is first scheduled
        try:
            _maybe_trigger_auto_prep(uid, contact_id, data)
        except Exception as ap_err:
            logger.warning(f"[outbox] auto-prep trigger failed for contact={contact_id}: {ap_err}")
    if new_stage == "connected" and not data.get("connectedAt"):
        updates["connectedAt"] = _now_iso()

    ref.update(updates)
    data.update(updates)

    # Cooldown: record outreach on first transition into a send stage.
    # Gated on prev_stage so we don't double-count when the Gmail webhook
    # (which calls record_outreach directly) has already moved the stage.
    if new_stage in AWAITING_REPLY_STAGES and prev_stage not in POST_SEND_STAGES:
        contact_email = data.get("draftToEmail") or data.get("email")
        if contact_email:
            try:
                from app.services.cooldown_service import record_outreach
                record_outreach(contact_email, uid)
            except Exception as cd_err:
                logger.warning(f"[outbox] cooldown record failed for contact={contact_id}: {cd_err}")

    return _contact_to_dict(contact_id, data)


def clear_unread_reply(uid, contact_id):
    """Mark a contact's reply as read by clearing hasUnreadReply."""
    ref, data = _get_contact(uid, contact_id)
    updates = {
        "hasUnreadReply": False,
        "updatedAt": _now_iso(),
    }
    ref.update(updates)
    data.update(updates)
    return _contact_to_dict(contact_id, data)


def archive_contact(uid, contact_id):
    """Archive a contact."""
    ref, data = _get_contact(uid, contact_id)
    updates = {
        "archivedAt": _now_iso(),
        "updatedAt": _now_iso(),
    }
    ref.update(updates)
    data.update(updates)
    return _contact_to_dict(contact_id, data)


def unarchive_contact(uid, contact_id):
    """Restore a contact from archive. Resets terminal stages to waiting_on_reply."""
    ref, data = _get_contact(uid, contact_id)
    updates = {
        "archivedAt": None,
        "updatedAt": _now_iso(),
    }
    # Only reset negative terminal stages, preserve positive ones
    RESET_ON_UNARCHIVE = frozenset({"no_response", "bounced", "closed"})
    if data.get("pipelineStage") in RESET_ON_UNARCHIVE:
        updates["pipelineStage"] = "waiting_on_reply"
    ref.update(updates)
    data.update(updates)
    return _contact_to_dict(contact_id, data)


def snooze_contact(uid, contact_id, snooze_until):
    """Snooze follow-ups until a specific date."""
    # Validate snooze_until is a parseable ISO date
    parsed = _parse_iso(snooze_until)
    if not parsed:
        raise ValueError("Invalid snoozeUntil date. Must be a valid ISO 8601 date string.")

    ref, data = _get_contact(uid, contact_id)
    updates = {
        "snoozedUntil": snooze_until,
        "updatedAt": _now_iso(),
    }
    ref.update(updates)
    data.update(updates)
    return _contact_to_dict(contact_id, data)


def mark_contact_won(uid, contact_id, resolution_details=None):
    """Mark a contact as won (meeting booked)."""
    ref, data = _get_contact(uid, contact_id)
    now = _now_iso()
    updates = {
        "pipelineStage": "meeting_scheduled",
        "resolution": "meeting_booked",
        "updatedAt": now,
    }
    if resolution_details:
        updates["resolutionDetails"] = resolution_details
    if not data.get("meetingScheduledAt"):
        updates["meetingScheduledAt"] = now
    ref.update(updates)
    data.update(updates)
    return _contact_to_dict(contact_id, data)


def mark_contact_resolution(uid, contact_id, resolution, details=None):
    """Set a resolution on a contact."""
    if resolution not in VALID_RESOLUTIONS:
        raise ValueError(f"Invalid resolution: {resolution}. Must be one of: {', '.join(sorted(VALID_RESOLUTIONS))}")

    ref, data = _get_contact(uid, contact_id)
    now = _now_iso()
    updates = {
        "resolution": resolution,
        "updatedAt": now,
    }
    if details:
        updates["resolutionDetails"] = details

    # Map resolution to appropriate pipelineStage
    if resolution == "meeting_booked":
        updates["pipelineStage"] = "meeting_scheduled"
        if not data.get("meetingScheduledAt"):
            updates["meetingScheduledAt"] = now
    elif resolution in ("hard_no", "ghosted"):
        updates["pipelineStage"] = "closed"
        updates["archivedAt"] = now
    elif resolution == "soft_no":
        updates["pipelineStage"] = "no_response"
    elif resolution == "completed":
        updates["pipelineStage"] = "connected"
        if not data.get("connectedAt"):
            updates["connectedAt"] = now

    ref.update(updates)
    data.update(updates)
    return _contact_to_dict(contact_id, data)


# ---------------------------------------------------------------------------
# Gmail sync
# ---------------------------------------------------------------------------

def _is_gmail_auth_error(e):
    """
    True if the exception indicates Gmail OAuth token is expired, revoked, or
    otherwise unable to authenticate. Checks SDK types first, then falls back
    to string matching for wrapped exceptions / older SDK versions.
    """
    # 1. google-auth refresh failures (invalid_grant, token revoked, etc.).
    try:
        from google.auth.exceptions import RefreshError
        if isinstance(e, RefreshError):
            return True
    except ImportError:
        pass

    # 2. googleapiclient HTTP errors: 401 Unauthorized, 403 Forbidden.
    try:
        from googleapiclient.errors import HttpError
        if isinstance(e, HttpError):
            status = getattr(getattr(e, "resp", None), "status", None)
            if status in (401, 403):
                return True
    except ImportError:
        pass

    # 3. String fallback for wrapped/re-raised errors. Kept deliberately
    # narrow — only match tokens that unambiguously indicate auth failure,
    # to avoid tagging transient network errors as "reconnect Gmail".
    msg = (str(e) or "").lower()
    return any(s in msg for s in (
        "invalid_grant", "token has been expired", "token expired",
        "token has been revoked", "refresh token invalid",
        "unauthorized_client", "invalid_client",
    ))


def _capture_sentry(e):
    try:
        import sentry_sdk
        sentry_sdk.capture_exception(e)
    except ImportError:
        pass


def _stage_when_draft_gone(data):
    """Stage to write when sync finds the contact's draft gone (i.e. sent).

    The draft check runs on every sync for as long as gmailDraftId is set, so
    it must not churn a stage that already says "sent":
      - waiting_on_reply / email_opened stay put. Re-deriving from
        emailOpenedAt would undo the reset to waiting_on_reply that a
        follow-up send makes, and the old literal demoted email_opened.
      - draft / email_sent stages are a first send confirmation: an earlier
        open goes straight to email_opened.
      - anything else keeps the previous behavior (waiting_on_reply).
    """
    current = data.get("pipelineStage") or ""
    if current in (WAITING_ON_REPLY, EMAIL_OPENED):
        return current
    if current in ("", "new", "draft_created", "draft_deleted", "email_sent"):
        return stage_after_send_confirmed(data)
    return WAITING_ON_REPLY


def _check_draft_status(gmail_service, data):
    """
    Check if a Gmail draft still exists.
    Returns dict of Firestore updates, or empty dict.
    """
    draft_id = data.get("gmailDraftId")
    if not draft_id:
        return {}

    try:
        gmail_service.users().drafts().get(
            userId="me", id=draft_id, format="minimal"
        ).execute()
        return {"draftStillExists": True}
    except Exception as e:
        # Only treat 404 as "draft gone"; other errors are transient
        is_not_found = False
        if hasattr(e, "resp"):
            is_not_found = getattr(e.resp, "status", 0) == 404
        elif "404" in str(e) or "not found" in str(e).lower():
            is_not_found = True

        if not is_not_found:
            logger.warning("Transient error checking draft %s: %s", draft_id, e)
            _capture_sentry(e)
            return {
                "lastSyncError": {
                    "code": "gmail_error",
                    "message": "Could not check draft status",
                    "at": _now_iso(),
                },
            }

        # Draft is gone - was either sent or deleted
        updates = {"draftStillExists": False}
        if data.get("gmailThreadId"):
            updates["pipelineStage"] = _stage_when_draft_gone(data)
            if not data.get("emailSentAt"):
                updates["emailSentAt"] = _now_iso()
        else:
            # Draft gone without threadId - search Gmail for the sent message
            found_sent = False
            contact_email = data.get("draftToEmail") or data.get("email")
            email_subject = data.get("emailSubject")
            if contact_email and email_subject:
                try:
                    query = f'to:{contact_email} subject:"{email_subject[:50]}"'
                    results = gmail_service.users().messages().list(
                        userId="me", q=query, maxResults=1
                    ).execute()
                    messages = results.get("messages", [])
                    if messages:
                        msg = gmail_service.users().messages().get(
                            userId="me", id=messages[0]["id"], format="minimal"
                        ).execute()
                        thread_id = msg.get("threadId")
                        if thread_id:
                            updates["gmailThreadId"] = thread_id
                            updates["pipelineStage"] = _stage_when_draft_gone(data)
                            if not data.get("emailSentAt"):
                                updates["emailSentAt"] = _now_iso()
                            found_sent = True
                except Exception as e:
                    logger.warning("Could not find threadId for sent draft: %s", e)
                    _capture_sentry(e)
            # No sent message found — draft was deleted, not sent
            if not found_sent:
                updates["pipelineStage"] = "draft_deleted"
        return updates


def _sync_thread_messages(gmail_service, data, user_email):
    """
    Sync latest message from a Gmail thread.
    Returns dict of Firestore updates, or empty dict.
    """
    gmail_thread_id = data.get("gmailThreadId")
    if not gmail_thread_id:
        return {}

    # Only sync if draft no longer exists (email was sent)
    if data.get("draftStillExists") is not False and data.get("gmailDraftId"):
        return {}

    contact_email = data.get("draftToEmail") or data.get("email")
    try:
        sync_result = sync_thread_message(
            gmail_service, gmail_thread_id, contact_email, user_email
        )
    except Exception as e:
        logger.warning("Could not sync thread %s: %s", gmail_thread_id, e)
        _capture_sentry(e)
        return {
            "lastSyncAt": _now_iso(),
            "lastSyncError": {
                "code": "gmail_error",
                "message": "Could not sync with Gmail",
                "at": _now_iso(),
            },
        }

    updates = {
        "lastMessageSnippet": sync_result.get("snippet", ""),
        "lastActivityAt": sync_result.get("lastActivityAt"),
        "lastSyncAt": _now_iso(),
        "lastSyncError": None,
    }
    if "hasUnreadReply" in sync_result:
        updates["hasUnreadReply"] = sync_result["hasUnreadReply"]
    if "status" in sync_result:
        updates["threadStatus"] = sync_result["status"]

    sync_status = sync_result.get("status")
    if sync_status in ("new_reply", "waiting_on_you") or sync_result.get("hasUnreadReply"):
        updates["pipelineStage"] = "replied"
        updates["lastMessageFrom"] = "contact"
        if not data.get("replyReceivedAt"):
            updates["replyReceivedAt"] = _now_iso()
    elif sync_status == "waiting_on_them":
        updates["lastMessageFrom"] = "user"

    return updates


def _try_acquire_sync_lock(ref):
    """
    Atomically claim the 60-second sync lock for one contact.

    Returns (acquired: bool, data: dict). Uses a Firestore transaction so two
    concurrent Refresh clicks can't both pass the freshness check and double-
    fire the Gmail API. The winner writes lastSyncAt; the loser returns the
    cached dict without calling Gmail.
    """
    db = get_db()

    @transactional
    def _attempt(transaction):
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise ValueError("contact_not_found")
        data = snap.to_dict() or {}

        last_sync = data.get("lastSyncAt")
        if last_sync:
            last_sync_dt = _parse_iso(last_sync)
            if last_sync_dt:
                elapsed = (
                    datetime.now(timezone.utc).replace(tzinfo=None) - last_sync_dt
                ).total_seconds()
                if elapsed < SYNC_LOCK_SECONDS:
                    return False, data

        # Claim the lock by advancing lastSyncAt in the same transaction.
        now = _now_iso()
        transaction.update(ref, {"lastSyncAt": now})
        data["lastSyncAt"] = now
        return True, data

    return _attempt(db.transaction())


def sync_contact_thread(uid, contact_id):
    """
    Full Gmail sync for one contact: check draft status, sync thread messages.
    Uses a transactional 60s sync lock so concurrent calls don't double-fire
    Gmail API requests. Returns updated contact dict.
    """
    ref = _get_contact_ref(uid, contact_id)

    acquired, data = _try_acquire_sync_lock(ref)
    if not acquired:
        return _contact_to_dict(contact_id, data)

    # Get Gmail service
    try:
        creds = _load_user_gmail_creds(uid)
        if not creds:
            ref.update({
                "lastSyncError": {"code": "gmail_disconnected", "message": "Please reconnect Gmail", "at": _now_iso()},
                "updatedAt": _now_iso(),
            })
            data["lastSyncError"] = {"code": "gmail_disconnected", "message": "Please reconnect Gmail", "at": _now_iso()}
            return _contact_to_dict(contact_id, data)
        gmail_service = _gmail_service(creds)
    except Exception as e:
        error_code = "gmail_disconnected" if _is_gmail_auth_error(e) else "gmail_error"
        error_msg = "Please reconnect Gmail" if _is_gmail_auth_error(e) else "Could not sync with Gmail"
        _capture_sentry(e)
        ref.update({
            "lastSyncError": {"code": error_code, "message": error_msg, "at": _now_iso()},
            "updatedAt": _now_iso(),
        })
        data["lastSyncError"] = {"code": error_code, "message": error_msg, "at": _now_iso()}
        return _contact_to_dict(contact_id, data)

    # Collect all updates, write once at the end
    all_updates = {}

    # 1. Check draft status
    if data.get("gmailDraftId"):
        draft_updates = _check_draft_status(gmail_service, data)
        all_updates.update(draft_updates)
        # Merge into data so _sync_thread_messages sees current state
        data.update(draft_updates)

    # 2. Sync thread messages
    user_email = None
    try:
        user_doc = get_db().collection("users").document(uid).get()
        if user_doc.exists:
            user_email = (user_doc.to_dict() or {}).get("email")
    except Exception:
        pass

    thread_updates = _sync_thread_messages(gmail_service, data, user_email)
    # Don't let sync overwrite a manually-set terminal stage
    current_stage = data.get("pipelineStage", "")
    if current_stage in DONE_STAGES and "pipelineStage" in thread_updates:
        del thread_updates["pipelineStage"]
    all_updates.update(thread_updates)

    # Write all updates in one batch
    if all_updates:
        all_updates["updatedAt"] = _now_iso()
        if "lastSyncError" not in all_updates:
            all_updates["lastSyncError"] = None
        ref.update(all_updates)
        data.update(all_updates)

    return _contact_to_dict(contact_id, data)


def send_reply_for_contact(uid, contact_id, body):
    """
    Send a reply (or follow-up) for a contact via the user's Gmail and update
    outbox state. Used by the "Send" button in the inbox detail panel.

    State changes on success:
        - pipelineStage:
            - "replied"   -> "waiting_on_reply"   (we just answered them)
            - "email_opened" -> "waiting_on_reply" (new email, not opened yet)
            - "draft_created" / "new" -> "email_sent"
            - otherwise unchanged (already mid-thread or terminal)
          Done-stage contacts are left alone — sending another note shouldn't
          unwind a manually-marked won/closed state.
        - lastMessageFrom = "user", lastActivityAt = now, emailSentAt = now,
          hasUnreadReply = False (any unread flag is cleared by responding),
          followUpCount += 1 when this send was a follow-up (no inbound reply
          to react to).
        - gmailThreadId / gmailMessageId stamped from the Gmail response if we
          didn't already have them, so future syncs can join replies on the
          thread id rather than the draftToEmail fallback.
        - Any cached replyDrafts/<id> doc is cleared so the next Generate
          click can't surface the now-stale draft.
    """
    import base64
    from email.mime.text import MIMEText

    text = (body or "").strip()
    if not text:
        raise ValueError("missing_body")

    ref, data = _get_contact(uid, contact_id)
    to_email = data.get("draftToEmail") or data.get("email") or ""
    if not to_email:
        raise ValueError("missing_recipient")

    subject = data.get("emailSubject") or data.get("draftSubject") or ""
    thread_id = data.get("gmailThreadId")
    had_unread_reply = bool(data.get("hasUnreadReply"))
    current_stage = data.get("pipelineStage") or ""

    creds = _load_user_gmail_creds(uid)
    if not creds:
        raise ValueError("gmail_disconnected")
    try:
        service = _gmail_service(creds)
    except Exception as e:
        logger.warning("[outbox] send_reply: gmail service init failed uid=%s contact=%s: %s", uid, contact_id, e)
        _capture_sentry(e)
        raise ValueError("gmail_disconnected")

    from app.services import email_tracking
    tracking_token = email_tracking.start(uid)
    msg = email_tracking.tracked_text_mime(text, tracking_token)
    msg["to"] = to_email
    if subject:
        msg["subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    message_body = {"raw": raw}
    if thread_id:
        message_body["threadId"] = thread_id

    try:
        sent = service.users().messages().send(userId="me", body=message_body).execute() or {}
    except Exception as e:
        logger.warning("[outbox] send_reply: Gmail send failed uid=%s contact=%s: %s", uid, contact_id, e)
        _capture_sentry(e)
        raise

    sent_message_id = sent.get("id") or ""
    sent_thread_id = sent.get("threadId") or thread_id or ""
    email_tracking.register(
        tracking_token, uid, "reply", recipient_email=to_email, contact_id=contact_id,
        message_id=sent_message_id, thread_id=sent_thread_id, sent=True,
    )
    now = _now_iso()

    updates = {
        "lastMessageFrom": "user",
        "lastActivityAt": now,
        "emailSentAt": now,
        "hasUnreadReply": False,
        "updatedAt": now,
    }
    if sent_message_id:
        updates["gmailMessageId"] = sent_message_id
    if sent_thread_id and not thread_id:
        updates["gmailThreadId"] = sent_thread_id
    # A follow-up is a send to a contact who hadn't replied yet. Counts up so
    # the existing "Nth follow-up" UI in ConversationPanel.tsx stays accurate.
    is_followup_send = not had_unread_reply and current_stage not in ("replied",)
    if is_followup_send:
        prior_followups = data.get("followUpCount") or 0
        updates["followUpCount"] = prior_followups + 1

    # Pipeline-stage transitions. Done-stage contacts are intentionally left
    # alone — the user marked them resolved and sending another note shouldn't
    # silently un-resolve them.
    if current_stage not in DONE_STAGES:
        if current_stage in ("replied", EMAIL_OPENED):
            # replied: we just answered them. email_opened: a follow-up is a
            # new email they haven't opened yet. emailOpenedAt keeps the first
            # open ever.
            updates["pipelineStage"] = "waiting_on_reply"
        elif current_stage in ("new", "draft_created", "draft_deleted", ""):
            updates["pipelineStage"] = "email_sent"
        # Otherwise leave the stage where it is (email_sent / waiting_on_reply
        # stays — we're just sending another follow-up in the same state).

    ref.update(updates)
    data.update(updates)

    # Cooldown: log this send against the recipient so other product paths
    # don't over-contact the same person. Mirrors the same hook used by
    # update_contact_stage on the email_sent transition.
    try:
        from app.services.cooldown_service import record_outreach
        record_outreach(to_email, uid)
    except Exception as cd_err:
        logger.warning("[outbox] send_reply: cooldown record failed contact=%s: %s", contact_id, cd_err)

    # Clear the cached reply draft so the next Generate click can't surface
    # the now-stale draft body that the user just sent.
    try:
        get_db().collection("users").document(uid).collection("replyDrafts").document(contact_id).delete()
    except Exception:
        pass

    try:
        from app.utils.metrics_events import log_event
        log_event(uid, "reply_response_sent", {
            "contact_id": contact_id,
            "is_followup": is_followup_send,
            "used_auto_draft": True,
        })
    except Exception:
        pass

    return _contact_to_dict(contact_id, data)


def html_to_plain_text(value):
    """Convert an HTML email body to readable plain text.

    Legacy hiring-manager / recruiter drafts were stored as HTML (a
    font-family wrapper div with <br> line breaks and escaped entities). The
    tracker renders the stored emailBody as text, so that markup showed up raw.
    This normalizes such bodies for display. Newly stored bodies are already
    plain text, so the no-op fast path below returns them untouched.

    Conversion order: block tags to newlines, strip remaining tags, decode
    entities last (so a literal &lt; in the source never becomes a tag we then
    strip), then collapse 3+ newlines to 2.
    """
    if not value:
        return value
    # Fast path: nothing to convert when there is no markup or entity.
    if "<" not in value and "&" not in value:
        return value
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)</(p|div)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def get_contact_thread_messages(uid, contact_id):
    """
    Return the full message chain for a contact's thread, for the inbox view.

    When gmailThreadId is set AND Gmail creds load AND the fetch succeeds,
    returns {"source": "gmail", "messages": [...]}.

    Otherwise (no thread id, no creds, or any Gmail failure) returns
    {"source": "local", "messages": [<single locally-stored emailBody as one
    msg>]} so the panel can still render the sent draft. Never raises on a
    missing Gmail connection — that's the explicit fallback path.
    """
    ref, data = _get_contact(uid, contact_id)
    gmail_thread_id = data.get("gmailThreadId")

    def _local_fallback(reason=None):
        # html_to_plain_text covers contacts saved before the storage fix, whose
        # emailBody is still HTML. No-op for plain-text bodies.
        body = html_to_plain_text((data.get("emailBody") or "").strip())
        subject = data.get("emailSubject") or ""
        sent_at = data.get("emailSentAt") or data.get("draftCreatedAt") or data.get("createdAt")
        messages = []
        if body:
            messages.append({
                "messageId": None,
                "sender": None,
                "isFromRecipient": False,
                "isFromUser": True,
                "sentAt": sent_at,
                "subject": subject,
                "body": body,
            })
        result = {"source": "local", "messages": messages}
        if reason:
            result["reason"] = reason
        return result

    if not gmail_thread_id:
        return _local_fallback("no_thread")

    try:
        creds = _load_user_gmail_creds(uid)
    except Exception as e:
        logger.warning("[outbox] thread messages: creds load failed for uid=%s contact=%s: %s", uid, contact_id, e)
        return _local_fallback("gmail_disconnected")
    if not creds:
        return _local_fallback("gmail_disconnected")

    contact_email = data.get("draftToEmail") or data.get("email")
    user_email = None
    try:
        user_doc = get_db().collection("users").document(uid).get()
        if user_doc.exists:
            user_email = (user_doc.to_dict() or {}).get("email")
    except Exception:
        pass

    try:
        service = _gmail_service(creds)
        chain = get_full_thread_chain(service, gmail_thread_id, contact_email, user_email)
    except Exception as e:
        logger.warning("[outbox] thread messages: Gmail fetch failed for uid=%s contact=%s thread=%s: %s",
                       uid, contact_id, gmail_thread_id, e)
        _capture_sentry(e)
        return _local_fallback("gmail_error")

    return {"source": "gmail", "messages": chain}


# ---------------------------------------------------------------------------
# Auto-Prep (Coffee Chat) — triggered when meeting_scheduled
# ---------------------------------------------------------------------------

# Single source of truth in config — this local copy silently drifted to 15
# while manual preps charged 30 (found 2026-07-07; both are 0 now).
from app.config import COFFEE_CHAT_CREDITS


def _maybe_trigger_auto_prep(uid, contact_id, contact_data):
    """Fire-and-forget auto-prep when a meeting is first scheduled.

    Deducts credits before spawning thread, refunds on failure.
    Pro/Elite only. Skips if no LinkedIn URL on the contact.
    """
    import threading as _threading

    db = get_db()

    # Tier check
    user_doc = db.collection("users").document(uid).get()
    user_data = user_doc.to_dict() if user_doc.exists else {}
    tier = user_data.get("subscriptionTier") or user_data.get("tier", "free")
    if tier not in ("pro", "elite"):
        logger.info(f"[auto_prep] uid={uid} skipping — tier={tier}")
        return

    # LinkedIn URL required
    linkedin_url = contact_data.get("linkedinUrl") or contact_data.get("linkedin_url", "")
    if not linkedin_url:
        logger.info(f"[auto_prep] uid={uid} contact={contact_id} skipping — no LinkedIn URL")
        return

    # Check for existing prep
    preps = list(
        db.collection("users").document(uid).collection("coffee-chat-preps")
        .where("contactId", "==", contact_id)
        .limit(1)
        .stream()
    )
    if preps:
        logger.info(f"[auto_prep] uid={uid} contact={contact_id} prep already exists")
        return

    # Deduct credits
    from app.services.auth import deduct_credits_atomic
    success, remaining = deduct_credits_atomic(uid, COFFEE_CHAT_CREDITS, "auto_coffee_chat_prep")
    if not success:
        logger.info(f"[auto_prep] uid={uid} insufficient credits")
        return

    # Write pending doc
    now_iso = _now_iso()
    prep_id = f"auto_{contact_id}_{now_iso.replace(':', '-').replace('.', '-')}"
    db.collection("users").document(uid).collection("pending_auto_preps").document(contact_id).set({
        "status": "pending",
        "prepId": prep_id,
        "contactId": contact_id,
        "createdAt": now_iso,
    })

    # Create the prep doc skeleton
    resume_text = (user_data.get("resumeParsed") or {}).get("rawText", "")
    db.collection("users").document(uid).collection("coffee-chat-preps").document(prep_id).set({
        "contactId": contact_id,
        "linkedinUrl": linkedin_url,
        "status": "queued",
        "createdAt": now_iso,
        "autoTriggered": True,
    })

    # Spawn background thread
    def _run():
        try:
            from app.routes.coffee_chat_prep import process_coffee_chat_prep_background
            process_coffee_chat_prep_background(
                prep_id=prep_id,
                linkedin_url=linkedin_url,
                user_id=uid,
                resume_text=resume_text,
                extra_context={"contactId": contact_id, "autoTriggered": True},
                user_profile=user_data,
                credits_charged=COFFEE_CHAT_CREDITS,
            )
            # Clean up pending doc on success
            db.collection("users").document(uid).collection("pending_auto_preps").document(contact_id).delete()
            logger.info(f"[auto_prep] uid={uid} contact={contact_id} prep completed")
            # Log coffee_chat_prep_used metric
            from app.utils.metrics_events import log_event
            log_event(uid, "coffee_chat_prep_used", {
                "auto_triggered": True,
                "contact_id": contact_id,
            })
        except Exception as exc:
            logger.error(f"[auto_prep] uid={uid} contact={contact_id} failed: {exc}")
            try:
                db.collection("users").document(uid).collection("pending_auto_preps").document(contact_id).set(
                    {"status": "failed", "error": str(exc)[:200], "updatedAt": _now_iso()},
                    merge=True,
                )
            except Exception:
                pass

    t = _threading.Thread(target=_run, daemon=True)
    t.start()
    logger.info(f"[auto_prep] uid={uid} contact={contact_id} background thread spawned, prep_id={prep_id}")


def trigger_auto_prep(uid, contact_id, contact_data):
    """On-demand auto-prep trigger (called from contacts route fallback)."""
    db = get_db()

    # Check credits and tier
    from app.services.auth import deduct_credits_atomic
    user_doc = db.collection("users").document(uid).get()
    user_data = user_doc.to_dict() if user_doc.exists else {}
    tier = user_data.get("subscriptionTier") or user_data.get("tier", "free")
    if tier not in ("pro", "elite"):
        return {"status": "tier_required", "message": "Pro or Elite required for auto-prep"}

    linkedin_url = contact_data.get("linkedinUrl") or contact_data.get("linkedin_url", "")
    if not linkedin_url:
        return {"status": "no_linkedin", "message": "Contact has no LinkedIn URL"}

    success, remaining = deduct_credits_atomic(uid, COFFEE_CHAT_CREDITS, "auto_coffee_chat_prep")
    if not success:
        return {"status": "insufficient_credits", "remaining": remaining}

    now_iso = _now_iso()
    prep_id = f"auto_{contact_id}_{now_iso.replace(':', '-').replace('.', '-')}"
    resume_text = (user_data.get("resumeParsed") or {}).get("rawText", "")

    # Create prep doc
    db.collection("users").document(uid).collection("coffee-chat-preps").document(prep_id).set({
        "contactId": contact_id,
        "linkedinUrl": linkedin_url,
        "status": "queued",
        "createdAt": now_iso,
        "autoTriggered": True,
    })

    # Write pending doc
    db.collection("users").document(uid).collection("pending_auto_preps").document(contact_id).set({
        "status": "pending",
        "prepId": prep_id,
        "contactId": contact_id,
        "createdAt": now_iso,
    })

    # Spawn thread
    import threading as _threading

    def _run():
        try:
            from app.routes.coffee_chat_prep import process_coffee_chat_prep_background
            process_coffee_chat_prep_background(
                prep_id=prep_id,
                linkedin_url=linkedin_url,
                user_id=uid,
                resume_text=resume_text,
                extra_context={"contactId": contact_id, "autoTriggered": True},
                user_profile=user_data,
                credits_charged=COFFEE_CHAT_CREDITS,
            )
            db.collection("users").document(uid).collection("pending_auto_preps").document(contact_id).delete()
        except Exception as exc:
            logger.error(f"[auto_prep] on-demand uid={uid} contact={contact_id} failed: {exc}")
            try:
                db.collection("users").document(uid).collection("pending_auto_preps").document(contact_id).set(
                    {"status": "failed", "error": str(exc)[:200], "updatedAt": _now_iso()},
                    merge=True,
                )
            except Exception:
                pass

    t = _threading.Thread(target=_run, daemon=True)
    t.start()

    return {"status": "generating", "prepId": prep_id}
