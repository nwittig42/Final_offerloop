"""Org join/leave/list routes (OFF-42)."""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

FAKE_USER = {"uid": "test-user-id", "email": "test@example.com"}


@pytest.fixture(autouse=True)
def _bypass_firebase_auth():
    with patch("firebase_admin._apps", {"[DEFAULT]": MagicMock()}), \
         patch("firebase_admin.auth.verify_id_token", return_value=FAKE_USER):
        yield


def _snap(data, exists=True, doc_id="x"):
    s = MagicMock(exists=exists, id=doc_id)
    s.to_dict.return_value = data
    return s


def _db(org_doc, user_docs, password_hash=None, attempts=None):
    """user_docs: successive user snapshots returned by user_ref.get().
    password_hash: the org_secrets hash (None = no password set).
    attempts: the user's orgJoinAttempts doc for the org (None = none)."""
    db = MagicMock()
    org_ref, user_ref = MagicMock(), MagicMock()
    org_ref.get.return_value = org_doc
    user_ref.get.side_effect = list(user_docs)
    orgs_col, users_col = MagicMock(), MagicMock()
    orgs_col.document.return_value = org_ref
    orgs_col.stream.return_value = [_snap({"name": "USC Tau Kappa Epsilon", "shortName": "USC TKE", "joinable": True}, doc_id="usc_tke"),
                                    _snap({"name": "Hidden"}, doc_id="hidden")]
    users_col.document.return_value = user_ref
    attempts_ref = user_ref.collection.return_value.document.return_value
    attempts_ref.get.return_value = _snap(attempts or {}, exists=attempts is not None)
    secrets_col = MagicMock()
    secrets_col.document.return_value.get.return_value = _snap(
        {"passwordHash": password_hash} if password_hash else {}, exists=password_hash is not None)
    db.collection.side_effect = lambda name: {"orgs": orgs_col, "users": users_col, "org_secrets": secrets_col}[name]
    db.attempts_ref = attempts_ref
    return db, user_ref


@pytest.fixture
def client():
    # Just this blueprint: the full create_app() pulls in WeasyPrint's native libs.
    from flask import Flask
    from app.routes.orgs import orgs_bp
    app = Flask(__name__)
    app.register_blueprint(orgs_bp)
    app.config["TESTING"] = True
    return app.test_client()


def _call(client, db, method, path, body=None):
    with patch("app.routes.orgs.get_db", return_value=db):
        kwargs = {"headers": {"Authorization": "Bearer t"}}
        if body is not None:
            kwargs["json"] = body
        return getattr(client, method)(path, **kwargs)


from werkzeug.security import generate_password_hash  # noqa: E402

PW_HASH = generate_password_hash("tke-1899")


def test_join_unknown_org_404(client):
    db, user_ref = _db(_snap({}, exists=False), [])
    r = _call(client, db, "post", "/api/orgs/nope/join")
    assert r.status_code == 404
    user_ref.set.assert_not_called()


def test_digit_leading_org_id_is_rejected_not_500(client):
    db, user_ref = _db(_snap({"joinable": True}), [_snap({"orgMemberships": {}})])
    assert _call(client, db, "post", "/api/orgs/2025club/leave").status_code == 404
    assert _call(client, db, "post", "/api/orgs/2025club/join").status_code == 404
    user_ref.update.assert_not_called()


def test_join_rejects_non_joinable_org(client):
    db, user_ref = _db(_snap({"name": "x"}), [])
    assert _call(client, db, "post", "/api/orgs/usc_tke/join").status_code == 404
    user_ref.set.assert_not_called()


def test_join_with_right_password_writes_active_membership(client):
    after = _snap({"orgMemberships": {"usc_tke": {"status": "active"}}})
    db, user_ref = _db(_snap({"joinable": True}), [after], password_hash=PW_HASH,
                       attempts={"count": 2, "windowStart": 9e12})
    r = _call(client, db, "post", "/api/orgs/usc_tke/join", {"password": "tke-1899"})
    assert r.status_code == 200
    assert r.get_json()["memberships"] == ["usc_tke"]
    (payload,), kwargs = user_ref.set.call_args
    assert kwargs == {"merge": True}
    assert payload["orgMemberships"]["usc_tke"]["status"] == "active"
    db.attempts_ref.delete.assert_called_once()  # success clears the throttle


