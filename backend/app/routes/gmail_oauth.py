"""
Gmail OAuth routes
"""
import os
import secrets
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, redirect
from urllib.parse import urlencode, urlsplit
from google_auth_oauthlib.flow import Flow

from app.config import GMAIL_SCOPES, OAUTH_REDIRECT_URI, get_frontend_redirect_uri
from ..extensions import require_firebase_auth
from app.services.gmail_client import _gmail_client_config, _save_user_gmail_creds, _load_user_gmail_creds, _gmail_service
from ..extensions import get_db

gmail_oauth_bp = Blueprint('gmail_oauth', __name__, url_prefix='/api/google')


def _sanitize_return_to(value):
    """Validate a return_to value as a same-origin relative path.

    Must start with a single "/" and contain no backslashes (browsers
    normalize "\\" to "/", so "/\\evil.com" would become "//evil.com").
    Returns the path or None — never an absolute/protocol-relative URL,
    so the OAuth callback can't be turned into an open redirect.
    """
    if not value or not isinstance(value, str):
        return None
    if "\\" in value:
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    return value


def _resolve_redirect_base(return_to):
    """Frontend URL the OAuth callback should redirect to.

    return_to (already sanitized, from the oauth_state doc) is resolved
    against the frontend origin; without it, fall back to the legacy
    /signin landing so old in-flight states keep working.
    """
    return_to = _sanitize_return_to(return_to)
    if return_to:
        parts = urlsplit(get_frontend_redirect_uri())
        return f"{parts.scheme}://{parts.netloc}{return_to}"
    return get_frontend_redirect_uri()


def build_gmail_oauth_url_for_user(uid, user_email=None):
    """
    Build Gmail OAuth URL for a user (e.g. when returning 401 gmail_token_expired).
    Stores state in Firestore and returns the auth URL. Callable from any route that has uid.
    """
    db = get_db()
    if not db:
        return None
    user_email = (user_email or "").strip().lower() or None
    state = secrets.token_urlsafe(32)
    state_data = {
        "uid": uid,
        "email": user_email,
        "created": datetime.utcnow(),
        "expires": datetime.utcnow() + timedelta(minutes=15),
    }
    try:
        db.collection("oauth_state").document(state).set(state_data)
    except Exception as e:
        print(f"Failed to save OAuth state for reconnect URL: {e}")
        return None
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    if not client_id:
        return None
    redirect_uri = OAUTH_REDIRECT_URI
    auth_base = "https://accounts.google.com/o/oauth2/v2/auth"
    scope_string = " ".join(GMAIL_SCOPES)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope_string,
        "access_type": "offline",
        "state": state,
        "prompt": "consent",
    }
    if user_email:
        params["login_hint"] = user_email
    return f"{auth_base}?{urlencode(params)}"



