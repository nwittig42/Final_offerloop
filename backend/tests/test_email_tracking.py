"""Tests for email open tracking (services/email_tracking.py + pixel route)."""
from email import message_from_bytes
from unittest.mock import patch

import pytest

from app.services import email_tracking


# ---------------------------------------------------------------------------
# Minimal in-memory Firestore
# ---------------------------------------------------------------------------

class _Snap:
    def __init__(self, ref):
        self.reference = ref
        self.id = ref.id
        self.exists = ref.id in ref.store

    def to_dict(self):
        return dict(self.reference.store.get(self.id, {}))


class _Doc:
    def __init__(self, store, doc_id, root):
        self.store, self.id, self.root = store, doc_id, root

    def get(self):
        return _Snap(self)

    def set(self, data):
        self.store[self.id] = dict(data)

    def update(self, data):
        doc = self.store[self.id]
        for k, v in data.items():
            if type(v).__name__ == "Increment":
                doc[k] = (doc.get(k) or 0) + v.value
            else:
                doc[k] = v

    def collection(self, name):
        return _Coll(self.root.setdefault(f"{self.id}/{name}", {}), self.root)


class _Query:
    def __init__(self, coll, field, value):
        self.coll, self.field, self.value = coll, field, value

    def limit(self, _n):
        return self

    def stream(self):
        return [_Snap(_Doc(self.coll.store, k, self.coll.root))
                for k, v in self.coll.store.items() if v.get(self.field) == self.value][:1]


class _Coll:
    def __init__(self, store, root):
        self.store, self.root = store, root

    def document(self, doc_id):
        return _Doc(self.store, doc_id, self.root)

    def where(self, field, _op, value):
        return _Query(self, field, value)


class FakeDb:
    def __init__(self):
        self.root = {}

    def collection(self, name):
        return _Coll(self.root.setdefault(name, {}), self.root)

    def contacts(self, uid):
        return self.root.setdefault(f"{uid}/contacts", {})

    def tracking(self):
        return self.root.setdefault(email_tracking.COLLECTION, {})


@pytest.fixture
def db():
    fake = FakeDb()
    with patch.object(email_tracking, "get_db", return_value=fake):
        yield fake


# ---------------------------------------------------------------------------
# Building the tracked email
# ---------------------------------------------------------------------------

def test_tracking_is_off_by_default(monkeypatch):
    monkeypatch.delenv("EMAIL_OPEN_TRACKING", raising=False)
    monkeypatch.delenv("EMAIL_OPEN_TRACKING_UIDS", raising=False)
    assert email_tracking.start("u1") is None


def test_tracking_on_for_everyone(monkeypatch):
    monkeypatch.setenv("EMAIL_OPEN_TRACKING", "1")
    assert email_tracking.start("u1")
    assert email_tracking.start(None) is None


def test_tracking_on_for_listed_users_only(monkeypatch):
    monkeypatch.delenv("EMAIL_OPEN_TRACKING", raising=False)
    monkeypatch.setenv("EMAIL_OPEN_TRACKING_UIDS", "u1, u2")
    assert email_tracking.start("u1")
    assert email_tracking.start("u3") is None


def test_off_means_emails_are_built_exactly_as_before(monkeypatch):
    from email.mime.text import MIMEText
    from app.services.gmail_client import _build_outreach_mime
    monkeypatch.delenv("EMAIL_OPEN_TRACKING", raising=False)
    monkeypatch.delenv("EMAIL_OPEN_TRACKING_UIDS", raising=False)
    token = email_tracking.start("u1")

    assert email_tracking.add_pixel("<p>hi</p>", token) == "<p>hi</p>"
    assert email_tracking.tracked_text_mime("hello", token).as_string() == MIMEText("hello").as_string()
    for user_info in ({"name": "Nick"}, None):
        msg = _build_outreach_mime("a@b.com", "me@usc.edu", "Hi", "Body", user_info=user_info,
                                   tracking_token=token)
        body = msg.get_payload()[0]
        assert not body.is_multipart()
        assert "/api/t/o/" not in body.get_payload(decode=True).decode()


def test_add_pixel_goes_before_closing_body():
    out = email_tracking.add_pixel("<html><body><p>hi</p></body></html>", "tok")
    assert "/api/t/o/tok.gif" in out
    assert out.index("tok.gif") < out.index("</body>")


