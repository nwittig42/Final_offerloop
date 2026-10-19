"""
Gmail client service - OAuth credentials management and Gmail API operations
"""
import os
import base64
import pickle
from datetime import datetime
from email.mime.text import MIMEText
import requests
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from google.cloud.firestore_v1 import FieldFilter

from app.config import (
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GMAIL_SCOPES, OAUTH_REDIRECT_URI,
    GOOGLE_SERVICE_ACCOUNT_FILE, GOOGLE_SERVICE_ACCOUNT_EMAIL,
    GMAIL_PUBSUB_TOPIC,
)
from app.extensions import get_db


def _gmail_client_config():
    """Get Gmail OAuth client configuration"""
    return {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "project_id": "offerloop-native",
            "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uris": [OAUTH_REDIRECT_URI],
        }
    }


def clear_user_gmail_integration(uid):
    """Clear Gmail integration for a user so gmail/status reflects disconnected state."""
    db = get_db()
    if not db:
        return
    ref = db.collection("users").document(uid).collection("integrations").document("gmail")
    ref.delete()
    print(f"[GmailClient] Cleared Gmail integration")


def _save_user_gmail_creds(uid, creds):
    """Save user Gmail credentials to Firestore"""
    db = get_db()
    if not db:
        return
    
    data = {
        "token": creds.token,
        "refresh_token": getattr(creds, "refresh_token", None),
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        # Do NOT store client_secret per-user — it's an app-level secret read from env vars.
        "scopes": creds.scopes,
        "expiry": creds.expiry.isoformat() if getattr(creds, "expiry", None) else None,
        "updatedAt": datetime.utcnow(),  # TODO: deprecated in Python 3.12
    }
    db.collection("users").document(uid).collection("integrations").document("gmail").set(data, merge=True)


def _load_user_gmail_creds(uid):
    """Load user Gmail credentials from Firestore with automatic token refresh"""
    db = get_db()
    if not db:
        print("❌ Database not available")
        return None
    
    snap = db.collection("users").document(uid).collection("integrations").document("gmail").get()
    if not snap.exists:
        print(f"[GmailClient] No Gmail credentials found")
        return None

    data = snap.to_dict() or {}

    # Check if we have required data
    if not data.get("token"):
        print(f"[GmailClient] No access token found")
        return None

    if not data.get("refresh_token"):
        print(f"[GmailClient] WARNING: No refresh token found - token cannot be refreshed!")
    
    # Create credentials object
    try:
        # Parse expiry if it exists
        expiry = None
        expiry_str = data.get("expiry")
        if expiry_str:
            try:
                # Parse ISO format string back to datetime
                # Handle both with and without timezone info
                if isinstance(expiry_str, str):
                    # Remove 'Z' suffix if present and replace with +00:00 for fromisoformat
                    expiry_str_clean = expiry_str.replace('Z', '+00:00')
                    expiry = datetime.fromisoformat(expiry_str_clean)
                    # Ensure expiry is timezone-aware (UTC) for proper comparison
                    if expiry.tzinfo is None:
                        from datetime import timezone
                        expiry = expiry.replace(tzinfo=timezone.utc)
                elif isinstance(expiry_str, datetime):
                    expiry = expiry_str
                    # Ensure expiry is timezone-aware (UTC) for proper comparison
                    if expiry.tzinfo is None:
                        from datetime import timezone
                        expiry = expiry.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError, AttributeError) as e:
                print(f"⚠️ Could not parse expiry '{expiry_str}': {e}")
                # If parsing fails, expiry will be None and token will be checked/refreshed
        
        # Build authorized_user_info dict
        authorized_user_info = {
            "token": data.get("token"),
            "refresh_token": data.get("refresh_token"),
            "token_uri": data.get("token_uri") or "https://oauth2.googleapis.com/token",
            "client_id": data.get("client_id") or GOOGLE_CLIENT_ID,
            "client_secret": data.get("client_secret") or GOOGLE_CLIENT_SECRET,
            "scopes": data.get("scopes") or GMAIL_SCOPES,
        }
        
        # Include expiry if we have it (Google OAuth2 library accepts datetime or RFC3339 string)
        # Note: Google's library expects expiry without timezone offset, so we strip it
        if expiry:
            # Convert to naive datetime (UTC) by removing timezone info before isoformat()
            # This prevents "unconverted data remains: +00:00" error in from_authorized_user_info()
            expiry_naive = expiry.replace(tzinfo=None) if expiry.tzinfo else expiry
            authorized_user_info["expiry"] = expiry_naive.isoformat()
        
        creds = Credentials.from_authorized_user_info(authorized_user_info)
        
        # If expiry wasn't set in authorized_user_info, set it directly on the creds object
        if expiry and not creds.expiry:
            creds.expiry = expiry
        
        # Check if credentials are valid
        # Only log if there's an issue (no expiry or expired)
        if not creds.expiry:
            print(f"[GmailClient] No expiry information - will check/refresh token")
        elif creds.expired:
            print(f"[GmailClient] Gmail token expired")

            if creds.refresh_token:
                try:
                    creds.refresh(Request())
                    print(f"[GmailClient] Token refreshed successfully")

                    # Save the refreshed credentials
                    _save_user_gmail_creds(uid, creds)
                except Exception as refresh_error:
                    error_msg = str(refresh_error).lower()
                    if 'invalid_grant' in error_msg:
                        print(f"[GmailClient] Refresh token is invalid or revoked")
                        # Preserve original error message
                        raise Exception(f"Gmail refresh token invalid: {refresh_error}")
                    else:
                        print(f"[GmailClient] Token refresh failed: {refresh_error}")
                        raise
            else:
                print(f"[GmailClient] No refresh token available")
                raise Exception("Gmail token expired and no refresh token available")
        
        return creds
        
    except Exception as e:
        print(f"[GmailClient] Error loading/refreshing Gmail credentials: {e}")
        import traceback
        traceback.print_exc()
        raise


def _gmail_service(creds):
    """Build Gmail service from credentials"""
    return build("gmail", "v1", credentials=creds)


def send_email_for_user(uid: str, to: str, subject: str, body_html: str, tracking_kind: str = None) -> dict:
    """Send an email via user's Gmail OAuth credentials (from the student's
    own address).

    Returns a tight, explicit contract:
        {
            "id":       Gmail message id (str),
            "threadId": Gmail thread id (str),
            "labelIds": list[str],  # whatever Gmail stamped
        }

    Pass tracking_kind (e.g. "outreach") for emails that go to a contact, so
    the open-tracking pixel is added. Leave it unset for emails the user sends
    to themselves (digests).

    Phase 9 callers (Loop auto-send) MUST stamp `id` and `threadId` onto the
    contact doc immediately so the Pub/Sub webhook can join replies via the
    preferred `gmailThreadId` join key rather than falling back to
    draftToEmail / alternateEmails matching (see gmail_webhook.py:230).

    Raises:
        ValueError: no Gmail credentials on file for this user, or the
            Gmail client failed to build (token expired and refresh failed).
        googleapiclient errors propagate unchanged so callers can distinguish
        quota / auth / network failures.
    """
    creds = _load_user_gmail_creds(uid)
    if not creds:
        raise ValueError(f"No Gmail credentials for uid={uid}")

    service = _gmail_service(creds)
    if not service:
        raise ValueError(f"Failed to build Gmail service for uid={uid}")

    from app.services import email_tracking
    tracking_token = email_tracking.start(uid) if tracking_kind else None
    message = MIMEText(email_tracking.add_pixel(body_html, tracking_token), "html")
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    resp = service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute() or {}
    email_tracking.register(
        tracking_token, uid, tracking_kind, recipient_email=to,
        message_id=resp.get("id"), thread_id=resp.get("threadId"), sent=True,
    )

    return {
        "id": resp.get("id", ""),
        "threadId": resp.get("threadId", ""),
        "labelIds": resp.get("labelIds", []) or [],
    }