@gmail_oauth_bp.get("/oauth/start")
@require_firebase_auth
def google_oauth_start():
    """Initiate Gmail OAuth flow with proper state management"""
    db = get_db()
    uid = request.firebase_user["uid"]

    # Without an OAuth client the consent URL would be broken (empty
    # client_id) and Google shows an error page; say so plainly instead.
    if not os.environ.get("GOOGLE_CLIENT_ID") or not os.environ.get("GOOGLE_CLIENT_SECRET"):
        print("[gmail_oauth] GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set; Gmail connection unavailable")
        return jsonify({
            "error": "gmail_oauth_not_configured",
            "message": "Gmail connection isn't configured on this server.",
        }), 503

    # Get email from Firestore user document (source of truth)
    # This ensures we use the correct email for the logged-in user
    user_email = None
    try:
        user_doc = db.collection("users").document(uid).get()
        if user_doc.exists:
            user_data = user_doc.to_dict() or {}
            user_email = user_data.get("email")
    except Exception as e:
        print(f"[gmail_oauth] Could not fetch email from Firestore: {e}")

    # Fallback to Firebase token email if Firestore doesn't have it
    if not user_email:
        user_email = request.firebase_user.get("email")

    # Normalize email (lowercase, strip whitespace)
    if user_email:
        user_email = user_email.strip().lower()

    print(f"[gmail_oauth] OAuth start for uid={uid}")

    # Generate secure state token for CSRF protection
    state = secrets.token_urlsafe(32)

    # Where the callback should send the browser afterwards. Popup flows pass
    # /oauth/complete (a page that closes itself); page flows pass their own
    # path so the user lands back where they started.
    return_to = _sanitize_return_to(request.args.get("return_to"))

    # Store state in Firestore with user context
    # Increased expiration to 15 minutes to handle slow OAuth flows
    state_data = {
        "uid": uid,
        "email": user_email,
        "created": datetime.utcnow(),
        "expires": datetime.utcnow() + timedelta(minutes=15)
    }
    if return_to:
        state_data["return_to"] = return_to

    try:
        db.collection("oauth_state").document(state).set(state_data)
    except Exception as e:
        print(f"[gmail_oauth] ERROR saving state document: {e}")
        import traceback
        traceback.print_exc()

    # Build OAuth URL with all required scopes
    CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
    REDIRECT_URI = OAUTH_REDIRECT_URI
    AUTH_BASE = "https://accounts.google.com/o/oauth2/v2/auth"

    # Use GMAIL_SCOPES constant for consistency
    scope_string = " ".join(GMAIL_SCOPES)

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": scope_string,
        "access_type": "offline",
        # Removed include_granted_scopes to ensure consent screen always shows
        # This is critical for Google's OAuth verification process
        "state": state,
    }

    # CRITICAL: Force consent screen to ALWAYS show (required for Google verification)
    # Using "consent" to force the consent screen with checkboxes to appear every time,
    # even if the user has previously granted these exact permissions
    # NOTE: We use "consent" (not "select_account consent") so that login_hint can
    # pre-select the account without forcing the account picker to show
    params["prompt"] = "consent"

    # Use login_hint to pre-select the user's account that's signed into the website
    # This ensures only the signed-in user's email is shown (no account picker)
    if user_email:
        params["login_hint"] = user_email

    # Note: In Testing mode, users must be added to Test users list in OAuth consent screen
    # Google Cloud Console > APIs & Services > OAuth consent screen > Test users
    #
    # IMPORTANT FOR GOOGLE VERIFICATION:
    # To ensure the consent screen always shows during verification:
    # 1. The prompt=select_account consent parameter forces the consent screen
    # 2. If testing with an account that already granted permissions, you may need to:
    #    - Revoke access at: https://myaccount.google.com/permissions
    #    - Or use a fresh test account that hasn't granted permissions yet
    # 3. The consent screen with checkboxes MUST appear for Google's verification process

    auth_url = f"{AUTH_BASE}?{urlencode(params)}"

    response_data = {
        "authUrl": auth_url,
        "state": state,
    }

    return jsonify(response_data)


