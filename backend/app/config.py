"""
Application configuration - all constants, environment variables, and config dictionaries
"""
import os
from datetime import timedelta
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ========================================
# API Keys & Secrets
# ========================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
PEOPLE_DATA_LABS_API_KEY = os.getenv('PEOPLE_DATA_LABS_API_KEY')
STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY')
STRIPE_PUBLISHABLE_KEY = os.getenv('STRIPE_PUBLISHABLE_KEY')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
SERPAPI_KEY = os.getenv('SERPAPI_KEY')
JINA_API_KEY = os.getenv("JINA_API_KEY", "")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")  # Path to service account JSON
GOOGLE_SERVICE_ACCOUNT_EMAIL = os.getenv("GOOGLE_SERVICE_ACCOUNT_EMAIL")  # Service account email for domain-wide delegation
FLASK_SECRET = os.getenv("FLASK_SECRET", "dev")

# Browserbase — auto-apply browser runtime. Stealth + solveCaptchas defeat
# reCAPTCHA on Greenhouse/Lever/Ashby; per-tenant email-code gates are
# handled separately in the filler via Gmail API lookup.
BROWSERBASE_API_KEY = os.getenv("BROWSERBASE_API_KEY", "")
BROWSERBASE_PROJECT_ID = os.getenv("BROWSERBASE_PROJECT_ID", "")

# Provider-spend alerting (see app/services/spend_alerts.py).
# Budgets are USD; the spend monitor alerts at 50/80/100% of each. 0 = disabled.
# Alerts email SPEND_ALERT_EMAILS (default support@offerloop.ai → whole team) via
# the Resend pipeline; Telegram is an optional backup (only if TELEGRAM_CHAT_ID set).
# SPEND_CHECK_TOKEN guards the cron-triggered /api/admin/spend-check endpoint.
# All read at call-time via os.getenv in spend_alerts.py.
SPEND_DAILY_ALERT_USD = float(os.getenv("SPEND_DAILY_ALERT_USD") or 0)
SPEND_MONTHLY_ALERT_USD = float(os.getenv("SPEND_MONTHLY_ALERT_USD") or 0)
SPEND_ALERT_EMAILS = os.getenv("SPEND_ALERT_EMAILS", "support@offerloop.ai")
SPEND_CHECK_TOKEN = os.getenv("SPEND_CHECK_TOKEN", "")

# Gmail push notifications (Pub/Sub)
GOOGLE_CLOUD_PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT_ID", "offerloop-native")
GMAIL_PUBSUB_TOPIC = os.getenv("GMAIL_PUBSUB_TOPIC", "projects/offerloop-native/topics/gmail-notifications")
GMAIL_WEBHOOK_SECRET = os.getenv("GMAIL_WEBHOOK_SECRET", "")  # Random string for webhook verification

# ========================================
# Gmail OAuth Configuration
# ========================================
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.readonly", 
    "https://www.googleapis.com/auth/gmail.send",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile"
]

def get_oauth_redirect_uri():
    """Get the appropriate OAuth redirect URI based on environment"""
    env_uri = os.getenv("OAUTH_REDIRECT_URI")
    if env_uri:
        return env_uri
    # Auto-detect based on Flask environment
    is_production = os.getenv("FLASK_ENV") == "production" or os.getenv("RENDER")
    return (
        "https://offerloop.ai/api/google/oauth/callback"
        if is_production
        else "http://localhost:5001/api/google/oauth/callback"
    )

def get_frontend_redirect_uri():
    """Get the appropriate frontend redirect URI based on environment
    
    ⚠️ IMPORTANT: Returns base URL without query params.
    Query params like ?connected=gmail or ?gmail_error=wrong_account 
    are added by gmail_oauth.py callback handler.
    """
    is_production = os.getenv("FLASK_ENV") == "production" or os.getenv("RENDER")
    return (
        "https://offerloop.ai/signin"
        if is_production
        else "http://localhost:8080/signin"
    )

OAUTH_REDIRECT_URI = get_oauth_redirect_uri()

