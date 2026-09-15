"""
RevenueCat webhook logic — Apple in-app purchases for the mobile app.

Split deliberately into a PURE decision layer and a thin Firestore layer:

  - `classify_event` turns a raw RevenueCat event into a `WebhookDecision`
    (what to do, to whom, for how many credits, under which idempotency key)
    with no I/O at all. Everything that can silently cost or give away money
    lives here, where `tests/test_revenuecat_webhook.py` can drive it with
    literal payloads.
  - `apply_decision` is the boring part that writes it down.

Why this exists: Apple takes the money for in-app purchases, so Stripe never
fires. RevenueCat calls us instead. See SPEC-iap-v2-pricing-and-credits.md §5.

Two rules from that spec that this module enforces:
  1. Credits are granted SERVER-SIDE off this webhook, never by the client.
  2. Purchased pack credits never expire — they land in the ledger's `bonus`
     bucket, which survives the monthly reset (see `credit_ledger.py`).

`app_user_id` on every event IS our Firebase uid, because the app calls
`Purchases.logIn(firebaseUid)` right after auth. That equality is the hinge the
whole file turns on; a purchase made before logIn lands on a `$RCAnonymousID:…`
instead and is deliberately dropped rather than credited to a stranger.
"""
import hmac
import logging
from dataclasses import dataclass, field
from typing import Optional

import requests

from app.config import (
    IAP_CREDIT_PACKS,
    IAP_ENTITLEMENT_TIERS,
    REVENUECAT_API_KEY,
    TIER_CONFIGS,
)

logger = logging.getLogger(__name__)

# Event types that describe the state of a SUBSCRIPTION. We don't hand-derive
# the resulting tier from these (RevenueCat's own guidance, and it's easy to get
# PRODUCT_CHANGE wrong) — any of them just means "go re-read the truth".
SUBSCRIPTION_EVENT_TYPES = frozenset({
    'INITIAL_PURCHASE',
    'RENEWAL',
    'UNCANCELLATION',
    'PRODUCT_CHANGE',
    'CANCELLATION',
    'EXPIRATION',
    'BILLING_ISSUE',
    'SUBSCRIPTION_PAUSED',
    'SUBSCRIPTION_EXTENDED',
    'REFUND',
})

# A consumable purchase. This is the only event type that grants pack credits.
PACK_PURCHASE_EVENT_TYPES = frozenset({'NON_RENEWING_PURCHASE'})

# RevenueCat's own alias/merge: fires when Purchases.logIn() merges an
# anonymous identity's purchase history into a real (Firebase-uid) identity.
# This is the exact case of a promo/offer-code redemption completed via Safari
# or the App Store before the app has ever logged the buyer in. The event's
# own app_user_id is documented as the destination, but transferred_to is the
# field RevenueCat guarantees, so classify_event reads that instead.
TRANSFER_EVENT_TYPES = frozenset({'TRANSFER'})

# Reversals. A consumable has no renewal to cancel, so CANCELLATION landing on a
# pack product means Apple reversed the charge.
PACK_REVERSAL_EVENT_TYPES = frozenset({'REFUND', 'CANCELLATION'})

# Stripe subscription statuses that still mean "the web owns this customer".
# past_due is a dunning failure, not a cancellation — Apple must not step in.
LIVE_STRIPE_STATUSES = frozenset({'active', 'trialing', 'past_due'})

RC_API_BASE = 'https://api.revenuecat.com/v1'
RC_API_TIMEOUT_S = 8


