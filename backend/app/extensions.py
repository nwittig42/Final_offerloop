"""
Flask extensions and initialization
"""
import os
import firebase_admin
from firebase_admin import credentials, firestore, auth as fb_auth
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import functools

# Global Firestore client
db = None
limiter = None

def get_limiter():
    """Get the rate limiter instance."""
    global limiter
    return limiter

def rate_limit_by_user(fn):
    """
    Rate limit decorator that uses user ID from Firebase auth.
    Falls back to IP address if user not authenticated.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        # Try to get user ID from request
        user_id = None
        if hasattr(request, 'firebase_user'):
            user_id = request.firebase_user.get('uid')
        
        # Use user ID for rate limiting if available, otherwise use IP
        key_func = lambda: f"user:{user_id}" if user_id else get_remote_address()
        
        # Apply rate limit
        if limiter:
            limiter.limit("100 per minute", key_func=key_func)(fn)(*args, **kwargs)
        
        return fn(*args, **kwargs)
    return wrapper

def init_firebase(app):
    """Initialize Firebase and set up Firestore client."""
    global db
    print(f"🔍 init_firebase called, current db value: {db}")
    print(f"🔍 firebase_admin._apps: {firebase_admin._apps}")
    if firebase_admin._apps:  # already initialized
        try:
            db = firestore.client()
            print(f"✅ Firebase already initialized, got Firestore client: {db}")
            print(f"🔍 db id: {id(db)}")
            return
        except Exception as e:
            print(f"⚠️ Firebase already initialized but Firestore client failed: {e}")
            firebase_admin._apps.clear()

    # Local end-to-end testing against the Firebase Emulator Suite
    # (scripts/e2e-emulator). Every Firebase service must point at a local
    # emulator, and the credential is anonymous, so nothing here can
    # authenticate to a real Google project: production is unreachable even
    # if a request somehow went to a real endpoint.
    if os.environ.get("OFFERLOOP_E2E_EMULATOR") == "1":
        _init_firebase_emulator()
        db = firestore.client()
        print(f"✅ Firebase initialized against the local emulators (no production access): {db}")
        return

    # Use only GOOGLE_APPLICATION_CREDENTIALS (no hardcoded fallback paths)
    cred = None
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")

    if cred_path and os.path.exists(cred_path):
        cred = credentials.Certificate(cred_path)
        print(f"✅ Using credentials from GOOGLE_APPLICATION_CREDENTIALS: {cred_path}")
    else:
        if cred_path:
            print(
                f"❌ GOOGLE_APPLICATION_CREDENTIALS is set to '{cred_path}' but file not found. "
                "Firebase will be initialized without credentials (e.g. for Render/cloud where credentials are provided differently)."
            )
        else:
            print(
                "❌ GOOGLE_APPLICATION_CREDENTIALS is not set. "
                "Firebase will be initialized with projectId/storageBucket only (e.g. for Render/cloud). "
                "Set GOOGLE_APPLICATION_CREDENTIALS to a service account JSON path for full Auth/Firestore/Storage."
            )

    try:
        if cred:
            firebase_admin.initialize_app(cred, {
                'projectId': 'offerloop-native',
                'storageBucket': 'offerloop-native.firebasestorage.app'
            })
            print("✅ Firebase initialized with credentials file")
        else:
            firebase_admin.initialize_app(options={
                'projectId': 'offerloop-native',
                'storageBucket': 'offerloop-native.firebasestorage.app'
            })
            print("✅ Firebase initialized with project ID / storage bucket only (no credentials file)")

        db = firestore.client()
        print(f"✅ Firestore client initialized successfully: {db}")
        print(f"🔍 db id: {id(db)}")

        # Verify Firebase Admin is properly initialized by checking _apps
        if not firebase_admin._apps:
            raise RuntimeError("Firebase Admin SDK initialization completed but _apps is empty")
        print(f"✅ Firebase Admin SDK verified: {len(firebase_admin._apps)} app(s) initialized")

    except Exception as e:
        error_msg = f"❌ Firebase initialization failed: {e}"
        print(error_msg)
        import traceback
        print(traceback.format_exc())
        db = None
        # Don't raise here - allow app to start but auth will fail gracefully
        print("⚠️ App will start but Firebase-dependent features will not work")

E2E_EMULATOR_ENV = ("FIRESTORE_EMULATOR_HOST", "FIREBASE_AUTH_EMULATOR_HOST", "STORAGE_EMULATOR_HOST")


class _AnonymousCredential(credentials.Base):
    """Firebase Admin credential that never authenticates (emulators only)."""

    def get_credential(self):
        from google.auth.credentials import AnonymousCredentials
        return AnonymousCredentials()


def _init_firebase_emulator():
    """Initialize Firebase Admin for the emulators, or refuse to start."""
    missing = [k for k in E2E_EMULATOR_ENV if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            "OFFERLOOP_E2E_EMULATOR=1 but these emulator hosts are not set: "
            + ", ".join(missing) + ". Refusing to start (it would fall back to production)."
        )
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        raise RuntimeError("OFFERLOOP_E2E_EMULATOR=1 must not run with GOOGLE_APPLICATION_CREDENTIALS set.")
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or "offerloop-native"
    firebase_admin.initialize_app(_AnonymousCredential(), {
        "projectId": project,
        "storageBucket": f"{project}.firebasestorage.app",
    })


def get_db():
    """Returns the Firestore client instance."""
    global db
    # Verbose debug log removed — fires on every request
    # If db is None but Firebase Admin is initialized, create the client on demand
    if db is None:
        if firebase_admin._apps:
            print("⚠️ db global is None but Firebase Admin is initialized, creating client on demand")
            try:
                db = firestore.client()
                print(f"✅ Firestore client created on demand: {db}, id: {id(db)}")
            except Exception as e:
                print(f"❌ Failed to create Firestore client: {e}")
                raise RuntimeError(f"Failed to create Firestore client: {e}")
        else:
            print("❌ ERROR: Firestore DB is None and Firebase Admin is not initialized!")
            print("❌ Make sure init_firebase() was called during app initialization")
            print("❌ Check GOOGLE_APPLICATION_CREDENTIALS environment variable")
            raise RuntimeError("Firestore DB not initialized. Call init_firebase() first.")
    return db

def require_firebase_auth(fn):
    """
    Decorator to require Firebase authentication for an endpoint.
    Extracts and verifies the Firebase ID token from the Authorization header.
    Allows OPTIONS requests (CORS preflight) to pass through without authentication.
    Includes retry logic for transient network errors.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        # Allow OPTIONS requests (CORS preflight) to pass through without authentication
        # Flask-CORS will automatically handle these and add the necessary headers
        if request.method == 'OPTIONS':
            # Skip auth check for OPTIONS - let Flask-CORS handle it automatically
            # The route handler will also return early for OPTIONS
            pass  # Continue to route handler which will handle OPTIONS
        else:
            # For non-OPTIONS requests, check authentication
            # Check if Firebase Admin is initialized
            if not firebase_admin._apps:
                error_msg = "Firebase Admin SDK not initialized. Call init_firebase() first."
                print(f"❌ {error_msg}")
                return jsonify({'error': error_msg}), 500
            
            auth_header = request.headers.get('Authorization', '')
            
            if not auth_header.startswith('Bearer '):
                print("❌ Missing or invalid Authorization header format")
                return jsonify({'error': 'Missing Authorization header'}), 401

            id_token = auth_header.split(' ', 1)[1].strip()

            # Retry logic for network errors
            import time
            try:
                import urllib3.exceptions
                URLLIB3_AVAILABLE = True
            except ImportError:
                URLLIB3_AVAILABLE = False
            
            max_retries = 3
            retry_delay = 0.5  # seconds
            
            for attempt in range(max_retries):
                try:
                    decoded = fb_auth.verify_id_token(id_token, clock_skew_seconds=5)
                    request.firebase_user = decoded
                    print("[Auth] Token verified")
                    # Lifecycle signal: fire-and-forget lastActiveAt stamp.
                    # In-process TTL cache prevents write-flooding.
                    try:
                        from app.services.lifecycle_signals import touch_last_active
                        touch_last_active(decoded.get('uid'))
                    except Exception:
                        pass
                    break  # Success, exit retry loop
                except ValueError as ve:
                    # Firebase Admin SDK not initialized error or invalid token format
                    error_str = str(ve)
                    if 'initialize' in error_str.lower() or 'init' in error_str.lower():
                        error_msg = "Firebase Admin SDK not initialized. Call init_firebase() first."
                        print(f"❌ {error_msg}")
                        return jsonify({'error': error_msg}), 500
                    else:
                        # Invalid token format - don't retry
                        print(f"❌ Token verification failed: {ve}")
                        return jsonify({'error': 'Invalid or expired token'}), 401
                except (ConnectionError, OSError) as network_error:
                    # Network-related errors - retry
                    error_str = str(network_error)
                    is_network_error = any(keyword in error_str.lower() for keyword in [
                        'connection', 'remote', 'disconnected', 'aborted', 'timeout', 
                        'network', 'unreachable', 'refused'
                    ])
                    
                    if is_network_error and attempt < max_retries - 1:
                        print(f"⚠️ Network error during token verification (attempt {attempt + 1}/{max_retries}): {network_error}")
                        time.sleep(retry_delay * (attempt + 1))  # Exponential backoff
                        continue
                    else:
                        # Max retries reached or non-retryable network error
                        print(f"❌ Token verification failed after {attempt + 1} attempts: {network_error}")
                        return jsonify({
                            'error': 'Network error during authentication. Please try again.',
                            'retry': True
                        }), 503  # Service Unavailable for network errors
                except Exception as token_error:
                    # Check if it's a network-related error by examining the exception
                    error_str = str(token_error)
                    error_type = type(token_error).__name__
                    
                    # Check for urllib3 errors if available
                    if URLLIB3_AVAILABLE:
                        try:
                            if isinstance(token_error, urllib3.exceptions.HTTPError):
                                if attempt < max_retries - 1:
                                    print(f"⚠️ HTTP error during token verification (attempt {attempt + 1}/{max_retries}): {token_error}")
                                    time.sleep(retry_delay * (attempt + 1))
                                    continue
                                else:
                                    return jsonify({
                                        'error': 'Network error during authentication. Please try again.',
                                        'retry': True
                                    }), 503
                        except:
                            pass
                    
                    # Check for network-related errors in the exception message or type
                    is_network_error = (
                        any(keyword in error_str.lower() for keyword in [
                            'connection', 'remote', 'disconnected', 'aborted', 'timeout',
                            'network', 'unreachable', 'refused'
                        ]) or
                        'Connection' in error_type or
                        'Remote' in error_type
                    )
                    
                    if is_network_error and attempt < max_retries - 1:
                        print(f"⚠️ Network error during token verification (attempt {attempt + 1}/{max_retries}): {token_error}")
                        time.sleep(retry_delay * (attempt + 1))  # Exponential backoff
                        continue
                    else:
                        # Not a network error or max retries reached - treat as auth failure
                        print(f"❌ Token verification failed: {token_error}")
                        if is_network_error:
                            return jsonify({
                                'error': 'Network error during authentication. Please try again.',
                                'retry': True
                            }), 503
                        else:
                            return jsonify({'error': 'Invalid or expired token. Please sign in again.'}), 401
            else:
                # All retries exhausted
                print(f"❌ Token verification failed after {max_retries} attempts")
                return jsonify({
                    'error': 'Authentication service temporarily unavailable. Please try again.',
                    'retry': True
                }), 503

        # Call the route handler - let its exceptions bubble up normally
        # For OPTIONS, the handler will return early; Flask-CORS will add headers
        return fn(*args, **kwargs)
    return wrapper