# Industry-aware semantic expansion for PDL prompts. When ON, prompts with
# industries but no specific company get their industry list broadened to
# PDL-canonical taxonomy siblings (and aligned title_variations added) via an
# LLM call (cached per parsed-prompt). Default OFF — flip to "true" once
# dogfooded; cost is ~$0.0001 per unique parse, free on cache hit.
ENABLE_INDUSTRY_EXPANSION = os.getenv('ENABLE_INDUSTRY_EXPANSION', 'false').lower() == 'true'

# ========================================
# Constants
# ========================================
RESUME_LINE = "For context, I've attached my resume below."
# Credit costs doubled 2026-06-10 as a pure marketing inflation: same dollar
# prices, same email outputs, but credit numbers feel more substantial.
# 10 cr = 1 email. Existing user balances are doubled via migrate_double_credits.py.
# Meeting prep is FREE as of 2026-07-07 (Rylan + cofounder call: the feature
# was underused behind the paywall). Monthly caps in TIER_CONFIGS stay as
# abuse valves — each prep still costs real provider money. Re-monetizing
# later is this one constant.
COFFEE_CHAT_CREDITS = 0
TIMELINE_CREDITS = 20
AUTO_APPLY_CREDITS = 5
SUPPORTED_AUTO_APPLY_ATS = {"greenhouse", "lever", "ashby"}
CACHE_DURATION = timedelta(days=365)
CREATE_GMAIL_DRAFTS = False  # Set True to create Gmail drafts; False to only return subject/body and compose links

# ========================================
# Feature Flags
# ========================================
PROMPT_SEARCH_ENABLED = os.getenv('PROMPT_SEARCH_ENABLED', 'false').lower() == 'true'  # Experimental prompt-first search

# Industry-aware semantic expansion for PDL prompts. When ON, prompts with
# industries but no specific company get their industry list broadened to
# PDL-canonical taxonomy siblings (and aligned title_variations added) via an
# LLM call (cached per parsed-prompt). Default OFF — flip to "true" once
# dogfooded; cost is ~$0.0001 per unique parse, free on cache hit.
ENABLE_INDUSTRY_EXPANSION = os.getenv('ENABLE_INDUSTRY_EXPANSION', 'false').lower() == 'true'

# ========================================
# Loops Notifications (PR2 scaffold)
# ========================================
# Master kill switch for all Loop alert emails. Defaults OFF so this PR
# ships as dead code at merge — PR3 wires the cycle pipeline caller.
LOOPS_ALERT_EMAILS_ENABLED = os.getenv("LOOPS_ALERT_EMAILS_ENABLED", "").lower() in ("1", "true", "yes")

# "Application submitted" receipt emailed to the user after a real auto-apply
# submit. Off by default; flip on per environment.
AUTO_APPLY_CONFIRMATION_EMAIL_ENABLED = os.getenv("AUTO_APPLY_CONFIRMATION_EMAIL_ENABLED", "").lower() in ("1", "true", "yes")

# Resend HTTP delivery (no Python SDK — raw HTTPS via `requests`).
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "loops@offerloop.ai")
# Used to verify the inbound bounce/complaint webhook signature.
RESEND_WEBHOOK_SECRET = os.getenv("RESEND_WEBHOOK_SECRET", "")

# Unsubscribe tokens are HMAC-signed with FLASK_SECRET and expire after N days.
LOOPS_UNSUBSCRIBE_TOKEN_TTL_DAYS = int(os.getenv("LOOPS_UNSUBSCRIBE_TOKEN_TTL_DAYS", "30"))

# ========================================
# Personalization Controlled Vocab
# ========================================
# Keep in sync with connect-grow-hire/src/lib/constants.ts

TARGET_INDUSTRIES = [
    "Investment Banking",
    "Management Consulting",
    "Private Equity / VC",
    "Technology",
    "Sales & Trading",
    "Corporate Finance / FP&A",
    "Data Science / Analytics",
    "Product Management",
    "Software Engineering",
    "Marketing",
    "Healthcare",
    "Real Estate",
    "Media & Entertainment",
    "Nonprofit / Public Sector",
    "Other",
]

TARGET_ROLE_TYPES = [
    "Internship",
    "Full-Time",
    "Part-Time",
    "Co-op",
    "Fellowship",
]

