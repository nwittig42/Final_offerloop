"""Send a contact's first email: the exact draft the user confirmed, once.

Used by POST /api/contacts/<id>/send-first-email (the onboarding
walkthrough's Send button).

What gets sent is the contact doc's current emailSubject / emailBody, the
draft the user edited in the app, never a stale Gmail draft. The client
sends a hash of the text it showed the user; a mismatch (edited elsewhere
since) is refused with draft_changed so nothing unconfirmed goes out.

Contact docs are client-writable, so nothing that decides money, limits or
duplicates lives there:
  * users/{uid}/firstEmailSends/{contactId}: the send ledger (idempotency).
  * users/{uid}/sendQuota/{YYYYMMDD}: the daily cap for this endpoint.
  * users/{uid}/revealedEmails/{candidateId}: the recipient, for people
    found through the recommender.
All three are server-only (Firestore rules default deny).

Duplicate protection: the ledger is claimed with create-if-absent /
compare-and-set before Gmail is called. A finished send replays its result.
A send that died mid-flight (the ledger still says "sending" after
IN_PROGRESS_SECONDS) may or may not have reached Gmail, so before trying
again we look for its unique Message-ID in the user's mailbox. If that
search can't run, the user is asked to check Sent first (confirm_not_sent).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DAILY_SEND_CAP = 20  # same number as runs.py ELITE_DAILY_SEND_CAP
IN_PROGRESS_SECONDS = 120
SEND_SCOPES = {
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://mail.google.com/",
}
SEARCH_SCOPES = {
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://mail.google.com/",
}

# Placeholders that must never go out: [Name], [Company], {{...}}, empty [],
# and generic bracketed ALL-CAPS slots like [YOUR NAME].
_PLACEHOLDER = re.compile(r"\[Name\]|\[Company\]|\{\{|\}\}|(?<!\w)\[\s*\]|\[[A-Z][A-Z _]{2,}\]")


class SendError(Exception):
    def __init__(self, code: str, status: int, **extra):
        super().__init__(code)
        self.code = code
        self.status = status
        self.extra = extra


def draft_hash(subject: str, body: str) -> str:
    """Hash of the confirmed text. The client computes the same thing."""
    return hashlib.sha256(f"{subject or ''}\n\u0000\n{body or ''}".encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(value) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# ── Review: what would be sent, and what blocks it ─────────────────────────

@dataclass
class Review:
    contact: dict
    recipient: str
    subject: str
    body: str
    hash: str
    blocking: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _user_ref(db, uid):
    return db.collection("users").document(uid)


def _recipient(db, uid: str, contact: dict) -> str:
    """People found through the recommender: the server-side reveal record,
    never the client-writable contact doc. Others: the contact's address."""
    cid = (contact.get("candidateId") or "").strip()
    if cid:
        snap = _user_ref(db, uid).collection("revealedEmails").document(cid).get()
        data = (snap.to_dict() or {}) if getattr(snap, "exists", False) else {}
        return (data.get("email") or "").strip()
    return (contact.get("draftToEmail") or contact.get("email") or "").strip()


def review(db, uid: str, contact_id: str) -> Optional[Review]:
    snap = _user_ref(db, uid).collection("contacts").document(contact_id).get()
    if not getattr(snap, "exists", False):
        return None
    contact = snap.to_dict() or {}
    subject = (contact.get("emailSubject") or "").strip()
    body = (contact.get("emailBody") or "").strip()
    recipient = _recipient(db, uid, contact)

    # Option A: only real problems block; style and length are warnings.
    blocking = []
    if not recipient or "@" not in recipient:
        blocking.append("no_recipient")
    if not subject:
        blocking.append("empty_subject")
    if not body:
        blocking.append("empty_body")
    if _PLACEHOLDER.search(subject) or _PLACEHOLDER.search(body):
        blocking.append("placeholder")

    warnings = []
    if body:
        try:
            from app.utils.email_quality import check_email_quality
            app_contact = {
                "FirstName": contact.get("firstName") or "",
                "LastName": contact.get("lastName") or "",
                "Company": contact.get("company") or "",
                "Title": contact.get("jobTitle") or "",
                "College": contact.get("college") or "",
            }
            result = check_email_quality(subject, body, app_contact, "")
            warnings = [f for f in result.get("failures", []) if f != "template_leak"]
        except Exception:
            logger.exception("first_email_send: quality check failed uid=%s contact=%s", uid, contact_id)

    return Review(contact, recipient, subject, body, draft_hash(subject, body), blocking, warnings)