def test_add_pixel_appends_to_fragment_and_ignores_missing_token():
    assert email_tracking.add_pixel("<p>hi</p>", "tok").endswith('style="border:0;">')
    assert email_tracking.add_pixel("<p>hi</p>", None) == "<p>hi</p>"


def test_tracked_text_mime_without_token_is_plain_text():
    msg = email_tracking.tracked_text_mime("hello", None)
    assert msg.get_content_type() == "text/plain"


def test_tracked_text_mime_keeps_text_and_adds_escaped_html():
    msg = email_tracking.tracked_text_mime("line one\n<b>two</b>", "tok")
    msg["to"] = "a@b.com"
    parsed = message_from_bytes(msg.as_bytes())
    assert parsed.get_content_type() == "multipart/alternative"
    plain, html_part = parsed.get_payload()
    assert plain.get_payload(decode=True).decode() == "line one\n<b>two</b>"
    html_body = html_part.get_payload(decode=True).decode()
    assert "line one<br>&lt;b&gt;two&lt;/b&gt;" in html_body
    assert "/api/t/o/tok.gif" in html_body


def test_outreach_builder_embeds_pixel():
    from app.services.gmail_client import _build_outreach_mime
    msg = _build_outreach_mime("a@b.com", "me@usc.edu", "Hi", "Body text",
                               user_info={"name": "Nick"}, tracking_token="tok")
    assert "/api/t/o/tok.gif" in msg.get_payload()[0].get_payload(decode=True).decode()
    untracked = _build_outreach_mime("a@b.com", "me@usc.edu", "Hi", "Body text",
                                     user_info={"name": "Nick"})
    assert "/api/t/o/" not in untracked.get_payload()[0].get_payload(decode=True).decode()


# ---------------------------------------------------------------------------
# Recording opens
# ---------------------------------------------------------------------------

def test_unknown_token_is_ignored(db):
    assert email_tracking.record_open("nope") is False


def test_direct_send_open_stamps_the_contact(db):
    db.contacts("u1")["c1"] = {"email": "a@b.com", "gmailThreadId": "t1"}
    email_tracking.register("tok", "u1", "outreach", recipient_email="A@b.com",
                            thread_id="t1", sent=True)

    assert email_tracking.record_open("tok") is True

    contact = db.contacts("u1")["c1"]
    assert contact["emailOpenCount"] == 1
    assert contact["emailOpenedAt"] == contact["emailLastOpenedAt"]
    assert db.tracking()["tok"]["contactId"] == "c1"


def test_burst_of_hits_counts_once(db):
    db.contacts("u1")["c1"] = {"email": "a@b.com"}
    email_tracking.register("tok", "u1", "outreach", recipient_email="a@b.com", sent=True)

    assert email_tracking.record_open("tok") is True
    assert email_tracking.record_open("tok") is False
    assert db.contacts("u1")["c1"]["emailOpenCount"] == 1


def test_later_open_increments_but_keeps_first_open(db):
    db.contacts("u1")["c1"] = {"email": "a@b.com"}
    email_tracking.register("tok", "u1", "outreach", recipient_email="a@b.com", sent=True)
    email_tracking.record_open("tok")
    first = db.contacts("u1")["c1"]["emailOpenedAt"]
    db.tracking()["tok"]["lastOpenedAt"] = "2020-01-01T00:00:00Z"

    assert email_tracking.record_open("tok") is True
    contact = db.contacts("u1")["c1"]
    assert contact["emailOpenCount"] == 2
    assert contact["emailOpenedAt"] == first


def test_hit_on_unsent_draft_does_not_count(db):
    db.contacts("u1")["c1"] = {"gmailDraftId": "d1", "pipelineStage": "draft_created"}
    email_tracking.register("tok", "u1", "outreach", recipient_email="a@b.com", draft_id="d1")

    with patch.object(email_tracking, "_draft_still_exists", return_value=True):
        assert email_tracking.record_open("tok") is False

    assert "emailOpenedAt" not in db.contacts("u1")["c1"]
    assert db.tracking()["tok"]["preSendHits"] == 1


def test_hit_after_draft_was_sent_counts(db):
    db.contacts("u1")["c1"] = {"gmailDraftId": "d1"}
    email_tracking.register("tok", "u1", "outreach", recipient_email="a@b.com", draft_id="d1")

    with patch.object(email_tracking, "_draft_still_exists", return_value=False):
        assert email_tracking.record_open("tok") is True

    assert db.contacts("u1")["c1"]["emailOpenCount"] == 1
    assert db.tracking()["tok"]["sentAt"]