OPEN_TO_LOCATIONS = [
    "New York, NY",
    "San Francisco, CA",
    "Los Angeles, CA",
    "Chicago, IL",
    "Boston, MA",
    "Washington, DC",
    "Seattle, WA",
    "Dallas, TX",
    "Houston, TX",
    "Austin, TX",
    "Atlanta, GA",
    "Miami, FL",
    "Denver, CO",
    "Philadelphia, PA",
    "Charlotte, NC",
    "Nashville, TN",
    "Detroit, MI",
    "Minneapolis, MN",
    "San Diego, CA",
    "Portland, OR",
    "Remote",
    "Open to Relocation",
]


# ========================================
# Database Configuration
# ========================================
# Get absolute path to contacts.db in project root
_config_dir = os.path.dirname(os.path.abspath(__file__))  # backend/app/
_backend_dir = os.path.dirname(_config_dir)  # backend/
_project_root = os.path.dirname(_backend_dir)  # project root/
DB_PATH = os.path.join(_project_root, 'contacts.db')

# ========================================
# PDL Configuration
# ========================================
PDL_BASE_URL = 'https://api.peopledatalabs.com/v5'
pdl_cache = {}  # In-memory cache for PDL data

# ========================================
# Stripe Price IDs — legacy flat constants (kept for back-compat)
# ========================================
# These two are the original Pro/Elite monthly student SKUs in Stripe.
# New code should read from STRIPE_PRICE_CATALOG below instead.
STRIPE_PRO_PRICE_ID = os.getenv('STRIPE_PRO_PRICE_ID', 'price_1ScLXrERY2WrVHp1bYgdMAu4')
STRIPE_ELITE_PRICE_ID = os.getenv('STRIPE_ELITE_PRICE_ID', 'price_1ScLcfERY2WrVHp1c5rcONJ3')

# ========================================
# Stripe Price Catalog — env-driven SKU matrix (skeleton, cofounders wire)
# ========================================
# Keyed (tier, cadence, audience, credits) → price_id. New SKUs are placeholders
# until cofounders create them in the Stripe dashboard and populate env vars.
# Defaults reuse the two existing live Price IDs so checkout still works pre-cutover.
STRIPE_PRICE_CATALOG = {
    # Doubled 2026-06-10 to 10 cr = 1 email math. Pro slider 1K/2K/3K/4K cr
    # = 100/200/300/400 emails. Elite 3K/5K/7K cr = 300/500/700 emails.
    # The legacy live Price IDs are mapped to the new default stops (2K Pro,
    # 5K Elite) for back-compat — same $/month, same email count.
    'pro': {
        'monthly': {
            'student': {
                1000: os.getenv('STRIPE_PRO_MONTHLY_STUDENT_1K',  ''),
                2000: os.getenv('STRIPE_PRO_MONTHLY_STUDENT_2K',  STRIPE_PRO_PRICE_ID),
                3000: os.getenv('STRIPE_PRO_MONTHLY_STUDENT_3K',  ''),
                4000: os.getenv('STRIPE_PRO_MONTHLY_STUDENT_4K',  ''),
            },
            'list': {
                2000: os.getenv('STRIPE_PRO_MONTHLY_LIST_2K',     ''),
            },
        },
        'annual': {
            'student': {
                2000: os.getenv('STRIPE_PRO_ANNUAL_STUDENT_2K',   ''),
            },
            'list': {
                2000: os.getenv('STRIPE_PRO_ANNUAL_LIST_2K',      ''),
            },
        },
    },
    'elite': {
        'monthly': {
            'student': {
                3000: os.getenv('STRIPE_ELITE_MONTHLY_STUDENT_3K', ''),
                5000: os.getenv('STRIPE_ELITE_MONTHLY_STUDENT_5K', STRIPE_ELITE_PRICE_ID),
                7000: os.getenv('STRIPE_ELITE_MONTHLY_STUDENT_7K', ''),
            },
            'list': {
                5000: os.getenv('STRIPE_ELITE_MONTHLY_LIST_5K',    ''),
            },
        },
        'annual': {
            'student': {
                5000: os.getenv('STRIPE_ELITE_ANNUAL_STUDENT_5K',  ''),
            },
            'list': {
                5000: os.getenv('STRIPE_ELITE_ANNUAL_LIST_5K',     ''),
            },
        },
    },
    'season_pass': {
        'one_time': {
            'student': os.getenv('STRIPE_SEASON_PASS_STUDENT', ''),
            'list':    os.getenv('STRIPE_SEASON_PASS_LIST',    ''),
        },
    },
    'topup': {
        500:  os.getenv('STRIPE_TOPUP_500',  ''),
        1000: os.getenv('STRIPE_TOPUP_1000', ''),
        3000: os.getenv('STRIPE_TOPUP_3000', ''),
    },
}