# ── Gmail ────────────────────────────────────────────────────────────────

@dataclass
class Gmail:
    service: object
    scopes: set
    address: str


def default_gmail(uid: str) -> Gmail:
    from app.services.gmail_client import _load_user_gmail_creds, _gmail_service
    creds = _load_user_gmail_creds(uid)
    if not creds:
        raise SendError("gmail_not_connected", 401)
    scopes = set(getattr(creds, "scopes", None) or [])
    if scopes and not (scopes & SEND_SCOPES):
        raise SendError("gmail_scope_missing", 403)
    service = _gmail_service(creds)
    try:
        address = service.users().getProfile(userId="me").execute().get("emailAddress") or ""
    except Exception as exc:
        raise _classify(exc)
    return Gmail(service, scopes, address)


def _classify(exc: Exception) -> SendError:
    """Map a Gmail/auth failure to a client-facing error. ambiguous=True
    means the message may have gone out (timeouts)."""
    name = type(exc).__name__
    status = getattr(getattr(exc, "resp", None), "status", None)
    text = str(exc).lower()
    if name == "RefreshError" or status == 401 or "invalid_grant" in text:
        return SendError("gmail_token_expired", 401)
    if status == 403 and "scope" in text:
        return SendError("gmail_scope_missing", 403)
    if isinstance(exc, TimeoutError) or "timed out" in text or "timeout" in text:
        return SendError("send_failed", 502, ambiguous=True)
    return SendError("send_failed", 502)


def _find_sent(gmail: Gmail, message_id_header: str) -> Optional[dict]:
    """Look for our Message-ID in the mailbox. None = not found; raises when
    the search can't run (no read scope, API error)."""
    if gmail.scopes and not (gmail.scopes & SEARCH_SCOPES):
        raise SendError("send_unconfirmed", 409)
    rid = message_id_header.strip("<>")
    res = gmail.service.users().messages().list(
        userId="me", q=f"rfc822msgid:{rid}", maxResults=1, includeSpamTrash=False,
    ).execute() or {}
    msgs = res.get("messages") or []
    return {"id": msgs[0].get("id"), "threadId": msgs[0].get("threadId")} if msgs else None


# ── Daily cap (server-controlled) ────────────────────────────────────────

def _quota_ref(db, uid, day):
    return _user_ref(db, uid).collection("sendQuota").document(day)


def take_quota(db, uid: str, day: str, cap: int = DAILY_SEND_CAP) -> bool:
    from google.api_core import exceptions as gexc
    ref = _quota_ref(db, uid, day)
    for _ in range(5):
        snap = ref.get()
        if not getattr(snap, "exists", False):
            try:
                ref.create({"count": 1, "day": day})
                return True
            except gexc.Conflict:
                continue
        n = int((snap.to_dict() or {}).get("count") or 0)
        if n >= cap:
            return False
        try:
            ref.update({"count": n + 1}, option=db.write_option(last_update_time=snap.update_time))
            return True
        except (gexc.FailedPrecondition, gexc.NotFound):
            continue
    return False


def give_back_quota(db, uid: str, day: str) -> None:
    from google.api_core import exceptions as gexc
    ref = _quota_ref(db, uid, day)
    for _ in range(5):
        try:
            snap = ref.get()
            if not getattr(snap, "exists", False):
                return
            n = int((snap.to_dict() or {}).get("count") or 0)
            ref.update({"count": max(0, n - 1)}, option=db.write_option(last_update_time=snap.update_time))
            return
        except (gexc.FailedPrecondition, gexc.NotFound):
            continue
        except Exception:
            logger.exception("first_email_send: quota give-back failed uid=%s", uid)
            return


# ── Ledger ───────────────────────────────────────────────────────────────

def _ledger_ref(db, uid, contact_id):
    return _user_ref(db, uid).collection("firstEmailSends").document(contact_id)


def _claim(db, ledger, snap, attempt: dict) -> None:
    """Create-if-absent, or compare-and-set over a failed / stale attempt."""
    from google.api_core import exceptions as gexc
    try:
        if getattr(snap, "exists", False):
            ledger.update(attempt, option=db.write_option(last_update_time=snap.update_time))
        else:
            ledger.create(attempt)
    except (gexc.Conflict, gexc.FailedPrecondition, gexc.NotFound):
        raise SendError("send_in_progress", 409)


# ── Send ─────────────────────────────────────────────────────────────────

