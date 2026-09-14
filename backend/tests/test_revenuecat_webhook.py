"""
Unit tests for `app.services.revenuecat_service` — pure functions only.

These tests deliberately avoid Firestore / Flask / the network. They drive the
webhook's decision logic with literal RevenueCat event payloads and assert what
the handler would DO. Coverage:

  - verify_webhook_secret: exact match, Bearer-prefixed, mismatch, fail-closed
    when nothing is configured
  - classify_event: pack purchase → grant, unknown product → nothing,
    refund → clawback with its own idempotency key, subscription events →
    sync, sandbox/production crossover ignored, missing uid ignored
  - tier_from_entitlements: elite outranks pro, nothing means free
  - has_active_stripe_subscription: the guardrail that stops an Apple event
    stomping a live web subscriber
  - clawback_amounts: clamp at zero when the refunded credits are already spent

The Firestore appliers in `revenuecat_webhook.py` are NOT tested here (same
reason `test_credit_ledger.py` skips the `apply_*_atomic` wrappers — they need
a real Firestore mock). The sandbox QA gate in the spec covers those.
"""
import pytest

from app.services.revenuecat_service import (
    classify_event,
    clawback_amounts,
    has_active_stripe_subscription,
    tier_from_entitlements,
    verify_webhook_secret,
)


# ---------------------------------------------------------------------------
# Event builders — the shapes RevenueCat actually POSTs
# ---------------------------------------------------------------------------

def pack_event(**overrides):
    """NON_RENEWING_PURCHASE — a credit pack bought in the app."""
    event = {
        'id': 'evt-pack-1',
        'type': 'NON_RENEWING_PURCHASE',
        'app_user_id': 'firebase-uid-abc',
        'product_id': 'ai.offerloop.app.credits.400',
        'transaction_id': 'txn-400-xyz',
        'environment': 'PRODUCTION',
        'store': 'APP_STORE',
        'period_type': 'NORMAL',
    }
    event.update(overrides)
    return event


def subscription_event(**overrides):
    """INITIAL_PURCHASE — Pro bought in the app."""
    event = {
        'id': 'evt-sub-1',
        'type': 'INITIAL_PURCHASE',
        'app_user_id': 'firebase-uid-abc',
        'product_id': 'ai.offerloop.app.pro.monthly',
        'transaction_id': 'txn-sub-xyz',
        'environment': 'PRODUCTION',
        'store': 'APP_STORE',
        'period_type': 'NORMAL',
        'entitlement_ids': ['pro'],
    }
    event.update(overrides)
    return event


# ---------------------------------------------------------------------------
# verify_webhook_secret
# ---------------------------------------------------------------------------

def test_verify_secret_accepts_exact_match():
    assert verify_webhook_secret('s3cret-value', 's3cret-value') is True


def test_verify_secret_accepts_bearer_prefixed_header():
    """RevenueCat sends whatever literal string you typed into the dashboard.
    People type "Bearer <secret>" out of habit, so accept both forms rather
    than fail a live webhook on a cosmetic prefix."""
    assert verify_webhook_secret('Bearer s3cret-value', 's3cret-value') is True


def test_verify_secret_rejects_wrong_secret():
    assert verify_webhook_secret('wrong-value', 's3cret-value') is False


def test_verify_secret_rejects_when_nothing_configured():
    """Fail CLOSED. An unset env var must not turn the endpoint into an open
    credit faucet."""
    assert verify_webhook_secret('anything', '') is False
    assert verify_webhook_secret('anything', None) is False


def test_verify_secret_rejects_missing_header():
    assert verify_webhook_secret(None, 's3cret-value') is False
    assert verify_webhook_secret('', 's3cret-value') is False


# ---------------------------------------------------------------------------
# classify_event — credit packs
# ---------------------------------------------------------------------------

def test_pack_purchase_grants_mapped_credits():
    d = classify_event(pack_event(), expect_production=True)
    assert d.action == 'grant_pack'
    assert d.credits == 400
    assert d.uid == 'firebase-uid-abc'


def test_pack_purchase_keys_idempotency_on_transaction_id():
    """Apple's transaction id is the only stable per-purchase identifier across
    RevenueCat's 5 retries."""
    d = classify_event(pack_event(transaction_id='txn-unique-1'), expect_production=True)
    assert d.idempotency_key == 'txn-unique-1'


def test_pack_purchase_falls_back_to_event_id_without_transaction_id():
    d = classify_event(pack_event(transaction_id=None, id='evt-fallback'), expect_production=True)
    assert d.idempotency_key == 'evt-fallback'


def test_every_spec_pack_size_maps():
    for product, credits in (
        ('ai.offerloop.app.credits.150', 150),
        ('ai.offerloop.app.credits.400', 400),
        ('ai.offerloop.app.credits.1000', 1000),
    ):
        d = classify_event(pack_event(product_id=product), expect_production=True)
        assert d.action == 'grant_pack'
        assert d.credits == credits, product