def require_tier(allowed_tiers):
    """
    Decorator to require specific subscription tier(s) for an endpoint.
    Must be used after @require_firebase_auth.
    
    Args:
        allowed_tiers: List of tier names (e.g., ['pro', 'elite']) or single tier string
    
    Example:
        @require_tier(['pro', 'elite'])
        @require_firebase_auth
        def export_contacts():
            ...
    """
    if isinstance(allowed_tiers, str):
        allowed_tiers = [allowed_tiers]
    
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # Must have Firebase auth first
            if not hasattr(request, 'firebase_user'):
                return jsonify({'error': 'Authentication required'}), 401
            
            user_id = request.firebase_user.get('uid')
            if not user_id:
                return jsonify({'error': 'User ID not found'}), 401
            
            # SECURITY: Always fetch tier from database, never from request
            db = get_db()
            if not db:
                return jsonify({'error': 'Database not available'}), 500
            
            try:
                user_ref = db.collection('users').document(user_id)
                user_doc = user_ref.get()
                
                if not user_doc.exists:
                    # New user defaults to free tier
                    tier = 'free'
                else:
                    user_data = user_doc.to_dict()
                    # Check both subscriptionTier and tier for backward compatibility
                    tier = user_data.get('subscriptionTier') or user_data.get('tier', 'free')
                
                # Check if user's tier is allowed
                if tier not in allowed_tiers:
                    tier_names = ', '.join([t.capitalize() for t in allowed_tiers])
                    try:
                        from app.utils.posthog_client import track_event
                        track_event(user_id, 'feature_gated', {
                            'feature': request.endpoint,
                            'path': request.path,
                            'required_tier': allowed_tiers,
                            'current_tier': tier,
                        })
                    except Exception:
                        pass
                    return jsonify({
                        'error': 'Upgrade required',
                        'message': f'This feature requires {tier_names} subscription',
                        'required_tier': allowed_tiers,
                        'current_tier': tier
                    }), 403
                
                # Store tier in request for use in route handler
                request.user_tier = tier
                return fn(*args, **kwargs)
                
            except Exception as e:
                print(f"Error checking tier: {e}")
                import traceback
                traceback.print_exc()
                return jsonify({'error': 'Failed to verify subscription tier'}), 500
        
        return wrapper
    return decorator

