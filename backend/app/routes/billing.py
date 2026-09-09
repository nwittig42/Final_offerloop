"""
Billing and tier management routes
"""
import stripe
from datetime import datetime
from flask import Blueprint, request, jsonify

from ..extensions import require_firebase_auth
from app.services.auth import check_and_reset_credits
from app.services.stripe_client import (
    create_checkout_session,
    handle_stripe_webhook,
    create_portal_session,
    handle_checkout_completed,
    update_subscription_tier,
    apply_post_checkout_upsell,
    record_post_checkout_upsell_decline,
    user_is_student_eligible,
)
from app.services.refund_service import create_refund_request
from app.services.topup_service import create_topup_session
from app.services.season_pass_service import create_season_pass_session
from app.services.credit_ledger import get_balance_breakdown
from app.config import (
    TIER_CONFIGS,
    CREDIT_COSTS,
    COFFEE_CHAT_CREDITS,
    STRIPE_PRICE_CATALOG,
    SLIDER_STOPS,
    ANNUAL_PRICING,
    SEASON_PASS,
    TOPUP_PACKS,
    STRIPE_COUPONS,
    TRIAL_DAYS_STUDENT,
    TRIAL_DAYS_NON_STUDENT,
    TRIAL_CREDITS,
    TRIAL_DAILY_EXPORT_CAP,
    TRIAL_CC_EXTENSION_DAYS,
    FREE_DRAFTS_PER_MONTH,
)
from ..extensions import get_db

billing_bp = Blueprint('billing', __name__, url_prefix='/api')


@billing_bp.route('/tier-config')
def get_tier_config():
    """Full pricing source of truth — tiers, credit costs, Stripe SKU matrix, slider stops, trial config.

    Public (no auth). Frontend caches via React Query (1h staleTime).
    All allocations and slider stops are runtime-tunable here; the frontend
    falls back to constants.ts defaults if this endpoint is unreachable.
    """
    # Active promo codes — only surface ones where a real Stripe coupon ID is wired.
    # The frontend gates the "20% off" badge on this list being non-empty.
    active_promos = {k: v for k, v in STRIPE_COUPONS.items() if v}

    return jsonify({
        'tiers': TIER_CONFIGS,
        'credit_costs': CREDIT_COSTS,
        'stripe_catalog': STRIPE_PRICE_CATALOG,
        'slider_stops': SLIDER_STOPS,
        'annual_pricing': ANNUAL_PRICING,
        'season_pass': SEASON_PASS,
        'topup_packs': TOPUP_PACKS,
        'active_promos': active_promos,
        'trial': {
            'days_student':       TRIAL_DAYS_STUDENT,
            'days_non_student':   TRIAL_DAYS_NON_STUDENT,
            'credits':            TRIAL_CREDITS,   # one-time grant (single-batch model)
            'daily_export_cap':   TRIAL_DAILY_EXPORT_CAP,
            'cc_extension_days':  TRIAL_CC_EXTENSION_DAYS,
        },
        'free_drafts_per_month': FREE_DRAFTS_PER_MONTH,
    })


@billing_bp.route('/active-promos')
def get_active_promos():
    """Return the list of live Stripe coupon IDs.

    Frontend uses this to decide whether to render urgency badges and exit-intent
    discount popups. Empty if no coupons are wired — no fake scarcity.
    """
    return jsonify({
        'promos': {k: v for k, v in STRIPE_COUPONS.items() if v},
    })


@billing_bp.route('/billing/accept-post-checkout-upsell', methods=['POST'])
@require_firebase_auth
def accept_post_checkout_upsell():
    """Pro → Elite upsell accept handler.

    Switches the user's Stripe subscription to Elite (no proration), creates a
    one-time $10 invoice item and charges the saved card, and bumps the user's
    credit allocation to Elite's monthly pool. Idempotent — second call returns
    409 instead of double-charging.

    See stripe_client.apply_post_checkout_upsell for the full mechanic + why
    we use invoice-item + subscription.modify instead of a coupon.
    """
    user_id = request.firebase_user.get('uid')
    if not user_id:
        return jsonify({'error': 'unauthenticated'}), 401

    result = apply_post_checkout_upsell(user_id)
    if not result.get('ok'):
        err = result.get('error', 'unknown')
        # Map specific errors to appropriate HTTP codes
        if err == 'already_accepted':
            return jsonify(result), 409
        if err in ('user_not_found', 'no_active_subscription'):
            return jsonify(result), 404
        if err == 'subscription_has_existing_discount':
            return jsonify(result), 409
        return jsonify(result), 500

    return jsonify(result), 200