@gmail_oauth_bp.get("/oauth/callback")
def google_oauth_callback():
    """Handle OAuth callback from Google"""
    import traceback
    from googleapiclient.discovery import build

    db = get_db()

    state = request.args.get("state")
    code = request.args.get("code")

    # Extract UID, expected email, and return_to from state. Read up front —
    # the access-denied branch below needs return_to too, so the popup lands
    # back on a page that closes itself instead of /signin.
    uid = None
    expected_email_from_state = None
    return_to = None
    state_doc_found = False
    if state:
        try:
            sdoc = db.collection("oauth_state").document(state).get()
            if not sdoc.exists:
                print(f"[gmail_oauth] State document not found — proceeding without state validation")
                # Don't return error - try to continue
            else:
                state_doc_found = True
                state_data = sdoc.to_dict() or {}
                uid = state_data.get("uid")
                expected_email_from_state = state_data.get("email")
                return_to = _sanitize_return_to(state_data.get("return_to"))
        except Exception as e:
            print(f"[gmail_oauth] Error retrieving state: {e}")
            import traceback
            traceback.print_exc()
            # Don't fail completely - try to continue

    if not code:
        error = request.args.get("error")
        error_description = request.args.get("error_description", "")

        # Check if user was denied access (not in test users list)
        if error == "access_denied" or (error_description and "not a test user" in error_description.lower()):
            print(f"[gmail_oauth] OAuth access denied: {error}")
            redirect_url = _resolve_redirect_base(return_to)
            sep = "&" if "?" in redirect_url else "?"
            return redirect(f"{redirect_url}{sep}gmail_error=not_test_user")

        return jsonify({"error": "Missing authorization code", "error_details": error}), 400

    if state_doc_found:
        # Clean up state document after use
        try:
            db.collection("oauth_state").document(state).delete()
        except Exception as cleanup_err:
            print(f"[gmail_oauth] Could not clean up state: {cleanup_err}")
    elif not state:
        # no state parameter — try to get UID from auth token, otherwise fail
        uid = (getattr(request, "firebase_user", {}) or {}).get("uid")
        if not uid:
            redirect_url = _resolve_redirect_base(None)
            return redirect(f"{redirect_url}?gmail_error=missing_state")
    
    try:
        # 1) Exchange code for tokens
        flow = Flow.from_client_config(_gmail_client_config(), scopes=GMAIL_SCOPES)
        flow.redirect_uri = OAUTH_REDIRECT_URI
        flow.fetch_token(code=code)
        creds = flow.credentials

        # 2) Get Gmail profile email
        gmail_service = build("gmail", "v1", credentials=creds)
        profile = gmail_service.users().getProfile(userId="me").execute()
        gmail_email = ((profile or {}).get("emailAddress") or "").strip().lower()

        # Bail early if the profile response didn't include an email — without
        # it we can't persist the integration or do a user lookup, and every
        # downstream step would silently misbehave.
        if not gmail_email:
            print("[gmail_oauth] getProfile returned no emailAddress; aborting OAuth")
            redirect_url = _resolve_redirect_base(return_to)
            sep = "&" if "?" in redirect_url else "?"
            return redirect(f"{redirect_url}{sep}gmail_error=profile_missing_email")

        # 3) If we don't have UID from state, try to find user by Gmail email
        if not uid:
            try:
                users_ref = db.collection("users")
                query = users_ref.where("email", "==", gmail_email).limit(1)
                matching_users = list(query.stream())
                if matching_users:
                    uid = matching_users[0].id
                else:
                    print(f"[gmail_oauth] No user found with email: {gmail_email}")
            except Exception as lookup_err:
                print(f"[gmail_oauth] Error looking up user by email: {lookup_err}")

        # 4) Look up the Offerloop user email
        user_email = None
        if uid:
            user_doc = db.collection("users").document(uid).get()
            if user_doc.exists:
                user_email = (user_doc.to_dict() or {}).get("email")
        else:
            # Use Gmail email as fallback
            user_email = gmail_email

        # Allow any Gmail account to be connected (users may use different email for sending)
        redirect_url = _resolve_redirect_base(return_to)

        # Build helper to append query params safely
        def add_param(url: str, key: str, value: str) -> str:
            sep = "&" if "?" in url else "?"
            return f"{url}{sep}{key}={value}"

        # 5) Save creds (only if we have a UID)
        if not uid:
            print("[gmail_oauth] Cannot save Gmail credentials — no UID available")
            redirect_url = add_param(redirect_url, "gmail_error", "no_user_id")
            return redirect(redirect_url)

        _save_user_gmail_creds(uid, creds)
        gmail_ref = db.collection("users").document(uid).collection("integrations").document("gmail")
        gmail_ref.set({"gmailAddress": gmail_email}, merge=True)
        # Write O(1) lookup mapping for webhook->user resolution
        if gmail_email:
            try:
                db.collection("gmail_mappings").document(gmail_email.strip().lower()).set({"uid": uid}, merge=True)
            except Exception:
                pass
        print(f"[gmail_oauth] Gmail connected for uid={uid} ({gmail_email})")

        try:
            from app.services.gmail_client import start_gmail_watch
            start_gmail_watch(uid)
        except Exception as e:
            print(f"[gmail_watch] Failed to start watch for uid={uid}: {e}")

        # 6) Redirect back with ?connected=gmail (state doc already cleaned up
        # right after the read, before token exchange)
        redirect_url = add_param(redirect_url, "connected", "gmail")
        return redirect(redirect_url)

    except Exception as e:
        import traceback
        print(f"[gmail_oauth] OAuth token exchange failed: {e}")
        traceback.print_exc()
        # This runs on Google's redirect back to us, so the user's browser is
        # sitting on the API URL — always send them back into the app instead
        # of rendering JSON. IntegrationsPage/SignIn map gmail_error codes to
        # user-facing toasts ("scopes_declined" already has copy).
        error_code = (
            "scopes_declined"
            if "scope has changed" in str(e).lower()
            else "token_exchange_failed"
        )
        redirect_url = _resolve_redirect_base(return_to)
        sep = "&" if "?" in redirect_url else "?"
        return redirect(f"{redirect_url}{sep}gmail_error={error_code}")



