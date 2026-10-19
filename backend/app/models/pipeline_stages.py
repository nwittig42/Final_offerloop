"""Pipeline stage values shared by every module that reads or writes
users/{uid}/contacts/{id}.pipelineStage.

Stage order:
    new -> draft_created -> email_sent / waiting_on_reply -> email_opened
        -> replied -> meeting_scheduled / connected

email_opened is set only by the tracking pixel (services/email_tracking.py).
An opened contact is still waiting for a reply, so every check for "sent,
awaiting a reply" must use AWAITING_REPLY_STAGES rather than the old
email_sent / waiting_on_reply pair; otherwise opened contacts silently drop
out of reply sync, nudges and stats.

Imported directly (not via app.models) so it stays free of side effects.
"""
from datetime import datetime, timezone

EMAIL_SENT = "email_sent"
WAITING_ON_REPLY = "waiting_on_reply"
EMAIL_OPENED = "email_opened"

# Sent, no reply yet.
AWAITING_REPLY_STAGES = frozenset({EMAIL_SENT, WAITING_ON_REPLY, EMAIL_OPENED})

# Stages a counted open may promote to email_opened. Never replied, a done
# stage or a draft stage.
OPEN_PROMOTABLE_STAGES = frozenset({EMAIL_SENT, WAITING_ON_REPLY})


def _parse_ts(value):
    """ISO string or Firestore datetime -> aware datetime, else None.

    Naive values are UTC (mirrors services/email_tracking._parse_iso, which
    can't be imported here without pulling in Firestore).
    """
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


def stage_after_send_confirmed(contact: dict) -> str:
    """Stage to write when a send is confirmed (Gmail webhook, draft-gone sync).

    A recipient can open the email before we learn it was sent, so a contact
    whose latest open came after the current draft was made goes straight to
    email_opened.

    emailOpenedAt is the first open ever and is never cleared, so it can't be
    trusted on its own: a contact that was opened, then re-drafted, would show
    Opened the moment the new email went out. Only an open (emailLastOpenedAt,
    falling back to emailOpenedAt) at or after the draft's creation time
    (draftCreatedAt / emailGeneratedAt, whichever is later) counts. With no
    draft timestamp we can't tell which email was opened, so: waiting_on_reply.
    """
    contact = contact or {}
    opened = (_parse_ts(contact.get("emailLastOpenedAt"))
              or _parse_ts(contact.get("emailOpenedAt")))
    drafted = [t for t in (_parse_ts(contact.get("draftCreatedAt")),
                           _parse_ts(contact.get("emailGeneratedAt"))) if t]
    if opened and drafted and opened >= max(drafted):
        return EMAIL_OPENED
    return WAITING_ON_REPLY
