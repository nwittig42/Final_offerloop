"""End-to-end backend journey for the onboarding-v2 walkthrough. Real Flask
routes, one shared in-memory Firestore, and mocked vendors: no network, no
paid APIs, no Gmail, no production data. (The browser-level version runs
against the Firebase emulators: scripts/e2e-emulator.)

    first-outreach status -> reveal-email (free) -> outreach (in-app draft)
    -> client edits the draft -> send without Gmail (401) -> OAuth callback
    (connected / scopes_declined) -> dry run -> stale hash (409) -> send
    -> retry (no duplicate) -> contact stage, walkthrough, Inbox fields
plus: a failed lookup keeps the benefit, cutoff unset stays off, an
interrupted send is found in the mailbox instead of resent.
"""
import base64
import email
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import firebase_admin
import pytest
from flask import Flask

from app.services import first_email_send as fes
from app.services import first_outreach as fo
from app.services.firm_cache.schema import FIRM_EMPLOYEES_COLLECTION
from tests.fake_firestore import FakeDB

NEW = datetime(2026, 11, 2, tzinfo=timezone.utc)
AUTH = {"Authorization": "Bearer x"}
COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
READONLY = "https://www.googleapis.com/auth/gmail.readonly"
GENERATED_SUBJECT = "Evercore question from a USC student"
GENERATED_BODY = ("Hi Pat,\n\nI'm a sophomore at USC studying economics and recruiting for banking. "
                  "I saw you moved from Michigan to Evercore. Would you be open to a 15-minute call?\n\nBest,\nAna")
EDITED_BODY = GENERATED_BODY.replace("Would you be open", "I'd be grateful for your advice. Would you be open")


@pytest.fixture
def env(monkeypatch):
    from app.routes import contacts, gmail_oauth, ranker

    monkeypatch.setitem(firebase_admin._apps, "[DEFAULT]", object())
    monkeypatch.setattr("app.extensions.fb_auth.verify_id_token",
                        lambda *a, **k: {"uid": "u1", "email": "ana@usc.edu", "name": "Ana Lee"})
    monkeypatch.setenv(fo.CUTOFF_ENV, "2026-11-01")
    db = FakeDB({
        "users/u1": {"email": "ana@usc.edu", "name": "Ana Lee", "credits": 300,
                     "walkthrough": {"status": "pending", "createdAt": "2026-11-02T00:00:00Z"}},
        f"{FIRM_EMPLOYEES_COLLECTION}/c1": {"first_name": "Pat", "last_name": "Kim", "company_display": "Evercore",
                                            "linkedin_url": "https://linkedin.com/in/pat"},
        f"{FIRM_EMPLOYEES_COLLECTION}/c2": {"first_name": "Sam", "last_name": "Lee", "company_display": "Lazard",
                                            "linkedin_url": "https://linkedin.com/in/sam"},
    })
    for mod in (ranker, contacts, gmail_oauth):
        monkeypatch.setattr(mod, "get_db", lambda: db)
    monkeypatch.setattr(ranker, "log_recommendation_event", MagicMock())
    monkeypatch.setattr(ranker.people_deck_cache, "remove", MagicMock())
    monkeypatch.setattr(fo, "account_created_at", lambda uid: NEW)

    gmail = MagicMock()
    gmail.users.return_value.getProfile.return_value.execute.return_value = {"emailAddress": "ana@usc.edu"}
    gmail.users.return_value.messages.return_value.send.return_value.execute.return_value = {"id": "m1", "threadId": "t1"}
    gmail.users.return_value.messages.return_value.list.return_value.execute.return_value = {}
    state = {"creds": None}
    m = {
        "deduct": MagicMock(return_value=(True, 292)),
        "hunter": MagicMock(return_value=None),
        "fullenrich": MagicMock(return_value=None),
        "llm": MagicMock(return_value={0: {"subject": GENERATED_SUBJECT, "body": GENERATED_BODY}}),
        "gmail_drafts": MagicMock(return_value=[{"draft_id": "should-not-happen"}]),
        "log_event": MagicMock(),
        "gmail": gmail,
        "state": state,
    }
    patches = [
        patch("app.services.auth.deduct_credits_atomic", m["deduct"]),
        patch.object(ranker, "_try_refund", MagicMock()),
        patch("app.services.email_fast.hunter_email", m["hunter"]),
        patch("app.services.fullenrich_client.find_work_email", m["fullenrich"]),
        patch("app.services.linkedin_backfill.find_linkedin_for_contact", MagicMock(return_value=None)),
        patch("app.services.firm_cache.reader.firm_employee_doc_to_app_contact",
              side_effect=lambda d: {"FirstName": d["first_name"], "LastName": d["last_name"],
                                     "Company": d["company_display"], "Title": "Analyst"}),
        patch("app.routes.runs._resolve_email_template", return_value=(None, "coffee_chat", None, None)),
        patch("app.services.email_request_builder.resolve_email_template", return_value=(None, "coffee_chat", None, None)),
        patch("app.services.reply_generation.batch_generate_emails", m["llm"]),
        patch("app.services.openai_client.get_openai_client", return_value=object()),
        patch("app.services.gmail_client._load_user_gmail_creds", side_effect=lambda uid: state["creds"]),
        patch("app.services.gmail_client._gmail_service", return_value=gmail),
        patch("app.services.gmail_client.create_drafts_parallel", m["gmail_drafts"]),
        patch("app.services.email_tracking.start", return_value=None),
        patch("app.services.email_tracking.register"),
        patch("app.services.cooldown_service.record_outreach"),
        patch("app.utils.metrics_events.log_event", m["log_event"]),
    ]
    for p in patches:
        p.start()
    app = Flask(__name__)
    for bp in (ranker.ranker_bp, contacts.contacts_bp, gmail_oauth.gmail_oauth_bp):
        app.register_blueprint(bp)
    yield app.test_client(), db, m
    for p in patches:
        p.stop()


