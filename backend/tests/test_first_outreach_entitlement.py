"""One free first outreach for new accounts (app.services.first_outreach)
and its use in POST /api/ranker/candidates/<id>/reveal-email.

In-process only: Firestore is tests.fake_firestore, Firebase Auth's creation
time is injected, and every email-lookup vendor (Hunter, FullEnrich,
Coresignal) is patched out, so nothing paid or remote runs.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import firebase_admin
import pytest
from flask import Flask

from app.services import first_outreach as fo
from tests.fake_firestore import FakeDB

CUTOFF = "2026-11-01T00:00:00Z"
NEW = datetime(2026, 11, 2, tzinfo=timezone.utc)
OLD = datetime(2026, 10, 1, tzinfo=timezone.utc)
GRANT = "users/u1/entitlements/firstOutreach"


def created(at):
    return lambda uid: at


@pytest.fixture
def launched(monkeypatch):
    monkeypatch.setenv(fo.CUTOFF_ENV, CUTOFF)


# ── Eligibility ──────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("value", [None, "", "not-a-date"])
def test_off_until_the_cutoff_is_set(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(fo.CUTOFF_ENV, raising=False)
    else:
        monkeypatch.setenv(fo.CUTOFF_ENV, value)
    db = FakeDB()
    assert fo.status(db, "u1", created(NEW)) == {"eligible": False, "status": "ineligible"}
    assert fo.reserve(db, "u1", "c1", created(NEW)) == (None, "unavailable")
    assert GRANT not in db.store


@pytest.mark.unit
def test_accounts_created_before_the_cutoff_are_unchanged(launched):
    assert fo.reserve(FakeDB(), "u1", "c1", created(OLD)) == (None, "unavailable")
    # Unknown creation time (Auth lookup failed) fails closed.
    assert fo.reserve(FakeDB(), "u1", "c1", created(None)) == (None, "unavailable")


@pytest.mark.unit
def test_a_date_only_cutoff_works(monkeypatch):
    monkeypatch.setenv(fo.CUTOFF_ENV, "2026-11-01")
    assert fo.status(FakeDB(), "u1", created(NEW))["eligible"] is True


# ── Reserve / consume / release ──────────────────────────────────────────

@pytest.mark.unit
def test_only_one_concurrent_reveal_gets_it(launched):
    db = FakeDB()
    first, _ = fo.reserve(db, "u1", "c1", created(NEW))
    assert first is not None
    assert fo.reserve(db, "u1", "c2", created(NEW)) == (None, "in_use")
    assert fo.status(db, "u1", created(NEW))["status"] == "reserved"


@pytest.mark.unit
def test_a_miss_gives_it_back(launched):
    db = FakeDB()
    r, _ = fo.reserve(db, "u1", "c1", created(NEW))
    fo.release(db, "u1", r)
    assert GRANT not in db.store
    assert fo.status(db, "u1", created(NEW))["status"] == "available"
    again, _ = fo.reserve(db, "u1", "c2", created(NEW))
    assert again is not None


@pytest.mark.unit
def test_consumed_once_and_never_again(launched):
    db = FakeDB()
    r, _ = fo.reserve(db, "u1", "c1", created(NEW))
    assert fo.consume(db, "u1", r, "c1") is True
    assert db.store[GRANT]["status"] == "consumed"
    assert fo.reserve(db, "u1", "c2", created(NEW)) == (None, "unavailable")
    assert fo.status(db, "u1", created(NEW)) == {"eligible": False, "status": "consumed"}


@pytest.mark.unit
def test_a_crashed_reservation_frees_itself(launched):
    db = FakeDB()
    r, _ = fo.reserve(db, "u1", "c1", created(NEW))
    db.store[GRANT]["reservedAt"] = (datetime.now(timezone.utc) - timedelta(seconds=fo.RESERVATION_TTL_SECONDS + 5)).isoformat()
    taken, _ = fo.reserve(db, "u1", "c2", created(NEW))
    assert taken is not None and db.store[GRANT]["candidateId"] == "c2"
    # The crashed request can no longer consume or release the new holder's reservation.
    assert fo.consume(db, "u1", r, "c1") is False
    fo.release(db, "u1", r)
    assert db.store[GRANT]["reservationId"] == taken.reservation_id


# ── reveal-email ─────────────────────────────────────────────────────────

FIRM = "firm_employees/c1"


@pytest.fixture
def reveal(monkeypatch):
    from app.routes import ranker
    from app.services.firm_cache.schema import FIRM_EMPLOYEES_COLLECTION

    monkeypatch.setitem(firebase_admin._apps, "[DEFAULT]", object())
    monkeypatch.setattr("app.extensions.fb_auth.verify_id_token", lambda *a, **k: {"uid": "u1"})
    db = FakeDB({
        f"{FIRM_EMPLOYEES_COLLECTION}/c1": {"first_name": "Pat", "last_name": "Kim",
                                            "company_display": "Evercore", "linkedin_url": "https://linkedin.com/in/pat"},
    })
    monkeypatch.setattr(ranker, "get_db", lambda: db)
    monkeypatch.setattr(ranker, "log_recommendation_event", MagicMock())
    monkeypatch.setattr(fo, "account_created_at", lambda uid: NEW)
    mocks = {
        "deduct": MagicMock(return_value=(True, 292)),
        "refund": MagicMock(),
        "hunter": MagicMock(return_value=None),
        "fullenrich": MagicMock(return_value=None),
        "linkedin": MagicMock(return_value=None),
    }
    app = Flask(__name__)
    app.register_blueprint(ranker.ranker_bp)
    with patch("app.services.auth.deduct_credits_atomic", mocks["deduct"]), \
         patch.object(ranker, "_try_refund", mocks["refund"]), \
         patch("app.services.email_fast.hunter_email", mocks["hunter"]), \
         patch("app.services.fullenrich_client.find_work_email", mocks["fullenrich"]), \
         patch("app.services.linkedin_backfill.find_linkedin_for_contact", mocks["linkedin"]):
        yield app.test_client(), db, mocks


def _reveal(client, **body):
    return client.post("/api/ranker/candidates/c1/reveal-email", json=body, headers={"Authorization": "Bearer x"})


def _hit(mocks):
    mocks["hunter"].return_value = {"email": "pat@evercore.com", "confidence": 99}


@pytest.mark.unit
def test_free_reveal_found_uses_the_benefit_not_credits(reveal, launched):
    client, db, mocks = reveal
    _hit(mocks)
    resp = _reveal(client, use_first_outreach=True)
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["free"] is True and resp.get_json()["charged"] is False
    mocks["deduct"].assert_not_called()
    mocks["refund"].assert_not_called()
    assert db.store[GRANT]["status"] == "consumed"


@pytest.mark.unit
def test_free_reveal_miss_keeps_the_benefit(reveal, launched):
    client, db, mocks = reveal
    resp = _reveal(client, use_first_outreach=True)
    assert resp.status_code == 200
    assert resp.get_json()["email"] is None and resp.get_json()["free"] is False
    mocks["deduct"].assert_not_called()
    mocks["refund"].assert_not_called()
    assert GRANT not in db.store


@pytest.mark.unit
def test_ineligible_free_request_is_refused_without_a_charge_or_lookup(reveal, monkeypatch):
    monkeypatch.delenv(fo.CUTOFF_ENV, raising=False)
    client, db, mocks = reveal
    resp = _reveal(client, use_first_outreach=True)
    assert resp.status_code == 409
    assert resp.get_json() == {"error": "free_outreach_unavailable", "required": 8}
    mocks["deduct"].assert_not_called()
    mocks["hunter"].assert_not_called()
    mocks["fullenrich"].assert_not_called()


@pytest.mark.unit
def test_a_second_free_request_while_one_is_running_is_refused(reveal, launched):
    client, db, mocks = reveal
    assert fo.reserve(db, "u1", "c9", lambda uid: NEW)[0] is not None
    resp = _reveal(client, use_first_outreach=True)
    assert resp.status_code == 409 and resp.get_json()["error"] == "free_outreach_in_use"
    mocks["hunter"].assert_not_called()


@pytest.mark.unit
def test_standard_reveal_is_unchanged(reveal, launched):
    client, db, mocks = reveal
    _hit(mocks)
    resp = _reveal(client)
    assert resp.status_code == 200
    assert resp.get_json()["charged"] is True and resp.get_json()["free"] is False
    mocks["deduct"].assert_called_once_with("u1", 8, "reveal_email")
    assert GRANT not in db.store


@pytest.mark.unit
def test_standard_miss_still_refunds(reveal, launched):
    client, db, mocks = reveal
    _reveal(client)
    mocks["deduct"].assert_called_once()
    mocks["refund"].assert_called_once_with("u1", 8, "reveal_email_refund")


@pytest.mark.unit
def test_cached_reveal_never_touches_the_benefit(reveal, launched):
    client, db, mocks = reveal
    db.store["users/u1/revealedEmails/c1"] = {"email": "pat@evercore.com", "verified": True,
                                              "revealed_at": datetime.now(timezone.utc).isoformat()}
    resp = _reveal(client, use_first_outreach=True)
    assert resp.status_code == 200 and resp.get_json()["cached"] is True
    assert GRANT not in db.store
    mocks["deduct"].assert_not_called()


@pytest.mark.unit
def test_status_endpoint(reveal, launched):
    client, db, mocks = reveal
    resp = client.get("/api/ranker/first-outreach", headers={"Authorization": "Bearer x"})
    assert resp.get_json() == {"eligible": True, "status": "available"}
