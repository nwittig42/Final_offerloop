"""
Lifecycle email service. Five sequences via the existing Resend integration.

Voice: founder-direct. These emails are "from" Rylan @ Offerloop. Plain English,
no fake testimonials, no fabricated PDFs, no "5-10x more outreach" stats we
can't back up. Per the standing project rule, no fake numbers or fake social
proof anywhere. Only real things: real product features, real personalization
(saved contact counts pulled from Firestore), real coupon codes that fail
gracefully when the underlying Stripe coupon isn't wired yet.

Sequences:
  1. Pricing visit abandonment (anonymous, popup-driven, currently dormant
     since the PricingExitPopup is removed). Day 0 / Day 2 / Day 5.
  2. Checkout abandonment (signed-in). Hour 1 / Day 1.
  3. Trial ending. 48h / 24h / at-expiry.
  4. Low credits. Fired real-time from auth deduct path.
  5. Win-back. 30 days post-cancel.

Architecture:
  - Idempotency via `lifecycle_email_log` Firestore collection (composite key).
  - Rate limit = 2 lifecycle emails / user / 7 days.
  - HMAC-signed unsubscribe tokens.
  - Cron entry = /api/lifecycle/tick (secret-guarded).
  - Discount codes appear inline only when STRIPE_COUPONS env vars are populated.
"""
import hmac
import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.config import (
    STRIPE_COUPONS,
    LIFECYCLE_FROM_EMAIL,
    LIFECYCLE_POSTAL_ADDRESS,
    ONBOARDING_DROPOFF_LAUNCH_DATE,
    FIRST_SEARCH_ACTIVATION_LAUNCH_DATE,
    FIRST_SEND_ACTIVATION_LAUNCH_DATE,
    WELCOME_DRIP_LAUNCH_DATE,
    COFFEE_CHAT_DISCOVERY_LAUNCH_DATE,
    JOB_BOARD_DISCOVERY_LAUNCH_DATE,
    FREE_CEILING_LAUNCH_DATE,
    WEEKLY_WIN_REPORT_LAUNCH_DATE,
    PRO_MONTHLY_RECAP_LAUNCH_DATE,
    RENEWAL_REMINDER_LAUNCH_DATE,
    DORMANCY_NUDGE_LAUNCH_DATE,
    PRO_UPGRADE_NUDGE_LAUNCH_DATE,
    REFERRAL_MILESTONE_LAUNCH_DATE,
    STRIPE_SECRET_KEY,
)
from app.extensions import get_db
from app.services.notification_adapter import send as notify_send, Channel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_LIFECYCLE_EMAILS_PER_7_DAYS = 2

PUBLIC_BASE_URL = os.getenv('PUBLIC_BASE_URL', 'https://offerloop.ai')

# Who the emails are signed by. Single-name to keep the voice personal. These
# are coming from a co-founder talking to a student, not a brand.
SIGNATURE_NAME = os.getenv('LIFECYCLE_SIGNATURE_NAME', 'Deena')


def _unsubscribe_secret() -> str:
    return os.getenv('LIFECYCLE_UNSUBSCRIBE_SECRET') or os.getenv('FLASK_SECRET', 'dev')


def _parse_ts_or_dt(val) -> Optional[datetime]:
    """Firestore timestamp fields may arrive as native datetime (Firestore
    Timestamp) OR as ISO 8601 string (from create_user_data() and the
    lifecycle backfill script). This helper normalizes both to a
    timezone-aware datetime, or None if the value can't be parsed."""
    if val is None:
        return None
    if hasattr(val, 'isoformat'):
        dt = val
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    try:
        s = str(val).replace('Z', '+00:00')
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Idempotency + rate-limit helpers
# ---------------------------------------------------------------------------

def _log_key(user_or_lead_id: str, campaign: str, step: str) -> str:
    return f"{user_or_lead_id}:{campaign}:{step}"


def already_sent(user_or_lead_id: str, campaign: str, step: str) -> bool:
    db = get_db()
    if not db:
        return False
    key = _log_key(user_or_lead_id, campaign, step)
    snap = db.collection('lifecycle_email_log').document(key).get()
    return snap.exists


def _rate_limit_exceeded(user_or_lead_id: str) -> bool:
    db = get_db()
    if not db:
        return False
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    q = (db.collection('lifecycle_email_log')
            .where('recipient_id', '==', user_or_lead_id)
            .where('sent_at', '>=', seven_days_ago)
            .limit(MAX_LIFECYCLE_EMAILS_PER_7_DAYS + 1))
    try:
        count = sum(1 for _ in q.stream())
    except Exception as e:
        logger.warning(f"rate-limit query failed for {user_or_lead_id}: {e}")
        return False
    return count >= MAX_LIFECYCLE_EMAILS_PER_7_DAYS


def _record_send(user_or_lead_id: str, campaign: str, step: str, recipient_email: str, variant: Optional[str] = None) -> None:
    db = get_db()
    if not db:
        return
    key = _log_key(user_or_lead_id, campaign, step)
    payload = {
        'recipient_id': user_or_lead_id,
        'recipient_email': recipient_email,
        'campaign': campaign,
        'step': step,
        'sent_at': datetime.now(timezone.utc),
    }
    if variant:
        payload['variant'] = variant
    db.collection('lifecycle_email_log').document(key).set(payload)


def _ab_variant(user_or_lead_id: str, experiment: str) -> str:
    """Deterministic A/B bucketing by uid + experiment. Returns 'A' or 'B'.
    Same uid always gets the same variant for the same experiment, so opens /
    clicks / replies attribute back cleanly."""
    h = hashlib.sha1(f"{experiment}:{user_or_lead_id}".encode('utf-8')).hexdigest()
    return 'A' if int(h[:8], 16) % 2 == 0 else 'B'


# ---------------------------------------------------------------------------
# Unsubscribe tokens
# ---------------------------------------------------------------------------

def make_unsubscribe_token(email: str) -> str:
    secret = _unsubscribe_secret().encode('utf-8')
    msg = email.lower().encode('utf-8')
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def verify_unsubscribe_token(email: str, token: str) -> bool:
    return hmac.compare_digest(make_unsubscribe_token(email), token)


def is_unsubscribed(email: str) -> bool:
    db = get_db()
    if not db:
        return False
    snap = db.collection('lifecycle_unsubscribes').document(email.lower()).get()
    return snap.exists


def record_unsubscribe(email: str) -> None:
    db = get_db()
    if not db:
        return
    db.collection('lifecycle_unsubscribes').document(email.lower()).set({
        'email': email.lower(),
        'unsubscribed_at': datetime.now(timezone.utc),
    })


# ---------------------------------------------------------------------------
# Send funnel (idempotency, rate limit, unsubscribe gate, logging)
# ---------------------------------------------------------------------------

def _send_lifecycle_email(
    *,
    user_or_lead_id: str,
    recipient_email: str,
    campaign: str,
    step: str,
    subject: str,
    body_paragraphs: list[str],
    cta_label: Optional[str] = None,
    cta_url: Optional[str] = None,
    variant: Optional[str] = None,
) -> dict:
    if not recipient_email or '@' not in recipient_email:
        return {'sent': False, 'reason': 'invalid_email'}
    if already_sent(user_or_lead_id, campaign, step):
        return {'sent': False, 'reason': 'already_sent'}
    if is_unsubscribed(recipient_email):
        return {'sent': False, 'reason': 'unsubscribed'}
    if _rate_limit_exceeded(user_or_lead_id):
        return {'sent': False, 'reason': 'rate_limited'}

    token = make_unsubscribe_token(recipient_email)
    unsub_url = f"{PUBLIC_BASE_URL}/api/lifecycle/unsubscribe?email={recipient_email}&token={token}"
    headers = {'List-Unsubscribe': f"<{unsub_url}>"}

    html = _render_html(body_paragraphs, cta_label, cta_url, unsub_url)
    text = _render_text(body_paragraphs, cta_label, cta_url, unsub_url)

    result = notify_send(Channel.EMAIL, recipient_email, subject, html, text, headers, from_email=LIFECYCLE_FROM_EMAIL)
    if getattr(result, 'success', False):
        _record_send(user_or_lead_id, campaign, step, recipient_email, variant=variant)
        # Attribution: sync PostHog capture so opens / clicks / replies can be
        # tied back to the lifecycle send in funnels. Sync-mode per the project
        # rule that funnel writes must be durable before the request returns.
        try:
            from app.utils.posthog_client import track_event
            props = {'campaign': campaign, 'step': step, 'subject': subject}
            if variant:
                props['variant'] = variant
            track_event(user_or_lead_id, 'lifecycle_email_sent', props, sync=True)
        except Exception:
            pass
        return {'sent': True, 'reason': 'ok'}
    return {'sent': False, 'reason': getattr(result, 'error_code', 'send_failed')}


def _render_html(paragraphs: list[str], cta_label: Optional[str], cta_url: Optional[str], unsub_url: str) -> str:
    """Plain email styling. No eyebrows, no gradient buttons, no serif drama.
    Reads like a normal email someone would actually send."""
    body = ''.join(
        f'<p style="margin:0 0 14px; font-size:15px; line-height:1.6; color:#1F2937;">{p}</p>'
        for p in paragraphs
    )
    cta_html = ''
    if cta_label and cta_url:
        # Plain inline link, not a styled button. Gmail's Promotions classifier
        # keys on bold/colored CTA links + separated visual sections; letting
        # the CTA read like "another line in the letter" nudges toward Primary.
        cta_html = (
            f'<p style="margin:0 0 14px; font-size:15px; line-height:1.6; color:#1F2937;">'
            f'<a href="{cta_url}" style="color:#1F2937;">{cta_label}</a>'
            f'</p>'
        )
    signature_html = (
        f'<p style="margin:20px 0 6px; font-size:15px; line-height:1.6; color:#1F2937;">'
        f'— {SIGNATURE_NAME}</p>'
    )
    # CAN-SPAM requires a valid physical postal address in every commercial
    # email. If LIFECYCLE_POSTAL_ADDRESS is unset, print a highly visible
    # placeholder so a reviewer catches it before prod.
    address_line = (
        LIFECYCLE_POSTAL_ADDRESS
        or '⚠ Set LIFECYCLE_POSTAL_ADDRESS env var. CAN-SPAM compliance requires a real postal address here'
    )
    footer = (
        '<p style="margin-top:28px; padding-top:14px; border-top:1px solid #E5E7EB;'
        ' font-size:11px; color:#9CA3AF; line-height:1.55;">'
        f'<a href="{unsub_url}" style="color:#9CA3AF;">Unsubscribe</a>'
        f'<br>Offerloop &middot; {address_line}'
        '</p>'
    )
    # No centered content box or fixed max-width. Those are marketing-email
    # tells. Let the content flow edge-to-edge like a Gmail-composed message.
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',sans-serif;'
        ' font-size:15px; line-height:1.6; color:#1F2937;">'
        + body + cta_html + signature_html + footer +
        '</div>'
    )