@billing_bp.route('/billing/create-topup-session', methods=['POST'])
@require_firebase_auth
def create_topup_session_route():
    """Start a Stripe Checkout Session for a one-time top-up credit pack.

    Body: { packId: 'starter' | 'best' | 'bulk' }
    Returns: { ok, session_id, url } on success, { ok: False, error } otherwise.

    On payment success, the webhook handler in stripe_client.py routes the
    completed session to topup_service.apply_topup_purchase, which adds the
    credits to the user's bonusCredits bucket (never expires).
    """
    user_id = request.firebase_user.get('uid')
    user_email = request.firebase_user.get('email')
    if not user_id:
        return jsonify({'error': 'unauthenticated'}), 401

    data = request.get_json() or {}
    pack_id = data.get('packId') or data.get('pack_id')
    if not pack_id:
        return jsonify({'error': 'packId_required'}), 400

    # Derive default success/cancel URLs from the request origin
    base = request.url_root.rstrip('/')
    if 'localhost' in base:
        base = 'http://localhost:8080'
    elif base.endswith('/api'):
        base = base[:-4]
    success_url = data.get('successUrl') or f"{base}/payment-success?topup={pack_id}&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = data.get('cancelUrl') or f"{base}/pricing"

    result = create_topup_session(user_id, user_email or '', pack_id, success_url, cancel_url)
    if not result.get('ok'):
        err = result.get('error', 'unknown')
        if err == 'unknown_pack':
            return jsonify(result), 404
        if err == 'stripe_sku_not_wired':
            return jsonify(result), 503
        return jsonify(result), 500

    return jsonify({'sessionId': result['session_id'], 'url': result['url']}), 200


@billing_bp.route('/billing/create-season-pass-session', methods=['POST'])
@require_firebase_auth
def create_season_pass_session_route():
    """Start a Stripe Checkout Session for a one-time Recruiting Season Pass.

    Body: { audience?: 'student' | 'list' }
    Returns: { sessionId, url } on success, { ok: False, error } otherwise.

    On payment success, the webhook handler in stripe_client.py routes the
    completed session to season_pass_service.apply_season_pass_purchase, which
    grants the season_pass tier for SEASON_PASS['months'] months.
    """
    user_id = request.firebase_user.get('uid')
    user_email = request.firebase_user.get('email')
    if not user_id:
        return jsonify({'error': 'unauthenticated'}), 401

    data = request.get_json() or {}
    audience = data.get('audience') or 'list'

    # Student pricing is .edu-gated on the server, not just in the UI. If the
    # caller asks for student audience but doesn't clear the eligibility bar,
    # silently downgrade to list pricing rather than fail — season pass has no
    # "no discount" cancel path in the UI, so falling through to list is the
    # correct fallback.
    if audience == 'student' and not user_is_student_eligible(user_id, user_email or ''):
        print(f"[SeasonPass] downgrading student->list: user_id={user_id} email={user_email}")
        audience = 'list'

    base = request.url_root.rstrip('/')
    if 'localhost' in base:
        base = 'http://localhost:8080'
    elif base.endswith('/api'):
        base = base[:-4]
    success_url = data.get('successUrl') or f"{base}/payment-success?season_pass=1&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = data.get('cancelUrl') or f"{base}/pricing"

    result = create_season_pass_session(user_id, user_email or '', audience, success_url, cancel_url)
    if not result.get('ok'):
        err = result.get('error', 'unknown')
        if err == 'stripe_sku_not_wired':
            return jsonify(result), 503
        return jsonify(result), 500

    return jsonify({'sessionId': result['session_id'], 'url': result['url']}), 200


