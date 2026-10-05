"""
Nudge API endpoints.

GET  /api/nudges              — fetch pending/recent nudges for current user
PATCH /api/nudges/<id>        — mark a nudge as read/acted_on/dismissed
POST /api/nudges/<id>/draft   — create a Gmail draft from the nudge follow-up text
PUT  /api/nudge-preferences   — update nudge timing and notification preferences
"""
import logging

from flask import Blueprint, jsonify, request

from app.extensions import require_firebase_auth, get_db

logger = logging.getLogger(__name__)

nudges_bp = Blueprint("nudges", __name__, url_prefix="/api")

VALID_NUDGE_STATUSES = frozenset({"read", "acted_on", "dismissed"})


@nudges_bp.get("/nudges")
@require_firebase_auth
def get_nudges():
    """Fetch pending and recent nudges for the current user."""
    uid = request.firebase_user["uid"]
    db = get_db()

    limit = min(int(request.args.get("limit", 20)), 50)
    status_filter = request.args.get("status")  # optional: "pending", "read", etc.

    nudges_ref = db.collection("users").document(uid).collection("nudges")

    # Try compound query first; fall back to client-side filter if the
    # composite index (status + createdAt) hasn't been created yet.
    docs = None
    if status_filter:
        try:
            docs = list(
                nudges_ref
                .where("status", "==", status_filter)
                .order_by("createdAt", direction="DESCENDING")
                .limit(limit)
                .stream()
            )
        except Exception:
            # Composite index likely missing — fall back to unordered filter
            docs = None

    if docs is None:
        try:
            docs = list(
                nudges_ref.order_by("createdAt", direction="DESCENDING").limit(limit).stream()
            )
        except Exception:
            # Even single-field order may fail on empty collection — fetch unordered
            docs = list(nudges_ref.limit(limit).stream())
        # Apply status filter client-side when the compound query wasn't available
        if status_filter:
            docs = [d for d in docs if (d.to_dict() or {}).get("status") == status_filter]

    nudges = []
    for doc in docs:
        data = doc.to_dict() or {}
        data["id"] = doc.id
        nudges.append(data)

    return jsonify({"nudges": nudges, "count": len(nudges)}), 200


@nudges_bp.patch("/nudges/<nudge_id>")
@require_firebase_auth
def update_nudge(nudge_id: str):
    """Update a nudge's status (read, acted_on, dismissed)."""
    uid = request.firebase_user["uid"]
    db = get_db()
    body = request.get_json(silent=True) or {}

    new_status = body.get("status")
    if not new_status or new_status not in VALID_NUDGE_STATUSES:
        return jsonify({
            "error": f"Invalid status. Must be one of: {', '.join(sorted(VALID_NUDGE_STATUSES))}"
        }), 400

    nudge_ref = (
        db.collection("users").document(uid)
        .collection("nudges").document(nudge_id)
    )
    nudge_doc = nudge_ref.get()
    if not nudge_doc.exists:
        return jsonify({"error": "Nudge not found"}), 404

    from datetime import datetime, timezone
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    updates = {"status": new_status, "updatedAt": now_iso}
    if new_status == "acted_on":
        updates["actedOn"] = True
        updates["actedOnAt"] = now_iso
    elif new_status == "dismissed":
        updates["dismissedAt"] = now_iso

    nudge_ref.update(updates)
    return jsonify({"ok": True, "nudgeId": nudge_id, "status": new_status}), 200


