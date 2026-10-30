"""Offerloop backend for isolated end-to-end testing.

Runs the real Flask app (backend/wsgi.py) against the local Firebase Emulator
Suite, with production unreachable:

  * Firestore, Auth and Storage go to the emulators; Firebase Admin uses an
    anonymous credential (OFFERLOOP_E2E_EMULATOR=1, see app/extensions.py).
  * Google Application Default Credentials are disabled, so nothing in the
    process can authenticate to a real Google project.
  * Every paid enrichment vendor key is forced empty; background daemons and
    MCP are off; open-tracking pixels point at this local server.

External services are fakes by default, so the whole journey can run with no
charges and no real email:

  E2E_FAKE_GMAIL=1  OAuth consent page served locally (allow / partial /
                    cancel) and an in-memory Gmail that records sent mail.
                    The real OAuth callback, credential storage, scope checks
                    and send pipeline all still run.
  E2E_FAKE_LLM=1    Deterministic resume parse and email draft (no OpenAI).

Set either to 0 (and put real keys in .env.e2e.local) only for an approved
real test. Usage: see scripts/e2e-emulator/README.md.
"""
from __future__ import annotations

import base64
import email
import itertools
import os
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("E2E_BACKEND_PORT", "5002"))
FRONTEND = os.environ.get("E2E_FRONTEND_URL", "http://127.0.0.1:8081")

# ── Isolation: set before anything imports the backend ───────────────────
ALLOWED_FROM_LOCAL_ENV = {
    "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "OPENAI_API_KEY",
    "FIRST_OUTREACH_FREE_SINCE", "E2E_RECIPIENT_EMAIL", "E2E_FAKE_GMAIL", "E2E_FAKE_LLM",
    "E2E_FAKE_GMAIL_ADDRESS",
}
FORCED_EMPTY = [
    "GOOGLE_APPLICATION_CREDENTIALS", "HUNTER_API_KEY", "FULLENRICH_API_KEY", "APIFY_API_KEY",
    "PEOPLE_DATA_LABS_API_KEY", "CORESIGNAL_API_KEY", "PERPLEXITY_API_KEY", "FIRECRAWL_API_KEY",
    "SERPAPI_KEY", "JINA_API_KEY", "BRIGHTDATA_API_KEY", "BROWSERBASE_API_KEY", "RESEND_API_KEY",
    "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "CLAUDE_API_KEY", "SENTRY_DSN", "PRERENDER_TOKEN",
    "GMAIL_PUBSUB_TOPIC", "NEVERBOUNCE_API_KEY",
]
DAEMON_FLAGS = [
    "NUDGES_ENABLED", "QUEUE_SCANNER_ENABLED", "AGGREGATION_SCANNER_ENABLED", "WATCH_RENEWAL_ENABLED",
    "WATCHDOG_ENABLED", "AGENT_DAEMON_ENABLED", "LOOP_SCHEDULER_ENABLED", "AGENT_FOLLOWUP_ENABLED",
    "AGENT_DIGEST_ENABLED", "FJ_MODIFIED_DAEMON_ENABLED", "FJ_EXPIRED_DAEMON_ENABLED",
    "LOOPS_ALERT_EMAILS_ENABLED", "DISCOVER_ALUMNI_ENABLED",
]


def _load_local_env() -> None:
    """Whitelisted keys from the gitignored .env.e2e.local (repo root)."""
    path = ROOT / ".env.e2e.local"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key in ALLOWED_FROM_LOCAL_ENV:
            os.environ[key] = value