def start_gmail_watch(uid):
    """
    Start Gmail push notifications watch for the user. Saves watchHistoryId, watchExpiration,
    watchStartedAt to users/{uid}/integrations/gmail. Raises if no/invalid Gmail credentials.
    """
    creds = _load_user_gmail_creds(uid)
    if not creds:
        raise ValueError(f"No Gmail credentials for uid={uid}")
    service = _gmail_service(creds)
    if not service:
        raise ValueError(f"Failed to build Gmail service for uid={uid}")
    body = {"topicName": GMAIL_PUBSUB_TOPIC}
    response = service.users().watch(userId="me", body=body).execute()
    history_id = str(response.get("historyId", ""))
    expiration = response.get("expiration")
    if expiration is not None:
        try:
            expiration = int(expiration)
        except (TypeError, ValueError):
            expiration = None
    db = get_db()
    if not db:
        raise RuntimeError("Database not available")
    gmail_ref = db.collection("users").document(uid).collection("integrations").document("gmail")
    now_iso = datetime.utcnow().isoformat() + "Z"  # TODO: deprecated in Python 3.12
    gmail_ref.set({
        "watchHistoryId": history_id,
        "watchExpiration": expiration,
        "watchStartedAt": now_iso,
    }, merge=True)
    print(f"[gmail_watch] Started watch historyId={history_id} expiration={expiration}")
    return response


def stop_gmail_watch(uid):
    """Stop Gmail push notifications and clear watch fields from users/{uid}/integrations/gmail."""
    from firebase_admin import firestore
    creds = _load_user_gmail_creds(uid)
    if not creds:
        raise ValueError(f"No Gmail credentials for uid={uid}")
    service = _gmail_service(creds)
    if not service:
        raise ValueError(f"Failed to build Gmail service for uid={uid}")
    service.users().stop(userId="me").execute()
    db = get_db()
    if db:
        gmail_ref = db.collection("users").document(uid).collection("integrations").document("gmail")
        gmail_ref.update({
            "watchHistoryId": firestore.DELETE_FIELD,
            "watchExpiration": firestore.DELETE_FIELD,
            "watchStartedAt": firestore.DELETE_FIELD,
        })
    print(f"[gmail_watch] Stopped watch")


def renew_gmail_watch(uid):
    """Renew Gmail watch (same as start — calling watch() again extends expiration)."""
    start_gmail_watch(uid)
    print(f"[gmail_watch] Renewed watch")


# Cache for find_uid_by_gmail_address (TTL 5 min) to avoid scanning all users on every notification
_gmail_address_to_uid_cache = {}
_gmail_address_cache_ts = 0
_GMAIL_ADDRESS_CACHE_TTL_SEC = 300


def find_uid_by_gmail_address(email_address):
    """
    Find user uid whose Gmail integration has the given email.
    First checks gmail_mappings/{email} for O(1) lookup, then falls back to
    collection group query on integrations, and finally falls back to full scan.
    Uses a module-level cache with 5-min TTL.
    """
    global _gmail_address_to_uid_cache, _gmail_address_cache_ts
    import time as _time
    now = _time.time()
    if now - _gmail_address_cache_ts > _GMAIL_ADDRESS_CACHE_TTL_SEC:
        _gmail_address_to_uid_cache = {}
        _gmail_address_cache_ts = now
    key = (email_address or "").strip().lower()
    if not key:
        return None
    if key in _gmail_address_to_uid_cache:
        return _gmail_address_to_uid_cache[key]
    db = get_db()
    if not db:
        return None

    # O(1) lookup via gmail_mappings collection
    try:
        mapping_doc = db.collection("gmail_mappings").document(key).get()
        if mapping_doc.exists:
            uid = (mapping_doc.to_dict() or {}).get("uid")
            if uid:
                _gmail_address_to_uid_cache[key] = uid
                return uid
    except Exception:
        pass

    # Fallback: collection group query on "integrations" where gmailAddress matches
    try:
        from google.cloud.firestore_v1 import FieldFilter
        query = db.collection_group("integrations").where(
            filter=FieldFilter("gmailAddress", "==", key)
        ).limit(1)
        for doc in query.stream():
            # doc path: users/{uid}/integrations/gmail
            uid = doc.reference.parent.parent.id
            _gmail_address_to_uid_cache[key] = uid
            # Backfill the mapping for future O(1) lookups
            try:
                db.collection("gmail_mappings").document(key).set({"uid": uid}, merge=True)
            except Exception:
                pass
            return uid
    except Exception:
        pass

    # Final fallback: full scan (only reached if collection group index not set up)
    for user_doc in db.collection("users").stream():
        uid = user_doc.id
        gmail_doc = db.collection("users").document(uid).collection("integrations").document("gmail").get()
        if not gmail_doc.exists:
            continue
        data = gmail_doc.to_dict() or {}
        stored = (data.get("gmailAddress") or "").strip().lower()
        if stored:
            _gmail_address_to_uid_cache[stored] = uid
            # Backfill mapping
            try:
                db.collection("gmail_mappings").document(stored).set({"uid": uid}, merge=True)
            except Exception:
                pass
    uid = _gmail_address_to_uid_cache.get(key)
    if uid is None and key not in _gmail_address_to_uid_cache:
        _gmail_address_to_uid_cache[key] = None
    return uid


def get_thread_messages(gmail_service, thread_id):
    """Get all messages in a Gmail thread"""
    try:
        thread = gmail_service.users().threads().get(
            userId='me',
            id=thread_id,
            format='metadata',
            metadataHeaders=['From', 'To', 'Subject']
        ).execute()
        return thread.get('messages', [])
    except Exception as e:
        print(f"Error getting thread messages: {e}")
        return []


def check_for_replies(gmail_service, thread_id, sent_to_email):
    """Check if there are any replies from the recipient in the thread"""
    try:
        messages = get_thread_messages(gmail_service, thread_id)
        
        # Skip the first message (our sent message)
        for msg in messages[1:]:
            headers = msg.get('payload', {}).get('headers', [])
            from_header = next((h['value'] for h in headers if h['name'].lower() == 'from'), '')
            
            # Check if this message is from the recipient
            if sent_to_email.lower() in from_header.lower():
                # Check if it's unread
                if 'UNREAD' in msg.get('labelIds', []):
                    return {
                        'hasReply': True,
                        'isUnread': True,
                        'messageId': msg['id']
                    }
                else:
                    return {
                        'hasReply': True,
                        'isUnread': False,
                        'messageId': msg['id']
                    }
        
        return {'hasReply': False, 'isUnread': False}
    except Exception as e:
        print(f"Error checking for replies: {e}")
        return {'hasReply': False, 'isUnread': False}


