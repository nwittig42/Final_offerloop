"""Sending the first email (app.services.first_email_send) and its route.

In-process only: Firestore is tests.fake_firestore, Gmail is a MagicMock
service, and tracking / cooldown / metrics / template lookups are patched,
so no real email is sent and nothing remote is called.
"""
import base64
import email
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import firebase_admin
import pytest
from flask import Flask

from app.services import first_email_send as fes
from tests.fake_firestore import FakeDB

NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
CONTACT = "users/u1/contacts/ranker_c1"
LEDGER = "users/u1/firstEmailSends/ranker_c1"
QUOTA = "users/u1/sendQuota/20261103"
SUBJECT = "Evercore question from a USC student"
BODY = ("Hi Pat,\n\nI'm a sophomore at USC studying economics and I'm recruiting for investment banking. "
        "I saw you moved from Michigan to Evercore and I'd love to hear how you approached that. "
        "Would you be open to a 15-minute call next week?\n\nBest,\nAna")
COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
READONLY = "https://www.googleapis.com/auth/gmail.readonly"


def make_db(**contact):
    return FakeDB({
        "users/u1": {"name": "Ana Lee", "email": "ana@usc.edu", "walkthrough": {"status": "drafted", "step": "draft"}},
        CONTACT: {"firstName": "Pat", "lastName": "Kim", "company": "Evercore", "jobTitle": "Analyst",
                  "candidateId": "c1", "draftToEmail": "tampered@evil.example", "email": "tampered@evil.example",
                  "emailSubject": SUBJECT, "emailBody": BODY, "pipelineStage": "generated", **contact},
        "users/u1/revealedEmails/c1": {"email": "pat@evercore.com"},
    })


def make_gmail(scopes=(COMPOSE, READONLY), found=None):
    service = MagicMock()
    users = service.users.return_value
    users.messages.return_value.send.return_value.execute.return_value = {"id": "m1", "threadId": "t1"}
    users.messages.return_value.list.return_value.execute.return_value = {"messages": [found]} if found else {}
    return fes.Gmail(service, set(scopes), "ana@usc.edu")


@pytest.fixture(autouse=True)
def quiet():
    with patch("app.services.email_tracking.start", return_value=None), \
         patch("app.services.email_tracking.register") as register, \
         patch("app.services.cooldown_service.record_outreach") as cooldown, \
         patch("app.utils.metrics_events.log_event") as log_event, \
         patch("app.services.first_outreach.account_created_at", return_value=NOW - timedelta(minutes=4)), \
         patch("app.services.email_request_builder.resolve_email_template", return_value=(None, "coffee_chat", None, None)), \
         patch("app.services.gmail_client._load_user_gmail_creds", return_value=None):
        yield {"register": register, "cooldown": cooldown, "log_event": log_event}


def send(db, gmail, confirmed=None, **kw):
    return fes.send_first_email(db, "u1", "ranker_c1", confirmed or fes.draft_hash(SUBJECT, BODY),
                                surface=kw.pop("surface", "web_walkthrough"), gmail_factory=lambda uid: gmail,
                                now=kw.pop("now", NOW), **kw)


def sent_message(gmail):
    raw = gmail.service.users().messages().send.call_args.kwargs["body"]["raw"]
    return email.message_from_bytes(base64.urlsafe_b64decode(raw))


# ── Review ───────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_recipient_comes_from_the_server_side_reveal_not_the_contact_doc():
    info = fes.review(make_db(), "u1", "ranker_c1")
    assert info.recipient == "pat@evercore.com"
    assert info.blocking == []


@pytest.mark.unit
@pytest.mark.parametrize("contact,blocking", [
    ({"emailBody": "Hi [Name], quick question about [Company]?"}, ["placeholder"]),
    ({"emailBody": "Hi {{first_name}}"}, ["placeholder"]),
    ({"emailBody": "Hi [YOUR NAME] here"}, ["placeholder"]),
    ({"emailBody": "   "}, ["empty_body"]),
    ({"emailSubject": ""}, ["empty_subject"]),
])
def test_serious_problems_block(contact, blocking):
    assert fes.review(make_db(**contact), "u1", "ranker_c1").blocking == blocking


@pytest.mark.unit
def test_no_recipient_blocks():
    db = make_db()
    del db.store["users/u1/revealedEmails/c1"]
    assert fes.review(db, "u1", "ranker_c1").blocking == ["no_recipient"]


@pytest.mark.unit
def test_style_and_length_are_only_warnings():
    short = "Hi Pat, would you be open to a quick call?"
    db = make_db(emailBody=short)
    info = fes.review(db, "u1", "ranker_c1")
    assert info.blocking == [] and "too_short" in info.warnings
    gmail = make_gmail()
    assert send(db, gmail, fes.draft_hash(SUBJECT, short))["sent"] is True