def _connect_gmail(m, scopes=(COMPOSE, READONLY)):
    m["state"]["creds"] = MagicMock(scopes=list(scopes), valid=True)


def _sent(m, n=-1):
    raw = m["gmail"].users().messages().send.call_args_list[n].kwargs["body"]["raw"]
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    text = " ".join(p.get_payload(decode=True).decode() for p in msg.walk() if p.get_content_maintype() == "text")
    return msg, text


def test_full_journey_free_reveal_draft_edit_connect_send_once(env):
    client, db, m = env

    # 1. Free first outreach is available for this new account.
    assert client.get("/api/ranker/first-outreach", headers=AUTH).get_json() == {"eligible": True, "status": "available"}

    # 2. Pick Pat -> find their email on the free benefit (no credits).
    m["hunter"].return_value = {"email": "pat@evercore.com", "confidence": 99}
    r = client.post("/api/ranker/candidates/c1/reveal-email", json={"use_first_outreach": True, "surface": "web_walkthrough"}, headers=AUTH)
    assert r.status_code == 200 and r.get_json()["free"] is True, r.get_json()
    m["deduct"].assert_not_called()
    assert db.store["users/u1/entitlements/firstOutreach"]["status"] == "consumed"

    # 3. Write the email in-app only, even though Gmail is connected later.
    _connect_gmail(m)
    r = client.post("/api/ranker/candidates/c1/outreach",
                    json={"action": "draft", "create_gmail_draft": False, "surface": "web_walkthrough"}, headers=AUTH)
    assert r.get_json() == {"drafted": True, "sent": False}
    m["gmail_drafts"].assert_not_called()
    contact = db.store["users/u1/contacts/ranker_c1"]
    assert contact["emailBody"] == GENERATED_BODY and contact["pipelineStage"] == "generated"
    m["state"]["creds"] = None  # not connected yet for the send step

    # 4. The user edits the draft (the client writes the contact doc).
    stale_hash = fes.draft_hash(GENERATED_SUBJECT, GENERATED_BODY)
    db.collection("users").document("u1").collection("contacts").document("ranker_c1").update(
        {"emailBody": EDITED_BODY, "emailEditedAt": "2026-11-02T00:05:00Z"})

    # 5. Sending without Gmail is refused, nothing recorded.
    r = client.post("/api/contacts/ranker_c1/send-first-email",
                    json={"confirmed_hash": fes.draft_hash(GENERATED_SUBJECT, EDITED_BODY), "surface": "web_walkthrough"}, headers=AUTH)
    assert r.status_code == 401 and r.get_json()["error"] == "gmail_not_connected"
    assert "users/u1/firstEmailSends/ranker_c1" not in db.store

    # 6. Connect Gmail.
    _connect_gmail(m)

    # 7. Review: recipient is the server-side reveal.
    review = client.post("/api/contacts/ranker_c1/send-first-email", json={"dry_run": True}, headers=AUTH).get_json()
    assert review["recipient"] == "pat@evercore.com" and review["blocking"] == []
    assert review["hash"] == fes.draft_hash(GENERATED_SUBJECT, EDITED_BODY)

    # 8. A confirm based on the old text is refused.
    r = client.post("/api/contacts/ranker_c1/send-first-email", json={"confirmed_hash": stale_hash, "surface": "web_walkthrough"}, headers=AUTH)
    assert r.status_code == 409 and r.get_json()["error"] == "draft_changed"
    m["gmail"].users().messages().send.assert_not_called()

    # 9. Send the reviewed (edited) version.
    r = client.post("/api/contacts/ranker_c1/send-first-email",
                    json={"confirmed_hash": review["hash"], "surface": "web_walkthrough"}, headers=AUTH)
    assert r.status_code == 200 and r.get_json()["sent"] is True and r.get_json()["alreadySent"] is False
    msg, text = _sent(m)
    assert msg["to"] == "pat@evercore.com" and msg["subject"] == GENERATED_SUBJECT
    assert "I'd be grateful for your advice" in text or "I&#x27;d be grateful for your advice" in text

    # 10. A retry (double click, refresh) never sends twice.
    r2 = client.post("/api/contacts/ranker_c1/send-first-email",
                     json={"confirmed_hash": review["hash"], "surface": "web_walkthrough"}, headers=AUTH)
    assert r2.get_json()["alreadySent"] is True
    assert m["gmail"].users().messages().send.call_count == 1

    # 11. Hand-off state: Inbox/Contacts fields, walkthrough done, analytics.
    contact = db.store["users/u1/contacts/ranker_c1"]
    assert contact["emailSent"] is True and contact["pipelineStage"] == "email_sent"
    assert contact["inOutbox"] is True and contact["gmailThreadId"] == "t1"
    assert db.store["users/u1"]["walkthrough"]["status"] == "completed"
    ev = m["log_event"].call_args
    assert ev.args[1] == "first_email_sent" and ev.args[2]["seconds_since_signup"] is not None
    assert "Pat" not in str(ev.args[2]) and "@" not in str(ev.args[2])