def configure_environment() -> None:
    _load_local_env()
    env = os.environ
    env["OFFERLOOP_E2E_EMULATOR"] = "1"
    env["FIRESTORE_EMULATOR_HOST"] = env.get("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8180")
    env["FIREBASE_AUTH_EMULATOR_HOST"] = env.get("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099")
    env["STORAGE_EMULATOR_HOST"] = env.get("STORAGE_EMULATOR_HOST", "http://127.0.0.1:9199")
    env["FIREBASE_STORAGE_EMULATOR_HOST"] = env["STORAGE_EMULATOR_HOST"].split("://", 1)[-1]
    env["GOOGLE_CLOUD_PROJECT"] = env["GCLOUD_PROJECT"] = "offerloop-native"
    # No Application Default Credentials and no GCE metadata server.
    env["CLOUDSDK_CONFIG"] = str(HERE / ".no-gcloud-config")
    env["NO_GCE_CHECK"] = "True"
    env["GCE_METADATA_HOST"] = env["GCE_METADATA_IP"] = "127.0.0.1:9"
    env["FLASK_ENV"] = "development"
    env["ENVIRONMENT"] = "e2e"
    env["DISABLE_MCP"] = "1"
    env["CREATE_GMAIL_DRAFTS"] = "false"
    env["DECK_PREFETCH_SURFACES"] = ""
    env["EMAIL_TRACKING_BASE_URL"] = f"http://127.0.0.1:{PORT}"
    env["FRONTEND_BASE_URL"] = FRONTEND
    env["OAUTH_REDIRECT_URI"] = f"http://127.0.0.1:{PORT}/api/google/oauth/callback"
    env["CORS_ORIGINS"] = FRONTEND
    for key in FORCED_EMPTY:
        env[key] = ""
    for key in DAEMON_FLAGS:
        env[key] = "false"
    env.setdefault("E2E_FAKE_GMAIL", "1")
    env.setdefault("E2E_FAKE_LLM", "1")
    if env["E2E_FAKE_GMAIL"] == "1":
        env["GOOGLE_CLIENT_ID"] = "e2e-fake-client.apps.googleusercontent.com"
        env["GOOGLE_CLIENT_SECRET"] = "e2e-fake-secret"
    if env["E2E_FAKE_LLM"] == "1":
        env["OPENAI_API_KEY"] = ""


def assert_isolated() -> None:
    """Refuse to start if production could be reached."""
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    for key in ("FIRESTORE_EMULATOR_HOST", "FIREBASE_AUTH_EMULATOR_HOST", "STORAGE_EMULATOR_HOST"):
        if not os.environ.get(key):
            sys.exit(f"[e2e] {key} not set; refusing to start")
    try:
        google.auth.default()
    except DefaultCredentialsError:
        pass
    else:
        sys.exit("[e2e] Google Application Default Credentials are available; refusing to start")
    for key in FORCED_EMPTY:
        if os.environ.get(key):
            sys.exit(f"[e2e] {key} is set; refusing to start")
    print("[e2e] isolation OK: emulators only, no Google credentials, vendor keys empty")


# ── Fake Gmail ────────────────────────────────────────────────────────────
SEND_SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]
_ids = itertools.count(1)
SENT: dict[str, dict] = {}  # message id -> {raw, to, subject, body, threadId, messageIdHeader}


class _Call:
    def __init__(self, result):
        self._result = result

    def execute(self):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result() if callable(self._result) else self._result


class _NotFound(Exception):
    def __init__(self):
        super().__init__("not found")
        self.resp = SimpleNamespace(status=404)


def _message_resource(mid: str) -> dict:
    m = SENT[mid]
    return {
        "id": mid, "threadId": m["threadId"], "labelIds": ["SENT"], "snippet": m["body"][:120],
        "internalDate": str(int(m["sentAt"].timestamp() * 1000)),
        "payload": {"mimeType": "text/plain", "headers": [
            {"name": "To", "value": m["to"]}, {"name": "From", "value": m["from"]},
            {"name": "Subject", "value": m["subject"]}, {"name": "Message-ID", "value": m["messageIdHeader"]},
            {"name": "Date", "value": m["sentAt"].strftime("%a, %d %b %Y %H:%M:%S +0000")},
        ], "body": {"data": base64.urlsafe_b64encode(m["body"].encode()).decode()}},
    }


def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    return " ".join(re.sub(r"<[^>]+>", " ", html).split(" ")).strip()