@dataclass(frozen=True)
class WebhookDecision:
    """What the handler should do about one event. `action` is one of:
    ignore | grant_pack | claw_back_pack | sync_subscription."""
    action: str
    reason: str
    uid: Optional[str] = None
    credits: int = 0
    idempotency_key: Optional[str] = None
    product_id: Optional[str] = None
    fallback_tier: Optional[str] = None
    period_type: Optional[str] = None
    environment: Optional[str] = None
    entitlement_ids: tuple = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def verify_webhook_secret(header_value: Optional[str],
                          expected: Optional[str]) -> bool:
    """Constant-time compare of the Authorization header against the shared
    secret configured in the RevenueCat dashboard.

    Fails CLOSED when no secret is configured — an unset env var must not turn
    this endpoint into an open credit faucet. Accepts both the bare secret and
    a `Bearer <secret>` form, because the dashboard field takes whatever literal
    string you type and people type "Bearer" out of habit.
    """
    if not expected or not header_value:
        return False
    candidate = header_value.strip()
    if candidate.lower().startswith('bearer '):
        candidate = candidate[7:].strip()
    return hmac.compare_digest(candidate, expected.strip())


def tier_from_entitlements(entitlement_ids) -> str:
    """Map active RevenueCat entitlements onto our tier. Elite outranks Pro if
    both are somehow active (a mid-cycle upgrade can briefly show both).
    Anything unrecognised grants nothing."""
    if not entitlement_ids:
        return 'free'
    tiers = {IAP_ENTITLEMENT_TIERS.get(e) for e in entitlement_ids}
    if 'elite' in tiers:
        return 'elite'
    if 'pro' in tiers:
        return 'pro'
    return 'free'


def has_active_stripe_subscription(user_data: dict) -> bool:
    """True when the web still owns this customer's subscription.

    The guardrail from spec §5: an Apple event must never stomp a live Stripe
    subscription (and the Stripe handler has the mirror-image rule). Once
    `subscriptionSource` says apple, a stale Stripe id left on the doc stops
    counting — otherwise Apple could never write to a former web subscriber.
    """
    if (user_data or {}).get('subscriptionSource') == 'apple':
        return False
    if not (user_data or {}).get('stripeSubscriptionId'):
        return False
    status = str((user_data or {}).get('subscriptionStatus') or '').lower()
    return status in LIVE_STRIPE_STATUSES


def clawback_amounts(bonus: int, amount: int) -> tuple:
    """Reverse a pack grant. Returns (new_bonus, shortfall).

    The open decision from spec §5, decided here: CLAMP at zero. If the credits
    were already spent we do not carry debt into the user's next purchase —
    a booby-trapped top-up is a support ticket and a chargeback, and the sums
    involved are single-digit dollars. The shortfall is returned so the caller
    can record it for support/abuse visibility instead.
    """
    bonus = max(0, int(bonus or 0))
    amount = max(0, int(amount or 0))
    removed = min(bonus, amount)
    return bonus - removed, amount - removed


def resolve_pack_credits(product_id: Optional[str]) -> int:
    """Credits for a pack product id, or 0 if we don't recognise it."""
    return int(IAP_CREDIT_PACKS.get(product_id or '', 0))


def _is_usable_uid(app_user_id: Optional[str]) -> bool:
    return bool(app_user_id) and not str(app_user_id).startswith('$RCAnonymousID')


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