@billing_bp.route('/credits/breakdown', methods=['GET'])
@require_firebase_auth
def credits_breakdown_route():
    """Read-only credit breakdown — monthly / bonus / promo / total.

    The frontend UsageMeter uses this to show "You have X credits + Y bonus
    that never expire" instead of a single opaque number.
    """
    user_id = request.firebase_user.get('uid')
    if not user_id:
        return jsonify({'error': 'unauthenticated'}), 401
    return jsonify(get_balance_breakdown(user_id)), 200


@billing_bp.route('/billing/request-refund', methods=['POST'])
@require_firebase_auth
def request_refund_route():
    """Submit a refund request.

    Policy:
      - Pro / Elite (monthly + annual): 7-day window from first charge
      - Season Pass: 14-day window WITH a <50% month-1 credits-used cap
      - Top-up credit packs: non-refundable (credits never expire)
      - Post-checkout upsell add-on: falls under standard 7-day window
      - Anti-abuse: requests are still subject to discretionary review per ToS

    Body: { invoiceId: string, reason?: string }
    Returns: { ok, eligible, request_id, product_type, amount, message }
    """
    user_id = request.firebase_user.get('uid')
    user_email = request.firebase_user.get('email')
    if not user_id:
        return jsonify({'error': 'unauthenticated'}), 401

    data = request.get_json() or {}
    invoice_id = data.get('invoiceId') or data.get('invoice_id')
    reason = data.get('reason', '')

    if not invoice_id:
        return jsonify({'error': 'invoiceId_required'}), 400

    result = create_refund_request(user_id, user_email, invoice_id, reason)
    if not result.get('ok'):
        err = result.get('error', 'unknown')
        if err in ('user_not_found', 'invoice_not_found'):
            return jsonify(result), 404
        if err == 'invoice_not_yours':
            return jsonify(result), 403
        return jsonify(result), 500

    return jsonify(result), 200


@billing_bp.route('/billing/decline-post-checkout-upsell', methods=['POST'])
@require_firebase_auth
def decline_post_checkout_upsell():
    """Pro → Elite upsell decline handler.

    Just marks `upsellShownAt` + `upsellDeclinedAt` so the modal never shows
    again. No second-chance email — chasing decline sleazes the brand.
    """
    user_id = request.firebase_user.get('uid')
    if not user_id:
        return jsonify({'error': 'unauthenticated'}), 401

    result = record_post_checkout_upsell_decline(user_id)
    if not result.get('ok'):
        return jsonify(result), 500
    return jsonify(result), 200


@billing_bp.route('/tier-info')
def get_tier_info():
    """Get information about available tiers"""
    return jsonify({
        'tiers': {
            'free': {
                'name': 'Free',
                'max_contacts': TIER_CONFIGS['free']['max_contacts'],
                'credits': TIER_CONFIGS['free']['credits'],
                'time_saved_minutes': TIER_CONFIGS['free']['time_saved_minutes'],
                'description': TIER_CONFIGS['free']['description'],
                'features': [
                    f"{TIER_CONFIGS['free']['credits']} credits",
                    f"Estimated time saved: {TIER_CONFIGS['free']['time_saved_minutes']} minutes",
                    "Interesting personalized emails",
                    "Mutual interest detection",
                    "Company-specific conversation starters",
                    "Try out platform risk free"
                ]
            },
            'pro': {
                'name': 'Pro',
                'max_contacts': TIER_CONFIGS['pro']['max_contacts'],
                'credits': TIER_CONFIGS['pro']['credits'],
                'time_saved_minutes': TIER_CONFIGS['pro']['time_saved_minutes'],
                'description': TIER_CONFIGS['pro']['description'],
                'features': [
                    f"{TIER_CONFIGS['pro']['credits']} credits",
                    f"Estimated time saved: {TIER_CONFIGS['pro']['time_saved_minutes']} minutes",
                    "Same quality interesting emails as Free",
                    "Resume-enhanced personalization",
                    "Directory permanently saves",
                    "Priority Support",
                    "Advanced features"
                ]
            },
            'elite': {
                'name': 'Elite',
                'max_contacts': TIER_CONFIGS['elite']['max_contacts'],
                'credits': TIER_CONFIGS['elite']['credits'],
                'time_saved_minutes': TIER_CONFIGS['elite']['time_saved_minutes'],
                'description': TIER_CONFIGS['elite']['description'],
                'features': [
                    f"{TIER_CONFIGS['elite']['credits']} credits",
                    f"Estimated time saved: {TIER_CONFIGS['elite']['time_saved_minutes']} minutes",
                    "Everything in Pro, plus:",
                    "Unlimited Coffee Chat Prep",
                    "Unlimited Interview Prep",
                    "Priority queue for contact generation",
                    "Personalized outreach templates",
                    "Weekly personalized firm insights",
                    "Early access to new AI tools"
                ]
            }
        }
    })


