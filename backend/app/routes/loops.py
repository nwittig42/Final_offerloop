"""
Loop routes — /api/agent/loops/*

Multi-Loop endpoints layered on top of the existing /api/agent/* singleton
routes. Existing routes keep working during the frontend migration.

Tier gating: any Loop endpoint requires a non-free tier OR free-tier users
within their max_loops cap (1). The decorator chain checks the cap inside
create_loop, so we don't need a tier guard on read endpoints — those are
already cheap.

Prefix: /api/agent/loops
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request

from app.extensions import get_db, require_firebase_auth
from app.services.agent_brief_parser import parse_brief
from app.services.loop_budget import (
    estimate_cycle_cost,
    usage_breakdown_this_month,
)
from app.services.loop_fleet_summary import (
    get_fleet_feed,
    get_fleet_weekly_summary,
    get_found_counts_by_loop,
    get_loop_live_stats,
    get_suggested_loops,
)
from app.services.loop_service import (
    LOOP_AUTO_SEND_MODES,
    LOOP_MODES,
    create_loop,
    delete_loop,
    get_loop,
    get_loop_activity,
    get_loop_limits,
    list_loops,
    pause_loop,
    resume_loop,
    run_loop_now,
    start_loop,
    update_loop,
)


def _validate_auto_send_fields(data: dict) -> tuple[str, str] | None:
    """Validate the Phase 9 auto-send fields on a POST/PATCH body.

    Returns (error_code, error_message) on failure, or None when the body
    is acceptable. Caller decides the HTTP status (400 vs 403).

    Rules:
      - autoSendMode must be one of LOOP_AUTO_SEND_MODES.
      - autoSendApprovedAfter must be an int in [0, 50] when present.
        0 means "no warmup gate" (the shipping default — send from cycle 1).
      - hardDailySendCap must be a non-negative int <= 200, or null.
      - autoSendApprovedCount is server-managed; reject if present.
    """
    if "autoSendApprovedCount" in data:
        return (
            "autoSendApprovedCount_read_only",
            "autoSendApprovedCount is bumped only by the approve-send "
            "endpoint. Manual updates would let users bypass the first-N "
            "gate.",
        )

    if "autoSendMode" in data and data["autoSendMode"] not in LOOP_AUTO_SEND_MODES:
        return (
            "invalid_autoSendMode",
            f"autoSendMode must be one of {sorted(LOOP_AUTO_SEND_MODES)}.",
        )

    if "autoSendApprovedAfter" in data:
        v = data["autoSendApprovedAfter"]
        if not isinstance(v, int) or isinstance(v, bool) or v < 0 or v > 50:
            return (
                "invalid_autoSendApprovedAfter",
                "autoSendApprovedAfter must be an integer between 0 and 50. "
                "0 means no warmup gate.",
            )

    if "hardDailySendCap" in data:
        v = data["hardDailySendCap"]
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool) or v < 0 or v > 200:
                return (
                    "invalid_hardDailySendCap",
                    "hardDailySendCap must be an integer between 0 and 200, or null.",
                )

    return None

logger = logging.getLogger(__name__)

loops_bp = Blueprint("loops", __name__, url_prefix="/api/agent/loops")


def _user_tier() -> str:
    """Fetch the authenticated user's tier from Firestore.

    These routes don't use @require_tier (Loops are available to all tiers,
    just gated by max_loops), so we look it up ourselves. Reads
    subscriptionTier first, falls back to legacy tier, then free.
    """
    cached = getattr(request, "user_tier", None)
    if cached:
        return cached
    uid = request.firebase_user.get("uid")
    if not uid:
        return "free"
    db = get_db()
    if not db:
        return "free"
    try:
        doc = db.collection("users").document(uid).get()
        if not doc.exists:
            return "free"
        data = doc.to_dict() or {}
        tier = data.get("subscriptionTier") or data.get("tier") or "free"
        request.user_tier = tier
        return tier
    except Exception:
        logger.exception("Failed to fetch tier for uid=%s", uid)
        return "free"


@loops_bp.route("", methods=["GET"])
@require_firebase_auth
def list_user_loops():
    uid = request.firebase_user["uid"]
    loops = list_loops(uid)
    # Attach per-Loop found counts (this week + all-time) so each LoopCard can
    # show weekly progress against its target AND cumulative progress — the
    # latter keeps the Monday weekly-reset from reading like nothing happened.
    # One contacts scan for the whole fleet, bucketed by loopId.
    found_counts = get_found_counts_by_loop(uid)
    for lp in loops:
        c = found_counts.get(lp.get("id", ""), {})
        lp["weekContactsFound"] = c.get("week", 0)
        lp["liveContactsFound"] = c.get("all", 0)
        lp["liveDraftsWaiting"] = c.get("drafts", 0)
    return jsonify({"loops": loops, "limits": get_loop_limits(uid, _user_tier())})


@loops_bp.route("", methods=["POST"])
@require_firebase_auth
def create_user_loop():
    """Create a Loop. If the body contains briefText, parses it in-line so the
    response includes the parsed structure (no second round-trip)."""
    uid = request.firebase_user["uid"]
    data = request.get_json() or {}

    # Validate loopMode if provided. Missing falls through to the service
    # default ("people") to preserve compatibility with old clients.
    if "loopMode" in data and data["loopMode"] not in LOOP_MODES:
        return jsonify({
            "error": "invalid_loopMode",
            "message": f"loopMode must be one of {sorted(LOOP_MODES)}.",
        }), 400

    # Phase 9 — validate auto-send fields (autoSendMode / autoSendApprovedAfter /
    # hardDailySendCap). Rejects autoSendApprovedCount in the body.
    err = _validate_auto_send_fields(data)
    if err:
        return jsonify({"error": err[0], "message": err[1]}), 400

    brief_text = (data.get("briefText") or "").strip()
    # Only re-parse server-side when the client didn't supply a usable parse.
    # The Loop composer parses on the frontend, lets the user edit chips, and
    # sends the curated result here as `briefParsed`. Treat null/empty as
    # "please parse for me" so old clients and short briefs still work.
    client_parsed = data.get("briefParsed")
    client_parsed_useful = isinstance(client_parsed, dict) and any(
        client_parsed.get(k)
        for k in ("companies", "industries", "roles", "locations", "constraints", "emailPurpose")
    )
    if brief_text and not client_parsed_useful:
        parsed, _status = parse_brief(brief_text)
        data["briefParsed"] = parsed

    try:
        loop = create_loop(uid, _user_tier(), data)
    except ValueError as e:
        msg = str(e)
        if msg == "tier_cap_reached":
            limits = get_loop_limits(uid, _user_tier())
            return jsonify({
                "error": "tier_cap_reached",
                "message": (
                    f"You're at your plan's limit of {limits['cap']} Loop"
                    f"{'s' if limits['cap'] != 1 else ''}. "
                    "Upgrade to add more."
                ),
                "limits": limits,
            }), 402
        return jsonify({"error": msg}), 400

    # Auto-start the Loop immediately. Idle-on-create was a vestige of
    # the old "review before deploy" wizard; the V2 wizard collects the
    # brief, the cadence (from tier default), and the approval mode in
    # one shot, so there's nothing left for the student to confirm. If
    # start fails (e.g. brief_required, though we just wrote one), the
    # creation still succeeds but we surface the failure on the response
    # so the wizard can show "Saved but didn't start" instead of a
    # cheerful "Deployed!" toast (S2.4 in the loops audit).
    auto_start_error = None
    try:
        started = start_loop(
            uid, loop["id"], app=current_app._get_current_object(),
        )
        if started:
            loop = started
    except Exception as start_err:
        logger.exception(
            "POST /loops: auto-start failed for uid=%s loop=%s",
            uid, loop.get("id"),
        )
        auto_start_error = type(start_err).__name__

    response_body = dict(loop)
    if auto_start_error:
        # Distinct from `error` (which would imply the whole create failed).
        # Wizard reads autoStartError and downgrades the success toast.
        response_body["autoStartError"] = auto_start_error
        response_body["autoStartMessage"] = (
            "Loop saved, but the first run didn't start. "
            "Tap Run it now from the fleet view."
        )
    return jsonify(response_body), 201


@loops_bp.route("/<loop_id>", methods=["GET"])
@require_firebase_auth
def get_user_loop(loop_id):
    uid = request.firebase_user["uid"]
    loop = get_loop(uid, loop_id)
    if not loop:
        return jsonify({"error": "not_found"}), 404
    # Live Found/Emailed/Replied from actual contact records — the detail-page
    # funnel reads this instead of the drift-prone totalContactsFound counters.
    loop["liveStats"] = get_loop_live_stats(uid, loop_id)
    return jsonify(loop)


@loops_bp.route("/<loop_id>", methods=["PATCH"])
@require_firebase_auth
def patch_user_loop(loop_id):
    uid = request.firebase_user["uid"]
    data = request.get_json() or {}

    # Mode is set at creation and cannot be changed afterward. Changing the
    # direction of a running Loop would invalidate its cached companies, jobs,
    # and HMs, and confuse the user about already-drafted work.
    if "loopMode" in data:
        return jsonify({
            "error": "loopMode_read_only",
            "message": "Loop mode is fixed at creation. Create a new Loop to change direction.",
        }), 400

    # Phase 9 — validate auto-send fields (autoSendMode / autoSendApprovedAfter /
    # hardDailySendCap). Rejects autoSendApprovedCount.
    err = _validate_auto_send_fields(data)
    if err:
        return jsonify({"error": err[0], "message": err[1]}), 400

    # If only briefText was sent, re-parse it before saving.
    if "briefText" in data and "briefParsed" not in data:
        parsed, _status = parse_brief(data["briefText"])
        data["briefParsed"] = parsed

    loop = update_loop(uid, loop_id, data, tier=_user_tier())
    if not loop:
        return jsonify({"error": "not_found"}), 404
    return jsonify(loop)


@loops_bp.route("/<loop_id>/contacts/<contact_id>/approve-send", methods=["POST"])
@require_firebase_auth
def approve_contact_send(loop_id, contact_id):
    """Phase 9 — manually approve auto-send for one contact.

    Sends the previously-drafted email from the student's Gmail and
    atomically bumps the Loop's autoSendApprovedCount. If a power-user
    has set autoSendApprovedAfter > 0 on this Loop, the first N
    approvals act as a warmup gate before background auto-send unlocks.
    The shipping default is 0 (no warmup) — in that case this endpoint
    is just a per-contact "send anyway" override after a different gate
    (Hunter, daily cap, etc.) denied.

    The send still runs the full gate (tier, Gmail-connected, quiet hours,
    daily cap, Hunter verification) — the only check that gets skipped is
    first_n_pending, because manual approval is precisely the override
    for that gate.

    Returns:
        200 { ok, messageId, autoSendApprovedCount, autoSendApprovedAfter,
              firstNSatisfied } on send.
        4xx with gate reason when a gate check denies.
    """
    from datetime import datetime, timezone

    from firebase_admin import firestore as _fs

    from app.services.agent_send_gate import can_auto_send
    from app.services.auth import increment_sends_today_atomic
    from app.services.gmail_client import send_email_for_user

    uid = request.firebase_user["uid"]
    tier = _user_tier()
    db = get_db()

    # 1. Load Loop and verify ownership.
    loop_ref = (
        db.collection("users").document(uid)
          .collection("loops").document(loop_id)
    )
    loop_snap = loop_ref.get()
    if not loop_snap.exists:
        return jsonify({"error": "loop_not_found"}), 404
    loop = loop_snap.to_dict() or {}

    # 2. Load contact and verify it belongs to THIS Loop. Without this
    #    cross-check a student could approve-send any of their contacts
    #    through any of their Loops, and the wrong autoSendApprovedCount
    #    would tick.
    contact_ref = (
        db.collection("users").document(uid)
          .collection("contacts").document(contact_id)
    )
    contact_snap = contact_ref.get()
    if not contact_snap.exists:
        return jsonify({"error": "contact_not_found"}), 404
    contact = contact_snap.to_dict() or {}
    if contact.get("loopId") and contact.get("loopId") != loop_id:
        return jsonify({"error": "contact_not_in_loop"}), 403

    email = (contact.get("email") or "").strip()
    if not email:
        return jsonify({"error": "no_email"}), 400

    email_subject = contact.get("emailSubject") or ""
    email_body = contact.get("emailBody") or ""
    if not email_body.strip():
        return jsonify({
            "error": "no_draft",
            "message": "This contact has no drafted email to send.",
        }), 400

    # 3. Load user state for the gate (timezone).
    user_snap = db.collection("users").document(uid).get()
    user_data = (user_snap.to_dict() or {}) if user_snap.exists else {}
    user_tz = user_data.get("timezone") or user_data.get("tz")

    # 4. Build a synthetic loop view that force-passes the first-N gate.
    #    The whole point of this endpoint is to manually override that
    #    specific check; all other gates (tier, gmail-connected, quiet
    #    hours, daily cap, Hunter verify) still apply.
    # `is not None` (not `or`) — autoSendApprovedAfter=0 is the no-warmup
    # default; `loop.get(k, 5) or 5` would silently turn 0 into 5.
    raw_after = loop.get("autoSendApprovedAfter")
    forced_count = int(raw_after) if raw_after is not None else 0
    loop_for_gate = {
        **loop,
        "autoSendApprovedCount": forced_count,
    }

    gate = can_auto_send(
        uid=uid,
        tier=tier,
        loop=loop_for_gate,
        contact={
            "email": email,
            "emailVerifiedAt": contact.get("emailVerifiedAt"),
            "emailVerificationStatus": contact.get("emailVerificationStatus"),
        },
        user_timezone=user_tz,
    )

    # Persist verification cache for the contact even on denial so we
    # don't re-pay Hunter on the next attempt.
    contact_update: dict = {}
    verification = gate.get("verification")
    if verification:
        contact_update["emailVerifiedAt"] = verification.get("verifiedAt")
        contact_update["emailVerificationStatus"] = verification.get("status")

    if not gate["allowed"]:
        contact_update["autoSendPausedReason"] = gate.get("reason") or "unknown"
        if gate.get("effective_cap") is not None:
            contact_update["autoSendDailyCap"] = gate["effective_cap"]
        if contact_update:
            contact_ref.update(contact_update)
        # 422 Unprocessable Entity — request well-formed but business rule denied.
        return jsonify({
            "error": "gate_denied",
            "reason": gate.get("reason"),
            "effective_cap": gate.get("effective_cap"),
        }), 422

    # 5. Atomic daily-cap reservation. Same race-safe pattern as the
    #    background path; if a parallel auto-send won the race, we
    #    surface daily_cap.
    reserved, _count, effective_cap = increment_sends_today_atomic(
        uid, tier, hard_cap=loop.get("hardDailySendCap"),
    )
    if not reserved:
        contact_update["autoSendPausedReason"] = "daily_cap"
        contact_update["autoSendDailyCap"] = effective_cap
        contact_ref.update(contact_update)
        return jsonify({
            "error": "gate_denied",
            "reason": "daily_cap",
            "effective_cap": effective_cap,
        }), 422

    # 6. Send via student's Gmail.
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        send_result = send_email_for_user(
            uid,
            to=email,
            subject=email_subject,
            body_html=email_body,
            tracking_kind="outreach",
        )
    except Exception as e:
        logger.warning(
            "approve_send_failed uid=%s loop=%s contact=%s err=%s",
            uid, loop_id, contact_id, e,
        )
        contact_update["autoSendError"] = str(e)
        contact_update["autoSendPausedReason"] = "send_error"
        contact_ref.update(contact_update)
        # 502 Bad Gateway — upstream (Gmail) failed; not the client's fault.
        return jsonify({"error": "send_failed", "message": str(e)}), 502

    # 7. Stamp success on the contact + clear any prior pause/error flags.
    contact_update.update({
        "gmailMessageId": send_result.get("id", ""),
        "gmailThreadId": send_result.get("threadId", ""),
        "emailSentAt": now_iso,
        "pipelineStage": "email_sent",
        "inOutbox": True,
        "autoSendPausedReason": _fs.DELETE_FIELD,
        "autoSendError": _fs.DELETE_FIELD,
    })
    contact_ref.update(contact_update)

    # 8. Atomically bump the Loop's first-N counter. Increment lets two
    #    parallel approve-send calls both stick without read-modify-write
    #    racing the count backwards.
    loop_ref.update({"autoSendApprovedCount": _fs.Increment(1)})

    # `is not None` — see note on the loop_for_gate block above.
    raw_count = loop.get("autoSendApprovedCount")
    raw_after = loop.get("autoSendApprovedAfter")
    new_count = (int(raw_count) if raw_count is not None else 0) + 1
    approved_after = int(raw_after) if raw_after is not None else 0
    return jsonify({
        "ok": True,
        "messageId": send_result.get("id", ""),
        "autoSendApprovedCount": new_count,
        "autoSendApprovedAfter": approved_after,
        "firstNSatisfied": new_count >= approved_after,
    })


@loops_bp.route("/<loop_id>", methods=["DELETE"])
@require_firebase_auth
def delete_user_loop(loop_id):
    uid = request.firebase_user["uid"]
    ok = delete_loop(uid, loop_id)
    if not ok:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"ok": True})


@loops_bp.route("/<loop_id>/start", methods=["POST"])
@require_firebase_auth
def start_user_loop(loop_id):
    uid = request.firebase_user["uid"]
    try:
        loop = start_loop(uid, loop_id, app=current_app._get_current_object())
    except ValueError as e:
        msg = str(e)
        if msg == "brief_required":
            return jsonify({
                "error": "brief_required",
                "message": "Add a brief before starting this Loop.",
            }), 400
        return jsonify({"error": msg}), 400
    if not loop:
        return jsonify({"error": "not_found"}), 404
    return jsonify(loop)


@loops_bp.route("/estimate", methods=["POST"])
@require_firebase_auth
def estimate_loop_cost():
    """Estimate per-cycle and per-month credits for a hypothetical Loop.

    Body: { briefParsed: {...}, cadence: 'daily'|'every_other_day'|'weekly' }
    Returns: { per_cycle_credits, monthly_credits, cycles_per_month, breakdown }

    Used by the Loop creation hero to show a live cost preview as the user
    types their brief.
    """
    data = request.get_json() or {}
    brief_parsed = data.get("briefParsed") or {}
    cadence = data.get("cadence") or "every_other_day"
    raw_mode = (data.get("loopMode") or "people")
    loop_mode = raw_mode if raw_mode in LOOP_MODES else "people"
    return jsonify(estimate_cycle_cost(brief_parsed, cadence, loop_mode=loop_mode))


@loops_bp.route("/usage-breakdown", methods=["GET"])
@require_firebase_auth
def get_usage_breakdown():
    """Where the user's credits went this month, grouped by source.

    Powers the Account Settings "Where my credits went" panel.
    """
    uid = request.firebase_user["uid"]
    return jsonify(usage_breakdown_this_month(uid))


@loops_bp.route("/<loop_id>/mark-reviewed", methods=["POST"])
@require_firebase_auth
def mark_loop_reviewed(loop_id):
    """Stamp lastReviewedAt=now on the Loop. Called by LoopDetailPage when
    the activity feed renders, so the inactivity-pause gate sees the user
    is engaged.
    """
    from datetime import datetime, timezone
    uid = request.firebase_user["uid"]
    db = get_db()
    ref = (
        db.collection("users").document(uid)
          .collection("loops").document(loop_id)
    )
    if not ref.get().exists:
        return jsonify({"error": "not_found"}), 404
    ref.update({"lastReviewedAt": datetime.now(timezone.utc).isoformat()})
    return jsonify({"ok": True})


@loops_bp.route("/<loop_id>/activity", methods=["GET"])
@require_firebase_auth
def get_user_loop_activity(loop_id):
    """Per-Loop activity feed — every find, newest first."""
    uid = request.firebase_user["uid"]
    limit = request.args.get("limit", 50, type=int)
    items = get_loop_activity(uid, loop_id, limit=min(limit, 100))
    return jsonify({"items": items})


@loops_bp.route("/<loop_id>/run-now", methods=["POST"])
@require_firebase_auth
def run_user_loop_now(loop_id):
    uid = request.firebase_user["uid"]
    loop = run_loop_now(uid, loop_id, app=current_app._get_current_object())
    if not loop:
        return jsonify({"error": "not_found"}), 404
    return jsonify(loop)


@loops_bp.route("/<loop_id>/pause", methods=["POST"])
@require_firebase_auth
def pause_user_loop(loop_id):
    uid = request.firebase_user["uid"]
    loop = pause_loop(uid, loop_id)
    if not loop:
        return jsonify({"error": "not_found"}), 404
    return jsonify(loop)


@loops_bp.route("/<loop_id>/resume", methods=["POST"])
@require_firebase_auth
def resume_user_loop(loop_id):
    uid = request.firebase_user["uid"]
    loop = resume_loop(uid, loop_id, app=current_app._get_current_object())
    if not loop:
        return jsonify({"error": "not_found"}), 404
    return jsonify(loop)


# ── Fleet-level rollups for the LoopsCommandBar ────────────────────────────


@loops_bp.route("/weekly-summary", methods=["GET"])
@require_firebase_auth
def fleet_weekly_summary():
    """Fleet-wide weekly aggregate for the LoopsCommandBar.

    Returns foundThisWeek, a 7-day sparkline, draftsWaiting, weeklyGoal
    (sum of per-Loop weeklyTarget), the goal-ring percentage, and the count
    of active Loops.
    """
    uid = request.firebase_user["uid"]
    return jsonify(get_fleet_weekly_summary(uid))


@loops_bp.route("/feed", methods=["GET"])
@require_firebase_auth
def fleet_feed():
    """Newest finds across every Loop, flattened for the ticker.

    `limit` query param caps the row count (default 20, max 50).
    """
    uid = request.firebase_user["uid"]
    limit = request.args.get("limit", 20, type=int)
    items = get_fleet_feed(uid, limit=min(max(limit, 1), 50))
    return jsonify({"items": items})


@loops_bp.route("/suggested", methods=["GET"])
@require_firebase_auth
def suggested_loops():
    """Quickstart Loop templates for the NewLoopTile one-tap suggestions.

    v1: static curated set, lightly personalized by the user's school. Each
    item carries a pre-seeded brief the /agent/setup page can read to skip
    the cold-start composer.
    """
    uid = request.firebase_user["uid"]
    items = get_suggested_loops(uid)
    return jsonify({"items": items})