def classify_event(event: dict, *, expect_production: bool) -> WebhookDecision:
    """Turn one RevenueCat event into a decision. No I/O, no side effects.

    Order matters: identity and environment are checked before anything that
    could move money.
    """
    event = event or {}
    event_type = str(event.get('type') or '').upper()
    uid = event.get('app_user_id')
    if event_type in TRANSFER_EVENT_TYPES:
        transferred_to = event.get('transferred_to') or []
        if transferred_to:
            uid = transferred_to[-1]
    product_id = event.get('product_id')
    environment = str(event.get('environment') or '').upper()
    period_type = event.get('period_type')
    entitlements = tuple(event.get('entitlement_ids') or ())

    def decision(action, reason, **kw):
        return WebhookDecision(
            action=action, reason=reason, product_id=product_id,
            environment=environment, period_type=period_type,
            entitlement_ids=entitlements, **kw,
        )

    # 1. Identity. app_user_id is our Firebase uid; without a real one there is
    #    nobody to credit and guessing is worse than dropping the event.
    if not uid:
        return decision('ignore', 'no_app_user_id')
    if not _is_usable_uid(uid):
        return decision('ignore', 'anonymous_app_user_id')

    # 2. Environment. Sandbox test-buys must never touch production balances,
    #    and staging must never process a real customer's purchase.
    if expect_production != (environment == 'PRODUCTION'):
        return decision('ignore', 'environment_mismatch', uid=uid)

    credits = resolve_pack_credits(product_id)
    is_pack_product = credits > 0

    # 3. Pack purchase. Idempotent on Apple's transaction id — the only stable
    #    per-purchase identifier across RevenueCat's 5 retries.
    if event_type in PACK_PURCHASE_EVENT_TYPES:
        if not is_pack_product:
            return decision('ignore', 'unknown_product', uid=uid)
        return decision(
            'grant_pack', 'ok', uid=uid, credits=credits,
            idempotency_key=str(event.get('transaction_id') or event.get('id') or ''),
        )

    # 4. Pack reversal. Keyed separately from the purchase it reverses: the
    #    refund carries the SAME transaction id, so sharing a key would make the
    #    clawback look like an already-applied grant and silently drop it.
    if event_type in PACK_REVERSAL_EVENT_TYPES and is_pack_product:
        txn = str(event.get('transaction_id') or event.get('id') or '')
        return decision(
            'claw_back_pack', 'ok', uid=uid, credits=credits,
            idempotency_key=f'refund:{txn}',
        )

    # 5. Subscription, including TRANSFER now that uid has been resolved to the
    #    destination above. _sync_subscription re-reads RevenueCat's live
    #    subscriber snapshot rather than trusting the event, so replaying this
    #    (retry, or a uid that was already synced by a normal RENEWAL) is a
    #    no-op: it only grants the monthly pool when tier actually changes.
    if event_type in SUBSCRIPTION_EVENT_TYPES or event_type in TRANSFER_EVENT_TYPES:
        return decision(
            'sync_subscription', 'ok', uid=uid,
            fallback_tier=tier_from_entitlements(entitlements),
            idempotency_key=str(event.get('id') or ''),
        )

    # 6. TEST (the dashboard's "send test webhook" button) and anything
    #    RevenueCat adds later.
    return decision('ignore', 'unhandled_event_type', uid=uid)


# ---------------------------------------------------------------------------
# The authoritative read
# ---------------------------------------------------------------------------

def fetch_subscriber_tier(uid: str) -> Optional[str]:
    """Ask RevenueCat what this user's subscription looks like RIGHT NOW.

    Preferred over deriving tier from the event type (spec §5, and RevenueCat's
    own recommendation): one code path handles purchase, renewal, upgrade,
    downgrade, cancel, expiry and refund without a per-event-type truth table
    to get wrong. Returns None if we can't reach RevenueCat, so the caller can
    fall back to the event-derived tier rather than downgrading a paying user
    because of a network blip.
    """
    if not REVENUECAT_API_KEY or not uid:
        return None
    try:
        resp = requests.get(
            f'{RC_API_BASE}/subscribers/{uid}',
            headers={'Authorization': f'Bearer {REVENUECAT_API_KEY}'},
            timeout=RC_API_TIMEOUT_S,
        )
        if resp.status_code != 200:
            logger.warning('RevenueCat subscriber fetch for %s returned %s', uid, resp.status_code)
            return None
        entitlements = ((resp.json() or {}).get('subscriber') or {}).get('entitlements') or {}
    except Exception as e:
        logger.warning('RevenueCat subscriber fetch failed for %s: %s', uid, e)
        return None

    # An entitlement is active when expires_date is in the future. RevenueCat
    # sends ISO8601 UTC, sometimes with milliseconds — parse rather than string
    # compare so the millisecond form doesn't read as already-expired.
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    def _still_active(ent) -> bool:
        raw = (ent or {}).get('expires_date')
        if not raw:
            return True  # lifetime / non-expiring entitlement
        try:
            return datetime.fromisoformat(str(raw).replace('Z', '+00:00')) > now
        except ValueError:
            # Unparseable date: treat as active. Erring toward "still paying"
            # is the safe direction — the alternative downgrades a real customer.
            logger.warning('Unparseable RevenueCat expires_date %r for %s', raw, uid)
            return True

    return tier_from_entitlements([n for n, e in entitlements.items() if _still_active(e)])


