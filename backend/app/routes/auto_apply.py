"""
Auto-apply endpoints.

Routes mounted under /api/job-board/auto-apply/* so they sit alongside the
existing job-board surface. Application Profile endpoints are mounted at
/api/users/application-profile/* on the same blueprint.

Phase 1 endpoints:
  GET  /api/users/application-profile           — read saved profile
  POST /api/users/application-profile           — save profile (stamps acknowledgedAt)
  POST /api/job-board/auto-apply/prepare        — generate preview (loads job + profile + LLM open-ended)
  POST /api/job-board/auto-apply/<id>/submit    — kick off Browserless form submission
  GET  /api/job-board/auto-apply/<id>/status    — poll job status

Tier gate: prepare/submit/status require Pro or Elite. Profile endpoints are
available to all signed-in users (Free can set their profile in advance even
though they cannot run auto-apply).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict

from flask import Blueprint, jsonify, request

from app.extensions import get_db, require_firebase_auth, require_tier
from app.services.auth import refund_credits_atomic
from app.services.auto_apply.answer_library import delete_answer, list_for_user, save_answer
from app.services.auto_apply.application_profile import (
    get_application_profile,
    is_acknowledged,
    save_application_profile,
    work_auth_complete,
)
from app.services.auto_apply.ats_detector import detect_platform, is_eligible
from app.services.auto_apply.preview import build_preview, load_user_for_apply
from app.services.auto_apply.submit_service import submit_auto_apply_for_user
from app.services.rq_queue import enqueue

# The filler itself runs OUT of this process — see services/auto_apply/jobs.py.
# Playwright's driver, started inside a web worker, killed the whole container
# ~23s in (2026-07-12), taking drafts and Scout with it. The web service now
# only ENQUEUES; the browser starts in the RQ worker.
#
# The worker owns the in-run wall-clock guard. This is the other half: a
# READ-time reaper, because if the WORKER dies mid-fill nothing in-process is
# left alive to finalize the doc. Whenever the client polls status or the list,
# an in-flight job silent longer than this is declared dead and refunded — so a
# job can never sit at running/filling_form forever (field logs 2026-07-12: 4
# such zombies, none ever resolved). Mirrors draft_jobs.STALE_RUNNING, and kept
# above the worker's 240s guard so a healthy-but-slow fill isn't reaped early.
AUTO_APPLY_STALE_AFTER_SECONDS = 300


def _reap_if_stale(uid: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Declare a silent in-flight auto-apply dead, and refund it.

    Read-time (so it survives worker restarts). Returns the doc dict, patched
    in place when reaped, so callers can just hand the result to jsonify.
    """
    if not data:
        return data
    stamp = data.get("updated_at") or data.get("created_at")
    if not stamp:
        return data
    try:
        last = datetime.fromisoformat(str(stamp).replace("Z", ""))
    except (TypeError, ValueError):
        return data

    # An emailed verification code that nobody entered for a day is dead:
    # ATS codes expire in minutes to hours, so after 24h the honest state is
    # "this one needs the company site", not an amber card that outlives the
    # code it is waiting for (the Bland AI zombie, 2026-08-30). Refund path
    # matches the silent-run reap below.
    if data.get("status") == "needs_verification":
        if (datetime.utcnow() - last).total_seconds() < 86400:
            return data
        now = datetime.utcnow().isoformat()
        patch: Dict[str, Any] = {
            "status": "submit_failed",
            "stage": "failed",
            "failure_reason": (
                "The confirmation code expired before it was entered. "
                "Apply on the company site to finish this one."
            ),
            "failed_at": now,
            "updated_at": now,
        }
        try:
            charged = int(data.get("credits_charged") or 0)
            if charged and not data.get("credits_refunded") and not data.get("dry_run"):
                refund_credits_atomic(uid, charged, "auto_apply_refund")
                patch["credits_refunded"] = True
            (
                get_db().collection("users").document(uid)
                .collection("autoApplyJobs").document(str(data.get("auto_apply_id")))
                .update(patch)
            )
        except Exception:
            logger.exception("verification reap failed for auto-apply %s", data.get("auto_apply_id"))
            return data
        return {**data, **patch}

    if data.get("status") not in ("queued", "running"):
        return data
    if (datetime.utcnow() - last).total_seconds() < AUTO_APPLY_STALE_AFTER_SECONDS:
        return data

    now = datetime.utcnow().isoformat()
    patch: Dict[str, Any] = {
        "status": "failed",
        "stage": "failed",
        "failure_reason": (
            "The application stopped partway through. Nothing was submitted — "
            "please try again."
        ),
        "failed_at": now,
        "updated_at": now,
    }
    auto_apply_id = data.get("auto_apply_id")
    try:
        charged = int(data.get("credits_charged") or 0)
        if charged and not data.get("credits_refunded") and not data.get("dry_run"):
            refund_credits_atomic(uid, charged, "auto_apply_refund")
            patch["credits_refunded"] = True
        (
            get_db().collection("users").document(uid)
            .collection("autoApplyJobs").document(str(auto_apply_id))
            .update(patch)
        )
    except Exception:
        logger.exception("stale reap failed for auto-apply %s", auto_apply_id)
        return data
    logger.info("reaped stale auto-apply %s (silent > %ss)", auto_apply_id, AUTO_APPLY_STALE_AFTER_SECONDS)
    return {**data, **patch}


