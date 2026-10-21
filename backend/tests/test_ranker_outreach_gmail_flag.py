"""POST /api/ranker/candidates/<id>/outreach: create_gmail_draft=false.

The first-use walkthrough drafts in the app only, so the user can edit the
email before it reaches Gmail. Every other caller keeps the default (a Gmail
draft when Gmail is connected). Runs in-process: auth, Firestore, the LLM
and Gmail are all fakes.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import firebase_admin
import pytest
from flask import Flask

from app.routes import ranker
from app.routes.ranker import ranker_bp


class _Snap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class _Doc:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def collection(self, name):
        return _Col(self.store, f"{self.path}/{name}")

    def get(self, **_):
        return _Snap(self.store.get(self.path))

    def set(self, data, merge=False):
        self.store[self.path] = {**(self.store.get(self.path) or {}), **data} if merge else dict(data)


class _Col:
    def __init__(self, store, path):
        self.store, self.path = store, path

    def document(self, doc_id):
        return _Doc(self.store, f"{self.path}/{doc_id}")


class FakeDB:
    def __init__(self, store):
        self.store = store

    def collection(self, name):
        return _Col(self.store, name)


def _store():
    from app.services.firm_cache.schema import FIRM_EMPLOYEES_COLLECTION
    return {
        "users/u1": {"email": "ana@umich.edu", "subscriptionTier": "free"},
        "users/u1/revealedEmails/c1": {
            "email": "pat@evercore.com", "verified": True, "source": "warehouse",
            "revealed_at": datetime.now(timezone.utc).isoformat(),
        },
        f"{FIRM_EMPLOYEES_COLLECTION}/c1": {"first_name": "Pat", "last_name": "Kim", "company_display": "Evercore"},
    }


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setitem(firebase_admin._apps, "[DEFAULT]", object())
    monkeypatch.setattr("app.extensions.fb_auth.verify_id_token",
                        lambda *a, **k: {"uid": "u1", "email": "ana@umich.edu"})
    store = _store()
    monkeypatch.setattr(ranker, "get_db", lambda: FakeDB(store))
    monkeypatch.setattr(ranker, "log_recommendation_event", MagicMock())
    monkeypatch.setattr(ranker.people_deck_cache, "remove", MagicMock())
    drafts = MagicMock(return_value=[{"draft_id": "r-123", "draft_url": "https://mail.google.com/x"}])
    contact = {"FirstName": "Pat", "LastName": "Kim", "Company": "Evercore", "Title": "Analyst"}
    with patch("app.services.firm_cache.reader.firm_employee_doc_to_app_contact", return_value=dict(contact)), \
         patch("app.routes.runs._resolve_email_template", return_value=(None, None, None, None)), \
         patch("app.services.reply_generation.batch_generate_emails",
               return_value={0: {"subject": "Coffee chat?", "body": "Hi Pat, ..."}}), \
         patch("app.services.gmail_client._load_user_gmail_creds", return_value=object()), \
         patch("app.services.gmail_client.create_drafts_parallel", drafts):
        app = Flask(__name__)
        app.register_blueprint(ranker_bp)
        yield app.test_client(), store, drafts


def _post(client, body):
    return client.post("/api/ranker/candidates/c1/outreach", json=body, headers={"Authorization": "Bearer x"})


@pytest.mark.unit
def test_default_still_creates_the_gmail_draft(env):
    client, store, drafts = env
    resp = _post(client, {"action": "draft"})
    assert resp.status_code == 200, resp.get_json()
    drafts.assert_called_once()
    doc = store["users/u1/contacts/ranker_c1"]
    assert doc["gmailDraftId"] == "r-123"
    assert doc["pipelineStage"] == "draft_created"


@pytest.mark.unit
def test_walkthrough_keeps_the_draft_in_app_only(env):
    client, store, drafts = env
    resp = _post(client, {"action": "draft", "create_gmail_draft": False, "surface": "web_walkthrough"})
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json() == {"drafted": True, "sent": False}
    drafts.assert_not_called()
    doc = store["users/u1/contacts/ranker_c1"]
    assert doc["emailSubject"] == "Coffee chat?"
    assert doc["emailBody"] == "Hi Pat, ..."
    assert "gmailDraftId" not in doc
    assert doc["pipelineStage"] == "generated"
    assert ranker.log_recommendation_event.call_args.kwargs["surface"] == "web_walkthrough"


@pytest.mark.unit
def test_no_reveal_is_still_refused(env):
    client, store, drafts = env
    del store["users/u1/revealedEmails/c1"]
    resp = _post(client, {"action": "draft", "create_gmail_draft": False})
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "no_verified_email"}
    drafts.assert_not_called()