@billing_bp.route('/check-credits', methods=['GET'])
@require_firebase_auth
def check_credits():
    """Check user's current credits"""
    try:
        db = get_db()
        user_email = request.firebase_user.get('email')
        user_id = request.firebase_user.get('uid')
        
        if db and user_id:
            # Try using user ID first (more reliable)
            user_ref = db.collection('users').document(user_id)
            user_doc = user_ref.get()
            
            if not user_doc.exists and user_email:
                # Fallback to email-based lookup
                user_ref = db.collection('users').document(user_email.replace('@', '_at_'))
                user_doc = user_ref.get()
            
            if user_doc.exists:
                user_data = user_doc.to_dict()
                credits = check_and_reset_credits(user_ref, user_data)
                tier = user_data.get('tier', 'free')
                max_credits = user_data.get('maxCredits', TIER_CONFIGS.get(tier, TIER_CONFIGS['free'])['credits'])
                
                # Calculate searches remaining
                searches_remaining = credits // 15
                
                return jsonify({
                    'credits': credits,
                    'max_credits': max_credits,
                    'searches_remaining': searches_remaining,
                    'tier': tier,
                    'user_email': user_email,
                    'credit_costs': {'coffee_chat_prep': COFFEE_CHAT_CREDITS},
                })
            else:
                # User doesn't exist yet - return default free tier credits
                return jsonify({
                    'credits': 300,
                    'max_credits': 300,
                    'searches_remaining': 20,
                    'tier': 'free',
                    'user_email': user_email,
                    'credit_costs': {'coffee_chat_prep': COFFEE_CHAT_CREDITS},
                })

        # If no Firebase, return defaults
        return jsonify({
            'credits': 0,
            'max_credits': 300,
            'searches_remaining': 0,
            'tier': 'free',
            'user_email': user_email,
            'credit_costs': {'coffee_chat_prep': COFFEE_CHAT_CREDITS},
        })
        
    except Exception as e:
        print(f"Check credits error: {e}")
        return jsonify({'error': 'Failed to check credits'}), 500