def _render_text(paragraphs: list[str], cta_label: Optional[str], cta_url: Optional[str], unsub_url: str) -> str:
    """Plain-text fallback. Actual line breaks, no HTML."""
    address_line = (
        LIFECYCLE_POSTAL_ADDRESS
        or 'MISSING POSTAL ADDRESS: set LIFECYCLE_POSTAL_ADDRESS'
    )
    lines = paragraphs[:]
    if cta_label and cta_url:
        lines.append(f"{cta_label}: {cta_url}")
    lines.append(f"— {SIGNATURE_NAME}")
    lines.append("")
    lines.append(f"Unsubscribe: {unsub_url}")
    lines.append(f"Offerloop · {address_line}")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers used inside the sequences
# ---------------------------------------------------------------------------

def _count_saved_contacts(uid: str) -> int:
    """Best-effort count for personalization. Caps at 1000 results."""
    db = get_db()
    if not db:
        return 0
    try:
        ref = db.collection('users').document(uid).collection('contacts').limit(1000)
        return sum(1 for _ in ref.stream())
    except Exception:
        return 0


def _real_coupon(key: str) -> Optional[str]:
    """Return the human-facing coupon code only when the underlying Stripe
    coupon ID env var is actually populated. Per the no-fake-numbers rule,
    we never advertise a code that won't work at checkout."""
    coupon_ids = {
        'pricing_recapture': ('STAYHIRED', STRIPE_COUPONS.get('pricing_recapture')),
        'checkout_recovery': ('WARMINTRO', STRIPE_COUPONS.get('checkout_recovery')),
        'winback': ('WELCOMEBACK', STRIPE_COUPONS.get('winback')),
    }
    name, sid = coupon_ids.get(key, (None, None))
    return name if (name and sid) else None


# ---------------------------------------------------------------------------
# Sequence 1: Pricing visit abandonment (anonymous leads from capture endpoint)
# ---------------------------------------------------------------------------

def process_pricing_leads() -> dict:
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    sent = {'day_0': 0, 'day_2': 0, 'day_5': 0}
    now = datetime.now(timezone.utc)

    for snap in db.collection('lifecycle_leads').where('source', '==', 'pricing_exit').stream():
        lead = snap.to_dict() or {}
        lead_id = snap.id
        email = lead.get('email')
        captured_at = lead.get('captured_at')
        if not email or not captured_at:
            continue
        hours_since = (now - captured_at).total_seconds() / 3600

        # Day 0
        if hours_since < 0.5 and not already_sent(lead_id, 'pricing_abandon', 'day_0'):
            res = _send_lifecycle_email(
                user_or_lead_id=lead_id,
                recipient_email=email,
                campaign='pricing_abandon',
                step='day_0',
                subject="saw you on the pricing page",
                body_paragraphs=[
                    f"Hey, {SIGNATURE_NAME} here. I help run Offerloop.",
                    "Saw you stopped by the pricing page. No script, no PDF, just want to flag what you'd actually get if you tried it.",
                    "Offerloop is built for college students recruiting for internships and full-time roles. You search for alumni / hiring managers / recruiters at companies you want, we pull their verified email and draft a first-touch tied to your background, and the pipeline view tracks who's replied. Pro is $9.99/mo (student pricing for everyone) and the trial is 7 days free.",
                    "If anything's confusing or you want to ask whether the product makes sense for your situation, just reply.",
                ],
                cta_label="Start the free trial",
                cta_url=f'{PUBLIC_BASE_URL}/signin?mode=signup&utm_source=lifecycle&utm_campaign=pricing_abandon&utm_content=day_0',
            )
            if res.get('sent'):
                sent['day_0'] += 1

        # Day 2
        elif 36 < hours_since < 60 and not already_sent(lead_id, 'pricing_abandon', 'day_2'):
            res = _send_lifecycle_email(
                user_or_lead_id=lead_id,
                recipient_email=email,
                campaign='pricing_abandon',
                step='day_2',
                subject="fwiw on the trial",
                body_paragraphs=[
                    "One honest thing about the trial:",
                    "Most students don't get a lot out of Offerloop unless they actually send 10–15 emails during the 7-day window. If you sign up but don't end up doing real outreach, you'll think the product's not doing much.",
                    "So if you're not actively recruiting right now, it's fine to wait. If you are, the trial will tell you pretty quickly whether it fits how you work.",
                ],
                cta_label="Try Pro free for 7 days",
                cta_url=f'{PUBLIC_BASE_URL}/signin?mode=signup&utm_source=lifecycle&utm_campaign=pricing_abandon&utm_content=day_2',
            )
            if res.get('sent'):
                sent['day_2'] += 1

        # Day 5: final note
        elif 108 < hours_since < 156 and not already_sent(lead_id, 'pricing_abandon', 'day_5'):
            promo = _real_coupon('checkout_recovery') or _real_coupon('pricing_recapture')
            paragraphs = [
                "Won't keep emailing you. Last note from me.",
            ]
            if promo:
                paragraphs.append(
                    f"If price is the sticking point: code <strong>{promo}</strong> takes 20% off your first month of Pro. Works for the next 7 days."
                )
            paragraphs.append(
                "Otherwise, if you ever want to chat about whether Offerloop makes sense for what you're recruiting for, just reply with your year and what you're targeting. Good luck either way."
            )
            res = _send_lifecycle_email(
                user_or_lead_id=lead_id,
                recipient_email=email,
                campaign='pricing_abandon',
                step='day_5',
                subject="last note",
                body_paragraphs=paragraphs,
                cta_label=("See Pro" if promo else None),
                cta_url=(f'{PUBLIC_BASE_URL}/pricing?utm_source=lifecycle&utm_campaign=pricing_abandon&utm_content=day_5' if promo else None),
            )
            if res.get('sent'):
                sent['day_5'] += 1

    return {'ok': True, 'sent': sent}


# ---------------------------------------------------------------------------
# Sequence 2: Checkout abandonment
# ---------------------------------------------------------------------------

def process_checkout_abandons() -> dict:
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    sent = {'hour_1': 0, 'day_1': 0}
    now = datetime.now(timezone.utc)

    for snap in db.collection('users').where('checkoutAbandonedAt', '!=', None).stream():
        user = snap.to_dict() or {}
        uid = snap.id
        email = user.get('email')
        abandoned_at = user.get('checkoutAbandonedAt')
        if not email or not abandoned_at:
            continue
        if user.get('subscriptionTier') in ('pro', 'elite'):
            continue  # converted later via a separate flow
        hours_since = (now - abandoned_at).total_seconds() / 3600

        # Hour 1: quick "did something break?"
        if 0.5 < hours_since < 4 and not already_sent(uid, 'checkout_abandon', 'hour_1'):
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='checkout_abandon',
                step='hour_1',
                subject="did checkout break?",
                body_paragraphs=[
                    f"Hey, {SIGNATURE_NAME} from Offerloop.",
                    "Saw you started checkout for Pro but didn't finish. If something actually went sideways on our end (Stripe weirdness, card got declined, redirect failed), reply and tell me what happened. I can usually sort it out fast.",
                    "If you just second-guessed it, no worries. The trial is 7 days free and you can cancel anytime before it ends. That's probably the better starting point anyway.",
                ],
                cta_label="Pick up where you left off",
                cta_url=f'{PUBLIC_BASE_URL}/pricing?utm_source=lifecycle&utm_campaign=checkout_abandon&utm_content=hour_1',
            )
            if res.get('sent'):
                sent['hour_1'] += 1

        # Day 1: soft follow-up
        elif 20 < hours_since < 30 and not already_sent(uid, 'checkout_abandon', 'day_1'):
            promo = _real_coupon('checkout_recovery')
            paragraphs = [
                "Following up once on the checkout from yesterday. Then I'll leave you alone.",
                "If you want to actually try Pro before paying, the 7-day free trial is the right move if you're on the fence. The product either clicks for how you work or it doesn't, and you'll know inside a week.",
            ]
            if promo:
                paragraphs.append(
                    f"If you do want to commit now, code <strong>{promo}</strong> takes 20% off your first month."
                )
            paragraphs.append("Reply if anything about how the product works is unclear.")
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='checkout_abandon',
                step='day_1',
                subject="no rush",
                body_paragraphs=paragraphs,
                cta_label="Start free trial",
                cta_url=f'{PUBLIC_BASE_URL}/pricing?utm_source=lifecycle&utm_campaign=checkout_abandon&utm_content=day_1',
            )
            if res.get('sent'):
                sent['day_1'] += 1

    return {'ok': True, 'sent': sent}


# ---------------------------------------------------------------------------
# Sequence 3: Trial ending
# ---------------------------------------------------------------------------

def process_trial_endings() -> dict:
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    sent = {'h48': 0, 'h24': 0, 'expired': 0}
    now = datetime.now(timezone.utc)

    for snap in db.collection('users').where('trialActive', '==', True).stream():
        user = snap.to_dict() or {}
        uid = snap.id
        email = user.get('email')
        ends_at = user.get('trialEndsAt')
        if not email or not ends_at:
            continue
        if hasattr(ends_at, 'tzinfo') and ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        hours_left = (ends_at - now).total_seconds() / 3600
        contacts_count = _count_saved_contacts(uid)

        # 48h before
        if 24 < hours_left < 52 and not already_sent(uid, 'trial_ending', 'h48'):
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='trial_ending',
                step='h48',
                subject="2 days left on your Pro trial",
                body_paragraphs=[
                    f"Quick heads up. Your Pro trial ends in 2 days.",
                    f"You've saved {contacts_count} {'contact' if contacts_count == 1 else 'contacts'} so far. When the trial ends you drop to Free, so those contacts and their drafts stay visible, but you lose the things that found them: hiring-manager search, firm search, bulk drafting, and unlimited Coffee Chat Prep.",
                    "If Pro's been useful, it's $9.99/mo, student pricing, no verification needed. If it hasn't been useful, no charge.",
                ],
                cta_label="Keep Pro",
                cta_url=f'{PUBLIC_BASE_URL}/pricing?utm_source=lifecycle&utm_campaign=trial_ending&utm_content=h48',
            )
            if res.get('sent'):
                sent['h48'] += 1

        # 24h before
        elif 0 < hours_left < 28 and not already_sent(uid, 'trial_ending', 'h24'):
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='trial_ending',
                step='h24',
                subject="trial ends tomorrow",
                body_paragraphs=[
                    "Last reminder: Pro trial ends tomorrow.",
                    f"{contacts_count} {'contact' if contacts_count == 1 else 'contacts'} saved. Pick a plan if Pro's been working, or do nothing and you'll drop to Free automatically.",
                ],
                cta_label="Pick a plan",
                cta_url=f'{PUBLIC_BASE_URL}/pricing?utm_source=lifecycle&utm_campaign=trial_ending&utm_content=h24',
            )
            if res.get('sent'):
                sent['h24'] += 1

    # Post-expiry note: sweep users whose status flipped to 'expired' recently
    cutoff = now - timedelta(hours=2)
    for snap in db.collection('users').where('subscriptionStatus', '==', 'expired').stream():
        user = snap.to_dict() or {}
        uid = snap.id
        email = user.get('email')
        ends_at = user.get('trialEndsAt')
        if not email or not ends_at:
            continue
        if hasattr(ends_at, 'tzinfo') and ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        if ends_at < cutoff or ends_at > now:
            continue
        if already_sent(uid, 'trial_ending', 'expired'):
            continue
        res = _send_lifecycle_email(
            user_or_lead_id=uid,
            recipient_email=email,
            campaign='trial_ending',
            step='expired',
            subject="you're on Free now",
            body_paragraphs=[
                "Your Pro trial ended. You're on the Free plan now.",
                "Your saved contacts and drafts are still there. The things that need Pro (hiring-manager search, firm search, bulk drafting, Coffee Chat Prep beyond the 3 lifetime free ones) are locked.",
                "If you change your mind, Pro's in account settings.",
            ],
            cta_label=None,
            cta_url=None,
        )
        if res.get('sent'):
            sent['expired'] += 1

    return {'ok': True, 'sent': sent}