# ── Sending ──────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_sends_the_saved_draft_once_and_records_it(quiet):
    db, gmail = make_db(gmailDraftId="d-old", gmailDraftUrl="https://mail.google.com/d-old"), make_gmail()
    stale_service = MagicMock()
    with patch("app.services.gmail_client._load_user_gmail_creds", return_value=object()), \
         patch("app.services.gmail_client._gmail_service", return_value=stale_service):
        result = send(db, gmail)
    assert result == {"sent": True, "alreadySent": False, "contactId": "ranker_c1", "messageId": "m1", "threadId": "t1"}

    msg = sent_message(gmail)
    assert msg["to"] == "pat@evercore.com"           # the reveal, not the tampered doc
    assert msg["subject"] == SUBJECT                 # no "Re:" on a first email
    assert msg["Message-ID"].endswith("@offerloop.ai>")
    text = " ".join(
        part.get_payload(decode=True).decode("utf-8")
        for part in msg.walk() if part.get_content_maintype() == "text"
    )
    assert "I saw you moved from Michigan to Evercore" in text   # the saved draft, verbatim

    contact = db.store[CONTACT]
    assert contact["emailSent"] is True and contact["pipelineStage"] == "email_sent"
    assert contact["gmailMessageId"] == "m1" and contact["gmailThreadId"] == "t1"
    assert contact["draftStillExists"] is False
    assert "gmailDraftId" not in contact and "gmailDraftUrl" not in contact
    stale_service.users().drafts().delete.assert_called_once_with(userId="me", id="d-old")

    assert db.store[LEDGER]["status"] == "sent"
    assert db.store[QUOTA]["count"] == 1
    assert db.store["users/u1"]["walkthrough"]["status"] == "completed"
    assert db.store["users/u1"]["walkthrough"]["contactId"] == "ranker_c1"

    event = quiet["log_event"].call_args
    assert event.args[:2] == ("u1", "first_email_sent")
    props = event.args[2]
    assert props["seconds_since_signup"] == 240
    assert not any(SUBJECT in str(v) or "Pat" in str(v) or "@" in str(v) for v in props.values())


@pytest.mark.unit
def test_a_repeat_never_sends_twice():
    db, gmail = make_db(), make_gmail()
    send(db, gmail)
    again = send(db, gmail)
    assert again["alreadySent"] is True and again["messageId"] == "m1"
    assert gmail.service.users().messages().send.call_count == 1
    assert db.store[QUOTA]["count"] == 1


@pytest.mark.unit
def test_already_sent_elsewhere_is_not_resent():
    db, gmail = make_db(emailSent=True, gmailMessageId="m0"), make_gmail()
    assert send(db, gmail)["alreadySent"] is True
    gmail.service.users().messages().send.assert_not_called()


@pytest.mark.unit
def test_only_the_confirmed_text_goes_out():
    db, gmail = make_db(), make_gmail()
    with pytest.raises(fes.SendError) as err:
        send(db, gmail, fes.draft_hash(SUBJECT, BODY + " (older version)"))
    assert err.value.code == "draft_changed" and err.value.status == 409
    gmail.service.users().messages().send.assert_not_called()
    assert LEDGER not in db.store


@pytest.mark.unit
def test_blocked_drafts_are_refused():
    db, gmail = make_db(emailBody="Hi [Name]"), make_gmail()
    with pytest.raises(fes.SendError) as err:
        send(db, gmail, fes.draft_hash(SUBJECT, "Hi [Name]"))
    assert err.value.status == 422 and err.value.extra["blocking"] == ["placeholder"]


@pytest.mark.unit
def test_the_daily_cap_is_server_side():
    db, gmail = make_db(), make_gmail()
    db.store["users/u1"]["dailySendCount"] = 0         # client-editable; ignored
    db.store[QUOTA] = {"count": fes.DAILY_SEND_CAP}
    db.times[QUOTA] = 1
    with pytest.raises(fes.SendError) as err:
        send(db, gmail)
    assert err.value.code == "daily_cap_reached" and err.value.status == 429
    gmail.service.users().messages().send.assert_not_called()
    assert db.store[LEDGER]["status"] == "failed"


@pytest.mark.unit
def test_concurrent_send_is_refused():
    db, gmail = make_db(), make_gmail()
    db.store[LEDGER] = {"status": "sending", "startedAt": (NOW - timedelta(seconds=10)).isoformat(),
                        "messageIdHeader": "<x@offerloop.ai>"}
    db.times[LEDGER] = 1
    with pytest.raises(fes.SendError) as err:
        send(db, gmail)
    assert err.value.code == "send_in_progress"
    gmail.service.users().messages().send.assert_not_called()


def _stale_attempt(db):
    db.store[LEDGER] = {"status": "sending", "startedAt": (NOW - timedelta(minutes=5)).isoformat(),
                        "messageIdHeader": "<abc@offerloop.ai>"}
    db.times[LEDGER] = 1