def extract_message_body(message, max_length=500):
    """Extract clean plain text body from a Gmail message, removing HTML/CSS/formatting"""
    import re
    
    try:
        payload = message.get('payload', {})
        body = ''
        
        def clean_html_text(html_text):
            """Thoroughly clean HTML text"""
            if not html_text:
                return ''
            
            # Remove style tags and their content completely
            html_text = re.sub(r'<style[^>]*>.*?</style>', '', html_text, flags=re.DOTALL | re.IGNORECASE)
            
            # Remove script tags
            html_text = re.sub(r'<script[^>]*>.*?</script>', '', html_text, flags=re.DOTALL | re.IGNORECASE)
            
            # Remove inline styles (style="...")
            html_text = re.sub(r'style\s*=\s*["\'][^"\']*["\']', '', html_text, flags=re.IGNORECASE)
            
            # Remove all HTML tags
            html_text = re.sub(r'<[^>]+>', '', html_text)
            
            # Decode HTML entities
            html_text = html_text.replace('&nbsp;', ' ')
            html_text = html_text.replace('&amp;', '&')
            html_text = html_text.replace('&lt;', '<')
            html_text = html_text.replace('&gt;', '>')
            html_text = html_text.replace('&quot;', '"')
            html_text = html_text.replace('&#39;', "'")
            html_text = html_text.replace('&apos;', "'")
            
            # Remove excessive whitespace but preserve line breaks
            html_text = re.sub(r'[ \t]+', ' ', html_text)  # Multiple spaces to single
            html_text = re.sub(r'\n\s*\n\s*\n+', '\n\n', html_text)  # Multiple newlines to double
            
            return html_text.strip()
        
        def remove_email_signature(text):
            """Remove common email signature patterns"""
            # Remove lines starting with common signature markers
            lines = text.split('\n')
            cleaned_lines = []
            in_signature = False
            
            signature_markers = [
                '--', '---', '___', 'Best regards', 'Best,', 'Sincerely', 'Thanks,',
                'Sent from', 'Get Outlook', 'Sent from my', 'iPhone', 'Android',
                'This email', 'Confidentiality Notice', 'Disclaimer:'
            ]
            
            for line in lines:
                line_lower = line.strip().lower()
                # Check if this line starts a signature
                if any(line_lower.startswith(marker.lower()) for marker in signature_markers):
                    in_signature = True
                
                # Stop at common signature separators
                if line.strip() in ['--', '---', '___']:
                    in_signature = True
                    continue
                
                if not in_signature:
                    cleaned_lines.append(line)
            
            return '\n'.join(cleaned_lines).strip()
        
        # Handle multipart messages
        if 'parts' in payload:
            # First, try to find text/plain
            for part in payload['parts']:
                mime_type = part.get('mimeType', '')
                body_data = part.get('body', {}).get('data')
                
                if mime_type == 'text/plain' and body_data:
                    try:
                        decoded = base64.urlsafe_b64decode(body_data).decode('utf-8', errors='ignore')
                        if decoded and decoded.strip():
                            body = decoded
                            break
                    except Exception as e:
                        print(f"Error decoding text/plain part: {e}")
                        continue
                
                # Recursively check nested parts
                if 'parts' in part:
                    for nested_part in part.get('parts', []):
                        nested_mime = nested_part.get('mimeType', '')
                        nested_data = nested_part.get('body', {}).get('data')
                        if nested_mime == 'text/plain' and nested_data:
                            try:
                                decoded = base64.urlsafe_b64decode(nested_data).decode('utf-8', errors='ignore')
                                if decoded and decoded.strip():
                                    body = decoded
                                    break
                            except Exception:
                                pass
                    if body:
                        break
            
            # If no plain text, try HTML
            if not body:
                for part in payload['parts']:
                    mime_type = part.get('mimeType', '')
                    body_data = part.get('body', {}).get('data')
                    
                    if mime_type == 'text/html' and body_data:
                        try:
                            decoded = base64.urlsafe_b64decode(body_data).decode('utf-8', errors='ignore')
                            if decoded:
                                body = clean_html_text(decoded)
                                if body.strip():
                                    break
                        except Exception as e:
                            print(f"Error decoding text/html part: {e}")
                            continue
                    
                    # Check nested parts for HTML
                    if 'parts' in part:
                        for nested_part in part.get('parts', []):
                            nested_mime = nested_part.get('mimeType', '')
                            nested_data = nested_part.get('body', {}).get('data')
                            if nested_mime == 'text/html' and nested_data:
                                try:
                                    decoded = base64.urlsafe_b64decode(nested_data).decode('utf-8', errors='ignore')
                                    if decoded:
                                        body = clean_html_text(decoded)
                                        if body.strip():
                                            break
                                except Exception:
                                    pass
                        if body:
                            break
        
        # Handle simple messages (not multipart)
        elif 'body' in payload and 'data' in payload['body']:
            try:
                decoded = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8', errors='ignore')
                # Check if it's HTML
                if '<' in decoded and '>' in decoded:
                    body = clean_html_text(decoded)
                else:
                    body = decoded
            except Exception as e:
                print(f"Error decoding simple message body: {e}")
        
        # Fallback to Gmail snippet if we have nothing
        if not body or not body.strip():
            body = message.get('snippet', '')
        
        # Clean up the body
        if body:
            # Remove email signatures
            body = remove_email_signature(body)
            
            # Remove quoted replies (lines starting with >)
            lines = body.split('\n')
            cleaned_lines = []
            in_quoted = False
            for line in lines:
                stripped = line.strip()
                # Detect quoted text
                if stripped.startswith('>') or (stripped.startswith('On ') and 'wrote:' in stripped):
                    in_quoted = True
                if in_quoted:
                    continue
                cleaned_lines.append(line)
            body = '\n'.join(cleaned_lines)
            
            # Final cleanup
            body = re.sub(r'\s+', ' ', body)  # Collapse all whitespace to single spaces
            body = body.strip()
            
            # Limit length for snippets if specified
            if max_length and len(body) > max_length:
                # Try to cut at a sentence boundary
                truncated = body[:max_length]
                last_period = truncated.rfind('.')
                last_exclamation = truncated.rfind('!')
                last_question = truncated.rfind('?')
                last_sentence = max(last_period, last_exclamation, last_question)
                if last_sentence > max_length * 0.7:  # Only use if we're not cutting too much
                    body = truncated[:last_sentence + 1] + '...'
                else:
                    body = truncated + '...'
        
        return body.strip()
    except Exception as e:
        print(f"Error extracting message body: {e}")
        import traceback
        traceback.print_exc()
        # Fallback to snippet
        return message.get('snippet', '').strip()


def get_latest_message_from_thread(gmail_service, thread_id, sent_to_email=None):
    """
    Get the latest message from a Gmail thread.
    If sent_to_email is provided, returns the latest message from that sender.
    Otherwise, returns the latest message in the thread.
    """
    try:
        thread = gmail_service.users().threads().get(
            userId='me',
            id=thread_id,
            format='full'
        ).execute()
        
        messages = thread.get('messages', [])
        if not messages:
            return None
        
        # If sent_to_email is provided, find latest message from that sender
        if sent_to_email:
            for msg in reversed(messages):  # Start from latest
                headers = msg.get('payload', {}).get('headers', [])
                from_header = next((h['value'] for h in headers if h['name'].lower() == 'from'), '')
                
                if sent_to_email.lower() in from_header.lower():
                    return msg
        
        # Otherwise, return the latest message
        return messages[-1]
    except Exception as e:
        print(f"Error getting latest message from thread: {e}")
        return None


def get_full_thread_chain(gmail_service, thread_id, sent_to_email=None, user_email=None):
    """
    Fetch every message in a Gmail thread with bodies decoded, returned
    oldest -> newest. Used by the outbox thread-view endpoint so the panel can
    render the actual conversation instead of the latest-snippet fallback.

    Each message dict:
        messageId, sender (raw From header), isFromRecipient (bool, matched on
        sent_to_email), isFromUser (bool, matched on user_email), sentAt (ISO
        UTC string), subject, body (extract_message_body output — signature and
        quoted-reply stripping is desirable for clean chain display).
    """
    try:
        thread = gmail_service.users().threads().get(
            userId='me',
            id=thread_id,
            format='full',
        ).execute()
    except Exception as e:
        print(f"Error fetching full thread chain {thread_id}: {e}")
        raise

    messages = thread.get('messages', []) or []
    chain = []
    for msg in messages:
        headers = msg.get('payload', {}).get('headers', []) or []
        from_header = next((h.get('value', '') for h in headers if (h.get('name') or '').lower() == 'from'), '')
        subject = next((h.get('value', '') for h in headers if (h.get('name') or '').lower() == 'subject'), '')

        is_from_recipient = bool(sent_to_email) and sent_to_email.lower() in (from_header or '').lower()
        is_from_user = bool(user_email) and user_email.lower() in (from_header or '').lower()

        sent_at = None
        ts = msg.get('internalDate')
        if ts:
            try:
                sent_at = datetime.utcfromtimestamp(int(ts) / 1000).isoformat() + "Z"
            except Exception:
                pass

        # Full body for display — pass max_length=None so we don't truncate.
        body = extract_message_body(msg, max_length=None)

        chain.append({
            'messageId': msg.get('id'),
            'sender': from_header,
            'isFromRecipient': is_from_recipient,
            'isFromUser': is_from_user,
            'sentAt': sent_at,
            'subject': subject,
            'body': body,
        })

    # Sort oldest -> newest by sentAt (internalDate); messages already arrive
    # in this order from Gmail but sort defensively in case of edge cases.
    chain.sort(key=lambda m: m.get('sentAt') or '')
    return chain


