import os
import logging
import threading
import time
from flask import Flask, send_from_directory, abort, request, redirect, make_response

# Configure logging BEFORE importing anything else that uses logging
from .app.logging_config import configure_logging
configure_logging()

# --- Import your API blueprints (use package-relative imports) ---
from .app.routes.health import health_bp
from .app.routes.gmail_oauth import gmail_oauth_bp
from .app.routes.emails import emails_bp
from .app.routes.contacts import contacts_bp
from .app.routes.runs import runs_bp
from .app.routes.runs_similar import runs_similar_bp
from .app.routes.ranker import ranker_bp
from .app.routes.enrichment import enrichment_bp
from .app.routes.resume import resume_bp
from .app.routes.resume_builder import resume_builder_bp
from .app.routes.coffee_chat_prep import coffee_chat_bp
from .app.routes.billing import billing_bp
from .app.routes.users import users_bp
from .app.routes.outbox import outbox_bp
from .app.routes.firm_search import firm_search_bp
from .app.routes.school_affinity import school_affinity_bp
from .app.routes.dashboard import dashboard_bp
from .app.routes.timeline import timeline_bp
from .app.routes.search_history import search_history_bp
from .app.routes.parse_prompt import parse_prompt_bp
from .app.routes.contact_import import contact_import_bp
from .app.routes.job_board import job_board_bp
from .app.routes.find_contacts import find_contacts_bp
from .app.routes.auto_apply import auto_apply_bp
from .app.routes.alumni_discovery_routes import alumni_discovery_bp
from .app.routes.scout_assistant import scout_assistant_bp, scout_admin_bp
from .app.routes.linkedin_import import linkedin_import_bp
from .app.routes.auth_extension import auth_extension_bp
from .app.routes.email_template import email_template_bp
from .app.routes.admin import admin_bp
from .app.routes.gmail_webhook import gmail_webhook_bp
from .app.routes.nudges import nudges_bp
from .app.routes.queue import queue_bp
from .app.routes.jobs import jobs_bp
from .app.routes.extension_logs import extension_logs_bp
from .app.routes.search_suggestions import search_suggestions_bp
from .app.routes.briefing import briefing_bp
from .app.routes.agent import agent_bp
from .app.routes.loop_notifications import loop_notifications_bp
from .app.routes.loops import loops_bp
from .app.routes.metrics import metrics_bp
from .app.routes.referrals import referrals_bp
from .app.routes.interview_prep_public import interview_prep_public_bp
from .app.routes.cover_letter_public import cover_letter_public_bp
from .app.routes.resume_workshop_public import resume_workshop_public_bp
from .app.routes.meeting_prep_public import meeting_prep_public_bp
from .app.routes.find_hiring_manager_public import find_hiring_manager_public_bp
from .app.routes.find_companies_public import find_companies_public_bp
from .app.routes.orgs import orgs_bp
from .app.routes.find_jobs_public import find_jobs_public_bp
from .app.routes.find_people_public import find_people_public_bp
from .app.routes.shares import shares_bp
from .app.routes.lifecycle import lifecycle_bp
from .app.routes.email_tracking import email_tracking_bp
from .app.routes.beehiiv_webhook import beehiiv_webhook_bp
from .app.routes.mobile_handoff import mobile_handoff_bp, web_handoff_bp
from .app.extensions import init_app_extensions

