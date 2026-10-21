"""POST /api/users/onboarding-event accepts every step the onboarding flow logs.

Onboarding v2 logs `intro` and `confirm`, and the org step logs `orgs`, which
was missing from the allow-list and 400'd silently (the client ignores the
response). Runs in-process: auth is faked and log_event is stubbed, so
nothing reaches Firestore.
"""
from unittest.mock import patch

import firebase_admin
import pytest
from flask import Flask

from app.routes.users import users_bp


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(firebase_admin._apps, "[DEFAULT]", object())
    monkeypatch.setattr("app.extensions.fb_auth.verify_id_token", lambda *a, **k: {"uid": "u1"})
    app = Flask(__name__)
    app.register_blueprint(users_bp)
    return app.test_client()


def _post(client, step, event="viewed"):
    with patch("app.utils.metrics_events.log_event") as log_event:
        resp = client.post(
            "/api/users/onboarding-event",
            json={"event": event, "step": step, "skipped": False},
            headers={"Authorization": "Bearer x"},
        )
    return resp, log_event


@pytest.mark.unit
@pytest.mark.parametrize("step", ["intro", "source", "resume_builder", "confirm", "orgs", "slides_1"])
def test_onboarding_v2_steps_are_accepted(client, step):
    resp, log_event = _post(client, step)
    assert resp.status_code == 200, resp.get_json()
    log_event.assert_called_once_with("u1", "onboarding_step_viewed", {"step": step, "skipped": False})


@pytest.mark.unit
def test_unknown_step_is_still_rejected(client):
    resp, log_event = _post(client, "not_a_step")
    assert resp.status_code == 400
    log_event.assert_not_called()