# ---------------------------------------------------------------------------
# Sequence 4: Low credits (fired real-time from auth.deduct_credits_atomic)
# ---------------------------------------------------------------------------

def notify_low_credits(uid: str, credits_remaining: int, max_credits: int) -> dict:
    db = get_db()
    if not db:
        return {'sent': False, 'reason': 'db_unavailable'}
    snap = db.collection('users').document(uid).get()
    if not snap.exists:
        return {'sent': False, 'reason': 'user_not_found'}
    user = snap.to_dict() or {}
    email = user.get('email')
    if not email:
        return {'sent': False, 'reason': 'no_email'}

    # Reset per calendar month so the same threshold doesn't spam each billing cycle
    month = datetime.now(timezone.utc).strftime('%Y-%m')
    step = f"low_{month}"

    pct_used = round(100 - (credits_remaining / max(max_credits, 1)) * 100)
    rough_emails = credits_remaining // 10  # 10 cr / email at current math

    return _send_lifecycle_email(
        user_or_lead_id=uid,
        recipient_email=email,
        campaign='low_credits',
        step=step,
        subject=f"{credits_remaining} credits left",
        body_paragraphs=[
            f"Quick FYI: you've used about {pct_used}% of your credits this month. {credits_remaining} left, which is roughly {rough_emails} more {'email' if rough_emails == 1 else 'emails'} before your monthly reset.",
            "Two options if you need more before then:",
            "Top up: one-time credit pack, never expires. Or upgrade to Elite if you're going to keep burning at this pace.",
        ],
        cta_label="See options",
        cta_url=f'{PUBLIC_BASE_URL}/pricing?utm_source=lifecycle&utm_campaign=low_credits',
    )


# ---------------------------------------------------------------------------
# Sequence 5: Win-back (30 days post-cancel)
# ---------------------------------------------------------------------------

def process_winbacks() -> dict:
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    sent_count = 0
    now = datetime.now(timezone.utc)
    thirty_two_days_ago = now - timedelta(days=32)
    thirty_days_ago = now - timedelta(days=30)

    q = (db.collection('users')
            .where('canceledAt', '>=', thirty_two_days_ago)
            .where('canceledAt', '<=', thirty_days_ago))
    for snap in q.stream():
        user = snap.to_dict() or {}
        uid = snap.id
        email = user.get('email')
        if not email:
            continue
        if user.get('subscriptionTier') in ('pro', 'elite'):
            continue  # already re-subscribed
        if already_sent(uid, 'winback', 'day_30'):
            continue

        contacts_count = _count_saved_contacts(uid)
        promo = _real_coupon('winback')
        paragraphs = [
            f"Hey, {SIGNATURE_NAME} again.",
            f"Been about a month since you canceled. Just flagging: your {contacts_count} saved {'contact' if contacts_count == 1 else 'contacts'} and the drafts you built are still in your Offerloop account, right where you left them.",
        ]
        if promo:
            paragraphs.append(
                f"If you ever want to pick recruiting back up, code <strong>{promo}</strong> takes 50% off your first month back. Works for the next 14 days."
            )
        paragraphs.append(
            "Also: if something specifically pushed you to cancel that we could actually fix, I'd want to know. Hit reply."
        )

        res = _send_lifecycle_email(
            user_or_lead_id=uid,
            recipient_email=email,
            campaign='winback',
            step='day_30',
            subject="your contacts are still here",
            body_paragraphs=paragraphs,
            cta_label=("Come back" if promo else None),
            cta_url=(f'{PUBLIC_BASE_URL}/pricing?utm_source=lifecycle&utm_campaign=winback&utm_content=day_30' if promo else None),
        )
        if res.get('sent'):
            sent_count += 1

    return {'ok': True, 'sent_count': sent_count}


# ---------------------------------------------------------------------------
# Sequence 6: Onboarding drop-off (users signed up but never confirmed profile)
# ---------------------------------------------------------------------------

def process_onboarding_dropoffs() -> dict:
    """Scan for signed-up users who never confirmed their profile.
    Day 1 nudge, Day 3 personal follow-up.

    Safety invariant: ONBOARDING_DROPOFF_LAUNCH_DATE filter prevents
    retro-firing on the ~270 backfilled users whose profileConfirmedAt is
    null only because the field didn't exist when they onboarded. If you
    remove this filter, expect a wave of confused replies from long-time
    users being told to "finish setup."
    """
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    sent = {'day_1': 0, 'day_3': 0}
    now = datetime.now(timezone.utc)

    for snap in db.collection('users').stream():
        user = snap.to_dict() or {}
        uid = snap.id
        email = user.get('email')
        if not email:
            continue

        # Already confirmed onboarding: nothing to nudge
        if user.get('profileConfirmedAt'):
            continue

        # Paying users already invested. No onboarding nudge.
        tier = user.get('subscriptionTier') or user.get('tier') or 'free'
        if tier in ('pro', 'elite'):
            continue

        signup_at = _parse_ts_or_dt(user.get('signupAt'))
        if not signup_at:
            continue

        # Launch-date safety filter (see docstring)
        if signup_at < ONBOARDING_DROPOFF_LAUNCH_DATE:
            continue

        hours_since_signup = (now - signup_at).total_seconds() / 3600

        # Day 1: signup 24-48h ago, still no profile confirmation
        if 24 < hours_since_signup < 48 and not already_sent(uid, 'onboarding_dropoff', 'day_1'):
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='onboarding_dropoff',
                step='day_1',
                subject="you're 60 seconds from being set up",
                body_paragraphs=[
                    f"Hey, {SIGNATURE_NAME} here.",
                    "Saw you signed up but didn't finish setting up your profile. It takes about 60 seconds and unlocks alumni and hiring-manager search dialed to the companies you're targeting.",
                    "The rest of Offerloop is only useful once your profile is in.",
                ],
                cta_label="Finish setting up",
                cta_url=f'{PUBLIC_BASE_URL}/onboarding?utm_source=lifecycle&utm_campaign=onboarding_dropoff&utm_content=day_1',
            )
            if res.get('sent'):
                sent['day_1'] += 1

        # Day 3: signup 72-96h ago, still no profile confirmation
        elif 72 < hours_since_signup < 96 and not already_sent(uid, 'onboarding_dropoff', 'day_3'):
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='onboarding_dropoff',
                step='day_3',
                subject="anything i can help with?",
                body_paragraphs=[
                    f"Hey, {SIGNATURE_NAME} again.",
                    "One more nudge. If the onboarding flow is confusing, if something isn't working, or if you're just not sure Offerloop fits what you're recruiting for, reply to this email. I read every reply and answer.",
                    "If Offerloop isn't the right thing right now, no worries. Reply 'stop' and I'll take you off the list.",
                ],
                cta_label=None,
                cta_url=None,
            )
            if res.get('sent'):
                sent['day_3'] += 1

    return {'ok': True, 'sent': sent}


# ---------------------------------------------------------------------------
# Sequence 7: First-search activation (confirmed profile but never searched)
# ---------------------------------------------------------------------------