# ========================================
# Stripe Coupons — lifecycle discount codes (skeleton)
# ========================================
# Cofounders create these in Stripe dashboard, then populate env.
# `active_promos` query reads these and the live state from Stripe API.
STRIPE_COUPONS = {
    'pricing_recapture': os.getenv('STRIPE_PRICING_RECAPTURE_COUPON_ID',   ''),  # 20% off Pro — STAYHIRED
    'winback':           os.getenv('STRIPE_WINBACK_COUPON_ID',             ''),  # 50% off Pro — WELCOMEBACK
    'checkout_recovery': os.getenv('STRIPE_CHECKOUT_RECOVERY_COUPON_ID',   ''),  # 20% off — WARMINTRO
}

# ========================================
# Credit slider stops — Higgsfield-style in-tier credit dial
# ========================================
# Each entry: (credits, student_price_dollars, list_price_dollars).
# Stop 2 (the second entry) is the default — what the page renders before drag.
SLIDER_STOPS = {
    # Doubled 2026-06-10 — 10 cr = 1 email. Same email counts as before
    # (Pro 100/200/300/400, Elite 300/500/700), inflated credit numbers.
    'pro': [
        {'credits': 1000, 'student': 9.99,  'list': 19.00},
        {'credits': 2000, 'student': 14.99, 'list': 29.00, 'default': True},
        {'credits': 3000, 'student': 19.99, 'list': 39.00},
        {'credits': 4000, 'student': 24.99, 'list': 49.00},
    ],
    'elite': [
        {'credits': 3000, 'student': 24.99, 'list': 44.00},
        {'credits': 5000, 'student': 34.99, 'list': 59.00, 'default': True},
        {'credits': 7000, 'student': 49.99, 'list': 84.00},
    ],
}

# Annual pricing math (~20% off monthly cadence = "2.5 months free").
# Applied uniformly to both student and list audiences so the annual discount
# is a real lever regardless of .edu status.
ANNUAL_PRICING = {
    'pro':   {'student': 144.00, 'list': 279.00},
    'elite': {'student': 336.00, 'list': 566.00},
}

# Season Pass — 4-month one-time. Doubled to 3,000 cr/mo = 300 emails/mo at
# the new 10 cr/email rate. Still sits cleanly between Pro (200) and Elite (500).
SEASON_PASS = {
    'student':       99.00,
    'list':          199.00,
    'months':        4,
    'credits_per_month': 3000,
    # New-only gate: don't show to existing paying users for first 60 days
    'new_users_only_until': os.getenv('SEASON_PASS_OPEN_DATE', '2026-08-09'),
}

# Top-up packs — one-time, no expiry on purchased credits. Doubled 2026-06-10
# along with everything else: 500/1000/3000 cr = 50/100/300 emails at the new
# 10 cr/email rate. Same dollar prices, same email outputs.
TOPUP_PACKS = [
    {'id': 'starter', 'credits': 500,  'price': 4.99,  'label': 'Starter'},
    {'id': 'best',    'credits': 1000, 'price': 9.99,  'label': 'Best value', 'recommended': True},
    {'id': 'bulk',    'credits': 3000, 'price': 24.99, 'label': 'Bulk'},
]

