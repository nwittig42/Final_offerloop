"""
One-off migration utilities for contact schema and pipeline stages.
"""
from datetime import datetime

from app.extensions import get_db
from app.models.pipeline_stages import stage_after_send_confirmed


# Pipeline stage enum values (canonical)
PIPELINE_STAGES = (
    "new",
    "draft_created",
    "email_sent",
    "waiting_on_reply",
    "email_opened",
    "replied",
    "meeting_scheduled",
    "connected",
    "no_response",
    "bounced",
    "closed",
)


def backfill_pipeline_stages(uid: str) -> dict:
    """
    Backfill pipelineStage and emailSentAt for all contacts of a user.
    Used for migration from threadStatus / draftStillExists to canonical pipelineStage.

    Rules:
    - hasUnreadReply == True → pipelineStage = "replied"
    - gmailThreadId and draftStillExists == False → pipelineStage = "waiting_on_reply"
      ("email_opened" when the pixel already recorded an open),
      and set emailSentAt from draftCreatedAt or lastActivityAt if missing
    - gmailDraftId and draftStillExists != False → pipelineStage = "draft_created"
    - No draft/thread IDs → pipelineStage = null (not in pipeline)

    Returns dict with: updated_count, skipped_count, errors (list of str).
    """
    db = get_db()
    if not db:
        return {"updated_count": 0, "skipped_count": 0, "errors": ["Database not initialized"]}

    contacts_ref = db.collection("users").document(uid).collection("contacts")
    docs = list(contacts_ref.stream())
    updated_count = 0
    skipped_count = 0
    errors = []

    for doc in docs:
        try:
            data = doc.to_dict() or {}
            doc_id = doc.id

            has_thread = bool(data.get("gmailThreadId") or data.get("gmail_thread_id"))
            has_draft = bool(
                data.get("gmailDraftId")
                or data.get("gmail_draft_id")
                or data.get("gmailDraftUrl")
                or data.get("gmail_draft_url")
            )
            draft_still_exists = data.get("draftStillExists", True)
            has_unread_reply = bool(data.get("hasUnreadReply") or data.get("has_unread_reply"))

            # Determine pipelineStage and optional emailSentAt
            new_stage = None
            email_sent_at = None

            if has_unread_reply:
                new_stage = "replied"
            elif has_thread and draft_still_exists is False:
                new_stage = stage_after_send_confirmed(data)
                if not data.get("emailSentAt") and not data.get("email_sent_at"):
                    # Estimate from draftCreatedAt or lastActivityAt
                    for key in ("draftCreatedAt", "draft_created_at", "lastActivityAt", "last_activity_at"):
                        val = data.get(key)
                        if val:
                            if isinstance(val, datetime):
                                email_sent_at = val.isoformat()
                            else:
                                email_sent_at = val
                            break
                    if not email_sent_at:
                        email_sent_at = datetime.utcnow().isoformat()
            elif has_draft and draft_still_exists is not False:
                new_stage = "draft_created"
            # else: no draft/thread → leave pipelineStage null

            updates = {}
            if new_stage is not None:
                updates["pipelineStage"] = new_stage
            if email_sent_at is not None:
                updates["emailSentAt"] = email_sent_at
            if updates:
                updates["updatedAt"] = datetime.utcnow().isoformat()
                doc.reference.update(updates)
                updated_count += 1
            else:
                skipped_count += 1
        except Exception as e:
            errors.append(f"{doc.id}: {e}")

    return {
        "updated_count": updated_count,
        "skipped_count": skipped_count,
        "total": len(docs),
        "errors": errors,
    }


def _contact_sort_key(doc):
    """Key for choosing the 'best' contact among duplicates (most recent first)."""
    data = doc.to_dict() or {}
    ts = (
        data.get("lastActivityAt")
        or data.get("last_activity_at")
        or data.get("updatedAt")
        or data.get("updated_at")
        or data.get("createdAt")
        or data.get("created_at")
        or ""
    )
    return (ts, doc.id)


def deduplicate_contacts(uid: str) -> dict:
    """
    Merge duplicate contacts (same email) into a single doc per email.
    - Groups contacts by normalized email (strip + lower).
    - For each group with >1 doc: keeps the doc with the most recent lastActivityAt/updatedAt,
      merges in non-empty fields from the others, then deletes the duplicate docs.
    - Logs what was merged and deleted.

    Returns dict with: merged_count (emails that had duplicates), deleted_count, merged_log (list of str), errors.
    """
    db = get_db()
    if not db:
        return {
            "merged_count": 0,
            "deleted_count": 0,
            "merged_log": [],
            "errors": ["Database not initialized"],
        }

    contacts_ref = db.collection("users").document(uid).collection("contacts")
    docs = list(contacts_ref.stream())
    by_email = {}

    for doc in docs:
        data = doc.to_dict() or {}
        email = (data.get("email") or "").strip().lower()
        if not email:
            continue
        if email not in by_email:
            by_email[email] = []
        by_email[email].append(doc)

    merged_count = 0
    deleted_count = 0
    merged_log = []
    errors = []

    for email, group in by_email.items():
        if len(group) <= 1:
            continue

        # Keep the most recent doc; merge others into it, then delete others
        group_sorted = sorted(group, key=_contact_sort_key, reverse=True)
        keep_doc = group_sorted[0]
        keep_id = keep_doc.id
        keep_data = keep_doc.to_dict() or {}
        duplicates = group_sorted[1:]

        # Merge: fill keep_doc with any non-empty field from duplicates that keep_doc is missing
        all_keys = set(keep_data.keys())
        for dup_doc in duplicates:
            all_keys.update((dup_doc.to_dict() or {}).keys())
        merge_updates = {}
        for key in all_keys:
            if key in ("email", "createdAt", "created_at"):
                continue
            if keep_data.get(key) not in (None, ""):
                continue
            for dup_doc in duplicates:
                val = (dup_doc.to_dict() or {}).get(key)
                if val is not None and val != "":
                    merge_updates[key] = val
                    break

        if merge_updates:
            merge_updates["updatedAt"] = datetime.utcnow().isoformat()
            try:
                keep_doc.reference.update(merge_updates)
            except Exception as e:
                errors.append(f"merge {keep_id}: {e}")
                continue

        duplicate_ids = [d.id for d in duplicates]
        for dup_doc in duplicates:
            try:
                dup_doc.reference.delete()
                deleted_count += 1
            except Exception as e:
                errors.append(f"delete {dup_doc.id}: {e}")

        merged_count += 1
        merged_log.append(
            f"email={email}: kept {keep_id}, merged from {duplicate_ids}, deleted {duplicate_ids}"
        )

    return {
        "merged_count": merged_count,
        "deleted_count": deleted_count,
        "merged_log": merged_log,
        "errors": errors,
    }