def max_credits_for_tier(tier: str) -> int:
    """Monthly allowance for a tier, straight from TIER_CONFIGS so the webhook
    can't drift from the rest of the backend."""
    return int((TIER_CONFIGS.get(tier) or TIER_CONFIGS['free']).get('credits') or 0)


# ---------------------------------------------------------------------------
# The write layer
# ---------------------------------------------------------------------------

def _is_already_exists(exc: Exception) -> bool:
    return type(exc).__name__ == 'AlreadyExists' or 'already exists' in str(exc).lower()


def _claim(db, uid: str, key: str, decision: 'WebhookDecision'):
    """Atomically claim an event id so it can only be applied once.

    `.create()` fails if the document is already there, which makes the claim
    itself the idempotency check — no read-then-write race between two of
    RevenueCat's 5 retries arriving at two gunicorn workers at the same time.

    Returns the claim ref, or None if this event was already applied.
    """
    from datetime import datetime, timezone
    ref = db.collection('users').document(uid).collection('iapPurchases').document(key)
    try:
        ref.create({
            'action': decision.action,
            'productId': decision.product_id,
            'credits': decision.credits,
            'environment': decision.environment,
            'claimedAt': datetime.now(timezone.utc).isoformat(),
            'status': 'claimed',
        })
        return ref
    except Exception as e:
        if _is_already_exists(e):
            return None
        raise


def apply_decision(decision: 'WebhookDecision', doc_id_scrubber=None) -> dict:
    """Write down whatever `classify_event` decided. Raises on failure so the
    route can 500 and let RevenueCat retry."""
    from app.extensions import get_db

    db = get_db()
    if not db:
        raise RuntimeError('firestore_unavailable')

    scrub = doc_id_scrubber or (lambda k: k)

    if decision.action == 'sync_subscription':
        return _sync_subscription(db, decision)

    key = scrub(decision.idempotency_key or '')
    claim = _claim(db, decision.uid, key, decision)
    if claim is None:
        logger.info('RevenueCat event %s already applied for %s — skipping', key, decision.uid)
        return {'alreadyApplied': True, 'credits': 0}

    try:
        if decision.action == 'grant_pack':
            result = _grant_pack(db, decision)
        elif decision.action == 'claw_back_pack':
            result = _claw_back_pack(db, decision)
        else:
            raise ValueError(f'unknown action {decision.action}')
    except Exception:
        # Release the claim so RevenueCat's retry can actually retry. Leaving a
        # claimed-but-unapplied sentinel behind would swallow the purchase.
        try:
            claim.delete()
        except Exception:
            logger.exception('Failed to release RevenueCat claim %s for %s', key, decision.uid)
        raise

    claim.update({'status': 'applied', **result})
    return result


def _grant_pack(db, decision: 'WebhookDecision') -> dict:
    """Add pack credits to the never-expiring bonus bucket."""
    from app.services.credit_ledger import apply_add_purchased_atomic

    ok, new_total = apply_add_purchased_atomic(decision.uid, decision.credits)
    if not ok:
        raise RuntimeError('ledger_write_failed')
    logger.info('IAP pack granted: user=%s +%d credits (%s) new_total=%d',
                decision.uid, decision.credits, decision.product_id, new_total)
    return {'credits': decision.credits, 'newTotal': new_total}


