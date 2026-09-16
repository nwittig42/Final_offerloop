"""Shared auto-apply submission entrypoint.

Extracted from the /api/job-board/auto-apply/<id>/submit route body so two
callers share ONE implementation of the eligibility / profile / dedupe /
credit checks and the worker hand-off:
  1. the HTTP route (routes/auto_apply.py), and
  2. Scout's auto_apply_to_job chat tool (services/scout/tools.py).

Scout has imported this module since the execute-actions work landed. It did
not exist on this branch, so every "apply me to that job" asked in the app
raised ImportError inside the tool, came back as code INTERNAL, and Scout
told the user something vague had gone wrong. Nothing was ever queued.

Returns (payload dict, http-ish status int). The status doubles as the HTTP
status for the route caller; Scout reads the payload's "code" field.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from app.config import AUTO_APPLY_CREDITS
from app.extensions import get_db
from app.services.auth import deduct_credits_atomic
from app.services.auto_apply.application_profile import (
    get_application_profile,
    is_acknowledged,
    work_auth_complete,
)
from app.services.auto_apply.ats_detector import detect_platform, is_eligible
from app.services.rq_queue import enqueue, is_durable

logger = logging.getLogger(__name__)


def submit_auto_apply_for_user(
    uid: str,
    job_id: str,
    *,
    dry_run: bool = True,
    edited_answers: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], int]:
    """Validate and queue one auto-apply submission for `uid`.

    Hands the form-filler to the RQ worker and returns an auto_apply_id the
    caller can poll at /api/job-board/auto-apply/<auto_apply_id>/status.
    Dry-run runs the filler but does NOT click Submit, and is free; real
    submits charge AUTO_APPLY_CREDITS, and the worker owns the refund.

    Every early return names a `code` so a chat caller can speak the blocker
    and a client can render the matching affordance. job_title and company
    ride on the success payload so a caller that never read the job doc can
    still name what it applied to.
    """
    edited_answers = edited_answers if isinstance(edited_answers, dict) else {}

    if not os.getenv("BROWSERBASE_API_KEY") or not os.getenv("BROWSERBASE_PROJECT_ID"):
        return {
            "error": (
                "Browserbase is not configured. Set BROWSERBASE_API_KEY and "
                "BROWSERBASE_PROJECT_ID in the environment to enable submissions."
            ),
            "code": "BROWSERBASE_NOT_CONFIGURED",
        }, 501

    db = get_db()
    if db is None:
        return {"error": "database unavailable", "code": "INTERNAL"}, 503

    # Eligibility re-check (cheap; prevents stale prepare → submit drift)
    job_snap = db.collection("jobs").document(str(job_id)).get()
    if not job_snap.exists:
        return {"error": "job not found", "code": "JOB_NOT_FOUND"}, 404
    job_data = job_snap.to_dict() or {}
    if not is_eligible(job_data):
        # apply_url rides along so a caller (Scout) that promises "here's the
        # direct link" on an ineligible ATS can actually keep that promise
        # instead of improvising or dropping the link (Rylan 2026-09-16).
        return {
            "error": "job is not auto-apply eligible",
            "code": "INELIGIBLE",
            "apply_url": job_data.get("apply_url") or "",
        }, 400

    job_title = job_data.get("title") or ""
    company = job_data.get("company") or ""

    profile = get_application_profile(uid)
    if not is_acknowledged(profile) or not work_auth_complete(profile):
        return {
            "error": "application profile incomplete",
            "code": "PROFILE_REQUIRED",
        }, 409

    # Dedupe: one live application per (user, job). Multiple client surfaces
    # can fire submit for the same role; return the existing record instead of
    # charging credits and spawning a second Browserbase run. Failed attempts
    # and dry-runs don't count, so retries stay possible.
    if not dry_run:
        existing = (
            db.collection("users").document(uid).collection("autoApplyJobs")
            .where("job_id", "==", str(job_id))
            .stream()
        )
        for snap in existing:
            data = snap.to_dict() or {}
            if data.get("dry_run") or data.get("status") in ("failed", "submit_failed"):
                continue
            logger.info(
                "dedupe: uid=%s already applied to job=%s (auto_apply_id=%s, status=%s); skipping",
                uid, job_id, snap.id, data.get("status"),
            )
            return {
                "auto_apply_id": snap.id,
                "job_id": str(job_id),
                "job_title": data.get("job_title") or job_title,
                "company": data.get("company") or company,
                "dry_run": False,
                "status": data.get("status") or "queued",
                "deduped": True,
            }, 200

    # Auto-apply MUST run out-of-process. Playwright's driver, started inside a
    # web worker, killed the whole container ~23s in (2026-07-12) — taking
    # drafts, Scout and the feed down with it. rq_queue's dev fallback would run
    # it in-process, which is exactly that crash, so if the RQ worker isn't
    # configured we refuse CLEANLY instead of taking the box down. Checked
    # before the credit deduction so a refusal never charges anyone.
    if not is_durable():
        logger.error("auto-apply refused: no REDIS_URL/RQ worker (would run in-process)")
        return {
            "error": "Auto-apply is temporarily unavailable. Nothing was charged.",
            "code": "AUTOAPPLY_UNAVAILABLE",
        }, 503

    # Credit deduction: only on REAL submits. Dry-runs are free so users can
    # iterate without burning credits. Refund on failure is the worker's job.
    if not dry_run:
        ok, _ = deduct_credits_atomic(uid, AUTO_APPLY_CREDITS, "auto_apply")
        if not ok:
            return {
                "error": "insufficient credits",
                "credits_needed": AUTO_APPLY_CREDITS,
                "code": "INSUFFICIENT_CREDITS",
            }, 402

    auto_apply_id = uuid.uuid4().hex
    db.collection("users").document(uid).collection(
        "autoApplyJobs"
    ).document(auto_apply_id).set({
        "auto_apply_id": auto_apply_id,
        "job_id": str(job_id),
        "ats_platform": detect_platform(job_data),
        # Denormalized for the Auto-Submission + Needs Attention tab cards so
        # they don't have to round-trip jobs/{job_id} for every list render.
        "job_title": job_title,
        "company": company,
        "apply_url": job_data.get("apply_url") or "",
        "dry_run": dry_run,
        "status": "queued",
        "stage": "queued",
        "credits_charged": 0 if dry_run else AUTO_APPLY_CREDITS,
        "created_at": datetime.utcnow().isoformat(),
    })

    # Hand off to the RQ worker. The browser (Playwright + Browserbase) starts
    # ONLY in that process — never here. The task owns the terminal state and
    # the refund; if the worker itself dies mid-fill, _reap_if_stale finalizes
    # and refunds on the next poll, so nothing can sit at 'running' forever.
    enqueue(
        "run_auto_apply",
        auto_apply_id=auto_apply_id,
        uid=uid,
        job_id=str(job_id),
        dry_run=dry_run,
        edited_answers=edited_answers,
    )

    return {
        "auto_apply_id": auto_apply_id,
        "job_id": str(job_id),
        "job_title": job_title,
        "company": company,
        "dry_run": dry_run,
        "status": "queued",
    }, 200