def test_failed_lookup_keeps_the_free_benefit_then_next_person_uses_it(env):
    client, db, m = env
    r = client.post("/api/ranker/candidates/c1/reveal-email", json={"use_first_outreach": True}, headers=AUTH)
    assert r.get_json()["email"] is None
    assert "users/u1/entitlements/firstOutreach" not in db.store
    m["hunter"].return_value = {"email": "sam@lazard.com", "confidence": 99}
    r = client.post("/api/ranker/candidates/c2/reveal-email", json={"use_first_outreach": True}, headers=AUTH)
    assert r.get_json()["free"] is True
    assert client.post("/api/ranker/candidates/c1/reveal-email", json={"use_first_outreach": True}, headers=AUTH).status_code == 409
    m["deduct"].assert_not_called()


def test_cutoff_unset_keeps_everything_on_the_paid_path(env, monkeypatch):
    client, db, m = env
    monkeypatch.delenv(fo.CUTOFF_ENV)
    assert client.get("/api/ranker/first-outreach", headers=AUTH).get_json()["eligible"] is False
    r = client.post("/api/ranker/candidates/c1/reveal-email", json={"use_first_outreach": True}, headers=AUTH)
    assert r.status_code == 409
    m["hunter"].assert_not_called()
    m["hunter"].return_value = {"email": "pat@evercore.com", "confidence": 99}
    client.post("/api/ranker/candidates/c1/reveal-email", json={}, headers=AUTH)
    m["deduct"].assert_called_once_with("u1", 8, "reveal_email")