# ========================================
# In-app purchases (Apple, via RevenueCat)
# ========================================
# The iOS credit-pack ladder from SPEC-iap-v2-pricing-and-credits.md section 2:
# 150 cr / $2.99, 400 cr / $5.99, 1,000 cr / $9.99. These are DELIBERATELY
# stingier per dollar than the web TOPUP_PACKS above so "just subscribe" stays
# the obvious answer for anyone topping up twice — the 1,000 pack sits at the
# 2x-of-Pro floor and must not grow further (Rylan, 2026-07-28).
#
# NOTE the web/iOS divergence: $4.99 buys 500 credits on the web here (and
# 1,500 for $9.99 on the web's own main branch) while the app's cheapest pack is
# 150 for $2.99. Same wallet, different value depending on where you tapped buy.
# Open question for Nick, spec section 9.2 — not resolved by this code.
#
# Product ids are the source of truth for how much a purchase is worth, so an id
# we don't recognise grants NOTHING rather than guessing an amount. The .preview
# ids are the staging bundle's mirrors of the same three packs.
IAP_CREDIT_PACKS = {
    'ai.offerloop.app.credits.150':          150,
    'ai.offerloop.app.credits.400':          400,
    'ai.offerloop.app.credits.1000':         1000,
    'ai.offerloop.app.preview.credits.150':  150,
    'ai.offerloop.app.preview.credits.400':  400,
    'ai.offerloop.app.preview.credits.1000': 1000,
}

# RevenueCat entitlement id → our tier. Entitlements (not product ids) are what
# we key tier off, so Rylan can rename/add products in App Store Connect without
# a backend deploy. Anything unrecognised grants no tier.
IAP_ENTITLEMENT_TIERS = {
    'pro':   'pro',
    'elite': 'elite',
}

# Shared secret RevenueCat sends in the Authorization header. Fail-closed: with
# this unset the webhook 401s everything rather than becoming a credit faucet.
REVENUECAT_WEBHOOK_SECRET = os.getenv('REVENUECAT_WEBHOOK_SECRET', '')
# Secret API key (sk_…) for reading the authoritative subscriber snapshot back
# from RevenueCat instead of hand-deriving tier from the event type.
REVENUECAT_API_KEY = os.getenv('REVENUECAT_API_KEY', '')
# Which store environment this deploy owns. Staging processes SANDBOX events
# only; production processes PRODUCTION events only. Both RevenueCat apps can
# safely point at the same URL with this set correctly on each service.
REVENUECAT_EXPECT_PRODUCTION = os.getenv('REVENUECAT_EXPECT_PRODUCTION', '').lower() in ('1', 'true', 'yes')

# ========================================
# Trial configuration
# ========================================
# Trial duration — unified at 14 days for everyone. We dropped the .edu
# differentiation because it confused users; the .edu benefit is price, not
# trial length. Both env vars kept for API back-compat.
# Single-batch model (2026-06-15): one bounded grant over a SHORT window, no
# daily refill. Rationale: a daily drip rewarded waiting (don't convert, free
# credits keep coming), stacked on top of Pro, and gave away ~4x a paid month.
# A one-time grant flips the incentive — converting is always a gain, never a
# loss — and bounds give-away to a single batch. 7 days lets the find→send→reply
# loop close so the payoff lands before the window ends.
TRIAL_DAYS_STUDENT     = int(os.getenv('TRIAL_DAYS_STUDENT', '7'))
TRIAL_DAYS_NON_STUDENT = int(os.getenv('TRIAL_DAYS_NON_STUDENT', '7'))
# One-time credit grant for the whole trial (NOT per day). 600 cr = ~60 contacts
# at 10 cr each — enough to feel Pro (multiple searches + drafts + a coffee prep),
# bounded to ~0.3x a Pro month so it can't substitute for paying.
TRIAL_CREDITS          = int(os.getenv('TRIAL_CREDITS', '600'))
# Legacy: kept so any old reference resolves; the single-batch model no longer
# refills daily. TRIAL_CREDITS is the source of truth for the grant.
TRIAL_DAILY_CREDITS    = int(os.getenv('TRIAL_DAILY_CREDITS', '600'))
TRIAL_DAILY_EXPORT_CAP = int(os.getenv('TRIAL_DAILY_EXPORT_CAP', '25'))
# +7-day extension if user adds credit card at trial start (commitment hook)
TRIAL_CC_EXTENSION_DAYS = int(os.getenv('TRIAL_CC_EXTENSION_DAYS', '7'))
# Free-tier AI draft cap — dropped to 0 = unlimited within credit budget.
# The 500 cr monthly Free allocation naturally gates this (~33 emails @
# 15 cr/email). The old 5-draft cap conflicted with the email-targeting math.
# Kept the env var so cofounders can re-enable a cap later if needed.
FREE_DRAFTS_PER_MONTH  = int(os.getenv('FREE_DRAFTS_PER_MONTH', '0'))