@billing_bp.route('/user/update-tier', methods=['POST'])
@require_firebase_auth
def update_user_tier():
    """
    Update user tier and credits - ADMIN/INTERNAL USE ONLY
    Requires the caller's UID to be in the Firestore 'admins' collection,
    OR a valid ADMIN_API_SECRET header for server-to-server calls (e.g. Stripe webhooks).
    """
    try:
        import os
        db = get_db()

        # --- Admin authorization check ---
        admin_secret = os.getenv("ADMIN_API_SECRET")
        provided_secret = request.headers.get("X-Admin-Secret", "")
        caller_uid = request.firebase_user.get('uid')

        is_admin = False
        # Check 1: server-to-server via shared secret
        if admin_secret and provided_secret and provided_secret == admin_secret:
            is_admin = True
        # Check 2: caller is in the admins collection
        elif db and caller_uid:
            admin_doc = db.collection('admins').document(caller_uid).get()
            if admin_doc.exists:
                is_admin = True

        if not is_admin:
            return jsonify({'error': 'Forbidden: admin access required'}), 403

        data = request.get_json() or {}
        user_email = data.get('userEmail', '').strip()
        tier = data.get('tier', '').strip()
        credits = data.get('credits', 0)
        max_credits = data.get('maxCredits', 0)

        if not user_email or not tier:
            return jsonify({'error': 'User email and tier required'}), 400

        # Validate tier
        if tier not in ['free', 'pro', 'elite']:
            return jsonify({'error': 'Invalid tier. Must be "free", "pro", or "elite"'}), 400

        # Store user tier info
        if db:
            user_ref = db.collection('users').document(user_email.replace('@', '_at_'))
            user_ref.set({
                'email': user_email,
                'tier': tier,
                'credits': credits,
                'maxCredits': max_credits,
                'updated_at': datetime.now().isoformat()
            }, merge=True)

        return jsonify({
            'success': True,
            'user': {
                'email': user_email,
                'tier': tier,
                'credits': credits,
                'maxCredits': max_credits
            }
        })

    except Exception as e:
        print(f"User tier update error: {e}")
        return jsonify({'error': 'Failed to update tier'}), 500


@billing_bp.route('/create-checkout-session', methods=['POST'])
@require_firebase_auth
def create_checkout_session_route():
    """Create Stripe checkout session for Pro upgrade"""
    return create_checkout_session()


