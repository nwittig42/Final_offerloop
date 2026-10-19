"""
Email open tracking.

Every email Offerloop builds for a user carries a 1x1 image whose URL holds a
random token. When the recipient's mail client loads the image, the public
pixel route calls record_open(), which stamps open status onto the contact doc
(users/{uid}/contacts/{id}). The website, Chrome extension and mobile app all
read that contact doc, so it is the single source of truth for open status.

Storage:
    emailTracking/{token}   one doc per tracked email (backend-only)
    users/{uid}/contacts/{id}
        emailOpenedAt       first counted open (ISO string)
        emailLastOpenedAt   most recent counted open
        emailOpenCount      number of counted opens

Known limits (inherent to pixel tracking, not bugs):
    - Apple Mail Privacy Protection preloads images, so an "open" can be a
      machine, not a person.
    - Clients that block remote images never report an open.
    - The sender re-reading their own sent email in Gmail later loads the pixel too
      and cannot be told apart from the recipient. Loads that happen while the
      email is still an unsent draft ARE filtered out (see _sent_at); so are loads within SELF_VIEW_GRACE of the send time.

Tracking must never block or break a send: every public function here
swallows its own errors.
"""
import html
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.extensions import get_db

logger = logging.getLogger(__name__)

COLLECTION = "emailTracking"

# Smallest valid transparent GIF.
PIXEL_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00"
    b"\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)

# A single open often produces a burst of image requests (proxy retries,
# preview pane then full view). Hits this close together count once.
DEDUPE_WINDOW = timedelta(seconds=30)

# Gmail renders a just-sent message to its sender, which loads the pixel. Hits
# this soon after the send time are the sender's own view, not the recipient's.
SELF_VIEW_GRACE = timedelta(seconds=60)


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _parse_iso(value):
    """Parse the ISO strings used across contact docs. Naive values are UTC."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def tracking_enabled(uid=None):
    """Off unless switched on, so deploying this code changes nothing by itself.

    EMAIL_OPEN_TRACKING=1            track every user
    EMAIL_OPEN_TRACKING_UIDS=a,b     track only these users (for a trial run)
    """
    if os.getenv("EMAIL_OPEN_TRACKING", "0") == "1":
        return True
    allowed = {u.strip() for u in os.getenv("EMAIL_OPEN_TRACKING_UIDS", "").split(",") if u.strip()}
    return bool(uid and uid in allowed)


def _base_url():
    return (os.getenv("EMAIL_TRACKING_BASE_URL") or "https://offerloop.ai").rstrip("/")


def start(uid):
    """Return a fresh tracking token, or None when this email can't be tracked."""
    if not uid or not tracking_enabled(uid):
        return None
    return secrets.token_urlsafe(16)


def pixel_url(token):
    return f"{_base_url()}/api/t/o/{token}.gif"


def pixel_tag(token):
    if not token:
        return ""
    return f'<img src="{pixel_url(token)}" width="1" height="1" alt="" style="border:0;">'


def add_pixel(html_body, token):
    """Insert the tracking pixel at the end of an HTML body."""
    if not token or not html_body:
        return html_body
    tag = pixel_tag(token)
    idx = html_body.lower().rfind("</body>")
    if idx == -1:
        return html_body + tag
    return html_body[:idx] + tag + "\n" + html_body[idx:]


def text_to_html(text):
    escaped = html.escape(text or "").replace("\r\n", "\n").replace("\n", "<br>")
    return f'<div style="font-family: Arial, Helvetica, sans-serif; font-size: 14px;">{escaped}</div>'


def tracked_text_mime(text, token):
    """Drop-in replacement for MIMEText(text) that can carry the pixel.

    A plain-text email has nowhere to put an image, so tracked emails go out as
    multipart/alternative: the original text plus an HTML copy with the pixel.
    Without a token this returns plain MIMEText, exactly as before.
    """
    if not token:
        return MIMEText(text)
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(text or "", "plain", "utf-8"))
    msg.attach(MIMEText(text_to_html(text) + pixel_tag(token), "html", "utf-8"))
    return msg


def register(token, uid, kind, recipient_email=None, contact_id=None,
             draft_id=None, thread_id=None, message_id=None, sent=False):
    """Record what a token belongs to. Call after the Gmail draft/send succeeds.

    sent=True means the email went straight out (messages.send), so every hit
    is a real open. For drafts, hits only count once the draft has been sent.
    """
    if not token or not uid:
        return
    try:
        now = _iso(_now())
        get_db().collection(COLLECTION).document(token).set({
            "uid": uid,
            "kind": kind,
            "recipientEmail": (recipient_email or "").strip().lower(),
            "contactId": contact_id or "",
            "gmailDraftId": draft_id or "",
            "gmailThreadId": thread_id or "",
            "gmailMessageId": message_id or "",
            "createdAt": now,
            "sentAt": now if sent else None,
            "openCount": 0,
        })
    except Exception as e:
        logger.warning("[email_tracking] register failed token=%s uid=%s: %s", token, uid, e)


