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
