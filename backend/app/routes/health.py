"""
Health check routes
"""
import os

from flask import Blueprint, jsonify
from app.services.gmail_client import get_gmail_service
import firebase_admin
from app.extensions import get_db

health_bp = Blueprint('health', __name__)

# Render sets these on every build. Read once at import: they are fixed for
# the life of the process, and a health check should not re-read the
# environment on every probe.
# Strip BEFORE the fallback: an env var set to "" or whitespace must read as
# "unknown", not as an empty commit. An empty string looks like a deploy
# mystery; "unknown" correctly says "not running on Render".
RUNNING_COMMIT = (os.environ.get("RENDER_GIT_COMMIT") or "").strip() or "unknown"
RUNNING_BRANCH = (os.environ.get("RENDER_GIT_BRANCH") or "").strip() or "unknown"


@health_bp.route('/ping')
def ping():
    return "pong"


@health_bp.route('/health')
def health():
    """Health check endpoint"""
    # Check Firebase status
    firebase_status = 'unknown'
    firebase_error = None
    try:
        if firebase_admin._apps:
            db = get_db()
            if db:
                firebase_status = 'initialized'
            else:
                firebase_status = 'apps_exist_but_db_none'
        else:
            firebase_status = 'not_initialized'
    except Exception as e:
        firebase_status = 'error'
        firebase_error = str(e)
    
    # Check Gmail service availability (without verbose logging)
    gmail_available = False
    try:
        gmail_service = get_gmail_service()
        gmail_available = gmail_service is not None
    except Exception:
        gmail_available = False
    
    return jsonify({
        'status': 'healthy',
        'tiers': ['free', 'pro'],
        'email_system': 'interesting_mutual_interests_v2',
        'services': {
            'pdl': 'connected',
            'openai': 'connected',
            'gmail': 'connected' if gmail_available else 'unavailable',
            'firebase': {
                'status': firebase_status,
                'error': firebase_error
            }
        }
    })


@health_bp.get("/healthz")
def healthz():
    """Liveness check, plus the commit actually serving traffic.

    `commit` answers the one question a health check normally cannot: is the
    deploy I just pushed the one that is live? Without it, old and new code
    are indistinguishable from outside, and confirming a deploy means opening
    the Render dashboard or authenticating against its API — OFF-63 shipped
    unverified for ten minutes for exactly this reason. Now it is one curl:

        curl -sL https://offerloop.ai/healthz

    Reads Render's own RENDER_GIT_COMMIT, so it reports the commit Render
    built rather than anything the app could get wrong about itself. Off
    Render (local, CI, tests) both fields read "unknown" — absence is honest,
    not an error, and never affects `status`.
    """
    return jsonify({
        "status": "ok",
        "commit": RUNNING_COMMIT,
        "branch": RUNNING_BRANCH,
    }), 200