@nudges_bp.post("/nudges/<nudge_id>/draft")
@require_firebase_auth
def create_nudge_draft(nudge_id: str):
    """Create a Gmail draft from the nudge's follow-up email text."""
    uid = request.firebase_user["uid"]
    db = get_db()

    # Get the nudge
    nudge_ref = (
        db.collection("users").document(uid)
        .collection("nudges").document(nudge_id)
    )
    nudge_doc = nudge_ref.get()
    if not nudge_doc.exists:
        return jsonify({"error": "Nudge not found"}), 404

    nudge_data = nudge_doc.to_dict() or {}
    follow_up_text = nudge_data.get("followUpDraft", "")
    if not follow_up_text:
        return jsonify({"error": "This nudge has no follow-up draft text"}), 400

    contact_id = nudge_data.get("contactId", "")
    if not contact_id:
        return jsonify({"error": "Nudge has no associated contact"}), 400

    # Get the contact's email address
    contact_ref = db.collection("users").document(uid).collection("contacts").document(contact_id)
    contact_doc = contact_ref.get()
    if not contact_doc.exists:
        return jsonify({"error": "Contact not found"}), 404

    contact_data = contact_doc.to_dict() or {}
    to_email = contact_data.get("email") or contact_data.get("draftToEmail") or ""
    if not to_email:
        return jsonify({"error": "Contact has no email address"}), 400

    contact_name = nudge_data.get("contactName", "")
    subject = f"Following up — {contact_name}" if contact_name else "Following up"

    # Create Gmail draft via the user's connected Gmail account
    try:
        from app.services.gmail_client import get_gmail_service_for_user
        import base64
        from email.mime.text import MIMEText

        user_data = (db.collection("users").document(uid).get().to_dict() or {})
        user_email = user_data.get("email", "")

        gmail_service = get_gmail_service_for_user(user_email, user_id=uid)
        if not gmail_service:
            # Fallback: return a mailto: compose link instead
            from urllib.parse import quote
            compose_url = f"https://mail.google.com/mail/?view=cm&to={quote(to_email)}&su={quote(subject)}&body={quote(follow_up_text)}"
            return jsonify({"ok": True, "composeUrl": compose_url, "draftId": None}), 200

        from app.services import email_tracking
        tracking_token = email_tracking.start(uid)
        msg = email_tracking.tracked_text_mime(follow_up_text, tracking_token)
        msg["to"] = to_email
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        draft = gmail_service.users().drafts().create(
            userId="me", body={"message": {"raw": raw}}
        ).execute()
        draft_id = draft.get("id", "")
        email_tracking.register(
            tracking_token, uid, "follow_up", recipient_email=to_email,
            contact_id=contact_id, draft_id=draft_id,
            thread_id=draft.get("message", {}).get("threadId"),
        )

        # Mark nudge as acted_on
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        nudge_ref.update({
            "status": "acted_on",
            "actedOn": True,
            "actedOnAt": now_iso,
        })

        return jsonify({
            "ok": True,
            "draftId": draft_id,
            "gmailUrl": f"https://mail.google.com/mail/#drafts/{draft_id}",
        }), 201
    except Exception as e:
        logger.error("Failed to create nudge draft for uid=%s nudge=%s: %s", uid, nudge_id, e)
        # Fallback: return compose link
        from urllib.parse import quote
        compose_url = f"https://mail.google.com/mail/?view=cm&to={quote(to_email)}&su={quote(subject)}&body={quote(follow_up_text)}"
        return jsonify({"ok": True, "composeUrl": compose_url, "draftId": None}), 200


@nudges_bp.put("/nudge-preferences")
@require_firebase_auth
def update_nudge_preferences():
    """Update nudge preferences (timing, frequency)."""
    uid = request.firebase_user["uid"]
    db = get_db()
    body = request.get_json(silent=True) or {}

    # Validate follow-up days (3-14 calendar days)
    followup_days = body.get("followUpDays")
    if followup_days is not None:
        try:
            followup_days = int(followup_days)
            if not (3 <= followup_days <= 14):
                return jsonify({"error": "followUpDays must be between 3 and 14"}), 400
        except (TypeError, ValueError):
            return jsonify({"error": "followUpDays must be an integer"}), 400

    # Validate max nudges per day (1-10)
    max_per_day = body.get("maxNudgesPerDay")
    if max_per_day is not None:
        try:
            max_per_day = int(max_per_day)
            if not (1 <= max_per_day <= 10):
                return jsonify({"error": "maxNudgesPerDay must be between 1 and 10"}), 400
        except (TypeError, ValueError):
            return jsonify({"error": "maxNudgesPerDay must be an integer"}), 400

    prefs = {}
    if followup_days is not None:
        prefs["nudgeFollowUpDays"] = followup_days
    if max_per_day is not None:
        prefs["nudgeMaxPerDay"] = max_per_day
    if "enabled" in body:
        prefs["nudgesEnabled"] = bool(body["enabled"])

    if not prefs:
        return jsonify({"error": "No valid preferences provided"}), 400

    db.collection("users").document(uid).update(prefs)
    return jsonify({"ok": True, "preferences": prefs}), 200
