"""Tests for email open tracking (services/email_tracking.py + pixel route)."""
import ast
from email import message_from_bytes
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _ago(**kw):
    return email_tracking._iso(email_tracking._now() - timedelta(**kw))


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


def test_text_to_html_keeps_spacing_and_indentation():
    out = email_tracking.text_to_html("a  b\n    indented")
    assert out == ('<div style="font-family: Arial, Helvetica, sans-serif; font-size: 14px; '
                   'white-space: pre-wrap;">a  b<br>    indented</div>')


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
    db.tracking()["tok"]["lastHitAt"] = "2020-01-01T00:00:00Z"

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
    assert not db.tracking()["tok"].get("sentAt")  # estimates are not persisted


def test_falls_back_to_contact_sent_stamp_when_gmail_unreachable(db):
    db.contacts("u1")["c1"] = {"gmailDraftId": "d1"}
    email_tracking.register("tok", "u1", "outreach", recipient_email="a@b.com", draft_id="d1")

    with patch.object(email_tracking, "_draft_still_exists", return_value=None):
        assert email_tracking.record_open("tok") is False
        db.tracking()["tok"]["createdAt"] = _ago(hours=1)
        db.tracking()["tok"]["lastHitAt"] = _ago(minutes=5)
        db.contacts("u1")["c1"]["emailSentAt"] = _ago(minutes=5)
        assert email_tracking.record_open("tok") is True


def test_hit_within_grace_of_send_time_does_not_count(db):
    db.contacts("u1")["c1"] = {"email": "a@b.com"}
    email_tracking.register("tok", "u1", "outreach", recipient_email="a@b.com")
    db.tracking()["tok"].update(sentAt=_ago(seconds=20), sentAtSource="gmail")

    assert email_tracking.record_open("tok") is False

    assert "emailOpenedAt" not in db.contacts("u1")["c1"]
    assert db.tracking()["tok"]["selfViewHits"] == 1


def test_hit_after_grace_counts(db):
    db.contacts("u1")["c1"] = {"email": "a@b.com"}
    email_tracking.register("tok", "u1", "outreach", recipient_email="a@b.com")
    db.tracking()["tok"].update(sentAt=_ago(minutes=5), sentAtSource="gmail")

    assert email_tracking.record_open("tok") is True


def test_direct_send_open_within_grace_counts(db):
    db.contacts("u1")["c1"] = {"email": "a@b.com"}
    email_tracking.register("tok", "u1", "outreach", recipient_email="a@b.com", sent=True)
    db.tracking()["tok"]["sentAt"] = _ago(seconds=20)

    assert email_tracking.record_open("tok") is True


class _FakeGmail:
    """messages().get returns `msg` (or raises if None); threads().get returns `thread`."""

    def __init__(self, label_ids=None, internal_ms=None, thread=None):
        self.msg = (None if label_ids is None
                    else {"labelIds": label_ids, "internalDate": str(internal_ms)})
        self.thread = thread
        self._kind = None
        self.calls = []  # one entry per execute(): "msg" / "thread"

    def users(self):
        return self

    def messages(self):
        self._kind = "msg"
        return self

    def threads(self):
        self._kind = "thread"
        return self

    def get(self, **_kw):
        return self

    def execute(self):
        self.calls.append(self._kind)
        data = self.msg if self._kind == "msg" else self.thread
        if data is None:
            raise RuntimeError("404")
        return data


def _ms_ago(**kw):
    return int((email_tracking._now() - timedelta(**kw)).timestamp() * 1000)


def _draft_with_message(db, gmail):
    db.contacts("u1")["c1"] = {"gmailDraftId": "d1"}
    email_tracking.register("tok", "u1", "outreach", recipient_email="a@b.com",
                            draft_id="d1", message_id="m1")
    return (patch("app.services.gmail_client._load_user_gmail_creds", return_value=object()),
            patch("app.services.gmail_client._gmail_service", return_value=gmail))


def test_draft_send_time_read_from_gmail_message(db):
    creds, svc = _draft_with_message(db, _FakeGmail(["SENT"], _ms_ago(seconds=10)))
    with creds, svc:
        assert email_tracking.record_open("tok") is False
    assert db.tracking()["tok"]["selfViewHits"] == 1
    assert "emailOpenedAt" not in db.contacts("u1")["c1"]

    db.tracking().clear()
    old = _ms_ago(minutes=10)
    creds, svc = _draft_with_message(db, _FakeGmail(["SENT"], old))
    with creds, svc:
        assert email_tracking.record_open("tok") is True
    sent = email_tracking._parse_iso(db.tracking()["tok"]["sentAt"])
    assert abs(sent.timestamp() * 1000 - old) < 1000
    assert db.tracking()["tok"]["sentAtSource"] == "gmail"


def _thread_of(*msgs):
    return {"messages": [{"labelIds": l, "internalDate": str(ms)} for l, ms in msgs]}


