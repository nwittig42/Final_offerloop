"""
Org alumni directories (OFF-42): list joinable orgs and join/leave them.

Membership is written here, server-side only (firestore.rules blocks client
writes to orgMemberships), so a later org-admin approval step only has to
change the status written on join from "active" to "pending".
"""
import re
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from firebase_admin import firestore

from app.extensions import get_db, require_firebase_auth
from app.services.org_directory import (
    MEMBERSHIPS_FIELD, _org_public, active_org_ids, check_org_password, directory_contacts, directory_row, list_orgs,
)

# Wrong-password throttle, per user per org, kept in Firestore so it holds
# across gunicorn workers.
ATTEMPTS_SUBCOLLECTION = "orgJoinAttempts"
MAX_ATTEMPTS = 8
ATTEMPT_WINDOW_SEC = 15 * 60

orgs_bp = Blueprint("orgs", __name__, url_prefix="/api/orgs")

# Must start with a letter or "_": Firestore field paths reject a segment
# that starts with a digit ("orgMemberships.2025club" raises on leave).
_ORG_ID_RE = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")


def _user_ref(db):
    return db.collection("users").document(request.firebase_user["uid"])


def _joinable(db, org_id: str) -> bool:
    if not _ORG_ID_RE.match(org_id):
        return False
    snap = db.collection("orgs").document(org_id).get()
    return bool(snap.exists and (snap.to_dict() or {}).get("joinable") is True)


@orgs_bp.route("", methods=["GET"])
@require_firebase_auth
def get_orgs():
    db = get_db()
    try:
        user = _user_ref(db).get()
        user_data = user.to_dict() if user.exists else {}
        return jsonify({"orgs": list_orgs(db), "memberships": active_org_ids(user_data)})
    except Exception as e:
        print(f"[Orgs] list failed: {e}")
        return jsonify({"error": "Could not load organizations"}), 500


@orgs_bp.route("/<org_id>/join", methods=["POST"])
@require_firebase_auth
def join_org(org_id):
    """Join with the org's password. Right password = instant active member."""
    db = get_db()
    if not _joinable(db, org_id):
        return jsonify({"error": "Organization not found"}), 404
    ref = _user_ref(db)
    attempts_ref = ref.collection(ATTEMPTS_SUBCOLLECTION).document(org_id)
    now = datetime.now(timezone.utc).timestamp()
    attempts_snap = attempts_ref.get()
    attempts = (attempts_snap.to_dict() or {}) if attempts_snap.exists else {}
    in_window = now - float(attempts.get("windowStart") or 0) < ATTEMPT_WINDOW_SEC
    failed = int(attempts.get("count") or 0) if in_window else 0
    if failed >= MAX_ATTEMPTS:
        return jsonify({"error": "too_many_attempts"}), 429

    password = ((request.get_json(silent=True) or {}).get("password") or "")
    ok = check_org_password(db, org_id, password)
    if ok is None:
        return jsonify({"error": "join_closed"}), 403
    if not ok:
        attempts_ref.set({"count": failed + 1, "windowStart": attempts.get("windowStart") if in_window else now})
        return jsonify({"error": "wrong_password", "attemptsLeft": MAX_ATTEMPTS - failed - 1}), 403

    if attempts_snap.exists:
        attempts_ref.delete()
    ref.set({MEMBERSHIPS_FIELD: {org_id: {
        "status": "active",
        "joinedAt": datetime.now(timezone.utc).isoformat(),
        "source": "password",
    }}}, merge=True)
    snap = ref.get()
    return jsonify({"ok": True, "memberships": active_org_ids(snap.to_dict() if snap.exists else {})})


@orgs_bp.route("/<org_id>/leave", methods=["POST"])
@require_firebase_auth
def leave_org(org_id):
    if not _ORG_ID_RE.match(org_id):
        return jsonify({"error": "Organization not found"}), 404
    db = get_db()
    ref = _user_ref(db)
    snap = ref.get()
    if snap.exists and org_id in ((snap.to_dict() or {}).get(MEMBERSHIPS_FIELD) or {}):
        ref.update({f"{MEMBERSHIPS_FIELD}.{org_id}": firestore.DELETE_FIELD})
        snap = ref.get()
    return jsonify({"ok": True, "memberships": active_org_ids(snap.to_dict() if snap.exists else {})})


def _member_org(db, org_id: str):
    """The org, if the signed-in user is an active member of it; else None."""
    if not _ORG_ID_RE.match(org_id):
        return None
    user = _user_ref(db).get()
    if not user.exists or org_id not in active_org_ids(user.to_dict()):
        return None
    snap = db.collection("orgs").document(org_id).get()
    return _org_public(snap.id, snap.to_dict() or {}) if snap.exists else None


@orgs_bp.route("/<org_id>/members", methods=["GET"])
@require_firebase_auth
def org_members(org_id):
    """The whole browsable directory: every living alum, with their email
    when we have one (free to members; Contact drafts the intro)."""
    db = get_db()
    org = _member_org(db, org_id)
    if org is None:
        return jsonify({"error": "not_a_member"}), 403
    try:
        rows = [directory_row(c) for c in directory_contacts(db, org, include_unreachable=True)]
        return jsonify({"org": org, "members": rows})
    except Exception as e:
        print(f"[Orgs] members failed for {org_id}: {e}")
        return jsonify({"error": "Could not load the directory"}), 500