def get_rate_limit_key():
    """
    Custom key function for rate limiting that excludes static assets.
    Uses user ID for authenticated requests, IP address for unauthenticated.
    Returns None for static assets (which exempts them from rate limiting).
    """
    from flask import request
    import re as _re
    # Exclude static assets and root route from rate limiting
    if (request.path.startswith('/assets/') or
        request.path == '/favicon.ico' or
        request.path == '/' or
        request.path.endswith('.js') or
        request.path.endswith('.css') or
        request.path.endswith('.png') or
        request.path.endswith('.jpg') or
        request.path.endswith('.svg') or
        request.path.endswith('.woff') or
        request.path.endswith('.woff2')):
        return None  # None exempts from rate limiting

    # Exempt coffee chat prep status polling (GET /api/coffee-chat-prep/<id>)
    if (request.method == 'GET' and
        _re.match(r'^/api/coffee-chat-prep/[^/]+$', request.path)):
        return None

    # Exempt MCP server routes: they enforce their own per-IP limits
    # via app.mcp_server.rate_limit.MCPRateLimit, and double-throttling
    # via Flask-Limiter would silently block legitimate MCP traffic.
    # /claim is the post-paywall signup landing; we never want to
    # rate-limit a conversion event.
    if (request.path == '/mcp'
            or request.path == '/api/mcp/health'
            or request.path == '/claim'):
        return None
    
    # Exempt the email open pixel: mail providers fetch images through a
    # handful of shared proxy IPs, so a per-IP limit would drop real opens.
    if request.path.startswith('/api/t/o/'):
        return None

    # For authenticated requests, use user ID instead of IP address
    if hasattr(request, 'firebase_user') and request.firebase_user:
        user_id = request.firebase_user.get('uid')
        if user_id:
            return f"user:{user_id}"
    
    # Fallback to IP address for unauthenticated requests
    return get_remote_address()

