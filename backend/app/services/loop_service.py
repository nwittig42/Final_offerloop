"""
Loop Service — multi-Loop layer on top of the existing agent infrastructure.

A Loop is one autonomous search the user kicked off with a natural-language
brief. Users can have multiple Loops running in parallel (subject to their
tier's max_loops cap). Each Loop runs cycles, drafts emails, and produces
its own SMS notification.

Data model:
    users/{uid}/loops/{loopId}
        id, name, briefText, briefParsed,
        reviewBeforeSend, weeklyTarget, smsEnabled,
        status (idle | running | paused | done),
        createdAt, lastRunAt, nextRunAt,
        totalContactsFound, totalEmailsDrafted, totalJobsFound, ...

Cycles/actions/jobs/companies stay at users/{uid}/agent_cycles, etc., but get
a loopId field so we can filter by Loop. Old documents without a loopId belong
to the "default" Loop created during migration.

This module is the source of truth for everything user-visible called a "Loop."
The legacy agent_service is now treated as plumbing — it knows how to execute
one cycle, but it doesn't decide which Loop the cycle belongs to.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

from app.config import TIER_CONFIGS
from app.extensions import get_db
from app.models.pipeline_stages import AWAITING_REPLY_STAGES
from app.services.agent_service import (
    DEFAULT_AGENT_CONFIG,
    _generate_short_code,
)
from app.services.loop_budget import (
    AUTO_SEND_CREDIT_COST,
    BUNDLED_BUDGET_BUFFER,
    BUNDLED_COST_PER_PERSON,
)
from app.services.tier_defaults import weekly_target_for_tier

logger = logging.getLogger(__name__)

# Fields the client is allowed to write. Counters and timestamps are managed
# server-side and rejected if posted by the client.
MUTABLE_LOOP_FIELDS = {
    "name",
    "briefText",
    "briefParsed",
    "reviewBeforeSend",
    "weeklyTarget",
    "smsEnabled",
    # Phase 8 — automation + budget
    "cadence",
    "creditBudgetPerWeek",
    "automationEnabled",
    # Phase 9 — auto-send. autoSendApprovedCount is server-managed (bumped
    # by the approve-send endpoint), so it's NOT in this set.
    "autoSendMode",
    "autoSendApprovedAfter",
    "hardDailySendCap",
}

LOOP_STATUS = {"idle", "running", "paused", "done"}

# Phase 9 — auto-send mode. Picks the point on the autonomy spectrum:
#   "approve_each" — cycles run on cadence, but each planner action queues as
#                    pending_approval. No credits spent until user approves.
#                    (Replaces today's reviewBeforeSend=True semantics, but
#                    with the bug fixed: the cycle actually runs.)
#   "draft_only"   — cycles run, AI finds + drafts, Gmail draft created. User
#                    sends manually from Gmail. (Today's "Autopilot".)
#   "send_for_me"  — cycles run, AI finds + drafts + verifies + sends from the
#                    student's own Gmail. Gated by Hunter verification, quiet
#                    hours, first-N approval, daily cap.
LOOP_AUTO_SEND_MODES = {"approve_each", "draft_only", "send_for_me"}

# Phase 8 — Loop cadence options. User picks at creation; default in
# _loop_defaults below is every_other_day.
LOOP_CADENCE = {"daily", "every_other_day", "weekly", "manual"}

# Loop modes. Read-only after creation: changing direction mid-flight would
# invalidate cached companies/jobs/HMs and confuse users about already-drafted
# work. To change direction, create a new Loop.
#   "people" — autonomous networking (find professionals, draft cold outreach).
#             Today's behavior. Default for old Loops missing the field.
#   "roles"  — autonomous job-search (find open postings, optionally draft
#             founder outreach about specific roles).
#   "both"   — pursue BOTH pipelines in one Loop. Planner balances networking
#             actions and job-search actions against a single credit budget.
#             HM outreach in this mode is template-selected by provenance
#             (role_search → founder voice, networking → people voice).
LOOP_MODES = {"people", "roles", "both"}


def cadence_delta_hours(cadence: str) -> int | None:
    """Hours between scheduled cycles for a given cadence. None means manual
    (no scheduled runs — only fires when the user clicks Run it now)."""
    return {
        "daily": 24,
        "every_other_day": 48,
        "weekly": 24 * 7,
        "manual": None,
    }.get(cadence, 48)


def _loop_defaults() -> dict:
    """Default shape for a freshly-created Loop. Counters start at zero."""
    return {
        "name": "Untitled Loop",
        "briefText": "",
        "briefParsed": None,
        # Append-only log of prior briefText/briefParsed snapshots whenever
        # the user PATCHes the brief. Capped at 20 entries (update_loop).
        # Surfaced in the LoopDetailPage edit-brief affordance + useful as
        # tuning data for the parser later.
        "briefVersionHistory": [],
        "reviewBeforeSend": True,
        "weeklyTarget": 5,
        "smsEnabled": False,
        "status": "idle",
        "shortCode": _generate_short_code(),
        "createdAt": None,
        "lastRunAt": None,
        "nextRunAt": None,
        "lastSmsAt": None,
        "totalContactsFound": 0,
        "totalEmailsDrafted": 0,
        "totalRepliesReceived": 0,
        "totalJobsFound": 0,
        "totalHmsContacted": 0,
        "totalCompaniesDiscovered": 0,
        "pendingDrafts": 0,
        "unreadReplies": 0,
        # Phase 8 — automation + budget. Tier defaults override
        # creditBudgetPerWeek at create_loop time.
        "cadence": "every_other_day",
        "creditBudgetPerWeek": 200,
        "automationEnabled": True,
        "lastReviewedAt": None,
        "weekCreditsSpent": 0,
        "weekStartedAt": None,
        "pauseReason": None,
        # Default mirrors the V2 wizard's hardcoded `loopMode: "both"` —
        # every Loop pursues networking + job-search against one budget.
        # Pre-V2 paths that omit loopMode used to silently get "people"
        # (S5.1 in the loops audit); now they get the same behavior as
        # the wizard, which is the actual product default.
        "loopMode": "both",
        # Phase 9 — auto-send. Default "draft_only" matches today's
        # "Autopilot" behavior (cycle runs, Gmail draft created, no send).
        # Flipping to "send_for_me" activates the send_gate in agent_actions.
        # autoSendApprovedAfter=0 means no warmup gate — if you picked
        # "Send for me", the loop sends from cycle 1. Power users can PATCH
        # to a positive int to require N manual approvals first. The gate
        # logic in agent_send_gate still honors a non-zero value.
        # hardDailySendCap=None means "use tier cap".
        "autoSendMode": "draft_only",
        "autoSendApprovedCount": 0,
        "autoSendApprovedAfter": 0,
        "hardDailySendCap": None,
        # Phase 9.1 — per-Loop concurrency lock. Prevents two parallel
        # cycle runs (manual "Run it now" + scheduler tick, double-click,
        # daemon overlap) from re-doing the same find actions and sending
        # the same email twice. Held by loop_jobs.run_loop_cycle_job for
        # the duration of one cycle; released on success and on exception.
        # Stale recovery in try_claim_cycle_lock catches crashed runs.
        # Server-managed only — NOT in MUTABLE_LOOP_FIELDS.
        "cycleRunning": False,
        "cycleStartedAt": None,
    }


# ── Phase 9.1: per-Loop concurrency lock ─────────────────────────────────
#
# Why: two parallel run_loop_cycle_job invocations on the same Loop both
# call the planner, both execute the same find actions (PDL returns the
# same top-ranked contact for a given company+title+location), and both
# fire auto_send. End result: same recipient gets 2 nearly-identical
# emails 10 seconds apart, plus credits charged twice.
#
# The lock is a simple `cycleRunning: bool` + `cycleStartedAt: iso str`
# on the Loop doc, claimed atomically inside a Firestore transaction.
# Stale recovery: if cycleStartedAt is more than STALE_LOCK_AFTER_MINUTES
# in the past, the previous holder almost certainly crashed without
# releasing — we reclaim instead of stalling forever.

STALE_LOCK_AFTER_MINUTES = 30


def try_claim_cycle_lock(
    uid: str,
    loop_id: str,
    now: datetime | None = None,
) -> bool:
    """Atomically claim the per-Loop cycle lock.

    Returns:
        True  — caller now holds the lock; must call release_cycle_lock
                on every exit path (success and exception).
        False — another cycle is already running (or the Loop was deleted).
                Caller should return early WITHOUT releasing.
    """
    from firebase_admin import firestore as _fs

    db = get_db()
    now = now or datetime.now(timezone.utc)
    loop_ref = (
        db.collection("users").document(uid)
          .collection("loops").document(loop_id)
    )

    @_fs.transactional
    def claim_in_txn(transaction):
        snap = loop_ref.get(transaction=transaction)
        if not snap.exists:
            return False  # Loop deleted between enqueue and run.
        data = snap.to_dict() or {}
        currently_running = bool(data.get("cycleRunning"))

        if currently_running:
            started_at = data.get("cycleStartedAt")
            if started_at:
                try:
                    started = datetime.fromisoformat(
                        started_at.replace("Z", "+00:00")
                    )
                    age = now - started
                    if age < timedelta(minutes=STALE_LOCK_AFTER_MINUTES):
                        return False  # Held by a healthy parallel cycle.
                    logger.warning(
                        "loop=%s cycle lock stale (%.1f min) — reclaiming",
                        loop_id, age.total_seconds() / 60,
                    )
                    # Fall through to reclaim.
                except (ValueError, AttributeError):
                    # Garbled timestamp — treat as stale and reclaim.
                    logger.warning(
                        "loop=%s cycleStartedAt unparseable (%r) — reclaiming",
                        loop_id, started_at,
                    )
            # cycleRunning=True but no timestamp at all = also stale.

        transaction.update(loop_ref, {
            "cycleRunning": True,
            "cycleStartedAt": now.isoformat(),
        })
        return True

    try:
        return claim_in_txn(db.transaction())
    except Exception:
        logger.exception(
            "try_claim_cycle_lock failed uid=%s loop=%s", uid, loop_id,
        )
        return False


def release_cycle_lock(uid: str, loop_id: str) -> None:
    """Release the per-Loop cycle lock. Best-effort — if the Loop was
    deleted mid-cycle, the write fails silently and is logged at INFO."""
    db = get_db()
    loop_ref = (
        db.collection("users").document(uid)
          .collection("loops").document(loop_id)
    )
    try:
        loop_ref.update({
            "cycleRunning": False,
            "cycleStartedAt": None,
        })
    except Exception as e:
        logger.info(
            "release_cycle_lock: loop=%s update failed (likely deleted): %s",
            loop_id, e,
        )


def _max_loops_for_tier(tier: str) -> int:
    """Look up the per-tier Loop cap. Defaults to 1 for unknown tiers."""
    cfg = TIER_CONFIGS.get(tier) or TIER_CONFIGS.get("free", {})
    return int(cfg.get("max_loops", 1))


def _auto_name_from_brief(brief_parsed: dict | None, brief_text: str) -> str:
    """Generate a short, human-readable name from the parsed brief.

    Examples:
        roles=["Analyst"], industries=["Investment Banking"]
            -> "Analyst roles in Investment Banking"
        companies=["Stripe","Linear"]
            -> "Roles at Stripe, Linear"
        empty brief
            -> first 40 chars of briefText, or "Untitled Loop"
    """
    if isinstance(brief_parsed, dict):
        roles = brief_parsed.get("roles") or []
        companies = brief_parsed.get("companies") or []
        industries = brief_parsed.get("industries") or []
        if roles and companies:
            return f"{roles[0]} at {', '.join(companies[:2])}"
        if roles and industries:
            return f"{roles[0]} roles in {industries[0]}"
        if companies:
            return f"Roles at {', '.join(companies[:2])}"
        if roles:
            return f"{roles[0]} roles"
        if industries:
            return industries[0]

    snippet = (brief_text or "").strip().splitlines()[0] if brief_text else ""
    if snippet:
        return snippet[:40] + ("…" if len(snippet) > 40 else "")
    return "Untitled Loop"


# ── CRUD ────────────────────────────────────────────────────────────────────

def list_loops(uid: str) -> list[dict]:
    """Return all Loops for the user, newest first.

    Triggers a one-time migration from the legacy agent_config singleton on
    first call: if the user has no loops/ docs but does have a stored
    agent_config, create a default Loop from it.
    """
    db = get_db()
    coll = db.collection("users").document(uid).collection("loops")
    docs = [{"id": d.id, **d.to_dict()} for d in coll.stream()]

    if not docs:
        migrated = _migrate_legacy_config(uid)
        if migrated:
            docs = [migrated]

    docs.sort(key=lambda d: d.get("createdAt") or "", reverse=True)
    return docs


def get_loop(uid: str, loop_id: str) -> dict | None:
    db = get_db()
    doc = (
        db.collection("users").document(uid)
          .collection("loops").document(loop_id).get()
    )
    if not doc.exists:
        return None
    return {"id": doc.id, **doc.to_dict()}


def create_loop(uid: str, tier: str, payload: dict) -> dict:
    """Create a new Loop after checking the tier cap.

    Raises:
        ValueError("tier_cap_reached") — user is at their tier's max_loops.
    """
    db = get_db()
    existing = list_loops(uid)
    cap = _max_loops_for_tier(tier)
    if len(existing) >= cap:
        raise ValueError("tier_cap_reached")

    now = datetime.now(timezone.utc).isoformat()
    loop_id = str(uuid.uuid4())

    # Filter to mutable fields, then layer on top of defaults.
    payload_clean = {k: v for k, v in (payload or {}).items() if k in MUTABLE_LOOP_FIELDS}

    # Phase 8: derive the weekly budget from the user's people/week target
    # (output-first wizard pattern) unless the client explicitly supplied one.
    # Always enforce the tier max and a 25-credit floor.
    tier_cfg = TIER_CONFIGS.get(tier) or TIER_CONFIGS["free"]
    max_budget = tier_cfg.get("max_credit_budget_per_week_per_loop")  # None = unbounded
    raw_mode_for_budget = (payload or {}).get("loopMode")
    loop_mode_for_budget = (
        raw_mode_for_budget if raw_mode_for_budget in BUNDLED_COST_PER_PERSON else "people"
    )

    if "creditBudgetPerWeek" in payload_clean:
        # Client supplied an explicit cap (Settings → "Hard weekly credit cap").
        # Trust it, but still clamp to tier max + 25-credit floor.
        budget = int(payload_clean["creditBudgetPerWeek"])
    else:
        # Wizard V2 hides cadence from the user — when weeklyTarget is missing
        # we derive it, in priority order:
        #   1. An explicit count in the brief ("10 analysts at jpmorgan" → 10).
        #      The brief parser already extracts this as targetCount; without
        #      this branch it was parsed then ignored, so every Loop silently
        #      paced at the tier default no matter what number the user wrote.
        #   2. The tier default (free 2 / pro 5 / elite 10).
        # A brief-supplied count is capped to what the tier's weekly budget can
        # actually fund, so the displayed pace never promises more than the
        # Loop can pay for — and free/paid tiering still holds via that cap.
        if not payload_clean.get("weeklyTarget"):
            bundled_cost = BUNDLED_COST_PER_PERSON[loop_mode_for_budget]
            brief_count = ((payload or {}).get("briefParsed") or {}).get("targetCount")
            if isinstance(brief_count, int) and brief_count > 0:
                target = brief_count
                if max_budget is not None:
                    affordable = max(
                        1, int(max_budget // (bundled_cost * BUNDLED_BUDGET_BUFFER))
                    )
                    target = min(target, affordable)
                payload_clean["weeklyTarget"] = target
            else:
                payload_clean["weeklyTarget"] = weekly_target_for_tier(tier)
        weekly = int(payload_clean["weeklyTarget"])
        bundled = BUNDLED_COST_PER_PERSON[loop_mode_for_budget]
        budget = int(weekly * bundled * BUNDLED_BUDGET_BUFFER)
        # Phase 9 — if the Loop is opting into auto-send, the per-person
        # cost gains a +1 credit overhead (Hunter verify). Add this on
        # top so the wizard's derived budget covers the new line item.
        if payload_clean.get("autoSendMode") == "send_for_me":
            budget += int(weekly * AUTO_SEND_CREDIT_COST * BUNDLED_BUDGET_BUFFER)

    if max_budget is not None:
        budget = min(budget, max_budget)
    payload_clean["creditBudgetPerWeek"] = max(budget, 25)

    # Validate cadence
    if payload_clean.get("cadence") and payload_clean["cadence"] not in LOOP_CADENCE:
        payload_clean["cadence"] = "every_other_day"

    # loopMode is create-only — handled outside MUTABLE_LOOP_FIELDS so it can
    # never be changed by a PATCH. Validate enum; fall through to default on
    # missing or invalid (preserves today's networking behavior).
    raw_mode = (payload or {}).get("loopMode")
    if raw_mode in LOOP_MODES:
        payload_clean["loopMode"] = raw_mode

    # Derive autoSendMode from the wizard's reviewBeforeSend pick if the
    # client didn't supply it explicitly. Without this, Autopilot Loops
    # (reviewBeforeSend=False) still default to "draft_only" and never
    # actually send — which was S5.3 in the loops audit. Explicit
    # autoSendMode (Settings power-user surface) still wins.
    if "autoSendMode" not in payload_clean:
        if payload_clean.get("reviewBeforeSend") is False:
            payload_clean["autoSendMode"] = "send_for_me"
        # else: keep _loop_defaults' "draft_only"

    doc = {
        **_loop_defaults(),
        **payload_clean,
        "createdAt": now,
    }
    if not doc.get("name") or doc["name"] == "Untitled Loop":
        doc["name"] = _auto_name_from_brief(doc.get("briefParsed"), doc.get("briefText", ""))

    db.collection("users").document(uid) \
      .collection("loops").document(loop_id).set(doc)

    # A Loop names firms and roles the People deck ranks toward.
    _refresh_people_deck(uid, db)
    return {"id": loop_id, **doc}


def _weekly_target_and_budget(
    tier: str | None, desired_count: int, loop_mode: str | None, auto_send_mode: str | None
) -> tuple[int, int]:
    """Derive (weeklyTarget, creditBudgetPerWeek) for a brief-supplied count.

    Mirrors the budget math in create_loop: budget = count × per-person ×
    buffer (+ auto-send overhead), capped by the tier's weekly max and floored
    at 25. The count itself is first capped to what that max can fund, so the
    displayed pace never outruns the budget.
    """
    tier_cfg = TIER_CONFIGS.get(tier or "free") or TIER_CONFIGS["free"]
    max_budget = tier_cfg.get("max_credit_budget_per_week_per_loop")  # None = unbounded
    mode = loop_mode if loop_mode in BUNDLED_COST_PER_PERSON else "people"
    bundled = BUNDLED_COST_PER_PERSON[mode]

    count = max(1, int(desired_count))
    if max_budget is not None:
        affordable = max(1, int(max_budget // (bundled * BUNDLED_BUDGET_BUFFER)))
        count = min(count, affordable)

    budget = int(count * bundled * BUNDLED_BUDGET_BUFFER)
    if auto_send_mode == "send_for_me":
        budget += int(count * AUTO_SEND_CREDIT_COST * BUNDLED_BUDGET_BUFFER)
    if max_budget is not None:
        budget = min(budget, max_budget)
    return count, max(budget, 25)


def update_loop(
    uid: str, loop_id: str, patch: dict, tier: str | None = None
) -> dict | None:
    db = get_db()
    ref = (
        db.collection("users").document(uid)
          .collection("loops").document(loop_id)
    )
    doc = ref.get()
    if not doc.exists:
        return None

    filtered = {k: v for k, v in (patch or {}).items() if k in MUTABLE_LOOP_FIELDS}
    # If the brief changed and the name was auto-generated, refresh the name.
    # Also snapshot the previous brief into briefVersionHistory so the user
    # can see how their goal has evolved (and we can tune the parser).
    if "briefParsed" in filtered or "briefText" in filtered:
        current = doc.to_dict() or {}
        if not patch.get("name"):
            new_name = _auto_name_from_brief(
                filtered.get("briefParsed", current.get("briefParsed")),
                filtered.get("briefText", current.get("briefText", "")),
            )
            filtered["name"] = new_name

        # Re-pace from the brief when it carries an explicit count ("10 analysts
        # at jpmorgan" → 10/week) and the caller didn't set weeklyTarget by hand.
        # Only fires on an explicit number, so a target someone tuned manually is
        # never clobbered. Budget is recomputed to match the new pace so the two
        # never drift. Mirrors the create_loop derivation.
        if "weeklyTarget" not in filtered:
            new_parsed = filtered.get("briefParsed") or current.get("briefParsed") or {}
            brief_count = new_parsed.get("targetCount")
            if isinstance(brief_count, int) and brief_count > 0:
                wt, budget = _weekly_target_and_budget(
                    tier,
                    brief_count,
                    current.get("loopMode"),
                    current.get("autoSendMode"),
                )
                filtered["weeklyTarget"] = wt
                filtered["creditBudgetPerWeek"] = budget

        # Append a version-history entry only when briefText actually changed.
        # A PATCH that touches briefParsed alone (without changing briefText)
        # is a backfill, not a user edit — don't pollute the history with it.
        old_text = current.get("briefText") or ""
        new_text = filtered.get("briefText", old_text) or ""
        if "briefText" in filtered and new_text != old_text:
            history_entry = {
                "briefText": old_text,
                "briefParsed": current.get("briefParsed") or {},
                "editedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            prev_history = list(current.get("briefVersionHistory") or [])
            prev_history.append(history_entry)
            # Cap to the last 20 versions. We don't write a TTL — old entries
            # fall off the end as new edits come in. 20 × ~2KB ≈ 40KB,
            # comfortably under Firestore's 1MB doc cap.
            filtered["briefVersionHistory"] = prev_history[-20:]
            # Purge brief-dependent caches (agent_companies, agent_jobs) so
            # the next cycle re-discovers against the new brief. HM cache
            # stays — founder identity doesn't change with brief edits.
            from app.services.agent_actions import purge_brief_dependent_caches
            purge_brief_dependent_caches(db, uid, loop_id)

    if filtered:
        ref.update(filtered)
        if {"briefParsed", "briefText", "status"} & set(filtered):
            _refresh_people_deck(uid, db)
    return get_loop(uid, loop_id)


def delete_loop(uid: str, loop_id: str) -> bool:
    """Hard-delete a Loop. Cycles/actions/contacts created under it stay; they
    just become orphaned by loopId. This keeps the Tracker history intact for
    contacts the user already reached out to."""
    db = get_db()
    ref = (
        db.collection("users").document(uid)
          .collection("loops").document(loop_id)
    )
    if not ref.get().exists:
        return False
    ref.delete()
    _refresh_people_deck(uid, db)
    return True


def _refresh_people_deck(uid: str, db) -> None:
    """Rebuild the student's saved People deck in the background. Never
    raises: a Loop save must not fail over a deck refresh."""
    try:
        from app.services.people_deck_cache import mark_stale
        mark_stale(uid, db, reason="loop")
    except Exception:
        logger.debug("people deck refresh failed uid=%s", uid)


# ── Lifecycle ───────────────────────────────────────────────────────────────

def start_loop(uid: str, loop_id: str, app=None) -> dict | None:
    """Mark a Loop as running and trigger its first cycle immediately.

    The cycle runs in a background thread so the API call returns fast.
    Phase 2 (RQ worker) will replace this thread with a durable job queue;
    until then this still works on Render but dies if the worker is recycled
    mid-cycle. The Loop's status stays 'running' so the user can retry.

    Returns the updated Loop doc, or None if the Loop doesn't exist.
    """
    db = get_db()
    ref = (
        db.collection("users").document(uid)
          .collection("loops").document(loop_id)
    )
    doc = ref.get()
    if not doc.exists:
        return None

    loop = doc.to_dict() or {}
    if not loop.get("briefText") and not (loop.get("briefParsed") or {}).get("companies"):
        raise ValueError("brief_required")

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()

    # Phase 8: next scheduled run is driven by cadence, not a hardcoded 24h.
    # `manual` Loops never have a nextRunAt — they only fire on Run it now.
    cadence = (loop or {}).get("cadence", "every_other_day")
    hours = cadence_delta_hours(cadence)
    next_run = (now_dt + timedelta(hours=hours)).isoformat() if hours else None

    ref.update({
        "status": "running",
        "lastRunAt": now,
        "nextRunAt": next_run,
        "startedAt": now,
        "pauseReason": None,
    })

    trigger_loop_cycle(uid, loop_id, app)
    return get_loop(uid, loop_id)


def trigger_loop_cycle(uid: str, loop_id: str, app=None) -> str | None:
    """Enqueue one cycle for this Loop.

    In production (REDIS_URL set), this hands the cycle to a separate RQ
    worker process — the cycle survives Gunicorn worker recycling, which
    was the silent-failure mode the threading.Thread version had.

    In dev (no REDIS_URL), the rq_queue module falls back to running the
    job in a daemon thread on this process, so local dev keeps working
    without Redis installed.

    The `app` parameter is kept for backwards compatibility with callers
    that still pass current_app; it's unused now because the job function
    runs in a separate process with its own Firestore client.

    Returns the job id, or None if the Loop doesn't exist.
    """
    del app  # legacy param, no longer needed

    # Loops feature removed from UI — refuse to enqueue new cycles so manual
    # start/run-now/resume endpoints (still exposed by loops.py) don't burn
    # credits. Revert this block to bring Loops back.
    logger.info("trigger_loop_cycle: loops disabled globally, skipping loop=%s", loop_id)
    return None

    db = get_db()
    loop_ref = (
        db.collection("users").document(uid)
          .collection("loops").document(loop_id)
    )
    if not loop_ref.get().exists:
        return None

    from app.services.rq_queue import enqueue
    return enqueue("run_loop_cycle", uid=uid, loop_id=loop_id)


def _contact_to_draft_item(contact_id: str, c: dict) -> dict:
    """Build a 'draft' activity row directly from a contact doc.

    Used when the agent_actions feed has no result row for a draft the Loop
    actually created (the contacts collection is the source of truth). Mirrors
    the draft shape _action_to_items emits so the frontend renders it the same.
    """
    name = (f"{c.get('firstName', '')} {c.get('lastName', '')}").strip() or (
        c.get("name") or "Contact"
    )
    email = (c.get("email") or c.get("draftToEmail") or "").strip()
    draft_url = (c.get("gmailDraftUrl") or "").strip()
    if contact_id:
        link, external = f"/outbox?contact={contact_id}", False
    elif draft_url:
        link, external = draft_url, True
    else:
        link, external = "/outbox", False
    return {
        "id": f"contact-{contact_id}",
        "type": "draft",
        "title": c.get("emailSubject") or f"Draft to {name}",
        "subtitle": email or (c.get("emailBody") or "")[:120] or "—",
        "email": email,
        "linkTo": link,
        "external": external,
        "createdAt": c.get("createdAt") or c.get("draftCreatedAt") or "",
        "contactId": contact_id,
        "hasOutreach": True,
        "isHm": bool(c.get("isHiringManager")),
    }


def _contact_to_found_item(contact_id: str, c: dict) -> dict:
    """Build a 'contact' (found, not yet emailed) row from a contact doc.

    For a person the Loop surfaced but couldn't draft to — usually no usable /
    verified email. Without this they'd be counted in the funnel's "Found" but
    never appear in "Found, not yet emailed", so the user can't see or act on
    them (e.g. open their LinkedIn). hasOutreach=False routes it to that
    section instead of the drafts list.
    """
    name = (f"{c.get('firstName', '')} {c.get('lastName', '')}").strip() or (
        c.get("name") or "Someone"
    )
    role = c.get("jobTitle") or c.get("title") or ""
    company = c.get("company") or ""
    subtitle = ", ".join([s for s in [role, company] if s]) or "—"
    return {
        "id": f"found-{contact_id}",
        "type": "contact",
        "title": name,
        "subtitle": subtitle,
        "linkTo": _feed_contact_link(contact_id, False, False),
        "createdAt": c.get("createdAt") or "",
        "contactId": contact_id,
        "hasOutreach": False,
        "isHm": bool(c.get("isHiringManager")),
        "linkedinUrl": (c.get("linkedinUrl") or "").strip(),
    }


def get_loop_activity(uid: str, loop_id: str, limit: int = 50) -> list[dict]:
    """Build a chronological activity feed for one Loop.

    Joins through agent_cycles (filtered by loopId) → agent_actions (filtered
    by cycleId in that loop's cycle list) → per-action result rows. Each row
    is one *thing* the Loop found: a person, a job, a hiring manager, or a
    company. Items are sorted newest first.

    Shape per item:
        {
            "id": str,           # unique within the feed
            "type": "contact" | "job" | "hm" | "company" | "draft",
            "title": str,        # primary line (person name, job title, ...)
            "subtitle": str,     # secondary line (company, location, role, ...)
            "linkTo": str,       # frontend route to drill into this find
            "createdAt": str,    # ISO timestamp
        }
    """
    db = get_db()

    # Cycles for this Loop. The loopId field is stamped in trigger_loop_cycle.
    cycles_q = (
        db.collection("users").document(uid)
          .collection("agent_cycles")
          .where("loopId", "==", loop_id)
    )
    cycle_ids = [doc.id for doc in cycles_q.stream()]

    items: list[dict] = []

    # agent_actions hold the executed work. result contains the contacts/jobs
    # arrays returned by each action type. Firestore 'in' caps at 30 values,
    # so for users with more cycles we iterate in chunks.
    #
    # Two-pass: first collect every contact.sourceJobId across the Loop's
    # actions so we know which find_jobs items are paired with a founder
    # draft. Unpaired jobs (large-co Apply-only) get no groupKey; paired
    # ones get one so the frontend can render them as a hierarchy block.
    actions_col = db.collection("users").document(uid).collection("agent_actions")
    raw_actions: list[tuple[str, dict]] = []
    referenced_group_keys: set[str] = set()
    for chunk_start in range(0, len(cycle_ids), 30):
        chunk = cycle_ids[chunk_start:chunk_start + 30]
        for doc in actions_col.where("cycleId", "in", chunk).where("status", "==", "completed").stream():
            data = doc.to_dict() or {}
            raw_actions.append((doc.id, data))
            if data.get("action") == "find_hiring_managers":
                result = data.get("result") or {}
                if isinstance(result, dict):
                    for contact in result.get("contacts") or []:
                        if not isinstance(contact, dict):
                            continue
                        sjid = contact.get("sourceJobId") or ""
                        if sjid:
                            referenced_group_keys.add(sjid)

    for action_id, data in raw_actions:
        items.extend(_action_to_items(action_id, data, referenced_group_keys))

    # Source-of-truth pass: surface this Loop's contacts straight from the
    # contacts collection for any the agent_actions feed missed (cycles that
    # saved a contact but wrote no result row — e.g. Startups). Drafted ones
    # become draft rows; found-but-undrafted ones (no usable email) become
    # "found, not yet emailed" rows so they're visible/actionable instead of a
    # phantom in the "Found" count. Deduped by contactId against the feed.
    already = {it.get("contactId") for it in items if it.get("contactId")}
    try:
        contacts_ref = db.collection("users").document(uid).collection("contacts")
        for snap in contacts_ref.where("loopId", "==", loop_id).stream():
            c = snap.to_dict() or {}
            if c.get("source") != "agent" or snap.id in already:
                continue
            has_draft = bool(
                c.get("emailSubject")
                or c.get("emailBody")
                or c.get("pipelineStage") == "draft_created"
            )
            if has_draft:
                items.append(_contact_to_draft_item(snap.id, c))
            else:
                items.append(_contact_to_found_item(snap.id, c))
    except Exception:
        logger.exception(
            "get_loop_activity: contact merge failed for loop=%s", loop_id
        )

    # Sort newest first, cap.
    items.sort(key=lambda x: x.get("createdAt") or "", reverse=True)
    items = items[:limit]

    # Re-resolve contact/hm/draft items from the live contact doc. The
    # snapshot in agent_actions.result is frozen at write time, so it misses
    # anything the contact picked up later — Gmail send, thread sync, reply
    # detection. The contact subcollection is the source of truth, so we
    # batch-read it and overwrite the click target so clicking lands on the
    # current Gmail thread/draft for that person.
    resolvable_types = {"contact", "hm", "draft"}
    contact_ids = [it.get("_contactId") for it in items
                   if it.get("type") in resolvable_types and it.get("_contactId")]
    if contact_ids:
        contacts_ref = db.collection("users").document(uid).collection("contacts")
        refs = [contacts_ref.document(cid) for cid in set(contact_ids)]
        try:
            contact_map = {
                snap.id: (snap.to_dict() or {})
                for snap in db.get_all(refs)
                if snap.exists
            }
        except Exception:
            contact_map = {}

        # Backfill missing draft URLs for legacy contacts. Before the
        # agent_actions.py id/url → draft_id/draft_url fix, Loop-created
        # drafts were saved with empty gmailDraftUrl, so clicking fell
        # back to the tracker. We look those up in Gmail by recipient +
        # subject and write the URL back to the contact doc so future
        # clicks land on the exact draft.
        _backfill_draft_urls_for_contacts(uid, contact_map)
        # Then backfill thread IDs for contacts whose email was sent but
        # didn't carry a gmailThreadId — happens when the email left via
        # the tracker's manual send rather than the Loop's auto/approve
        # paths. Without a thread ID the Draft button falls back to a
        # compose URL; with one it lands on the exact thread.
        _backfill_thread_ids_for_contacts(uid, contact_map)

        for it in items:
            if it.get("type") not in resolvable_types:
                continue
            cid = it.pop("_contactId", None)
            if not cid:
                continue
            cdata = contact_map.get(cid)
            if not cdata:
                continue
            thread_id = (cdata.get("gmailThreadId") or "").strip()
            draft_url = (cdata.get("gmailDraftUrl") or "").strip()
            email = (cdata.get("email") or "").strip()
            # For draft rows without a real Gmail draft, rebuild the compose
            # URL now using the live contact doc's emailBody — `_action_to_items`
            # had to build it from the agent_actions snapshot, which only
            # carries a 200-char preview. Without this the compose opens with
            # an empty body even though we have the full text on file.
            if (
                it.get("type") == "draft"
                and not thread_id
                and not draft_url
                and email
            ):
                fallback_link = _gmail_compose_url(
                    to=email,
                    subject=(cdata.get("emailSubject") or it.get("emailSubject") or ""),
                    body=(cdata.get("emailBody") or ""),
                )
                fallback_external = True
            else:
                fallback_link = it.get("linkTo") or "/tracker"
                fallback_external = bool(it.get("external"))
            link, external = _gmail_link_for_contact(
                thread_id=thread_id,
                draft_url=draft_url,
                fallback=fallback_link,
                fallback_external=fallback_external,
            )
            it["linkTo"] = link
            it["external"] = external
            if email and it.get("type") == "draft":
                it["subtitle"] = email
                it["email"] = email
                # Per-row state for the drafts list. Replaces the old
                # hardcoded "SENT" badge with the contact's actual phase so
                # the user can scan replied / sent / drafted at a glance.
                # replyReceivedAt and emailSentAt may be strings or
                # Firestore timestamps depending on writer — truthy check
                # handles both shapes.
                if cdata.get("replyReceivedAt"):
                    it["state"] = "replied"
                elif thread_id or cdata.get("emailSentAt"):
                    it["state"] = "sent"
                else:
                    it["state"] = "drafted"
    else:
        for it in items:
            it.pop("_contactId", None)

    return items


def _backfill_draft_urls_for_contacts(uid: str, contact_map: dict) -> None:
    """Look up gmailDraftUrl in Gmail for contacts that are missing it.

    Mutates contact_map in place so the caller sees the freshly-stamped
    URLs in this same request. Also writes the URLs back to Firestore so
    subsequent polls skip the lookup. Idempotent: contacts where lookup
    already ran (whether or not a draft was found) carry a
    gmailDraftBackfilled flag and are skipped.
    """
    # Pick contacts that look like they SHOULD have a draft URL but don't.
    # An emailSubject means a draft was attempted; an email address is
    # required to search Gmail for it. Skip contacts already backfilled.
    needs_backfill = []
    for cid, cdata in contact_map.items():
        if not cdata:
            continue
        if cdata.get("gmailDraftBackfilled"):
            continue
        if (cdata.get("gmailDraftUrl") or "").strip():
            continue
        if (cdata.get("gmailThreadId") or "").strip():
            continue
        if not (cdata.get("emailSubject") or "").strip():
            continue
        if not (cdata.get("email") or "").strip():
            continue
        needs_backfill.append((cid, cdata))

    if not needs_backfill:
        return

    db = get_db()
    user_snap = db.collection("users").document(uid).get()
    user_data = (user_snap.to_dict() or {}) if user_snap.exists else {}
    user_email = (user_data.get("email") or "").strip()
    if not user_email:
        return

    from app.services.gmail_client import find_draft_for_recipient

    contacts_ref = db.collection("users").document(uid).collection("contacts")
    for cid, cdata in needs_backfill:
        match = find_draft_for_recipient(
            user_email=user_email,
            user_id=uid,
            recipient_email=cdata.get("email"),
            subject=cdata.get("emailSubject"),
        )
        update = {"gmailDraftBackfilled": True}
        if match:
            update["gmailDraftId"] = match.get("draft_id") or ""
            update["gmailDraftUrl"] = match.get("draft_url") or ""
            if match.get("thread_id") and not (cdata.get("gmailThreadId") or "").strip():
                update["gmailThreadId"] = match["thread_id"]
            # Mirror the update into the in-memory map so the same request
            # resolves linkTo without a re-read.
            cdata["gmailDraftId"] = update["gmailDraftId"]
            cdata["gmailDraftUrl"] = update["gmailDraftUrl"]
            if "gmailThreadId" in update:
                cdata["gmailThreadId"] = update["gmailThreadId"]
        cdata["gmailDraftBackfilled"] = True
        try:
            contacts_ref.document(cid).update(update)
        except Exception as e:
            logger.warning("Draft URL backfill write failed for %s: %s", cid, e)


def _backfill_thread_ids_for_contacts(uid: str, contact_map: dict) -> None:
    """Look up gmailThreadId in Gmail for contacts missing it.

    Contacts sent through the Loop's auto-send or approve-send paths
    already carry gmailThreadId. Contacts sent manually from the tracker
    (or via older code paths) may not — clicking the Draft button then
    falls back to the compose URL instead of the actual conversation.

    Mutates contact_map in place so the caller resolves linkTo to the
    real thread without a re-read; also writes the thread id back to
    Firestore so subsequent polls skip the lookup. Idempotent via the
    gmailThreadBackfilled flag.
    """
    needs_backfill = []
    for cid, cdata in contact_map.items():
        if not cdata:
            continue
        if cdata.get("gmailThreadBackfilled"):
            continue
        if (cdata.get("gmailThreadId") or "").strip():
            continue
        if not (cdata.get("email") or "").strip():
            continue
        # Only chase a thread when the email looks like it actually sent.
        # emailSentAt is the strongest signal; a sent/awaiting-reply
        # pipelineStage and inOutbox=True also indicate a send. Without either we don't
        # waste a Gmail call on a contact that never went out.
        if not (
            cdata.get("emailSentAt")
            or cdata.get("pipelineStage") in AWAITING_REPLY_STAGES
            or cdata.get("inOutbox")
        ):
            continue
        needs_backfill.append((cid, cdata))

    if not needs_backfill:
        return

    db = get_db()
    user_snap = db.collection("users").document(uid).get()
    user_data = (user_snap.to_dict() or {}) if user_snap.exists else {}
    user_email = (user_data.get("email") or "").strip()
    if not user_email:
        return

    from app.services.gmail_client import find_sent_thread_for_recipient

    contacts_ref = db.collection("users").document(uid).collection("contacts")
    for cid, cdata in needs_backfill:
        match = find_sent_thread_for_recipient(
            user_email=user_email,
            user_id=uid,
            recipient_email=cdata.get("email"),
            subject=cdata.get("emailSubject"),
        )
        update = {"gmailThreadBackfilled": True}
        if match and match.get("thread_id"):
            update["gmailThreadId"] = match["thread_id"]
            if match.get("message_id"):
                update["gmailMessageId"] = match["message_id"]
            cdata["gmailThreadId"] = update["gmailThreadId"]
        cdata["gmailThreadBackfilled"] = True
        try:
            contacts_ref.document(cid).update(update)
        except Exception as e:
            logger.warning("Thread id backfill write failed for %s: %s", cid, e)


# Names that almost certainly came from a scraper hitting a broken page.
# Filtered at the feed layer so users never see "404 – Page Not Found" as
# a company. The upstream discover_companies action should also be cleaned
# up, but until then this catches the common garbage strings.
_GARBAGE_NAME_TOKENS = (
    "404",
    "not found",
    "page not found",
    "access denied",
    "forbidden",
    "error 4",
    "error 5",
    "untitled",
)


def _looks_like_garbage(name: str) -> bool:
    if not name:
        return True
    lowered = name.strip().lower()
    if not lowered:
        return True
    return any(tok in lowered for tok in _GARBAGE_NAME_TOKENS)


def _gmail_link_for_contact(
    *,
    thread_id: str,
    draft_url: str,
    fallback: str,
    fallback_external: bool = False,
) -> tuple[str, bool]:
    """Best Gmail destination for a Loop draft row.

    Priority: live thread → captured draft URL (the exact compose URL we
    stamped on the contact at draft-creation time, same one the Find People
    spreadsheet links to). Falls back to the passed-in route when we have
    neither — its `fallback_external` flag is preserved so callers can pass
    a Gmail compose link (external=True) without `get_loop_activity`'s
    re-resolve flipping it back to an internal route.

    Used for draft rows only; non-draft contact rows stay in-app via
    `_feed_contact_link` so the user can see network context before
    jumping to Gmail.
    """
    if thread_id:
        return f"https://mail.google.com/mail/u/0/#inbox/{thread_id}", True
    if draft_url:
        return draft_url, True
    return fallback, fallback_external


def _gmail_compose_url(*, to: str, subject: str, body: str = "") -> str:
    """Gmail compose URL with `to`, `subject`, and `body` prefilled.

    Used when a Loop drafted an email but no actual Gmail draft was created
    (Gmail not connected, low-confidence email source, draft creation
    failed). The Draft button always landing in Gmail beats falling back to
    the tracker — the student opens a compose with everything already
    filled in so all that's left is one click to send.
    """
    parts = [
        "https://mail.google.com/mail/u/0/?view=cm&fs=1",
        f"&to={quote_plus(to)}",
        f"&su={quote_plus(subject or '')}",
    ]
    if body:
        parts.append(f"&body={quote_plus(body)}")
    return "".join(parts)


def _feed_contact_link(contact_id: str, is_hm: bool, has_outreach: bool) -> str:
    """In-app deep link for a Loop activity card (non-draft rows).

    Draft rows route to Gmail via _gmail_link_for_contact above. Other
    contact-shaped rows stay in-app so the user can see network context:
      HM            -> Find > Hiring Managers tab
      has outreach  -> tracker row (/outbox)
      bare person   -> My Network people row
    """
    if not contact_id:
        return "/outbox"
    if is_hm:
        return f"/find?tab=hiring-managers&contact={contact_id}"
    if has_outreach:
        return f"/outbox?contact={contact_id}"
    return f"/my-network/people?contact={contact_id}"


def _action_to_items(
    action_id: str,
    action: dict,
    referenced_group_keys: set[str] | None = None,
) -> list[dict]:
    """Expand one completed agent_action into 0..N activity feed items.

    referenced_group_keys: every contact.sourceJobId value present elsewhere
    in this Loop's actions. Used to decide which find_jobs items should
    emit a groupKey so the frontend can pair them with a founder draft.
    """
    result = action.get("result") or {}
    if not isinstance(result, dict):
        return []
    created_at = action.get("completedAt") or action.get("createdAt") or ""
    action_type = action.get("action")
    refs = referenced_group_keys or set()
    out: list[dict] = []

    # find / find_hiring_managers — produce contact rows
    if action_type in ("find", "find_hiring_managers"):
        contacts = result.get("contacts") or []
        is_hm = action_type == "find_hiring_managers"
        for i, c in enumerate(contacts):
            if not isinstance(c, dict):
                continue
            name = c.get("name") or c.get("fullName") or "Someone"
            if _looks_like_garbage(name):
                continue
            role = c.get("title") or c.get("role") or ""
            company = c.get("company") or action.get("company") or ""
            subtitle = ", ".join([s for s in [role, company] if s])
            contact_id = c.get("contactId") or c.get("id") or ""

            # Variables also consumed by the draft row below (which routes
            # to Gmail directly via _gmail_link_for_contact).
            contact_email = (c.get("email") or "").strip()
            thread_id = (c.get("gmailThreadId") or "").strip()
            draft_url = (c.get("gmailDraftUrl") or "").strip()
            # In-app deep link to the exact record (see _feed_contact_link):
            # a drafted/sent person points at their tracker row, a bare person
            # at their My Network row, an HM at the Find > Hiring Managers tab.
            # The draft row below uses _gmail_link_for_contact instead so its
            # Draft button lands in Gmail.
            person_has_outreach = bool(
                c.get("emailSubject") or c.get("emailBodyPreview") or thread_id
            )
            link = _feed_contact_link(contact_id, is_hm, person_has_outreach)
            external = False
            # role_search HMs carry a foreign key into the find_jobs item
            # they were paired with. Surface it on the activity items so the
            # feed can render the founder draft inline below its source
            # posting. Networking-mode HMs have no sourceJobId and render as
            # standalone rows (today's behavior).
            source_job_id = c.get("sourceJobId") or "" if is_hm else ""
            contact_item = {
                "id": f"{action_id}-c{i}",
                "type": "hm" if is_hm else "contact",
                "title": name,
                "subtitle": subtitle or "—",
                "linkTo": link,
                "external": external,
                "createdAt": created_at,
                # Internal: lets get_loop_activity re-resolve the live
                # contact doc so older items pick up a thread/draft URL
                # that was stamped after the action ran. Stripped before
                # the item leaves the backend.
                "_contactId": contact_id,
                # Explicit fields for the per-card action buttons (My Network /
                # Inbox / Find), so the frontend doesn't parse them out of linkTo.
                "contactId": contact_id,
                "hasOutreach": person_has_outreach,
                "isHm": is_hm,
            }
            if source_job_id:
                contact_item["groupKey"] = source_job_id
            out.append(contact_item)
            # If a draft was generated alongside, surface it as its own row.
            # Draft rows route to Gmail (not the tracker) — that's the whole
            # point of the Draft button on the frontend's EmailRow. Reuse the
            # contact's thread/draft URL when we have one, otherwise fall back
            # to a Gmail compose URL with to+subject prefilled so the student
            # can finish the message manually.
            if c.get("emailSubject") or c.get("emailBodyPreview"):
                if contact_email:
                    draft_compose_fallback = _gmail_compose_url(
                        to=contact_email,
                        subject=c.get("emailSubject") or "",
                    )
                    draft_link, draft_external = _gmail_link_for_contact(
                        thread_id=thread_id,
                        draft_url=draft_url,
                        fallback=draft_compose_fallback,
                        fallback_external=True,
                    )
                else:
                    draft_link, draft_external = link, external
                draft_item = {
                    "id": f"{action_id}-d{i}",
                    "type": "draft",
                    "title": name,
                    "subtitle": contact_email or (c.get("emailBodyPreview") or "")[:120],
                    "contactName": name,
                    "emailSubject": c.get("emailSubject") or "",
                    "email": contact_email,
                    "linkTo": draft_link,
                    "external": draft_external,
                    "createdAt": created_at,
                    # Explicit fields for the per-card action buttons.
                    "contactId": contact_id,
                    "hasOutreach": True,
                    "isHm": is_hm,
                    # Internal: re-resolution hook (stripped before the item
                    # leaves the backend; lets get_loop_activity overwrite
                    # the click target from the live contact doc).
                    "_contactId": contact_id,
                }
                if source_job_id:
                    draft_item["groupKey"] = source_job_id
                out.append(draft_item)

    # find_jobs — produce job rows. If the job has an apply link, route the
    # "View" click straight to it (external). Otherwise drop the user on the
    # job board filtered to this job.
    elif action_type == "find_jobs":
        jobs = result.get("jobs") or []
        for i, j in enumerate(jobs):
            if not isinstance(j, dict):
                continue
            title = j.get("title") or "Job"
            if _looks_like_garbage(title):
                continue
            company = j.get("company") or action.get("company") or ""
            location = j.get("location") or ""
            subtitle = " · ".join([s for s in [company, location] if s])
            apply_link = j.get("applyLink") or j.get("url") or ""
            job_id = j.get("id") or ""
            if apply_link:
                link, external = apply_link, True
            elif job_id:
                link, external = f"/job-board?job={job_id}", False
            else:
                link, external = "/job-board", False
            item_id = f"{action_id}-j{i}"
            job_item = {
                "id": item_id,
                "type": "job",
                "title": title,
                "subtitle": subtitle or "—",
                "linkTo": link,
                "external": external,
                "createdAt": created_at,
            }
            # Broadening telemetry → UI badge inputs. Absent on pre-PR docs.
            broaden_level = j.get("broadenLevel")
            if isinstance(broaden_level, int) and broaden_level > 0:
                job_item["broadenLevel"] = broaden_level
                original_role = j.get("originalRole") or ""
                target_company = j.get("targetCompany") or ""
                wider_location = j.get("widerLocation") or ""
                if original_role:
                    job_item["originalRole"] = original_role
                if target_company:
                    job_item["targetCompany"] = target_company
                if wider_location:
                    job_item["widerLocation"] = wider_location
            # Emit groupKey only when some founder-draft contact in this Loop
            # references this exact job item — pairs the job row with its
            # inline founder-draft sub-card. Unpaired large-co postings stay
            # ungrouped and render Apply-only.
            if item_id in refs:
                job_item["groupKey"] = item_id
            out.append(job_item)

    # discover_companies — produce company rows
    elif action_type == "discover_companies":
        companies = result.get("companies") or []
        for i, co in enumerate(companies):
            if not isinstance(co, dict):
                continue
            name = co.get("name") or "Company"
            if _looks_like_garbage(name):
                continue
            reason = co.get("reason") or co.get("industry") or ""
            out.append({
                "id": f"{action_id}-co{i}",
                "type": "company",
                "title": name,
                "subtitle": reason or "—",
                "linkTo": f"/find?tab=companies&q={name}",
                "createdAt": created_at,
            })

    return out


def run_loop_now(uid: str, loop_id: str, app=None) -> dict | None:
    """Manually trigger another cycle for a Loop already in 'running' state.

    Used by the "Run now" button on the detail page. Doesn't change status —
    if the Loop was running, it stays running; if it was paused, the caller
    should resume_loop instead.
    """
    db = get_db()
    ref = (
        db.collection("users").document(uid)
          .collection("loops").document(loop_id)
    )
    if not ref.get().exists:
        return None
    trigger_loop_cycle(uid, loop_id, app)
    return get_loop(uid, loop_id)


def pause_loop(uid: str, loop_id: str) -> dict | None:
    db = get_db()
    ref = (
        db.collection("users").document(uid)
          .collection("loops").document(loop_id)
    )
    if not ref.get().exists:
        return None
    ref.update({"status": "paused", "nextRunAt": None})
    return get_loop(uid, loop_id)


def resume_loop(uid: str, loop_id: str, app=None) -> dict | None:
    db = get_db()
    ref = (
        db.collection("users").document(uid)
          .collection("loops").document(loop_id)
    )
    doc = ref.get()
    if not doc.exists:
        return None
    loop = doc.to_dict() or {}
    # Phase 8: respect cadence on resume just like start.
    cadence = loop.get("cadence", "every_other_day")
    hours = cadence_delta_hours(cadence)
    next_run = (
        (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        if hours else None
    )
    ref.update({
        "status": "running",
        "nextRunAt": next_run,
        "pauseReason": None,
    })
    # Resume = the user wants something to happen now, not on the next tick.
    trigger_loop_cycle(uid, loop_id, app)
    return get_loop(uid, loop_id)


# ── Tier caps ───────────────────────────────────────────────────────────────

def get_loop_limits(uid: str, tier: str) -> dict:
    """Used by the frontend to decide whether to disable the '+ New Loop' tile."""
    existing = list_loops(uid)
    cap = _max_loops_for_tier(tier)
    return {
        "used": len(existing),
        "cap": cap,
        "canCreate": len(existing) < cap,
    }


# ── Legacy migration ────────────────────────────────────────────────────────

def _migrate_legacy_config(uid: str) -> dict | None:
    """Convert a legacy users/{uid}/settings/agent_config singleton into the
    user's first Loop. Returns the new Loop doc, or None if there was no
    legacy config to migrate.

    Runs at most once per user — after migration, the legacy config is marked
    with migratedToLoops=True so we don't re-migrate on every list call.
    """
    db = get_db()
    legacy_ref = (
        db.collection("users").document(uid)
          .collection("settings").document("agent_config")
    )
    legacy = legacy_ref.get()
    if not legacy.exists:
        return None
    legacy_data = legacy.to_dict() or {}
    if legacy_data.get("migratedToLoops"):
        return None
    # Only migrate if there's actually something there worth preserving.
    has_targets = bool(
        legacy_data.get("targetCompanies")
        or legacy_data.get("targetIndustries")
        or legacy_data.get("briefText")
    )
    if not has_targets:
        legacy_ref.update({"migratedToLoops": True})
        return None

    brief_text = legacy_data.get("briefText") or ""
    brief_parsed = legacy_data.get("briefParsed")

    # If the user predates briefs entirely, synthesize one from legacy targets
    # so the Loop has something meaningful to plan against.
    if not brief_text and not brief_parsed:
        targets = legacy_data.get("targetCompanies") or []
        roles = legacy_data.get("targetRoles") or []
        industries = legacy_data.get("targetIndustries") or []
        parts = []
        if roles:
            parts.append(f"{roles[0]} roles")
        if targets:
            parts.append(f"at {', '.join(targets[:3])}")
        elif industries:
            parts.append(f"in {industries[0]}")
        brief_text = " ".join(parts) or "Networking outreach"
        brief_parsed = {
            "companies": targets,
            "industries": industries,
            "roles": roles,
            "locations": legacy_data.get("targetLocations") or [],
            "emailPurpose": None,
            "constraints": [],
            "targetCount": None,
        }

    now = datetime.now(timezone.utc).isoformat()
    loop_id = str(uuid.uuid4())
    loop_doc = {
        **_loop_defaults(),
        "name": _auto_name_from_brief(brief_parsed, brief_text),
        "briefText": brief_text,
        "briefParsed": brief_parsed,
        "reviewBeforeSend": legacy_data.get("approvalMode", "review_first") == "review_first",
        "weeklyTarget": legacy_data.get("weeklyContactTarget", 5),
        "smsEnabled": legacy_data.get("smsEnabled", False),
        "status": legacy_data.get("status", "idle") if legacy_data.get("status") in LOOP_STATUS else "idle",
        "createdAt": legacy_data.get("deployedAt") or now,
        "lastRunAt": legacy_data.get("lastCycleAt"),
        "nextRunAt": legacy_data.get("nextCycleAt"),
        "totalContactsFound": legacy_data.get("totalContactsFound", 0),
        "totalEmailsDrafted": legacy_data.get("totalEmailsDrafted", 0),
        "totalRepliesReceived": legacy_data.get("totalRepliesReceived", 0),
        "totalJobsFound": legacy_data.get("totalJobsFound", 0),
        "totalHmsContacted": legacy_data.get("totalHmsContacted", 0),
        "totalCompaniesDiscovered": legacy_data.get("totalCompaniesDiscovered", 0),
    }
    db.collection("users").document(uid) \
      .collection("loops").document(loop_id).set(loop_doc)

    legacy_ref.update({"migratedToLoops": True, "primaryLoopId": loop_id})
    logger.info("Loop migration: created loop=%s for uid=%s", loop_id, uid)
    return {"id": loop_id, **loop_doc}