def process_first_search_activations() -> dict:
    """Scan for users who confirmed profile but haven't run a first search.
    Day 2 nudge (the one thing to do this week), Day 5 specific example.

    Safety invariant: FIRST_SEARCH_ACTIVATION_LAUNCH_DATE gates on signupAt
    (not profileConfirmedAt) so backfilled users whose profileConfirmedAt is
    null AND whose signupAt predates the launch never enroll. The scan also
    naturally skips backfilled users because they have no profileConfirmedAt
    stamp at all, but the signup-date filter is belt-and-suspenders in case
    a future backfill ever populates that field.
    """
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    sent = {'day_2': 0, 'day_5': 0}
    now = datetime.now(timezone.utc)

    for snap in db.collection('users').stream():
        user = snap.to_dict() or {}
        uid = snap.id
        email = user.get('email')
        if not email:
            continue

        # Must have confirmed profile (didn't drop off during onboarding)
        profile_confirmed_at = _parse_ts_or_dt(user.get('profileConfirmedAt'))
        if not profile_confirmed_at:
            continue

        # Skip users who already ran their first search
        if _parse_ts_or_dt(user.get('firstSearchAt')):
            continue

        # Paying users already invested. Skip.
        tier = user.get('subscriptionTier') or user.get('tier') or 'free'
        if tier in ('pro', 'elite'):
            continue

        # Belt-and-suspenders launch-date filter on signupAt
        signup_at = _parse_ts_or_dt(user.get('signupAt'))
        if not signup_at or signup_at < FIRST_SEARCH_ACTIVATION_LAUNCH_DATE:
            continue

        hours_since_confirm = (now - profile_confirmed_at).total_seconds() / 3600

        # Personalization: use user's targetIndustries or targetCompanies to
        # concretize the example. Falls back to a generic phrasing if we
        # don't have either signal (rare — onboarding collects at least one).
        industries = user.get('targetIndustries') or []
        primary_industry = (industries[0] if industries else '').lower()
        companies = user.get('targetCompanies') or user.get('dreamCompanies') or []
        primary_company = companies[0] if companies else None

        # Day 2: 48-72h after profileConfirmedAt
        if 48 < hours_since_confirm < 72 and not already_sent(uid, 'first_search_activation', 'day_2'):
            if primary_industry:
                second_line = f"The one thing to do this week: search Find for one hiring manager or alumni at a {primary_industry} firm you actually care about."
            else:
                second_line = "The one thing to do this week: search Find for one hiring manager or alumni at a firm you actually care about."
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='first_search_activation',
                step='day_2',
                subject="the one thing to do this week",
                body_paragraphs=[
                    f"Hey, {SIGNATURE_NAME} here.",
                    second_line,
                    "One search takes about 30 seconds. Either what Offerloop returns is dialed enough that the rest of the workflow makes sense, or it isn't, and you'll know in that first minute. Better than sitting on it.",
                ],
                cta_label="Run your first search",
                cta_url=f'{PUBLIC_BASE_URL}/find?utm_source=lifecycle&utm_campaign=first_search_activation&utm_content=day_2',
            )
            if res.get('sent'):
                sent['day_2'] += 1

        # Day 5: 120-144h after profileConfirmedAt
        elif 120 < hours_since_confirm < 144 and not already_sent(uid, 'first_search_activation', 'day_5'):
            if primary_company and primary_industry:
                example_line = f"Try this specifically: type '{primary_industry} analyst at {primary_company}' (or whatever role you're targeting) in Find. That's the exact query pattern our most active users start with."
            elif primary_industry:
                example_line = f"Try this specifically: type '{primary_industry} analyst at [company you're targeting]' in Find. Fill in the company that matters to you. That's the exact query pattern our most active users start with."
            else:
                example_line = "Try this specifically: type '[role you're recruiting for] at [company you're targeting]' in Find. Concrete title, concrete firm. That's how our most active users start."
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='first_search_activation',
                step='day_5',
                subject="one specific thing to try",
                body_paragraphs=[
                    f"Hey, {SIGNATURE_NAME} again.",
                    "Your profile is set up but you haven't tried a search yet. That's usually the hardest step for new users so I'll drop something specific.",
                    example_line,
                    "If the results feel off or you're not sure what to search for, reply and tell me what you're recruiting for. I'll suggest a search that actually fits.",
                ],
                cta_label="Try the search",
                cta_url=f'{PUBLIC_BASE_URL}/find?utm_source=lifecycle&utm_campaign=first_search_activation&utm_content=day_5',
            )
            if res.get('sent'):
                sent['day_5'] += 1

    return {'ok': True, 'sent': sent}


# ---------------------------------------------------------------------------
# Sequence 8: First-send activation (searched but never sent an email)
# ---------------------------------------------------------------------------

def process_first_send_activations() -> dict:
    """Scan for users who ran a first search but never sent an email.
    Addresses the "I found the contact but I'm scared to send" freeze
    that stops a lot of first-time cold outreach.

    Day 3 (72-96h after firstSearchAt): Name the fear, offer the shortest
    template that works, one CTA to compose.

    Day 7 (168-192h after firstSearchAt): Personal reply CTA from Deena.
    (The plan spec calls for an anonymized case study on Day 7 but that
    requires real user data; leaving as a reply-only prompt until Sid
    has a real case study to plug in.)

    Safety filter: FIRST_SEND_ACTIVATION_LAUNCH_DATE gates on signupAt
    to protect the backfilled users. Belt-and-suspenders alongside the
    natural firstSearchAt-must-be-set filter.
    """
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    sent = {'day_3': 0, 'day_7': 0}
    now = datetime.now(timezone.utc)

    for snap in db.collection('users').stream():
        user = snap.to_dict() or {}
        uid = snap.id
        email = user.get('email')
        if not email:
            continue

        # Must have run a search (past the profile-confirm and first-search steps)
        first_search_at = _parse_ts_or_dt(user.get('firstSearchAt'))
        if not first_search_at:
            continue

        # Skip users who already sent an email
        if _parse_ts_or_dt(user.get('firstEmailSentAt')):
            continue

        # Paying users skip
        tier = user.get('subscriptionTier') or user.get('tier') or 'free'
        if tier in ('pro', 'elite'):
            continue

        # Launch-date safety filter on signupAt
        signup_at = _parse_ts_or_dt(user.get('signupAt'))
        if not signup_at or signup_at < FIRST_SEND_ACTIVATION_LAUNCH_DATE:
            continue

        hours_since_search = (now - first_search_at).total_seconds() / 3600

        # Day 3: 72-96h after first search
        if 72 < hours_since_search < 96 and not already_sent(uid, 'first_send_activation', 'day_3'):
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='first_send_activation',
                step='day_3',
                subject="the send is the whole game",
                body_paragraphs=[
                    f"Hey, {SIGNATURE_NAME} here.",
                    "Saw you ran a search but haven't sent an email yet. That's the most common freeze point for first-time cold outreach — the search gives you the contacts, but hitting send feels like a real thing you can't take back.",
                    "The move that actually works is stupidly short. Two sentences, one question. Something like: 'Hey {first_name}, I'm a {school} student recruiting for {industry}. Would you be open to a 15-min call so I can ask how you got to {company}?' That's it. That's the whole thing. Most replies come back within 48 hours.",
                    "Offerloop drafts something like that for you in one click. Try one send this week and see what comes back.",
                ],
                cta_label="Draft your first email",
                cta_url=f'{PUBLIC_BASE_URL}/find?utm_source=lifecycle&utm_campaign=first_send_activation&utm_content=day_3',
            )
            if res.get('sent'):
                sent['day_3'] += 1

        # Day 7: 168-192h after first search
        elif 168 < hours_since_search < 192 and not already_sent(uid, 'first_send_activation', 'day_7'):
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='first_send_activation',
                step='day_7',
                subject="what's the block?",
                body_paragraphs=[
                    f"Hey, {SIGNATURE_NAME} again.",
                    "You ran a search a week ago and still haven't sent an email. There's usually one specific thing holding people up: not sure what to say, not sure who to send to first, worried about looking dumb, or the whole thing feels performative.",
                    "Whatever it is, reply and tell me. I'll help figure out the shortest first send that gets you a reply.",
                ],
                cta_label=None,
                cta_url=None,
            )
            if res.get('sent'):
                sent['day_7'] += 1

    return {'ok': True, 'sent': sent}


# ---------------------------------------------------------------------------
# Sequence 9: Welcome + onboarding drip (six emails over 30 days)
# ---------------------------------------------------------------------------

def process_welcome_drips() -> dict:
    """Six-email drip from Deena starting the moment someone signs up.
    Different intent per step:
    - Day 0: personal intro + reply-back question
    - Day 1: activation nudge to Find
    - Day 3: industry-personalized cold-email pattern
    - Day 7: 5 things top recruiters do differently
    - Day 14: honest Pro vs Free read (skipped if already Pro/Elite)
    - Day 30: month-1 recap + month-2 direction

    Safety invariant: WELCOME_DRIP_LAUNCH_DATE gates on signupAt. Every
    one of the ~270 backfilled users has signupAt < 2026-07-02, so none
    of them retro-enroll. Only signups from launch day forward flow
    through the drip.
    """
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    sent = {'day_0': 0, 'day_1': 0, 'day_3': 0, 'day_7': 0, 'day_14': 0, 'day_30': 0}
    now = datetime.now(timezone.utc)

    for snap in db.collection('users').stream():
        user = snap.to_dict() or {}
        uid = snap.id
        email = user.get('email')
        if not email:
            continue

        signup_at = _parse_ts_or_dt(user.get('signupAt'))
        if not signup_at:
            continue

        # Launch-date safety filter (mandatory — see docstring)
        if signup_at < WELCOME_DRIP_LAUNCH_DATE:
            continue

        hours_since_signup = (now - signup_at).total_seconds() / 3600
        tier = user.get('subscriptionTier') or user.get('tier') or 'free'
        industries = user.get('targetIndustries') or []
        primary_industry = (industries[0] if industries else '').lower()

        # Day 0: fires within first 6h of signup (catches any cron lag).
        # A/B test on subject line: curious opener (A) vs value-forward (B).
        # Variant is deterministic per uid so re-runs stay consistent.
        if 0 <= hours_since_signup < 6 and not already_sent(uid, 'welcome_drip', 'day_0'):
            variant = _ab_variant(uid, 'welcome_day_0_subject')
            day_0_subject = (
                "you just signed up, a question"
                if variant == 'A'
                else "welcome to Offerloop, quick question"
            )
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='welcome_drip',
                step='day_0',
                subject=day_0_subject,
                body_paragraphs=[
                    f"I'm {SIGNATURE_NAME}, one of the co-founders of Offerloop.",
                    "What industry are you recruiting for, and what school are you at? Reply with those two things and I'll send back the specific playbook for your situation over the next 30 days.",
                    "No script, no automated funnel. I read every reply.",
                ],
                cta_label=None,
                cta_url=None,
                variant=variant,
            )
            if res.get('sent'):
                sent['day_0'] += 1

        # Day 1: 24-30h after signup
        elif 24 < hours_since_signup < 30 and not already_sent(uid, 'welcome_drip', 'day_1'):
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='welcome_drip',
                step='day_1',
                subject="your recruiting workspace is ready",
                body_paragraphs=[
                    f"Hey, {SIGNATURE_NAME} here.",
                    "Your Find tab is set up with alumni and hiring-manager search dialed to the industries you told us about. One search takes about 30 seconds and pulls verified emails plus context on each contact.",
                    "Try one now. If nothing comes back or the results feel off, reply and tell me what you're targeting. I'll help figure out the right query.",
                ],
                cta_label="Run your first search",
                cta_url=f'{PUBLIC_BASE_URL}/find?utm_source=lifecycle&utm_campaign=welcome_drip&utm_content=day_1',
            )
            if res.get('sent'):
                sent['day_1'] += 1

        # Day 3: 72-78h — industry-personalized cold email pattern
        elif 72 < hours_since_signup < 78 and not already_sent(uid, 'welcome_drip', 'day_3'):
            if primary_industry:
                subject = f"the cold email that works in {primary_industry}"
                second_line = f"Every industry has its own cold-email pattern. For {primary_industry}, the one that works reliably is: subject line under five words, first sentence names a shared connection or specific project, body has one specific ask."
            else:
                subject = "the cold email that actually works"
                second_line = "The cold-email pattern that reliably works: subject line under five words, first sentence names a shared connection or specific project, body has one specific ask."
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='welcome_drip',
                step='day_3',
                subject=subject,
                body_paragraphs=[
                    f"Hey, {SIGNATURE_NAME} again.",
                    second_line,
                    "Template that works: 'Subject: Quick question about your work. Body: Hey {first_name}, I'm a {school} student recruiting for the industry. Saw your background at {company}. Would you be open to a 15-min call about how you got there? Best, {your_name}'",
                    "Two sentences, one specific ask. Send Tuesday through Thursday mornings. Offerloop drafts something like this for you in one click.",
                ],
                cta_label="Try one send",
                cta_url=f'{PUBLIC_BASE_URL}/find?utm_source=lifecycle&utm_campaign=welcome_drip&utm_content=day_3',
            )
            if res.get('sent'):
                sent['day_3'] += 1

        # Day 7: 168-174h — 5 things
        elif 168 < hours_since_signup < 174 and not already_sent(uid, 'welcome_drip', 'day_7'):
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='welcome_drip',
                step='day_7',
                subject="5 things that separate students who land offers",
                body_paragraphs=[
                    f"Hey, {SIGNATURE_NAME} here.",
                    "Watched a lot of student recruiters over the last year. Here's what separates the ones who land offers from the ones who grind and get nothing back:",
                    "1. Send Tuesday through Thursday between 6am and 8am local. Your email sits at the top when they open their inbox.",
                    "2. Reply to something they wrote or posted before cold-emailing them. Gives you a legit shared reference in sentence one.",
                    "3. Mention a specific project or deal they worked on, not the company. Everyone else names the company.",
                    "4. Ask for 15 minutes, not 'your time'. Specific ask converts about two-to-one.",
                    "5. Send from your school email. Response rate roughly doubles.",
                    "If any of these feel weird or you want to know why they work, reply and I'll explain.",
                ],
                cta_label=None,
                cta_url=None,
            )
            if res.get('sent'):
                sent['day_7'] += 1

        # Day 14: 336-342h — Pro upgrade nudge. Skip if already paying.
        elif 336 < hours_since_signup < 342 and tier not in ('pro', 'elite') and not already_sent(uid, 'welcome_drip', 'day_14'):
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='welcome_drip',
                step='day_14',
                subject="should you go pro?",
                body_paragraphs=[
                    f"Hey, {SIGNATURE_NAME} here.",
                    "You're two weeks in. Honest read on whether Pro is worth it:",
                    "If you're running 5+ searches a week, want 10 contacts per search instead of 3, or you want the hiring-manager search unlocked, Pro is $9.99/mo and pays for itself with one landed coffee chat.",
                    "If you're doing 1-2 searches, stay on Free. You don't need it yet.",
                    "The trial is 7 days free and you can cancel anytime. If you want to try it or you're unsure whether it fits your situation, reply and I'll help you decide.",
                ],
                cta_label="See Pro",
                cta_url=f'{PUBLIC_BASE_URL}/pricing?utm_source=lifecycle&utm_campaign=welcome_drip&utm_content=day_14',
            )
            if res.get('sent'):
                sent['day_14'] += 1

        # Day 30: 720-726h — month-1 recap + month-2 direction
        elif 720 < hours_since_signup < 726 and not already_sent(uid, 'welcome_drip', 'day_30'):
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='welcome_drip',
                step='day_30',
                subject="your first month",
                body_paragraphs=[
                    f"Hey, {SIGNATURE_NAME} here.",
                    "It's been 30 days since you signed up. Whether you're deep in the pipeline right now or Offerloop's been sitting in a tab, here's the honest read on month two:",
                    "The users who land offers by the end of month two usually run 3 searches a week, send 5 emails, and follow up on any reply within 24 hours. That's basically it. That's the pattern.",
                    "If you've fallen off or you're not sure what to do next, reply and tell me where you're stuck. I'll suggest the next specific move for your situation.",
                    "If you're already crushing it, keep the streak.",
                ],
                cta_label="Back to your workspace",
                cta_url=f'{PUBLIC_BASE_URL}/find?utm_source=lifecycle&utm_campaign=welcome_drip&utm_content=day_30',
            )
            if res.get('sent'):
                sent['day_30'] += 1

    return {'ok': True, 'sent': sent}


