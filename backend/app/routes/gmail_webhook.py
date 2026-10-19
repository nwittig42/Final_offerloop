"""
Gmail push notification webhook — receives Pub/Sub notifications and detects replies.
"""
import base64
import hmac
import json
import logging
import re
import threading
from datetime import datetime, timezone
from email.utils import parseaddr

from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

from google.cloud.firestore_v1 import ArrayUnion, transactional

from app.config import GMAIL_WEBHOOK_SECRET
from app.extensions import get_db
from app.models.pipeline_stages import stage_after_send_confirmed
from app.services.gmail_client import (
    find_uid_by_gmail_address,
    get_gmail_service_for_user,
    start_gmail_watch,
)
from app.utils.bounce_detection import is_bounce_message

gmail_webhook_bp = Blueprint("gmail_webhook", __name__, url_prefix="/api/gmail")


def _extract_email_from_header(from_header):
    """Extract email from 'Name <email@domain.com>' or 'email@domain.com'."""
    if not from_header:
        return ""
    from_header = (from_header or "").strip()
    match = re.search(r"<([^>]+)>", from_header)
    if match:
        return match.group(1).strip().lower()
    return from_header.lower()


def _lookup_impression_context(db, uid, contact_email):
    """Find the most recent recommendation_shown event for this uid+contact_email.

    Returns dict with request_id, session_id, rank, score, matched.
    Returns empty/default values if no match (e.g. manual contact entry).
    Never raises.
    """
    result = {"request_id": "", "session_id": "", "rank": None, "score": None, "matched": False}
    try:
        if not db or not contact_email:
            return result
        query = (
            db.collection("recommendation_events")
            .where("event_type", "==", "recommendation_shown")
            .where("uid", "==", uid)
            .where("contact_email", "==", contact_email.strip().lower())
            .order_by("server_timestamp", direction="DESCENDING")
            .limit(1)
        )
        docs = list(query.stream())
        if docs:
            data = docs[0].to_dict() or {}
            result["request_id"] = data.get("request_id", "")
            result["session_id"] = data.get("session_id", "")
            result["rank"] = data.get("rank")
            result["score"] = data.get("score")
            result["matched"] = True
        else:
            # No impression found — user emailed someone not surfaced by the recommender
            logger.info(f"[gmail_webhook] No impression found for uid={uid} contact_email={contact_email} (manual entry or pre-Track-C contact)")
    except Exception as e:
        logger.warning(f"[gmail_webhook] Impression lookup failed for uid={uid}: {e}")
    return result


def _append_reply_notification(db, notif_ref, new_item, now_iso):
    """Idempotently prepend a reply notification item, keyed on Gmail message id.

    Pub/Sub delivers at-least-once, so the same reply can be processed by two
    overlapping webhook workers. This runs the read-modify-write inside a
    Firestore transaction and skips the insert when an item with the same
    messageId already exists, so a redelivery cannot produce a second item for
    the same reply (which would differ only by timestamp and slip past the
    frontend dedupe). Returns the resulting unreadReplyCount, or None when the
    item was skipped as a duplicate.
    """
    transaction = db.transaction()

    @transactional
    def _run(txn):
        snapshot = notif_ref.get(transaction=txn)
        data = snapshot.to_dict() if snapshot.exists else {}
        items = list(data.get("items", []))
        message_id = new_item.get("messageId")
        if message_id and any(it.get("messageId") == message_id for it in items):
            return None
        items.insert(0, new_item)
        items = items[:20]
        unread_count = max(0, int(data.get("unreadReplyCount", 0))) + 1
        txn.set(
            notif_ref,
            {
                "unreadReplyCount": unread_count,
                "items": items,
                "updatedAt": now_iso,
            },
            merge=True,
        )
        return unread_count

    return _run(transaction)