def _edited_draft(db, thread):
    db.contacts("u1")["c1"] = {"gmailDraftId": "d1"}
    email_tracking.register("tok", "u1", "outreach", recipient_email="a@b.com",
                            draft_id="d1", message_id="m1", thread_id="t1")
    db.tracking()["tok"]["createdAt"] = _ago(hours=1)
    gmail = _FakeGmail(thread=thread)  # messages().get raises: stored id went stale
    return (patch("app.services.gmail_client._load_user_gmail_creds", return_value=object()),
            patch("app.services.gmail_client._gmail_service", return_value=gmail))


def test_edited_draft_send_time_read_from_thread(db):
    creds, svc = _edited_draft(db, _thread_of((["SENT"], _ms_ago(seconds=10))))
    with creds, svc:
        assert email_tracking.record_open("tok") is False
    assert db.tracking()["tok"]["selfViewHits"] == 1

    db.tracking().clear()
    old = _ms_ago(minutes=10)
    creds, svc = _edited_draft(db, _thread_of((["SENT"], old)))
    with creds, svc:
        assert email_tracking.record_open("tok") is True
    sent = email_tracking._parse_iso(db.tracking()["tok"]["sentAt"])
    assert abs(sent.timestamp() * 1000 - old) < 1000


def test_thread_with_no_sent_message_falls_through(db):
    creds, svc = _edited_draft(db, _thread_of((["DRAFT"], _ms_ago(minutes=10))))
    with creds, svc, patch.object(email_tracking, "_draft_still_exists", return_value=True):
        assert email_tracking.record_open("tok") is False
    assert db.tracking()["tok"]["preSendHits"] == 1


def test_estimated_send_time_is_not_persisted(db):
    db.contacts("u1")["c1"] = {"gmailDraftId": "d1"}
    email_tracking.register("tok", "u1", "outreach", recipient_email="a@b.com", draft_id="d1")
    with patch.object(email_tracking, "_draft_still_exists", return_value=False):
        assert email_tracking.record_open("tok") is True
    assert not db.tracking()["tok"]["sentAt"]
    assert not db.tracking()["tok"].get("sentAtSource")


def test_message_still_labeled_draft_is_not_sent(db):
    creds, svc = _draft_with_message(db, _FakeGmail(["DRAFT"], _ms_ago(minutes=10)))
    with creds, svc, patch.object(email_tracking, "_draft_still_exists", return_value=True):
        assert email_tracking.record_open("tok") is False
    assert db.tracking()["tok"]["preSendHits"] == 1
    assert not db.tracking()["tok"]["sentAt"]


def test_message_labeled_draft_skips_the_draft_lookup(db):
    """A stored message still labeled DRAFT already answers "unsent"; asking
    drafts().get as well would spend a second Gmail call per hit."""
    gmail = _FakeGmail(["DRAFT"], _ms_ago(minutes=10))
    creds, svc = _draft_with_message(db, gmail)
    with creds, svc, patch.object(email_tracking, "_draft_still_exists") as draft_lookup:
        assert email_tracking.record_open("tok") is False
    assert draft_lookup.call_count == 0
    assert gmail.calls == ["msg"]
    assert db.tracking()["tok"]["preSendHits"] == 1


def test_burst_of_hits_costs_one_gmail_lookup(db):
    """The public pixel route must not turn a burst of hits into a burst of
    Gmail API calls: hits within DEDUPE_WINDOW of the last hit stop early."""
    gmail = _FakeGmail(["DRAFT"], _ms_ago(minutes=10))
    creds, svc = _draft_with_message(db, gmail)
    with creds, svc, patch.object(email_tracking, "_draft_still_exists", return_value=True):
        for _ in range(5):
            assert email_tracking.record_open("tok") is False
    assert gmail.calls == ["msg"]
    assert db.tracking()["tok"]["preSendHits"] == 1

    # Once the window has passed, the next hit asks Gmail again.
    db.tracking()["tok"]["lastHitAt"] = _ago(seconds=31)
    with creds, svc, patch.object(email_tracking, "_draft_still_exists", return_value=True):
        assert email_tracking.record_open("tok") is False
    assert gmail.calls == ["msg", "msg"]
    assert db.tracking()["tok"]["preSendHits"] == 2


def test_burst_after_send_counts_once_with_one_gmail_lookup(db):
    gmail = _FakeGmail(["SENT"], _ms_ago(minutes=10))
    creds, svc = _draft_with_message(db, gmail)
    with creds, svc:
        assert email_tracking.record_open("tok") is True
        for _ in range(3):
            assert email_tracking.record_open("tok") is False
    # The first hit learned and stored the send time; later hits never ask.
    assert gmail.calls == ["msg"]
    assert db.contacts("u1")["c1"]["emailOpenCount"] == 1
    assert db.tracking()["tok"]["openCount"] == 1


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


def _gmail_send_calls(tree):
    """Calls shaped like <x>.drafts().create(...) / <x>.messages().send(...),
    wherever line breaks fall."""
    wanted = {"create": "drafts", "send": "messages"}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in wanted
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Attribute)
                and node.func.value.func.attr == wanted[node.func.attr]):
            yield node