def _claw_back_pack(db, decision: 'WebhookDecision') -> dict:
    """Reverse a refunded pack.

    Writes `bonusCredits` directly in a transaction rather than going through
    the ledger's add/deduct ops: this is not spending, and the clamp policy in
    `clawback_amounts` (never carry debt into the next purchase) is specific to
    store refunds. `bonusCredits` is the same single field `add_purchased`
    writes, so the shapes stay consistent.
    """
    from datetime import datetime, timezone

    from firebase_admin import firestore

    user_ref = db.collection('users').document(decision.uid)

    @firestore.transactional
    def claw(transaction):
        snap = user_ref.get(transaction=transaction)
        if not snap.exists:
            return 0, decision.credits
        data = snap.to_dict() or {}
        new_bonus, shortfall = clawback_amounts(data.get('bonusCredits'), decision.credits)
        updates = {
            'bonusCredits': new_bonus,
            'lastCreditUpdate': datetime.now(timezone.utc).isoformat(),
        }
        if shortfall:
            # Recorded for support, NOT applied as debt — see clawback_amounts.
            updates['lastPackRefundShortfall'] = shortfall
        transaction.update(user_ref, updates)
        return new_bonus, shortfall

    new_bonus, shortfall = claw(db.transaction())
    if shortfall:
        logger.warning('IAP refund clawback short by %d for %s (%s) — credits already spent',
                       shortfall, decision.uid, decision.product_id)
    logger.info('IAP pack refunded: user=%s -%d credits (%s) bonus_now=%d',
                decision.uid, decision.credits, decision.product_id, new_bonus)
    return {'credits': -decision.credits, 'shortfall': shortfall, 'bonusCredits': new_bonus}


def _sync_subscription(db, decision: 'WebhookDecision') -> dict:
    """Set tier from the authoritative subscriber snapshot.

    Not idempotency-claimed on purpose: this is a converge-to-current-state
    write, so applying it twice is harmless and applying a STALE event twice is
    still better than skipping the newest one.
    """
    from datetime import datetime, timezone

    user_ref = db.collection('users').document(decision.uid)
    snap = user_ref.get()
    if not snap.exists:
        logger.warning('RevenueCat subscription event for unknown user %s', decision.uid)
        return {'skipped': 'user_not_found'}
    user_data = snap.to_dict() or {}

    # Guardrail (spec §5): Apple must never stomp a live web subscription.
    if has_active_stripe_subscription(user_data):
        logger.warning('Ignoring Apple subscription event for %s — active Stripe subscription',
                       decision.uid)
        return {'skipped': 'stripe_owns_subscription'}

    # Authoritative read, with the event's own entitlements as the fallback so a
    # RevenueCat outage can't downgrade a paying user.
    tier = fetch_subscriber_tier(decision.uid)
    source = 'revenuecat_api'
    if tier is None:
        tier = decision.fallback_tier or 'free'
        source = 'event'

    now = datetime.now(timezone.utc).isoformat()
    current_tier = str(user_data.get('subscriptionTier') or user_data.get('tier') or 'free').lower()
    max_credits = max_credits_for_tier(tier)

    updates = {
        'subscriptionTier': tier,
        'tier': tier,  # legacy mirror, same as the Stripe path
        'maxCredits': max_credits,
        'subscriptionSource': 'apple',
        'subscriptionStatus': 'active' if tier != 'free' else None,
        'updatedAt': now,
    }

    if tier != current_tier:
        if tier == 'free':
            # Mirrors the Stripe cancellation path: keep what's left, capped at
            # what a free user is allowed to hold.
            updates['credits'] = min(int(user_data.get('credits') or 0), max_credits)
            updates['canceledAt'] = now
        else:
            # New subscription or upgrade — hand over the full monthly pool now.
            # Deliberately NOT done on same-tier RENEWAL events: the allowance
            # resets on the 1st, and refilling on Apple's renewal date too would
            # hand out two months of credits to anyone billed mid-month.
            updates['credits'] = max_credits
            updates['lastCreditReset'] = now
            updates['trialActive'] = False
            updates['upgraded_at'] = now

    if decision.period_type:
        updates['applePeriodType'] = decision.period_type

    user_ref.update(updates)
    logger.info('IAP subscription synced: user=%s %s → %s (via %s, period=%s)',
                decision.uid, current_tier, tier, source, decision.period_type)
    return {'tier': tier, 'previousTier': current_tier, 'tierSource': source}