logger = logging.getLogger(__name__)

auto_apply_bp = Blueprint("auto_apply", __name__)


# =============================================================================
# Application Profile (available to all tiers)
# =============================================================================

@auto_apply_bp.route("/api/users/application-profile", methods=["GET"])
@require_firebase_auth
def read_application_profile():
    uid = request.firebase_user["uid"]
    profile = get_application_profile(uid)
    return jsonify({
        "profile": profile,
        "acknowledged": is_acknowledged(profile),
        "work_auth_complete": work_auth_complete(profile),
    })


@auto_apply_bp.route("/api/users/application-answers", methods=["GET"])
@require_firebase_auth
def list_saved_answers():
    """The saved custom-question answers, for the transparency surface
    (Rylan 2026-08-18: "you don't ever get to see those answers"). Read-only
    list of what auto-apply has learned and reuses; sensitive slots never
    land in this library by design (answer_library refuses them)."""
    uid = request.firebase_user["uid"]
    return jsonify({"answers": list_for_user(uid)})


@auto_apply_bp.route("/api/users/application-answers/<question_id>", methods=["DELETE"])
@require_firebase_auth
def remove_saved_answer(question_id):
    uid = request.firebase_user["uid"]
    return jsonify({"deleted": bool(delete_answer(uid, question_id))})


@auto_apply_bp.route("/api/users/application-profile", methods=["POST"])
@require_firebase_auth
def write_application_profile():
    uid = request.firebase_user["uid"]
    payload = request.get_json(silent=True) or {}
    incoming = payload.get("profile") if isinstance(payload, dict) else None
    if not isinstance(incoming, dict):
        return jsonify({"error": "profile object required"}), 400
    saved = save_application_profile(uid, incoming)
    return jsonify({
        "profile": saved,
        "acknowledged": is_acknowledged(saved),
        "work_auth_complete": work_auth_complete(saved),
    })


# =============================================================================
# Auto-apply: Pro / Elite only
# =============================================================================

def _load_job(job_id: str) -> Dict[str, Any]:
    """Load a job from the global jobs/ collection."""
    db = get_db()
    snap = db.collection("jobs").document(str(job_id)).get()
    return (snap.to_dict() or {}) if snap.exists else {}