@billing_bp.route('/complete-upgrade', methods=['POST'])
@require_firebase_auth
def complete_upgrade():
    """Complete Pro upgrade - called by frontend after successful payment"""
    try:
        db = get_db()
        data = request.get_json() or {}
        session_id = data.get('sessionId')
        
        user_id = request.firebase_user['uid']
        user_email = request.firebase_user.get('email')
        
        print(f"\n💳 Completing upgrade for {user_email}")
        print(f"   User ID: {user_id}")
        print(f"   Session ID: {session_id}")
        
        if not session_id:
            print("   ⚠️  No session ID provided")
            return jsonify({'error': 'Session ID is required'}), 400
        
        # Verify with Stripe
        subscription_id = None
        customer_id = None
        stripe_session = None
        
        try:
            from app.config import STRIPE_SECRET_KEY
            if not STRIPE_SECRET_KEY:
                print("   ⚠️  Stripe not configured, proceeding without verification")
            else:
                stripe.api_key = STRIPE_SECRET_KEY
                session = stripe.checkout.Session.retrieve(session_id)
                stripe_session = session
                print(f"   Payment status: {session.payment_status}")
                print(f"   Session mode: {session.mode}")
                print(f"   Customer: {session.customer}")
                print(f"   Subscription: {session.subscription}")
                
                # 'paid' = money moved now; 'no_payment_required' = trial
                # checkout with $0 due today (the norm since the 2026-07-27
                # 7-day card-on-file trial). Both mean checkout completed.
                if session.payment_status not in ('paid', 'no_payment_required'):
                    print(f"   ❌ Payment not completed. Status: {session.payment_status}")
                    return jsonify({
                        'error': f'Payment not completed. Status: {session.payment_status}',
                        'payment_status': session.payment_status
                    }), 400
                
                subscription_id = session.subscription
                customer_id = session.customer
                print(f"   ✅ Payment verified - Customer: {customer_id}, Subscription: {subscription_id}")
                    
        except stripe.error.StripeError as e:
            print(f"   ❌ Stripe error: {e}")
            return jsonify({
                'error': f'Stripe verification failed: {str(e)}',
                'stripe_error': str(e)
            }), 400
        except Exception as e:
            print(f"   ❌ Stripe session retrieval failed (non-Stripe error): {e}")
            import traceback
            traceback.print_exc()
            # Hard fail: never upgrade without payment verification.
            # Stripe webhooks handle eventual consistency if this endpoint is unavailable.
            return jsonify({
                'error': 'Payment verification temporarily unavailable. Your subscription will activate automatically via webhook.',
                'retry': True
            }), 503
        
        # Update Firebase
        if not db:
            print("   ❌ Firebase not initialized")
            return jsonify({'error': 'Database not available'}), 500
        
        user_ref = db.collection('users').document(user_id)
        
        # Check if user exists first
        user_doc = user_ref.get()
        if not user_doc.exists:
            print(f"   ⚠️  User document doesn't exist, creating new one")
        
        # Determine tier from Stripe subscription price ID; use session metadata as fallback (e.g. Elite price ID mismatch)
        from app.services.stripe_client import get_tier_from_price_id
        
        tier = 'pro'  # Default
        tier_from_metadata = None
        price_id = None
        sub_status = 'active'  # Fallback; 'trialing' during the 7-day trial
        if stripe_session:
            tier_from_metadata = (stripe_session.metadata or {}).get('tier')
        if subscription_id:
            try:
                subscription = stripe.Subscription.retrieve(subscription_id)
                sub_status = subscription.status or 'active'
                if subscription.items.data:
                    price_id = subscription.items.data[0].price.id
                    tier_from_stripe = get_tier_from_price_id(price_id)
                    if tier_from_metadata in ('pro', 'elite'):
                        tier = tier_from_metadata
                        if tier != tier_from_stripe:
                            print(f"   ⚠️ Tier mismatch: metadata={tier_from_metadata}, price_id={price_id} -> {tier_from_stripe}. Using metadata tier {tier}.")
                    else:
                        tier = tier_from_stripe
            except Exception as e:
                print(f"   ⚠️  Error retrieving subscription: {e}")
                if tier_from_metadata in ('pro', 'elite'):
                    tier = tier_from_metadata
        elif tier_from_metadata in ('pro', 'elite'):
            tier = tier_from_metadata

        print(f"   ✅ Tier assignment: {tier} (metadata={tier_from_metadata}, price_id={price_id})")
        
        tier_config = TIER_CONFIGS.get(tier, TIER_CONFIGS['pro'])
        credits = tier_config['credits']

        update_data = {
            'tier': tier,
            'subscriptionTier': tier,
            'credits': credits,
            'maxCredits': credits,
            # Actual Stripe status so trial subscribers show 'trialing', not
            # 'active' (the webhook writes the same value; whichever runs last
            # must not clobber it with a wrong hardcode).
            'subscriptionStatus': sub_status,
            'upgraded_at': datetime.now().isoformat(),
            'lastCreditReset': datetime.now().isoformat(),
            'coffeeChatPrepsUsed': 0,
            'coffeeChatPrepsLimit': tier_config['coffee_chat_preps'],
        }
        if sub_status == 'trialing':
            # Consume the one-per-account trial token here too, in case the
            # checkout.session.completed webhook is delayed or dropped.
            update_data['trialUsedAt'] = datetime.now()

        if customer_id:
            update_data['stripeCustomerId'] = customer_id
        if subscription_id:
            update_data['stripeSubscriptionId'] = subscription_id

        # Read the pre-update doc for upsell history — the post-checkout
        # Pro→Elite offer is shown at most once per account.
        already_saw_upsell = bool((user_doc.to_dict() or {}).get('upsellShownAt')) if user_doc.exists else False

        user_ref.set(update_data, merge=True)
        print(f"   ✅ Updated Firebase user document")

        print(f"✅ Successfully upgraded {user_email} to {tier.capitalize()} (status={sub_status})!")

        return jsonify({
            'success': True,
            'message': f'Successfully upgraded to {tier.capitalize()}',
            'tier': tier,
            'credits': credits,
            'subscriptionId': subscription_id,
            'customerId': customer_id,
            'subscriptionStatus': sub_status,
            'paymentStatus': stripe_session.payment_status if stripe_session else 'paid',
            'upsellShownAt': already_saw_upsell,
        })
        
    except KeyError as e:
        print(f"❌ Missing required field: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Missing required field in request'}), 400
    except Exception as e:
        print(f"❌ Upgrade completion error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Failed to complete upgrade'}), 500


@billing_bp.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhook events"""
    return handle_stripe_webhook()


@billing_bp.route('/update-subscription', methods=['POST'])
@require_firebase_auth
def update_subscription():
    """Update existing subscription tier (e.g., Pro → Elite) via Stripe modify"""
    return update_subscription_tier()


@billing_bp.route('/create-portal-session', methods=['POST'])
@require_firebase_auth
def create_portal_session_route():
    """Create Stripe customer portal session"""
    return create_portal_session()


@billing_bp.route('/subscription-status', methods=['GET'])
@require_firebase_auth
def subscription_status():
    """Get subscription status"""
    try:
        db = get_db()
        user_id = request.firebase_user.get('uid')
        
        if not db:
            return jsonify({'subscribed': False}), 200
        
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return jsonify({'subscribed': False, 'tier': 'free'}), 200
        
        user_data = user_doc.to_dict()
        tier = user_data.get('tier', 'free')
        subscription_id = user_data.get('stripeSubscriptionId')
        # Use actual status from Firestore so trialing subscribers see 'trialing'
        raw_status = user_data.get('subscriptionStatus')
        status = raw_status if tier in ['pro', 'elite'] and raw_status else ('active' if tier in ['pro', 'elite'] else 'inactive')
        
        return jsonify({
            'subscribed': tier in ['pro', 'elite'],
            'tier': tier,
            'status': status,
            'hasSubscription': tier in ['pro', 'elite'],
            'subscriptionId': subscription_id,
            'credits': user_data.get('credits', 0)
        })
        
    except Exception as e:
        print(f"Subscription status error: {e}")
        return jsonify({'subscribed': False, 'tier': 'free'}), 200


@billing_bp.route('/user/subscription', methods=['GET'])
@require_firebase_auth
def get_user_subscription():
    """Get user subscription tier and usage information"""
    try:
        db = get_db()
        user_id = request.firebase_user.get('uid')
        
        if not db or not user_id:
            return jsonify({
                'tier': 'free',
                'credits': 0,
                'maxCredits': TIER_CONFIGS['free']['credits'],
                'alumniSearchesUsed': 0,
                'alumniSearchesLimit': TIER_CONFIGS['free']['alumni_searches'],
                'coffeeChatPrepsUsed': 0,
                'coffeeChatPrepsLimit': TIER_CONFIGS['free']['coffee_chat_preps'],
            }), 200

        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()

        if not user_doc.exists:
            return jsonify({
                'tier': 'free',
                'credits': 0,
                'maxCredits': TIER_CONFIGS['free']['credits'],
                'alumniSearchesUsed': 0,
                'alumniSearchesLimit': TIER_CONFIGS['free']['alumni_searches'],
                'coffeeChatPrepsUsed': 0,
                'coffeeChatPrepsLimit': TIER_CONFIGS['free']['coffee_chat_preps'],
            }), 200
        
        user_data = user_doc.to_dict()
        tier = user_data.get('subscriptionTier') or user_data.get('tier', 'free')
        tier_config = TIER_CONFIGS.get(tier, TIER_CONFIGS['free'])
        
        # Check and reset usage if needed
        from app.services.auth import check_and_reset_usage
        check_and_reset_usage(user_ref, user_data)
        user_doc = user_ref.get()  # Refresh after potential reset
        user_data = user_doc.to_dict()
        
        return jsonify({
            'tier': tier,
            'credits': user_data.get('credits', 0),
            # Always report the tier's CURRENT configured cap, not the stored
            # maxCredits. Legacy accounts carry a stale maxCredits from before
            # the credit restructuring (e.g. an elite account still showing the
            # old 3000 cap), which froze the sidebar credit bar. Deriving from
            # TIER_CONFIGS makes the cap self-correct on every read.
            'maxCredits': tier_config['credits'],
            'alumniSearchesUsed': user_data.get('alumniSearchesUsed', 0),
            'alumniSearchesLimit': tier_config['alumni_searches'],
            'coffeeChatPrepsUsed': user_data.get('coffeeChatPrepsUsed', 0),
            'coffeeChatPrepsLimit': tier_config['coffee_chat_preps'],
            'resumeFileName': user_data.get('resumeFileName'),
        })
        
    except Exception as e:
        print(f"Get subscription error: {e}")
        return jsonify({'error': 'Failed to retrieve subscription info'}), 500


@billing_bp.route('/user/check-feature', methods=['POST'])
@require_firebase_auth
def check_feature():
    """Check if user can access a feature"""
    try:
        db = get_db()
        user_id = request.firebase_user.get('uid')
        data = request.get_json() or {}
        feature = data.get('feature')
        
        if not feature:
            return jsonify({'error': 'Feature name is required'}), 400
        
        if not db or not user_id:
            return jsonify({'allowed': False, 'reason': 'User not found'}), 200
        
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return jsonify({'allowed': False, 'reason': 'User not found'}), 200
        
        user_data = user_doc.to_dict()
        tier = user_data.get('subscriptionTier') or user_data.get('tier', 'free')
        tier_config = TIER_CONFIGS.get(tier, TIER_CONFIGS['free'])
        
        # Check feature access based on tier
        from app.services.auth import can_access_feature
        allowed, reason = can_access_feature(tier, feature, user_data, tier_config)
        
        return jsonify({
            'allowed': allowed,
            'reason': reason,
            'tier': tier
        })
        
    except Exception as e:
        print(f"Check feature error: {e}")
        return jsonify({'error': 'Failed to check feature access'}), 500


@billing_bp.route('/user/increment-usage', methods=['POST'])
@require_firebase_auth
def increment_usage():
    """Increment usage counter for a feature"""
    try:
        db = get_db()
        user_id = request.firebase_user.get('uid')
        data = request.get_json() or {}
        feature = data.get('feature')
        
        if not feature:
            return jsonify({'error': 'Feature name is required'}), 400
        
        if not db or not user_id:
            return jsonify({'error': 'Database not available'}), 500
        
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return jsonify({'error': 'User not found'}), 404
        
        user_data = user_doc.to_dict()
        
        # Check and reset usage if needed
        from app.services.auth import check_and_reset_usage
        check_and_reset_usage(user_ref, user_data)
        user_doc = user_ref.get()  # Refresh
        user_data = user_doc.to_dict()
        
        # Map feature names to field names
        field_map = {
            'alumni_search': 'alumniSearchesUsed',
            'coffee_chat_prep': 'coffeeChatPrepsUsed',
        }
        
        field_name = field_map.get(feature)
        if not field_name:
            return jsonify({'error': f'Unknown feature: {feature}'}), 400
        
        # Increment usage
        current_usage = user_data.get(field_name, 0)
        user_ref.update({
            field_name: current_usage + 1,
            'updatedAt': datetime.now().isoformat()
        })
        
        return jsonify({
            'success': True,
            'usage': current_usage + 1
        })
        
    except Exception as e:
        print(f"Increment usage error: {e}")
        return jsonify({'error': 'Failed to update usage'}), 500


@billing_bp.route('/debug/check-upgrade/<user_id>', methods=['GET'])
@require_firebase_auth
def debug_check_upgrade(user_id):
    """Debug endpoint to check user upgrade status (dev only)"""
    import os
    is_dev = (
        os.getenv("FLASK_ENV") == "development"
        or os.getenv("FLASK_DEBUG") == "1"
    )
    if not is_dev:
        return jsonify({'error': 'Not available in production'}), 404

    try:
        db = get_db()
        if not db:
            return jsonify({'error': 'Firebase not initialized'}), 500

        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()

        if not user_doc.exists:
            return jsonify({'error': 'User not found'}), 404

        user_data = user_doc.to_dict()

        return jsonify({
            'user_id': user_id,
            'tier': user_data.get('tier', 'free'),
            'credits': user_data.get('credits', 0),
            'maxCredits': user_data.get('maxCredits', 0),
            'subscriptionId': user_data.get('stripeSubscriptionId'),
            'customerId': user_data.get('stripeCustomerId'),
            'upgraded_at': user_data.get('upgraded_at')
        })

    except Exception as e:
        print(f"Debug check-upgrade error: {e}")
        return jsonify({'error': 'Failed to check upgrade status'}), 500