def sync_thread_message(gmail_service, thread_id, sent_to_email=None, user_email=None):
    """
    Sync the latest message from a Gmail thread and return message snippet with status.
    Returns dict with: snippet, hasUnreadReply, lastActivityAt, status, isFromRecipient
    """
    try:
        # Get full thread to analyze all messages
        thread = gmail_service.users().threads().get(
            userId='me',
            id=thread_id,
            format='full'
        ).execute()
        
        messages = thread.get('messages', [])
        if not messages:
            return {
                'snippet': 'No messages found in thread.',
                'hasUnreadReply': False,
                'lastActivityAt': None,
                'status': 'no_reply_yet',
                'isFromRecipient': False
            }
        
        # Get the latest message
        latest_msg = messages[-1]
        
        # Determine who sent the latest message
        headers = latest_msg.get('payload', {}).get('headers', [])
        from_header = next((h['value'] for h in headers if h['name'].lower() == 'from'), '')
        
        # Check if latest message is from recipient
        is_from_recipient = False
        if sent_to_email:
            is_from_recipient = sent_to_email.lower() in from_header.lower()
        
        # Check if latest message is from user
        is_from_user = False
        if user_email:
            is_from_user = user_email.lower() in from_header.lower()
        
        # Extract message body
        snippet = extract_message_body(latest_msg, max_length=300)
        if not snippet:
            # Fallback to Gmail's snippet
            snippet = latest_msg.get('snippet', 'No message content available.')
            # Clean the snippet too
            import re
            snippet = re.sub(r'\s+', ' ', snippet).strip()
        
        # Check if unread
        has_unread = 'UNREAD' in latest_msg.get('labelIds', [])
        
        # Get timestamp
        timestamp = latest_msg.get('internalDate')
        last_activity = None
        if timestamp:
            try:
                last_activity = datetime.utcfromtimestamp(int(timestamp) / 1000).isoformat() + "Z"  # TODO: deprecated in Python 3.12
            except Exception:
                pass
        
        # Determine status
        status = 'waiting_on_them'
        if is_from_recipient and has_unread:
            status = 'new_reply'
        elif is_from_recipient and not has_unread:
            status = 'waiting_on_you'  # They replied, we haven't responded yet
        elif is_from_user:
            status = 'waiting_on_them'  # We sent, waiting for their reply
        else:
            # Check if there are any replies from recipient in the thread
            has_any_reply = False
            for msg in messages:
                msg_headers = msg.get('payload', {}).get('headers', [])
                msg_from = next((h['value'] for h in msg_headers if h['name'].lower() == 'from'), '')
                if sent_to_email and sent_to_email.lower() in msg_from.lower():
                    has_any_reply = True
                    break
            
            if not has_any_reply:
                status = 'no_reply_yet'
            else:
                status = 'waiting_on_you'
        
        return {
            'snippet': snippet,
            'hasUnreadReply': has_unread and is_from_recipient,
            'lastActivityAt': last_activity or datetime.utcnow().isoformat(),  # TODO: deprecated in Python 3.12
            'messageId': latest_msg.get('id'),
            'status': status,
            'isFromRecipient': is_from_recipient
        }
    except Exception as e:
        print(f"Error syncing thread message: {e}")
        import traceback
        traceback.print_exc()
        # datetime is imported at module level, use it directly
        return {
            'snippet': 'Error syncing message from Gmail.',
            'hasUnreadReply': False,
            'lastActivityAt': datetime.utcnow().isoformat(),  # TODO: deprecated in Python 3.12
            'status': 'waiting_on_them',
            'isFromRecipient': False
        }


def get_gmail_service():
    """Get Gmail API service using token.pickle (shared account)"""
    try:
        # Try multiple possible locations for token.pickle
        # Get the backend directory path
        backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))  # Go up from services/ to backend/
        current_dir = os.getcwd()
        
        possible_paths = [
            os.path.join(backend_dir, 'token.pickle'),  # backend/token.pickle (most likely)
            'token.pickle',  # Current working directory
            os.path.join(current_dir, 'token.pickle'),  # Explicit current directory
            os.path.join(os.path.dirname(__file__), '..', '..', 'token.pickle'),  # Relative from services/
        ]
        
        token_path = None
        for path in possible_paths:
            abs_path = os.path.abspath(path)
            if os.path.exists(abs_path):
                token_path = abs_path
                break

        if not token_path:
            print("[GmailClient] token.pickle not found in expected locations")
            return None
        
        creds = None
        with open(token_path, 'rb') as token:
            creds = pickle.load(token)
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(token_path, 'wb') as token:
                    pickle.dump(creds, token)
            else:
                print("[GmailClient] No valid Gmail credentials found in token.pickle")
                return None
        
        service = build('gmail', 'v1', credentials=creds)
        
        # Verify connection by getting profile (only log in debug mode or when explicitly requested)
        # This is often called from health checks, so we don't want to spam logs
        try:
            profile = service.users().getProfile(userId='me').execute()
            email = profile.get('emailAddress', 'unknown')
            # Only print if explicitly requested (not for health checks)
            # The email is logged elsewhere when actually used for user operations
        except Exception as profile_error:
            # Only log errors, not successful connections
            print(f"[GmailClient] Gmail service connected but couldn't fetch profile: {profile_error}")
        
        return service
        
    except Exception as e:
        print(f"[GmailClient] Gmail service failed: {e}")
        import traceback
        traceback.print_exc()
        return None


def get_gmail_service_with_service_account(user_email):
    """Get Gmail service using service account with domain-wide delegation (no OAuth required)"""
    try:
        if not GOOGLE_SERVICE_ACCOUNT_FILE or not os.path.exists(GOOGLE_SERVICE_ACCOUNT_FILE):
            print(f"[GmailClient] Service account file not found, falling back to shared account")
            return None

        if not user_email:
            print(f"[GmailClient] No user email provided")
            return None
        
        # Load service account credentials
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE,
            scopes=GMAIL_SCOPES
        )
        
        # If service account email is provided, use domain-wide delegation to impersonate user
        if GOOGLE_SERVICE_ACCOUNT_EMAIL:
            # Delegate domain-wide authority to impersonate the user
            delegated_creds = creds.with_subject(user_email)
            service = build('gmail', 'v1', credentials=delegated_creds)
            print(f"[GmailClient] Gmail service created with domain-wide delegation")
            return service
        else:
            # Use service account directly (shared account)
            service = build('gmail', 'v1', credentials=creds)
            print(f"[GmailClient] Gmail service created using service account")
            return service
        
    except Exception as e:
        print(f"[GmailClient] Error creating service account Gmail service: {e}")
        import traceback
        traceback.print_exc()
        return None


def download_resume_from_url(resume_url):
    """
    Download resume PDF from a URL (Google Drive, Firebase Storage, etc.)
    Returns tuple: (resume_content: bytes, filename: str) or (None, None) on error
    """
    import re
    
    try:
        print(f"📎 Downloading resume from: {resume_url}")
        
        # Normalize Google Drive URLs to direct download format
        normalized_url = resume_url
        if 'drive.google.com' in resume_url:
            # Format: https://drive.google.com/file/d/FILE_ID/view
            match = re.search(r'/file/d/([^/]+)', resume_url)
            if match:
                file_id = match.group(1)
                normalized_url = f'https://drive.google.com/uc?export=download&id={file_id}'
                print(f"   🔄 Normalized Google Drive URL: {normalized_url}")
            else:
                # Format: https://drive.google.com/open?id=FILE_ID
                match = re.search(r'[?&]id=([^&]+)', resume_url)
                if match:
                    file_id = match.group(1)
                    normalized_url = f'https://drive.google.com/uc?export=download&id={file_id}'
                    print(f"   🔄 Normalized Google Drive URL: {normalized_url}")
        
        # Handle Firebase Storage URLs - they should work directly
        if 'firebasestorage.googleapis.com' in normalized_url:
            print(f"   📦 Detected Firebase Storage URL")
        
        print(f"   ⬇️ Downloading resume from {normalized_url}")
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(normalized_url, timeout=30, headers=headers, allow_redirects=True)
        response.raise_for_status()
        
        # Check if we got actual PDF content
        content_type = response.headers.get('Content-Type', '').lower()
        print(f"   📄 Content-Type: {content_type}")
        
        if 'html' in content_type and 'drive.google.com' in normalized_url:
            # Google Drive might be showing a warning page, try alternative method
            print(f"   ⚠️ Got HTML instead of PDF, trying alternative download method...")
            # Extract file ID and try direct download
            match = re.search(r'id=([^&]+)', normalized_url)
            if match:
                file_id = match.group(1)
                alt_url = f'https://drive.google.com/uc?export=download&id={file_id}&confirm=t'
                print(f"   🔄 Trying alternative URL: {alt_url}")
                response = requests.get(alt_url, timeout=30, headers=headers, allow_redirects=True)
                response.raise_for_status()
                content_type = response.headers.get('Content-Type', '').lower()
        
        # Get filename from URL or headers
        filename = "resume.pdf"
        if 'Content-Disposition' in response.headers:
            content_disp = response.headers['Content-Disposition']
            if 'filename=' in content_disp:
                filename = content_disp.split('filename=')[1].strip('"').strip("'")
        
        if not filename.endswith('.pdf') and not filename.endswith('.docx'):
            # Try to extract from URL
            for part in reversed(resume_url.split('/')):
                if '.pdf' in part.lower() or '.docx' in part.lower():
                    filename = part.split('?')[0]
                    break

        resume_content = response.content
        
        return resume_content, filename
        
    except Exception as resume_error:
        print(f"[GmailClient] Could not download resume: {resume_error}")
        import traceback
        traceback.print_exc()
        return None, None


