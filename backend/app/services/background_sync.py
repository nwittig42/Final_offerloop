"""
Background sync for Outbox: find stale threads and sync them with Gmail.
"""
import logging

from app.extensions import get_db
from app.models.pipeline_stages import AWAITING_REPLY_STAGES
from app.services.outbox_service import sync_contact_thread

logger = logging.getLogger(__name__)

STALE_PIPELINE_STAGES = AWAITING_REPLY_STAGES | frozenset({"replied"})


def get_stale_thread_ids(uid, max_threads=10):
    """
    Return contact IDs that are candidates for sync: inOutbox=True,
    pipelineStage in [email_sent, waiting_on_reply, email_opened, replied],
    have gmailThreadId.
    Sorted by lastSyncAt ascending (oldest first). At most max_threads.
    """
    try:
        db = get_db()
    except RuntimeError:
        return []

    contacts_ref = db.collection("users").document(uid).collection("contacts")
    query = contacts_ref.where("inOutbox", "==", True)
    docs = list(query.stream())

    candidates = []
    for doc in docs:
        data = doc.to_dict() or {}
        stage = data.get("pipelineStage")
        if stage not in STALE_PIPELINE_STAGES:
            continue
        if not data.get("gmailThreadId"):
            continue
        if data.get("archivedAt"):
            continue
        candidates.append((doc.id, data.get("lastSyncAt") or ""))

    candidates.sort(key=lambda x: x[1])
    return [cid for cid, _ in candidates[:max_threads]]


def sync_stale_threads(uid, max_threads=10):
    """
    Sync up to max_threads stale contacts with Gmail.
    Returns {"synced_count": N, "failed_count": N, "total_stale": N}.
    """
    ids = get_stale_thread_ids(uid, max_threads=max_threads)
    total_stale = len(ids)
    synced_count = 0
    failed_count = 0

    for contact_id in ids:
        try:
            sync_contact_thread(uid, contact_id)
            synced_count += 1
        except Exception as e:
            failed_count += 1
            logger.warning("background_sync failed for contact %s: %s", contact_id, e)

    return {
        "synced_count": synced_count,
        "failed_count": failed_count,
        "total_stale": total_stale,
    }