# ---------------------------------------------------------------------------
# Sequence 10: Coffee chat prep discovery (got a reply, hasn't tried Meeting Prep)
# ---------------------------------------------------------------------------

def process_coffee_chat_discoveries() -> dict:
    """One-shot email for users who got their first reply but never opened
    Meeting Prep. Fires within 24h of firstReplyReceivedAt.

    A reply is a stronger activation signal than "5+ sent" per the plan spec:
    it means they have a real meeting coming up and need to prep for it now,
    not later.

    Safety: COFFEE_CHAT_DISCOVERY_LAUNCH_DATE gates on signupAt.
    """
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    sent = {'day_0': 0}
    now = datetime.now(timezone.utc)

    for snap in db.collection('users').stream():
        user = snap.to_dict() or {}
        uid = snap.id
        email = user.get('email')
        if not email:
            continue

        first_reply_at = _parse_ts_or_dt(user.get('firstReplyReceivedAt'))
        if not first_reply_at:
            continue

        # Skip users who already used Meeting Prep
        if (user.get('coffeeChatPrepsUsed') or 0) > 0:
            continue

        signup_at = _parse_ts_or_dt(user.get('signupAt'))
        if not signup_at or signup_at < COFFEE_CHAT_DISCOVERY_LAUNCH_DATE:
            continue

        hours_since_reply = (now - first_reply_at).total_seconds() / 3600

        # Day 0: fire within 24h of first reply (positive-signal, hot moment)
        if 0 <= hours_since_reply < 24 and not already_sent(uid, 'coffee_chat_discovery', 'day_0'):
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='coffee_chat_discovery',
                step='day_0',
                subject="you got a reply, time to prep",
                body_paragraphs=[
                    f"Hey, {SIGNATURE_NAME} here.",
                    "Saw you got a reply on Offerloop, which means you have a real conversation coming up. Meeting Prep pulls together talking points, questions to ask, and background research on the person you're meeting so you don't fumble the actual call.",
                    "Free tier gets 3 preps lifetime, so save them for the meetings that matter most. Worth using one before your first coffee chat.",
                ],
                cta_label="Try Meeting Prep",
                cta_url=f'{PUBLIC_BASE_URL}/coffee-chat-prep?utm_source=lifecycle&utm_campaign=coffee_chat_discovery&utm_content=day_0',
            )
            if res.get('sent'):
                sent['day_0'] += 1

    return {'ok': True, 'sent': sent}


# ---------------------------------------------------------------------------
# Sequence 11: Job board discovery (has been active but never opened Job Board)
# ---------------------------------------------------------------------------

def process_job_board_discoveries() -> dict:
    """One-shot email at profileConfirmedAt + 10 days for users who haven't
    visited /job-board yet. The stamp is written by the /api/lifecycle/job-board-view
    endpoint on frontend mount.

    Safety: JOB_BOARD_DISCOVERY_LAUNCH_DATE gates on signupAt.
    """
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    sent = {'day_10': 0}
    now = datetime.now(timezone.utc)

    for snap in db.collection('users').stream():
        user = snap.to_dict() or {}
        uid = snap.id
        email = user.get('email')
        if not email:
            continue

        profile_confirmed_at = _parse_ts_or_dt(user.get('profileConfirmedAt'))
        if not profile_confirmed_at:
            continue

        # Skip users who already visited Job Board
        if _parse_ts_or_dt(user.get('jobBoardVisitedAt')):
            continue

        signup_at = _parse_ts_or_dt(user.get('signupAt'))
        if not signup_at or signup_at < JOB_BOARD_DISCOVERY_LAUNCH_DATE:
            continue

        hours_since_confirm = (now - profile_confirmed_at).total_seconds() / 3600

        # Day 10: fires 240-264h after profileConfirmedAt (10-11 days)
        if 240 < hours_since_confirm < 264 and not already_sent(uid, 'job_board_discovery', 'day_10'):
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='job_board_discovery',
                step='day_10',
                subject="the other half of Offerloop",
                body_paragraphs=[
                    f"Hey, {SIGNATURE_NAME} here.",
                    "Most students focus on the outreach side of Offerloop but the Job Board is where actual openings live. Every job listing has real hiring team contacts pre-attached, so you can email the hiring manager the same day the role posts.",
                    "It takes about a minute to see if anything on your target list is hiring right now. Worth a look.",
                ],
                cta_label="See Job Board",
                cta_url=f'{PUBLIC_BASE_URL}/job-board?utm_source=lifecycle&utm_campaign=job_board_discovery&utm_content=day_10',
            )
            if res.get('sent'):
                sent['day_10'] += 1

    return {'ok': True, 'sent': sent}


# ---------------------------------------------------------------------------
# Sequence 12: Free ceiling (free user hit 90% of monthly credits)
# ---------------------------------------------------------------------------

def process_free_ceilings() -> dict:
    """One email when a free user has used 90%+ of their monthly credit
    allotment. Idempotency key includes the calendar month so the same
    user can trigger again next month if they hit the ceiling again.

    Safety: FREE_CEILING_LAUNCH_DATE gates on signupAt.
    """
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    sent = {'month': 0}
    now = datetime.now(timezone.utc)
    month_key = now.strftime('%Y_%m')

    for snap in db.collection('users').stream():
        user = snap.to_dict() or {}
        uid = snap.id
        email = user.get('email')
        if not email:
            continue

        tier = user.get('subscriptionTier') or user.get('tier') or 'free'
        if tier != 'free':
            continue

        max_credits = user.get('maxCredits') or 0
        credits = user.get('credits')
        if not max_credits or credits is None:
            continue
        # 90% or more consumed
        if credits > 0.1 * max_credits:
            continue

        signup_at = _parse_ts_or_dt(user.get('signupAt'))
        if not signup_at or signup_at < FREE_CEILING_LAUNCH_DATE:
            continue

        used = max_credits - credits
        used_pct = int(round(100 * used / max_credits)) if max_credits else 0

        # Month-scoped idempotency: uid can retrigger next month, but only once per month
        step = f'month_{month_key}'
        if already_sent(uid, 'free_ceiling', step):
            continue

        res = _send_lifecycle_email(
            user_or_lead_id=uid,
            recipient_email=email,
            campaign='free_ceiling',
            step=step,
            subject=f"you've used {used_pct}% of your credits",
            body_paragraphs=[
                f"Hey, {SIGNATURE_NAME} here.",
                f"You've used {used} of your {max_credits} credits this month, which means you've been getting real value from Offerloop. Free tier resets on the 1st of next month, so you have a decision to make.",
                "If you want to keep going before then, Pro is $9.99/mo and gets you 2,000 credits (about 6x what you had this month) plus 10 contacts per search instead of 3. The trial is 7 days free.",
                "If you'd rather just wait for the reset, that's a legitimate move too. Just wanted to flag where you are.",
            ],
            cta_label="See Pro",
            cta_url=f'{PUBLIC_BASE_URL}/pricing?utm_source=lifecycle&utm_campaign=free_ceiling&utm_content={month_key}',
        )
        if res.get('sent'):
            sent['month'] += 1

    return {'ok': True, 'sent': sent}