def get_gmail_service_for_user(user_email, user_id=None):
    """
    Get Gmail service for a specific user.
    Priority:
    1. Per-user OAuth credentials (if user_id provided and credentials exist)
    2. Shared token.pickle account (fallback)
    """
    try:
        # user_email is only an identity hint — credentials are loaded by uid.
        # Apple sign-in tokens can omit email entirely, so only bail when we
        # have neither an email nor a uid to work with.
        if not user_email and not user_id:
            print("[GmailClient] No user email or user id provided")
            return None

        # Priority 1: Try per-user OAuth credentials if user_id is provided
        if user_id:
            try:
                creds = _load_user_gmail_creds(user_id)
                if creds:
                    service = _gmail_service(creds)
                    if service:
                        try:
                            service.users().getProfile(userId='me').execute()
                            print(f"[GmailClient] Using user's own Gmail account")
                            return service
                        except Exception as profile_err:
                            print(f"[GmailClient] Could not verify per-user account: {profile_err}")
                            # Continue to fallback
            except Exception as oauth_err:
                print(f"[GmailClient] Per-user OAuth not available: {oauth_err}")
                # Continue to fallback

        # Priority 2: Fallback to shared token.pickle account
        service = get_gmail_service()
        if service:
            return service

        print(f"[GmailClient] Gmail service not available - token.pickle not found")
        return None
        
    except Exception as e:
        print(f"[GmailClient] Error getting Gmail service: {e}")
        import traceback
        traceback.print_exc()
        return None


def get_user_gmail_service_strict(uid):
    """Gmail service from the user's OWN OAuth creds only. No shared fallback.

    Returns None when the user has no (working) Gmail integration. Used by
    user-facing draft creation, where falling back to the shared inbox would
    write drafts into a mailbox the user cannot see.
    """
    if not uid:
        return None
    try:
        creds = _load_user_gmail_creds(uid)
        if not creds:
            return None
        service = _gmail_service(creds)
        if not service:
            return None
        service.users().getProfile(userId='me').execute()
        return service
    except Exception as e:
        print(f"[GmailClient] strict per-user service unavailable for {uid}: {e}")
        return None


def _select_recipient_email(contact):
    """Pick the best recipient address for a contact.

    Preference order: work email, then generic email, then personal email.
    Returns (email, source) or (None, None) when no usable address exists.
    Shared by the draft path and the send path so both target the same inbox.
    """
    if contact.get('WorkEmail') and contact['WorkEmail'] != 'Not available' and '@' in contact['WorkEmail']:
        return contact['WorkEmail'], 'WorkEmail'
    if contact.get('Email') and '@' in contact['Email'] and not contact['Email'].endswith('@domain.com'):
        return contact['Email'], 'Email'
    if contact.get('PersonalEmail') and contact['PersonalEmail'] != 'Not available' and '@' in contact['PersonalEmail']:
        return contact['PersonalEmail'], 'PersonalEmail'
    return None, None


def _build_outreach_mime(recipient_email, gmail_account_email, email_subject, email_body,
                         user_info=None, resume_content=None, resume_filename=None, resume_url=None,
                         tracking_token=None):
    """Build the multipart outreach message (HTML body, signature, resume attachment).

    This is the single source of truth for outreach email content. Both
    create_gmail_draft_for_user (draft) and send_gmail_email_for_user (send)
    call it, so a draft and a send produce byte-for-byte identical email,
    including the resume attachment. email_subject and email_body are expected
    to already be cleaned by the caller.

    tracking_token (from email_tracking.start) adds the open-tracking pixel.
    """
    from app.services import email_tracking
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders

    # Create multipart message
    message = MIMEMultipart('mixed')
    message['to'] = recipient_email
    message['subject'] = email_subject
    # Use the actual Gmail account email as the "from" address
    message['from'] = gmail_account_email

    # Add body (HTML if user_info provided, plain text otherwise)
    if user_info:
        # Create HTML email with professional signature
        user_name = user_info.get('name', '[Your Name]')
        user_email_addr = user_info.get('email', '')
        user_phone = user_info.get('phone', '')
        user_linkedin = user_info.get('linkedin', '')

        # Build contact signature FIRST
        contact_parts = []
        if user_email_addr:
            contact_parts.append(f'<a href="mailto:{user_email_addr}" style="color: #2563eb; text-decoration: none;">{user_email_addr}</a>')
        if user_phone:
            contact_parts.append(f'<span>{user_phone}</span>')
        if user_linkedin:
            linkedin_clean = user_linkedin.replace('https://', '').replace('http://', '').replace('www.', '')
            contact_parts.append(f'<a href="{user_linkedin}" style="color: #2563eb; text-decoration: none;">{linkedin_clean}</a>')

        contact_html = ' · '.join(contact_parts) if contact_parts else ''

        # Convert line breaks to <br> tags for Gmail
        email_body = email_body.strip()
        email_body_html = email_body.replace('\n\n', '<br><br>').replace('\n', '<br>')

        # Simple HTML wrapper
        html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
* {{ margin: 0 !important; padding: 0 !important; text-indent: 0 !important; }}
</style>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
             line-height: 1.6; color: #1f2937; margin-left: -8px !important;">
