/**
 * Analytics Helper Functions
 * 
 * Centralized wrapper for PostHog analytics using the global event schema.
 * All analytics tracking should go through these helpers to ensure consistency.
 */

import posthog from './posthog';

/**
 * Track navigation clicks
 * @param nav_item - The navigation item that was clicked (e.g., "Contact Search", "Coffee Chat Prep")
 * @param location - Where the navigation click occurred (e.g., "sidebar", "header", "footer")
 * @param feature - Optional feature context (e.g., "contact_search", "coffee_chat_prep")
 */
export function trackNavClick(
  nav_item: string,
  location: string,
  feature?: string
): void {
  posthog.capture('nav_clicked', {
    nav_item,
    location,
    ...(feature && { feature }),
  });
}

/**
 * Track feature action completions
 * @param feature - The feature name (e.g., "contact_search", "coffee_chat_prep", "interview_prep")
 * @param action - The action performed (e.g., "search", "generate", "create")
 * @param success - Whether the action succeeded
 * @param credits_spent - Optional credits spent for this action
 * @param results_count - Optional number of results returned
 * @param additionalProps - Optional additional properties (must not contain sensitive data)
 */
export function trackFeatureActionCompleted(
  feature: string,
  action: string,
  success: boolean = true,
  options?: {
    credits_spent?: number;
    results_count?: number;
    [key: string]: any;
  }
): void {
  posthog.capture('feature_action_completed', {
    feature,
    action,
    success,
    ...(options?.credits_spent !== undefined && { credits_spent: options.credits_spent }),
    ...(options?.results_count !== undefined && { results_count: options.results_count }),
    ...(options && Object.fromEntries(
      Object.entries(options).filter(([key]) => 
        key !== 'credits_spent' && key !== 'results_count'
      )
    )),
  });
}

/**
 * Track content views
 * @param feature - The feature where content was viewed (e.g., "coffee_chat_prep", "interview_prep")
 * @param content_type - Type of content viewed (e.g., "pdf", "prep", "library_item")
 * @param content_id - Optional unique identifier for the content
 */
export function trackContentViewed(
  feature: string,
  content_type: string,
  content_id?: string
): void {
  posthog.capture('content_viewed', {
    feature,
    content_type,
    ...(content_id && { content_id }),
  });
}

/**
 * Track upgrade clicks
 * @param feature - The feature where the upgrade was clicked (e.g., "contact_search", "coffee_chat_prep")
 * @param from_action - Optional action context (e.g., "limit_reached", "paywall_shown")
 * @param from_location - Optional location context (e.g., "sidebar", "modal", "banner")
 * @param plan_selected - Optional plan that was selected (e.g., "pro", "elite")
 */
export function trackUpgradeClick(
  feature: string,
  options?: {
    from_action?: string;
    from_location?: string;
    plan_selected?: string;
  }
): void {
  posthog.capture('upgrade_clicked', {
    feature,
    ...(options?.from_action && { from_action: options.from_action }),
    ...(options?.from_location && { from_location: options.from_location }),
    ...(options?.plan_selected && { plan_selected: options.plan_selected }),
  });
}

/**
 * Track checkout completion
 * @param plan - The plan that was purchased (e.g., "pro", "elite")
 * @param feature - Optional feature that led to checkout
 */
export function trackCheckoutCompleted(
  plan: string,
  feature?: string
): void {
  posthog.capture('checkout_completed', {
    plan,
    ...(feature && { feature }),
  });
}

/**
 * Track errors
 * @param feature - The feature where the error occurred (e.g., "contact_search", "coffee_chat_prep")
 * @param action - The action that failed (e.g., "search", "generate", "download")
 * @param error_type - Type of error (e.g., "api_error", "network_error", "validation_error")
 * @param error_code - Optional error code
 */
export function trackError(
  feature: string,
  action: string,
  error_type: string,
  error_code?: string
): void {
  posthog.capture('error_occurred', {
    feature,
    action,
    error_type,
    ...(error_code && { error_code }),
  });
}


// ---------------------------------------------------------------------------
// Pricing overhaul telemetry (Wave 7) — surfaces the funnel signal we need to
// tune the lifecycle email sequences, the post-checkout upsell discount, the
// slider stops, and the top-up pack pricing.
// ---------------------------------------------------------------------------