def _apply_bounce(
    *,
    uid,
    db,
    contact_doc,
    from_email,
    subject_header,
    snippet,
    now_iso,
):
    """Full bounce treatment for a single contact match.

    Called from both bounce gates in _process_gmail_notification: the early
    gate (before reply detection, prevents notification false positives) and
    the per-contact gate inside the reply branch. Shared so both paths
    stamp the contact, push to the suppression list, fire the metric, and
    dismiss pending nudges identically. Pre-extraction, the early gate did a
    bare-minimum update and skipped suppression+metrics, which meant bounces
    detected there silently leaked back through future send paths.
    """
    contact_data = contact_doc.to_dict() or {}
    contact_id = contact_doc.id
    bounced_email = (
        contact_data.get("email")
        or contact_data.get("draftToEmail")
        or ""
    ).strip().lower()

    contact_ref = db.collection("users").document(uid).collection("contacts").document(contact_id)
    bounce_updates = {
        "pipelineStage": "bounced",
        "inOutbox": False,
        "hasUnreadReply": False,
        "threadStatus": "bounced",
        "emailVerificationStatus": "bounced",
        "bouncedAt": now_iso,
        "lastActivityAt": now_iso,
        "lastMessageSnippet": (snippet or "")[:200],
        "updatedAt": now_iso,
    }
    logger.info(
        f"[gmail_webhook] uid={uid} BOUNCE for contact_id={contact_id} "
        f"email={bounced_email} from={from_email} subject={subject_header!r}"
    )
    contact_ref.update(bounce_updates)

    try:
        from app.services.suppression import record_bounce
        record_bounce(uid, bounced_email, contact_id=contact_id, reason="dsn")
    except Exception as supp_err:
        logger.warning(f"[gmail_webhook] suppression record failed: {supp_err}")

    try:
        from app.utils.metrics_events import log_event
        log_event(uid, "email_bounced", {
            "contact_id": contact_id,
            "email": bounced_email,
            "from": from_email,
        })
    except Exception:
        pass

    try:
        from app.services.nudge_service import dismiss_pending_nudges_for_contact
        dismiss_pending_nudges_for_contact(db, uid, contact_id)
    except Exception:
        pass


def _build_reply_updates(contact_data, message_snippet, now_iso):
    """Build the Firestore update dict for an inbound reply matched to a contact.

    Pure function (no I/O) so the reply-update logic stays unit-testable. The
    base fields mirror the inline dict this replaced: mark the thread replied and
    unread, stamp activity, and set replyReceivedAt only on the first reply.

    Additionally clears archivedAt when it was set. A contact auto-archived by
    mark_contact_resolution (hard_no / ghosted) keeps inOutbox and gmailThreadId,
    so the reply still matches it, but get_outbox_contacts (outbox_service.py)
    filters out any archivedAt-set contact. Without clearing it, a genuine reply
    would be recorded and notified yet stay hidden. Cleared only when actually
    set, so never-archived contacts are untouched.
    """
    updates = {
        "pipelineStage": "replied",
        "inOutbox": True,
        "hasUnreadReply": True,
        "lastActivityAt": now_iso,
        "lastMessageSnippet": message_snippet,
        "threadStatus": "new_reply",
        "updatedAt": now_iso,
    }
    if not contact_data.get("replyReceivedAt"):
        updates["replyReceivedAt"] = now_iso
    if contact_data.get("archivedAt"):
        updates["archivedAt"] = None
    return updates


