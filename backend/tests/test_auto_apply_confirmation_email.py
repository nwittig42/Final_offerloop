"""
Auto-apply confirmation email + submit-evidence tests.

Mocks Firestore and the notification_adapter so no real Resend traffic
fires. The contracts that matter: one receipt per application, nothing
sent unless the application really was submitted, and the copy never
claims the employer confirmed receipt when the ATS showed us nothing.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.auto_apply import _form_filler_common as common
from app.services.auto_apply import confirmation_email
from app.services.auto_apply.confirmation_email import (
    render_application_confirmation,
    send_application_confirmation,
)
from app.services.notification_adapter import Channel, SendResult


# ── Helpers ──────────────────────────────────────────────────────────────


def _job(**overrides) -> dict:
    job = {
        "status": "submitted",
        "dry_run": False,
        "job_title": "Software Engineer Intern",
        "company": "Stripe",
        "ats_platform": "greenhouse",
        "apply_url": "https://job-boards.greenhouse.io/stripe/jobs/123",
        "submit_evidence": "confirmation_url",
        "applicant_email": "sam.resume@example.com",
    }
    job.update(overrides)
    return job


def _fake_db(job: dict | None = None, user: dict | None = None) -> MagicMock:
    """Firestore stand-in: users/{uid} + users/{uid}/autoApplyJobs/{id}."""
    if user is None:
        user = {"email": "sam@example.com", "firstName": "Sam"}
    job_ref = MagicMock()
    job_ref.get.return_value = MagicMock(to_dict=lambda: job if job is not None else _job())
    job_updates: list[dict] = []
    job_ref.update.side_effect = lambda d: job_updates.append(d)
    job_ref._updates = job_updates  # type: ignore[attr-defined]

    user_ref = MagicMock()
    user_ref.get.return_value = MagicMock(to_dict=lambda: user)
    user_ref.collection.return_value.document.return_value = job_ref

    db = MagicMock()
    db.collection.return_value.document.return_value = user_ref
    db._job_ref = job_ref  # type: ignore[attr-defined]
    return db


def _ok(recipient: str = "sam@example.com") -> SendResult:
    return SendResult(
        success=True,
        channel=Channel.EMAIL,
        recipient=recipient,
        vendor_message_id="re_123",
    )


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(
        confirmation_email.config, "AUTO_APPLY_CONFIRMATION_EMAIL_ENABLED", True
    )


def _page(url: str = "", text: str = "") -> MagicMock:
    page = MagicMock()
    page.url = url
    page.evaluate.return_value = text
    return page


_KEYWORDS = ("thank", "confirmation", "/success", "complete")


# ── Submit evidence ──────────────────────────────────────────────────────


def test_evidence_confirmation_url():
    page = _page(url="https://job-boards.greenhouse.io/stripe/jobs/123/confirmation")
    out = common.classify_submit_evidence(page, _KEYWORDS)
    assert out["submit_evidence"] == "confirmation_url"
    assert out["submit_final_url"].endswith("/confirmation")


def test_evidence_confirmation_text():
    page = _page(
        url="https://jobs.ashbyhq.com/acme/abc/application",
        text="Thanks for applying! We received your application.",
    )
    out = common.classify_submit_evidence(page, _KEYWORDS)
    assert out["submit_evidence"] == "confirmation_text"


def test_evidence_unconfirmed_when_page_shows_nothing():
    page = _page(url="https://boards.greenhouse.io/acme/jobs/1", text="Careers at Acme")
    out = common.classify_submit_evidence(page, _KEYWORDS)
    assert out["submit_evidence"] == "unconfirmed"


def test_evidence_never_raises_on_dead_page():
    page = MagicMock()
    type(page).url = property(lambda self: (_ for _ in ()).throw(RuntimeError("closed")))
    page.evaluate.side_effect = RuntimeError("closed")
    out = common.classify_submit_evidence(page, _KEYWORDS)
    assert out == {"submit_evidence": "unconfirmed", "submit_final_url": ""}


# ── Send gates ───────────────────────────────────────────────────────────


def test_flag_off_sends_nothing():
    db = _fake_db()
    with patch.object(confirmation_email, "adapter_send") as send:
        result = send_application_confirmation("u1", "aa1", db=db)
    assert result.error_code == "disabled"
    send.assert_not_called()


@pytest.mark.parametrize("job", [
    _job(status="submit_failed"),
    _job(status="needs_verification"),
    _job(status="dry_run_complete", dry_run=True),
    _job(dry_run=True),
])
def test_not_sent_unless_really_submitted(enabled, job):
    db = _fake_db(job=job)
    with patch.object(confirmation_email, "adapter_send") as send:
        result = send_application_confirmation("u1", "aa1", db=db)
    assert result.error_code == "not_submitted"
    send.assert_not_called()


def test_deduped_once_sent(enabled):
    db = _fake_db(job=_job(confirmation_email_sent_at="2026-10-05T00:00:00+00:00"))
    with patch.object(confirmation_email, "adapter_send") as send:
        result = send_application_confirmation("u1", "aa1", db=db)
    assert result is None
    send.assert_not_called()


def test_opt_out_respected(enabled):
    user = {
        "email": "sam@example.com",
        "emailPreferences": {"applicationConfirmations": False},
    }
    db = _fake_db(user=user)
    with patch.object(confirmation_email, "adapter_send") as send:
        result = send_application_confirmation("u1", "aa1", db=db)
    assert result.error_code == "opted_out"
    send.assert_not_called()


def test_no_account_email(enabled):
    db = _fake_db(user={"firstName": "Sam"})
    with patch.object(confirmation_email, "adapter_send") as send:
        result = send_application_confirmation("u1", "aa1", db=db)
    assert result.error_code == "invalid_recipient"
    send.assert_not_called()


# ── Send path ────────────────────────────────────────────────────────────


def test_sends_to_account_email_and_stamps_dedupe(enabled):
    db = _fake_db()
    with patch.object(confirmation_email, "adapter_send", return_value=_ok()) as send:
        result = send_application_confirmation("u1", "aa1", db=db)
    assert result.success
    kwargs = send.call_args.kwargs
    assert kwargs["recipient"] == "sam@example.com"
    assert kwargs["subject"] == "Application submitted: Software Engineer Intern at Stripe"
    assert "sam.resume@example.com" in kwargs["text_body"]
    updates = db._job_ref._updates
    assert len(updates) == 1
    assert updates[0]["confirmation_email_message_id"] == "re_123"
    assert updates[0]["confirmation_email_sent_at"]


def test_failed_send_leaves_no_dedupe_stamp(enabled):
    db = _fake_db()
    failed = SendResult(
        success=False, channel=Channel.EMAIL,
        recipient="sam@example.com", error_code="vendor_error",
    )
    with patch.object(confirmation_email, "adapter_send", return_value=failed):
        result = send_application_confirmation("u1", "aa1", db=db)
    assert not result.success
    assert db._job_ref._updates == []


def test_unconfirmed_submit_gets_the_softer_copy(enabled):
    db = _fake_db(job=_job(submit_evidence="unconfirmed"))
    with patch.object(confirmation_email, "adapter_send", return_value=_ok()) as send:
        send_application_confirmation("u1", "aa1", db=db)
    text = send.call_args.kwargs["text_body"]
    assert "confirmed it was received" not in text
    assert "didn't show us a confirmation page" in text
    assert "https://job-boards.greenhouse.io/stripe/jobs/123" in text


def test_missing_evidence_field_is_treated_as_unconfirmed(enabled):
    job = _job()
    job.pop("submit_evidence")
    db = _fake_db(job=job)
    with patch.object(confirmation_email, "adapter_send", return_value=_ok()) as send:
        send_application_confirmation("u1", "aa1", db=db)
    assert "confirmed it was received" not in send.call_args.kwargs["text_body"]


# ── Rendering ────────────────────────────────────────────────────────────


def test_render_confirmed():
    html, text = render_application_confirmation(
        first_name="Sam",
        job_title="Analyst",
        company="Bain",
        confirmed=True,
        applicant_email="sam@example.com",
        ats_label="Lever",
        apply_url="https://jobs.lever.co/bain/1",
    )
    for body in (html, text):
        assert "Sam — your application is in." in body
        assert "Analyst" in body and "Bain" in body
        assert "through Lever" in body
        assert "confirmed it was received" in body
        assert "sam@example.com" in body
        assert "apply directly" not in body


def test_render_escapes_company_name_in_html():
    html, _ = render_application_confirmation(
        first_name=None,
        job_title="Intern",
        company="<script>x</script>",
        confirmed=True,
        applicant_email=None,
        ats_label=None,
        apply_url=None,
    )
    assert "<script>x</script>" not in html
    assert "Your application is in." in html