@auto_apply_bp.route("/api/job-board/auto-apply/prepare", methods=["POST"])
@require_firebase_auth
@require_tier(["free", "pro", "elite"])
def prepare_auto_apply():
    """Validate eligibility, load profile, return a preview payload the client
    renders in the review modal. Does NOT deduct credits — that happens on
    submit.

    Returns 409 PROFILE_REQUIRED if the user has not acknowledged their
    Application Profile yet, so the client can render the profile modal first.
    """
    uid = request.firebase_user["uid"]
    payload = request.get_json(silent=True) or {}
    job_id = payload.get("job_id")
    if not job_id:
        return jsonify({"error": "job_id required"}), 400

    job = _load_job(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    if not is_eligible(job):
        return jsonify({"error": "job is not auto-apply eligible", "code": "INELIGIBLE"}), 400

    profile = get_application_profile(uid)
    if not is_acknowledged(profile):
        return jsonify({
            "error": "application profile required",
            "code": "PROFILE_REQUIRED",
        }), 409
    if not work_auth_complete(profile):
        return jsonify({
            "error": "work authorization required in profile",
            "code": "WORK_AUTH_REQUIRED",
        }), 409

    try:
        user = load_user_for_apply(uid)
        user["applicationProfile"] = profile  # already loaded; avoid double-read
        preview = build_preview(job, user)
        preview_complete = True
    except Exception as exc:
        # Don't blow up on data-shape surprises — return a degraded preview
        # (empty identity + answers + LLM) so the user can still see the modal
        # and edit. Logged so we can spot recurring shapes that need defensive
        # coverage in build_preview.
        logger.exception("preview generation failed for uid=%s job=%s: %s", uid, job_id, exc)
        from app.services.auto_apply.preview import build_resume_descriptor
        preview = {
            "fields": {"first_name": "", "last_name": "", "full_name": "",
                       "email": "", "phone": "", "location": "",
                       "linkedin_url": "", "github_url": "", "portfolio_url": ""},
            "structured_answers": {
                "authorized_to_work_us": None, "requires_sponsorship": None,
                "visa_status": None, "gender": "decline", "race": "decline",
                "ethnicity": "decline", "lgbtq": "decline",
                "veteran_status": "decline", "disability_status": "decline",
                "earliest_start_date": None, "expected_salary_usd": None,
                "open_to_relocation": None, "open_to_remote": None,
            },
            "open_ended_answers": {},
            "resume": {"has_resume": False, "filename": "resume.pdf"},
            "unmapped_fields": [],
        }
        preview_complete = False

    return jsonify({
        "job_id": str(job_id),
        "ats_platform": detect_platform(job),
        "preview": preview,
        "preview_complete": preview_complete,
        "job": {
            "title": job.get("title"),
            "company": job.get("company"),
            "apply_url": job.get("apply_url"),
        },
    })


@auto_apply_bp.route("/api/job-board/auto-apply/<job_id>/submit", methods=["POST"])
@require_firebase_auth
@require_tier(["free", "pro", "elite"])
def submit_auto_apply(job_id: str):
    """Spawn the form-filler in a background thread, return an auto_apply_id
    the client polls for status. Body:
      { dry_run: bool, edited_answers: { why_role: str, why_company: str } }
    Dry-run runs the filler but does NOT click Submit — useful for verifying
    selectors and previewing the filled state with a screenshot.

    The body of this handler now lives in auto_apply.submit_service so Scout's
    auto_apply_to_job tool runs the SAME eligibility, profile, dedupe, worker
    and credit path the website's Apply button runs. Two copies would drift,
    and the copy Scout could not reach was the one that never existed."""
    uid = request.firebase_user["uid"]
    payload = request.get_json(silent=True) or {}
    result, status = submit_auto_apply_for_user(
        uid,
        str(job_id),
        dry_run=bool(payload.get("dry_run", True)),
        edited_answers=payload.get("edited_answers") or {},
    )
    return jsonify(result), status


@auto_apply_bp.route(
    "/api/job-board/auto-apply/<auto_apply_id>/status", methods=["GET"]
)
@require_firebase_auth
@require_tier(["free", "pro", "elite"])
def auto_apply_status(auto_apply_id: str):
    """Return the current autoApplyJobs doc so the modal can poll."""
    uid = request.firebase_user["uid"]
    snap = (
        get_db()
        .collection("users")
        .document(uid)
        .collection("autoApplyJobs")
        .document(auto_apply_id)
        .get()
    )
    if not snap.exists:
        return jsonify({"error": "not found", "code": "NOT_FOUND"}), 404
    # Reap on read: a job whose worker died (deploy/OOM/crash) would otherwise
    # poll as "running" forever. See _reap_if_stale.
    return jsonify(_reap_if_stale(uid, snap.to_dict() or {}))


# =============================================================================
# Needs Attention queue + answer-library resolution
# =============================================================================


@auto_apply_bp.route(
    "/api/job-board/auto-apply/<auto_apply_id>/resolve", methods=["POST"]
)
@require_firebase_auth
@require_tier(["free", "pro", "elite"])
def resolve_needs_attention(auto_apply_id: str):
    """User answered some / all of the pending questions in the Needs Attention
    drawer. Body: { answers: { question_id: value, ... } }.

    Behavior:
      - Each answer is saved to the per-user applicationAnswerLibrary so the
        same question on the next job auto-fills.
      - Pending questions whose question_id is in `answers` are cleared from
        the autoApplyJobs doc.
      - If any required pending question remains, status stays
        "needs_attention" and the doc reflects the partial progress.
      - If all required pending questions are now answered, we re-spawn the
        background worker which re-runs the filler. On the second run the
        library hits make the filler resolve every field, and the actual
        submission fires.

    Credits are NOT touched here — they were deducted at the original Submit
    click and stay deducted through needs_attention.
    """
    uid = request.firebase_user["uid"]
    payload = request.get_json(silent=True) or {}
    incoming = payload.get("answers")
    if not isinstance(incoming, dict) or not incoming:
        return jsonify({"error": "answers object required", "code": "BAD_REQUEST"}), 400

    db = get_db()
    job_ref = (
        db.collection("users").document(uid)
        .collection("autoApplyJobs").document(auto_apply_id)
    )
    snap = job_ref.get()
    if not snap.exists:
        return jsonify({"error": "not found", "code": "NOT_FOUND"}), 404
    data = snap.to_dict() or {}

    if data.get("status") != "needs_attention":
        return jsonify({
            "error": f"job is in status {data.get('status')!r}, not needs_attention",
            "code": "NOT_PAUSED",
        }), 409

    pending = data.get("pending_questions") or []
    by_id = {p.get("field_id") or p.get("question_id"): p for p in pending}

    # Write each provided answer to the library. Synchronous (per
    # feedback_sync_event_writes.md — durability beats latency for the queue).
    saved_ids = []
    for qid, value in incoming.items():
        question = by_id.get(qid)
        if not question:
            continue  # Unknown question_id — silently skip
        # Pending questions carry their text under "question" (the drawer's
        # shape); older shapes used "label". Reading only "label" saved every
        # answer with EMPTY question text, so the library could never match
        # it to the same question on the next job: saved, but useless.
        label = question.get("label") or question.get("question") or ""
        field_type = question.get("field_type") or "text"
        options = question.get("options")
        save_answer(
            uid=uid,
            question_text=label,
            answer=value,
            field_type=field_type,
            options=options,
            source="user_answered",
        )
        saved_ids.append(qid)

    # Recompute remaining pending: anything still required AND not in saved_ids
    remaining = [
        q for q in pending
        if (q.get("field_id") or q.get("question_id")) not in saved_ids
    ]
    required_remaining = [q for q in remaining if q.get("required") is True]

    update_payload: Dict[str, Any] = {
        "pending_questions": remaining,
        "pending_resolved_at": datetime.utcnow().isoformat(),
    }

    if required_remaining:
        # Partial resolution — stay in needs_attention until the user finishes.
        update_payload["status"] = "needs_attention"
        job_ref.update(update_payload)
        return jsonify({
            "auto_apply_id": auto_apply_id,
            "status": "needs_attention",
            "pending_questions": remaining,
            "saved_to_library": saved_ids,
        }), 200

    # Loop breaker (2026-08-30, the Snowflake checkbox): if two answer-and-
    # retry rounds have already failed on the SAME form, a third ask is a
    # treadmill, not progress — the form itself is defeating the filler
    # (checkbox click timeouts, custom widgets). Stop honestly: demote to
    # apply-on-the-site and refund, instead of re-asking questions the user
    # already answered twice.
    _rounds = int(data.get("attention_rounds") or 0)
    if _rounds >= 2:
        now_iso = datetime.utcnow().isoformat()
        fail_payload: Dict[str, Any] = {
            "status": "submit_failed",
            "stage": "failed",
            "failure_reason": (
                "This company's form keeps blocking our filler even with your "
                "answers. Your answers were used each try; finishing it on the "
                "company site is the honest next step."
            ),
            "failed_at": now_iso,
            "updated_at": now_iso,
            "pending_questions": [],
        }
        try:
            charged = int(data.get("credits_charged") or 0)
            if charged and not data.get("credits_refunded") and not data.get("dry_run"):
                refund_credits_atomic(uid, charged, "auto_apply_refund")
                fail_payload["credits_refunded"] = True
        except Exception:
            logger.exception("loop-breaker refund failed for %s", auto_apply_id)
        job_ref.update(fail_payload)
        return jsonify({
            "auto_apply_id": auto_apply_id,
            "status": "submit_failed",
            "pending_questions": [],
            "saved_to_library": saved_ids,
        }), 200

    # All required pending questions resolved → re-run the worker. The library
    # is now populated, so the next filler pass will resolve every field and
    # actually click Submit.
    update_payload["status"] = "queued"
    update_payload["stage"] = "queued_for_resume"
    update_payload["attention_rounds"] = _rounds + 1
    # Clear the submit-attempt stamp: the USER has just answered the drawer and
    # explicitly asked us to try again, so another submission is authorized.
    #
    # This is what separates a legitimate retry from a duplicate. The RQ worker
    # refuses to run a job whose submit_attempted_at is set, because a requeue
    # after a mid-apply worker restart would otherwise re-submit the application
    # (Discord, 2026-07-14). A requeue carries no new user input and never clears
    # this; a drawer retry does. Without clearing it here, the guard would block
    # the very flow it's meant to protect — the user answers the question, and
    # nothing happens.
    from firebase_admin import firestore as _fs
    update_payload["submit_attempted_at"] = _fs.DELETE_FIELD
    job_ref.update(update_payload)

    job_id = data.get("job_id")
    dry_run = bool(data.get("dry_run"))

    # Pass the user's drawer answers to the resume worker as edited_answers.
    # Without this, sensitive-slot answers (work auth, demographics) that
    # the library refuses to save get silently dropped, and the worker
    # re-runs with the same resolver state — leading to the "asked twice"
    # drawer loop. edited_answers wins over library+profile+LLM inside the
    # filler so user input always trumps stale stored values.
    drawer_answers = {qid: incoming[qid] for qid in saved_ids if qid in incoming}
    # Also include answers that didn't save to the library (sensitive slots)
    # so they at least flow through to this resume run, even if they won't
    # persist to the next job.
    for qid, value in incoming.items():
        if qid not in drawer_answers and by_id.get(qid):
            drawer_answers[qid] = value

    # Same rule as submit: the browser only ever starts in the RQ worker.
    enqueue(
        "run_auto_apply",
        auto_apply_id=auto_apply_id,
        uid=uid,
        job_id=str(job_id),
        dry_run=dry_run,
        edited_answers=drawer_answers,
    )

    return jsonify({
        "auto_apply_id": auto_apply_id,
        "status": "queued",
        "pending_questions": [],
        "saved_to_library": saved_ids,
    }), 200


@auto_apply_bp.route(
    "/api/job-board/auto-apply/needs-attention", methods=["GET"]
)
@require_firebase_auth
@require_tier(["free", "pro", "elite"])
def list_needs_attention():
    """Return all autoApplyJobs docs for this user with status=needs_attention.
    Used by the Needs Attention tab in the job board."""
    uid = request.firebase_user["uid"]
    docs = (
        get_db().collection("users").document(uid)
        .collection("autoApplyJobs")
        .where("status", "==", "needs_attention")
        .stream()
    )
    items = [d.to_dict() or {} for d in docs]
    items.sort(key=lambda x: x.get("inspect_completed_at") or x.get("created_at") or "", reverse=True)
    return jsonify({"items": items, "count": len(items)})


# =============================================================================
# Needs Verification queue + "finish in browser" resolution
# =============================================================================

@auto_apply_bp.route(
    "/api/job-board/auto-apply/needs-verification", methods=["GET"]
)
@require_firebase_auth
@require_tier(["free", "pro", "elite"])
def list_needs_verification():
    """Return all autoApplyJobs docs for this user with status=needs_verification.

    These are jobs where the filler completed the form fill but the ATS
    (Greenhouse / Lever / Ashby) ships CAPTCHA that would reject our
    headless-browser submission. The user finishes the submit themselves
    in their own browser; reCAPTCHA / hCaptcha scores their real-device
    session as human.

    Each item carries `prepared_answers` (what we filled), `captcha`
    (vendor + sitekey), and `apply_url` (where to go). The frontend
    surfaces these in the Auto-Submission tab as "Finish in browser"
    cards."""
    uid = request.firebase_user["uid"]
    docs = (
        get_db().collection("users").document(uid)
        .collection("autoApplyJobs")
        .where("status", "==", "needs_verification")
        .stream()
    )
    items = [d.to_dict() or {} for d in docs]
    items.sort(
        key=lambda x: x.get("inspect_completed_at") or x.get("created_at") or "",
        reverse=True,
    )
    return jsonify({"items": items, "count": len(items)})


@auto_apply_bp.route(
    "/api/job-board/auto-apply/<auto_apply_id>/mark-submitted", methods=["POST"]
)
@require_firebase_auth
@require_tier(["free", "pro", "elite"])
def mark_submitted(auto_apply_id: str):
    """The user finished a needs_verification job in their own browser and
    is confirming they hit Submit. We transition the job to `submitted`
    and stamp `completed_at` so it leaves the verification queue and shows
    up in the regular "submitted" list. Credits stay deducted — the user
    got value from the pre-fill.

    Returns 409 if the job isn't in needs_verification (no-op on stale
    clicks)."""
    uid = request.firebase_user["uid"]
    db = get_db()
    job_ref = (
        db.collection("users").document(uid)
        .collection("autoApplyJobs").document(auto_apply_id)
    )
    snap = job_ref.get()
    if not snap.exists:
        return jsonify({"error": "not found", "code": "NOT_FOUND"}), 404
    data = snap.to_dict() or {}

    if data.get("status") != "needs_verification":
        return jsonify({
            "error": f"job is in status {data.get('status')!r}, "
                     "not needs_verification",
            "code": "NOT_PAUSED",
        }), 409

    now = datetime.utcnow().isoformat()
    job_ref.update({
        "status": "submitted",
        "completed_at": now,
        "user_marked_submitted_at": now,
    })
    return jsonify({
        "auto_apply_id": auto_apply_id,
        "status": "submitted",
    }), 200


@auto_apply_bp.route(
    "/api/job-board/auto-apply/list", methods=["GET"]
)
@require_firebase_auth
@require_tier(["free", "pro", "elite"])
def list_auto_apply_jobs():
    """Return autoApplyJobs for this user, optionally filtered by status.
    Used by the Auto-Submission tab: in-flight + submitted + failed mixed.

    Query params:
      ?status=queued,running,submitted,submit_failed,failed,needs_attention
        (comma-separated)
      ?limit=N  (default 100, max 200)
    """
    uid = request.firebase_user["uid"]
    status_filter = request.args.get("status")
    statuses = [s.strip() for s in status_filter.split(",")] if status_filter else None
    try:
        limit = min(int(request.args.get("limit") or 100), 200)
    except ValueError:
        limit = 100

    # LIST fields only — the union of what the web Auto-Submission tab and the
    # mobile Network tab actually render. The full docs carry screenshot_b64,
    # attempt_log, filled_summary, etc., which ballooned this response to ~5MB
    # per poll and helped OOM the staging box (2026-07-07). The Firestore
    # `select` projection means the heavy fields are never even read; detail
    # views keep using GET /auto-apply/<id>/status, which returns everything.
    _LIST_FIELDS = [
        "auto_apply_id",
        "job_id",
        "job_title",
        "company",
        "ats_platform",
        "apply_url",
        "dry_run",
        "status",
        "stage",
        "failure_reason",
        "submit_evidence",
        "applicant_email",
        "pending_questions",
        "credits_charged",
        "credits_refunded",
        "created_at",
        "updated_at",
        "inspect_completed_at",
        "completed_at",
    ]

    collection = (
        get_db().collection("users").document(uid).collection("autoApplyJobs")
    )
    if statuses:
        # Firestore `in` queries cap at 10 values — auto-apply has only 6
        # statuses today so we're fine.
        query = collection.where("status", "in", statuses)
    else:
        query = collection
    try:
        docs = query.select(_LIST_FIELDS).stream()
        items = [d.to_dict() or {} for d in docs]
    except Exception:
        # Projection failed (unexpected) — fall back to full docs but strip
        # the known heavy fields so the response stays list-sized.
        _HEAVY = {"screenshot_b64", "attempt_log", "filled_summary", "unmapped", "attempted_urls"}
        docs = query.stream()
        items = [
            {k: v for k, v in (d.to_dict() or {}).items() if k not in _HEAVY}
            for d in docs
        ]

    # Reap on read here too — the Applications tab polls this even when no
    # detail modal is open, so it's often the first thing to notice a job whose
    # worker died. Cheap: only touches docs already stuck in queued/running.
    items = [_reap_if_stale(uid, it) for it in items]
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return jsonify({"items": items[:limit], "count": len(items)})