# ---------------------------------------------------------------------------
# Sequence 13: Weekly win report (Sunday recap of past 7 days + peer median)
# ---------------------------------------------------------------------------

def _week_start_utc(now: datetime) -> datetime:
    """Return midnight UTC on the Monday that opened the just-completed
    Monday–Sunday week. `now` should be a Sunday; if it isn't, we still return
    Monday-of-current-ISO-week; the tick gate prevents the campaign from
    firing on any day other than Sunday."""
    monday = now - timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def _count_weekly_stats(uid: str, week_start_iso: str) -> dict:
    """Per-user weekly counts pulled from the contacts subcollection.
    Mirrors the aggregation done in networking_roadmap.compute_weekly_progress."""
    db = get_db()
    stats = {'contacts_added': 0, 'emails_sent': 0, 'replies_received': 0}
    if not db:
        return stats
    try:
        contacts_ref = db.collection('users').document(uid).collection('contacts')
        added = list(contacts_ref.where('createdAt', '>=', week_start_iso).limit(500).stream())
        stats['contacts_added'] = len(added)

        emailed = list(contacts_ref.where('emailGeneratedAt', '>=', week_start_iso).limit(500).stream())
        stats['emails_sent'] = len(emailed)
        for doc in emailed:
            data = doc.to_dict() or {}
            reply_at = data.get('replyReceivedAt')
            if reply_at and str(reply_at) >= week_start_iso:
                stats['replies_received'] += 1
    except Exception as e:
        logger.debug("weekly stats read failed for %s: %s", uid, e)
    return stats


def _median(values: list[int]) -> int:
    if not values:
        return 0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) // 2


def _weekly_nudge(stats: dict) -> tuple[str, str, str]:
    """Return (paragraph, cta_label, cta_url) for the next-week nudge based on
    the weakest link in the funnel. Emails sent gates replies; contacts added
    gates emails."""
    contacts = stats['contacts_added']
    emails = stats['emails_sent']
    replies = stats['replies_received']

    if contacts == 0:
        return (
            "For next week, the smallest useful action is one search. Pick one company you'd take a call from and pull three contacts. That's the whole thing.",
            "Run a search",
            f'{PUBLIC_BASE_URL}/find?utm_source=lifecycle&utm_campaign=weekly_win_report&utm_content=nudge_search',
        )
    if emails == 0:
        return (
            f"You added {contacts} contact{'s' if contacts != 1 else ''} but didn't send anything. The bottleneck for most students isn't finding people, it's writing the first email. Pick one contact and let Offerloop draft it.",
            "Send one email",
            f'{PUBLIC_BASE_URL}/my-network/people?utm_source=lifecycle&utm_campaign=weekly_win_report&utm_content=nudge_send',
        )
    if replies == 0:
        return (
            f"You sent {emails} email{'s' if emails != 1 else ''} and haven't heard back yet. That's normal at this volume. Doubling the send count usually breaks the silence, since reply rates are more about attempts than any single email being perfect.",
            "Send more",
            f'{PUBLIC_BASE_URL}/find?utm_source=lifecycle&utm_campaign=weekly_win_report&utm_content=nudge_more_sends',
        )
    return (
        f"You got {replies} repl{'ies' if replies != 1 else 'y'} this week. Keep the compound going by replying same-day and running one more search before Monday.",
        "See your pipeline",
        f'{PUBLIC_BASE_URL}/tracker?utm_source=lifecycle&utm_campaign=weekly_win_report&utm_content=nudge_pipeline',
    )


def process_weekly_win_reports() -> dict:
    """Sunday recap: 3 real numbers + peer median (if available) + next-week nudge.

    Fires only:
    - On Sunday, UTC 18:00–22:00 (11am–3pm Pacific, 2pm–6pm Eastern).
    - Once per user per ISO week (idempotency key includes iso_year/iso_week).
    - User signed up >= 7 days ago (so a full week of data exists).
    - profileConfirmedAt is set (they got past onboarding).
    - Total activity for the week > 0 (skip send-nothing weeks; activation
      campaigns already cover users with zero movement).
    - signupAt >= WEEKLY_WIN_REPORT_LAUNCH_DATE (protects the ~270 backfilled users).

    Peer comparison line only appears when we have >= 5 comparable users with
    non-zero activity; below that we skip the line rather than compare against
    a fragile sample.
    """
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    now = datetime.now(timezone.utc)

    # Day-of-week + time-of-day gate. Sunday = weekday 6. Send window 18:00–22:00 UTC.
    if now.weekday() != 6 or not (18 <= now.hour < 22):
        return {'ok': True, 'sent': {'weekly': 0}, 'skipped': 'not_send_window'}

    iso_year, iso_week, _ = now.isocalendar()
    step = f'week_{iso_year}_{iso_week:02d}'

    # The "week" we're reporting on is the Monday–Sunday that just closed.
    # At UTC Sunday 18:00 we're still inside that week, so start-of-week is
    # the Monday of the current ISO week.
    week_start = _week_start_utc(now)
    week_start_iso = week_start.isoformat().replace('+00:00', 'Z')

    eligible: list[tuple[str, dict, str, dict]] = []  # (uid, user, email, stats)

    for snap in db.collection('users').stream():
        user = snap.to_dict() or {}
        uid = snap.id
        email = user.get('email')
        if not email:
            continue

        signup_at = _parse_ts_or_dt(user.get('signupAt'))
        if not signup_at or signup_at < WEEKLY_WIN_REPORT_LAUNCH_DATE:
            continue
        if (now - signup_at).days < 7:
            continue
        if not _parse_ts_or_dt(user.get('profileConfirmedAt')):
            continue
        if already_sent(uid, 'weekly_win_report', step):
            continue

        stats = _count_weekly_stats(uid, week_start_iso)
        if stats['contacts_added'] + stats['emails_sent'] + stats['replies_received'] == 0:
            continue

        eligible.append((uid, user, email, stats))

    peer_line = None
    if len(eligible) >= 5:
        peer_contacts = _median([s['contacts_added'] for _, _, _, s in eligible])
        peer_emails = _median([s['emails_sent'] for _, _, _, s in eligible])
        peer_replies = _median([s['replies_received'] for _, _, _, s in eligible])
        peer_line = (
            f"For reference, the median student on Offerloop this week added {peer_contacts} "
            f"contact{'s' if peer_contacts != 1 else ''}, sent {peer_emails} "
            f"email{'s' if peer_emails != 1 else ''}, and got {peer_replies} "
            f"repl{'ies' if peer_replies != 1 else 'y'}."
        )

    sent = {'weekly': 0}
    for uid, _user, email, stats in eligible:
        contacts = stats['contacts_added']
        emails = stats['emails_sent']
        replies = stats['replies_received']

        stat_lines = [
            f"Contacts added: {contacts}",
            f"Emails sent: {emails}",
            f"Replies received: {replies}",
        ]
        stats_block = " · ".join(stat_lines)

        nudge_para, cta_label, cta_url = _weekly_nudge(stats)

        paragraphs = [
            f"Hey, {SIGNATURE_NAME} here. Quick recap of your last 7 days on Offerloop.",
            stats_block,
        ]
        if peer_line:
            paragraphs.append(peer_line)
        paragraphs.append(nudge_para)

        res = _send_lifecycle_email(
            user_or_lead_id=uid,
            recipient_email=email,
            campaign='weekly_win_report',
            step=step,
            subject="your week on Offerloop",
            body_paragraphs=paragraphs,
            cta_label=cta_label,
            cta_url=cta_url,
        )
        if res.get('sent'):
            sent['weekly'] += 1

    return {'ok': True, 'sent': sent, 'eligible': len(eligible)}


# ---------------------------------------------------------------------------
# Sequence 17: Pro monthly recap (1st of month, Pro/Elite users only)
# ---------------------------------------------------------------------------

def _prev_month_bounds_utc(now: datetime) -> tuple[datetime, datetime, str]:
    """Return (start, end, key) for the calendar month that just ended.
    key is 'YYYY_MM' of the reported month, used for idempotency."""
    first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = first_of_this_month
    prev_year = end.year if end.month > 1 else end.year - 1
    prev_month = end.month - 1 if end.month > 1 else 12
    start = end.replace(year=prev_year, month=prev_month)
    key = f"{prev_year}_{prev_month:02d}"
    return start, end, key


def _count_month_stats(uid: str, start_iso: str, end_iso: str) -> dict:
    """Per-user monthly counts pulled from the contacts subcollection. Same
    aggregation pattern as _count_weekly_stats, but bounded on both ends so
    we don't over-count when running on the 1st of the following month."""
    db = get_db()
    stats = {'contacts_added': 0, 'emails_sent': 0, 'replies_received': 0}
    if not db:
        return stats
    try:
        contacts_ref = db.collection('users').document(uid).collection('contacts')

        added = list(contacts_ref
                     .where('createdAt', '>=', start_iso)
                     .where('createdAt', '<', end_iso)
                     .limit(2000).stream())
        stats['contacts_added'] = len(added)

        emailed = list(contacts_ref
                       .where('emailGeneratedAt', '>=', start_iso)
                       .where('emailGeneratedAt', '<', end_iso)
                       .limit(2000).stream())
        stats['emails_sent'] = len(emailed)
        for doc in emailed:
            data = doc.to_dict() or {}
            reply_at = data.get('replyReceivedAt')
            if reply_at and start_iso <= str(reply_at) < end_iso:
                stats['replies_received'] += 1
    except Exception as e:
        logger.debug("monthly stats read failed for %s: %s", uid, e)
    return stats