def test_staging_preview_pack_ids_map_to_the_same_credits():
    """The staging build ships the .preview bundle id, so its product ids
    differ. They must grant identically or the QA gate proves nothing."""
    d = classify_event(
        pack_event(product_id='ai.offerloop.app.preview.credits.1000',
                   environment='SANDBOX'),
        expect_production=False,
    )
    assert d.action == 'grant_pack'
    assert d.credits == 1000


def test_unknown_product_grants_nothing():
    """A product id nobody mapped must never guess an amount."""
    d = classify_event(pack_event(product_id='ai.offerloop.app.credits.99999'),
                       expect_production=True)
    assert d.action == 'ignore'
    assert d.reason == 'unknown_product'
    assert d.credits == 0


# ---------------------------------------------------------------------------
# classify_event — refunds
# ---------------------------------------------------------------------------

def test_refund_of_pack_claws_back_the_same_credits():
    d = classify_event(pack_event(type='REFUND'), expect_production=True)
    assert d.action == 'claw_back_pack'
    assert d.credits == 400


def test_refund_idempotency_key_differs_from_the_purchase():
    """The refund carries the SAME transaction id as the purchase it reverses.
    Without a distinct key the clawback looks like an already-applied grant and
    gets silently dropped."""
    purchase = classify_event(pack_event(transaction_id='txn-same'), expect_production=True)
    refund = classify_event(pack_event(type='REFUND', transaction_id='txn-same'),
                            expect_production=True)
    assert refund.idempotency_key != purchase.idempotency_key


def test_cancellation_of_a_pack_is_a_clawback():
    """A consumable has no renewal to cancel, so CANCELLATION on a pack product
    is Apple reversing the charge."""
    d = classify_event(pack_event(type='CANCELLATION'), expect_production=True)
    assert d.action == 'claw_back_pack'


def test_refund_of_a_subscription_syncs_instead_of_clawing_back_credits():
    """A refunded subscription is a tier change, not a pack reversal — it must
    not try to remove pack credits."""
    d = classify_event(subscription_event(type='REFUND'), expect_production=True)
    assert d.action == 'sync_subscription'


# ---------------------------------------------------------------------------
# classify_event — subscriptions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('event_type', [
    'INITIAL_PURCHASE', 'RENEWAL', 'UNCANCELLATION', 'PRODUCT_CHANGE',
    'CANCELLATION', 'EXPIRATION', 'BILLING_ISSUE', 'SUBSCRIPTION_PAUSED',
])
def test_subscription_events_all_sync(event_type):
    d = classify_event(subscription_event(type=event_type), expect_production=True)
    assert d.action == 'sync_subscription', event_type


def test_trial_purchase_is_treated_as_an_active_subscription():
    """period_type TRIAL still carries the entitlement — the app must gate the
    same during the 7 free days as after."""
    d = classify_event(subscription_event(period_type='TRIAL'), expect_production=True)
    assert d.action == 'sync_subscription'
    assert d.fallback_tier == 'pro'
    assert d.period_type == 'TRIAL'


def test_expiration_falls_back_to_free():
    """No entitlements left on the event means the subscription lapsed."""
    d = classify_event(
        subscription_event(type='EXPIRATION', entitlement_ids=[]),
        expect_production=True,
    )
    assert d.fallback_tier == 'free'


def test_elite_event_falls_back_to_elite():
    d = classify_event(subscription_event(entitlement_ids=['elite']),
                       expect_production=True)
    assert d.fallback_tier == 'elite'


# ---------------------------------------------------------------------------
# classify_event — guards
# ---------------------------------------------------------------------------

def test_sandbox_event_is_ignored_in_production():
    """Rylan test-buying on a sandbox Apple ID must never touch real balances."""
    d = classify_event(pack_event(environment='SANDBOX'), expect_production=True)
    assert d.action == 'ignore'
    assert d.reason == 'environment_mismatch'


def test_production_event_is_ignored_on_staging():
    """Both RevenueCat apps can point at the same URL; staging must not process
    a real customer's purchase."""
    d = classify_event(pack_event(environment='PRODUCTION'), expect_production=False)
    assert d.action == 'ignore'
    assert d.reason == 'environment_mismatch'


def test_missing_app_user_id_is_ignored():
    """app_user_id IS our Firebase uid. Without it there is nobody to credit."""
    d = classify_event(pack_event(app_user_id=None), expect_production=True)
    assert d.action == 'ignore'
    assert d.reason == 'no_app_user_id'


def test_anonymous_revenuecat_id_is_ignored():
    """A purchase made before Purchases.logIn() lands on RevenueCat's own
    generated id, which is not a Firebase uid and must not be credited."""
    d = classify_event(
        pack_event(app_user_id='$RCAnonymousID:8e9f2a1b4c'),
        expect_production=True,
    )
    assert d.action == 'ignore'
    assert d.reason == 'anonymous_app_user_id'