def test_interrupted_send_is_found_not_resent(env):
    client, db, m = env
    m["hunter"].return_value = {"email": "pat@evercore.com", "confidence": 99}
    client.post("/api/ranker/candidates/c1/reveal-email", json={}, headers=AUTH)
    client.post("/api/ranker/candidates/c1/outreach", json={"action": "draft", "create_gmail_draft": False}, headers=AUTH)
    _connect_gmail(m)
    h = client.post("/api/contacts/ranker_c1/send-first-email", json={"dry_run": True}, headers=AUTH).get_json()["hash"]
    m["gmail"].users().messages().send.return_value.execute.side_effect = TimeoutError("timed out")
    r = client.post("/api/contacts/ranker_c1/send-first-email", json={"confirmed_hash": h}, headers=AUTH)
    assert r.status_code == 502
    # Immediate retry: still in flight.
    assert client.post("/api/contacts/ranker_c1/send-first-email", json={"confirmed_hash": h}, headers=AUTH).status_code == 409
    # Later: it actually went out; the mailbox search finds it.
    ledger = db.store["users/u1/firstEmailSends/ranker_c1"]
    ledger["startedAt"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    m["gmail"].users().messages().list.return_value.execute.return_value = {"messages": [{"id": "m-late", "threadId": "t-late"}]}
    r = client.post("/api/contacts/ranker_c1/send-first-email", json={"confirmed_hash": h}, headers=AUTH)
    assert r.status_code == 200 and r.get_json()["messageId"] == "m-late"
    assert m["gmail"].users().messages().send.call_count == 1


def _callback(client, db, monkeypatch, *, fetch_error=None):
    from app.routes import gmail_oauth
    db.store["oauth_state/st1"] = {"uid": "u1", "email": "ana@usc.edu", "return_to": "/get-started?returnTo=%2Fpricing"}
    db.times["oauth_state/st1"] = 1
    flow = MagicMock()
    if fetch_error:
        flow.fetch_token.side_effect = fetch_error
    flow.credentials = MagicMock(scopes=[COMPOSE, READONLY])
    monkeypatch.setattr(gmail_oauth.Flow, "from_client_config", lambda *a, **k: flow)
    monkeypatch.setattr(gmail_oauth, "_save_user_gmail_creds", MagicMock())
    monkeypatch.setattr(gmail_oauth, "_gmail_client_config", lambda: {})
    profile = MagicMock()
    profile.users.return_value.getProfile.return_value.execute.return_value = {"emailAddress": "ana@usc.edu"}
    with patch("googleapiclient.discovery.build", return_value=profile), \
         patch("app.services.gmail_client.start_gmail_watch", MagicMock()):
        return client.get("/api/google/oauth/callback?state=st1&code=abc")


def test_oauth_callback_returns_to_the_walkthrough(env, monkeypatch):
    client, db, m = env
    r = _callback(client, db, monkeypatch)
    assert r.status_code == 302
    assert r.headers["Location"] == "http://localhost:8080/get-started?returnTo=%2Fpricing&connected=gmail"
    assert "oauth_state/st1" not in db.store


def test_oauth_partial_grant_returns_scopes_declined(env, monkeypatch):
    client, db, m = env
    r = _callback(client, db, monkeypatch, fetch_error=Warning('Scope has changed from "a b" to "a".'))
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/get-started?returnTo=%2Fpricing&gmail_error=scopes_declined")


def test_oauth_access_denied_returns_to_the_walkthrough(env, monkeypatch):
    client, db, m = env
    from app.routes import gmail_oauth
    db.store["oauth_state/st2"] = {"uid": "u1", "return_to": "/get-started"}
    db.times["oauth_state/st2"] = 1
    r = client.get("/api/google/oauth/callback?state=st2&error=access_denied")
    assert r.status_code == 302
    assert r.headers["Location"].startswith("http://localhost:8080/get-started?gmail_error=")