def _process_gmail_notification(email_address, history_id):
    """
    Background worker: find user, fetch history, detect new replies, update contacts and notifications.
    """
    try:
        logger.info(f"[gmail_webhook] Processing notification for email={email_address} historyId={history_id}")
        uid = find_uid_by_gmail_address(email_address)
        if not uid:
            logger.info(f"[gmail_webhook] No user found for Gmail address: {email_address}")
            return
        logger.info(f"[gmail_webhook] Resolved uid={uid} for email={email_address}")

        db = get_db()
        if not db:
            print(f"[gmail_webhook] Database not available")
            return

        gmail_ref = db.collection("users").document(uid).collection("integrations").document("gmail")
        gmail_doc = gmail_ref.get()
        if not gmail_doc.exists:
            print(f"[gmail_webhook] No Gmail integration for uid={uid}")
            return

        gmail_data = gmail_doc.to_dict() or {}
        last_history_id_raw = gmail_data.get("watchHistoryId")
        try:
            last_history_id = str(last_history_id_raw).strip() if last_history_id_raw is not None else None
        except Exception:
            last_history_id = None

        # Skip if we already processed this or newer (at-least-once delivery)
        try:
            hi_int = int(history_id) if history_id else 0
            last_int = int(last_history_id) if last_history_id else 0
            if last_int >= hi_int and last_history_id is not None:
                return
        except (TypeError, ValueError):
            pass

        if not last_history_id:
            print(f"[gmail_webhook] No watchHistoryId for uid={uid}, updating to {history_id} and skipping")
            gmail_ref.set({"watchHistoryId": history_id}, merge=True)
            return

        # NOTE: watchHistoryId is advanced at the END of this function, only
        # after all messages in the history delta have been processed. If we
        # crash mid-loop, Pub/Sub at-least-once redelivery will re-invoke this
        # webhook with the same history_id and we'll retry from last_history_id.
        # Downstream writes (contact matching, stage transitions, notification
        # doc) are idempotent, so redelivery is safe. Advancing the pointer
        # early risks silently dropping messages if any step below fails.

        user_doc = db.collection("users").document(uid).get()
        user_email = (user_doc.to_dict() or {}).get("email") if user_doc.exists else email_address
        if not user_email:
            user_email = email_address

        service = get_gmail_service_for_user(user_email, user_id=uid)
        if not service:
            print(f"[gmail_webhook] Could not get Gmail service for uid={uid}")
            return

        # Fetch history (paginate, deduplicate message IDs)
        all_message_ids = []
        seen_msg_ids = set()
        page_token = None
        while True:
            try:
                history_request = service.users().history().list(
                    userId="me",
                    startHistoryId=last_history_id,
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                )
                history_response = history_request.execute()
            except Exception as e:
                err_str = str(e).lower()
                if "404" in err_str or "not found" in err_str or "historyid" in err_str:
                    print(f"[gmail_webhook] History 404 or invalid for uid={uid}, resetting watch: {e}")
                    try:
                        start_gmail_watch(uid)
                    except Exception as watch_err:
                        print(f"[gmail_webhook] Failed to reset watch: {watch_err}")
                else:
                    print(f"[gmail_webhook] History list error for uid={uid}: {e}")
                return

            for hist in history_response.get("history", []):
                for added in hist.get("messagesAdded", []):
                    msg = added.get("message", {})
                    msg_id = msg.get("id")
                    if msg_id and msg_id not in seen_msg_ids:
                        seen_msg_ids.add(msg_id)
                        all_message_ids.append((msg_id, msg.get("threadId")))

            page_token = history_response.get("nextPageToken")
            if not page_token:
                break

        logger.info(f"[gmail_webhook] uid={uid} fetched {len(all_message_ids)} new message(s) from history (lastHistoryId={last_history_id} -> {history_id})")

        from datetime import timezone
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        user_email_lower = (user_email or "").lower()
        contacts_ref = db.collection("users").document(uid).collection("contacts")
        notif_ref = db.collection("users").document(uid).collection("notifications").document("outbox")

        for msg_id, thread_id in all_message_ids:
            if not thread_id:
                continue
            try:
                msg_resp = service.users().messages().get(
                    userId="me",
                    id=msg_id,
                    format="metadata",
                    metadataHeaders=["From", "To", "Subject"],
                ).execute()
            except Exception as e:
                print(f"[gmail_webhook] messages.get error msg={msg_id}: {e}")
                continue

            logger.info(f"[gmail_webhook] uid={uid} fetched message msg_id={msg_id} thread_id={thread_id}")

            headers = msg_resp.get("payload", {}).get("headers", [])
            from_header = next((h.get("value", "") for h in headers if h.get("name", "").lower() == "from"), "")
            from_email = _extract_email_from_header(from_header)

            logger.info(f"[gmail_webhook] uid={uid} msg_id={msg_id} from={from_email} user_email={user_email_lower}")

            label_ids = msg_resp.get('labelIds', [])
            logger.info(f"[gmail_webhook] uid={uid} msg_id={msg_id} labels={label_ids}")

            is_sent = 'SENT' in label_ids
            is_from_user = from_email and user_email_lower and from_email == user_email_lower

            if not is_sent and is_from_user:
                # User's own message without SENT label — skip (e.g. draft)
                logger.info(f"[gmail_webhook] uid={uid} msg_id={msg_id} skipping — from user but no SENT label")
                continue

            if is_from_user:
                # --- User sent a message (draft was sent) ---
                logger.info(f"[gmail_webhook] uid={uid} msg_id={msg_id} detected as SENT message (from==user)")
                to_header = next((h.get("value", "") for h in headers if h.get("name", "").lower() == "to"), "")
                _, to_email = parseaddr(to_header)
                to_email = (to_email or "").lower().strip()
                logger.info(f"[gmail_webhook] uid={uid} msg_id={msg_id} to_email={to_email}")

                if not to_email or not thread_id:
                    logger.info(f"[gmail_webhook] uid={uid} msg_id={msg_id} skipping: no to_email or thread_id")
                    continue

                contact_ref = None
                contact_doc = None

                # Try: contact with gmailThreadId == thread_id
                logger.info(f"[gmail_webhook] uid={uid} Strategy 1: matching gmailThreadId={thread_id}")
                thread_matches = contacts_ref.where("gmailThreadId", "==", thread_id).limit(1).get()
                if thread_matches:
                    contact_doc = thread_matches[0]
                    contact_ref = contact_doc.reference
                    logger.info(f"[gmail_webhook] uid={uid} Strategy 1 MATCHED: contact_id={contact_doc.id}")
                else:
                    logger.info(f"[gmail_webhook] uid={uid} Strategy 1 no match")
                    # Strategy 2a: contact whose email matches 'To' AND is in draft state
                    logger.info(f"[gmail_webhook] uid={uid} Strategy 2a: matching email={to_email} with draft state")
                    email_matches = contacts_ref.where("email", "==", to_email).limit(5).get()
                    logger.info(f"[gmail_webhook] uid={uid} Strategy 2a found {len(email_matches)} email matches")
                    for doc in email_matches:
                        data = doc.to_dict() or {}
                        stage = data.get("pipelineStage")
                        has_draft = (
                            data.get("gmailDraftId")
                            or data.get("gmailDraftUrl")
                        )
                        if stage == "draft_created" or has_draft:
                            contact_doc = doc
                            contact_ref = doc.reference
                            logger.info(f"[gmail_webhook] uid={uid} Strategy 2a MATCHED: contact_id={doc.id} stage={stage}")
                            break

                    if not contact_ref:
                        logger.info(f"[gmail_webhook] uid={uid} Strategy 2a no match")

                    # Strategy 2b: check alternateEmails on draft_created contacts
                    if not contact_ref:
                        logger.info(f"[gmail_webhook] uid={uid} Strategy 2b: scanning alternateEmails for to_email={to_email}")
                        try:
                            draft_contacts = contacts_ref.where(
                                "pipelineStage", "==", "draft_created"
                            ).where("inOutbox", "==", True).get()
                            for doc in draft_contacts:
                                data = doc.to_dict() or {}
                                alt_emails = data.get("alternateEmails") or []
                                if to_email in [e.lower() for e in alt_emails]:
                                    contact_doc = doc
                                    contact_ref = doc.reference
                                    print(f"[gmail_webhook] Strategy 2b matched: {to_email} in alternateEmails for contact {doc.id}")
                                    break
                        except Exception as e:
                            print(f"[gmail_webhook] Strategy 2b alternateEmails scan error: {e}")

                    # Strategy 2c: match by draftToEmail field
                    if not contact_ref:
                        logger.info(f"[gmail_webhook] uid={uid} Strategy 2c: matching draftToEmail={to_email}")
                        try:
                            draft_to_matches = contacts_ref.where(
                                "draftToEmail", "==", to_email
                            ).where("inOutbox", "==", True).limit(5).get()
                            for doc in draft_to_matches:
                                data = doc.to_dict() or {}
                                stage = data.get("pipelineStage")
                                has_draft = data.get("gmailDraftId") or data.get("gmailDraftUrl")
                                if stage == "draft_created" or has_draft:
                                    contact_doc = doc
                                    contact_ref = doc.reference
                                    print(f"[gmail_webhook] Strategy 2c matched: draftToEmail={to_email} for contact {doc.id}")
                                    break
                        except Exception as e:
                            print(f"[gmail_webhook] Strategy 2c draftToEmail scan error: {e}")

                # Strategy 3: find contacts whose draft has disappeared (was sent)
                # Capped at 10 candidates to prevent excessive Gmail API calls
                STRATEGY_3_CAP = 10
                if not contact_ref:
                    logger.info(f"[gmail_webhook] uid={uid} Strategy 3: checking for disappeared drafts (cap={STRATEGY_3_CAP})")
                    try:
                        draft_candidates = contacts_ref.where(
                            "pipelineStage", "==", "draft_created"
                        ).where("inOutbox", "==", True).limit(STRATEGY_3_CAP + 1).get()
                        candidates_list = list(draft_candidates)
                        if len(candidates_list) > STRATEGY_3_CAP:
                            print(f"[gmail_webhook] Strategy 3: {len(candidates_list)}+ draft contacts, capping at {STRATEGY_3_CAP}")
                            candidates_list = candidates_list[:STRATEGY_3_CAP]
                        for candidate in candidates_list:
                            cdata = candidate.to_dict() or {}
                            draft_id = cdata.get("gmailDraftId")
                            if not draft_id:
                                continue
                            # Verify the contact's intended recipient matches the sent-to address
                            contact_to = (cdata.get("draftToEmail") or cdata.get("email") or "").lower()
                            if to_email and contact_to and to_email != contact_to:
                                continue
                            try:
                                service.users().drafts().get(
                                    userId="me", id=draft_id, format="minimal"
                                ).execute()
                                # Draft still exists, skip
                            except Exception as draft_err:
                                if hasattr(draft_err, "resp") and getattr(draft_err.resp, "status", 0) == 404:
                                    # Draft is gone — it was sent
                                    contact_doc = candidate
                                    contact_ref = candidate.reference
                                    # Store the sent-to email as alternateEmail if different from stored email
                                    stored_email = (cdata.get("email") or "").lower()
                                    if to_email and to_email != stored_email:
                                        try:
                                            contact_ref.update({"alternateEmails": ArrayUnion([to_email])})
                                            print(f"[gmail_webhook] Stored alternateEmail {to_email} for contact {candidate.id}")
                                        except Exception as ae:
                                            print(f"[gmail_webhook] Failed to store alternateEmail: {ae}")
                                    print(f"[gmail_webhook] Strategy 3 matched: draft {draft_id} gone for contact {candidate.id}")
                                    break
                    except Exception as e:
                        print(f"[gmail_webhook] Strategy 3 draft scan error: {e}")

                if not contact_ref:
                    logger.info(f"[gmail_webhook] uid={uid} msg_id={msg_id} NO CONTACT FOUND for sent message to={to_email} thread={thread_id}")
                    continue

                contact_data = contact_doc.to_dict() or {}
                current_stage = contact_data.get("pipelineStage")

                # Only update if currently in a draft/pre-send state
                if current_stage not in (None, "draft_created", "email_sent"):
                    logger.info(f"[gmail_webhook] uid={uid} contact_id={contact_doc.id} skipping update: current stage={current_stage} not in draft/pre-send states")
                    continue

                update_fields = {
                    "draftStillExists": False,
                    # An open can be counted before this webhook confirms the
                    # send; such a contact goes straight to email_opened.
                    "pipelineStage": stage_after_send_confirmed(contact_data),
                    "inOutbox": True,
                    "lastActivityAt": now_iso,
                    "updatedAt": now_iso,
                }

                if not contact_data.get("emailSentAt"):
                    update_fields["emailSentAt"] = now_iso

                if not contact_data.get("gmailThreadId"):
                    update_fields["gmailThreadId"] = thread_id

                msg_snippet = msg_resp.get("snippet") or ""
                if msg_snippet:
                    update_fields["lastMessageSnippet"] = msg_snippet

                logger.info(f"[gmail_webhook] uid={uid} contact_id={contact_doc.id} UPDATING sent message: stage {current_stage}->{update_fields['pipelineStage']}, fields={list(update_fields.keys())}")
                contact_ref.update(update_fields)

                # Metrics: email_actually_sent
                try:
                    from app.utils.metrics_events import log_event
                    log_event(uid, "email_actually_sent", {
                        "contact_id": contact_doc.id,
                    })
                except Exception:
                    pass

                # Lifecycle signal: stamp firstEmailSentAt / lastEmailSentAt
                try:
                    from app.services.lifecycle_signals import stamp_first_email_sent
                    stamp_first_email_sent(uid)
                except Exception:
                    pass

                # Cooldown: record global outreach for saturation tracking
                try:
                    from app.services.cooldown_service import record_outreach
                    record_outreach(to_email, uid)
                except Exception:
                    pass

                # Recommendation event: email_sent
                # Backfill impression context (request_id, session_id, rank, score)
                # from the most recent recommendation_shown for this uid+contact_email.
                try:
                    from app.utils.recommendation_events import log_recommendation_event
                    impression_ctx = _lookup_impression_context(db, uid, to_email)
                    log_recommendation_event(
                        "email_sent",
                        uid,
                        contact_id=contact_doc.id,
                        contact_email=to_email,
                        rank=impression_ctx.get("rank"),
                        score=impression_ctx.get("score"),
                        surface="gmail_webhook",
                        extra={
                            "impression_request_id": impression_ctx.get("request_id", ""),
                            "impression_session_id": impression_ctx.get("session_id", ""),
                            "has_impression": impression_ctx.get("matched", False),
                        },
                    )
                except Exception:
                    pass

                continue

            # Bounce / DSN detection: Mailer-Daemon and similar postmaster
            # messages share the original thread_id, so without this gate we
            # mis-label them as "{contact} responded to you!" and surface a
            # notification.
            subject_header = next((h.get("value", "") for h in headers if h.get("name", "").lower() == "subject"), "")
            raw_snippet = msg_resp.get("snippet") or ""
            if is_bounce_message(from_email, subject_header, raw_snippet):
                logger.info(
                    f"[gmail_webhook] uid={uid} msg_id={msg_id} detected BOUNCE "
                    f"from={from_email!r} subject={subject_header[:80]!r}"
                )
                try:
                    bounce_matches = contacts_ref.where("gmailThreadId", "==", thread_id).limit(1).get()
                    if bounce_matches:
                        _apply_bounce(
                            uid=uid, db=db, contact_doc=bounce_matches[0],
                            from_email=from_email, subject_header=subject_header,
                            snippet=raw_snippet, now_iso=now_iso,
                        )
                    else:
                        logger.info(
                            f"[gmail_webhook] uid={uid} bounce for thread={thread_id} "
                            f"had no matching contact — dropping silently"
                        )
                except Exception as bounce_err:
                    logger.warning(
                        f"[gmail_webhook] uid={uid} bounce contact update failed for "
                        f"thread={thread_id}: {bounce_err}"
                    )
                continue

            # Find contact with this thread (reply from contact)
            logger.info(f"[gmail_webhook] uid={uid} msg_id={msg_id} detected as INCOMING reply from={from_email}")
            logger.info(f"[gmail_webhook] uid={uid} Reply Strategy 1: matching gmailThreadId={thread_id}")
            try:
                query = contacts_ref.where("gmailThreadId", "==", thread_id).limit(1)
                contact_docs = list(query.stream())
            except Exception as e:
                logger.info(f"[gmail_webhook] Contact query error: {e}")
                continue

            if contact_docs:
                logger.info(f"[gmail_webhook] uid={uid} Reply Strategy 1 MATCHED: contact_id={contact_docs[0].id}")

            # Fallback: match by from_email against stored email, draftToEmail, or alternateEmails
            if not contact_docs and from_email:
                logger.info(f"[gmail_webhook] uid={uid} Reply Strategy 1 no match, trying email fallbacks for from={from_email}")
                try:
                    email_matches = contacts_ref.where("email", "==", from_email).where("inOutbox", "==", True).limit(1).get()
                    if email_matches:
                        contact_docs = email_matches

                    # Try draftToEmail match
                    if not contact_docs:
                        draft_to_matches = contacts_ref.where("draftToEmail", "==", from_email).where("inOutbox", "==", True).limit(1).get()
                        if draft_to_matches:
                            contact_docs = draft_to_matches
                            print(f"[gmail_webhook] Reply matched via draftToEmail: {from_email} -> contact {draft_to_matches[0].id}")

                    if not contact_docs:
                        # Scan outbox contacts for alternateEmails match
                        outbox_contacts = contacts_ref.where("inOutbox", "==", True).get()
                        for doc in outbox_contacts:
                            data = doc.to_dict() or {}
                            alt_emails = data.get("alternateEmails") or []
                            if from_email in [e.lower() for e in alt_emails]:
                                contact_docs = [doc]
                                print(f"[gmail_webhook] Reply matched via alternateEmails: {from_email} -> contact {doc.id}")
                                break
                except Exception as e:
                    print(f"[gmail_webhook] Reply email fallback error: {e}")

            if not contact_docs:
                logger.info(f"[gmail_webhook] uid={uid} msg_id={msg_id} NO CONTACT FOUND for incoming reply from={from_email} thread={thread_id}")
                continue

            contact_doc = contact_docs[0]
            contact_id = contact_doc.id
            contact_data = contact_doc.to_dict() or {}
            contact_name = (contact_data.get("firstName") or "").strip()
            if contact_name:
                contact_name += " "
            contact_name += (contact_data.get("lastName") or "").strip()
            contact_name = contact_name.strip() or contact_data.get("email", "")
            company = (contact_data.get("company") or "").strip()
            full_snippet = msg_resp.get("snippet") or ""
            message_snippet = full_snippet[:100]

            # Bounce / DSN gate: if this message looks like a Mailer-Daemon
            # delivery failure, stamp the contact as bounced + suppress the
            # address instead of treating it as a reply.
            subject_header = next(
                (h.get("value", "") for h in headers if h.get("name", "").lower() == "subject"),
                "",
            )
            if is_bounce_message(from_email, subject_header, full_snippet):
                _apply_bounce(
                    uid=uid, db=db, contact_doc=contact_doc,
                    from_email=from_email, subject_header=subject_header,
                    snippet=full_snippet, now_iso=now_iso,
                )
                continue

            contact_ref = contacts_ref.document(contact_id)
            updates = _build_reply_updates(contact_data, message_snippet, now_iso)
            logger.info(f"[gmail_webhook] uid={uid} contact_id={contact_id} UPDATING reply: stage->replied, hasUnreadReply->True, fields={list(updates.keys())}")
            contact_ref.update(updates)

            # Metrics: reply_received
            try:
                from app.utils.metrics_events import log_event
                hours_since = None
                draft_at = contact_data.get("draftCreatedAt") or contact_data.get("emailSentAt")
                if draft_at:
                    try:
                        sent_dt = datetime.fromisoformat(str(draft_at).replace("Z", "+00:00"))
                        hours_since = round((datetime.now(timezone.utc) - sent_dt).total_seconds() / 3600, 1)
                    except Exception:
                        pass
                log_event(uid, "reply_received", {
                    "contact_id": contact_id,
                    "hours_since_send": hours_since,
                })
            except Exception:
                pass

            # Lifecycle signal: stamp firstReplyReceivedAt / lastReplyReceivedAt
            try:
                from app.services.lifecycle_signals import stamp_first_reply
                stamp_first_reply(uid)
            except Exception:
                pass

            # Recommendation event: email_replied
            try:
                from app.utils.recommendation_events import log_recommendation_event
                log_recommendation_event(
                    "email_replied",
                    uid,
                    contact_id=contact_id,
                    contact_email=from_email,
                    surface="gmail_webhook",
                    extra={"hours_since_send": hours_since},
                )
            except Exception:
                pass

            # Dismiss any pending nudges for this contact (reply makes follow-up nudges stale)
            try:
                from app.services.nudge_service import dismiss_pending_nudges_for_contact
                dismiss_pending_nudges_for_contact(db, uid, contact_id)
            except Exception as nudge_err:
                logger.warning(f"[gmail_webhook] Failed to dismiss nudges for contact={contact_id}: {nudge_err}")

            logger.info(f"[gmail_webhook] uid={uid} Reply detected and processed for contact={contact_id} from={from_header}")

            # Notification doc (idempotent: dedupe by Gmail message id so an
            # at-least-once Pub/Sub redelivery, or two overlapping workers,
            # cannot append a second item for the same reply).
            new_item = {
                "contactId": contact_id,
                "contactName": contact_name,
                "company": company,
                "snippet": message_snippet,
                "timestamp": now_iso,
                "read": False,
                "messageId": msg_id,
            }
            unread_count = _append_reply_notification(db, notif_ref, new_item, now_iso)
            if unread_count is None:
                logger.info(f"[gmail_webhook] uid={uid} notification skipped (duplicate messageId={msg_id}) for contact={contact_id}")
            else:
                logger.info(f"[gmail_webhook] uid={uid} notification updated: unreadReplyCount={unread_count} for contact={contact_id}")

            # Reply Coach: auto-generate a draft reply in the background
            try:
                from app.services.reply_coach import spawn_reply_coach
                spawn_reply_coach(uid, contact_id, contact_data, message_snippet)
            except Exception as rc_err:
                logger.warning(f"[gmail_webhook] reply_coach spawn failed for contact={contact_id}: {rc_err}")

        # All history delta messages processed successfully — advance the
        # watchHistoryId pointer. On crash/exception above, this line is
        # skipped, and the next webhook replays from last_history_id (safe
        # because downstream writes are idempotent).
        try:
            gmail_ref.set({"watchHistoryId": history_id}, merge=True)
        except Exception as persist_err:
            logger.error(
                f"[gmail_webhook] uid={uid} FAILED to persist watchHistoryId={history_id}: {persist_err}"
            )

        logger.info(f"[gmail_webhook] uid={uid} processing complete: handled {len(all_message_ids)} message(s)")

    except Exception as e:
        logger.error(f"[gmail_webhook] _process_gmail_notification error: {e}")
        import traceback
        traceback.print_exc()