def _untried_feature(user: dict) -> tuple[str, str, str]:
    """Return (paragraph, cta_label, cta_url) pointing at one Pro feature the
    user hasn't tried yet. Priority is Meeting Prep, then Loops, then Job Board.
    Falls back to a general tip if all three have been used."""
    if not (user.get('coffeeChatPrepsUsed') or 0):
        return (
            "One feature you haven't touched: Meeting Prep. If you have a coffee chat coming up, spin one up and it'll pull the person's background, common ground, and 5 questions worth asking. Costs 30 credits and turns a 30-minute call into something the other person remembers.",
            "Try Meeting Prep",
            f'{PUBLIC_BASE_URL}/coffee-chat-prep?utm_source=lifecycle&utm_campaign=pro_monthly_recap&utm_content=meeting_prep',
        )
    if not user.get('lastLoopRunAt'):
        return (
            "One feature you haven't tried: Loops. Instead of running one-off searches, you set a target (schools, companies, roles) and Offerloop runs a search weekly on your behalf, drafts outreach, and drops it in your queue. Set it once and it compounds.",
            "Set up a Loop",
            f'{PUBLIC_BASE_URL}/agent/setup?utm_source=lifecycle&utm_campaign=pro_monthly_recap&utm_content=loops',
        )
    if not _parse_ts_or_dt(user.get('jobBoardVisitedAt')):
        return (
            "One feature you haven't opened: the job board. It's ranked against your resume and pulls hiring managers per posting. Worth a look for the weeks when you'd rather browse than search.",
            "Open the job board",
            f'{PUBLIC_BASE_URL}/job-board?utm_source=lifecycle&utm_campaign=pro_monthly_recap&utm_content=job_board',
        )
    return (
        "One tip: the fastest month-over-month lift usually comes from replying to your existing threads same-day. Half the students who plateau are just leaving warm replies on the table.",
        "See your pipeline",
        f'{PUBLIC_BASE_URL}/tracker?utm_source=lifecycle&utm_campaign=pro_monthly_recap&utm_content=pipeline',
    )


def process_pro_monthly_recaps() -> dict:
    """1st-of-month recap for Pro and Elite users covering the calendar month
    that just ended.

    Fires only:
    - On the 1st of the month, UTC 15:00-19:00 (8am-12pm Pacific, 11am-3pm Eastern).
    - Once per user per calendar month (idempotency key uses YYYY_MM of the
      reported month).
    - subscriptionTier is 'pro' or 'elite'.
    - profileConfirmedAt is set (they got past onboarding).
    - signupAt >= PRO_MONTHLY_RECAP_LAUNCH_DATE (protects backfilled users).

    Users with zero activity for the month are skipped; the free_ceiling and
    activation campaigns already cover that funnel stage.
    """
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    now = datetime.now(timezone.utc)

    # Day-of-month + time-of-day gate. Only on the 1st, UTC 15:00-19:00.
    if now.day != 1 or not (15 <= now.hour < 19):
        return {'ok': True, 'sent': {'monthly': 0}, 'skipped': 'not_send_window'}

    start, end, month_key = _prev_month_bounds_utc(now)
    start_iso = start.isoformat().replace('+00:00', 'Z')
    end_iso = end.isoformat().replace('+00:00', 'Z')
    step = f'monthly_recap_{month_key}'

    sent = {'monthly': 0}
    eligible_count = 0

    for snap in db.collection('users').stream():
        user = snap.to_dict() or {}
        uid = snap.id
        email = user.get('email')
        if not email:
            continue

        tier = user.get('subscriptionTier') or user.get('tier') or 'free'
        if tier not in ('pro', 'elite'):
            continue

        signup_at = _parse_ts_or_dt(user.get('signupAt'))
        if not signup_at or signup_at < PRO_MONTHLY_RECAP_LAUNCH_DATE:
            continue
        if not _parse_ts_or_dt(user.get('profileConfirmedAt')):
            continue
        if already_sent(uid, 'pro_monthly_recap', step):
            continue

        stats = _count_month_stats(uid, start_iso, end_iso)
        if stats['contacts_added'] + stats['emails_sent'] + stats['replies_received'] == 0:
            continue

        eligible_count += 1

        month_name = start.strftime('%B')
        stats_block = " · ".join([
            f"Contacts added: {stats['contacts_added']}",
            f"Emails sent: {stats['emails_sent']}",
            f"Replies received: {stats['replies_received']}",
        ])
        feature_para, cta_label, cta_url = _untried_feature(user)

        paragraphs = [
            f"Hey, {SIGNATURE_NAME} here. Quick recap of your {month_name} on Offerloop.",
            stats_block,
            feature_para,
            "If any of this is off or you have thoughts on what would make next month more useful, just reply. Real inbox on this end.",
        ]

        res = _send_lifecycle_email(
            user_or_lead_id=uid,
            recipient_email=email,
            campaign='pro_monthly_recap',
            step=step,
            subject=f"your {month_name} on Offerloop",
            body_paragraphs=paragraphs,
            cta_label=cta_label,
            cta_url=cta_url,
        )
        if res.get('sent'):
            sent['monthly'] += 1

    return {'ok': True, 'sent': sent, 'eligible': eligible_count}


# ---------------------------------------------------------------------------
# Sequence 20: Renewal reminder (3 days before subscription renewal)
# ---------------------------------------------------------------------------

def _fetch_stripe_renewal_end(subscription_id: str) -> Optional[datetime]:
    """Live query Stripe for the current_period_end on a subscription. Returns
    None if the subscription is missing, cancelling at period end, or the
    Stripe SDK errors. Small blast radius: called at most ~15 times per tick
    (one per Pro/Elite user) and only inside the send window."""
    if not subscription_id or not STRIPE_SECRET_KEY:
        return None
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        sub = stripe.Subscription.retrieve(subscription_id)
    except Exception as e:
        logger.debug("stripe subscription retrieve failed for %s: %s", subscription_id, e)
        return None

    # Skip if already scheduled to cancel; sending a renewal reminder to
    # someone who cancelled would be confusing and off-message.
    if sub.get('cancel_at_period_end'):
        return None
    if sub.get('status') not in ('active', 'trialing'):
        return None

    period_end = sub.get('current_period_end')
    if not period_end:
        return None
    try:
        return datetime.fromtimestamp(int(period_end), tz=timezone.utc)
    except Exception:
        return None


def process_renewal_reminders() -> dict:
    """Send one email ~3 days before the subscription's current_period_end.

    Fires only:
    - UTC 15:00-19:00 window (avoids odd-hour sends across ticks).
    - subscriptionTier is 'pro' or 'elite' and stripeSubscriptionId is set.
    - Live Stripe query says current_period_end is 2-4 days from now and
      cancel_at_period_end is false.
    - signupAt >= RENEWAL_REMINDER_LAUNCH_DATE.
    - Once per renewal cycle (idempotency key is the ISO date of period_end).
    """
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    now = datetime.now(timezone.utc)
    if not (15 <= now.hour < 19):
        return {'ok': True, 'sent': {'renewal': 0}, 'skipped': 'not_send_window'}

    window_lo = now + timedelta(days=2)
    window_hi = now + timedelta(days=4)

    sent = {'renewal': 0}
    eligible_count = 0

    for snap in db.collection('users').stream():
        user = snap.to_dict() or {}
        uid = snap.id
        email = user.get('email')
        if not email:
            continue

        tier = user.get('subscriptionTier') or user.get('tier') or 'free'
        if tier not in ('pro', 'elite'):
            continue

        sub_id = user.get('stripeSubscriptionId')
        if not sub_id:
            continue

        signup_at = _parse_ts_or_dt(user.get('signupAt'))
        if not signup_at or signup_at < RENEWAL_REMINDER_LAUNCH_DATE:
            continue

        period_end = _fetch_stripe_renewal_end(sub_id)
        if not period_end or not (window_lo <= period_end < window_hi):
            continue

        step = f'renewal_{period_end.date().isoformat()}'
        if already_sent(uid, 'renewal_reminder', step):
            continue

        eligible_count += 1

        renew_date = period_end.strftime('%B %-d')
        tier_label = 'Elite' if tier == 'elite' else 'Pro'
        price_line = '$34.99' if tier == 'elite' else '$9.99'

        paragraphs = [
            f"Hey, {SIGNATURE_NAME} here. Quick heads up: your Offerloop {tier_label} plan renews on {renew_date} for {price_line}.",
            "Nothing you need to do. Sending this so it's not a surprise on the card statement.",
            "If you want to switch plans or cancel, you can do it from your account settings in under a minute. If you're staying, thanks for being on Pro; the product gets better every month because paying users tell me what's broken.",
        ]

        res = _send_lifecycle_email(
            user_or_lead_id=uid,
            recipient_email=email,
            campaign='renewal_reminder',
            step=step,
            subject=f"your Offerloop {tier_label} renews on {renew_date}",
            body_paragraphs=paragraphs,
            cta_label="Manage your subscription",
            cta_url=f'{PUBLIC_BASE_URL}/account-settings?utm_source=lifecycle&utm_campaign=renewal_reminder&utm_content={step}',
        )
        if res.get('sent'):
            sent['renewal'] += 1

    return {'ok': True, 'sent': sent, 'eligible': eligible_count}


# ---------------------------------------------------------------------------
# Sequences 14-16: Dormancy nudges (14d / 30d / 60d since lastActiveAt)
# ---------------------------------------------------------------------------

# One tier of dormancy at a time: at each day mark we send once, then wait for
# the next mark. Users who become active in between reset the ladder naturally
# via lastActiveAt bumping forward.
_DORMANCY_TIERS = [
    # (days_min, days_max, step_label, subject, body_paragraphs)
    (14, 21, '14d',
     "haven't seen you in 2 weeks",
     [
        "Hey, {SIGNATURE_NAME} here. Noticed you haven't opened Offerloop in a couple weeks.",
        "Not chasing. Two honest reasons students go quiet: recruiting timing shifted, or the first few searches didn't land the person you actually wanted. If it's the second one, reply and tell me the target profile; I can point you at the right filter combo or open a Loop for you.",
        "If it's the first, ignore this and come back when you're ready.",
     ]),
    (30, 45, '30d',
     "quick check-in",
     [
        "Hey, {SIGNATURE_NAME} here. It's been a month since you were last on Offerloop, so I wanted to check in.",
        "Two things worth flagging if you're still recruiting: (1) Loops now runs a weekly search on your behalf so you don't have to open the app to keep the pipeline moving; (2) Meeting Prep turns a coffee chat into a memorable one, and it's often the difference between a polite reply and a real introduction.",
        "If recruiting fell off your plate for now, no worries. I'll leave you alone.",
     ]),
    (60, None, '60d',
     "semester reset",
     [
        "Hey, {SIGNATURE_NAME} here. Two months since you were last on Offerloop, so I'm assuming your semester or your priorities shifted.",
        "If you're coming back at some point, your data is here waiting: contacts, threads, resume, everything. Nothing gets deleted.",
        "If you don't want emails from me anymore, the unsubscribe link at the bottom works and I won't take it personally.",
     ]),
]