@gmail_oauth_bp.post("/gmail/revoke")
@require_firebase_auth
def revoke_gmail_permissions():
    """Revoke Gmail permissions for the current user - forces consent screen to show on next OAuth"""
    db = get_db()
    uid = request.firebase_user["uid"]

    try:
        # Delete the Gmail credentials document
        gmail_doc_ref = db.collection("users").document(uid).collection("integrations").document("gmail")
        gmail_doc = gmail_doc_ref.get()

        if gmail_doc.exists:
            # Try to revoke the token with Google if we have credentials
            try:
                creds = _load_user_gmail_creds(uid)
                if creds and hasattr(creds, 'token'):
                    import requests
                    revoke_url = 'https://oauth2.googleapis.com/revoke'
                    revoke_params = {'token': creds.token}
                    requests.post(revoke_url, params=revoke_params)
            except Exception as revoke_err:
                print(f"[gmail_oauth] Could not revoke token with Google: {revoke_err}")

            # Delete the credentials document
            gmail_doc_ref.delete()
            return jsonify({
                "success": True,
                "message": "Gmail permissions revoked. The consent screen will appear on next OAuth attempt."
            }), 200
        else:
            return jsonify({
                "success": True,
                "message": "No Gmail permissions found to revoke."
            }), 200

    except Exception as e:
        print(f"[gmail_oauth] Error revoking Gmail permissions: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "error": str(e)
        }), 500


@gmail_oauth_bp.get("/gmail/status")
@require_firebase_auth
def gmail_status():
    """Return whether Gmail is connected for the signed-in user."""
    db = get_db()
    uid = request.firebase_user["uid"]

    # Make sure a creds doc exists first (fast path)
    doc = db.collection("users").document(uid).collection("integrations").document("gmail").get()
    if not doc.exists:
        return jsonify({"connected": False, "reason": "no_credentials"}), 200

    try:
        # Load creds (reconstruct google.oauth2.credentials.Credentials)
        creds = _load_user_gmail_creds(uid)
        if not creds:
            return jsonify({"connected": False, "reason": "creds_load_failed"}), 200

        # Refresh if expired and we have a refresh token
        if not creds.valid and getattr(creds, "refresh_token", None):
            from google.auth.transport.requests import Request
            creds.refresh(Request())
            # persist refreshed access token
            db.collection("users").document(uid).collection("integrations").document("gmail").set(
                {"token": creds.token, "updatedAt": datetime.utcnow()}, merge=True
            )

        if not creds.valid:
            return jsonify({"connected": False, "reason": "invalid_or_no_refresh"}), 200

        # Live check against Gmail
        service = _gmail_service(creds)
        profile = service.users().getProfile(userId="me").execute()
        gmail_address = profile.get("emailAddress")

        return jsonify({
            "connected": True,
            "gmail_address": gmail_address,
            "scopes": list(getattr(creds, "scopes", []) or []),
        }), 200

    except Exception as e:
        # Avoid leaking internal errors; return a stable shape
        print(f"[gmail_oauth] Error checking Gmail status for uid={uid}: {e}")
        return jsonify({"connected": False, "reason": "api_error"}), 200