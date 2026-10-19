"""
Recommendation event logger — writes to Firestore recommendation_events collection.

Full 18-field schema captured from day 1. Most fields will be null initially.
Fire-and-forget: never raises, never blocks the caller.
"""
import logging
from datetime import datetime, timezone

from flask import g
from google.cloud.firestore_v1 import SERVER_TIMESTAMP

from app.extensions import get_db

logger = logging.getLogger(__name__)

# Valid event types for the recommendation funnel
VALID_REC_EVENT_TYPES = {
    "recommendation_shown",     # contact appeared in search results
    "recommendation_clicked",   # user clicked on a contact card
    "email_drafted",            # email generated for contact
    "email_sent",               # email confirmed sent (via gmail webhook)
    "email_replied",            # reply detected (via gmail webhook)
    "meeting_scheduled",        # meeting booked with contact
    "offer_received",           # offer from contact's company
    "connection_made",          # contact reached the connected stage
    # People deck (person_ranker). Each carries a deterministic doc_id.
    "deck_served",              # one row per deck: every person shown, rank, features
    "person_swipe_right",       # user swiped right on a ranker candidate
    "person_swipe_left",        # user swiped left on a ranker candidate
    "person_swipe_skip",        # user skipped a ranker candidate (pass)
    "person_swipe_restore",     # user put a passed person back in the pool
    "email_revealed",           # metered email reveal, credit-worthy result
    "outreach_sent",            # deck outreach sent straight from the card
    "outreach_drafted",         # deck outreach saved as a Gmail draft
}


def log_recommendation_event(
    event_type: str,
    uid: str,
    *,
    contact_id: str = "",
    contact_email: str = "",
    rank: int | None = None,
    score: float | None = None,
    model_version: str = "",
    variant: str = "",
    surface: str = "",
    search_query: dict | None = None,
    features_snapshot: dict | None = None,
    attribution_source: str = "",
    extra: dict | None = None,
    doc_id: str | None = None,
) -> None:
    """Write a recommendation event to Firestore. Swallows all errors.

    When doc_id is provided, uses .document(doc_id).set() — deterministic,
    so client retries and Pub/Sub redelivery can't double-count. When
    omitted, falls back to .add() (auto-ID) for legacy call sites.
    """
    try:
        if event_type not in VALID_REC_EVENT_TYPES:
            logger.warning("recommendation log called with unknown event_type=%s", event_type)
            return

        db = get_db()
        if not db:
            return

        now = datetime.now(timezone.utc)

        doc = {
            # Identity
            "event_type": event_type,
            "uid": uid or "unknown",
            "contact_id": contact_id,
            "contact_email": contact_email,

            # Request context (from middleware)
            "request_id": getattr(g, "request_id", "") if _has_request_context() else "",
            "session_id": getattr(g, "session_id", "") if _has_request_context() else "",

            # Ranking metadata
            "rank": rank,
            "score": score,
            "model_version": model_version or "heuristic_v0",
            "variant": variant,
            "surface": surface or "find_search",

            # Search context
            "search_query": search_query or {},

            # Feature snapshot (captured at impression time, irrecoverable later)
            "features_snapshot": features_snapshot or {},

            # Attribution
            "attribution_source": attribution_source,

            # Timestamps
            "event_ts": now.isoformat(),
            "event_date": now.strftime("%Y-%m-%d"),
            "server_timestamp": SERVER_TIMESTAMP,

            # Extensible
            **(extra or {}),
        }

        if doc_id:
            db.collection("recommendation_events").document(doc_id).set(doc)
        else:
            db.collection("recommendation_events").add(doc)

        logger.debug(
            "recommendation event logged: type=%s uid=%s contact=%s",
            event_type, uid, contact_id,
        )
    except Exception:
        logger.exception(
            "recommendation event log failed: type=%s uid=%s", event_type, uid
        )


def _has_request_context() -> bool:
    """Check if we're inside a Flask request context."""
    try:
        _ = g.request_id  # noqa: F841
        return True
    except RuntimeError:
        return False