# ========================================
# Credit Costs — canonical per-action price sheet
# ========================================
# Single source of truth. All routes should import CREDIT_COSTS and reference
# by key instead of redefining local constants. Per-contact metering for find
# actions is deliberate value-based pricing (Hiring Manager worth more than
# Employee) even though COGS is per-query (~$0.19 regardless of count).
# Healthy gross margins at tier caps (Pro 68%, Elite 85%) make this acceptable.
CREDIT_COSTS = {
    # Doubled 2026-06-10 — 10 cr = 1 email. Same $/email, same email counts,
    # just inflated credit numbers for marketing optics. Scout stays free.
    # Existing user balances doubled via migrate_double_credits.py.

    # Find actions — per contact returned.
    'find_contact':         10,  # default contact search (incl. verified email + AI draft)
    'find_hiring_manager':  10,  # Pro+ gated
    'find_recruiter':       6,
    'find_employee':        4,
    # A right swipe on the mobile People deck: unlock this one person's address
    # and write to them. Cheaper than find_contact because the search that found
    # them was already paid for (the deck was surfaced free, one PDL search for
    # twenty people) — this buys the verification and the written email, not a
    # fresh search. The app has shown 8 since the ranker deck shipped; this is
    # that number moved to where prices actually live.
    'reveal_person':        8,
    'firm_search':          10,  # per firm returned, Pro+ gated
    # iOS ranker swipe-right reveal-email. Cheaper than find_contact (10)
    # because the firm_cache path saves us the PDL cost; we still pay
    # Hunter Email Finder. Refunded when Hunter returns unverified /
    # below-confidence / no-hit (see routes/ranker.py:REVEAL_CHARGE_MIN_CONFIDENCE).
    'reveal_email':          8,

    # Generative actions — per output
    'coffee_chat_prep':     0,   # FREE (2026-07-07) — see COFFEE_CHAT_CREDITS
    'meeting_prep':         0,   # same as coffee_chat_prep — label-only rename
    'resume_optimization':  40,
    'cover_letter':         20,
    'timeline':             20,  # alias for back-compat with TIMELINE_CREDITS
    'reply_generation':     20,

    # Loop sub-actions
    'loop_contact_draft':   18,
    'loop_hm_draft':        26,
    'loop_job_found':       2,
    'loop_auto_send':       2,
    'loop_company_discovered': 2,

    # Scout chat — explicitly FREE (unchanged)
    'scout_chat':           0,
}

