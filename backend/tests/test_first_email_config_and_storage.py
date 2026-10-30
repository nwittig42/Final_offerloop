"""Configuration handling and resume storage for the signup -> first email
journey: clear errors when an integration isn't configured, the confirmed
profile reaching the email writer, download tokens minted at upload, and the
local-emulator guard. In-process only; nothing remote is called.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import firebase_admin
import pytest
from flask import Flask

from tests.fake_firestore import FakeDB


# ── Gmail OAuth start without a client ───────────────────────────────────

@pytest.fixture
def oauth_client(monkeypatch):
    from app.routes import gmail_oauth
    monkeypatch.setitem(firebase_admin._apps, "[DEFAULT]", object())
    monkeypatch.setattr("app.extensions.fb_auth.verify_id_token", lambda *a, **k: {"uid": "u1", "email": "a@b.edu"})
    db = FakeDB({"users/u1": {"email": "a@b.edu"}})
    monkeypatch.setattr(gmail_oauth, "get_db", lambda: db)
    app = Flask(__name__)
    app.register_blueprint(gmail_oauth.gmail_oauth_bp)
    return app.test_client(), db


@pytest.mark.unit
@pytest.mark.parametrize("unset", ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"])
def test_oauth_start_says_when_gmail_is_not_configured(oauth_client, monkeypatch, unset):
    client, db = oauth_client
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv(unset, "")
    resp = client.get("/api/google/oauth/start?return_to=/get-started", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "gmail_oauth_not_configured"
    assert not any(p.startswith("oauth_state/") for p in db.store)


@pytest.mark.unit
def test_oauth_start_still_works_when_configured(oauth_client, monkeypatch):
    client, db = oauth_client
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    resp = client.get("/api/google/oauth/start?return_to=/get-started", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 200
    assert "client_id=cid.apps.googleusercontent.com" in resp.get_json()["authUrl"]
    state = [p for p in db.store if p.startswith("oauth_state/")]
    assert len(state) == 1 and db.store[state[0]]["return_to"] == "/get-started"


# ── Config helpers ───────────────────────────────────────────────────────

@pytest.mark.unit
def test_frontend_base_url_override(monkeypatch):
    from app.config import get_frontend_redirect_uri
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.delenv("FRONTEND_BASE_URL", raising=False)
    assert get_frontend_redirect_uri() == "http://localhost:8080/signin"
    monkeypatch.setenv("FRONTEND_BASE_URL", "https://staging.example.test/")
    assert get_frontend_redirect_uri() == "https://staging.example.test/signin"
    monkeypatch.delenv("FRONTEND_BASE_URL")
    monkeypatch.setenv("FLASK_ENV", "production")
    assert get_frontend_redirect_uri() == "https://offerloop.ai/signin"


@pytest.mark.unit
def test_integration_status_reports_names_only(monkeypatch):
    from app.config import integration_status
    for k in ("OPENAI_API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.setenv(k, "")
    status = integration_status()
    assert status["resume parsing + email writing (OpenAI)"] is False
    assert status["Gmail connect + send (Google OAuth client)"] is False
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "x")
    assert integration_status()["Gmail connect + send (Google OAuth client)"] is False  # both needed
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "y")
    assert integration_status()["Gmail connect + send (Google OAuth client)"] is True
    assert all(isinstance(v, bool) for v in status.values())


# ── Outreach: no model configured, and the confirmed profile ─────────────

@pytest.fixture
def outreach(monkeypatch):
    from app.routes import ranker
    from app.services.firm_cache.schema import FIRM_EMPLOYEES_COLLECTION
    monkeypatch.setitem(firebase_admin._apps, "[DEFAULT]", object())
    monkeypatch.setattr("app.extensions.fb_auth.verify_id_token", lambda *a, **k: {"uid": "u1", "email": "a@usc.edu"})
    db = FakeDB({
        "users/u1": {
            "email": "a@usc.edu",
            "onboarding": {"profileConfirmedAt": "2026-11-02T00:00:00Z"},
            "profile": {"fullName": "Avery Lee"},
            "academics": {"university": "University of Southern California", "major": "Economics and Data Science",
                          "graduationYear": "2027"},
            "resumeParsed": {"name": "Avery L", "education": {"university": "USC", "major": "Economics", "graduation": "May 2026"},
                             "experience": [{"title": "Summer Analyst", "company": "Bank"}]},
            "resumeText": "x" * 200,
        },
        "users/u1/revealedEmails/c1": {"email": "pat@evercore.com", "verified": True,
                                       "revealed_at": datetime.now(timezone.utc).isoformat()},
        f"{FIRM_EMPLOYEES_COLLECTION}/c1": {"first_name": "Pat", "last_name": "Kim", "company_display": "Evercore"},
    })
    monkeypatch.setattr(ranker, "get_db", lambda: db)
    monkeypatch.setattr(ranker, "log_recommendation_event", MagicMock())
    monkeypatch.setattr(ranker.people_deck_cache, "remove", MagicMock())
    gen = MagicMock(return_value={0: {"subject": "Hi", "body": "Body"}})
    with patch("app.services.firm_cache.reader.firm_employee_doc_to_app_contact",
               return_value={"FirstName": "Pat", "LastName": "Kim", "Company": "Evercore"}), \
         patch("app.routes.runs._resolve_email_template", return_value=(None, None, None, None)), \
         patch("app.services.reply_generation.batch_generate_emails", gen), \
         patch("app.services.gmail_client._load_user_gmail_creds", return_value=None):
        app = Flask(__name__)
        app.register_blueprint(ranker.ranker_bp)
        yield app.test_client(), db, gen


def _draft(client):
    return client.post("/api/ranker/candidates/c1/outreach", json={"action": "draft", "create_gmail_draft": False},
                       headers={"Authorization": "Bearer x"})


@pytest.mark.unit
def test_outreach_says_when_no_model_is_configured(outreach):
    client, db, gen = outreach
    with patch("app.services.openai_client.get_openai_client", return_value=None), \
         patch("app.services.openai_client.get_anthropic_client", return_value=None):
        resp = _draft(client)
    assert resp.status_code == 503
    assert resp.get_json() == {"error": "email_generation_not_configured"}
    gen.assert_not_called()
    assert "users/u1/contacts/ranker_c1" not in db.store


@pytest.mark.unit
def test_confirmed_profile_reaches_the_email_writer(outreach):
    client, db, gen = outreach
    with patch("app.services.openai_client.get_openai_client", return_value=object()):
        assert _draft(client).status_code == 200
    kwargs = gen.call_args.kwargs
    # No re-parse of the resume text: the confirmed profile is the source.
    assert kwargs["resume_text"] is None
    assert kwargs["user_profile"]["major"] == "Economics and Data Science"
    assert kwargs["user_profile"]["university"] == "University of Southern California"
    assert kwargs["user_profile"]["name"] == "Avery Lee"
    edu = kwargs["pre_parsed_user_info"]["education"]
    assert edu == {"university": "University of Southern California", "major": "Economics and Data Science", "graduation": "2027"}
    # Everything else from the stored parse is kept.
    assert kwargs["pre_parsed_user_info"]["experience"] == [{"title": "Summer Analyst", "company": "Bank"}]


@pytest.mark.unit
def test_not_in_school_means_no_school_in_the_email(outreach):
    client, db, gen = outreach
    db.store["users/u1"]["universityOptOut"] = True
    with patch("app.services.openai_client.get_openai_client", return_value=object()):
        _draft(client)
    kwargs = gen.call_args.kwargs
    assert kwargs["user_profile"]["university"] == ""
    assert kwargs["pre_parsed_user_info"]["education"]["university"] == ""


@pytest.mark.unit
def test_unconfirmed_users_keep_the_existing_inputs(outreach):
    client, db, gen = outreach
    del db.store["users/u1"]["onboarding"]
    with patch("app.services.openai_client.get_openai_client", return_value=object()):
        _draft(client)
    kwargs = gen.call_args.kwargs
    assert kwargs["resume_text"] == "x" * 200
    assert kwargs["pre_parsed_user_info"]["education"]["major"] == "Economics"


# ── Resume storage ───────────────────────────────────────────────────────

class _Blob:
    def __init__(self):
        self.name = "resumes/u1/cv.pdf"
        self.bucket = MagicMock()
        self.bucket.name = "offerloop-native.firebasestorage.app"
        self.metadata = None
        self.uploaded_with = None
        self.patched = False

    def upload_from_string(self, data, content_type=None):
        self.uploaded_with = dict(self.metadata or {})

    def reload(self):
        pass

    def patch(self):
        self.patched = True


@pytest.mark.unit
def test_download_token_is_minted_with_the_upload(monkeypatch):
    from app.services import resume_storage
    monkeypatch.delenv("STORAGE_EMULATOR_HOST", raising=False)
    blob = _Blob()
    bucket = MagicMock()
    bucket.blob.return_value = blob
    monkeypatch.setattr(resume_storage, "_bucket", lambda: bucket)
    monkeypatch.setattr(resume_storage, "render_thumbnail", lambda data: None)
    out = resume_storage.store_resume("u1", "cv.pdf", b"%PDF", "application/pdf")
    token = blob.uploaded_with["firebaseStorageDownloadTokens"]
    assert token and not blob.patched  # one write, no follow-up metadata patch
    assert out["url"] == (
        "https://firebasestorage.googleapis.com/v0/b/offerloop-native.firebasestorage.app/o/"
        f"resumes%2Fu1%2Fcv.pdf?alt=media&token={token}"
    )


@pytest.mark.unit
def test_links_point_at_the_storage_emulator_when_testing(monkeypatch):
    from app.services import resume_storage
    monkeypatch.setenv("STORAGE_EMULATOR_HOST", "http://127.0.0.1:9199")
    blob = _Blob()
    blob.metadata = {"firebaseStorageDownloadTokens": "tok"}
    url = resume_storage.token_url(blob)
    assert url.startswith("http://127.0.0.1:9199/v0/b/offerloop-native.firebasestorage.app/o/resumes%2Fu1%2Fcv.pdf")
    assert resume_storage.storage_path_from_url(url) == "resumes/u1/cv.pdf"


@pytest.mark.unit
def test_existing_link_parsing_is_unchanged(monkeypatch):
    from app.services import resume_storage
    monkeypatch.delenv("STORAGE_EMULATOR_HOST", raising=False)
    assert resume_storage.storage_path_from_url(
        "https://firebasestorage.googleapis.com/v0/b/b/o/resumes%2Fu1%2Fcv.pdf?alt=media&token=t") == "resumes/u1/cv.pdf"
    assert resume_storage.storage_path_from_url("https://storage.googleapis.com/b/resumes/u1/cv.pdf") == "resumes/u1/cv.pdf"
    assert resume_storage.storage_path_from_url("http://127.0.0.1:9199/v0/b/b/o/x") is None


# ── Local emulator guard ─────────────────────────────────────────────────

@pytest.mark.unit
def test_emulator_mode_refuses_to_start_without_every_emulator(monkeypatch):
    from app import extensions
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8180")
    monkeypatch.setenv("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099")
    monkeypatch.delenv("STORAGE_EMULATOR_HOST", raising=False)
    with pytest.raises(RuntimeError, match="STORAGE_EMULATOR_HOST"):
        extensions._init_firebase_emulator()


@pytest.mark.unit
def test_emulator_mode_refuses_real_credentials(monkeypatch):
    from app import extensions
    for k, v in (("FIRESTORE_EMULATOR_HOST", "h:1"), ("FIREBASE_AUTH_EMULATOR_HOST", "h:2"),
                 ("STORAGE_EMULATOR_HOST", "http://h:3"), ("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa.json")):
        monkeypatch.setenv(k, v)
    with pytest.raises(RuntimeError, match="GOOGLE_APPLICATION_CREDENTIALS"):
        extensions._init_firebase_emulator()


@pytest.mark.unit
def test_emulator_credential_is_anonymous():
    from google.auth.credentials import AnonymousCredentials
    from app.extensions import _AnonymousCredential
    assert isinstance(_AnonymousCredential().get_credential(), AnonymousCredentials)
