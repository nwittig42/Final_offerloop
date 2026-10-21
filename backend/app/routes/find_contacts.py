"""Find contacts routes: people to reach for a saved job, 5 credits per person, once per student per job."""
from __future__ import annotations

import logging

from firebase_admin import firestore
from google.api_core.exceptions import AlreadyExists
from flask import Blueprint, jsonify, request

from app.extensions import get_db, require_firebase_auth
from app.routes.job_board import RECRUITER_CREDIT_COST
from app.services.auth import check_and_reset_credits, deduct_credits_atomic
from app.services.find_contacts.service import find_contacts_for_job, reveal_email

logger = logging.getLogger(__name__)

find_contacts_bp = Blueprint("find_contacts", __name__, url_prefix="/api/job-board")

MIN_PEOPLE = 3  # pre-check covers a full 3-slot result


def _student_school(user_data: dict) -> str | None:
    academics = user_data.get("academics")
    candidates = [
        academics.get("university") if isinstance(academics, dict) else None,
        user_data.get("university"),
        user_data.get("school"),
    ]
    return next((c for c in candidates if isinstance(c, str) and c.strip()), None)


def _insufficient(required: int, available: int):
    return jsonify({
        "error": "Insufficient credits",
        "creditsRequired": required,
        "creditsAvailable": available,
    }), 402


@find_contacts_bp.route("/find-contacts", methods=["POST"])
@require_firebase_auth
def find_contacts():
    uid = request.firebase_user.get("uid")
    data = request.get_json(force=True, silent=True) or {}
    job_id = data.get("jobId")
    if not isinstance(job_id, str) or not job_id.strip() or "/" in job_id:
        return jsonify({"error": "jobId is required"}), 400

    db = get_db()
    if not db:
        return jsonify({"error": "Database not available"}), 500

    user_ref = db.collection("users").document(uid)
    user_doc = user_ref.get()
    if not user_doc.exists:
        return jsonify({"error": "User not found"}), 404
    user_data = user_doc.to_dict() or {}
    current_credits = check_and_reset_credits(user_ref, user_data)

    unlock_ref = user_ref.collection("contactUnlocks").document(job_id)
    already_unlocked = unlock_ref.get().exists

    required = RECRUITER_CREDIT_COST * MIN_PEOPLE
    if not already_unlocked and current_credits < required:
        return _insufficient(required, current_credits)

    result = find_contacts_for_job(db, job_id, _student_school(user_data))
    status = result.get("status")
    if status == "searching":
        return jsonify({"status": "searching"}), 202
    if status != "ready":
        return jsonify({"error": "Job not found"}), 404

    charged = 0
    balance = current_credits
    if not already_unlocked:
        amount = RECRUITER_CREDIT_COST * len(result.get("people") or [])
        try:
            # Claim first: only one concurrent request can create the doc and pay.
            unlock_ref.create({"charged": amount, "at": firestore.SERVER_TIMESTAMP})
        except AlreadyExists:
            amount = 0
        if amount:
            ok, new_balance = deduct_credits_atomic(uid, amount, "find_contacts")
            if not ok:
                try:
                    unlock_ref.delete()
                except Exception:
                    logger.exception("[FindContacts] failed to release unlock claim uid=%s job=%s", uid, job_id)
                return _insufficient(amount, current_credits)
            charged, balance = amount, new_balance

    return jsonify({**result, "creditsCharged": charged, "credits": balance}), 200


@find_contacts_bp.route("/find-contacts/email", methods=["POST"])
@require_firebase_auth
def find_contacts_email():
    data = request.get_json(force=True, silent=True) or {}
    job_id = data.get("jobId")
    person_key = data.get("personKey")
    if not isinstance(job_id, str) or not job_id.strip() or "/" in job_id \
            or not isinstance(person_key, str) or not person_key.strip():
        return jsonify({"error": "jobId and personKey are required"}), 400

    db = get_db()
    if not db:
        return jsonify({"error": "Database not available"}), 500

    uid = request.firebase_user.get("uid")
    unlock = db.collection("users").document(uid).collection("contactUnlocks").document(job_id).get()
    if not unlock.exists:
        return jsonify({"error": "Find contacts for this job first"}), 403

    result = reveal_email(db, job_id, person_key)
    if result is None:
        return jsonify({"error": "Email not available"}), 404
    return jsonify(result), 200