def _verify_google_pubsub_jwt() -> bool:
    """Verify the Google-signed OIDC JWT in the Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    id_token = auth_header.split("Bearer ", 1)[1]
    try:
        from google.oauth2 import id_token as google_id_token
        from google.auth.transport import requests as google_requests
        claim = google_id_token.verify_oauth2_token(
            id_token,
            google_requests.Request(),
            audience=None,  # Accept any audience; Pub/Sub uses the push endpoint URL
        )
        # Verify the issuer is Google
        issuer = claim.get("iss", "")
        if issuer not in ("accounts.google.com", "https://accounts.google.com"):
            logger.warning("[gmail_webhook] JWT issuer mismatch: %s", issuer)
            return False
        return True
    except Exception as e:
        logger.warning("[gmail_webhook] JWT verification failed: %s", e)
        return False


@gmail_webhook_bp.post("/webhook")
def webhook():
    """
    Pub/Sub push endpoint. Verifies authenticity via Google OIDC JWT (primary)
    or static token (fallback for backwards compatibility), decodes message,
    dispatches processing to a background thread, and returns 200 immediately.
    """
    # Primary: verify Google-signed JWT from Pub/Sub
    jwt_ok = _verify_google_pubsub_jwt()

    # Fallback: static token (for backwards compat during migration)
    token = (request.args.get("token") or "").strip()
    token_ok = bool(GMAIL_WEBHOOK_SECRET and hmac.compare_digest(token, GMAIL_WEBHOOK_SECRET))

    if not jwt_ok and not token_ok:
        logger.warning("[gmail_webhook] Auth failed: jwt=%s token=%s", jwt_ok, token_ok)
        return jsonify({"error": "Forbidden"}), 403

    envelope = request.get_json(silent=True) or {}
    message = envelope.get("message", {})
    data_b64 = message.get("data", "")
    if not data_b64:
        return jsonify({"status": "ok"}), 200

    try:
        data_bytes = base64.urlsafe_b64decode(data_b64)
        data = json.loads(data_bytes.decode("utf-8"))
    except Exception as e:
        print(f"[gmail_webhook] Failed to decode message: {e}")
        return jsonify({"status": "ok"}), 200

    email_address = (data.get("emailAddress") or "").strip()
    history_id = str(data.get("historyId", ""))

    threading.Thread(
        target=_process_gmail_notification,
        args=(email_address, history_id),
        daemon=True,
    ).start()

    return jsonify({"status": "ok"}), 200