def test_test_event_from_the_dashboard_is_ignored():
    """RevenueCat's "Send test webhook" button fires type TEST."""
    d = classify_event(pack_event(type='TEST'), expect_production=True)
    assert d.action == 'ignore'
    assert d.reason == 'unhandled_event_type'


def test_transfer_event_syncs_the_destination_uid():
    """TRANSFER fires when Purchases.logIn() merges an anonymous purchase (an
    offer-code redemption completed before the buyer ever logged into the
    app) into their real Firebase uid. It must sync that uid's tier from
    RevenueCat's live snapshot, not get dropped like the anonymous purchase
    that spawned it correctly was."""
    d = classify_event(
        subscription_event(
            type='TRANSFER',
            app_user_id='$RCAnonymousID:8e9f2a1b4c',
            transferred_to=['firebase-uid-real'],
        ),
        expect_production=True,
    )
    assert d.action == 'sync_subscription'
    assert d.uid == 'firebase-uid-real'


def test_transfer_event_with_no_transferred_to_keeps_the_event_uid():
    """Defensive: if RevenueCat ever omits transferred_to, fall back to the
    event's own app_user_id rather than syncing nobody."""
    d = classify_event(
        subscription_event(type='TRANSFER', app_user_id='firebase-uid-real'),
        expect_production=True,
    )
    assert d.action == 'sync_subscription'
    assert d.uid == 'firebase-uid-real'


def test_transfer_event_to_multiple_destinations_uses_the_last_one():
    """transferred_to is documented as an array; the last entry is the
    current destination."""
    d = classify_event(
        subscription_event(
            type='TRANSFER',
            app_user_id='$RCAnonymousID:8e9f2a1b4c',
            transferred_to=['firebase-uid-old', 'firebase-uid-newest'],
        ),
        expect_production=True,
    )
    assert d.uid == 'firebase-uid-newest'


# ---------------------------------------------------------------------------
# tier_from_entitlements
# ---------------------------------------------------------------------------

def test_elite_outranks_pro_when_both_are_active():
    assert tier_from_entitlements(['pro', 'elite']) == 'elite'


def test_single_entitlement_maps_straight_through():
    assert tier_from_entitlements(['pro']) == 'pro'
    assert tier_from_entitlements(['elite']) == 'elite'


def test_no_entitlements_means_free():
    assert tier_from_entitlements([]) == 'free'
    assert tier_from_entitlements(None) == 'free'


def test_unrecognized_entitlement_does_not_grant_a_tier():
    assert tier_from_entitlements(['some_future_addon']) == 'free'


# ---------------------------------------------------------------------------
# has_active_stripe_subscription — the Apple/Stripe guardrail
# ---------------------------------------------------------------------------

def test_active_stripe_subscriber_is_protected():
    user = {'stripeSubscriptionId': 'sub_123', 'subscriptionStatus': 'active'}
    assert has_active_stripe_subscription(user) is True


def test_trialing_stripe_subscriber_is_protected():
    user = {'stripeSubscriptionId': 'sub_123', 'subscriptionStatus': 'trialing'}
    assert has_active_stripe_subscription(user) is True


def test_past_due_stripe_subscriber_is_protected():
    """Still Stripe's customer — a dunning failure is not a cancellation."""
    user = {'stripeSubscriptionId': 'sub_123', 'subscriptionStatus': 'past_due'}
    assert has_active_stripe_subscription(user) is True


def test_canceled_stripe_subscription_does_not_block_apple():
    user = {'stripeSubscriptionId': 'sub_123', 'subscriptionStatus': 'canceled'}
    assert has_active_stripe_subscription(user) is False


def test_user_who_never_paid_on_the_web_does_not_block_apple():
    assert has_active_stripe_subscription({}) is False


def test_apple_sourced_user_does_not_block_itself():
    """Once Apple owns the subscription, later Apple events must still apply
    even if a stale Stripe id is lying around on the doc."""
    user = {
        'stripeSubscriptionId': 'sub_old',
        'subscriptionStatus': 'active',
        'subscriptionSource': 'apple',
    }
    assert has_active_stripe_subscription(user) is False


# ---------------------------------------------------------------------------
# clawback_amounts
# ---------------------------------------------------------------------------

def test_clawback_removes_credits_that_are_still_there():
    new_bonus, shortfall = clawback_amounts(bonus=1000, amount=400)
    assert new_bonus == 600
    assert shortfall == 0


def test_clawback_clamps_at_zero_when_credits_were_already_spent():
    """The decision the spec left open: clamp, don't carry debt into the next
    purchase. A student who refunds after spending keeps what they used; we
    record the shortfall for support instead of booby-trapping their next
    top-up."""
    new_bonus, shortfall = clawback_amounts(bonus=100, amount=400)
    assert new_bonus == 0
    assert shortfall == 300


def test_clawback_of_a_fully_spent_pack_reports_the_whole_amount():
    new_bonus, shortfall = clawback_amounts(bonus=0, amount=150)
    assert new_bonus == 0
    assert shortfall == 150