# ========================================
# Tier Configurations
# ========================================
TIER_CONFIGS = {
    'free': {
        'max_contacts': 10,  # App limit-model 2026-07-10 (staging only): free bumped 3→10
        'batch_size': 1,      # Maximum batch operations allowed (per audit spec)
        'min_contacts': 1,
        'fields': ['FirstName', 'LastName', 'LinkedIn', 'Email', 'Title', 'Company', 'City', 'State', 'College', 'Hometown'],
        'uses_pdl': True,
        'uses_email_drafting': True,
        'uses_resume': False,
        # Doubled 2026-06-10: 300 cr = 30 emails at 10 cr/email.
        'credits': 300,
        'time_saved_minutes': 300,
        'description': 'Try it out for free',
        'alumni_searches': 10,
        'coffee_chat_preps': 8,
        # Find contacts free, cap personalized drafts. Custom templates without
        # AI personalization remain unlimited.
        'ai_drafts_per_month': FREE_DRAFTS_PER_MONTH,
        'firm_search': False,
        'smart_filters': False,
        'bulk_drafting': False,
        'export_enabled': False,
        'priority_queue': False,
        'personalized_templates': False,
        'weekly_insights': False,
        'early_access': False,
        'agent_enabled': True,
        'max_loops': 1,
        # Per-Loop weekly credit budget (Phase 8 — automation/pricing)
        'default_credit_budget_per_week_per_loop': 75,
        'max_credit_budget_per_week_per_loop': 150,
        # Phase 9 — Loop auto-send. Free never auto-sends; the wizard's
        # "Send for me" mode is gated off entirely for Free.
        'max_auto_sends_per_day': 0,
    },
    'pro': {
        'max_contacts': 15,  # App limit-model 2026-07-10 (staging only): pro bumped 8→15
        'batch_size': 5,      # Maximum batch operations allowed (per audit spec)
        'min_contacts': 1, 
        'fields': ['FirstName', 'LastName', 'LinkedIn', 'Email', 'Title', 'Company', 'City', 'State', 'College',
                  'Phone', 'PersonalEmail', 'WorkEmail', 'SocialProfiles', 'EducationTop', 'VolunteerHistory',
                  'WorkSummary', 'Group', 'Hometown', 'Similarity'],
        'uses_pdl': True,
        'uses_email_drafting': True,
        'uses_resume': True,
        # Doubled 2026-06-10: 2,000 cr = 200 emails at 10 cr/email default.
        'credits': 2000,
        'time_saved_minutes': 2500,
        'description': 'Best for Students',
        'alumni_searches': 'unlimited',
        'coffee_chat_preps': 40,
        'firm_search': True,
        'smart_filters': True,
        'bulk_drafting': True,
        'export_enabled': True,
        'priority_queue': False,
        'personalized_templates': False,
        'weekly_insights': False,
        'early_access': False,
        'agent_enabled': True,
        'max_loops': 5,
        # Per-Loop weekly credit budget (Phase 8 — automation/pricing)
        'default_credit_budget_per_week_per_loop': 200,
        'max_credit_budget_per_week_per_loop': 600,
        # Phase 9 — Loop auto-send. Conservative ceiling; Gmail's free-account
        # soft throttle is ~500/day but 25 cold sends/day from a personal
        # student inbox is already aggressive.
        'max_auto_sends_per_day': 25,
    },
    'elite': {
        'max_contacts': 30,  # Maximum contacts returned per search
        'batch_size': 30,     # Maximum batch operations allowed (per audit spec)
        'min_contacts': 1,
        'fields': ['FirstName', 'LastName', 'LinkedIn', 'Email', 'Title', 'Company', 'City', 'State', 'College',
                  'Phone', 'PersonalEmail', 'WorkEmail', 'SocialProfiles', 'EducationTop', 'VolunteerHistory',
                  'WorkSummary', 'Group', 'Hometown', 'Similarity'],
        'uses_pdl': True,
        'uses_email_drafting': True,
        'uses_resume': True,
        # Doubled 2026-06-10: 5,000 cr = 500 emails at 10 cr/email default.
        # See SLIDER_STOPS['elite'] for in-tier slider variants (3K/5K/7K).
        'credits': 5000,
        'time_saved_minutes': 3500,
        'description': 'For serious recruiting season',
        'alumni_searches': 'unlimited',
        'coffee_chat_preps': 'unlimited',
        'firm_search': True,
        'smart_filters': True,
        'bulk_drafting': True,
        'export_enabled': True,
        'priority_queue': True,
        'personalized_templates': True,
        'weekly_insights': True,
        'early_access': True,
        'agent_enabled': True,
        # Elite is "unlimited" in copy; capped at 50 internally so a single
        # account can't accidentally spin up hundreds of Loops + cycles.
        'max_loops': 50,
        # Per-Loop weekly credit budget. Elite max is None = bounded only by
        # the monthly pool, so a power user can dial individual Loops up.
        'default_credit_budget_per_week_per_loop': 400,
        'max_credit_budget_per_week_per_loop': None,
        # Phase 9 — Loop auto-send. Power-user ceiling. Per-Loop override
        # available via hardDailySendCap.
        'max_auto_sends_per_day': 75,
    },
    # Season Pass — 4-month one-time pre-paid pass. Treated as a tier for the
    # duration of its active window so feature gates light up identically to Pro.
    'season_pass': {
        'max_contacts': 8,
        'batch_size': 5,
        'min_contacts': 1,
        'fields': TARGET_INDUSTRIES and None,  # populated below
        'uses_pdl': True,
        'uses_email_drafting': True,
        'uses_resume': True,
        'credits': SEASON_PASS['credits_per_month'],
        'time_saved_minutes': 2500,
        'description': 'Recruiting season — 4 months, one charge',
        'alumni_searches': 'unlimited',
        'coffee_chat_preps': 40,
        'firm_search': True,
        'smart_filters': True,
        'bulk_drafting': True,
        'export_enabled': True,
        'priority_queue': False,
        'personalized_templates': False,
        'weekly_insights': False,
        'early_access': False,
        'agent_enabled': True,
        'max_loops': 5,
        'default_credit_budget_per_week_per_loop': 200,
        'max_credit_budget_per_week_per_loop': 600,
    },
}
# Populate Season Pass fields from Pro's field list (avoids forward-ref order)
TIER_CONFIGS['season_pass']['fields'] = TIER_CONFIGS['pro']['fields']