@pytest.mark.unit
def test_an_interrupted_send_that_reached_gmail_is_recorded_not_resent():
    db = make_db()
    _stale_attempt(db)
    gmail = make_gmail(found={"id": "m-earlier", "threadId": "t-earlier"})
    result = send(db, gmail)
    assert result["messageId"] == "m-earlier"
    gmail.service.users().messages().list.assert_called_once()
    assert gmail.service.users().messages().list.call_args.kwargs["q"] == "rfc822msgid:abc@offerloop.ai"
    gmail.service.users().messages().send.assert_not_called()
    assert db.store[CONTACT]["emailSent"] is True


@pytest.mark.unit
def test_an_interrupted_send_that_never_arrived_is_sent_now():
    db = make_db()
    _stale_attempt(db)
    gmail = make_gmail()
    assert send(db, gmail)["messageId"] == "m1"
    assert gmail.service.users().messages().send.call_count == 1


@pytest.mark.unit
def test_without_read_access_the_user_checks_sent_first():
    db = make_db()
    _stale_attempt(db)
    gmail = make_gmail(scopes=(COMPOSE,))
    with pytest.raises(fes.SendError) as err:
        send(db, gmail)
    assert err.value.code == "send_unconfirmed"
    gmail.service.users().messages().send.assert_not_called()
    assert send(db, gmail, confirm_not_sent=True)["sent"] is True


@pytest.mark.unit
def test_a_failed_send_can_be_retried_and_gives_back_the_slot():
    db, gmail = make_db(), make_gmail()
    gmail.service.users().messages().send.return_value.execute.side_effect = [Exception("backend error"), {"id": "m2", "threadId": "t2"}]
    with pytest.raises(fes.SendError) as err:
        send(db, gmail)
    assert err.value.code == "send_failed"
    assert db.store[LEDGER]["status"] == "failed"
    assert db.store[QUOTA]["count"] == 0
    assert db.store[CONTACT].get("emailSent") is None
    assert send(db, gmail)["messageId"] == "m2"


@pytest.mark.unit
def test_a_timeout_is_treated_as_maybe_sent():
    db, gmail = make_db(), make_gmail()
    gmail.service.users().messages().send.return_value.execute.side_effect = TimeoutError("timed out")
    with pytest.raises(fes.SendError) as err:
        send(db, gmail)
    assert err.value.code == "send_failed"
    # Left "sending": the next try looks for it in the mailbox first.
    assert db.store[LEDGER]["status"] == "sending"


@pytest.mark.unit
def test_expired_gmail_access():
    db, gmail = make_db(), make_gmail()

    class RefreshError(Exception):
        pass

    gmail.service.users().messages().send.return_value.execute.side_effect = RefreshError("invalid_grant")
    with pytest.raises(fes.SendError) as err:
        send(db, gmail)
    assert err.value.code == "gmail_token_expired" and err.value.status == 401
    assert db.store[LEDGER]["status"] == "failed"


@pytest.mark.unit
def test_connected_without_send_permission():
    creds = MagicMock(scopes=[READONLY])
    with patch("app.services.gmail_client._load_user_gmail_creds", return_value=creds):
        with pytest.raises(fes.SendError) as err:
            fes.default_gmail("u1")
    assert err.value.code == "gmail_scope_missing" and err.value.status == 403


@pytest.mark.unit
def test_other_surfaces_dont_touch_the_walkthrough():
    db, gmail = make_db(), make_gmail()
    send(db, gmail, surface="inbox")
    assert db.store["users/u1"]["walkthrough"]["status"] == "drafted"


# ── Route ────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from app.routes import contacts
    monkeypatch.setitem(firebase_admin._apps, "[DEFAULT]", object())
    monkeypatch.setattr("app.extensions.fb_auth.verify_id_token", lambda *a, **k: {"uid": "u1"})
    db = make_db()
    monkeypatch.setattr(contacts, "get_db", lambda: db)
    app = Flask(__name__)
    app.register_blueprint(contacts.contacts_bp)
    return app.test_client(), db


@pytest.mark.unit
def test_route_dry_run_reviews_without_sending(client):
    c, db = client
    resp = c.post("/api/contacts/ranker_c1/send-first-email", json={"dry_run": True}, headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["recipient"] == "pat@evercore.com" and body["hash"] == fes.draft_hash(SUBJECT, BODY)
    assert body["blocking"] == [] and body["alreadySent"] is False
    assert LEDGER not in db.store


@pytest.mark.unit
def test_route_maps_gmail_not_connected(client):
    c, db = client
    resp = c.post("/api/contacts/ranker_c1/send-first-email",
                  json={"confirmed_hash": fes.draft_hash(SUBJECT, BODY), "surface": "web_walkthrough"},
                  headers={"Authorization": "Bearer x"})
    assert resp.status_code == 401 and resp.get_json() == {"error": "gmail_not_connected"}
    assert LEDGER not in db.store


@pytest.mark.unit
def test_route_requires_auth(client):
    c, _ = client
    assert c.post("/api/contacts/ranker_c1/send-first-email", json={}).status_code == 401
