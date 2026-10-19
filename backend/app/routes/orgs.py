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
from app.services.org_directory import MEMBERSHIPS_FIELD, active_org_ids, list_orgs

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
    db = get_db()
    if not _joinable(db, org_id):
        return jsonify({"error": "Organization not found"}), 404
    ref = _user_ref(db)
    ref.set({MEMBERSHIPS_FIELD: {org_id: {
        "status": "active",
        "joinedAt": datetime.now(timezone.utc).isoformat(),
        "source": "self_select",
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