def test_join_wrong_or_missing_password_is_refused_and_counted(client):
    for body in ({"password": "nope"}, {}, {"password": ""}):
        db, user_ref = _db(_snap({"joinable": True}), [], password_hash=PW_HASH)
        r = _call(client, db, "post", "/api/orgs/usc_tke/join", body)
        assert r.status_code == 403 and r.get_json()["error"] == "wrong_password"
        assert r.get_json()["attemptsLeft"] == 7
        user_ref.set.assert_not_called()
        (counted,), _ = db.attempts_ref.set.call_args
        assert counted["count"] == 1


def test_join_without_a_password_set_is_closed(client):
    db, user_ref = _db(_snap({"joinable": True}), [])
    r = _call(client, db, "post", "/api/orgs/usc_tke/join", {"password": "anything"})
    assert r.status_code == 403 and r.get_json()["error"] == "join_closed"
    user_ref.set.assert_not_called()


def test_join_throttles_after_max_attempts(client):
    import time
    db, user_ref = _db(_snap({"joinable": True}), [], password_hash=PW_HASH,
                       attempts={"count": 8, "windowStart": time.time() - 60})
    r = _call(client, db, "post", "/api/orgs/usc_tke/join", {"password": "tke-1899"})
    assert r.status_code == 429
    user_ref.set.assert_not_called()


def test_join_throttle_window_expires(client):
    import time
    after = _snap({"orgMemberships": {"usc_tke": {"status": "active"}}})
    db, _ = _db(_snap({"joinable": True}), [after], password_hash=PW_HASH,
                attempts={"count": 8, "windowStart": time.time() - 3600})
    assert _call(client, db, "post", "/api/orgs/usc_tke/join", {"password": "tke-1899"}).status_code == 200


def test_list_never_returns_a_password(client):
    db, _ = _db(_snap({}), [_snap({})], password_hash=PW_HASH)
    body = _call(client, db, "get", "/api/orgs").get_json()
    assert "password" not in str(body).lower()


def test_leave_deletes_membership(client):
    before = _snap({"orgMemberships": {"usc_tke": {"status": "active"}}})
    db, user_ref = _db(_snap({}), [before, _snap({"orgMemberships": {}})])
    r = _call(client, db, "post", "/api/orgs/usc_tke/leave")
    assert r.status_code == 200
    assert r.get_json()["memberships"] == []
    (update,), _ = user_ref.update.call_args
    assert list(update) == ["orgMemberships.usc_tke"]


def test_list_returns_joinable_orgs_and_memberships(client):
    db, _ = _db(_snap({}), [_snap({"orgMemberships": {"usc_tke": {"status": "active"}}})])
    r = _call(client, db, "get", "/api/orgs")
    body = r.get_json()
    assert [o["id"] for o in body["orgs"]] == ["usc_tke"]
    assert body["memberships"] == ["usc_tke"]


# ---------------------------------------------------------------------------
# Browsable directory + unlock
# ---------------------------------------------------------------------------
from google.api_core.exceptions import AlreadyExists  # noqa: E402

TKE = {"id": "usc_tke", "name": "USC Tau Kappa Epsilon", "shortName": "USC TKE", "school": "USC",
       "type": "fraternity", "memberCount": 2127, "aliases": ["tke"]}


def _contact(key="p1", email="jamie@gmail.com"):
    return {"PersonKey": key, "FirstName": "Jamie", "LastName": "Park", "Title": "", "Company": "",
            "OrgHeadline": "Goldman Sachs Analyst", "OrgYear": 2017, "OrgBadge": "USC TKE Alumni '17",
            "LinkedIn": "", "PhotoUrl": "", "College": "USC", "Email": email, "Phone": "", "PersonalEmail": email}