<div style="white-space: pre-wrap; margin: 0 !important; padding: 0 !important;">
{email_body_html}
</div>
</body>
</html>"""
        html_content = email_tracking.add_pixel(html_content, tracking_token)
        message.attach(MIMEText(html_content, 'html', 'utf-8'))
    elif tracking_token:
        # Plain text can't carry the pixel, so send text + an HTML copy.
        message.attach(email_tracking.tracked_text_mime(email_body, tracking_token))
    else:
        # Plain text fallback
        message.attach(MIMEText(email_body, 'plain', 'utf-8'))

    # Attach resume if available
    # Prefer pre-downloaded resume_content over resume_url to avoid redundant downloads
    if resume_content:
        print(f"[GmailClient] Attaching resume: {resume_filename or 'resume.pdf'}")
        try:
            filename = resume_filename or "resume.pdf"

            import mimetypes
            mime_type, _ = mimetypes.guess_type(filename)
            if mime_type and '/' in mime_type:
                main_type, sub_type = mime_type.split('/', 1)
            else:
                main_type, sub_type = 'application', 'pdf'

            attachment = MIMEBase(main_type, sub_type)
            attachment.set_payload(resume_content)
            encoders.encode_base64(attachment)
            attachment.add_header('Content-Disposition', f'attachment; filename="{filename}"')
            message.attach(attachment)
            print(f"[GmailClient] Resume attached successfully")

        except Exception as resume_error:
            print(f"[GmailClient] Could not attach resume: {resume_error}")
            import traceback
            traceback.print_exc()
            # Continue without resume - don't fail the entire message
    elif resume_url:
        # Fallback: download from URL if resume_content not provided (for backward compatibility)
        print(f"[GmailClient] Attaching resume from URL")
        resume_content, filename = download_resume_from_url(resume_url)
        if resume_content:
            try:
                import mimetypes as _mt
                _mime, _ = _mt.guess_type(filename)
                if _mime and '/' in _mime:
                    main_type, sub_type = _mime.split('/', 1)
                else:
                    main_type, sub_type = 'application', 'pdf'

                attachment = MIMEBase(main_type, sub_type)
                attachment.set_payload(resume_content)
                encoders.encode_base64(attachment)
                attachment.add_header('Content-Disposition', f'attachment; filename="{filename}"')
                message.attach(attachment)
                print(f"[GmailClient] Resume attached successfully")
            except Exception as resume_error:
                print(f"[GmailClient] Could not attach resume: {resume_error}")
                import traceback
                traceback.print_exc()
                # Continue without resume - don't fail the entire message
        else:
            print(f"[GmailClient] Failed to download resume from URL - skipping attachment")

    return message


def create_gmail_draft_for_user(contact, email_subject, email_body, tier='free', user_email=None, resume_url=None, resume_content=None, resume_filename=None, user_info=None, user_id=None):
    """
    Create Gmail draft in the user's account with optional resume attachment and HTML formatting

    Args:
        contact: Contact dictionary
        email_subject: Email subject line
        email_body: Email body text
        tier: 'free' or 'pro'
        user_email: User's email address
        resume_url: URL to download resume from (deprecated - use resume_content instead)
        resume_content: Pre-downloaded resume content as bytes (preferred)
        resume_filename: Filename for the resume attachment
        user_info: User profile information
        user_id: User ID for Gmail credentials
    """
    # Import clean_email_text from utils (will be created)
    from app.utils.contact import clean_email_text

    try:
        # Clean the email subject and body FIRST
        email_subject = clean_email_text(email_subject)
        email_body = clean_email_text(email_body)

        gmail_service = get_gmail_service_for_user(user_email, user_id=user_id)

        if not gmail_service:
            print(f"[GmailClient] Gmail unavailable - creating mock draft")
            return f"mock_{tier}_draft_{contact.get('FirstName', 'unknown').lower()}"

        # Get the actual Gmail account email (might be shared account)
        try:
            gmail_account_email = gmail_service.users().getProfile(userId='me').execute().get('emailAddress')
        except Exception as profile_error:
            print(f"[GmailClient] Could not get Gmail profile: {profile_error}")
            gmail_account_email = user_email or os.getenv("DEFAULT_FROM_EMAIL", "noreply@offerloop.ai")

        print(f"[GmailClient] Creating {tier.capitalize()} Gmail draft for contact {contact.get('FirstName', 'Unknown')}")

        # Pick the best recipient address (work/verified preferred over personal).
        recipient_email, source = _select_recipient_email(contact)
        print(f"[GmailDraft] Recipient for {contact.get('FirstName')} {contact.get('LastName')}: selected={recipient_email} (source={source}) | WorkEmail={contact.get('WorkEmail', 'n/a')} | Email={contact.get('Email', 'n/a')} | PersonalEmail={contact.get('PersonalEmail', 'n/a')}")

        if not recipient_email:
            print(f"[GmailClient] No valid email found for contact - creating mock draft")
            return f"mock_{tier}_draft_{contact.get('FirstName', 'unknown').lower()}_no_email"

        # Phase 2.4: suppression gate. If this address has bounced before
        # (per-user OR globally), skip the draft. Returns a sentinel matching
        # the existing "no_email" shape so downstream callers treat it as
        # "no real draft" without crashing.
        try:
            from app.services.suppression import is_suppressed
            if is_suppressed(user_id, recipient_email):
                print(f"[GmailClient] SUPPRESSED — skipping draft for {recipient_email} (previous bounce)")
                return f"suppressed_{tier}_draft_{contact.get('FirstName', 'unknown').lower()}"
        except Exception as supp_err:
            # Suppression lookup must never block sending a draft.
            print(f"[GmailClient] Suppression check failed (proceeding with draft): {supp_err}")

        # Phase 3c: per-contact low-confidence gate. Same intent as Phase 2.2's
        # batch-level email_quality gate in agent_actions.execute_find_and_draft,
        # but here at the chokepoint so it catches Find People + contact_import
        # + linkedin_import + referral paths too. Only fires when EmailSource is
        # explicitly low-confidence — manual contacts (no EmailSource) are
        # unaffected.
        LOW_CONFIDENCE_SOURCES = {
            "pattern",
            "domain_generated",
            "pdl_fallback",
            "hunter_finder_risky",
            "neverbounce_acceptall",
        }
        email_source = (contact.get("EmailSource") or "").strip()
        if email_source and email_source in LOW_CONFIDENCE_SOURCES:
            print(
                f"[GmailClient] LOW-CONFIDENCE source={email_source} — skipping draft "
                f"for {recipient_email} (contact surfaces, no Gmail draft)"
            )
            return f"low_confidence_{tier}_draft_{contact.get('FirstName', 'unknown').lower()}"

        # Build the multipart message (HTML body, signature, resume attachment),
        # shared with the send path so drafts and sends are identical.
        from app.services import email_tracking
        tracking_token = email_tracking.start(user_id)
        message = _build_outreach_mime(
            recipient_email=recipient_email,
            gmail_account_email=gmail_account_email,
            email_subject=email_subject,
            email_body=email_body,
            user_info=user_info,
            resume_content=resume_content,
            resume_filename=resume_filename,
            resume_url=resume_url,
            tracking_token=tracking_token,
        )

        # Create the draft
        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
        
        draft_body = {
            'message': {
                'raw': raw_message
            }
        }
        
        # Create the draft via Gmail API
        try:
            draft_result = gmail_service.users().drafts().create(userId='me', body=draft_body).execute()
            draft_id = draft_result['id']
            message_id = draft_result.get('message', {}).get('id')
            email_tracking.register(
                tracking_token, user_id, 'outreach', recipient_email=recipient_email,
                draft_id=draft_id, message_id=message_id,
                thread_id=draft_result.get('message', {}).get('threadId'),
            )
        except Exception as api_error:
            print(f"[GmailClient] Gmail API error creating draft: {api_error}")
            import traceback
            traceback.print_exc()
            raise  # Re-raise to be caught by outer exception handler
        
        # If message ID not in response, try to fetch it
        if not message_id:
            try:
                draft_full = gmail_service.users().drafts().get(userId='me', id=draft_id, format='full').execute()
                message_id = draft_full.get('message', {}).get('id')
            except Exception as fetch_err:
                print(f"[GmailClient] Could not fetch message ID: {fetch_err}")
        
        # Get the Gmail account where the draft was created and build draft URL
        # Use message ID format for more reliable draft URL (Option A from fix doc)
        # Format: https://mail.google.com/mail/u/0/#drafts?compose=<messageId>
        gmail_draft_url = None
        try:
            account_email = gmail_service.users().getProfile(userId='me').execute().get('emailAddress')
            # Prefer message ID format (more reliable)
            if message_id:
                gmail_draft_url = f"https://mail.google.com/mail/u/0/#drafts?compose={message_id}"
            else:
                # Fallback to draft ID format if message ID not available
                gmail_draft_url = f"https://mail.google.com/mail/u/0/#draft/{draft_id}"
            print(f"[GmailClient] Created {tier.capitalize()} Gmail draft {draft_id}")
        except Exception as profile_err:
            print(f"[GmailClient] Created {tier.capitalize()} Gmail draft {draft_id}")
            # Still create URL even if we can't get profile
            if message_id:
                gmail_draft_url = f"https://mail.google.com/mail/u/0/#drafts?compose={message_id}"
            else:
                gmail_draft_url = f"https://mail.google.com/mail/u/0/#draft/{draft_id}"
        
        # Return draft_id, message_id, URL, and actual recipient as a dict for easier access
        return {
            'draft_id': draft_id,
            'message_id': message_id,
            'draft_url': gmail_draft_url,
            'recipient_email': recipient_email,
        }
        
    except Exception as e:
        print(f"[GmailClient] {tier.capitalize()} Gmail draft creation failed: {e}")
        import traceback
        traceback.print_exc()
        return f"mock_{tier}_draft_{contact.get('FirstName', 'unknown').lower()}"


def find_sent_thread_for_recipient(user_email, user_id, recipient_email, subject=None):
    """Look up the most recent sent Gmail thread addressed to a recipient.

    Used to backfill gmailThreadId on Loop-found contacts whose email was
    sent (manually from the tracker, or via an older code path that didn't
    stamp the thread id). Once stamped, the activity feed's Draft button
    deep-links to the exact thread instead of falling back to a compose URL
    or the tracker.

    Returns {thread_id, message_id} on a match, else None.
    """
    if not recipient_email:
        return None
    try:
        service = get_gmail_service_for_user(user_email, user_id=user_id)
        if not service:
            return None
        q = f'in:sent to:{recipient_email}'
        if subject:
            clean = subject.replace('"', '').strip()
            if clean:
                q += f' subject:"{clean}"'
        resp = service.users().messages().list(userId='me', q=q, maxResults=1).execute()
        messages = resp.get('messages') or []
        if not messages:
            return None
        return {
            'thread_id': messages[0].get('threadId') or '',
            'message_id': messages[0].get('id') or '',
        }
    except Exception as e:
        print(f"[GmailClient] find_sent_thread_for_recipient failed for {recipient_email}: {e}")
        return None


def find_draft_for_recipient(user_email, user_id, recipient_email, subject=None):
    """Look up an existing Gmail draft addressed to a specific recipient.

    Used to backfill gmailDraftUrl on Loop-found contacts whose draft was
    created before the (id/url → draft_id/draft_url) field-name bug was
    fixed in agent_actions.py. Same compose URL shape that
    create_gmail_draft_for_user returns, so the activity feed can deep-link
    to the exact draft (matching Find People spreadsheet behavior).

    Returns {draft_id, message_id, draft_url, thread_id} on a match, else None.
    """
    if not recipient_email:
        return None
    try:
        service = get_gmail_service_for_user(user_email, user_id=user_id)
        if not service:
            return None
        # Gmail search query — narrow to drafts addressed to this recipient.
        # Subject is included when available to disambiguate multiple drafts
        # to the same person (e.g. an initial outreach + a follow-up).
        q = f'in:drafts to:{recipient_email}'
        if subject:
            # Strip quotes from the subject so the Gmail query parser doesn't
            # see unbalanced ones. Surround the whole thing in quotes for an
            # exact phrase match.
            clean = subject.replace('"', '').strip()
            if clean:
                q += f' subject:"{clean}"'
        resp = service.users().messages().list(userId='me', q=q, maxResults=5).execute()
        messages = resp.get('messages') or []
        if not messages:
            return None
        # Take the first match. message.id is the underlying message; threadId
        # is the conversation. We need draft_id too — fetch the message to
        # confirm it's actually a draft and to get the draft envelope.
        message_id = messages[0].get('id')
        thread_id = messages[0].get('threadId')
        if not message_id:
            return None
        # Locate the draft envelope by listing drafts and matching message id.
        # drafts.list returns up to 500 at a time; for users with thousands of
        # drafts this would need pagination, but the common case fits in one
        # page.
        draft_id = ''
        try:
            drafts_resp = service.users().drafts().list(userId='me', maxResults=500).execute()
            for d in drafts_resp.get('drafts') or []:
                if (d.get('message') or {}).get('id') == message_id:
                    draft_id = d.get('id') or ''
                    break
        except Exception:
            pass
        draft_url = f"https://mail.google.com/mail/u/0/#drafts?compose={message_id}"
        return {
            'draft_id': draft_id,
            'message_id': message_id,
            'draft_url': draft_url,
            'thread_id': thread_id or '',
        }
    except Exception as e:
        print(f"[GmailClient] find_draft_for_recipient failed for {recipient_email}: {e}")
        return None


# ISSUE 3 FIX: Parallel Gmail draft creation with rate limiting
def create_drafts_parallel(contacts_with_emails, resume_bytes=None, resume_filename=None, user_info=None, user_id=None, tier='free', user_email=None, resume_url=None):
    """
    Create all Gmail drafts in parallel with rate limiting.
    
    Args:
        contacts_with_emails: List of dicts, each containing:
            - contact: Contact dict
            - email_subject: Subject line
            - email_body: Body text
        resume_bytes: Optional resume content as bytes
        resume_filename: Optional resume filename
        user_info: Optional user profile info
        user_id: Optional user ID
        tier: 'free' or 'pro'
        user_email: User's email address
        resume_url: Optional URL to download resume when resume_bytes is not provided
    
    Returns:
        List of results (dicts with draft_id, message_id, draft_url) or error strings
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    
    if not contacts_with_emails:
        return []
    
    # When resume_bytes not provided but resume_url is set, download once for all drafts
    if resume_bytes is None and resume_url:
        try:
            resume_bytes, downloaded_filename = download_resume_from_url(resume_url)
            if resume_bytes and not resume_filename:
                resume_filename = downloaded_filename
        except Exception as e:
            print(f"⚠️ Could not download resume from URL in create_drafts_parallel: {e}")
    
    # Gmail per-user quota: 250 units/sec; drafts.create costs 10 units → 25
    # sustained draft creations/sec. 10 concurrent workers stay well inside
    # the budget while cutting Elite's 15-draft run from 3 waves to 2.
    max_workers = min(10, len(contacts_with_emails))
    results = []
    results_lock = threading.Lock()
    
    def create_single_draft(item):
        """Create a single draft with resume attached when available."""
        try:
            contact = item['contact']
            email_subject = item['email_subject']
            email_body = item['email_body']
            
            result = create_gmail_draft_for_user(
                contact, email_subject, email_body, tier, user_email,
                None,  # resume_url (deprecated)
                resume_bytes,
                resume_filename,
                user_info,
                user_id
            )
            return item.get('index', 0), result, None
        except Exception as e:
            return item.get('index', 0), None, str(e)
    
    # Create all drafts in parallel
    print(f"[GmailClient] Creating {len(contacts_with_emails)} Gmail drafts in parallel (max {max_workers} concurrent)")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks
        future_to_item = {
            executor.submit(create_single_draft, item): item 
            for item in contacts_with_emails
        }
        
        # Collect results as they complete
        for future in as_completed(future_to_item):
            item = future_to_item[future]
            try:
                index, result, error = future.result()
                with results_lock:
                    results.append((index, result, error))
            except Exception as e:
                with results_lock:
                    results.append((item.get('index', 0), None, str(e)))
    
    # Sort results by index to maintain order
    results.sort(key=lambda x: x[0])
    return [result for _, result, _ in results]