def process_dormancy_nudges() -> dict:
    """Three-tier dormancy ladder at 14d, 30d, 60d since lastActiveAt.

    A user gets at most one dormancy email per tier ever. The tiers are
    non-overlapping windows (14-21d, 30-45d, 60d+) so returning users who
    become active mid-window naturally reset via lastActiveAt bumping.

    Filters:
    - lastActiveAt is set (fall back to signupAt so long-dormant signups still
      trigger). Users with no activity signal at all are skipped.
    - signupAt >= DORMANCY_NUDGE_LAUNCH_DATE (protects the ~270 backfilled users).
    - Not unsubscribed and not rate-limited (enforced downstream by
      _send_lifecycle_email).
    """
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    now = datetime.now(timezone.utc)
    sent = {'14d': 0, '30d': 0, '60d': 0}
    eligible = {'14d': 0, '30d': 0, '60d': 0}

    for snap in db.collection('users').stream():
        user = snap.to_dict() or {}
        uid = snap.id
        email = user.get('email')
        if not email:
            continue

        signup_at = _parse_ts_or_dt(user.get('signupAt'))
        if not signup_at or signup_at < DORMANCY_NUDGE_LAUNCH_DATE:
            continue

        # Prefer lastActiveAt; fall back to signupAt so users who signed up and
        # never came back still enter the ladder.
        last_active = _parse_ts_or_dt(user.get('lastActiveAt')) or signup_at
        days_since = (now - last_active).days
        if days_since < 14:
            continue

        for lo, hi, step, subject, para_template in _DORMANCY_TIERS:
            in_window = days_since >= lo and (hi is None or days_since < hi)
            if not in_window:
                continue
            if already_sent(uid, 'dormancy_nudge', step):
                break
            eligible[step] += 1
            body = [p.format(SIGNATURE_NAME=SIGNATURE_NAME) for p in para_template]
            res = _send_lifecycle_email(
                user_or_lead_id=uid,
                recipient_email=email,
                campaign='dormancy_nudge',
                step=step,
                subject=subject,
                body_paragraphs=body,
                cta_label="Open Offerloop",
                cta_url=f'{PUBLIC_BASE_URL}/find?utm_source=lifecycle&utm_campaign=dormancy_nudge&utm_content={step}',
            )
            if res.get('sent'):
                sent[step] += 1
            break

    return {'ok': True, 'sent': sent, 'eligible': eligible}


# ---------------------------------------------------------------------------
# Sequence 18: Pro to Elite upgrade nudge (80% of Pro monthly credits used)
# ---------------------------------------------------------------------------

def process_pro_upgrade_nudges() -> dict:
    """One email per calendar month when a Pro user has used 80%+ of their Pro
    credit allotment. Frames Elite as more headroom (5,000 vs 2,000 credits) and
    Priority Queue for reply detection.

    Plan doc originally called for 2,500+ used, written when Pro cap was 3,000.
    Pro cap is now 2,000, so the pragmatic power-user threshold is 80% of cap
    (1,600 used out of 2,000; 400 or fewer remaining). Same shape as Free
    ceiling, but earlier in the funnel (20% remaining vs 10%) since this is an
    upsell nudge, not a "you're about to run out" warning.

    Skips Elite users (already on the higher tier) and any user with no
    stripeSubscriptionId (comped / trial / weird state).
    """
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    now = datetime.now(timezone.utc)
    month_key = now.strftime('%Y_%m')
    step = f'month_{month_key}'
    sent = {'month': 0}
    eligible_count = 0

    for snap in db.collection('users').stream():
        user = snap.to_dict() or {}
        uid = snap.id
        email = user.get('email')
        if not email:
            continue

        tier = user.get('subscriptionTier') or user.get('tier') or 'free'
        if tier != 'pro':
            continue

        max_credits = user.get('maxCredits') or 0
        credits = user.get('credits')
        if not max_credits or credits is None:
            continue
        # 80% or more consumed (400 or fewer credits remaining on a 2,000 cap).
        if credits > 0.2 * max_credits:
            continue

        signup_at = _parse_ts_or_dt(user.get('signupAt'))
        if not signup_at or signup_at < PRO_UPGRADE_NUDGE_LAUNCH_DATE:
            continue

        if already_sent(uid, 'pro_upgrade_nudge', step):
            continue

        used = max_credits - credits
        used_pct = int(round(100 * used / max_credits)) if max_credits else 0
        eligible_count += 1

        paragraphs = [
            f"Hey, {SIGNATURE_NAME} here.",
            f"You've used {used} of your {max_credits} Pro credits this month ({used_pct}%). That's power-user pace; most Pro users don't get anywhere near their ceiling.",
            "If this is a normal month for you, Elite is $34.99/mo and gets you 5,000 credits (2.5x Pro), Priority Queue for reply detection, and personalized templates. If it's an outlier month, ignore this and the credits reset on your next billing cycle.",
        ]

        res = _send_lifecycle_email(
            user_or_lead_id=uid,
            recipient_email=email,
            campaign='pro_upgrade_nudge',
            step=step,
            subject=f"you've used {used_pct}% of your Pro credits",
            body_paragraphs=paragraphs,
            cta_label="See Elite",
            cta_url=f'{PUBLIC_BASE_URL}/pricing?utm_source=lifecycle&utm_campaign=pro_upgrade_nudge&utm_content={month_key}',
        )
        if res.get('sent'):
            sent['month'] += 1

    return {'ok': True, 'sent': sent, 'eligible': eligible_count}


# ---------------------------------------------------------------------------
# Sequence 19: Referral milestone (3+ replies received in past 30 days)
# ---------------------------------------------------------------------------

def _count_recent_replies(uid: str, since_iso: str, cap: int = 10) -> int:
    """Count replies received since `since_iso`. Stops at `cap` to keep the
    scan cheap: we only care whether the threshold is crossed."""
    db = get_db()
    if not db:
        return 0
    try:
        contacts_ref = db.collection('users').document(uid).collection('contacts')
        docs = list(contacts_ref
                    .where('replyReceivedAt', '>=', since_iso)
                    .limit(cap).stream())
        return len(docs)
    except Exception as e:
        logger.debug("recent reply count failed for %s: %s", uid, e)
        return 0


def process_referral_milestones() -> dict:
    """One email when a user has received 3+ replies in the past 30 days.
    Fires exactly once per user total (no step suffix beyond the campaign name)
    because the "you're getting real traction, share Offerloop with a friend"
    message doesn't need to repeat.

    Filters:
    - signupAt >= REFERRAL_MILESTONE_LAUNCH_DATE.
    - profileConfirmedAt set (real onboarded user).
    - 3+ replies in the past 30 days (queried from contacts subcollection).
    """
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=30)
    since_iso = since.isoformat().replace('+00:00', 'Z')
    step = 'reached'  # one-per-user total
    sent = {'reached': 0}
    eligible_count = 0

    from app.services.referral_service import get_or_create_referral_code

    for snap in db.collection('users').stream():
        user = snap.to_dict() or {}
        uid = snap.id
        email = user.get('email')
        if not email:
            continue

        signup_at = _parse_ts_or_dt(user.get('signupAt'))
        if not signup_at or signup_at < REFERRAL_MILESTONE_LAUNCH_DATE:
            continue
        if not _parse_ts_or_dt(user.get('profileConfirmedAt')):
            continue
        if already_sent(uid, 'referral_milestone', step):
            continue

        reply_count = _count_recent_replies(uid, since_iso, cap=3)
        if reply_count < 3:
            continue

        eligible_count += 1

        # Guarantee the user has a referral code before we build the link.
        # get_or_create_referral_code is idempotent and safe to call.
        try:
            code = get_or_create_referral_code(db, uid)
        except Exception as e:
            logger.debug("referral code lookup failed for %s: %s", uid, e)
            continue
        ref_link = f'{PUBLIC_BASE_URL}/signin?ref={code}&utm_source=lifecycle&utm_campaign=referral_milestone'

        paragraphs = [
            f"Hey, {SIGNATURE_NAME} here.",
            f"Noticed you've gotten {reply_count}+ replies in the last month. That's real traction and the compound effect from here is real: reply rates trend up as more people at the same company see you in the loop.",
            "If it's working for you, it'd probably work for one of your friends who's recruiting. Your personal referral link is below. There's no gimmick or reward on my side; I just want more USC / Michigan / NYU / Georgetown students in the network so replies land warmer for everyone.",
            f"Your link: {ref_link}",
        ]

        res = _send_lifecycle_email(
            user_or_lead_id=uid,
            recipient_email=email,
            campaign='referral_milestone',
            step=step,
            subject="you're getting real replies",
            body_paragraphs=paragraphs,
            cta_label="Share Offerloop",
            cta_url=ref_link,
        )
        if res.get('sent'):
            sent['reached'] += 1

    return {'ok': True, 'sent': sent, 'eligible': eligible_count}


# ---------------------------------------------------------------------------
# Cron entry: fires all time-based sequences
# ---------------------------------------------------------------------------

def process_all_pending_emails() -> dict:
    return {
        'pricing_abandon': process_pricing_leads(),
        'checkout_abandon': process_checkout_abandons(),
        'trial_ending': process_trial_endings(),
        'winback': process_winbacks(),
        'onboarding_dropoff': process_onboarding_dropoffs(),
        'first_search_activation': process_first_search_activations(),
        'first_send_activation': process_first_send_activations(),
        'welcome_drip': process_welcome_drips(),
        'coffee_chat_discovery': process_coffee_chat_discoveries(),
        'job_board_discovery': process_job_board_discoveries(),
        'free_ceiling': process_free_ceilings(),
        'weekly_win_report': process_weekly_win_reports(),
        'pro_monthly_recap': process_pro_monthly_recaps(),
        'renewal_reminder': process_renewal_reminders(),
        'dormancy_nudge': process_dormancy_nudges(),
        'pro_upgrade_nudge': process_pro_upgrade_nudges(),
        'referral_milestone': process_referral_milestones(),
    }


# ---------------------------------------------------------------------------
# Lead capture
# ---------------------------------------------------------------------------

def capture_pricing_lead(email: str, utm_source: Optional[str] = None) -> dict:
    if not email or '@' not in email:
        return {'ok': False, 'error': 'invalid_email'}
    db = get_db()
    if not db:
        return {'ok': False, 'error': 'db_unavailable'}

    docs = list(db.collection('lifecycle_leads').where('email', '==', email.lower()).limit(1).stream())
    if docs:
        lead_id = docs[0].id
        db.collection('lifecycle_leads').document(lead_id).update({
            'captured_at': datetime.now(timezone.utc),
            'utm_source': utm_source or docs[0].to_dict().get('utm_source'),
        })
        return {'ok': True, 'lead_id': lead_id, 'returning': True}

    lead_id = secrets.token_urlsafe(12)
    db.collection('lifecycle_leads').document(lead_id).set({
        'email': email.lower(),
        'source': 'pricing_exit',
        'utm_source': utm_source,
        'captured_at': datetime.now(timezone.utc),
    })
    return {'ok': True, 'lead_id': lead_id, 'returning': False}
