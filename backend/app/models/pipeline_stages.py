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

EMAIL_SENT = "email_sent"
WAITING_ON_REPLY = "waiting_on_reply"
EMAIL_OPENED = "email_opened"

# Sent, no reply yet.
AWAITING_REPLY_STAGES = frozenset({EMAIL_SENT, WAITING_ON_REPLY, EMAIL_OPENED})

# Stages a counted open may promote to email_opened. Never replied, a done
# stage or a draft stage.
OPEN_PROMOTABLE_STAGES = frozenset({EMAIL_SENT, WAITING_ON_REPLY})


def stage_after_send_confirmed(contact: dict) -> str:
    """Stage to write when a send is confirmed (Gmail webhook, draft-gone sync).

    A recipient can open the email before we learn it was sent, so a contact
    that already has an open goes straight to email_opened.
    """
    return EMAIL_OPENED if (contact or {}).get("emailOpenedAt") else WAITING_ON_REPLY