def send_gmail_email_for_user(contact, email_subject, email_body, tier='free', user_email=None, resume_url=None, resume_content=None, resume_filename=None, user_info=None, user_id=None):
    """Send an outreach email from the user's Gmail account.

    This is the shared send path: it builds the exact same message as the draft
    path (via _build_outreach_mime, resume attachment included) and calls
    messages().send() instead of drafts().create(). A future "send from tracker"
    feature should call this function rather than reimplementing send logic.

    Mirrors create_gmail_draft_for_user's signature so the parallel wrapper is a
    drop-in counterpart to create_drafts_parallel.

    Returns a dict {message_id, thread_id, recipient_email} on success, or a
    "mock_..." string when Gmail is unavailable or no recipient address exists
    (same sentinel convention as the draft path, so callers can skip it).
    """
    from app.utils.contact import clean_email_text

    try:
        # Clean the email subject and body FIRST
        email_subject = clean_email_text(email_subject)
        email_body = clean_email_text(email_body)

        gmail_service = get_gmail_service_for_user(user_email, user_id=user_id)

        if not gmail_service:
            print(f"[GmailClient] Gmail unavailable - cannot send, returning mock")
            return f"mock_{tier}_send_{contact.get('FirstName', 'unknown').lower()}"

        # Get the actual Gmail account email (might be shared account)
        try:
            gmail_account_email = gmail_service.users().getProfile(userId='me').execute().get('emailAddress')
        except Exception as profile_error:
            print(f"[GmailClient] Could not get Gmail profile: {profile_error}")
            gmail_account_email = user_email or os.getenv("DEFAULT_FROM_EMAIL", "noreply@offerloop.ai")

        print(f"[GmailClient] Sending {tier.capitalize()} email for contact {contact.get('FirstName', 'Unknown')}")

        # Pick the best recipient address (work/verified preferred over personal).
        recipient_email, source = _select_recipient_email(contact)
        print(f"[GmailSend] Recipient for {contact.get('FirstName')} {contact.get('LastName')}: selected={recipient_email} (source={source})")

        if not recipient_email:
            print(f"[GmailClient] No valid email found for contact - cannot send, returning mock")
            return f"mock_{tier}_send_{contact.get('FirstName', 'unknown').lower()}_no_email"

        # Build the same multipart message the draft path builds.
        from app.services import email_tracking
        tracking_token = email_tracking.start(user_id)
        message = _build_outreach_mime(
            recipient_email=recipient_email,
            gmail_account_email=gmail_account_email,
            email_subject=email_subject,
            email_body=email_body,
            user_info=user_info,
            resume_content=resume_content,
            resume_filename=resume_filename,
            resume_url=resume_url,
            tracking_token=tracking_token,
        )

        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')

        try:
            send_result = gmail_service.users().messages().send(userId='me', body={'raw': raw_message}).execute()
        except Exception as api_error:
            print(f"[GmailClient] Gmail API error sending message: {api_error}")
            import traceback
            traceback.print_exc()
            raise  # Re-raise so the outer handler can catch token-expiry, etc.

        message_id = send_result.get('id')
        thread_id = send_result.get('threadId')
        print(f"[GmailClient] Sent {tier.capitalize()} email {message_id} (thread {thread_id})")
        email_tracking.register(
            tracking_token, user_id, 'outreach', recipient_email=recipient_email,
            message_id=message_id, thread_id=thread_id, sent=True,
        )

        return {
            'message_id': message_id,
            'thread_id': thread_id,
            'recipient_email': recipient_email,
        }

    except Exception as e:
        print(f"[GmailClient] {tier.capitalize()} Gmail send failed: {e}")
        import traceback
        traceback.print_exc()
        raise  # Re-raise so the caller can handle token expiry and surface failures