def _resolve_contact(db, uid, data):
    """Find the contact this tracked email was written to."""
    contacts = db.collection("users").document(uid).collection("contacts")
    if data.get("contactId"):
        snap = contacts.document(data["contactId"]).get()
        if snap.exists:
            return snap.reference, snap.to_dict() or {}
    lookups = [
        ("gmailDraftId", data.get("gmailDraftId")),
        ("gmailThreadId", data.get("gmailThreadId")),
        ("draftToEmail", data.get("recipientEmail")),
        ("email", data.get("recipientEmail")),
    ]
    for field, value in lookups:
        if not value:
            continue
        docs = list(contacts.where(field, "==", value).limit(1).stream())
        if docs:
            return docs[0].reference, docs[0].to_dict() or {}
    return None, {}


def _draft_still_exists(uid, draft_id):
    """True/False when Gmail answers, None when we couldn't ask."""
    try:
        from googleapiclient.errors import HttpError
        from app.services.gmail_client import _load_user_gmail_creds, _gmail_service
        creds = _load_user_gmail_creds(uid)
        if not creds:
            return None
        service = _gmail_service(creds)
        try:
            service.users().drafts().get(userId="me", id=draft_id, format="minimal").execute()
            return True
        except HttpError as e:
            if getattr(e.resp, "status", None) == 404:
                return False
            return None
    except Exception as e:
        logger.info("[email_tracking] draft lookup failed uid=%s: %s", uid, e)
        return None


def _message_sent_time(uid, message_id):
    """Send time (datetime) when Gmail says this message was sent, else None.

    None covers every "can't tell" case: no credentials, Gmail errors, a 404
    (the id is no longer valid), or the message still being a draft.
    """
    try:
        from app.services.gmail_client import _load_user_gmail_creds, _gmail_service
        creds = _load_user_gmail_creds(uid)
        if not creds:
            return None
        service = _gmail_service(creds)
        msg = service.users().messages().get(
            userId="me", id=message_id, format="minimal").execute()
        labels = msg.get("labelIds") or []
        if "SENT" not in labels or "DRAFT" in labels:
            return None
        return datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=timezone.utc)
    except Exception as e:
        logger.info("[email_tracking] message lookup failed uid=%s: %s", uid, e)
        return None


def _sent_at(uid, data, contact):
    """When did this tracked email leave the user's account? None = not yet.

    While it is still a draft, the only person loading the pixel is the user
    looking at their own draft, so those hits must not count.
    """
    stored = _parse_iso(data.get("sentAt"))
    if stored:
        return stored
    message_id = data.get("gmailMessageId")
    if message_id:
        sent = _message_sent_time(uid, message_id)
        if sent:
            return sent
    draft_id = data.get("gmailDraftId")
    if draft_id:
        exists = _draft_still_exists(uid, draft_id)
        if exists is not None:
            # Draft gone means sent, but Gmail didn't say when: treat as
            # sent long enough ago that the sender's own render is over.
            return None if exists else _now() - SELF_VIEW_GRACE - timedelta(seconds=1)
    # Couldn't ask Gmail: fall back to the contact's own sent stamp.
    created = _parse_iso(data.get("createdAt"))
    sent = _parse_iso(contact.get("emailSentAt"))
    if created and sent and sent >= created:
        return sent
    return None


def record_open(token):
    """Handle one pixel load. Returns True when it counted as an open."""
    if not token:
        return False
    try:
        from google.cloud.firestore_v1 import Increment

        db = get_db()
        ref = db.collection(COLLECTION).document(token)
        snap = ref.get()
        if not snap.exists:
            return False
        data = snap.to_dict() or {}
        uid = data.get("uid")
        if not uid:
            return False

        now = _now()
        now_iso = _iso(now)

        contact_ref, contact = _resolve_contact(db, uid, data)

        sent_at = _sent_at(uid, data, contact)
        if sent_at is None:
            ref.update({"preSendHits": Increment(1), "lastHitAt": now_iso})
            return False

        if not data.get("sentAt"):
            ref.update({"sentAt": _iso(sent_at)})
            data["sentAt"] = _iso(sent_at)

        if now - sent_at < SELF_VIEW_GRACE:
            ref.update({"selfViewHits": Increment(1), "lastHitAt": now_iso})
            return False

        last = _parse_iso(data.get("lastOpenedAt"))
        if last and now - last < DEDUPE_WINDOW:
            return False

        tracking_updates = {
            "openCount": Increment(1),
            "lastOpenedAt": now_iso,
            "lastHitAt": now_iso,
        }
        if not data.get("firstOpenedAt"):
            tracking_updates["firstOpenedAt"] = now_iso
        if contact_ref is not None and not data.get("contactId"):
            tracking_updates["contactId"] = contact_ref.id
        ref.update(tracking_updates)

        if contact_ref is None:
            logger.info("[email_tracking] open for token=%s uid=%s matched no contact", token, uid)
            return True

        contact_updates = {
            "emailLastOpenedAt": now_iso,
            "emailOpenCount": Increment(1),
        }
        if not contact.get("emailOpenedAt"):
            contact_updates["emailOpenedAt"] = now_iso
        contact_ref.update(contact_updates)
        return True
    except Exception as e:
        logger.warning("[email_tracking] record_open failed token=%s: %s", token, e)
        return False