def init_app_extensions(app: Flask):
    """Initializes Flask extensions like CORS, Rate Limiting, and Firebase."""
    global limiter
    # Initialize rate limiter with custom key function that excludes static assets
    limiter = Limiter(
        app=app,
        key_func=get_rate_limit_key,
        default_limits=["2000 per day", "500 per hour"],
        storage_uri="memory://",
        strategy="fixed-window",
        headers_enabled=True
    )
    # Replace in-memory storage with Firestore for persistence across workers/restarts
    try:
        from app.utils.firestore_limiter import FirestoreStorage
        limiter._storage = FirestoreStorage()
        print("[Extensions] Rate limiter using Firestore storage")
    except Exception as e:
        print(f"[Extensions] Firestore rate limiter unavailable, using in-memory: {e}")
    app.limiter = limiter
    
    # Check if we're in development mode
    is_dev = (
        os.getenv("FLASK_ENV") == "development" or 
        os.getenv("ENVIRONMENT") == "development" or
        os.getenv("FLASK_DEBUG") == "1" or
        app.debug
    )
    
    # Get allowed origins from environment or use defaults
    allowed_origins_env = os.getenv("CORS_ORIGINS", "")
    allowed_origins = [o.strip() for o in allowed_origins_env.split(",") if o.strip()] if allowed_origins_env else []
    
    # NOTE: Cannot use "*" with supports_credentials=True - must specify origins explicitly
    if is_dev:
        # Development: only Vite dev server origins
        dev_origins = [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
            "http://localhost:8081",
            "http://127.0.0.1:8081",
        ]
        all_origins = list(set(dev_origins + allowed_origins))
        print(f"🔧 Development mode: CORS configured with origins: {all_origins}")
        cors_config = {
            "origins": all_origins,
            "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
            "allow_headers": ["Content-Type", "Authorization", "X-Requested-With", "X-Session-Id"],
            "supports_credentials": True,
            "max_age": 3600,
            "expose_headers": ["Content-Type", "Authorization"]
        }
    else:
        # Production: production domains + localhost (localhost is harmless in
        # prod since it can't be reached from real users, and including it
        # means `python3 wsgi.py` works without needing FLASK_ENV=development).
        prod_origins = [
            "https://offerloop.ai",
            "https://www.offerloop.ai",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:8080",
            "http://127.0.0.1:8080",
            "http://localhost:8081",
            "http://127.0.0.1:8081",
        ]
        all_origins = list(set(prod_origins + allowed_origins))
        cors_config = {
            "origins": all_origins,
            "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
            "allow_headers": ["Content-Type", "Authorization", "X-Requested-With", "X-Session-Id"],
            "supports_credentials": True,
            "max_age": 3600,
            "expose_headers": ["Content-Type", "Authorization"]
        }
    
    CORS(app,
         resources={
             r"/api/*": cors_config,
             r"/*": cors_config  # Also allow CORS for all routes (for SPA)
         },
         automatic_options=True,  # Explicitly enable automatic OPTIONS handling
         supports_credentials=True)
    flask_secret = os.getenv("FLASK_SECRET")
    is_production = os.getenv("FLASK_ENV") == "production" or os.getenv("RENDER")
    if is_production and (not flask_secret or flask_secret == "dev"):
        raise RuntimeError(
            "FLASK_SECRET must be set to a secure random value in production. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    app.secret_key = flask_secret or "dev"
    init_firebase(app)  # Initialize Firebase when extensions are initialized