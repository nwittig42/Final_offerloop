"""
Auto-apply orchestrator.

Called from a background thread spawned by the submit endpoint. Loads
everything the per-ATS filler needs, dispatches, persists status updates to
Firestore as the run progresses, cleans up the temp resume file, and on
real (non-dry-run) success writes `applied_at` onto users/{uid}/savedJobs.

Job-doc collection: users/{uid}/autoApplyJobs/{auto_apply_id}
  status:      "queued" | "running" | "dry_run_complete" | "submitted" | "failed"
  stage:       human-readable progress string
  job_id:      source job ID (for the savedJobs link)
  dry_run:     bool
  screenshot_b64: base64 PNG (small for now; move to Storage if it grows)
  filled_summary: { field_name: "filled" | "empty" | error_msg }
  unmapped:    [{ field_id, label }] — fields the filler couldn't map
  failure_reason: str
  created_at:  ISO timestamp
  completed_at: ISO timestamp
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from urllib.parse import unquote
import threading
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import requests
from firebase_admin import firestore

from app.extensions import get_db
from app.services.auto_apply.answer_library import lookup_answer as _lib_lookup
from app.services.auto_apply.application_profile import get_application_profile
from app.services.auto_apply.ats_detector import detect_platform
from app.services.auto_apply.preview import build_preview, load_user_for_apply
from app.services.auto_apply.screening_answers import summarize_resume_for_prompt


logger = logging.getLogger(__name__)


JOB_COLLECTION_NAME = "autoApplyJobs"


def _make_answer_lookup(uid: str):
    """Build the callback the form-fillers use to query this user's answer
    library. Bound to the uid so the filler is uid-agnostic."""
    def lookup(label: str, field_type: str, options):
        return _lib_lookup(uid, label, field_type, options)
    return lookup


def update_status(uid: str, auto_apply_id: str, **fields: Any) -> None:
    """Patch the job doc. Stamps `updated_at` automatically."""
    fields.setdefault("updated_at", datetime.utcnow().isoformat())
    try:
        get_db().collection("users").document(uid).collection(
            JOB_COLLECTION_NAME
        ).document(auto_apply_id).update(fields)
    except Exception:
        logger.exception("update_status failed for %s/%s", uid, auto_apply_id)


# Per-user concurrency cap. Each submit spawns its own daemon thread, so a user
# swiping several jobs in a row fires several Browserbase sessions at once — and
# concurrent sessions amplify the submit/validate timing races (a job that
# submits cleanly when run alone gets a transient aria-invalid under load). Cap
# the number of simultaneous live runs PER USER (not globally, so one user's
# burst never blocks another user). Waiting jobs sit on the semaphore showing
# "queued" until a slot frees.
_APPLY_CONCURRENCY_PER_USER = 2
_user_apply_sems_lock = threading.Lock()
_user_apply_sems: Dict[str, threading.BoundedSemaphore] = {}


def _user_apply_semaphore(uid: str) -> threading.BoundedSemaphore:
    with _user_apply_sems_lock:
        sem = _user_apply_sems.get(uid)
        if sem is None:
            sem = threading.BoundedSemaphore(_APPLY_CONCURRENCY_PER_USER)
            _user_apply_sems[uid] = sem
        return sem


def run_auto_apply_job(
    auto_apply_id: str,
    uid: str,
    job_id: str,
    dry_run: bool,
    edited_answers: Dict[str, str],
) -> None:
    """Background entry point. Drives the whole run and writes status to
    Firestore so the polling endpoint can serve the UI."""
    resume_path: Optional[str] = None
    # Throttle concurrent runs for this user (see note above). Blocks here while
    # the user's other runs hold the slots; the doc stays "queued" meanwhile.
    sem = _user_apply_semaphore(uid)
    sem.acquire()
    try:
        update_status(uid, auto_apply_id, status="running", stage="loading_data")

        db = get_db()
        job_snap = db.collection("jobs").document(str(job_id)).get()
        if not job_snap.exists:
            update_status(
                uid, auto_apply_id,
                status="failed",
                failure_reason="job not found",
                completed_at=datetime.utcnow().isoformat(),
            )
            return
        job_data = job_snap.to_dict() or {}

        user = load_user_for_apply(uid)
        profile = get_application_profile(uid)
        user["applicationProfile"] = profile

        preview = build_preview(job_data, user)
        # Carry the profile inside the preview dict so the form-fillers can
        # reach it without re-fetching. Underscore-prefixed so it's clearly
        # internal and never accidentally rendered in the modal payload.
        preview["_application_profile"] = profile

        resume_summary = _build_student_context(user, preview.get("fields"))

        update_status(
            uid, auto_apply_id,
            # Diagnostic: lets us see post-hoc whether the LLM had any context
            # to work with. Empty context => everything will route to drawer.
            context_chars=len(resume_summary or ""),
        )

        update_status(uid, auto_apply_id, stage="downloading_resume")

        resume_url = user.get("resumeUrl") or user.get("resumeURL")
        if resume_url:
            try:
                resume_path = _download_resume_to_temp(
                    resume_url, user.get("resumeFileName") or "resume.pdf"
                )
            except Exception as exc:
                logger.warning("resume download failed: %s", exc)
                resume_path = None

        update_status(uid, auto_apply_id, stage="filling_form")

        def _progress(stage: str) -> None:
            # Mid-filler stage hop. The filler runs synchronously (fill →
            # submit → wait on the emailed code), so without this the doc is
            # stuck at filling_form for the whole submit+code-read window.
            #
            # awaiting_verification is emitted by the fillers IMMEDIATELY BEFORE
            # they click Submit, so it doubles as the "we are about to send a
            # real application" record. Stamping it here is what makes the job
            # idempotent: RQ requeues a task whose worker died, the task re-runs
            # from the top, and without this the second run cheerfully submits
            # the application a SECOND time. Discord got two runs on 2026-07-14
            # because a deploy restarted the worker mid-apply. A recruiter
            # receiving the same application twice is a real cost to the user.
            fields = {"stage": stage}
            if stage == "awaiting_verification" and not dry_run:
                fields["submit_attempted_at"] = datetime.now(timezone.utc)
            update_status(uid, auto_apply_id, **fields)

        platform = detect_platform(job_data)
        if platform == "greenhouse":
            from app.services.auto_apply.greenhouse import run_greenhouse_filler
            result = run_greenhouse_filler(
                apply_url=job_data.get("apply_url") or "",
                preview=preview,
                edited_answers=edited_answers or {},
                resume_path=resume_path,
                dry_run=dry_run,
                job_id=str(job_id),
                answer_lookup=_make_answer_lookup(uid),
                uid=uid,
                resume_summary=resume_summary,
                job_data=job_data,
                progress_cb=_progress,
            )
        elif platform == "lever":
            from app.services.auto_apply.lever import run_lever_filler
            result = run_lever_filler(
                apply_url=job_data.get("apply_url") or "",
                preview=preview,
                edited_answers=edited_answers or {},
                resume_path=resume_path,
                dry_run=dry_run,
                job_id=str(job_id),
                answer_lookup=_make_answer_lookup(uid),
                uid=uid,
                resume_summary=resume_summary,
                job_data=job_data,
                progress_cb=_progress,
            )
        elif platform == "ashby":
            from app.services.auto_apply.ashby import run_ashby_filler
            result = run_ashby_filler(
                apply_url=job_data.get("apply_url") or "",
                preview=preview,
                edited_answers=edited_answers or {},
                resume_path=resume_path,
                dry_run=dry_run,
                job_id=str(job_id),
                answer_lookup=_make_answer_lookup(uid),
                uid=uid,
                resume_summary=resume_summary,
                job_data=job_data,
                progress_cb=_progress,
            )
        else:
            result = {
                "status": "failed",
                "failure_reason": f"no form-filler for {platform!r} yet",
            }

        # Two terminal-but-not-completed states. Both mean "filler did
        # everything it could, now waiting on the user", and neither marks
        # `completed_at`:
        #   - needs_attention: required fields the resolver couldn't answer
        #     (LLM NEEDS_USER, library miss, etc). Drawer asks the user to
        #     fill them in; on resolve we re-spawn the worker.
        #   - needs_verification: form fill succeeded but the ATS ships
        #     CAPTCHA (Greenhouse reCAPTCHA, Lever hCaptcha, Ashby
        #     reCAPTCHA v3). User finishes the submission from their own
        #     browser (real device + IP = high CAPTCHA score). Drawer
        #     shows prepared_answers as a reference.
        status = result.get("status", "failed")
        is_paused_for_user = status in ("needs_attention", "needs_verification")

        patch: Dict[str, Any] = {
            "status": status,
            "filled_summary": result.get("filled") or {},
            "unmapped": result.get("unmapped") or [],
            "inspect_completed_at": datetime.utcnow().isoformat(),
        }
        if not is_paused_for_user:
            patch["completed_at"] = datetime.utcnow().isoformat()
        if result.get("pending_questions"):
            patch["pending_questions"] = result["pending_questions"]
        if result.get("prepared_answers"):
            patch["prepared_answers"] = result["prepared_answers"]
        if result.get("captcha"):
            patch["captcha"] = result["captcha"]
        if result.get("apply_url"):
            patch["apply_url"] = result["apply_url"]
        if result.get("screenshot_b64"):
            # Firestore caps a single property value at 1,048,487 bytes.
            # Full-page Greenhouse screenshots routinely exceed that (long
            # forms scroll past 1MB of PNG base64). Drop the screenshot
            # when it would blow the limit so the rest of the patch lands.
            # TODO: write oversized screenshots to Cloud Storage and store
            # the URL here instead.
            shot = result["screenshot_b64"]
            if isinstance(shot, str) and len(shot) <= 1_000_000:
                patch["screenshot_b64"] = shot
            else:
                logger.warning(
                    "auto_apply screenshot too large for Firestore (%d bytes); dropping",
                    len(shot) if isinstance(shot, str) else -1,
                )
        if result.get("failure_reason"):
            patch["failure_reason"] = result["failure_reason"]
        if result.get("attempted_urls"):
            patch["attempted_urls"] = result["attempted_urls"]
        if result.get("attempt_log"):
            patch["attempt_log"] = result["attempt_log"]
        if status == "submitted":
            # What the ATS showed us after submit (confirmation URL / text, or
            # nothing), and the address the employer's own confirmation goes
            # to — the resume email, which can differ from the account email.
            patch["submit_evidence"] = result.get("submit_evidence") or "unconfirmed"
            if result.get("submit_final_url"):
                patch["submit_final_url"] = result["submit_final_url"]
            applicant_email = ((preview.get("fields") or {}).get("email") or "").strip()
            if applicant_email:
                patch["applicant_email"] = applicant_email
        update_status(uid, auto_apply_id, **patch)

        # Ping the user (bell + push) when we stop for input, so they don't have
        # to watch the Applied tab. Deduped + best-effort inside the helper.
        if status == "needs_attention":
            try:
                from app.services.auto_apply.notify import notify_needs_attention
                notify_needs_attention(uid, auto_apply_id, db=db)
            except Exception:
                logger.exception("needs_attention notify failed for %s", auto_apply_id)

        # Terminal outcomes get a ping too (real submissions only): submitted
        # is the win the user is waiting on; needs_verification is one human
        # tap from done; failed shouldn't sit silent in a tab.
        if not dry_run and status in ("submitted", "needs_verification", "failed", "submit_failed"):
            try:
                from app.services.auto_apply.notify import notify_application_result
                notify_application_result(uid, auto_apply_id, status, db=db)
            except Exception:
                logger.exception("application result notify failed for %s", auto_apply_id)

        # Email receipt for the win, alongside the bell + push above.
        if not dry_run and status == "submitted":
            try:
                from app.services.auto_apply.confirmation_email import (
                    send_application_confirmation,
                )
                send_application_confirmation(uid, auto_apply_id, db=db)
            except Exception:
                logger.exception("confirmation email failed for %s", auto_apply_id)

        if result.get("status") == "submitted":
            try:
                db.collection("users").document(uid).collection(
                    "savedJobs"
                ).document(str(job_id)).set(
                    {"applied_at": firestore.SERVER_TIMESTAMP},
                    merge=True,
                )
            except Exception:
                logger.exception("failed to stamp savedJobs.applied_at")

    except Exception as exc:
        logger.exception("auto-apply runner crashed for %s/%s", uid, auto_apply_id)
        update_status(
            uid, auto_apply_id,
            status="failed",
            failure_reason=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc()[-2000:],
            completed_at=datetime.utcnow().isoformat(),
        )
        if not dry_run:
            try:
                from app.services.auto_apply.notify import notify_application_result
                notify_application_result(uid, auto_apply_id, "failed", db=db)
            except Exception:
                logger.exception("crash-path result notify failed for %s", auto_apply_id)
    finally:
        try:
            sem.release()
        except (ValueError, RuntimeError):
            # BoundedSemaphore over-release guard — should never happen since we
            # always acquire once above, but never let it mask a real error.
            pass
        if resume_path and os.path.exists(resume_path):
            try:
                os.unlink(resume_path)
            except Exception:
                pass


def _build_student_context(
    user: Dict[str, Any],
    preview_fields: Optional[Dict[str, Any]] = None,
) -> str:
    """Compose the context the LLM uses to answer form questions.

    Pulls from EVERY surface the runner has access to:
      - `preview.fields` — name/email/phone/location/LinkedIn/GitHub/
        most_recent_company/most_recent_title (computed by build_structured_fields
        from resume + profile, this is the highest-quality denormalization)
      - `user.resumeParsed` — long-form experience/education/skills
      - `user.academics` — school/major/year (when resume parsing missed it)
      - `user.applicationProfile.workAuthorization` — work auth + sponsorship +
        visa status. Critical context so the LLM can answer "Do you have the
        legal right to work?" without seeing a blank `<student>` block.

    The LLM gets one cohesive view of the candidate, named sections so it can
    quote them ("CURRENT ROLE", "LOCATION", "WORK AUTHORIZATION") in answers."""
    parts: list[str] = []
    pf = preview_fields or {}

    name = pf.get("full_name") or user.get("name")
    if name:
        parts.append(f"NAME: {name}")

    email = pf.get("email") or user.get("email")
    if email:
        parts.append(f"EMAIL: {email}")

    phone = pf.get("phone")
    if phone:
        parts.append(f"PHONE: {phone}")

    # Location — preview.fields uses resume.location merged with profile.
    location = pf.get("location")
    if not location:
        loc_doc = user.get("location") or {}
        if isinstance(loc_doc, dict):
            location = loc_doc.get("currentLocation") or loc_doc.get("city")
            if not location:
                pref = loc_doc.get("preferredLocation")
                if isinstance(pref, list) and pref:
                    location = pref[0]
                elif isinstance(pref, str):
                    location = pref
        elif isinstance(loc_doc, str):
            location = loc_doc
    if location:
        parts.append(f"LOCATION: {location}")

    # Preferred work geographies the user chose in onboarding / profile. These
    # answer "preferred office location(s)" style questions: the resolver matches
    # them against the form's option list. "Select 1-3" is satisfied by ONE, so
    # surfacing the whole list lets it pick a valid option and clear the field
    # instead of escalating to the drawer. Gathered from the mobile top-level
    # `preferredLocations` and the onboarding nested `location.preferredLocation`.
    pref_raw: list[str] = []
    top_pref = user.get("preferredLocations")
    if isinstance(top_pref, list):
        pref_raw += [str(x) for x in top_pref if x]
    loc_doc2 = user.get("location") or {}
    if isinstance(loc_doc2, dict):
        nested = loc_doc2.get("preferredLocation") or loc_doc2.get("preferredLocations")
        if isinstance(nested, list):
            pref_raw += [str(x) for x in nested if x]
        elif isinstance(nested, str) and nested.strip():
            pref_raw.append(nested.strip())
    seen_pl: set[str] = set()
    pref_locs: list[str] = []
    for p in pref_raw:
        key = p.strip().lower()
        if key and key not in seen_pl:
            seen_pl.add(key)
            pref_locs.append(p.strip())
    if pref_locs:
        parts.append(f"PREFERRED LOCATIONS: {'; '.join(pref_locs[:6])}")

    # Current role — preview.fields derives this from resume.experience[0].
    # If the resume parser hasn't filled experience yet, fall back to
    # professionalInfo on the user doc (some Offerloop users set
    # currentCompany / currentTitle during onboarding).
    cur_company = pf.get("most_recent_company")
    cur_title = pf.get("most_recent_title")
    prof = user.get("professionalInfo") or {}
    if not cur_company:
        cur_company = prof.get("currentCompany") or prof.get("company")
    if not cur_title:
        cur_title = prof.get("currentTitle") or prof.get("title") or prof.get("role")
    if cur_company or cur_title:
        parts.append(
            f"CURRENT ROLE: {cur_title or 'unknown title'} at {cur_company or 'unknown company'}"
        )

    # Links — preview.fields merges profile overrides for LinkedIn (profile wins).
    link_bits = []
    for key, label in (
        ("linkedin_url", "LinkedIn"),
        ("github_url", "GitHub"),
        ("portfolio_url", "Portfolio"),
    ):
        val = pf.get(key)
        if val:
            link_bits.append(f"{label}={val}")
    if link_bits:
        parts.append("LINKS: " + " | ".join(link_bits))

    # Work authorization — critical, currently the most-missed field. Surface
    # explicit YES/NO/UNSET so the LLM can answer dropdown questions ("Do you
    # have the legal right to work?") truthfully.
    profile = user.get("applicationProfile") or {}
    wa = profile.get("workAuthorization") or {}
    wa_bits = []
    auth_us = wa.get("authorizedToWorkUS")
    if auth_us is True:
        wa_bits.append("authorized to work in US=YES")
    elif auth_us is False:
        wa_bits.append("authorized to work in US=NO")
    req_sp = wa.get("requiresSponsorship")
    if req_sp is True:
        wa_bits.append("requires visa sponsorship=YES")
    elif req_sp is False:
        wa_bits.append("requires visa sponsorship=NO")
    visa = wa.get("visaStatus")
    if visa:
        wa_bits.append(f"visa status={visa}")
    if wa_bits:
        parts.append("WORK AUTHORIZATION: " + " | ".join(wa_bits))

    # Resume long-form (experience + education + skills + summary).
    resume_summary = summarize_resume_for_prompt(user.get("resumeParsed") or {})
    if resume_summary:
        parts.append("RESUME:\n" + resume_summary)

    # Academics — only adds info when resume parsing missed it.
    academics = user.get("academics") or {}
    acad_bits = []
    for key in ("school", "major", "minor", "degree", "graduationYear", "gpa"):
        val = academics.get(key)
        if val:
            acad_bits.append(f"{key}={val}")
    if acad_bits:
        parts.append("ACADEMICS: " + " | ".join(acad_bits))

    return "\n\n".join(parts)


def _download_resume_to_temp(url: str, filename: str) -> str:
    """Pull the resume PDF to a temp file and return a local path for
    Playwright's set_input_files.

    The BASENAME of this path is the filename the EMPLOYER sees in their ATS.
    mkstemp() named it for the machine, so every application we have ever
    submitted arrived as "autoapply_resume_8c6k9nbn.pdf" — a random temp name
    attached to a real application under the user's real name. Write the file
    into a temp DIRECTORY under the user's actual filename instead, so the
    recruiter gets "Rylan Bohnett Resume April 2025.pdf".

    Also decodes the stored name: some resumes were saved URL-encoded
    ("Rylan%20Bohnett%20Resume.pdf"), and we are NOT putting %20s on somebody's
    job application.
    """
    raw = os.path.basename((filename or "").strip())
    try:
        raw = unquote(raw)
    except Exception:
        pass
    # Strip anything that isn't safe as a path component; keep it human.
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", raw)
    safe = re.sub(r"\s+", " ", safe).strip(" .")
    if not safe:
        safe = "Resume.pdf"
    if not os.path.splitext(safe)[1]:
        safe += ".pdf"

    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    # mkdtemp (not mkstemp): the directory carries the uniqueness, the FILE
    # keeps the human name.
    tmpdir = tempfile.mkdtemp(prefix="autoapply_resume_")
    path = os.path.join(tmpdir, safe)
    try:
        with open(path, "wb") as f:
            f.write(resp.content)
        return path
    except Exception:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass
        raise


def _upload_screenshot_to_storage(
    uid: str, auto_apply_id: str, b64: str
) -> Optional[str]:
    """Upload a base64-encoded PNG screenshot to Firebase Cloud Storage and
    return a URL. Returns None on any failure so the caller can fall back
    to dropping the screenshot.

    Used when the screenshot is too large for Firestore's per-property 1MB
    cap. Tries `make_public()` first (matches resume.py upload pattern); if
    that raises — typically a missing `roles/storage.legacyObjectReader`
    grant on the Render service account — falls back to a 7-day signed URL,
    which doesn't require legacy ACL permissions. Without this fallback,
    every long-form failure shipped with zero visual evidence."""
    try:
        import base64 as _base64
        from firebase_admin import storage

        png_bytes = _base64.b64decode(b64)
        bucket = storage.bucket()
        blob = bucket.blob(
            f"auto_apply_screenshots/{uid}/{auto_apply_id}.png"
        )
        blob.upload_from_string(png_bytes, content_type="image/png")
    except Exception as exc:
        logger.warning(
            "auto_apply screenshot Cloud Storage upload failed for %s/%s: %r",
            uid, auto_apply_id, exc,
        )
        return None

    try:
        blob.make_public()
        return blob.public_url
    except Exception as exc:
        logger.info(
            "auto_apply screenshot make_public failed for %s/%s (%r); "
            "falling back to signed URL",
            uid, auto_apply_id, exc,
        )

    try:
        return blob.generate_signed_url(
            expiration=timedelta(days=7),
            method="GET",
            version="v4",
        )
    except Exception as exc:
        logger.warning(
            "auto_apply screenshot signed URL fallback failed for %s/%s: %r",
            uid, auto_apply_id, exc,
        )
        return None