def test_every_gmail_send_site_is_tracked():
    """Every Gmail draft/send site must carry the pixel (email_tracking) or
    pass tracking_kind to the shared sender."""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for path in sorted(app_dir.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source)
        funcs = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for call in _gmail_send_calls(tree):
            where = f"{path.relative_to(app_dir.parent)}:{call.lineno}"
            enclosing = [f for f in funcs if f.lineno <= call.lineno <= f.end_lineno]
            if not enclosing:
                offenders.append(f"{where} (module level)")
                continue
            fn = max(enclosing, key=lambda f: f.lineno)  # innermost
            if f"{path.name}:{fn.name}" in UNTRACKED_OK:
                continue
            body = "\n".join(lines[fn.lineno - 1:fn.end_lineno])
            if "email_tracking" not in body and "tracking_kind" not in body:
                offenders.append(f"{where} in {fn.name}()")
    assert not offenders, (
        "Gmail draft/send sites without open tracking (use email_tracking, or add "
        "to UNTRACKED_OK with a reason):\n  " + "\n  ".join(offenders)
    )


# "file:function" -> reason a send_email_for_user call deliberately has no
# tracking_kind (the email goes to the user, not a contact).
SEND_EMAIL_UNTRACKED_OK = {
    "agent_service.py:send_daily_digests": "daily digest sent to the user's own address",
}


def _passes_tracking_kind(call):
    """tracking_kind=<anything but a literal None>."""
    for k in call.keywords:
        if k.arg == "tracking_kind":
            return not (isinstance(k.value, ast.Constant) and k.value.value is None)
    return False


def test_every_send_email_for_user_call_passes_tracking_kind():
    """send_email_for_user adds the pixel only when tracking_kind is set, and
    the send-site guard above can't see that from a caller, so check callers."""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for path in sorted(app_dir.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "send_email_for_user(" not in source:
            continue
        tree = ast.parse(source)
        funcs = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name != "send_email_for_user":
                continue
            if _passes_tracking_kind(node):
                continue
            enclosing = [fn for fn in funcs if fn.lineno <= node.lineno <= fn.end_lineno]
            fn_name = max(enclosing, key=lambda fn: fn.lineno).name if enclosing else "<module>"
            if f"{path.name}:{fn_name}" in SEND_EMAIL_UNTRACKED_OK:
                continue
            offenders.append(f"{path.relative_to(app_dir.parent)}:{node.lineno} in {fn_name}()")
    assert not offenders, (
        "send_email_for_user() without tracking_kind (pass it for emails to a "
        "contact, or add to SEND_EMAIL_UNTRACKED_OK with a reason):\n  "
        + "\n  ".join(offenders)
    )


def test_tracking_kind_guard_rejects_literal_none():
    def call(src):
        return ast.parse(src).body[0].value

    assert _passes_tracking_kind(call("send_email_for_user(u, t, s, b, tracking_kind='outreach')"))
    assert _passes_tracking_kind(call("send_email_for_user(u, t, s, b, tracking_kind=kind)"))
    assert not _passes_tracking_kind(call("send_email_for_user(u, t, s, b, tracking_kind=None)"))
    assert not _passes_tracking_kind(call("send_email_for_user(u, t, s, b)"))


def test_send_email_for_user_registers_contact_id(db, monkeypatch):
    from app.services import gmail_client

    monkeypatch.setenv("EMAIL_OPEN_TRACKING", "1")
    service = MagicMock()
    service.users().messages().send().execute.return_value = {"id": "m1", "threadId": "t1"}
    with patch.object(gmail_client, "_load_user_gmail_creds", return_value=object()), \
         patch.object(gmail_client, "_gmail_service", return_value=service):
        gmail_client.send_email_for_user("u1", "a@b.com", "Hi", "<p>Body</p>",
                                         tracking_kind="outreach", contact_id="c9")
        gmail_client.send_email_for_user("u1", "a@b.com", "Hi", "<p>Body</p>",
                                         tracking_kind="outreach")
    docs = sorted(db.tracking().values(), key=lambda d: d["contactId"])
    assert [d["contactId"] for d in docs] == ["", "c9"]
    assert all(d["sentAt"] and d["gmailMessageId"] == "m1" for d in docs)


def test_register_for_draft_stores_message_id(db):
    email_tracking.register(
        "tok", "u1", "outreach", recipient_email="a@b.com",
        draft_id="d1", message_id="m1", thread_id="t1",
    )
    doc = db.tracking()["tok"]
    assert doc["gmailMessageId"] == "m1"
    assert doc["gmailDraftId"] == "d1"


def test_every_draft_register_passes_message_id():
    """Draft tokens must store the Gmail message id (read later for send time)."""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for path in sorted(app_dir.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "email_tracking" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "register"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "email_tracking"):
                continue
            kws = {k.arg for k in node.keywords}
            if "draft_id" in kws and "message_id" not in kws:
                offenders.append(f"{path.relative_to(app_dir.parent)}:{node.lineno}")
    assert not offenders, (
        "email_tracking.register(draft_id=...) without message_id:\n  "
        + "\n  ".join(offenders)
    )