def test_falls_back_to_contact_sent_stamp_when_gmail_unreachable(db):
    db.contacts("u1")["c1"] = {"gmailDraftId": "d1"}
    email_tracking.register("tok", "u1", "outreach", recipient_email="a@b.com", draft_id="d1")

    with patch.object(email_tracking, "_draft_still_exists", return_value=None):
        assert email_tracking.record_open("tok") is False
        db.contacts("u1")["c1"]["emailSentAt"] = "2999-01-01T00:00:00Z"
        assert email_tracking.record_open("tok") is True


def test_open_with_no_matching_contact_still_recorded_on_token(db):
    email_tracking.register("tok", "u1", "outreach", recipient_email="gone@b.com", sent=True)
    assert email_tracking.record_open("tok") is True
    assert db.tracking()["tok"]["openCount"] == 1


def test_register_never_raises():
    with patch.object(email_tracking, "get_db", side_effect=RuntimeError("down")):
        email_tracking.register("tok", "u1", "outreach")
        assert email_tracking.record_open("tok") is False


# ---------------------------------------------------------------------------
# Pixel route
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    # Just this blueprint: the route needs nothing else from the full app.
    from flask import Flask
    from app.routes.email_tracking import email_tracking_bp
    app = Flask(__name__)
    app.register_blueprint(email_tracking_bp)
    return app.test_client()


def test_pixel_route_returns_gif_and_records(client):
    with patch("app.routes.email_tracking.record_open") as rec:
        resp = client.get("/api/t/o/abc123.gif")
    assert resp.status_code == 200
    assert resp.mimetype == "image/gif"
    assert resp.data == email_tracking.PIXEL_GIF
    assert "no-store" in resp.headers["Cache-Control"]
    rec.assert_called_once_with("abc123")


def test_pixel_route_serves_gif_even_when_recording_fails(client):
    with patch("app.routes.email_tracking.record_open", side_effect=RuntimeError("boom")):
        resp = client.get("/api/t/o/abc123.gif")
    assert resp.status_code == 200
    assert resp.data == email_tracking.PIXEL_GIF


# ---------------------------------------------------------------------------
# Coverage guard
# ---------------------------------------------------------------------------

# "file:function" -> reason a Gmail draft/send site is deliberately untracked.
UNTRACKED_OK = {}


def test_every_gmail_send_site_is_tracked():
    """Every Gmail draft/send site must carry the pixel (email_tracking) or
    pass tracking_kind to the shared sender."""
    import ast
    import re
    from pathlib import Path

    app_dir = Path(__file__).resolve().parent.parent / "app"
    pattern = re.compile(r"drafts\(\)\.create\(|messages\(\)\.send\(")
    offenders = []
    for path in sorted(app_dir.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not pattern.search(source):
            continue
        lines = source.splitlines()
        funcs = [n for n in ast.walk(ast.parse(source))
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for lineno, line in enumerate(lines, 1):
            if not pattern.search(line) or line.lstrip().startswith("#"):
                continue
            enclosing = [f for f in funcs if f.lineno <= lineno <= f.end_lineno]
            if not enclosing:
                offenders.append(f"{path.relative_to(app_dir.parent)}:{lineno} (module level)")
                continue
            fn = max(enclosing, key=lambda f: f.lineno)  # innermost
            body = "\n".join(lines[fn.lineno - 1:fn.end_lineno])
            key = f"{path.name}:{fn.name}"
            if key in UNTRACKED_OK:
                continue
            if "email_tracking" not in body and "tracking_kind" not in body:
                offenders.append(f"{path.relative_to(app_dir.parent)}:{lineno} in {fn.name}()")
    assert not offenders, (
        "Gmail draft/send sites without open tracking (use email_tracking, or add "
        "to UNTRACKED_OK with a reason):\n  " + "\n  ".join(offenders)
    )


def test_register_for_draft_stores_message_id(db):
    email_tracking.register(
        "tok", "u1", "outreach", recipient_email="a@b.com",
        draft_id="d1", message_id="m1", thread_id="t1",
    )
    doc = db.tracking()["tok"]
    assert doc["gmailMessageId"] == "m1"
    assert doc["gmailDraftId"] == "d1"