# ========================================
# PDL Metro Areas
# ========================================
PDL_METRO_AREAS = {
    'san francisco': 'san francisco, california',
    'san francisco bay area': 'san francisco, california',
    'bay area': 'san francisco, california',
    'sf': 'san francisco, california',
    'los angeles': 'los angeles, california',
    'la': 'los angeles, california',
    'new york': 'new york, new york',
    'new york city': 'new york, new york',
    'nyc': 'new york, new york',
    'chicago': 'chicago, illinois',
    'boston': 'boston, massachusetts',
    'washington dc': 'washington, district of columbia',
    'dc': 'washington, district of columbia',
    'seattle': 'seattle, washington',
    'atlanta': 'atlanta, georgia',
    'dallas': 'dallas, texas',
    'houston': 'houston, texas',
    'miami': 'miami, florida',
    'denver': 'denver, colorado',
    'phoenix': 'phoenix, arizona',
    'philadelphia': 'philadelphia, pennsylvania',
    'detroit': 'detroit, michigan',
    'minneapolis': 'minneapolis, minnesota',
    'austin': 'austin, texas',
    'san diego': 'san diego, california',
    'portland': 'portland, oregon',
    'orlando': 'orlando, florida',
    'tampa': 'tampa, florida',
    'nashville': 'nashville, tennessee',
    'charlotte': 'charlotte, north carolina',
    'pittsburgh': 'pittsburgh, pennsylvania',
    'cleveland': 'cleveland, ohio',
    'cincinnati': 'cincinnati, ohio',
    'columbus': 'columbus, ohio',
    'indianapolis': 'indianapolis, indiana',
    'milwaukee': 'milwaukee, wisconsin',
    'kansas city': 'kansas city, missouri',
    'sacramento': 'sacramento, california',
    'las vegas': 'las vegas, nevada',
    'salt lake city': 'salt lake city, utah',
    'raleigh': 'raleigh, north carolina',
    'richmond': 'richmond, virginia',
    'birmingham': 'birmingham, alabama',
    'memphis': 'memphis, tennessee',
    'louisville': 'louisville, kentucky',
    'jacksonville': 'jacksonville, florida',
    'oklahoma city': 'oklahoma city, oklahoma',
    'buffalo': 'buffalo, new york',
    'rochester': 'rochester, new york',
    'albany': 'albany, new york',
    'hartford': 'hartford, connecticut',
    'providence': 'providence, rhode island'
}

# ========================================
# Validation
# ========================================
if not PEOPLE_DATA_LABS_API_KEY:
    print("WARNING: PEOPLE_DATA_LABS_API_KEY not found in .env file")

if not OPENAI_API_KEY:
    print("WARNING: OPENAI_API_KEY not found in .env file")

if not CLAUDE_API_KEY:
    print("WARNING: CLAUDE_API_KEY not found in .env file")
else:
    print("✓ Claude API key loaded")

if STRIPE_SECRET_KEY:
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    print("✓ Stripe initialized successfully")
else:
    print("WARNING: STRIPE_SECRET_KEY not found in .env file")

# OAuth insecure transport for localhost
if (os.environ.get("OAUTH_REDIRECT_URI") or "").startswith("http://localhost"):
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"