def send_first_email(
    db,
    uid: str,
    contact_id: str,
    confirmed_hash: str,
    *,
    surface: str = "",
    confirm_not_sent: bool = False,
    gmail_factory: Callable[[str], Gmail] = default_gmail,
    now: Optional[datetime] = None,
) -> dict:
    now = now or _now()
    ledger = _ledger_ref(db, uid, contact_id)
    snap = ledger.get()
    prior = (snap.to_dict() or {}) if getattr(snap, "exists", False) else {}

    # 1. Already sent: replay, never send twice.
    if prior.get("status") == "sent":
        return {"sent": True, "alreadySent": True, "contactId": contact_id,
                "messageId": prior.get("messageId"), "threadId": prior.get("threadId")}

    info = review(db, uid, contact_id)
    if info is None:
        raise SendError("contact_not_found", 404)
    if info.contact.get("emailSent"):
        # Sent some other way (Inbox, the agent); this endpoint won't resend.
        return {"sent": True, "alreadySent": True, "contactId": contact_id,
                "messageId": info.contact.get("gmailMessageId"), "threadId": info.contact.get("gmailThreadId")}

    # 2. Exactly what the user confirmed, and nothing that can't go out.
    if not confirmed_hash or confirmed_hash != info.hash:
        raise SendError("draft_changed", 409)
    if info.blocking:
        raise SendError("blocked", 422, blocking=info.blocking)

    gmail = gmail_factory(uid)

    # 3. A previous attempt that never finished: did it reach Gmail?
    if prior.get("status") == "sending":
        started = _parse(prior.get("startedAt"))
        if started and (now - started).total_seconds() < IN_PROGRESS_SECONDS:
            raise SendError("send_in_progress", 409)
        header = prior.get("messageIdHeader") or ""
        found = None
        if header:
            try:
                found = _find_sent(gmail, header)
            except SendError:
                if not confirm_not_sent:
                    raise
            except Exception:
                if not confirm_not_sent:
                    raise SendError("send_unconfirmed", 409)
        if found:
            return _finish(db, uid, contact_id, info, found, ledger, surface, now, prior_attempt=prior)

    # 4. Claim this attempt, then the day's send slot.
    day = now.strftime("%Y%m%d")
    header = f"<{uuid.uuid4().hex}@offerloop.ai>"
    _claim(db, ledger, snap, {
        "status": "sending",
        "attemptId": uuid.uuid4().hex,
        "messageIdHeader": header,
        "startedAt": _iso(now),
        "contentHash": info.hash,
        "quotaDay": day,
    })
    if not take_quota(db, uid, day):
        ledger.update({"status": "failed", "error": "daily_cap_reached"})
        raise SendError("daily_cap_reached", 429, cap=DAILY_SEND_CAP)

    # 5. Build the same message every outreach send builds, and send it.
    # Open tracking: _build_raw adds the email_tracking pixel and _finish
    # registers the token once Gmail accepts the message.
    try:
        raw, tracking = _build_raw(db, uid, info, gmail.address, header)
        result = gmail.service.users().messages().send(userId="me", body={"raw": raw}).execute() or {}
    except Exception as exc:
        err = exc if isinstance(exc, SendError) else _classify(exc)
        if err.extra.get("ambiguous"):
            # It may have gone out: leave the attempt "sending" so the next
            # try looks for it before sending again.
            raise SendError("send_failed", 502, retry_after=IN_PROGRESS_SECONDS)
        ledger.update({"status": "failed", "error": err.code})
        give_back_quota(db, uid, day)
        raise err

    sent = {"id": result.get("id") or "", "threadId": result.get("threadId") or ""}
    return _finish(db, uid, contact_id, info, sent, ledger, surface, now, tracking=tracking)


def _build_raw(db, uid: str, info: Review, from_address: str, message_id_header: str) -> tuple[str, str]:
    """The base64 message and its open-tracking token."""
    from app.services.gmail_client import _build_outreach_mime
    from app.services import email_tracking
    from app.utils.contact import clean_email_text
    from app.utils.users import get_outreach_email
    from app.services.reply_generation import PURPOSES_INCLUDE_RESUME, email_body_mentions_resume

    user_snap = _user_ref(db, uid).get()
    user_data = (user_snap.to_dict() or {}) if getattr(user_snap, "exists", False) else {}

    # Same resume rule as the outreach route: the template's purpose, or a
    # body that says a resume is attached.
    purpose = None
    try:
        from app.services.email_request_builder import resolve_email_template
        purpose = resolve_email_template(None, uid, db, user_data=user_data)[1]
    except Exception:
        logger.warning("first_email_send: template lookup failed uid=%s; using default purpose", uid)
    attach_resume = purpose in PURPOSES_INCLUDE_RESUME or email_body_mentions_resume(info.body)
    resume_url = (user_data.get("resumeUrl") or user_data.get("resumeURL")) if attach_resume else None

    token = email_tracking.start(uid)
    message = _build_outreach_mime(
        recipient_email=info.recipient,
        gmail_account_email=from_address or get_outreach_email(user_data) or "",
        email_subject=clean_email_text(info.subject),
        email_body=clean_email_text(info.body),
        user_info={"name": user_data.get("name") or "", "email": get_outreach_email(user_data) or "",
                   "phone": "", "linkedin": ""},
        resume_url=resume_url,
        tracking_token=token,
    )
    message["Message-ID"] = message_id_header
    return base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8"), token