class _Messages:
    def send(self, userId="me", body=None):
        raw = (body or {}).get("raw", "")
        msg = email.message_from_bytes(base64.urlsafe_b64decode(raw))
        text = " ".join(
            p.get_payload(decode=True).decode("utf-8", "replace")
            for p in msg.walk() if p.get_content_type() == "text/plain"
        ) or " ".join(
            _html_to_text(p.get_payload(decode=True).decode("utf-8", "replace"))
            for p in msg.walk() if p.get_content_type() == "text/html"
        )
        mid = f"e2e-msg-{next(_ids)}"
        SENT[mid] = {
            "to": msg["to"], "from": msg["from"], "subject": msg["subject"], "body": text.strip(),
            "threadId": body.get("threadId") or f"e2e-thread-{mid}", "messageIdHeader": msg["Message-ID"] or "",
            "attachments": [p.get_filename() for p in msg.walk() if p.get_filename()],
            "sentAt": datetime.now(timezone.utc),
        }
        _record(mid)
        print(f"[e2e fake gmail] SENT {mid} to={msg['to']} subject={msg['subject']!r}")
        return _Call({"id": mid, "threadId": SENT[mid]["threadId"], "labelIds": ["SENT"]})

    def list(self, userId="me", q="", **_):
        wanted = q.split("rfc822msgid:", 1)[1].strip() if "rfc822msgid:" in q else None
        hits = [{"id": i, "threadId": m["threadId"]} for i, m in SENT.items()
                if wanted and m["messageIdHeader"].strip("<>") == wanted]
        return _Call({"messages": hits} if hits else {"resultSizeEstimate": 0})

    def get(self, userId="me", id=None, **_):
        return _Call(lambda: _message_resource(id) if id in SENT else (_ for _ in ()).throw(_NotFound()))

    def __getattr__(self, name):
        return lambda *a, **k: _Call({})


class _Threads:
    def get(self, userId="me", id=None, **_):
        msgs = [_message_resource(i) for i, m in SENT.items() if m["threadId"] == id]
        return _Call({"id": id, "messages": msgs} if msgs else _NotFound())

    def __getattr__(self, name):
        return lambda *a, **k: _Call({"threads": []})


class _Drafts:
    deleted: list[str] = []

    def delete(self, userId="me", id=None):
        _Drafts.deleted.append(id)
        return _Call({})

    def get(self, userId="me", id=None, **_):
        return _Call(_NotFound())

    def __getattr__(self, name):
        return lambda *a, **k: _Call({"drafts": []})


class _Users:
    def getProfile(self, userId="me"):
        return _Call({"emailAddress": os.environ.get("E2E_FAKE_GMAIL_ADDRESS", "e2e-sender@example.test"),
                      "messagesTotal": len(SENT)})

    def messages(self):
        return _Messages()

    def threads(self):
        return _Threads()

    def drafts(self):
        return _Drafts()

    def __getattr__(self, name):
        return lambda *a, **k: SimpleNamespace(execute=lambda: {}, list=lambda *a, **k: _Call({}))


class FakeGmailService:
    def users(self):
        return _Users()


def _record(mid: str) -> None:
    """Mirror each fake send into the emulator for inspection (e2e_fake_gmail_sent)."""
    try:
        from backend.app.extensions import get_db
        m = SENT[mid]
        get_db().collection("e2e_fake_gmail_sent").document(mid).set({
            k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in m.items()
        })
    except Exception as exc:
        print(f"[e2e fake gmail] could not mirror {mid}: {exc}")


class FakeFlow:
    """Stands in for google_auth_oauthlib Flow on the callback."""

    def __init__(self):
        self.redirect_uri = None
        self.credentials = None

    @classmethod
    def from_client_config(cls, *_a, **_k):
        return cls()

    def fetch_token(self, code=None, **_):
        if code == "e2e-partial":
            # What oauthlib raises when the user unticks a permission.
            raise Warning('Scope has changed from "' + " ".join(SEND_SCOPES) + '" to "openid".')
        self.credentials = SimpleNamespace(
            token=f"e2e-token-{uuid.uuid4().hex[:8]}", refresh_token="e2e-refresh",
            token_uri="https://oauth2.googleapis.com/token", client_id=os.environ["GOOGLE_CLIENT_ID"],
            scopes=list(SEND_SCOPES), expiry=datetime.utcnow() + timedelta(days=30), valid=True,
        )