def send_emails_parallel(contacts_with_emails, resume_bytes=None, resume_filename=None, user_info=None, user_id=None, tier='free', user_email=None, resume_url=None):
    """Send all outreach emails in parallel with rate limiting.

    Counterpart to create_drafts_parallel: same arguments and ordering
    guarantees, but each item is sent (not drafted) via send_gmail_email_for_user.

    Returns a list (aligned with contacts_with_emails order) of dicts with
    {message_id, thread_id, recipient_email}, or "mock_..." strings / None on
    per-item failure.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    if not contacts_with_emails:
        return []

    # When resume_bytes not provided but resume_url is set, download once for all sends
    if resume_bytes is None and resume_url:
        try:
            resume_bytes, downloaded_filename = download_resume_from_url(resume_url)
            if resume_bytes and not resume_filename:
                resume_filename = downloaded_filename
        except Exception as e:
            print(f"⚠️ Could not download resume from URL in send_emails_parallel: {e}")

    max_workers = min(5, len(contacts_with_emails))
    results = []
    results_lock = threading.Lock()

    def send_single(item):
        """Send a single email with resume attached when available."""
        try:
            contact = item['contact']
            email_subject = item['email_subject']
            email_body = item['email_body']

            result = send_gmail_email_for_user(
                contact, email_subject, email_body, tier, user_email,
                None,  # resume_url (deprecated, resume_bytes preferred)
                resume_bytes,
                resume_filename,
                user_info,
                user_id
            )
            return item.get('index', 0), result, None
        except Exception as e:
            return item.get('index', 0), None, str(e)

    print(f"[GmailClient] Sending {len(contacts_with_emails)} emails in parallel (max {max_workers} concurrent)")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_item = {
            executor.submit(send_single, item): item
            for item in contacts_with_emails
        }
        for future in as_completed(future_to_item):
            item = future_to_item[future]
            try:
                index, result, error = future.result()
                with results_lock:
                    results.append((index, result, error))
            except Exception as e:
                with results_lock:
                    results.append((item.get('index', 0), None, str(e)))

    # Sort results by index to maintain order
    results.sort(key=lambda x: x[0])
    return [result for _, result, _ in results]




# ============================================================================
# Auto-apply verification code lookup
# ============================================================================
# Greenhouse (and a few other ATSes) gate submit behind a per-tenant email
# verification step: after the form POSTs, they email an N-character code
# the candidate has to paste back into the form to actually complete the
# submission. This helper polls the candidate's Gmail for that code so the
# auto-apply runner can paste it programmatically.
#
# Verified against Temelio Greenhouse (2026-06-18 dogfood): sender is
# no-reply@greenhouse.io, body contains the code on its own line as an
# 8-char alphanumeric string, code is single-use + time-limited.

def search_for_verification_code(
    uid: str,
    *,
    sender_pattern: str = "from:greenhouse.io",
    code_regex: str = r"\b[A-Za-z0-9]{8}\b",
    since_epoch_seconds: int = 0,
    max_wait_seconds: int = 60,
    poll_interval_seconds: int = 5,
):
    """Poll the user's Gmail for an ATS verification code.

    Args:
        uid: Offerloop user id; their Gmail OAuth creds must be on file.
        sender_pattern: Gmail search operator narrowing the inbox (e.g.
            "from:greenhouse.io" or "from:no-reply@greenhouse.io").
        code_regex: Regex with at least one capturing group OR a whole match
            that yields the code. The first non-empty match is returned.
        since_epoch_seconds: Filter to messages received after this Unix
            timestamp. Use the submit-click timestamp so old codes don't
            get picked up. Gmail's after: operator is day-granular, so we
            also filter strict-inequality on internalDate after the fetch.
        max_wait_seconds: Total poll budget.
        poll_interval_seconds: Sleep between polls.

    Returns:
        The extracted code string, or None if no matching email arrived in
        the poll window or the user has no Gmail creds.

    Failure modes:
        - User never connected Gmail -> returns None immediately
        - Token expired and refresh failed -> returns None
        - Email arrives but the body doesn't match the regex -> returns None
          (the caller falls back to user-facing needs_verification UX)
    """
    import re
    import time as _time

    creds = _load_user_gmail_creds(uid)
    if not creds:
        return None
    try:
        service = _gmail_service(creds)
    except Exception:
        return None

    # Gmail's after: takes a Unix timestamp (seconds). Use the day-bucket
    # of since_epoch_seconds as the query filter; then strict-greater-than
    # on internalDate afterward.
    # Don't use `after:` operator — Gmail's API search index lags 30-60s
    # behind inbox delivery, and `after:` is day-granular anyway. Pull the
    # most recent N messages from the sender and post-filter by
    # internalDate (which IS millisecond-precise and updated synchronously
    # with delivery).
    q = sender_pattern

    pattern = re.compile(code_regex)
    deadline = _time.time() + max_wait_seconds
    poll_n = 0

    while _time.time() < deadline:
        poll_n += 1
        try:
            resp = service.users().messages().list(
                userId="me", q=q, maxResults=10,
            ).execute()
        except Exception as exc:
            print(f"[gmail.code] poll {poll_n} list failed: {exc}", flush=True)
            _time.sleep(poll_interval_seconds)
            continue

        messages = resp.get("messages") or []
        print(f"[gmail.code] poll {poll_n}: {len(messages)} candidate messages "
              f"(filter: internalDate > {since_epoch_seconds * 1000})", flush=True)

        for i, msg_ref in enumerate(messages):
            try:
                msg = service.users().messages().get(
                    userId="me", id=msg_ref["id"], format="full",
                ).execute()
            except Exception as exc:
                print(f"[gmail.code]   msg {i} fetch failed: {exc}", flush=True)
                continue

            try:
                internal_ms = int(msg.get("internalDate") or 0)
            except (TypeError, ValueError):
                internal_ms = 0
            internal_age_sec = (since_epoch_seconds * 1000 - internal_ms) / 1000.0
            if since_epoch_seconds and internal_ms <= since_epoch_seconds * 1000:
                print(f"[gmail.code]   msg {i} skipped — too old (Δ={internal_age_sec:.0f}s before submit)",
                      flush=True)
                continue

            body = extract_message_body(msg, max_length=4000) or ""
            matches = list(pattern.finditer(body))
            print(f"[gmail.code]   msg {i} fresh (Δ={-internal_age_sec:.0f}s after submit), "
                  f"body {len(body)} chars, {len(matches)} regex match(es)", flush=True)
            if not matches:
                # Surface the body once so we can see what the parser produced;
                # caller will iterate on the regex.
                print(f"[gmail.code]   msg {i} body preview: {body[:300]!r}", flush=True)
            for match in matches:
                code = match.group(1) if match.groups() else match.group(0)
                if code:
                    print(f"[gmail.code] CODE FOUND on poll {poll_n}: {code!r}", flush=True)
                    return code

        _time.sleep(poll_interval_seconds)

    print(f"[gmail.code] no code found after {poll_n} polls", flush=True)
    return None