def create_app() -> Flask:
    # Project layout assumptions:
    REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
    FRONTEND_DIR = os.path.join(REPO_ROOT, "connect-grow-hire")
    STATIC_DIR = os.path.join(FRONTEND_DIR, "dist")

    app = Flask(
        __name__,
        static_folder=STATIC_DIR,
        static_url_path=""
    )

    # --- 410 Gone for pruned dead SEO pages ---
    # These pages (old firm-name-swapped templates with zero clicks) were
    # permanently removed on 2026-06-15 and dropped from the sitemap. Serving
    # 410 (not 404) tells Google to deindex them. Earners are NOT in this list.
    # Registered before the prerender middleware so crawlers get the 410 itself.
    PRUNED_PATHS = set()
    try:
        _pruned_file = os.path.join(os.path.dirname(__file__), "app", "seo_pruned_urls.txt")
        with open(_pruned_file, "r", encoding="utf-8") as _pf:
            PRUNED_PATHS = {ln.strip().rstrip("/") for ln in _pf if ln.strip() and not ln.startswith("#")}
        app.logger.info(f"Loaded {len(PRUNED_PATHS)} pruned (410) SEO paths")
    except FileNotFoundError:
        app.logger.warning("seo_pruned_urls.txt not found; 410 pruning inactive")

    @app.before_request
    def gone_pruned_pages():
        if request.method != "GET":
            return None
        if (request.path.rstrip("/") or "/") in PRUNED_PATHS:
            resp = make_response(
                "<!doctype html><title>410 Gone</title>"
                "<h1>410 Gone</h1><p>This page has been permanently removed.</p>"
            )
            resp.status_code = 410
            resp.mimetype = "text/html"
            return resp
        return None

    # --- Prerender.io middleware for bot crawlers (SEO/AEO) ---
    PRERENDER_TOKEN = os.environ.get("PRERENDER_TOKEN")
    if not PRERENDER_TOKEN:
        app.logger.warning("PRERENDER_TOKEN not set — bot SSR via Prerender.io is disabled")
    else:
        app.logger.info(f"PRERENDER_TOKEN loaded: {PRERENDER_TOKEN[:6]}... ({len(PRERENDER_TOKEN)} chars)")
    BOT_AGENTS = [
        'googlebot', 'bingbot', 'yandex', 'duckduckbot', 'slurp',
        'baiduspider', 'facebookexternalhit', 'twitterbot', 'linkedinbot',
        'embedly', 'quora link preview', 'showyoubot', 'outbrain',
        'pinterest', 'developers.google.com/+/web/snippet', 'slackbot',
        'vkshare', 'w3c_validator', 'redditbot', 'applebot', 'whatsapp',
        'flipboard', 'tumblr', 'bitlybot', 'skypeuripreview', 'nuzzel',
        'discordbot', 'google page speed', 'qwantify', 'pinterestbot',
        'bitrix link preview', 'xing-contenttabreceiver', 'chrome-lighthouse',
        'telegrambot', 'gptbot', 'claudebot', 'anthropic-ai', 'perplexitybot',
        'ccbot', 'chatgpt-user', 'google-extended', 'bytespider'
    ]

    @app.before_request
    def prerender_middleware():
        user_agent = request.headers.get('User-Agent', '').lower()
        is_bot = any(bot in user_agent for bot in BOT_AGENTS)

        # Only prerender GET requests to non-API, non-asset routes
        if not is_bot or not PRERENDER_TOKEN:
            return None
        if request.method != 'GET':
            return None
        if request.path.startswith('/api/'):
            return None
        if request.path.startswith('/assets/'):
            return None
        if '.' in request.path.split('/')[-1]:  # skip files like .js .css .png
            return None

        import requests as req
        prerender_url = f"https://service.prerender.io/{request.url}"
        try:
            resp = req.get(
                prerender_url,
                headers={
                    'X-Prerender-Token': PRERENDER_TOKEN,
                    'User-Agent': request.headers.get('User-Agent', '')
                },
                timeout=10
            )
            if resp.status_code >= 500:
                app.logger.warning(f"Prerender returned {resp.status_code} for {request.url}, falling through to SPA")
                return None
            from flask import Response
            return Response(
                resp.content,
                status=resp.status_code,
                content_type=resp.headers.get('Content-Type', 'text/html')
            )
        except Exception as e:
            app.logger.warning(f"Prerender failed for {request.url}: {e}")
            return None  # Fall through to normal serving

    # Request context: attach request_id + session_id to every request
    from .app.utils.request_context import init_request_context
    init_request_context(app)

    print("🚀 Initializing app extensions...")
    init_app_extensions(app)
    try:
        from .app.config import integration_status
        missing = [name for name, ok in integration_status().items() if not ok]
        print("[config] signup -> first email integrations not configured: " + (", ".join(missing) or "none"))
    except Exception as e:
        print(f"[config] integration status check failed: {e}")
    print("✅ App extensions initialized")

    # Security headers on every response. Enumerated script-src so a compromised
    # subdomain can't inject; default/connect stay `https:` so we don't have to
    # keep chasing Firebase/PostHog/Sentry subdomains as they add hosts.
    CSP_POLICY = (
        "default-src 'self' https:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
        "https://apis.google.com https://accounts.google.com "
        "https://js.stripe.com https://checkout.stripe.com "
        "https://subscribe-forms.beehiiv.com "
        "https://*.posthog.com https://us-assets.i.posthog.com "
        "https://browser.sentry-cdn.com https://*.sentry.io; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "img-src 'self' data: blob: https:; "
        "connect-src 'self' https: wss:; "
        "frame-src 'self' blob: https://js.stripe.com https://checkout.stripe.com "
        "https://accounts.google.com https://*.firebaseapp.com; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self' https:; "
        "frame-ancestors 'self'; "
        "upgrade-insecure-requests"
    )

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains; preload",
        )
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=(), payment=(self \"https://js.stripe.com\" \"https://checkout.stripe.com\")",
        )
        response.headers.setdefault("Content-Security-Policy", CSP_POLICY)
        return response
    
    # Initialize Sentry error tracking
    from app.utils.sentry_config import init_sentry
    init_sentry(app)
    
    # Initialize Swagger API documentation (only in development)
    if os.environ.get('FLASK_ENV') == 'development':
        from app.utils.swagger_config import init_swagger
        init_swagger(app)
    
    # Register error handlers
    from app.utils.exceptions import register_error_handlers
    register_error_handlers(app)
    print("✅ Error handlers registered")
    
    # Check if db was initialized
    from .app.extensions import db
    print(f"🔍 After init_app_extensions, db is: {db}")
    print(f"🔍 db type: {type(db)}")
    
    # --- Logging (handy on Render) ---
    app.logger.setLevel(logging.INFO)
    app.logger.info("STATIC FOLDER: %s", app.static_folder)
    app.logger.info("INDEX EXISTS? %s", os.path.exists(os.path.join(app.static_folder, "index.html")))

    # --- Register API blueprints FIRST ---
    app.register_blueprint(health_bp)
    app.register_blueprint(gmail_oauth_bp)
    
    # --- Backwards compatibility: Add old /api/gmail/* routes ---
    # These routes call the same handlers as /api/google/* routes
    from .app.extensions import require_firebase_auth
    from .app.routes.gmail_oauth import google_oauth_start, gmail_status
    
    @app.route('/api/gmail/oauth/start', methods=['GET', 'OPTIONS'])
    @require_firebase_auth
    def gmail_oauth_start_legacy():
        """Legacy route for /api/gmail/oauth/start - calls same handler as /api/google/oauth/start"""
        return google_oauth_start()
    
    @app.route('/api/gmail/status', methods=['GET', 'OPTIONS'])
    @require_firebase_auth
    def gmail_status_legacy():
        """Legacy route for /api/gmail/status - calls same handler as /api/google/gmail/status"""
        return gmail_status()
    
    app.register_blueprint(emails_bp)
    app.register_blueprint(linkedin_import_bp)  # Register before contacts_bp to avoid route conflicts
    app.register_blueprint(contacts_bp)
    app.register_blueprint(shares_bp)
    app.register_blueprint(runs_bp)
    app.register_blueprint(runs_similar_bp)
    app.register_blueprint(ranker_bp)  # People deck, ported from staging/mobile-field 2026-09-07
    app.register_blueprint(enrichment_bp)
    app.register_blueprint(resume_bp)
    app.register_blueprint(resume_builder_bp)
    app.register_blueprint(coffee_chat_bp)
    app.register_blueprint(billing_bp)
    app.register_blueprint(users_bp)
    app.register_blueprint(outbox_bp)
    app.register_blueprint(firm_search_bp)
    app.register_blueprint(school_affinity_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(timeline_bp)
    app.register_blueprint(search_history_bp)
    app.register_blueprint(parse_prompt_bp)
    app.register_blueprint(contact_import_bp)
    app.register_blueprint(job_board_bp)
    app.register_blueprint(find_contacts_bp)
    app.register_blueprint(auto_apply_bp)
    app.register_blueprint(alumni_discovery_bp)
    app.register_blueprint(scout_assistant_bp)
    app.register_blueprint(scout_admin_bp)
    app.register_blueprint(auth_extension_bp)
    app.register_blueprint(email_template_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(gmail_webhook_bp)
    app.register_blueprint(nudges_bp)
    app.register_blueprint(queue_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(extension_logs_bp)
    app.register_blueprint(search_suggestions_bp)
    app.register_blueprint(briefing_bp)
    app.register_blueprint(agent_bp)
    app.register_blueprint(loops_bp)
    app.register_blueprint(loop_notifications_bp)
    app.register_blueprint(metrics_bp)
    app.register_blueprint(referrals_bp)
    app.register_blueprint(interview_prep_public_bp)
    app.register_blueprint(cover_letter_public_bp)
    app.register_blueprint(resume_workshop_public_bp)
    app.register_blueprint(meeting_prep_public_bp)
    app.register_blueprint(find_hiring_manager_public_bp)
    app.register_blueprint(find_companies_public_bp)
    app.register_blueprint(orgs_bp)  # Org alumni directories (OFF-42)
    app.register_blueprint(find_jobs_public_bp)
    app.register_blueprint(find_people_public_bp)
    app.register_blueprint(lifecycle_bp)         # /api/lifecycle/tick + unsubscribe
    app.register_blueprint(email_tracking_bp)    # /api/t/o/<token>.gif (email open pixel, public)
    app.register_blueprint(beehiiv_webhook_bp)   # /api/beehiiv/webhook (inbound unsub sync)
    app.register_blueprint(mobile_handoff_bp)    # /api/mobile/web-handoff (app -> web checkout)
    app.register_blueprint(web_handoff_bp)       # /api/web/handoff-exchange (web burn code -> custom token)

    # --- MCP server (anonymous IP-based, mounts /mcp + /api/mcp/health) ---
    # Skippable for local dev: the MCP mount refuses to boot against the prod
    # Firestore project from a non-prod env, so DISABLE_MCP=1 lets a developer
    # run the rest of the app locally without it.
    if os.getenv("DISABLE_MCP") != "1":
        from .app.mcp_server.flask_mount import register_mcp_blueprint
        register_mcp_blueprint(app)

    # --- /claim?token=xyz attribution landing for MCP paywall ---
    from .app.routes.claim import claim_bp
    app.register_blueprint(claim_bp)

    # --- Debug route to check frontend build (dev only) ---
    @app.route('/api/debug/frontend')
    def debug_frontend():
        is_dev = (
            os.getenv("FLASK_ENV") == "development"
            or os.getenv("FLASK_DEBUG") == "1"
            or app.debug
        )
        if not is_dev:
            return {"error": "Not available in production"}, 404

        static_dir = app.static_folder
        exists = os.path.exists(static_dir)
        index_exists = os.path.exists(os.path.join(static_dir, 'index.html')) if exists else False

        return {
            'static_folder_exists': exists,
            'index_html_exists': index_exists,
            'file_count': len(os.listdir(static_dir)) if exists else 0,
        }

    # --- Serve built asset files (e.g., /assets/*) ---
    @app.route('/assets/<path:filename>')
    def vite_assets(filename):
        assets_dir = os.path.join(app.static_folder, 'assets')
        response = send_from_directory(assets_dir, filename)
        # Add caching headers for static assets (1 year cache)
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        return response

    # --- Serve root index.html ---
    @app.route('/')
    def index():
        response = make_response(send_from_directory(app.static_folder, 'index.html'))
        # No s-maxage: a CDN-cached index.html can outlive a deploy and point
        # at hashed chunks that no longer exist, breaking every page load
        # until the shared cache expires. index.html must always revalidate.
        response.headers['Cache-Control'] = 'public, max-age=0, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    # --- Serve SEO/AEO root files explicitly ---
    @app.route('/sitemap.xml')
    def sitemap():
        return send_from_directory(app.static_folder, 'sitemap.xml', mimetype='application/xml')

    @app.route('/robots.txt')
    def robots():
        return send_from_directory(app.static_folder, 'robots.txt', mimetype='text/plain')

    @app.route('/llms.txt')
    def llms():
        return send_from_directory(app.static_folder, 'llms.txt', mimetype='text/plain')

    # Domain verification token for the OpenAI ChatGPT Apps directory.
    # Returned at the .well-known path OpenAI's submission flow expects.
    @app.route('/.well-known/openai-apps-challenge')
    def openai_apps_challenge():
        token = os.environ.get(
            'OPENAI_APPS_CHALLENGE_TOKEN',
            'kMyLrHsGzLSj1Of8pzrzlHuLKo9cXWhMbDnWF8rhgOA',
        )
        resp = make_response(token, 200)
        resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    # --- 404 handler for SPA (catches all unmatched routes) ---
    @app.errorhandler(404)
    def not_found(e):
        # Don't serve index.html for API routes that don't exist
        if request.path.startswith('/api/'):
            return "API endpoint not found", 404

        # Never SPA-fallback missing build assets: after a deploy, stale tabs
        # request old hashed chunks — serving index.html as the "chunk"
        # produces a confusing MIME error and can poison shared caches with
        # HTML under a .js URL. A clean 404 lets the frontend's chunk-reload
        # recovery kick in.
        if request.path.startswith('/assets/'):
            return "Asset not found", 404

        # For everything else, serve the React app
        index_path = os.path.join(app.static_folder, 'index.html')
        if os.path.exists(index_path):
            app.logger.info(f"404 handler serving index.html for path: {request.path}")
            response = make_response(send_from_directory(app.static_folder, 'index.html'))
            # No s-maxage — see the index() route: stale shared-cache HTML
            # breaks chunk loading after every deploy.
            response.headers['Cache-Control'] = 'public, max-age=0, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            return response
        else:
            return "Frontend build not found", 500

    # Start tracker scanner background thread (every 6 hours).
    #
    # This single thread dispatches to multiple scanners per iteration. Each
    # scanner runs inside its own try/except so one scanner's failure does
    # NOT prevent the other from running in the same iteration.
    #
    # Contract: docs/designs/tracker-daemon-contract.md
    # Ownership: Flywheel owns the thread lifecycle. Each scanner is owned
    # by its feature team. Adding a third scanner requires updating the
    # contract doc and re-evaluating the cadence math.
    _tracker_logger = logging.getLogger("tracker_scanner")

    def _tracker_scanner_loop():
        _tracker_logger.info("Tracker scanner thread started (interval=6 hours)")
        SIX_HOURS = 6 * 3600
        # Initial delay: wait 5 minutes after boot to let app stabilize
        time.sleep(300)
        while True:
            # ---- Nudge scanner (Flywheel Phase 1) -----------------------
            # Isolated: a crash here MUST NOT block other scanners below.
            if os.getenv("NUDGES_ENABLED", "true").lower() == "true":
                try:
                    with app.app_context():
                        from .app.services.nudge_service import scan_and_generate_nudges
                        scan_and_generate_nudges()
                except Exception:
                    logging.getLogger("nudge_scanner").exception(
                        "Nudge scanner iteration failed"
                    )
            else:
                _tracker_logger.info("Nudge scanner disabled via NUDGES_ENABLED=false")

            # ---- Queue scanner (Agentic Queue Phase 2) ------------------
            # Tuesday-gated per docs/designs/tracker-daemon-contract.md.
            # Isolated: a crash here must NOT block the aggregation scanner.
            if os.getenv("QUEUE_SCANNER_ENABLED", "true").lower() == "true":
                try:
                    with app.app_context():
                        from .app.services.queue_service import scan_and_generate_queues
                        scan_and_generate_queues()
                except Exception:
                    logging.getLogger("queue_scanner").exception(
                        "Queue scanner iteration failed"
                    )
            else:
                _tracker_logger.info(
                    "Queue scanner disabled via QUEUE_SCANNER_ENABLED=false"
                )

            # ---- Aggregation scanner (AI Flywheel Phase 2) --------------
            # Sunday 3-9am UTC window, gated internally. Full contact scan
            # for analytics/email_outcomes segments. See the daemon contract.
            if os.getenv("AGGREGATION_SCANNER_ENABLED", "true").lower() == "true":
                try:
                    with app.app_context():
                        from .app.services.email_baseline import aggregate_email_outcomes
                        aggregate_email_outcomes()
                except Exception:
                    logging.getLogger("aggregation_scanner").exception(
                        "Aggregation scanner iteration failed"
                    )
            else:
                _tracker_logger.info(
                    "Aggregation scanner disabled via AGGREGATION_SCANNER_ENABLED=false"
                )

            time.sleep(SIX_HOURS)

    tracker_thread = threading.Thread(target=_tracker_scanner_loop, daemon=True)
    tracker_thread.start()
    _tracker_logger.info("Tracker scanner thread registered (first run in ~5 minutes)")

    # Start Gmail watch renewal background thread (every 6 days)
    _watch_logger = logging.getLogger("watch_renewal")

    def _watch_renewal_loop():
        _watch_logger.info("Gmail watch renewal thread started (interval=6 days)")
        SIX_DAYS = 6 * 24 * 3600
        # Boot stabilization delay (matches agent_daemon, watchdog).
        # Sleep-at-end below means a redeploy-restarted process runs its
        # first cycle ~10 min after boot, not 6 days later.
        time.sleep(600)
        while True:
            try:
                with app.app_context():
                    from .app.routes.admin import renew_watches as _renew_endpoint
                    # Call the core logic directly, not the HTTP handler
                    from .app.extensions import get_db
                    from .app.services.gmail_client import renew_gmail_watch
                    db = get_db()
                    now_ms = int(time.time() * 1000)
                    one_day_ms = 86400 * 1000
                    renewed = 0
                    failed = 0
                    # Materialize the user list here so a mid-iteration
                    # Firestore page-fetch failure raises inside the outer
                    # try (aborts cycle, retries in 6 days) rather than
                    # partway through the loop (skipping later users for
                    # 6 days). The per-user try below only guards the body.
                    user_docs = list(db.collection("users").stream())
                    for user_doc in user_docs:
                        uid = user_doc.id
                        try:
                            gmail_ref = db.collection("users").document(uid).collection("integrations").document("gmail")
                            gmail_doc = gmail_ref.get()
                            if not gmail_doc.exists:
                                continue
                            data = gmail_doc.to_dict() or {}
                            if not (data.get("token") or data.get("refresh_token")):
                                continue
                            watch_exp = data.get("watchExpiration")
                            if watch_exp is not None:
                                try:
                                    watch_exp = int(watch_exp)
                                except (TypeError, ValueError):
                                    watch_exp = None
                            if watch_exp is not None and (watch_exp - now_ms) >= one_day_ms:
                                continue
                            try:
                                renew_gmail_watch(uid)
                                renewed += 1
                            except Exception as e:
                                failed += 1
                                _watch_logger.error("Watch renewal failed uid=%s: %s", uid, e)
                        except Exception:
                            failed += 1
                            _watch_logger.exception("Watch renewal aborted for uid=%s", uid)
                    _watch_logger.info("Watch renewal complete: renewed=%d failed=%d", renewed, failed)
                    # Heartbeat: mark cycle success for the daemon watchdog
                    # at system/gmail_watch. Failure to write must not
                    # break the cycle.
                    try:
                        from datetime import datetime, timezone
                        db.collection("system").document("gmail_watch").set({
                            "lastSuccessAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                            "renewedCount": renewed,
                            "failedCount": failed,
                        })
                    except Exception:
                        _watch_logger.exception("Failed to write gmail_watch heartbeat")
            except Exception as e:
                _watch_logger.error("Watch renewal loop error: %s", e)
            time.sleep(SIX_DAYS)

    if os.getenv("WATCH_RENEWAL_ENABLED", "true").lower() == "true":
        watch_renewal_thread = threading.Thread(target=_watch_renewal_loop, daemon=True)
        watch_renewal_thread.start()
        _watch_logger.info("Gmail watch renewal thread registered (first run in ~10 minutes)")
    else:
        _watch_logger.info("Gmail watch renewal disabled via WATCH_RENEWAL_ENABLED=false")

    # ---- Daemon healthcheck watchdog (every 1 hour) ----------------------
    #
    # Reads health docs from Firestore for each daemon scanner (nudge, queue,
    # gmail_watch) and logs warnings when lastSuccessAt is stale.
    #
    # Contract: docs/designs/tracker-daemon-contract.md
    # TODOS.md P1: "Daemon Thread Healthcheck & Auto-Restart"
    _watchdog_logger = logging.getLogger("daemon_watchdog")

    # Staleness thresholds in seconds.
    # Nudge: 8h (2h slack on 6h cadence)
    # Queue: 7d (slightly over one-week Tuesday cadence)
    # Aggregation: 8d (slightly over one-week Sunday cadence)
    # Gmail watch: 6.5d (6-day renewal cadence + 12h slack). MUST stay
    # under 7d — Gmail push watches expire at exactly 7d, so this must
    # fire BEFORE expiry to leave time for investigation / manual re-arm.
    # A 7d threshold means the alert lands the same moment watches die.
    _STALENESS = {
        "nudge_scanner": 8 * 3600,
        "queue_scanner": 7 * 24 * 3600,
        "aggregation_scanner": 8 * 24 * 3600,
        "gmail_watch": 6 * 24 * 3600 + 12 * 3600,
    }

    def _watchdog_loop():
        ONE_HOUR = 3600
        _watchdog_logger.info("Daemon watchdog started (interval=1 hour)")
        # Wait 10 minutes to give scanners time to do their first run.
        time.sleep(600)
        while True:
            try:
                with app.app_context():
                    from .app.extensions import get_db
                    from datetime import datetime, timezone
                    db = get_db()
                    now = datetime.now(timezone.utc)
                    stale_scanners = []

                    for scanner_name, threshold_seconds in _STALENESS.items():
                        try:
                            doc = db.collection("system").document(scanner_name).get()
                            if not doc.exists:
                                # Scanner hasn't run yet — only warn if it
                                # should have had enough time (> threshold).
                                _watchdog_logger.debug(
                                    "No health doc for %s (may not have run yet)",
                                    scanner_name,
                                )
                                continue

                            data = doc.to_dict() or {}
                            last_success = data.get("lastSuccessAt")
                            if not last_success:
                                continue

                            # Parse ISO timestamp
                            if isinstance(last_success, str):
                                last_success = datetime.fromisoformat(
                                    last_success.replace("Z", "+00:00")
                                )

                            age_seconds = (now - last_success).total_seconds()
                            if age_seconds > threshold_seconds:
                                stale_scanners.append(scanner_name)
                                _watchdog_logger.critical(
                                    "STALE: %s last succeeded %.1f hours ago "
                                    "(threshold: %.1f hours)",
                                    scanner_name,
                                    age_seconds / 3600,
                                    threshold_seconds / 3600,
                                )
                                # Emit a PostHog event so staleness is
                                # queryable outside logs. sync=True because
                                # the daemon can die between async flushes.
                                try:
                                    from .app.utils.posthog_client import track_event
                                    track_event(
                                        None,
                                        "daemon_stale",
                                        {
                                            "scanner": scanner_name,
                                            "age_hours": age_seconds / 3600,
                                            "threshold_hours": threshold_seconds / 3600,
                                        },
                                        sync=True,
                                    )
                                except Exception:
                                    _watchdog_logger.exception("Failed to emit daemon_stale event")
                        except Exception:
                            _watchdog_logger.exception(
                                "Error checking health for %s", scanner_name
                            )

                    if not stale_scanners:
                        _watchdog_logger.info("All daemons healthy")
                    else:
                        # Write a watchdog status doc so external monitors
                        # (Render health checks, future alerting) can read it.
                        try:
                            db.collection("system").document("watchdog").set({
                                "lastCheckAt": now.isoformat().replace("+00:00", "Z"),
                                "staleScanners": stale_scanners,
                                "healthy": len(stale_scanners) == 0,
                            })
                        except Exception:
                            _watchdog_logger.exception("Failed to write watchdog status")

            except Exception:
                _watchdog_logger.exception("Watchdog loop error")

            time.sleep(ONE_HOUR)

    if os.getenv("WATCHDOG_ENABLED", "true").lower() == "true":
        watchdog_thread = threading.Thread(target=_watchdog_loop, daemon=True)
        watchdog_thread.start()
        _watchdog_logger.info("Daemon watchdog registered (first check in ~10 minutes)")
    else:
        _watchdog_logger.info("Daemon watchdog disabled via WATCHDOG_ENABLED=false")

    # ---- Agent daemon thread (every 1 hour) ─────────────────────────────────
    #
    # Separate thread (NOT a 4th scanner) per daemon contract.
    # Scans active agents and runs cycles where nextCycleAt <= now.
    _agent_logger = logging.getLogger("agent_daemon")

    def _agent_daemon_loop():
        ONE_HOUR = 3600
        _agent_logger.info("Agent daemon thread started (interval=1 hour)")
        # Boot stabilization delay
        time.sleep(600)
        while True:
            try:
                with app.app_context():
                    from .app.services.agent_service import run_due_agent_cycles
                    run_due_agent_cycles()
            except Exception:
                _agent_logger.exception("Agent daemon failed")
            time.sleep(ONE_HOUR)

    if os.getenv("AGENT_DAEMON_ENABLED", "true").lower() == "true":
        agent_thread = threading.Thread(target=_agent_daemon_loop, daemon=True)
        agent_thread.start()
        _agent_logger.info("Agent daemon registered (first run in ~10 minutes)")
    else:
        _agent_logger.info("Agent daemon disabled via AGENT_DAEMON_ENABLED=false")

    # ---- Loop scheduler daemon (every 1 hour) ────────────────────────────────
    #
    # Phase 8. Scans users/{uid}/loops/{loopId} for status='running' AND
    # nextRunAt<=now. Gates each by cadence, credit pool, weekly per-Loop
    # budget, inactivity, and quiet hours. Fires due cycles via RQ.
    #
    # Separate from the legacy agent daemon above — that one still serves
    # users on the singleton agent_config doc until they get migrated.
    _loop_sched_logger = logging.getLogger("loop_scheduler_daemon")

    def _loop_scheduler_loop():
        ONE_HOUR = 3600
        _loop_sched_logger.info("Loop scheduler daemon started (interval=1 hour)")
        time.sleep(600)  # boot stabilization
        while True:
            try:
                with app.app_context():
                    from .app.services.loop_scheduler import run_due_loops
                    run_due_loops()
            except Exception:
                _loop_sched_logger.exception("Loop scheduler daemon failed")
            time.sleep(ONE_HOUR)

    if os.getenv("LOOP_SCHEDULER_ENABLED", "true").lower() == "true":
        loop_sched_thread = threading.Thread(target=_loop_scheduler_loop, daemon=True)
        loop_sched_thread.start()
        _loop_sched_logger.info("Loop scheduler registered (first run in ~10 minutes)")
    else:
        _loop_sched_logger.info("Loop scheduler disabled via LOOP_SCHEDULER_ENABLED=false")

    # ---- Agent follow-up daemon (every 1 hour) ───────────────────────────────
    #
    # Separate from the main agent daemon. Only scans for stale outreach
    # (emails sent 7+ days ago with no reply) and queues follow-up nudges.
    _followup_logger = logging.getLogger("agent_followup_daemon")

    def _agent_followup_loop():
        ONE_HOUR = 3600
        _followup_logger.info("Agent followup daemon started (interval=1 hour)")
        time.sleep(600)  # boot stabilization
        while True:
            try:
                with app.app_context():
                    from .app.services.agent_service import run_followup_scan
                    run_followup_scan()
            except Exception:
                _followup_logger.exception("Agent followup daemon failed")
            time.sleep(ONE_HOUR)

    if os.getenv("AGENT_FOLLOWUP_ENABLED", "true").lower() == "true":
        followup_thread = threading.Thread(target=_agent_followup_loop, daemon=True)
        followup_thread.start()
        _followup_logger.info("Agent followup daemon registered (first run in ~10 minutes)")
    else:
        _followup_logger.info("Agent followup daemon disabled via AGENT_FOLLOWUP_ENABLED=false")

    # ---- Agent daily digest daemon (every 24 hours) ─────────────────────────
    #
    # Sends a daily summary email via each user's own Gmail OAuth.
    # Separate from the agent cycle daemon.
    _digest_logger = logging.getLogger("agent_digest_daemon")

    def _agent_digest_loop():
        TWENTY_FOUR_HOURS = 86400
        _digest_logger.info("Agent digest daemon started (interval=24h)")
        time.sleep(3600)  # boot stabilization — 1 hour
        while True:
            try:
                with app.app_context():
                    from .app.services.agent_service import send_daily_digests
                    send_daily_digests()
            except Exception:
                _digest_logger.exception("Agent digest daemon failed")
            time.sleep(TWENTY_FOUR_HOURS)

    if os.getenv("AGENT_DIGEST_ENABLED", "true").lower() == "true":
        digest_thread = threading.Thread(target=_agent_digest_loop, daemon=True)
        digest_thread.start()
        _digest_logger.info("Agent digest daemon registered (first run in ~1 hour)")
    else:
        _digest_logger.info("Agent digest daemon disabled via AGENT_DIGEST_ENABLED=false")

    # ---- Fantastic.jobs delta ingest daemon (every 24 hours) ────────────────
    #
    # Calls the Ultra+ Modified Jobs API (1 Request credit/call, NOT Jobs
    # credits) to pick up postings modified in the last 24h. Matches the same
    # filter recipes as the weekly 7d sweep. Cheap to run daily because the
    # endpoint doesn't burn Jobs credits.
    _fj_modified_logger = logging.getLogger("fantastic_modified_daemon")

    def _fantastic_modified_loop():
        TWENTY_FOUR_HOURS = 86400
        _fj_modified_logger.info("Fantastic.jobs modified daemon started (interval=24h)")
        time.sleep(1800)  # boot stabilization — 30 min
        while True:
            try:
                with app.app_context():
                    from .pipeline.main import run_fantastic_modified
                    run_fantastic_modified()
            except Exception:
                _fj_modified_logger.exception("Fantastic.jobs modified daemon failed; will retry next cycle")
            time.sleep(TWENTY_FOUR_HOURS)

    if os.getenv("FJ_MODIFIED_DAEMON_ENABLED", "true").lower() == "true":
        fj_modified_thread = threading.Thread(target=_fantastic_modified_loop, daemon=True)
        fj_modified_thread.start()
        _fj_modified_logger.info("Fantastic.jobs modified daemon registered (first run in ~30 minutes)")
    else:
        _fj_modified_logger.info("Fantastic.jobs modified daemon disabled via FJ_MODIFIED_DAEMON_ENABLED=false")

    # ---- Fantastic.jobs expired sweep daemon (every 24 hours) ───────────────
    #
    # Calls the Ultra+ Expired Jobs API (also doesn't burn Jobs credits) to
    # get yesterday's expired job IDs and marks the corresponding Firestore
    # docs `status="expired"`. Keeps the active job index honest without a
    # full re-fetch.
    _fj_expired_logger = logging.getLogger("fantastic_expired_daemon")

    def _fantastic_expired_loop():
        TWENTY_FOUR_HOURS = 86400
        _fj_expired_logger.info("Fantastic.jobs expired sweep daemon started (interval=24h)")
        time.sleep(1800)  # boot stabilization — 30 min (offset from modified)
        while True:
            try:
                with app.app_context():
                    from .pipeline.main import run_sweep_expired
                    run_sweep_expired()
            except Exception:
                _fj_expired_logger.exception("Fantastic.jobs expired sweep daemon failed; will retry next cycle")
            time.sleep(TWENTY_FOUR_HOURS)

    if os.getenv("FJ_EXPIRED_DAEMON_ENABLED", "true").lower() == "true":
        fj_expired_thread = threading.Thread(target=_fantastic_expired_loop, daemon=True)
        fj_expired_thread.start()
        _fj_expired_logger.info("Fantastic.jobs expired sweep daemon registered (first run in ~30 minutes)")
    else:
        _fj_expired_logger.info("Fantastic.jobs expired sweep daemon disabled via FJ_EXPIRED_DAEMON_ENABLED=false")

    return app

# Gunicorn entrypoint
app = create_app()

# Optional: list all registered routes when LIST_ROUTES=1 (e.g. LIST_ROUTES=1 python wsgi.py)
if os.environ.get("LIST_ROUTES"):
    print("--- Registered routes ---")
    for rule in app.url_map.iter_rules():
        print(f"{rule.endpoint}: {rule.methods} {rule.rule}")
    print("--- End routes ---")