CONSENT_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Fake Google consent (e2e)</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:system-ui;max-width:520px;margin:40px auto;padding:0 16px;color:#1f2937}}
a{{display:block;margin:10px 0;padding:12px 14px;border:1px solid #d1d5db;border-radius:10px;text-decoration:none;color:#111}}
.note{{background:#fef3c7;padding:10px 12px;border-radius:8px;font-size:14px}}</style></head><body>
<p class="note">E2E TEST ONLY. This stands in for Google's consent screen. No real Google account is involved.</p>
<h1>Offerloop wants to access your Gmail</h1>
<a id="allow" href="{cb}?state={state}&code=e2e-allow">Allow (all permissions)</a>
<a id="partial" href="{cb}?state={state}&code=e2e-partial">Continue with a permission unticked</a>
<a id="cancel" href="{cb}?state={state}&error=access_denied">Cancel</a>
</body></html>"""


def install_fakes(app) -> None:
    from flask import jsonify, request

    if os.environ.get("E2E_FAKE_GMAIL") == "1":
        import googleapiclient.discovery
        from backend.app.routes import gmail_oauth
        from backend.app.services import gmail_client
        # Same modules imported both ways (app.* and backend.app.*).
        import app.routes.gmail_oauth as gmail_oauth_alias
        import app.services.gmail_client as gmail_client_alias

        def fake_build(*_a, **_k):
            return FakeGmailService()

        googleapiclient.discovery.build = fake_build
        for mod in (gmail_client, gmail_client_alias):
            mod.build = fake_build
            mod.start_gmail_watch = lambda *a, **k: None
        for mod in (gmail_oauth, gmail_oauth_alias):
            mod.Flow = FakeFlow

        real_start = app.view_functions["gmail_oauth.google_oauth_start"]

        def fake_start(*a, **k):
            resp = real_start(*a, **k)
            data = resp.get_json() if hasattr(resp, "get_json") else None
            if not data or "state" not in data:
                return resp
            cb = os.environ["OAUTH_REDIRECT_URI"]
            consent = f"http://127.0.0.1:{PORT}/e2e/fake-google-consent?state={data['state']}"
            print(f"[e2e fake oauth] consent page {consent} -> callback {cb}")
            return jsonify({"authUrl": consent, "state": data["state"]})

        app.view_functions["gmail_oauth.google_oauth_start"] = fake_start

        @app.get("/e2e/fake-google-consent")
        def e2e_fake_consent():
            return CONSENT_PAGE.format(cb=os.environ["OAUTH_REDIRECT_URI"], state=request.args.get("state", ""))

        @app.get("/e2e/fake-gmail/sent")
        def e2e_fake_sent():
            return jsonify([{"id": i, **{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in m.items()}}
                            for i, m in SENT.items()])

        print("[e2e] fake Gmail + fake OAuth consent installed (no real Google calls)")

    if os.environ.get("E2E_FAKE_LLM") == "1":
        import app.services.reply_generation as reply_generation
        import backend.app.services.reply_generation as reply_generation_b
        import app.routes.resume as resume_routes
        import backend.app.routes.resume as resume_routes_b

        for mod in (reply_generation, reply_generation_b):
            mod.batch_generate_emails = fake_batch_generate_emails
        # The outreach route refuses to run with no model configured; in fake
        # mode report one, which fails loudly if anything but the faked
        # functions above tries to use it.
        import app.services.openai_client as openai_client
        import backend.app.services.openai_client as openai_client_b
        for mod in (openai_client, openai_client_b):
            mod.get_anthropic_client = lambda: _FakeModelClient()
        for mod in (resume_routes, resume_routes_b):
            mod.parse_resume_info = fake_parse_resume_info
        print("[e2e] fake resume parser + fake email writer installed (no OpenAI calls)")


# ── Fake LLM ──────────────────────────────────────────────────────────────

class _FakeModelClient:
    """Stands in for a configured model client in fake-LLM mode."""

    def __getattr__(self, name):
        raise RuntimeError(f"e2e fake LLM: unexpected direct model call ({name}); only the faked functions run")


def fake_parse_resume_info(text: str) -> dict:
    """Deterministic parse of a plain resume (PDF text often arrives as one
    line, so this matches on the whole text, not on lines)."""
    t = " ".join((text or "").split())
    name_m = re.match(r"([A-Z][\w'-]+(?:\s+[A-Z][\w'-]+){1,2})", t)
    uni_m = re.search(r"(University of [A-Z][a-z]+(?: [A-Z][a-z]+)*|[A-Z][a-z]+(?: [A-Z][a-z]+)* (?:University|College))", t)
    major_m = re.search(r"\b(?:B\.?S\.?|B\.?A\.?|Bachelor of \w+) in ([A-Z][a-z]+(?: [A-Z][a-z]+)*)", t)
    year_m = re.search(r"\b(20\d{2})\b", t)
    objective_m = re.search(r"Objective:\s*([^.]+\.)", t)
    email_m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", t)
    experience = [
        {"title": title.strip(), "company": company.strip(), "bullets": []}
        for title, company in re.findall(
            r"((?:Summer |Finance |Investment Banking |Software )?(?:Analyst|Intern|Associate|Engineer|Consultant)) at ([A-Z][\w&]*(?: [A-Z][\w&]*)*)", t)
    ]
    clubs = [{"activity": c, "organization": c, "role": ""}
             for c in re.findall(r"Club:\s*([A-Z][\w&]*(?: [A-Z][\w&]*)*)", t)]
    return {
        "name": name_m.group(1) if name_m else "",
        "contact": {"email": email_m.group(0) if email_m else "", "phone": ""},
        "objective": objective_m.group(1).strip() if objective_m else "",
        "education": {"university": uni_m.group(1) if uni_m else "", "major": major_m.group(1) if major_m else "",
                      "graduation": year_m.group(1) if year_m else "", "degree": "", "minor": ""},
        "experience": experience,
        "extracurriculars": clubs,
        "projects": [],
        "skills": {},
    }


def fake_batch_generate_emails(contacts, resume_text=None, user_profile=None, career_interests=None, *a,
                               pre_parsed_user_info=None, **k) -> dict:
    """Same inputs as the real writer: the sender profile first, then the
    stored parse (so confirmed corrections show up in the draft)."""
    out = {}
    profile = user_profile or {}
    parsed = pre_parsed_user_info if isinstance(pre_parsed_user_info, dict) else {}
    edu = parsed.get("education") or {}
    me = (profile.get("name") or parsed.get("name") or "a student").split(" ")[0]
    school = profile.get("university") if "university" in profile else edu.get("university")
    major = profile.get("major") or edu.get("major") or "my major"
    intro = f"a student at {school} studying {major}" if school else f"studying {major}"
    for i, c in enumerate(contacts or []):
        first = c.get("FirstName") or "there"
        company = c.get("Company") or "your firm"
        title = (c.get("Title") or "your role").lower()
        out[i] = {
            "subject": f"{school or 'Student'} question about {company}",
            "body": (
                f"Hi {first},\n\n"
                f"I'm {me}, {intro}. I came across your profile and was "
                f"interested in how you got to {company} and what your work as {title} looks like day to day. "
                f"I'm recruiting for similar roles and would really value hearing what helped you most, and "
                f"what you'd do differently if you were starting out again.\n\n"
                f"Would you be open to a 15-minute call in the next couple of weeks? Happy to work around your schedule.\n\n"
                f"Best,\n{me}"
            ),
        }
    return out


def main() -> None:
    configure_environment()
    assert_isolated()
    sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
    from backend.wsgi import app
    install_fakes(app)
    print(f"[e2e] backend on http://127.0.0.1:{PORT} (frontend {FRONTEND})")
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