def _finish(db, uid, contact_id, info: Review, sent: dict, ledger, surface, now, *,
            tracking=None, prior_attempt=None) -> dict:
    """Record a send that reached Gmail. Each step after the ledger is best
    effort: the email is out, so a later failure must not report a failure."""
    from firebase_admin import firestore

    now_iso = _iso(now)
    ledger.update({"status": "sent", "messageId": sent.get("id"), "threadId": sent.get("threadId"), "sentAt": now_iso})

    contact = info.contact
    contact_ref = _user_ref(db, uid).collection("contacts").document(contact_id)
    updates = {
        "emailSent": True,
        "emailSentAt": now_iso,
        "lastMessageFrom": "user",
        "lastActivityAt": now_iso,
        "lastContactDate": now.strftime("%m/%d/%Y"),
        "inOutbox": True,
        "hasUnreadReply": False,
        "draftStillExists": False,
        "pipelineStage": "email_sent",
        "status": "Contacted",
    }
    if sent.get("id"):
        updates["gmailMessageId"] = sent["id"]
    if sent.get("threadId"):
        updates["gmailThreadId"] = sent["threadId"]
    old_draft = (contact.get("gmailDraftId") or "").strip()
    if old_draft:
        updates["gmailDraftId"] = firestore.DELETE_FIELD
        updates["gmailDraftUrl"] = firestore.DELETE_FIELD
    try:
        contact_ref.update(updates)
    except Exception:
        logger.exception("first_email_send: contact update failed uid=%s contact=%s", uid, contact_id)

    # The Gmail draft written earlier is now stale: delete it so it can't be
    # sent a second time from Gmail.
    if old_draft:
        try:
            from app.services.gmail_client import _load_user_gmail_creds, _gmail_service
            creds = _load_user_gmail_creds(uid)
            if creds:
                _gmail_service(creds).users().drafts().delete(userId="me", id=old_draft).execute()
        except Exception as exc:
            logger.info("first_email_send: stale draft cleanup skipped uid=%s draft=%s: %s", uid, old_draft, exc)

    if surface == "web_walkthrough":
        try:
            _user_ref(db, uid).set(
                {"walkthrough": {"status": "completed", "completedAt": now_iso, "contactId": contact_id}},
                merge=True,
            )
        except Exception:
            logger.exception("first_email_send: walkthrough completion failed uid=%s", uid)

    try:
        from app.services import email_tracking
        if tracking:
            email_tracking.register(tracking, uid, "outreach", recipient_email=info.recipient,
                                    contact_id=contact_id, message_id=sent.get("id"),
                                    thread_id=sent.get("threadId"), sent=True)
    except Exception:
        logger.exception("first_email_send: tracking register failed uid=%s", uid)
    try:
        from app.services.cooldown_service import record_outreach
        record_outreach(info.recipient, uid)
    except Exception:
        logger.warning("first_email_send: cooldown record failed uid=%s", uid)

    # Time from signup to first outreach, from the trusted Auth creation time.
    # No recipient, subject or body in the event.
    try:
        from app.services.first_outreach import account_created_at
        from app.utils.metrics_events import log_event
        created = account_created_at(uid)
        log_event(uid, "first_email_sent", {
            "surface": surface or "",
            "sent_at": now_iso,
            "signed_up_at": _iso(created) if created else None,
            "seconds_since_signup": int((now - created).total_seconds()) if created else None,
            "recovered": bool(prior_attempt),
        })
    except Exception:
        logger.exception("first_email_send: metrics event failed uid=%s", uid)

    return {"sent": True, "alreadySent": False, "contactId": contact_id,
            "messageId": sent.get("id"), "threadId": sent.get("threadId")}
