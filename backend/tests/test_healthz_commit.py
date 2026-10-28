"""
/healthz reports the commit it is running, so a deploy can be verified with
one unauthenticated request.

The gap this closes: old and new code answered /healthz identically, so
confirming a deploy meant the Render dashboard or an authenticated API call.
OFF-63 went to production unverified for ten minutes because of it.
"""

import importlib
from unittest.mock import patch

import pytest
from flask import Flask

from app.routes import health as health_module


@pytest.fixture
def healthz_client():
    """A bare app with only health_bp on it.

    Deliberately not the shared `client` fixture: that boots the full wsgi
    app, which refuses to mount the MCP blueprint when the active Firebase
    project is prod and FLASK_ENV is testing. /healthz has no dependencies —
    testing it should not require the whole stack to come up.
    """
    app = Flask(__name__)
    app.register_blueprint(health_module.health_bp)
    return app.test_client()


@pytest.mark.unit
def test_healthz_is_still_a_liveness_check(healthz_client):
    """Whatever we add, /healthz must keep answering 200/ok — it is wired to
    uptime checks, not just to humans."""
    resp = healthz_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


@pytest.mark.unit
def test_healthz_reports_commit_and_branch(healthz_client):
    body = healthz_client.get("/healthz").get_json()
    assert "commit" in body
    assert "branch" in body


@pytest.mark.unit
def test_commit_is_unknown_off_render():
    """Local and CI have no RENDER_GIT_COMMIT. Absence reports "unknown"
    rather than empty-string or a crash."""
    with patch.dict("os.environ", {}, clear=True):
        reloaded = importlib.reload(health_module)
        assert reloaded.RUNNING_COMMIT == "unknown"
        assert reloaded.RUNNING_BRANCH == "unknown"
    importlib.reload(health_module)


@pytest.mark.unit
def test_commit_is_read_from_render_env():
    sha = "8a300cb620b247c8d4c3a5676a8a2e685ede7b1a"
    with patch.dict("os.environ",
                    {"RENDER_GIT_COMMIT": sha, "RENDER_GIT_BRANCH": "main"},
                    clear=True):
        reloaded = importlib.reload(health_module)
        assert reloaded.RUNNING_COMMIT == sha
        assert reloaded.RUNNING_BRANCH == "main"
    importlib.reload(health_module)


@pytest.mark.unit
def test_blank_env_var_reads_as_unknown():
    """An env var set to empty string must not surface as an empty commit —
    that would read as a deploy mystery rather than as "not on Render"."""
    with patch.dict("os.environ",
                    {"RENDER_GIT_COMMIT": "   ", "RENDER_GIT_BRANCH": ""},
                    clear=True):
        reloaded = importlib.reload(health_module)
        assert reloaded.RUNNING_COMMIT == "unknown"
        assert reloaded.RUNNING_BRANCH == "unknown"
    importlib.reload(health_module)
