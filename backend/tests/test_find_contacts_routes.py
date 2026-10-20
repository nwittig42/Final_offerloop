"""Routes for Find contacts: credits, status codes, email reveal."""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FAKE_USER = {"uid": "test-user-id", "email": "test@example.com"}
URL = "/api/job-board/find-contacts"
EMAIL_URL = "/api/job-board/find-contacts/email"
HEADERS = {"Authorization": "Bearer fake-token"}
MOD = "backend.app.routes.find_contacts"


@pytest.fixture(autouse=True)
def _bypass_firebase_auth():
    with patch("firebase_admin._apps", {"[DEFAULT]": MagicMock()}), \
         patch("firebase_admin.auth.verify_id_token", return_value=FAKE_USER):
        yield


def _db(unlocked=False, school="USC"):
    db = MagicMock()
    user_ref = MagicMock()
    user_doc = MagicMock()
    user_doc.exists = True
    user_doc.to_dict.return_value = {"academics": {"university": school}}
    user_ref.get.return_value = user_doc
    unlock_ref = MagicMock()
    unlock_ref.get.return_value.exists = unlocked
    user_ref.collection.return_value.document.return_value = unlock_ref
    db.collection.return_value.document.return_value = user_ref
    db.unlock_ref = unlock_ref
    return db


def _people(n):
    return [{"person_key": f"k{i}", "name": f"P{i}"} for i in range(n)]


def _ready(n):
    return {"status": "ready", "company": "Acme", "team": "Data", "team_source": "job title",
            "people": _people(n)}


def _post(client, db, search=None, credits=100, deduct=(True, 85), body=None, url=URL):
    search = search or MagicMock(return_value=_ready(3))
    with patch(f"{MOD}.get_db", return_value=db), \
         patch(f"{MOD}.check_and_reset_credits", return_value=credits), \
         patch(f"{MOD}.find_contacts_for_job", search), \
         patch(f"{MOD}.deduct_credits_atomic", return_value=deduct) as ded:
        resp = client.post(url, json=body if body is not None else {"jobId": "j1"}, headers=HEADERS)
    return resp, search, ded


def test_charges_5_per_person_once(client):
    db = _db()
    resp, search, ded = _post(client, db)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["creditsCharged"] == 15 and body["credits"] == 85
    ded.assert_called_once_with("test-user-id", 15, "find_contacts")
    search.assert_called_once_with(db, "j1", "USC")
    db.unlock_ref.set.assert_called_once()
    assert db.unlock_ref.set.call_args[0][0]["charged"] == 15

    resp2, _, ded2 = _post(client, _db(unlocked=True), credits=85)
    assert resp2.status_code == 200
    assert resp2.get_json()["creditsCharged"] == 0
    ded2.assert_not_called()


def test_insufficient_credits_before_search(client):
    resp, search, ded = _post(client, _db(), credits=14)
    assert resp.status_code == 402
    assert resp.get_json()["error"] == "Insufficient credits"
    search.assert_not_called()
    ded.assert_not_called()


def test_unlocked_student_with_low_credits_is_not_blocked(client):
    resp, search, _ = _post(client, _db(unlocked=True), credits=0)
    assert resp.status_code == 200
    search.assert_called_once()


def test_searching_returns_202(client):
    db = _db()
    resp, _, ded = _post(client, db, search=MagicMock(return_value={"status": "searching"}))
    assert resp.status_code == 202
    ded.assert_not_called()
    db.unlock_ref.set.assert_not_called()


def test_unknown_job_404(client):
    resp, _, ded = _post(client, _db(), search=MagicMock(return_value={"status": "not_found"}))
    assert resp.status_code == 404
    ded.assert_not_called()


def test_missing_job_id_400(client):
    resp, search, _ = _post(client, _db(), body={})
    assert resp.status_code == 400
    search.assert_not_called()


def test_ready_with_no_people_charges_zero_and_unlocks(client):
    db = _db()
    resp, _, ded = _post(client, db, search=MagicMock(return_value=_ready(0)))
    assert resp.status_code == 200
    assert resp.get_json()["creditsCharged"] == 0
    ded.assert_not_called()
    assert db.unlock_ref.set.call_args[0][0]["charged"] == 0


def test_failed_deduction_returns_402_without_unlock(client):
    db = _db()
    resp, _, _ = _post(client, db, deduct=(False, 3))
    assert resp.status_code == 402
    db.unlock_ref.set.assert_not_called()


def test_email_route_no_charge(client):
    db = _db()
    with patch(f"{MOD}.get_db", return_value=db), \
         patch(f"{MOD}.reveal_email", return_value={"email": "a@b.com", "status": "found"}) as rev, \
         patch(f"{MOD}.deduct_credits_atomic") as ded:
        resp = client.post(EMAIL_URL, json={"jobId": "j1", "personKey": "k1"}, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.get_json() == {"email": "a@b.com", "status": "found"}
    rev.assert_called_once_with(db, "j1", "k1")
    ded.assert_not_called()


def test_email_route_not_available_404(client):
    with patch(f"{MOD}.get_db", return_value=_db()), \
         patch(f"{MOD}.reveal_email", return_value=None):
        resp = client.post(EMAIL_URL, json={"jobId": "j1", "personKey": "k1"}, headers=HEADERS)
    assert resp.status_code == 404


def test_requires_auth(client):
    resp = client.post(URL, json={"jobId": "j1"})
    assert resp.status_code == 401
