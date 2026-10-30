"""The email_opened pipeline stage (OFF-12).

A counted pixel open promotes a contact from email_sent / waiting_on_reply to
email_opened. Every place that treats a contact as "sent, awaiting a reply"
must treat email_opened the same way, or opened contacts silently drop out of
reply sync, nudges and stats.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.services import email_tracking
from tests.test_email_tracking import FakeDb, _ago


@pytest.fixture
def db(monkeypatch):
    # Stage promotion only happens while tracking is switched on.
    monkeypatch.setenv("EMAIL_OPEN_TRACKING", "1")
    fake = FakeDb()
    with patch.object(email_tracking, "get_db", return_value=fake):
        yield fake


def _direct_send_contact(db, stage, **extra):
    db.contacts("u1")["c1"] = {"email": "a@b.com", "pipelineStage": stage, **extra}
    email_tracking.register("tok", "u1", "outreach", recipient_email="a@b.com",
                            contact_id="c1", sent=True)


# ---------------------------------------------------------------------------
# record_open
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stage", ["waiting_on_reply", "email_sent"])
def test_open_promotes_waiting_on_reply(db, stage):
    _direct_send_contact(db, stage)

    assert email_tracking.record_open("tok") is True

    contact = db.contacts("u1")["c1"]
    assert contact["pipelineStage"] == "email_opened"
    assert contact["emailOpenCount"] == 1


@pytest.mark.parametrize("stage", [
    "replied", "meeting_scheduled", "connected", "no_response",
    "bounced", "closed", "draft_created",
])
def test_open_never_demotes_replied(db, stage):
    _direct_send_contact(db, stage)

    assert email_tracking.record_open("tok") is True

    contact = db.contacts("u1")["c1"]
    assert contact["pipelineStage"] == stage
    assert contact["emailOpenCount"] == 1


def test_open_on_already_opened_contact_keeps_stage(db):
    _direct_send_contact(db, "email_opened", emailOpenedAt=_ago(days=1), emailOpenCount=1)

    assert email_tracking.record_open("tok") is True

    contact = db.contacts("u1")["c1"]
    assert contact["pipelineStage"] == "email_opened"
    assert contact["emailOpenCount"] == 2


def test_uncounted_hit_does_not_change_stage(db):
    # Pre-send hit: the draft still exists.
    db.contacts("u1")["c1"] = {"gmailDraftId": "d1", "pipelineStage": "waiting_on_reply"}
    email_tracking.register("tok", "u1", "outreach", recipient_email="a@b.com",
                            contact_id="c1", draft_id="d1")
    with patch.object(email_tracking, "_draft_still_exists", return_value=True):
        assert email_tracking.record_open("tok") is False
    assert db.contacts("u1")["c1"]["pipelineStage"] == "waiting_on_reply"
    assert db.tracking()["tok"]["preSendHits"] == 1

    # Self-view hit: within the grace window of a Gmail-confirmed send.
    # (lastHitAt moved back so the 30s hit dedupe doesn't swallow it.)
    db.tracking()["tok"].update(sentAt=_ago(seconds=10), sentAtSource="gmail",
                                lastHitAt=_ago(minutes=5))
    assert email_tracking.record_open("tok") is False
    assert db.contacts("u1")["c1"]["pipelineStage"] == "waiting_on_reply"
    assert db.tracking()["tok"]["selfViewHits"] == 1


def test_open_with_tracking_off_records_but_does_not_promote(db, monkeypatch):
    """Kill switch: opens on already-sent tokens still count, but no contact
    moves to email_opened while tracking is off."""
    monkeypatch.delenv("EMAIL_OPEN_TRACKING", raising=False)
    monkeypatch.delenv("EMAIL_OPEN_TRACKING_UIDS", raising=False)
    _direct_send_contact(db, "waiting_on_reply")

    assert email_tracking.record_open("tok") is True

    contact = db.contacts("u1")["c1"]
    assert contact["pipelineStage"] == "waiting_on_reply"
    assert contact["emailOpenCount"] == 1
    assert contact["emailOpenedAt"] and contact["emailLastOpenedAt"]
    assert db.tracking()["tok"]["openCount"] == 1


def test_open_with_tracking_on_for_this_uid_promotes(db, monkeypatch):
    monkeypatch.delenv("EMAIL_OPEN_TRACKING", raising=False)
    monkeypatch.setenv("EMAIL_OPEN_TRACKING_UIDS", "u1")
    _direct_send_contact(db, "waiting_on_reply")

    assert email_tracking.record_open("tok") is True
    assert db.contacts("u1")["c1"]["pipelineStage"] == "email_opened"


def test_open_on_draft_created_contact_stamps_last_opened(db):
    """An open that lands before we learn the draft was sent keeps the stage
    but stamps emailLastOpenedAt, which send confirmation reads."""
    _direct_send_contact(db, "draft_created", draftCreatedAt=_ago(hours=1))

    assert email_tracking.record_open("tok") is True

    contact = db.contacts("u1")["c1"]
    assert contact["pipelineStage"] == "draft_created"
    assert contact["emailLastOpenedAt"]
    from app.models.pipeline_stages import stage_after_send_confirmed
    assert stage_after_send_confirmed(contact) == "email_opened"


def test_open_promotion_uses_one_contact_read(db):
    """The stage comes from the contact doc record_open already fetched."""
    _direct_send_contact(db, "waiting_on_reply")
    calls = []
    real = email_tracking._resolve_contact

    def spy(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    with patch.object(email_tracking, "_resolve_contact", side_effect=spy):
        assert email_tracking.record_open("tok") is True
    assert len(calls) == 1
    assert db.contacts("u1")["c1"]["pipelineStage"] == "email_opened"


# ---------------------------------------------------------------------------
# Send confirmation (Gmail webhook, outbox draft-gone sync)
# ---------------------------------------------------------------------------

def _webhook_db(contact):
    contacts_collection = MagicMock()
    thread_query = MagicMock()
    thread_query.limit.return_value.get.return_value = [contact]
    contacts_collection.where.return_value = thread_query

    gmail_doc = MagicMock(exists=True)
    gmail_doc.to_dict.return_value = {"watchHistoryId": "100"}
    gmail_ref = MagicMock()
    gmail_ref.get.return_value = gmail_doc
    user_doc = MagicMock(exists=True)
    user_doc.to_dict.return_value = {"email": "student@example.com"}

    def doc_user(_uid):
        user_ref = MagicMock()
        user_ref.get.return_value = user_doc

        def sub(name):
            if name == "contacts":
                return contacts_collection
            coll = MagicMock()
            if name == "integrations":
                coll.document.return_value = gmail_ref
            return coll

        user_ref.collection.side_effect = sub
        return user_ref

    db = MagicMock()

    def collection(name):
        if name == "users":
            users = MagicMock()
            users.document.side_effect = doc_user
            return users
        return MagicMock()

    db.collection.side_effect = collection
    return db


def _run_sent_webhook(contact_data):
    contact = MagicMock()
    contact.id = "c1"
    contact.to_dict.return_value = contact_data
    contact.reference = MagicMock()
    db = _webhook_db(contact)

    service = MagicMock()
    service.users().history().list().execute.return_value = {
        "history": [{"messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]}],
        "nextPageToken": None,
    }
    service.users().messages().get().execute.return_value = {
        "labelIds": ["SENT"],
        "payload": {"headers": [
            {"name": "From", "value": "Student <student@example.com>"},
            {"name": "To", "value": "a@b.com"},
            {"name": "Subject", "value": "Hello"},
        ]},
        "snippet": "Hi there",
    }
    with (
        patch("app.routes.gmail_webhook.find_uid_by_gmail_address", return_value="u1"),
        patch("app.routes.gmail_webhook.get_db", return_value=db),
        patch("app.routes.gmail_webhook.get_gmail_service_for_user", return_value=service),
        patch("app.utils.metrics_events.log_event"),
        patch("app.services.lifecycle_signals.stamp_first_email_sent"),
        patch("app.services.cooldown_service.record_outreach"),
        patch("app.utils.recommendation_events.log_recommendation_event"),
    ):
        from app.routes.gmail_webhook import _process_gmail_notification
        _process_gmail_notification("student@example.com", "200")

    assert contact.reference.update.called, "sent message did not update the contact"
    return contact.reference.update.call_args[0][0]


DRAFTED = "2026-10-18T10:00:00Z"
OPENED_AFTER = "2026-10-18T11:00:00Z"
OPENED_BEFORE = "2026-10-01T09:00:00Z"


def test_webhook_send_confirm_respects_earlier_open():
    from app.models.pipeline_stages import stage_after_send_confirmed

    # An open of THIS draft that landed before the send was confirmed.
    this_draft_opened = {"draftCreatedAt": DRAFTED, "emailOpenedAt": OPENED_AFTER,
                         "emailLastOpenedAt": OPENED_AFTER}
    assert stage_after_send_confirmed(this_draft_opened) == "email_opened"
    assert stage_after_send_confirmed({}) == "waiting_on_reply"

    base = {"email": "a@b.com", "pipelineStage": "draft_created",
            "gmailThreadId": "t1", "inOutbox": True}
    opened = _run_sent_webhook({**base, **this_draft_opened})
    assert opened["pipelineStage"] == "email_opened"
    unopened = _run_sent_webhook({**base, "draftCreatedAt": DRAFTED})
    assert unopened["pipelineStage"] == "waiting_on_reply"
    # Open of an earlier email, then re-drafted: the new send is unopened.
    redrafted = _run_sent_webhook({**base, "draftCreatedAt": DRAFTED,
                                   "emailOpenedAt": OPENED_BEFORE,
                                   "emailLastOpenedAt": OPENED_BEFORE})
    assert redrafted["pipelineStage"] == "waiting_on_reply"


@pytest.mark.parametrize("contact,expected", [
    # Old first open + old last open, newer draft: the new email is unopened.
    ({"emailOpenedAt": OPENED_BEFORE, "emailLastOpenedAt": OPENED_BEFORE,
      "draftCreatedAt": DRAFTED}, "waiting_on_reply"),
    # Old first open, but the latest open is after this draft was made.
    ({"emailOpenedAt": OPENED_BEFORE, "emailLastOpenedAt": OPENED_AFTER,
      "draftCreatedAt": DRAFTED}, "email_opened"),
    # Only emailOpenedAt (no last-open stamp) still works as a fallback.
    ({"emailOpenedAt": OPENED_AFTER, "draftCreatedAt": DRAFTED}, "email_opened"),
    ({"emailOpenedAt": OPENED_BEFORE, "draftCreatedAt": DRAFTED}, "waiting_on_reply"),
    # Open at exactly the draft time counts.
    ({"emailLastOpenedAt": DRAFTED, "draftCreatedAt": DRAFTED}, "email_opened"),
    # No draft timestamp at all: can't tell which email was opened.
    ({"emailOpenedAt": OPENED_AFTER, "emailLastOpenedAt": OPENED_AFTER}, "waiting_on_reply"),
    # emailGeneratedAt stands in when draftCreatedAt is missing.
    ({"emailLastOpenedAt": OPENED_AFTER, "emailGeneratedAt": DRAFTED}, "email_opened"),
    ({"emailLastOpenedAt": OPENED_BEFORE, "emailGeneratedAt": DRAFTED}, "waiting_on_reply"),
    # Unparseable values never yield email_opened.
    ({"emailLastOpenedAt": "x", "draftCreatedAt": DRAFTED}, "waiting_on_reply"),
    ({"emailLastOpenedAt": OPENED_AFTER, "draftCreatedAt": "x"}, "waiting_on_reply"),
    # Naive ISO (datetime.utcnow().isoformat()) is read as UTC.
    ({"emailLastOpenedAt": OPENED_AFTER, "draftCreatedAt": "2026-10-18T10:59:00.123456"},
     "email_opened"),
    ({"emailLastOpenedAt": OPENED_AFTER, "draftCreatedAt": "2026-10-18T11:01:00"},
     "waiting_on_reply"),
    (None, "waiting_on_reply"),
])
def test_stage_after_send_confirmed_only_honors_opens_of_this_draft(contact, expected):
    from app.models.pipeline_stages import stage_after_send_confirmed
    assert stage_after_send_confirmed(contact) == expected


def test_stage_after_send_confirmed_reads_firestore_datetimes():
    from datetime import datetime, timezone
    from app.models.pipeline_stages import stage_after_send_confirmed

    drafted = datetime(2026, 10, 18, 10, 0, tzinfo=timezone.utc)
    after = datetime(2026, 10, 18, 11, 0, tzinfo=timezone.utc)
    assert stage_after_send_confirmed(
        {"draftCreatedAt": drafted, "emailLastOpenedAt": after}) == "email_opened"
    assert stage_after_send_confirmed(
        {"draftCreatedAt": after, "emailLastOpenedAt": drafted}) == "waiting_on_reply"
    # Naive datetimes are UTC; mixing with aware ones must not raise.
    assert stage_after_send_confirmed(
        {"draftCreatedAt": datetime(2026, 10, 18, 10, 0),
         "emailLastOpenedAt": OPENED_AFTER}) == "email_opened"


class _NotFound(Exception):
    def __init__(self):
        super().__init__("not found")
        self.resp = MagicMock(status=404)


def _gone_draft_service():
    service = MagicMock()
    service.users().drafts().get().execute.side_effect = _NotFound()
    return service


_OPENED_THIS_DRAFT = {"draftCreatedAt": DRAFTED, "emailOpenedAt": OPENED_AFTER,
                      "emailLastOpenedAt": OPENED_AFTER}


@pytest.mark.parametrize("data,expected", [
    ({"pipelineStage": "draft_created", "draftCreatedAt": DRAFTED}, "waiting_on_reply"),
    ({"pipelineStage": "draft_created", **_OPENED_THIS_DRAFT}, "email_opened"),
    ({"pipelineStage": "email_sent", **_OPENED_THIS_DRAFT}, "email_opened"),
    # Re-drafted after an open of an earlier email: the new send is unopened.
    ({"pipelineStage": "draft_created", "draftCreatedAt": DRAFTED,
      "emailOpenedAt": OPENED_BEFORE, "emailLastOpenedAt": OPENED_BEFORE}, "waiting_on_reply"),
    # Already confirmed sent: periodic re-syncs must not flip the stage.
    ({"pipelineStage": "email_opened", **_OPENED_THIS_DRAFT}, "email_opened"),
    # A follow-up from email_opened reset this to waiting_on_reply; keep it.
    ({"pipelineStage": "waiting_on_reply", **_OPENED_THIS_DRAFT}, "waiting_on_reply"),
])
def test_draft_gone_sync_respects_open(data, expected):
    from app.services.outbox_service import _check_draft_status

    updates = _check_draft_status(_gone_draft_service(),
                                  {"gmailDraftId": "d1", "gmailThreadId": "t1", **data})
    assert updates["pipelineStage"] == expected


# ---------------------------------------------------------------------------
# Follow-up send
# ---------------------------------------------------------------------------

def _send_reply(stage, **extra):
    from app.services import outbox_service

    data = {"email": "a@b.com", "pipelineStage": stage, "gmailThreadId": "t1",
            "emailSubject": "Hi", **extra}
    ref = MagicMock()
    service = MagicMock()
    service.users().messages().send().execute.return_value = {"id": "m2", "threadId": "t1"}
    with (
        patch.object(outbox_service, "_get_contact", return_value=(ref, data)),
        patch.object(outbox_service, "_load_user_gmail_creds", return_value=object()),
        patch.object(outbox_service, "_gmail_service", return_value=service),
        patch.object(outbox_service, "get_db", return_value=MagicMock()),
        patch.object(email_tracking, "start", return_value=None),
        patch("app.services.cooldown_service.record_outreach"),
        patch("app.utils.metrics_events.log_event"),
    ):
        outbox_service.send_reply_for_contact("u1", "c1", "Following up")
    return ref.update.call_args[0][0]


def test_followup_send_from_opened_resets_to_waiting():
    updates = _send_reply("email_opened", emailOpenedAt="2026-10-18T00:00:00Z")
    assert updates["pipelineStage"] == "waiting_on_reply"
    assert updates["followUpCount"] == 1
    # The contact keeps its first-ever open stamp.
    assert "emailOpenedAt" not in updates


# ---------------------------------------------------------------------------
# Every "awaiting reply" check includes email_opened
# ---------------------------------------------------------------------------

def test_email_opened_is_awaiting_reply_everywhere():
    from app.models.pipeline_stages import AWAITING_REPLY_STAGES, EMAIL_OPENED
    from app.services import background_sync, email_baseline, migration, nudge_service, outbox_service
    from app.services.scout import workflow_state

    assert EMAIL_OPENED == "email_opened"
    assert AWAITING_REPLY_STAGES == frozenset({"email_sent", "waiting_on_reply", "email_opened"})
    assert "email_opened" in background_sync.STALE_PIPELINE_STAGES
    assert "email_opened" in nudge_service.NUDGE_ELIGIBLE_STAGES
    assert "email_opened" in outbox_service.ALLOWED_PIPELINE_STAGES
    assert "email_opened" in outbox_service.POST_SEND_STAGES
    assert "email_opened" in migration.PIPELINE_STAGES
    assert "email_opened" in workflow_state._AWAITING_REPLY_STAGES
    assert "email_opened" in email_baseline.ELIGIBLE_STAGES



def test_opened_contact_is_reply_rate_eligible_in_outbox_stats():
    from app.services import outbox_service

    contacts = [{"pipelineStage": "email_opened"}, {"pipelineStage": "replied"}]
    with patch.object(outbox_service, "get_outbox_contacts", return_value=contacts):
        stats = outbox_service.get_outbox_stats("u1")
    assert stats["replyRate"] == 0.5
    assert stats["byStage"]["email_opened"] == 1
    assert stats["waitingCount"] == 2


def test_loop_thread_backfill_treats_opened_as_sent():
    """loop_service only chases a Gmail thread for contacts that look sent."""
    from app.services import loop_service

    class _Stop(Exception):
        pass

    # get_db is reached only once a contact is queued for a thread lookup.
    with patch.object(loop_service, "get_db", side_effect=_Stop):
        with pytest.raises(_Stop):
            loop_service._backfill_thread_ids_for_contacts(
                "u1", {"c1": {"email": "a@b.com", "pipelineStage": "email_opened"}})


# ---------------------------------------------------------------------------
# Only the pixel sets email_opened
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    # Just the outbox blueprint: the full app needs native PDF libs.
    from flask import Flask
    from app.routes.outbox import outbox_bp
    app = Flask(__name__)
    app.register_blueprint(outbox_bp)
    return app.test_client()


@pytest.fixture
def _bypass_firebase_auth():
    with patch("firebase_admin._apps", {"[DEFAULT]": MagicMock()}), \
         patch("firebase_admin.auth.verify_id_token",
               return_value={"uid": "u1", "email": "student@example.com"}):
        yield


def _put_stage(client, stage, current):
    ref = MagicMock()
    with patch("app.services.outbox_service._get_contact",
               return_value=(ref, {"pipelineStage": current})):
        resp = client.put("/api/outbox/threads/c1/stage", json={"stage": stage},
                          headers={"Authorization": "Bearer fake"})
    return resp, ref


def test_manual_stage_change_to_email_opened_is_rejected(client, _bypass_firebase_auth):
    from app.services import outbox_service

    for current in ("waiting_on_reply", "email_sent", "email_opened"):
        resp, ref = _put_stage(client, "email_opened", current)
        assert resp.status_code == 400
        assert "Invalid stage" in resp.get_json()["error"]
        assert not ref.update.called

    # Same shape/status as any other invalid stage.
    bogus, _ = _put_stage(client, "not_a_stage", "waiting_on_reply")
    assert bogus.status_code == 400 and "Invalid stage" in bogus.get_json()["error"]

    # Still a valid stored value.
    assert "email_opened" in outbox_service.ALLOWED_PIPELINE_STAGES
    with pytest.raises(ValueError, match="Invalid stage"):
        outbox_service.update_contact_stage("u1", "c1", "email_opened")


@pytest.mark.parametrize("target", ["replied", "meeting_scheduled", "waiting_on_reply", "closed"])
def test_manual_stage_change_from_email_opened_still_works(client, _bypass_firebase_auth, target):
    with patch("app.services.cooldown_service.record_outreach"):
        resp, ref = _put_stage(client, target, "email_opened")
    assert resp.status_code == 200, resp.get_json()
    assert ref.update.call_args[0][0]["pipelineStage"] == target
    assert resp.get_json()["thread"]["pipelineStage"] == target


def test_bulk_contact_create_ignores_requested_email_opened():
    """POST /api/contacts/bulk only honors new/draft_created from the client."""
    import inspect
    from app.routes import contacts

    src = inspect.getsource(contacts)
    assert "if req_stage in ('new', 'draft_created'):" in src


# ---------------------------------------------------------------------------
# Rollback script (scripts/rollback_email_opened.py)
# ---------------------------------------------------------------------------

class _RbSnap:
    def __init__(self, store, uid, cid):
        self.id, self._store, self._uid = cid, store, uid
        self.reference = ("ref", uid, cid)

    def to_dict(self):
        return dict(self._store[self._uid][self.id])


class _RbQuery:
    def __init__(self, store, uid, field, value):
        self.store, self.uid, self.field, self.value = store, uid, field, value

    def stream(self):
        return [_RbSnap(self.store, self.uid, cid)
                for cid, d in self.store[self.uid].items() if d.get(self.field) == self.value]


class _RbContacts:
    def __init__(self, store, uid):
        self.store, self.uid = store, uid

    def where(self, field, op, value):
        assert op == "=="
        return _RbQuery(self.store, self.uid, field, value)


class _RbUserDoc:
    def __init__(self, store, uid):
        self.store, self.id = store, uid

    def collection(self, name):
        assert name == "contacts"
        return _RbContacts(self.store, self.id)


class _RbUsers:
    def __init__(self, store):
        self.store = store

    def stream(self):
        return [MagicMock(id=uid) for uid in self.store]

    def document(self, uid):
        return _RbUserDoc(self.store, uid)


class _RbBatch:
    def __init__(self, db):
        self.db, self.ops = db, []

    def update(self, ref, data):
        self.ops.append((ref, data))

    def commit(self):
        self.db.commits.append(len(self.ops))
        for (_r, uid, cid), data in self.ops:
            self.db.store[uid][cid].update(data)
        self.ops = []


class _RbDb:
    def __init__(self, store):
        self.store, self.commits = store, []

    def collection(self, name):
        assert name == "users"
        return _RbUsers(self.store)

    def batch(self):
        return _RbBatch(self)


def _rb_store():
    return {
        "u1": {
            "a": {"pipelineStage": "email_opened", "emailOpenCount": 2, "emailOpenedAt": OPENED_AFTER},
            "b": {"pipelineStage": "waiting_on_reply"},
            "c": {"pipelineStage": "replied", "emailOpenedAt": OPENED_AFTER},
        },
        "u2": {"d": {"pipelineStage": "email_opened"}},
        "u3": {"e": {"pipelineStage": "draft_created"}},
    }


def test_rollback_update_only_touches_email_opened():
    from scripts.rollback_email_opened import rollback_update

    assert rollback_update({"pipelineStage": "email_opened"})["pipelineStage"] == "waiting_on_reply"
    for stage in ("waiting_on_reply", "email_sent", "replied", "draft_created", None):
        assert rollback_update({"pipelineStage": stage}) is None
    # Open stamps are history, not stage: the rollback leaves them.
    update = rollback_update({"pipelineStage": "email_opened", "emailOpenedAt": OPENED_AFTER})
    assert set(update) <= {"pipelineStage", "updatedAt"}


def test_rollback_dry_run_counts_and_writes_nothing():
    from scripts.rollback_email_opened import run

    store = _rb_store()
    db = _RbDb(store)
    counts = run(db, apply=False, log=lambda *_a: None)

    assert counts == {"u1": 1, "u2": 1}
    assert db.commits == []
    assert store["u1"]["a"]["pipelineStage"] == "email_opened"


def test_rollback_apply_moves_opened_back_in_batches():
    from scripts.rollback_email_opened import run

    store = _rb_store()
    store["u4"] = {f"x{i}": {"pipelineStage": "email_opened"} for i in range(5)}
    db = _RbDb(store)
    counts = run(db, apply=True, batch_limit=2, log=lambda *_a: None)

    assert counts == {"u1": 1, "u2": 1, "u4": 5}
    assert all(n <= 2 for n in db.commits) and sum(db.commits) == 7
    for uid, contacts in store.items():
        for c in contacts.values():
            assert c["pipelineStage"] != "email_opened"
    assert store["u1"]["a"]["pipelineStage"] == "waiting_on_reply"
    assert store["u1"]["a"]["emailOpenCount"] == 2
    assert store["u1"]["c"]["pipelineStage"] == "replied"
    # Idempotent.
    assert run(db, apply=True, log=lambda *_a: None) == {}


# ---------------------------------------------------------------------------
# First email from the Inbox: no "Re:", a real stage, no stale Gmail draft
# ---------------------------------------------------------------------------

def _send_first(data):
    from app.services import outbox_service

    ref = MagicMock()
    service = MagicMock()
    service.users().messages().send().execute.return_value = {"id": "m1", "threadId": "t-new"}
    with (
        patch.object(outbox_service, "_get_contact", return_value=(ref, data)),
        patch.object(outbox_service, "_load_user_gmail_creds", return_value=object()),
        patch.object(outbox_service, "_gmail_service", return_value=service),
        patch.object(outbox_service, "get_db", return_value=MagicMock()),
        patch.object(email_tracking, "start", return_value=None),
        patch("app.services.cooldown_service.record_outreach"),
        patch("app.utils.metrics_events.log_event"),
    ):
        outbox_service.send_reply_for_contact("u1", "c1", "Hi Pat, would you have 15 minutes next week?")
    import base64
    import email as _email
    raw = service.users().messages().send.call_args.kwargs["body"]["raw"]
    return ref.update.call_args[0][0], _email.message_from_bytes(base64.urlsafe_b64decode(raw)), service


def test_first_email_keeps_its_subject_and_cleans_up_the_gmail_draft():
    from firebase_admin import firestore

    updates, msg, service = _send_first({
        "email": "pat@evercore.com", "pipelineStage": "generated", "emailSubject": "Evercore question",
        "gmailDraftId": "d1", "gmailDraftUrl": "https://mail.google.com/d1",
    })
    assert msg["subject"] == "Evercore question"
    assert updates["pipelineStage"] == "email_sent"
    assert updates["emailSent"] is True and updates["draftStillExists"] is False
    assert updates["gmailDraftId"] is firestore.DELETE_FIELD
    assert updates["gmailThreadId"] == "t-new"
    service.users().drafts().delete.assert_called_once_with(userId="me", id="d1")


def test_reply_in_a_thread_still_gets_re():
    updates, msg, service = _send_first({
        "email": "pat@evercore.com", "pipelineStage": "replied", "emailSubject": "Evercore question",
        "gmailThreadId": "t1", "emailSent": True, "gmailDraftId": "d-old",
    })
    assert msg["subject"] == "Re: Evercore question"
    assert "gmailDraftId" not in updates
    service.users().drafts().delete.assert_not_called()
