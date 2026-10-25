"""POST /api/contacts/<id>/gmail-draft: copy the saved in-app draft into Gmail drafts.

In-process only: Firestore is tests.fake_firestore; the Gmail creds loader and
the draft creator are patched, so nothing remote is called.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import firebase_admin
import pytest
from flask import Flask

from tests.fake_firestore import FakeDB

CONTACT = "users/u1/contacts/ranker_c1"
COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
READONLY = "https://www.googleapis.com/auth/gmail.readonly"
URL = "/api/contacts/ranker_c1/gmail-draft"
HEADERS = {"Authorization": "Bearer x"}


def make_db(**contact):
    return FakeDB({
        "users/u1": {"email": "ana@usc.edu", "subscriptionTier": "free", "resumeUrl": "https://r.example/ana.pdf",
                     "resumeFileName": "ana.pdf"},
        CONTACT: {"firstName": "Pat", "lastName": "Kim", "company": "Evercore", "jobTitle": "Analyst",
                  "emailSubject": "Evercore question", "emailBody": "Hi Pat, quick question.",
                  "draftToEmail": "pat@evercore.com", "pipelineStage": "generated", "draftStillExists": False,
                  **contact},
    })


@pytest.fixture
def setup(monkeypatch):
    from app.routes import contacts
    monkeypatch.setitem(firebase_admin._apps, "[DEFAULT]", object())
    monkeypatch.setattr("app.extensions.fb_auth.verify_id_token",
                        lambda *a, **k: {"uid": "u1", "email": "ana@usc.edu"})
    state = SimpleNamespace(db=make_db(), creds=SimpleNamespace(scopes=[COMPOSE]),
                            creator=MagicMock(return_value={"draft_id": "d1", "message_id": "m1",
                                                            "draft_url": "https://mail.google.com/mail/u/0/#draft/d1"}))
    monkeypatch.setattr(contacts, "get_db", lambda: state.db)
    monkeypatch.setattr(contacts, "_load_user_gmail_creds", lambda uid: state.creds)
    monkeypatch.setattr("app.services.gmail_client.create_gmail_draft_for_user",
                        lambda *a, **k: state.creator(*a, **k))
    monkeypatch.setattr("app.services.gmail_client.download_resume_from_url",
                        lambda url: (b"%PDF", "ana.pdf"))
    app = Flask(__name__)
    app.register_blueprint(contacts.contacts_bp)
    state.client = app.test_client()
    return state


@pytest.mark.unit
def test_creates_the_draft_and_writes_the_four_fields(setup):
    resp = setup.client.post(URL, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.get_json() == {"gmailDraftId": "d1", "gmailDraftUrl": "https://mail.google.com/mail/u/0/#draft/d1",
                               "created": True}
    doc = setup.db.store[CONTACT]
    assert doc["gmailDraftId"] == "d1" and doc["gmailDraftUrl"].endswith("#draft/d1")
    assert doc["pipelineStage"] == "draft_created" and doc["draftStillExists"] is True
    args, kwargs = setup.creator.call_args
    assert args[0]["Email"] == "pat@evercore.com"
    assert args[1:3] == ("Evercore question", "Hi Pat, quick question.")
    assert kwargs["resume_content"] == b"%PDF" and kwargs["resume_filename"] == "ana.pdf"
    assert kwargs["user_id"] == "u1"


@pytest.mark.unit
def test_existing_draft_is_not_recreated(setup, monkeypatch):
    from app.routes import contacts
    db = make_db(gmailDraftId="old", gmailDraftUrl="https://mail.google.com/x")
    monkeypatch.setattr(contacts, "get_db", lambda: db)
    resp = setup.client.post(URL, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.get_json() == {"gmailDraftId": "old", "gmailDraftUrl": "https://mail.google.com/x", "created": False}
    setup.creator.assert_not_called()


@pytest.mark.unit
def test_no_creds_is_409_not_connected(setup):
    setup.creds = None
    resp = setup.client.post(URL, headers=HEADERS)
    assert resp.status_code == 409 and resp.get_json() == {"error": "gmail_not_connected"}
    setup.creator.assert_not_called()


@pytest.mark.unit
def test_creds_without_a_compose_scope_is_409_scope_missing(setup):
    setup.creds = SimpleNamespace(scopes=[READONLY])
    resp = setup.client.post(URL, headers=HEADERS)
    assert resp.status_code == 409 and resp.get_json() == {"error": "gmail_scope_missing"}
    setup.creator.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("missing", ["emailBody", "draftToEmail"])
def test_no_saved_draft_is_400(setup, monkeypatch, missing):
    from app.routes import contacts
    db = make_db(**{missing: ""})
    monkeypatch.setattr(contacts, "get_db", lambda: db)
    resp = setup.client.post(URL, headers=HEADERS)
    assert resp.status_code == 400 and resp.get_json() == {"error": "no_saved_draft"}
    setup.creator.assert_not_called()


@pytest.mark.unit
def test_unknown_contact_is_404(setup):
    resp = setup.client.post("/api/contacts/nope/gmail-draft", headers=HEADERS)
    assert resp.status_code == 404 and resp.get_json() == {"error": "contact_not_found"}


@pytest.mark.unit
def test_requires_auth(setup):
    assert setup.client.post(URL).status_code == 401


CLAIM = "users/u1/gmailDraftClaims/ranker_c1"


@pytest.mark.unit
def test_fresh_claim_is_409_in_progress_and_gmail_untouched(setup):
    from datetime import datetime
    setup.db.store[CLAIM] = {"claimedAt": datetime.utcnow().isoformat()}
    setup.db.times[CLAIM] = 1
    resp = setup.client.post(URL, headers=HEADERS)
    assert resp.status_code == 409 and resp.get_json() == {"error": "draft_in_progress"}
    setup.creator.assert_not_called()
    assert CLAIM in setup.db.store


@pytest.mark.unit
def test_stale_claim_is_reclaimed(setup):
    from datetime import datetime, timedelta
    setup.db.store[CLAIM] = {"claimedAt": (datetime.utcnow() - timedelta(minutes=3)).isoformat()}
    setup.db.times[CLAIM] = 1
    resp = setup.client.post(URL, headers=HEADERS)
    assert resp.status_code == 200 and resp.get_json()["created"] is True
    setup.creator.assert_called_once()
    assert CLAIM not in setup.db.store


@pytest.mark.unit
def test_success_and_failure_both_clear_the_claim(setup):
    assert setup.client.post(URL, headers=HEADERS).status_code == 200
    assert CLAIM not in setup.db.store
    setup.db.store[CONTACT].pop("gmailDraftId")
    setup.creator.side_effect = RuntimeError("gmail down")
    resp = setup.client.post(URL, headers=HEADERS)
    assert resp.status_code == 502 and resp.get_json() == {"error": "gmail_draft_failed"}
    assert CLAIM not in setup.db.store
    assert "gmailDraftId" not in setup.db.store[CONTACT]


SEND_ONLY = "https://www.googleapis.com/auth/gmail.send"


@pytest.mark.unit
@pytest.mark.parametrize("scopes", [[SEND_ONLY], [], None])
def test_send_only_or_no_scopes_is_409_scope_missing(setup, scopes):
    # gmail.send can't create drafts; unknown scopes can't be trusted to.
    setup.creds = SimpleNamespace(scopes=scopes)
    resp = setup.client.post(URL, headers=HEADERS)
    assert resp.status_code == 409 and resp.get_json() == {"error": "gmail_scope_missing"}
    setup.creator.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("scope", [COMPOSE, "https://www.googleapis.com/auth/gmail.modify", "https://mail.google.com/"])
def test_any_draft_capable_scope_is_enough(setup, scope):
    setup.creds = SimpleNamespace(scopes=[scope])
    assert setup.client.post(URL, headers=HEADERS).status_code == 200


@pytest.mark.unit
def test_suppressed_recipient_is_422(setup):
    setup.creator.return_value = "suppressed_free_draft_pat"
    resp = setup.client.post(URL, headers=HEADERS)
    assert resp.status_code == 422 and resp.get_json() == {"error": "recipient_suppressed"}
    assert "gmailDraftId" not in setup.db.store[CONTACT]
    assert CLAIM not in setup.db.store


@pytest.mark.unit
def test_email_source_is_passed_to_gmail(setup):
    setup.db.store[CONTACT]["emailSource"] = "hunter"
    setup.client.post(URL, headers=HEADERS)
    assert setup.creator.call_args[0][0]["EmailSource"] == "hunter"
    setup.db.store[CONTACT].pop("gmailDraftId")
    setup.db.store[CONTACT].pop("emailSource")
    setup.client.post(URL, headers=HEADERS)
    assert setup.creator.call_args[0][0]["EmailSource"] == ""


def _fail_contact_set(db, monkeypatch):
    from tests import fake_firestore
    real = fake_firestore.Doc.set

    def flaky(self, data, merge=False):
        if self.path == CONTACT and "gmailDraftId" in data:
            raise RuntimeError("firestore down")
        return real(self, data, merge=merge)
    monkeypatch.setattr(fake_firestore.Doc, "set", flaky)
    return lambda: monkeypatch.setattr(fake_firestore.Doc, "set", real)


@pytest.mark.unit
def test_failed_contact_write_keeps_the_claim_and_a_retry_reuses_the_draft(setup, monkeypatch):
    restore = _fail_contact_set(setup.db, monkeypatch)
    resp = setup.client.post(URL, headers=HEADERS)
    assert resp.status_code == 502
    claim = setup.db.store[CLAIM]
    assert claim["draftId"] == "d1" and claim["draftUrl"].endswith("#draft/d1")
    assert "gmailDraftId" not in setup.db.store[CONTACT]

    restore()
    resp = setup.client.post(URL, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.get_json() == {"gmailDraftId": "d1", "gmailDraftUrl": "https://mail.google.com/mail/u/0/#draft/d1",
                               "created": False}
    setup.creator.assert_called_once()
    doc = setup.db.store[CONTACT]
    assert doc["gmailDraftId"] == "d1" and doc["pipelineStage"] == "draft_created"
    assert CLAIM not in setup.db.store


@pytest.mark.unit
def test_a_claim_taken_over_by_another_call_is_not_deleted(setup):
    # While Gmail is drafting, a newer claim replaces ours; we must leave it.
    def creator(*a, **k):
        setup.db.store[CLAIM] = {"claimedAt": "someone-else"}
        return {"draft_id": "d1", "draft_url": "u"}
    setup.creator.side_effect = creator
    assert setup.client.post(URL, headers=HEADERS).status_code == 200
    assert setup.db.store[CLAIM]["claimedAt"] == "someone-else"
