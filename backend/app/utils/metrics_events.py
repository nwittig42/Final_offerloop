"""
Fire-and-forget metrics event logging.
All writes wrapped in try/except — never blocks request flow.
"""
import logging
from datetime import datetime, timezone
from google.cloud.firestore_v1 import SERVER_TIMESTAMP
from app.extensions import get_db

logger = logging.getLogger(__name__)

VALID_EVENT_TYPES = {
    "email_generated",
    "email_actually_sent",
    "email_bounced",
    "hunter_rate_limited",
    "pdl_topup_triggered",
    "pdl_topup_records_fetched",
    "pdl_budget_cap_hit",
    "reply_received",
    "search_performed",
    "briefing_viewed",
    "reply_response_sent",
    "coffee_chat_prep_used",
    "onboarding_step_viewed",
    "onboarding_step_completed",
    "suggestion_shown",
    "suggestion_clicked",
    "suggestion_dismissed",
    "first_email_sent",
}


def log_event(uid, event_type, properties=None):
    """Write a metrics event to Firestore. Swallows all errors."""
    try:
        if event_type not in VALID_EVENT_TYPES:
            logger.warning("metrics log_event called with unknown event_type=%s", event_type)
            return
        db = get_db()
        db.collection("metrics_events").add({
            "uid": uid or "unknown",
            "event_type": event_type,
            "properties": properties or {},
            "timestamp": SERVER_TIMESTAMP,
            "event_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        })
    except Exception:
        logger.exception("metrics log_event failed for event_type=%s uid=%s", event_type, uid)
