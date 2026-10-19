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


def _db(org_doc, user_docs):
    """user_docs: successive user snapshots returned by user_ref.get()."""
    db = MagicMock()
    org_ref, user_ref = MagicMock(), MagicMock()
    org_ref.get.return_value = org_doc
    user_ref.get.side_effect = list(user_docs)
    orgs_col, users_col = MagicMock(), MagicMock()
    orgs_col.document.return_value = org_ref
    orgs_col.stream.return_value = [_snap({"name": "USC Tau Kappa Epsilon", "shortName": "USC TKE", "joinable": True}, doc_id="usc_tke"),
                                    _snap({"name": "Hidden"}, doc_id="hidden")]
    users_col.document.return_value = user_ref
    db.collection.side_effect = lambda name: {"orgs": orgs_col, "users": users_col}[name]
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


def _call(client, db, method, path):
    with patch("app.routes.orgs.get_db", return_value=db):
        return getattr(client, method)(path, headers={"Authorization": "Bearer t"})


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


def test_join_writes_active_membership(client):
    after = _snap({"orgMemberships": {"usc_tke": {"status": "active"}}})
    db, user_ref = _db(_snap({"joinable": True}), [after])
    r = _call(client, db, "post", "/api/orgs/usc_tke/join")
    assert r.status_code == 200
    assert r.get_json()["memberships"] == ["usc_tke"]
    (payload,), kwargs = user_ref.set.call_args
    assert kwargs == {"merge": True}
    assert payload["orgMemberships"]["usc_tke"]["status"] == "active"


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