def _dir_call(client, method, path, member=True, contacts=(), body=None, deduct=(True, 490)):
    db = MagicMock()
    reveal_ref = MagicMock()
    user_ref = db.collection.return_value.document.return_value
    user_ref.collection.return_value.document.return_value = reveal_ref
    user_ref.collection.return_value.where.return_value.stream.return_value = []
    deduct_mock = MagicMock(return_value=deduct)
    with patch("app.routes.orgs.get_db", return_value=db), \
         patch("app.routes.orgs._member_org", return_value=TKE if member else None), \
         patch("app.routes.orgs.directory_contacts", return_value=list(contacts)), \
         patch("app.services.auth.deduct_credits_atomic", deduct_mock):
        kwargs = {"headers": {"Authorization": "Bearer t"}}
        if body is not None:
            kwargs["json"] = body
        r = getattr(client, method)(path, **kwargs)
    return r, reveal_ref, deduct_mock


def test_members_requires_membership(client):
    r, _, _ = _dir_call(client, "get", "/api/orgs/usc_tke/members", member=False)
    assert r.status_code == 403


def test_members_rows_never_carry_email_or_phone(client):
    r, _, _ = _dir_call(client, "get", "/api/orgs/usc_tke/members", contacts=[_contact()])
    body = r.get_json()
    assert r.status_code == 200
    row = body["members"][0]
    assert row["hasEmail"] is True and row["badge"] == "USC TKE Alumni '17"
    assert "jamie@gmail.com" not in json_dumps(body["members"])
    assert not any(k.lower().endswith(("email", "phone")) and k != "hasEmail" for k in row)


def json_dumps(v):
    import json
    return json.dumps(v)


def test_reveal_charges_find_contact_once(client):
    r, reveal_ref, deduct = _dir_call(client, "post", "/api/orgs/usc_tke/reveal", contacts=[_contact()], body={"personKey": "p1"})
    assert r.status_code == 200
    assert r.get_json()["contact"]["Email"] == "jamie@gmail.com"
    deduct.assert_called_once_with("test-user-id", 10, "org_directory_reveal")
    reveal_ref.create.assert_called_once()


def test_reveal_already_unlocked_is_free(client):
    db_reveal_exists = AlreadyExists("exists")
    db = MagicMock()
    reveal_ref = db.collection.return_value.document.return_value.collection.return_value.document.return_value
    reveal_ref.create.side_effect = db_reveal_exists
    deduct = MagicMock()
    with patch("app.routes.orgs.get_db", return_value=db), \
         patch("app.routes.orgs._member_org", return_value=TKE), \
         patch("app.routes.orgs.directory_contacts", return_value=[_contact()]), \
         patch("app.services.auth.deduct_credits_atomic", deduct):
        r = client.post("/api/orgs/usc_tke/reveal", json={"personKey": "p1"}, headers={"Authorization": "Bearer t"})
    assert r.get_json()["already_revealed"] is True
    deduct.assert_not_called()


def test_reveal_no_email_is_not_charged(client):
    r, _, deduct = _dir_call(client, "post", "/api/orgs/usc_tke/reveal", contacts=[_contact(email="")], body={"personKey": "p1"})
    assert r.status_code == 404 and r.get_json()["error"] == "no_email"
    deduct.assert_not_called()


def test_reveal_out_of_credits_releases_the_claim(client):
    r, reveal_ref, _ = _dir_call(client, "post", "/api/orgs/usc_tke/reveal", contacts=[_contact()],
                                 body={"personKey": "p1"}, deduct=(False, 3))
    assert r.status_code == 402
    reveal_ref.delete.assert_called_once()


def test_reveal_requires_membership(client):
    r, _, deduct = _dir_call(client, "post", "/api/orgs/usc_tke/reveal", member=False, body={"personKey": "p1"})
    assert r.status_code == 403
    deduct.assert_not_called()


def test_member_org_gate():
    from flask import Flask, request
    from app.routes import orgs as orgs_mod
    app = Flask(__name__)
    org_doc = _snap({"name": "USC Tau Kappa Epsilon", "shortName": "USC TKE", "joinable": True}, doc_id="usc_tke")
    cases = [
        ({"orgMemberships": {"usc_tke": {"status": "active"}}}, "usc_tke"),
        ({"orgMemberships": {"usc_tke": {"status": "pending"}}}, None),
        ({}, None),
    ]
    for user_data, expected in cases:
        db, _ = _db(org_doc, [_snap(user_data)])
        with app.test_request_context():
            request.firebase_user = FAKE_USER
            got = orgs_mod._member_org(db, "usc_tke")
        assert (got or {}).get("id") == expected, user_data