/** User landed on the active-trial banner for the first time. */
export function trackTrialStarted(opts?: { tier?: string; from_location?: string }): void {
  posthog.capture('trial_started', {
    ...(opts?.tier && { tier: opts.tier }),
    ...(opts?.from_location && { from_location: opts.from_location }),
  });
}

/** Active trial dropped to Free — fired from TrialBanner when the polled
 *  status returns is_active=false after a previous active read. */
export function trackTrialExpired(opts?: { days_used?: number }): void {
  posthog.capture('trial_expired', {
    ...(opts?.days_used !== undefined && { days_used: opts.days_used }),
  });
}

/** Pricing-page exit-intent popup mounted (visible) — sequence 1 lead funnel. */
export function trackPricingPageExit(): void {
  posthog.capture('pricing_page_exit', {});
}

/** User submitted email in the pricing-page exit-intent popup. */
export function trackPricingEmailCaptured(opts?: { had_coupon?: boolean }): void {
  posthog.capture('pricing_email_captured', {
    ...(opts?.had_coupon !== undefined && { had_coupon: opts.had_coupon }),
  });
}

/** Slider stop changed on the pricing page (Pro or Elite credit dial). */
export function trackSliderDragged(opts: { tier: 'pro' | 'elite'; credits: number; from_index: number; to_index: number }): void {
  posthog.capture('slider_dragged', opts);
}

/** Season Pass CTA tapped. */
export function trackSeasonPassClicked(opts?: { audience?: 'student' | 'list' }): void {
  posthog.capture('season_pass_clicked', {
    ...(opts?.audience && { audience: opts.audience }),
  });
}

/** Top-up modal opened (from UsageMeter or pricing-page pack tile). */
export function trackTopupModalOpened(opts?: { from: string }): void {
  posthog.capture('topup_modal_opened', {
    ...(opts?.from && { from: opts.from }),
  });
}

/** User selected a top-up pack inside the modal (before payment). */
export function trackTopupPackSelected(opts: { pack_id: string; credits: number; price: number }): void {
  posthog.capture('topup_pack_selected', opts);
}

/** Top-up successfully purchased via Stripe checkout (fire on payment-success
 *  page when topup=… query param present). */
export function trackCreditPackPurchased(opts: { pack_id: string; credits: number; price: number }): void {
  posthog.capture('credit_pack_purchased', opts);
}

/** Low-credits in-app banner shown (when remaining/max < 10%). */
export function trackLowCreditsBannerShown(opts?: { remaining: number; max: number }): void {
  posthog.capture('low_credits_banner_shown', {
    ...(opts?.remaining !== undefined && { remaining: opts.remaining }),
    ...(opts?.max !== undefined && { max: opts.max }),
  });
}

/** User clicked the top-up CTA inside a low-credits banner. */
export function trackLowCreditsTopupClicked(): void {
  posthog.capture('low_credits_topup_clicked', {});
}

/** Lifecycle email recipient clicked through and landed on the site —
 *  captured by checking UTM params on the landing page mount. */
export function trackLifecycleEmailClicked(opts: { campaign: string; content?: string }): void {
  posthog.capture('lifecycle_email_clicked', opts);
}

/** Win-back lifecycle email click (subset of lifecycle_email_clicked, fired
 *  in addition when utm_campaign === 'winback'). */
export function trackWinbackClicked(): void {
  posthog.capture('winback_clicked', {});
}

/**
 * Signup → first outreach funnel (onboarding v2). Every event carries when
 * it happened and how long after signup, so time-to-first-email can be
 * measured. Properties are primitives only, and callers must never pass
 * email text, names, addresses or profile details.
 */
export type FirstOutreachFunnelEvent =
  | 'onboarding_completed'
  | 'walkthrough_match_selected'
  | 'walkthrough_draft_generated'
  | 'walkthrough_gmail_connected'
  | 'first_email_sent';

export function trackFirstOutreachFunnel(
  event: FirstOutreachFunnelEvent,
  signedUpAt?: string | null,
  props: Record<string, string | number | boolean | null> = {},
): void {
  const now = new Date();
  const signup = signedUpAt ? Date.parse(signedUpAt) : NaN;
  posthog.capture(event, {
    ...props,
    occurred_at: now.toISOString(),
    signed_up_at: Number.isNaN(signup) ? null : new Date(signup).toISOString(),
    seconds_since_signup: Number.isNaN(signup) ? null : Math.max(0, Math.round((now.getTime() - signup) / 1000)),
  });
}
