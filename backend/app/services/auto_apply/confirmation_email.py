"""
confirmation_email — "your application is in" receipt after an auto-apply.

A swipe hands the whole application to us, so the user never sees the
employer's thank-you page. This email is their receipt: what we applied to,
when, which address the employer's own confirmation will go to, and whether
the employer's site actually showed us a confirmation.

Contract:
  send_application_confirmation(uid, auto_apply_id, db=None) -> SendResult | None

Honesty:
  The copy follows `submit_evidence` on the autoApplyJobs doc. Only
  confirmation_url / confirmation_text say the employer confirmed receipt.
  An `unconfirmed` submit gets a softer email that tells the user to watch
  for the employer's own confirmation and links the posting as a fallback.

Idempotency:
  One email per application, keyed on `confirmation_email_sent_at` on the
  same doc. Returns None when it was already sent. Only a successful send
  stamps it, so a transient Resend failure can be retried.

Gates:
  AUTO_APPLY_CONFIRMATION_EMAIL_ENABLED (default off), and the per-user
  opt-out users/{uid}.emailPreferences.applicationConfirmations == False.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import config
from app.services.notification_adapter import Channel, SendResult, send as adapter_send

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "templates",
    "notifications",
)

_jinja_env = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    autoescape=select_autoescape(["html", "html.j2"]),
    trim_blocks=False,
    lstrip_blocks=False,
)

_CONFIRMED_EVIDENCE = ("confirmation_url", "confirmation_text")
_ATS_LABELS = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby"}
_APPLICATIONS_URL = "https://offerloop.ai/job-board"
_SETTINGS_URL = "https://offerloop.ai/account-settings"


def _skipped(error_code: str, recipient: str = "") -> SendResult:
    return SendResult(
        success=False,
        channel=Channel.EMAIL,
        recipient=recipient,
        error_code=error_code,
    )


def render_application_confirmation(
    *,
    first_name: str | None,
    job_title: str,
    company: str,
    confirmed: bool,
    applicant_email: str | None,
    ats_label: str | None,
    apply_url: str | None,
) -> tuple[str, str]:
    """Render the HTML + text bodies for the application receipt."""
    ctx = {
        "first_name": (first_name or "").strip() or None,
        "job_title": job_title,
        "company": company,
        "confirmed": confirmed,
        "applicant_email": applicant_email or None,
        "ats_label": ats_label,
        "apply_url": apply_url or None,
        "applications_url": _APPLICATIONS_URL,
        "settings_url": _SETTINGS_URL,
    }
    html = _jinja_env.get_template("application_submitted.html.j2").render(**ctx)
    text = _jinja_env.get_template("application_submitted.txt.j2").render(**ctx)
    return html, text


def send_application_confirmation(
    uid: str,
    auto_apply_id: str,
    db=None,
) -> SendResult | None:
    """Email the user a receipt for one submitted auto-apply.

    Returns:
        SendResult on a fresh attempt (success, a skip, or a send failure).
        None when this application's receipt was already sent.
    """
    if not config.AUTO_APPLY_CONFIRMATION_EMAIL_ENABLED:
        logger.info(
            "confirmation_email: send skipped (flag disabled) uid=%s aa=%s",
            uid, auto_apply_id,
        )
        return _skipped("disabled")

    if db is None:
        from app.extensions import get_db
        db = get_db()

    user_ref = db.collection("users").document(uid)
    job_ref = user_ref.collection("autoApplyJobs").document(auto_apply_id)
    job = job_ref.get().to_dict() or {}

    # Receipt for real submissions only: a dry run never reached the employer.
    if job.get("status") != "submitted" or job.get("dry_run"):
        return _skipped("not_submitted")
    if job.get("confirmation_email_sent_at"):
        logger.info("confirmation_email: deduped uid=%s aa=%s", uid, auto_apply_id)
        return None

    user = user_ref.get().to_dict() or {}
    prefs = user.get("emailPreferences") or {}
    if prefs.get("applicationConfirmations") is False:
        return _skipped("opted_out")

    recipient = (user.get("email") or "").strip()
    if not recipient:
        logger.warning("confirmation_email: no email on file uid=%s", uid)
        return _skipped("invalid_recipient")

    first_name = user.get("firstName") or (user.get("name") or "").split(" ")[0]
    job_title = job.get("job_title") or "the role"
    company = job.get("company") or "the company"
    confirmed = job.get("submit_evidence") in _CONFIRMED_EVIDENCE

    html, text = render_application_confirmation(
        first_name=first_name,
        job_title=job_title,
        company=company,
        confirmed=confirmed,
        applicant_email=job.get("applicant_email"),
        ats_label=_ATS_LABELS.get(job.get("ats_platform") or ""),
        apply_url=job.get("apply_url"),
    )
    subject = f"Application submitted: {job_title} at {company}"

    result = adapter_send(
        Channel.EMAIL,
        recipient=recipient,
        subject=subject,
        html_body=html,
        text_body=text,
    )

    if result.success:
        try:
            job_ref.update({
                "confirmation_email_sent_at": datetime.now(timezone.utc).isoformat(),
                "confirmation_email_message_id": result.vendor_message_id,
            })
        except Exception:
            logger.exception(
                "confirmation_email: dedupe write failed uid=%s aa=%s",
                uid, auto_apply_id,
            )
    else:
        logger.warning(
            "confirmation_email: send failed uid=%s aa=%s error=%s",
            uid, auto_apply_id, result.error_code,
        )
    return result
