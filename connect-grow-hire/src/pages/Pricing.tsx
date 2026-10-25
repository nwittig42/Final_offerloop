import { useState, useEffect, useMemo, Fragment } from "react";
import { Helmet } from "react-helmet-async";
import {
  Check,
  ArrowLeft,
  ArrowRight,
  Settings,
  Shield,
  ChevronDown,
  X,
  Menu,
  Sparkles,
  Zap,
  Clock,
  Plus,
  Star,
  GraduationCap,
  Lock,
} from "lucide-react";
import { useNavigate, Link } from "react-router-dom";
import { useFirebaseAuth } from "../contexts/FirebaseAuthContext";
import OfferloopLogo from '@/assets/offerloop_logo2.png';
import MarketingHeader from '@/components/MarketingHeader';
import { loadStripe } from "@stripe/stripe-js";
import { getAuth } from 'firebase/auth';
import { BACKEND_URL } from '@/services/api';
import {
  trackUpgradeClick,
  trackSeasonPassClicked,
} from "../lib/analytics";
import {
  useTierConfig,
  resolvePriceId,
  resolveSeasonPassPriceId,
  seasonPassVisible,
  emailsFromCredits,
} from "@/hooks/useTierConfig";
import { TopUpModal } from "@/components/TopUpModal";
import { useCreditsView } from "@/hooks/useCreditsView";
import { isEduEligible } from "@/lib/eduDiscount";

// Design tokens — from the Offerloop design-system handoff (Lora display +
// Inter body on paper-2, with the vibrant purple/magenta accent layer used
// across the live product).
const T = {
  heading: '#1E2D4D',
  ink: '#0A0A0A',
  ink2: '#475569',
  ink3: '#64748B',
  ink4: '#94A3B8',
  paper: '#FFFFFF',
  paper2: '#F5F6F8',
  night: '#1A1A1A',
  border: '#E5E7EC',
  borderLight: '#EFF0F3',
  primary: '#4A60A8',
  primaryDark: '#3C4F8E',
  primary100: '#E4E9F5',
  purple: '#7C3AED',
  purpleDeep: '#8B3DE0',
  purpleLight: '#B478FF',
  magentaDeep: '#C4267E',
  pinkTint: '#FBE4EF',
  greenFg: '#2E7D32',
  greenBg: '#E8F5E9',
  serif: "'Lora', Georgia, serif",
  sans: "'Inter', sans-serif",
};

const EASE = 'cubic-bezier(0.16, 1, 0.3, 1)';
const CTA_GRADIENT = 'linear-gradient(135deg, #7C3AED 0%, #3457C4 100%)';
const ELITE_CTA_GRADIENT = 'linear-gradient(135deg, #8B3DE0 0%, #4364D6 100%)';
const MAGENTA_GRADIENT = 'linear-gradient(135deg, #E5397F, #C4267E)';
const TIER_NAME_GRADIENT = 'linear-gradient(120deg, #7C3AED, #4A60A8)';
const ELITE_NAME_GRADIENT = 'linear-gradient(120deg, #B478FF, #7C3AED)';
const NAVY_GRADIENT = 'linear-gradient(135deg, #1E2D4D 0%, #2C3F6B 60%, #3B3070 100%)';
const BADGE_NAVY_GRADIENT = 'linear-gradient(135deg, #1E2D4D 0%, #34457A 100%)';
// Gradient-border card (double-background trick) — Pro card + Best-value pack.
const GRADIENT_BORDER_BG = `linear-gradient(${T.paper}, ${T.paper}), linear-gradient(135deg, #7C3AED, #4A60A8)`;

// Prices: integers plain, otherwise 2 decimals ($14.99, $99).
const fmt = (n: number) => `$${Number.isInteger(n) ? n : n.toFixed(2)}`;

const STRIPE_PUBLISHABLE_KEY = "pk_live_51S4BB8ERY2WrVHp1acXrKE6RBG7NBlfHcMZ2kf7XhCX2E5g8Lasedx6ntcaD1H4BsoUMBGYXIcKHcAB4JuohLa2B00j7jtmWnB";
const stripePromise = loadStripe(STRIPE_PUBLISHABLE_KEY);

// Fallback Stripe Price IDs — used only if `useTierConfig()` returns an empty
// SKU. Keyed by audience: 'student' is the 50%-off .edu price (2026-07-28),
// 'list' is what everyone else pays. Must match backend STRIPE_PRICE_CATALOG
// defaults. The old $14.99 SKU stays live for existing subscribers only.
const FALLBACK_PRICE_IDS: Record<'student' | 'list', { pro: string; elite: string }> = {
  student: {
    pro: "price_1TyFiKERY2WrVHp1vTi7L5Wj", // $4.99/mo — pro_monthly_edu_499_2026
    elite: "price_1TyFiKERY2WrVHp15fnEAhPu", // $17.49/mo — elite_monthly_edu_1749_2026
  },
  list: {
    pro: "price_1TxzloERY2WrVHp15NcO6deA", // $9.99/mo — pro_monthly_999_2026
    elite: "price_1ScLcfERY2WrVHp1c5rcONJ3", // $34.99/mo
  },
};

interface SubscriptionStatus {
  tier: string;
  status: string;
  hasSubscription: boolean;
  // Present only when a REAL Stripe subscription exists. Null during the no-card
  // Pro trial — used to route trial upgrades to checkout, not subscription-modify.
  subscriptionId?: string | null;
  currentPeriodEnd?: number;
  cancelAtPeriodEnd?: boolean;
}

// One feature line on a plan card: check icon + label.
const FeatureRow: React.FC<{
  children: React.ReactNode;
  checkColor?: string;
  textColor?: string;
  weight?: number;
}> = ({ children, checkColor = T.primary, textColor = T.ink2, weight = 400 }) => (
  <div style={{ display: 'flex', gap: 9, alignItems: 'flex-start', fontSize: 13, color: textColor, fontWeight: weight, fontFamily: T.sans }}>
    <Check size={15} strokeWidth={2.4} style={{ color: checkColor, flexShrink: 0, marginTop: 1 }} />
    <span>{children}</span>
  </div>
);

// Small pill chip under the price block (.edu required / trial / cancel anytime).
const Chip: React.FC<{
  color: string;
  background: string;
  icon?: React.ReactNode;
  children: React.ReactNode;
}> = ({ color, background, icon, children }) => (
  <span
    style={{
      display: 'inline-flex',
      alignItems: 'center',
      gap: 5,
      fontSize: 11.5,
      fontWeight: 600,
      color,
      background,
      padding: '5px 10px',
      borderRadius: 100,
      fontFamily: T.sans,
      whiteSpace: 'nowrap',
    }}
  >
    {icon}
    {children}
  </span>
);

// Section eyebrow (uppercase kicker above section headlines).
const Eyebrow: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div
    style={{
      fontFamily: T.sans,
      fontSize: 12,
      fontWeight: 700,
      letterSpacing: '0.1em',
      textTransform: 'uppercase',
      color: T.primary,
      marginBottom: 12,
    }}
  >
    {children}
  </div>
);

// Italic serif accent inside headlines.
const Em: React.FC<{ color?: string; children: React.ReactNode }> = ({ color = T.primary, children }) => (
  <em style={{ fontStyle: 'italic', color }}>{children}</em>
);

// FAQ item — controlled so the accordion is single-open.
const FAQItem: React.FC<{
  question: string;
  answer: string;
  isOpen: boolean;
  onToggle: () => void;
}> = ({ question, answer, isOpen, onToggle }) => (
  <div style={{ borderBottom: `1px solid ${T.border}` }}>
    <button
      onClick={onToggle}
      style={{
        width: '100%',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        gap: 16,
        background: 'none',
        border: 'none',
        cursor: 'pointer',
        padding: '20px 4px',
        textAlign: 'left',
        fontFamily: T.sans,
        fontSize: 16,
        fontWeight: 600,
        color: T.heading,
      }}
    >
      <span>{question}</span>
      <ChevronDown
        size={18}
        style={{
          flexShrink: 0,
          color: T.primary,
          transition: `transform .3s ${EASE}`,
          transform: isOpen ? 'rotate(180deg)' : 'none',
        }}
      />
    </button>
    {isOpen && (
      <p style={{ margin: 0, padding: '0 4px 20px', fontSize: 14.5, lineHeight: 1.65, color: T.ink2, maxWidth: 620, fontFamily: T.sans }}>
        {answer}
      </p>
    )}
  </div>
);

// One cell of the comparison table: true → check, false → em dash, string → text.
type CompareCell = boolean | string;

const CompareCellValue: React.FC<{ value: CompareCell; isPro?: boolean }> = ({ value, isPro }) => {
  if (value === true) return <Check size={17} strokeWidth={2.4} style={{ color: T.purple, display: 'inline' }} />;
  if (value === false) return <span style={{ color: T.ink4, fontSize: 16 }}>—</span>;
  return (
    <span style={{ fontSize: 13, fontWeight: isPro ? 600 : 500, color: isPro ? T.purple : T.ink, fontFamily: T.sans }}>
      {value}
    </span>
  );
};

const Pricing = () => {
  const [isLoading, setIsLoading] = useState(false);
  const [subscriptionStatus, setSubscriptionStatus] = useState<SubscriptionStatus | null>(null);
  const [navbarScrolled, setNavbarScrolled] = useState(false);
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [topUpModalOpen, setTopUpModalOpen] = useState(false);
  const [openFaq, setOpenFaq] = useState<number | null>(0);
  const navigate = useNavigate();
  const { user, updateUser, checkCredits, isLoading: authLoading } = useFirebaseAuth();

  // 2026-07-28 .edu discount: monthly only, no slider, no annual toggle. One
  // decision: Free or Pro (Elite anchors). Verified .edu users see the
  // 'student' audience (50% off); everyone else, including signed-out
  // visitors, sees 'list'. The backend re-checks eligibility at checkout.
  const billingCadence = 'monthly' as const;
  const isEdu = isEduEligible(user);
  const audience: 'student' | 'list' = isEdu ? 'student' : 'list';

  // Display-only toggle: preview prices with or without the .edu discount.
  // Checkout NEVER follows the preview — it always uses the server-verified
  // `audience` above, so a non-.edu user previewing $4.99 still checks out at
  // list (and the CTA fine print says so).
  const [previewAudience, setPreviewAudience] = useState<'student' | 'list'>(audience);
  useEffect(() => {
    setPreviewAudience(isEdu ? 'student' : 'list');
  }, [isEdu]);
  const showEduPrices = previewAudience === 'student';

  // Pull runtime tier config (cached via React Query). Drives prices, Stripe
  // SKUs, trial days, active promos, top-up packs. Falls back to
  // lib/constants.ts defaults if the endpoint is unreachable.
  const { config: tierConfig } = useTierConfig();
  const creditsView = useCreditsView();
  const proStops = tierConfig.slider_stops.pro;
  const eliteStops = tierConfig.slider_stops.elite;
  const proStop = proStops.find((s) => s.default) ?? proStops[0];
  const eliteStop = eliteStops.find((s) => s.default) ?? eliteStops[0];

  // Real price for this user (server-verified audience) — used in FAQ/trial
  // copy. Card display uses the preview toggle instead (proShown/eliteShown).
  const proMonthlyPrice = proStop[audience];
  // List prices anchor the strikethrough when the .edu discount applies.
  const proListPrice = proStop.list;
  const eliteListPrice = eliteStop.list;

  // Stripe Price ID resolution from the env-driven catalog. Empty string means
  // cofounders haven't wired that SKU yet — CTA falls back to the legacy default.
  const proPriceId = useMemo(() => {
    const id = resolvePriceId(
      tierConfig.stripe_catalog,
      'pro',
      billingCadence,
      audience,
      proStop.credits,
    );
    return id || FALLBACK_PRICE_IDS[audience].pro;
  }, [tierConfig.stripe_catalog, billingCadence, audience, proStop.credits]);

  const elitePriceId = useMemo(() => {
    const id = resolvePriceId(
      tierConfig.stripe_catalog,
      'elite',
      billingCadence,
      audience,
      eliteStop.credits,
    );
    return id || FALLBACK_PRICE_IDS[audience].elite;
  }, [tierConfig.stripe_catalog, billingCadence, audience, eliteStop.credits]);

  const seasonPassPriceId = useMemo(
    () => resolveSeasonPassPriceId(tierConfig.stripe_catalog, audience),
    [tierConfig.stripe_catalog, audience],
  );

  // Real coupon-gated urgency badge. Empty active_promos = no badge.
  const hasActivePromo = Object.values(tierConfig.active_promos).some(Boolean);

  // Trial duration — single value for everyone, from config. The .edu benefit is the
  // price discount, not a longer trial. Simpler to communicate, less confusing.
  const trialDays = tierConfig.trial.days_non_student;

  useEffect(() => {
    if (user) {
      fetchSubscriptionStatus();
    }
  }, [user]);

  useEffect(() => {
    const handleScroll = () => setNavbarScrolled(window.scrollY > 50);
    window.addEventListener('scroll', handleScroll, { passive: true });
    return () => window.removeEventListener('scroll', handleScroll);
  }, []);

  const fetchSubscriptionStatus = async () => {
    try {
      const auth = getAuth();
      const firebaseUser = auth.currentUser;

      if (!firebaseUser) return;

      const token = await firebaseUser.getIdToken();

      const API_URL = BACKEND_URL;

      const response = await fetch(`${API_URL}/api/subscription-status`, {
        headers: {
          'Authorization': `Bearer ${token}`,
        },
      });

      if (response.ok) {
        const data = await response.json();
        setSubscriptionStatus(data);
      }
    } catch (error) {
      console.error('Failed to fetch subscription status:', error);
    }
  };

  const handleManageSubscription = async () => {
    if (!user) return;

    setIsLoading(true);
    try {
      const auth = getAuth();
      const firebaseUser = auth.currentUser;

      if (!firebaseUser) {
        throw new Error('No Firebase user found');
      }

      const token = await firebaseUser.getIdToken();

      const API_URL = BACKEND_URL;

      const response = await fetch(`${API_URL}/api/create-portal-session`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`,
        },
        body: JSON.stringify({
          returnUrl: `${window.location.origin}/pricing`,
        }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({ error: 'Unknown error' }));
        const errorMessage = errorData.details || errorData.error || 'Failed to create portal session';
        throw new Error(errorMessage);
      }

      const { url } = await response.json();
      if (!url) {
        throw new Error('No portal URL received from server');
      }

      window.location.href = url;

    } catch (error) {
      console.error('Portal error:', error);
      const errorMessage = error instanceof Error ? error.message : 'Failed to open subscription management. Please try again.';

      // Show more helpful error message
      if (errorMessage.includes('mode mismatch') || errorMessage.includes('test mode') || errorMessage.includes('live mode')) {
        alert('Stripe Configuration Error: There is a mismatch between test and live mode keys. Please contact support or check your Stripe configuration.');
      } else {
        alert(`Failed to open subscription management: ${errorMessage}`);
      }
    } finally {
      setIsLoading(false);
    }
  };


  const handleResetCredits = async (tier: 'free' | 'pro' | 'elite') => {
    if (!user) return;

    // Credit amounts based on tier (matches backend/app/config.py TIER_CONFIGS,
    // doubled 2026-06-10 — see lib/constants.ts TIER_CONFIGS)
    const creditMap = {
      'free': 300,
      'pro': 2000,
      'elite': 5000
    };

    const maxCredits = creditMap[tier];

    try {
      console.log(`🔄 Resetting credits for ${tier} tier to ${maxCredits}`);
      await updateUser({
        credits: maxCredits,
        maxCredits: maxCredits
      });

      // Refresh credits to update UI
      if (checkCredits) {
        await checkCredits();
      }

      // No popup - just silently reset credits
    } catch (error) {
      console.error("Error resetting credits:", error);
      // Silently fail - don't show popup
    }
  };

  const handleSubscriptionUpgrade = async (newTier: 'pro' | 'elite') => {
    if (!user) return;

    setIsLoading(true);
    try {
      const auth = getAuth();
      const firebaseUser = auth.currentUser;
      if (!firebaseUser) throw new Error('No Firebase user found');

      const token = await firebaseUser.getIdToken();

      const API_URL = BACKEND_URL;

      const priceId = newTier === 'elite' ? elitePriceId : proPriceId;

      const response = await fetch(`${API_URL}/api/update-subscription`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`,
        },
        body: JSON.stringify({ priceId }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({ error: 'Unknown error' }));
        throw new Error(errorData.error || 'Failed to update subscription');
      }

      await response.json();

      if (checkCredits) await checkCredits();
      await fetchSubscriptionStatus();

      alert(`Successfully upgraded to ${newTier === 'elite' ? 'Elite' : 'Pro'}! Your credits have been updated.`);
    } catch (error) {
      console.error('Subscription upgrade error:', error);
      const errorMessage = error instanceof Error ? error.message : 'Unknown error';
      alert(`Failed to upgrade: ${errorMessage}`);
    } finally {
      setIsLoading(false);
    }
  };

  const handleUpgrade = async (planType: 'free' | 'pro' | 'elite', fromFeature?: string) => {
    // Public pricing page - visitors without an account get bounced to sign-in
    // and brought back to /pricing with the plan they tapped pre-selected.
    if (!user) {
      navigate(`/signin?next=/pricing&plan=${planType}`);
      return;
    }

    try {
      if (planType === 'free') {
        await updateUser({
          tier: 'free',
          credits: 300,
          maxCredits: 300
        });
        navigate("/find");
      }
      else if (planType === 'pro' || planType === 'elite') {
        trackUpgradeClick(fromFeature || 'pricing', {
          from_location: 'pricing_page',
          plan_selected: planType,
        });

        // Card-on-file trial: every upgrade goes through Stripe Checkout,
        // which grants the 7-day trial server-side (skipped if already used).

        // Modify-in-place only works with a REAL Stripe subscription. A no-card
        // trial user has subscriptionStatus 'trialing' but no Stripe sub, so they
        // must go through checkout (which converts the trial to paid + hands over
        // the full monthly pool). Gating on subscriptionId, not the trialing flag.
        const hasRealStripeSub = !!subscriptionStatus?.subscriptionId;
        if (hasRealStripeSub) {
          await handleSubscriptionUpgrade(planType);
        } else {
          await handleStripeCheckout(planType);
        }
      }
    } catch (error) {
      console.error("Error updating user:", error);
    }
  };

  const handleStripeCheckout = async (tier: 'pro' | 'elite' = 'pro') => {
    if (!user) {
      // Anonymous visitor on the public /pricing page — send them to sign up,
      // then bring them straight back here to complete the upgrade.
      navigate(`/signin?mode=signup&redirect=${encodeURIComponent(`/pricing?tier=${tier}`)}`);
      return;
    }

    setIsLoading(true);

    try {
      const auth = getAuth();
      const firebaseUser = auth.currentUser;

      if (!firebaseUser) {
        throw new Error('No Firebase user found');
      }

      const token = await firebaseUser.getIdToken();

      const API_URL = BACKEND_URL;

      // Resolve Stripe Price ID from the runtime catalog. Falls back to the
      // legacy live SKU if cofounders haven't wired the new audience/cadence/
      // credits combination yet (resolvePriceId returns '' on miss).
      const priceId = tier === 'elite' ? elitePriceId : proPriceId;
      // Also send the raw slider selection so the backend can build inline
      // price_data for stops without a wired SKU (e.g. Pro 3K = $19.99). Backend
      // looks the amount up from SLIDER_STOPS server-side, so a client can't
      // spoof the price.
      const selectedStop = tier === 'elite' ? eliteStop : proStop;

      const response = await fetch(`${API_URL}/api/create-checkout-session`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`,
        },
        body: JSON.stringify({
          priceId: priceId,
          tier,
          credits: selectedStop.credits,
          cadence: billingCadence,
          audience,
          userId: user.uid,
          userEmail: user.email,
          successUrl: `${window.location.origin}/payment-success?session_id={CHECKOUT_SESSION_ID}`,
          cancelUrl: `${window.location.origin}/pricing`,
        }),
      });

      if (!response.ok) {
        const errorText = await response.text();
        console.error('Checkout session creation failed:', response.status, errorText);
        throw new Error(`Failed to create checkout session: ${response.status} - ${errorText}`);
      }

      const responseData = await response.json();
      console.log('Checkout session response:', responseData);

      const sessionId = responseData.sessionId;
      if (!sessionId) {
        console.error('No sessionId in response:', responseData);
        throw new Error('Invalid response from server: missing sessionId');
      }

      const stripe = await stripePromise;
      if (!stripe) {
        throw new Error('Stripe failed to initialize');
      }

      const { error } = await stripe.redirectToCheckout({
        sessionId: sessionId,
      });

      if (error) {
        console.error('Stripe redirect error:', error);
        alert('Payment error: ' + error.message);
      }
    } catch (error) {
      console.error('Checkout error:', error);
      const errorMessage = error instanceof Error ? error.message : 'Unknown error';
      alert(`Something went wrong: ${errorMessage}`);
    } finally {
      setIsLoading(false);
    }
  };

  // Season Pass — one-time, mode=payment checkout (NOT a subscription). Routed
  // to its own backend endpoint, which grants the season_pass tier on the
  // webhook. Mirrors the top-up purchase path, not handleStripeCheckout.
  const handleSeasonPassCheckout = async () => {
    if (!user) {
      navigate(`/signin?mode=signup&redirect=${encodeURIComponent('/pricing')}`);
      return;
    }
    setIsLoading(true);
    try {
      const fbUser = getAuth().currentUser;
      if (!fbUser) throw new Error('No Firebase user found');
      const token = await fbUser.getIdToken();
      const res = await fetch(`${BACKEND_URL}/api/billing/create-season-pass-session`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          audience,
          successUrl: `${window.location.origin}/payment-success?season_pass=1&session_id={CHECKOUT_SESSION_ID}`,
          cancelUrl: `${window.location.origin}/pricing`,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.message || `Season Pass checkout failed: ${res.status}`);
      }
      const { sessionId } = await res.json();
      const stripe = await stripePromise;
      if (!stripe) throw new Error('Stripe failed to initialize');
      const { error } = await stripe.redirectToCheckout({ sessionId });
      if (error) alert('Payment error: ' + error.message);
    } catch (error) {
      console.error('Season Pass checkout error:', error);
      alert(error instanceof Error ? error.message : 'Something went wrong');
    } finally {
      setIsLoading(false);
    }
  };

  // Tier resolution, in priority order:
  //   1. the freshly-fetched subscription status (richest, but async),
  //   2. the `user` object (auth context resolves it from Firestore),
  //   3. a localStorage hint from a prior visit — lets a returning user see the
  //      correct UI on the VERY FIRST paint, with no flash.
  const cachedTier = (typeof window !== 'undefined'
    ? (localStorage.getItem('offerloop_tier') as 'free' | 'pro' | 'elite' | null)
    : null);
  const currentTier = subscriptionStatus?.tier || user?.subscriptionTier || user?.tier || cachedTier || 'free';
  // Derive subscription flags from the RESOLVED tier so the promo card, the
  // subscription banner, and locked-feature states don't flash in/out before
  // /api/subscription-status returns. Once that fetch lands we honor its live
  // active/trialing status; until then we trust the tier from user/cache.
  const isProUser = subscriptionStatus
    ? (subscriptionStatus.tier === 'pro' && (subscriptionStatus.status === 'active' || subscriptionStatus.status === 'trialing'))
    : currentTier === 'pro';
  const isEliteUser = subscriptionStatus
    ? (subscriptionStatus.tier === 'elite' && (subscriptionStatus.status === 'active' || subscriptionStatus.status === 'trialing'))
    : currentTier === 'elite';
  const hasActiveSubscription = isProUser || isEliteUser;
  // True only when there's a real Stripe subscription to manage. The tier alone
  // is not enough — admin-bumped users and no-card Path A trial users have a
  // paid tier with no stripeCustomerId, so the billing portal would 404.
  const hasRealStripeSub = !!subscriptionStatus?.subscriptionId;
  // Ready as soon as auth resolves — or immediately if we have a cached tier hint
  // (the common returning-visitor case), so the buttons never pop in from a box.
  const ctaReady = !authLoading || !!cachedTier;

  // Persist the resolved tier as a first-paint hint; clear it once we know the
  // user is signed out, so a returning visitor never sees a stale tier.
  useEffect(() => {
    if (user) {
      const t = user.subscriptionTier || user.tier;
      if (t) localStorage.setItem('offerloop_tier', t);
    } else if (!authLoading) {
      localStorage.removeItem('offerloop_tier');
    }
  }, [user, authLoading]);

  // Format renewal date
  const renewalDate = subscriptionStatus?.currentPeriodEnd
    ? new Date(subscriptionStatus.currentPeriodEnd * 1000).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
    : null;

  const periodLabel = '/mo';

  const freeEmails = emailsFromCredits(300, tierConfig.credit_costs.find_contact).toLocaleString();
  const proEmails = emailsFromCredits(proStop.credits, tierConfig.credit_costs.find_contact).toLocaleString();
  const eliteEmails = emailsFromCredits(eliteStop.credits, tierConfig.credit_costs.find_contact).toLocaleString();

  // Card prices follow the preview toggle; checkout follows real eligibility.
  const proShown = proStop[previewAudience];
  const eliteShown = eliteStop[previewAudience];

  const scrollToCompare = () => {
    document.getElementById('compare')?.scrollIntoView({ behavior: 'smooth' });
  };

  // Comparison-table data. Grouped by workflow stage; values over checkmarks
  // wherever the tiers differ by amount, not by access.
  const compareGroups: [string, [string, CompareCell, CompareCell, CompareCell][]][] = [
    ['Discovery & credits', [
      ['Emails / month', `~${freeEmails}`, `~${proEmails}`, `~${eliteEmails}`],
      ['Monthly credits', '300', proStop.credits.toLocaleString(), eliteStop.credits.toLocaleString()],
      ['Contacts per search', '3', '8', '15'],
      ['Concurrent agents', false, '1', 'Up to 5'],
      ['Find hiring managers', false, true, true],
      ['Firm & company search', false, true, true],
    ]],
    ['Outreach', [
      ['AI email drafting, straight to Gmail', true, true, true],
      ['Custom email templates', true, true, 'Resume-tailored'],
      ['Bulk drafting', true, true, true],
      ['Export (CSV & Gmail)', false, true, true],
      ['Unlimited directory saving', false, true, true],
    ]],
    ['Tracking & prep', [
      ['Outreach pipeline tracking', true, true, true],
      ['Meeting prep', true, true, true],
      ['Weekly personalized firm insights', false, false, true],
      ['Top-up credit packs', false, true, true],
    ]],
    ['Access & support', [
      ['Priority queue', false, false, true],
      ['Early access to new AI tools', false, false, true],
      [`${trialDays}-day free trial`, false, true, false],
    ]],
  ];

  const faqs: [string, string][] = [
    [
      'How does the free trial work?',
      `You get ${trialDays} days of full Pro access. Add a payment method to start, and you won't be charged until day ${trialDays + 1}. Cancel anytime during the trial in two clicks from account settings and you pay nothing. After the trial it's $${proMonthlyPrice}/mo. The trial is for Pro; you can also use the Free plan forever with no card at all.`,
    ],
    [
      'Do students get a discount?',
      `Yes: 50% off every paid plan. Sign up with your .edu email and Pro is $${proStop.student}/mo instead of $${proStop.list}/mo, Elite $${eliteStop.student}/mo instead of $${eliteStop.list}/mo. The discount applies automatically at checkout when your account email ends in .edu, no coupon codes needed. Signed up with a personal email? Contact support to verify your .edu address and unlock the same rate.`,
    ],
    [
      'What happens when I run out of credits?',
      "Searches pause until your plan renews (the 1st of the next month) or you upgrade. Pro and Elite subscribers can also top up with a credit pack, and purchased credits never expire. All your saved contacts and drafts stay put.",
    ],
    [
      'Can I change plans or cancel anytime?',
      'Yep, anytime. Upgrading? You get access immediately. Downgrading or canceling? Takes effect at your next billing cycle, and you keep access until then. Takes 10 seconds either way.',
    ],
    [
      'Do credits roll over?',
      "Nope, they reset on the 1st of each month. Use 'em or lose 'em - but honestly, most students use them up well before the month is over during peak recruiting.",
    ],
    [
      "What's the Recruiting Season Pass?",
      `A one-time charge for ${tierConfig.season_pass.months} months of Pro-level access with ${tierConfig.season_pass.credits_per_month.toLocaleString()} credits refilled monthly, and no auto-renewal. Ideal for a focused recruiting sprint.`,
    ],
    [
      "What's your refund policy?",
      "Pro and Elite are refundable for 7 days from your first charge. The Recruiting Season Pass has a 14-day window, provided you haven't used more than half of your month-1 credits. Top-up credit packs are non-refundable (your credits never expire, so there's no reason they should). Email support@offerloop.ai or request a refund from your account settings. We typically respond within 24 hours.",
    ],
  ];

  return (
    <div style={{ fontFamily: T.sans, color: T.ink, background: T.paper2, minHeight: '100vh' }}>
      <Helmet>
        <title>Offerloop Pricing - Student Plans for College Networking</title>
        <meta name="description" content={`Pro $9.99/mo with a ${trialDays}-day free trial, Elite $34.99/mo. Students with a .edu email get 50% off: Pro $4.99/mo, Elite $17.49/mo. Offerloop helps college students network into consulting, investment banking, and tech.`} />
        <link rel="canonical" href="https://offerloop.ai/pricing" />
      </Helmet>

      <style>{`
        @keyframes ofUp { from { opacity: 0; transform: translateY(22px); } to { opacity: 1; transform: none; } }
        .of-up { animation: ofUp .7s ${EASE} both; }
        .of-card { transition: transform .25s ${EASE}, box-shadow .25s ${EASE}; }
        .of-card:hover { transform: translateY(-4px); box-shadow: 0 12px 32px rgba(15, 37, 69, 0.14); }
        .of-pro-card { transition: box-shadow .25s ${EASE}; }
        .of-pro-card:hover { box-shadow: 0 24px 54px rgba(124, 58, 237, 0.26); }
        .of-cta { transition: all .2s ${EASE}; }
        .of-cta-grad:hover:not(:disabled) { filter: brightness(1.07); transform: translateY(-2px); box-shadow: 0 8px 22px rgba(124, 58, 237, 0.4); }
        .of-cta-outline:hover:not(:disabled) { background: ${T.paper2}; border-color: ${T.ink4} !important; transform: translateY(-2px); }
        .of-cta-white:hover:not(:disabled) { transform: translateY(-2px); box-shadow: 0 10px 24px rgba(0, 0, 0, 0.2); }
        .of-link { transition: color .2s ${EASE}; }
        .of-link:hover { color: ${T.primary}; }
      `}</style>

      {/* Marketing header - logged-out visitors only. In-app pricing keeps its own nav. */}
      {!user && <MarketingHeader active="pricing" />}

      {/* ======= ABOVE THE FOLD: toggles + plan cards, no hero copy. The buy
          buttons sit inside each card directly under the price block, before
          the feature list, so price + CTA are visible without scrolling. ======= */}
      <section style={{ position: 'relative', overflow: 'hidden', padding: '22px 24px 44px' }}>
        {/* Soft radial washes behind the cards */}
        <div
          aria-hidden
          style={{
            position: 'absolute',
            inset: 0,
            background: 'radial-gradient(900px 460px at 50% -6%, rgba(124,58,237,0.12), transparent 62%), radial-gradient(700px 400px at 84% 8%, rgba(74,96,168,0.10), transparent 60%)',
            pointerEvents: 'none',
          }}
        />

        <div style={{ position: 'relative', maxWidth: 1180, margin: '0 auto' }}>
          {/* Back navigation — always lands on home. Logged-in users go to the
              dashboard; logged-out marketing visitors go to the landing page.
              Never navigate(-1): a Stripe-cancel bounce would otherwise dump them
              back on Stripe. */}
          <button
            onClick={() => navigate(user ? '/find' : '/')}
            className="of-link"
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 7,
              fontSize: 13.5,
              fontWeight: 500,
              color: T.ink3,
              background: 'none',
              border: 'none',
              cursor: 'pointer',
              padding: 0,
              marginBottom: 22,
              fontFamily: T.sans,
            }}
          >
            <ArrowLeft size={15} />
            Back to home
          </button>

          {/* Subscription Status Banner */}
          {hasActiveSubscription && (
            <div
              className="of-up"
              style={{
                background: T.paper,
                border: `1px solid ${T.border}`,
                borderRadius: 16,
                padding: '16px 22px',
                marginBottom: 22,
                display: 'flex',
                flexWrap: 'wrap',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: 14,
                boxShadow: '0 1px 3px rgba(15,37,69,0.06)',
              }}
            >
              <div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ fontFamily: T.serif, fontSize: 17, fontWeight: 600, color: T.heading }}>
                    {isEliteUser ? 'Elite' : 'Pro'} subscription
                  </span>
                  {subscriptionStatus?.status === 'trialing' ? (
                    <Chip color="#92600A" background="#FDF3DC">Trial</Chip>
                  ) : (
                    <Chip color={T.greenFg} background={T.greenBg}>Active</Chip>
                  )}
                </div>
                <p style={{ margin: '4px 0 0', fontSize: 13, color: T.ink3 }}>
                  {creditsView.balance.toLocaleString()} credits {creditsView.isTrialing ? 'today' : 'remaining'}
                  {renewalDate && !subscriptionStatus?.cancelAtPeriodEnd && ` · Renews ${renewalDate}`}
                  {subscriptionStatus?.cancelAtPeriodEnd && renewalDate && ` · Cancels ${renewalDate}`}
                </p>
              </div>
              <button
                onClick={handleManageSubscription}
                disabled={isLoading}
                className="of-cta of-cta-grad"
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 7,
                  padding: '10px 18px',
                  borderRadius: 10,
                  background: T.night,
                  color: '#fff',
                  border: 'none',
                  fontWeight: 600,
                  fontSize: 13.5,
                  cursor: 'pointer',
                  fontFamily: T.sans,
                  opacity: isLoading ? 0.5 : 1,
                }}
              >
                <Settings size={14} />
                Manage subscription
              </button>
            </div>
          )}

          {/* Live coupon banner — vibrant magenta when a real Stripe coupon ID
              is wired via env. No fake scarcity. Per project standing rule. */}
          {hasActivePromo && tierConfig.active_promos.pricing_recapture && (
            <div className="of-up" style={{ textAlign: 'center', marginBottom: 18 }}>
              <div
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 10,
                  padding: '10px 20px',
                  background: MAGENTA_GRADIENT,
                  borderRadius: 100,
                  fontSize: 13,
                  fontWeight: 700,
                  color: '#fff',
                  boxShadow: '0 8px 22px -8px rgba(196,38,126,0.6)',
                }}
              >
                <Sparkles size={14} />
                <span style={{ letterSpacing: '0.04em' }}>
                  Code <strong style={{ letterSpacing: '0.16em' }}>STAYHIRED</strong>: 20% off your first month
                </span>
              </div>
            </div>
          )}

          {/* .edu discount badge — eligible users see their discount applied;
              everyone else sees the hook to sign up with a .edu email. */}
          <div
            className="of-up"
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              marginBottom: 30,
              animationDelay: '.05s',
            }}
          >
            <div
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 8,
                background: T.primary100,
                border: `1px solid ${T.primary}`,
                borderRadius: 100,
                padding: '8px 14px',
                boxShadow: '0 1px 3px rgba(15,37,69,0.06)',
              }}
            >
              <GraduationCap size={15} style={{ color: T.primary }} />
              <span style={{ fontSize: 13, fontWeight: 600, color: T.heading }}>
                {isEdu
                  ? 'Your .edu discount is applied: 50% off every plan'
                  : 'Students: sign up with your .edu email for 50% off'}
              </span>
            </div>
          </div>

          {/* .edu price preview toggle — display-only. Checkout always charges
              the server-verified audience, never the preview. */}
          <div
            className="of-up"
            style={{
              display: 'flex',
              justifyContent: 'center',
              marginTop: -16,
              marginBottom: 30,
              animationDelay: '.08s',
            }}
          >
            <div
              role="group"
              aria-label="Preview prices with or without a .edu email"
              style={{
                display: 'inline-flex',
                background: T.paper,
                border: `1px solid ${T.border}`,
                borderRadius: 100,
                padding: 4,
                gap: 2,
                boxShadow: '0 1px 3px rgba(15,37,69,0.06)',
              }}
            >
              {([
                ['student', 'With .edu email'],
                ['list', 'Without .edu email'],
              ] as const).map(([value, label]) => {
                const active = previewAudience === value;
                return (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={active}
                    onClick={() => setPreviewAudience(value)}
                    style={{
                      border: 'none',
                      cursor: 'pointer',
                      borderRadius: 100,
                      padding: '7px 16px',
                      fontSize: 12.5,
                      fontWeight: 600,
                      fontFamily: T.sans,
                      background: active ? CTA_GRADIENT : 'transparent',
                      color: active ? '#fff' : T.ink3,
                      transition: `all .2s ${EASE}`,
                    }}
                  >
                    {value === 'student' ? `${label} · 50% off` : label}
                  </button>
                );
              })}
            </div>
          </div>

          {/* ======= PLAN CARDS ======= */}
          <div
            className="grid grid-cols-1 md:grid-cols-3 gap-5 items-start"
            style={{ maxWidth: 1040, margin: '0 auto', paddingTop: 16 }}
          >
            {/* FREE */}
            <div
              className="of-card of-up"
              style={{
                background: T.paper,
                border: `1px solid ${T.border}`,
                borderRadius: 16,
                padding: '28px 26px',
                display: 'flex',
                flexDirection: 'column',
                boxShadow: '0 1px 3px rgba(15,37,69,0.06)',
                animationDelay: '.1s',
              }}
            >
              <div style={{ fontFamily: T.serif, fontSize: 24, fontWeight: 600, color: T.heading, marginBottom: 4 }}>Free</div>
              <div style={{ fontSize: 13, color: T.ink3, marginBottom: 18 }}>Try it out for free</div>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 5, marginBottom: 2 }}>
                <span style={{ fontFamily: T.serif, fontSize: 40, fontWeight: 600, color: T.heading, letterSpacing: '-0.02em' }}>$0</span>
                <span style={{ fontSize: 14, color: T.ink3 }}>/forever</span>
              </div>
              <div style={{ fontSize: 12.5, color: T.ink4, marginBottom: 20, minHeight: 16 }}>
                <strong style={{ color: T.ink3, fontWeight: 600 }}>~{freeEmails} emails</strong> / month · 300 credits
              </div>

              {!ctaReady ? (
                <div aria-hidden style={{ padding: 12, borderRadius: 10, background: T.paper2, color: 'transparent' }} className="animate-pulse select-none">&nbsp;</div>
              ) : (
                <button
                  onClick={() => {
                    if (!user) {
                      navigate('/signin?next=/pricing&plan=free');
                    } else if (currentTier === 'free') {
                      handleResetCredits('free');
                    } else {
                      handleUpgrade('free', 'pricing_page');
                    }
                  }}
                  className="of-cta of-cta-outline"
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    padding: 12,
                    borderRadius: 10,
                    background: 'transparent',
                    color: T.ink,
                    border: `1.5px solid ${T.border}`,
                    fontWeight: 600,
                    fontSize: 14.5,
                    cursor: 'pointer',
                    fontFamily: T.sans,
                  }}
                >
                  {!user ? 'Get started free' : currentTier === 'free' ? 'Current plan' : 'Start for free'}
                </button>
              )}

              <div style={{ borderTop: `1px solid ${T.borderLight}`, marginTop: 22, paddingTop: 18, display: 'flex', flexDirection: 'column', gap: 10 }}>
                <FeatureRow>~{freeEmails} emails / month (300 credits)</FeatureRow>
                <FeatureRow>Up to 3 contacts per search</FeatureRow>
                <FeatureRow>AI email drafting, straight to Gmail</FeatureRow>
                <FeatureRow>Custom email templates</FeatureRow>
                <FeatureRow>Smart filters & meeting prep</FeatureRow>
              </div>
            </div>

            {/* PRO — highlighted, gradient border, elevated. First in the stack
                on mobile so the recommended plan is the thumb-first card. */}
            <div
              className="of-pro-card of-up order-first md:order-none md:-translate-y-2"
              style={{
                position: 'relative',
                border: '2px solid transparent',
                borderRadius: 16,
                padding: '28px 26px',
                display: 'flex',
                flexDirection: 'column',
                boxShadow: '0 18px 44px rgba(124,58,237,0.18)',
                backgroundImage: GRADIENT_BORDER_BG,
                backgroundOrigin: 'border-box',
                backgroundClip: 'padding-box, border-box',
                animationDelay: '.16s',
              }}
            >
              <div
                style={{
                  position: 'absolute',
                  top: -13,
                  left: '50%',
                  transform: 'translateX(-50%)',
                  background: MAGENTA_GRADIENT,
                  color: '#fff',
                  fontSize: 11,
                  fontWeight: 700,
                  letterSpacing: '0.05em',
                  textTransform: 'uppercase',
                  padding: '6px 15px',
                  borderRadius: 100,
                  boxShadow: '0 3px 10px rgba(196,38,126,0.32)',
                  whiteSpace: 'nowrap',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 5,
                }}
              >
                <Star size={12} fill="currentColor" />
                Most popular
              </div>

              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4, flexWrap: 'wrap' }}>
                <span
                  style={{
                    fontFamily: T.serif,
                    fontStyle: 'italic',
                    fontSize: 24,
                    fontWeight: 600,
                    background: TIER_NAME_GRADIENT,
                    WebkitBackgroundClip: 'text',
                    backgroundClip: 'text',
                    WebkitTextFillColor: 'transparent',
                  }}
                >
                  Pro
                </span>
                {currentTier === 'pro' && (
                  <Chip color={T.greenFg} background={T.greenBg}>Your plan</Chip>
                )}
              </div>
              <div style={{ fontSize: 13, color: T.ink3, marginBottom: 16 }}>Best for students</div>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 7, marginBottom: 2 }}>
                {showEduPrices && (
                  <span style={{ fontFamily: T.serif, fontSize: 22, fontWeight: 500, color: T.ink4, textDecoration: 'line-through', fontVariantNumeric: 'tabular-nums' }}>
                    {fmt(proListPrice)}
                  </span>
                )}
                <span style={{ fontFamily: T.serif, fontSize: 40, fontWeight: 600, color: T.heading, letterSpacing: '-0.02em', fontVariantNumeric: 'tabular-nums' }}>
                  {fmt(proShown)}
                </span>
                <span style={{ fontSize: 14, color: T.ink3 }}>{periodLabel}</span>
              </div>
              <div style={{ fontSize: 12.5, color: T.ink4, marginBottom: 14, minHeight: 16 }}>
                <strong style={{ color: T.ink3, fontWeight: 600 }}>~{proEmails} emails</strong> / month · {proStop.credits.toLocaleString()} credits
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 16 }}>
                <Chip color={T.greenFg} background={T.greenBg}>{trialDays} days free</Chip>
                {showEduPrices && (
                  <Chip color={T.primary} background={T.primary100} icon={<GraduationCap size={12} />}>
                    50% .edu discount
                  </Chip>
                )}
                <Chip color={T.primary} background={T.primary100}>Cancel anytime</Chip>
              </div>

              {!ctaReady ? (
                <div aria-hidden style={{ padding: 13, borderRadius: 10, background: T.paper2, color: 'transparent' }} className="animate-pulse select-none">&nbsp;</div>
              ) : (
                <button
                  onClick={
                    isLoading ? undefined :
                    currentTier === 'pro' && hasRealStripeSub
                      ? (e: React.MouseEvent) => {
                          if (e.shiftKey) {
                            handleResetCredits('pro');
                          } else {
                            handleManageSubscription();
                          }
                        }
                      : () => handleUpgrade('pro', 'pricing_page')
                  }
                  disabled={isLoading || currentTier === 'elite'}
                  title={currentTier === 'pro' && hasRealStripeSub ? 'Click to manage subscription. Hold Shift+Click to reset credits.' : currentTier === 'elite' ? 'You are on Elite plan' : undefined}
                  className="of-cta of-cta-grad"
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: 7,
                    padding: 13,
                    borderRadius: 10,
                    background: currentTier === 'elite' ? T.paper2 : CTA_GRADIENT,
                    color: currentTier === 'elite' ? T.ink3 : '#fff',
                    border: 'none',
                    fontWeight: 600,
                    fontSize: 15,
                    cursor: currentTier === 'elite' ? 'not-allowed' : 'pointer',
                    boxShadow: currentTier === 'elite' ? 'none' : '0 4px 14px rgba(124,58,237,0.32)',
                    opacity: isLoading ? 0.6 : 1,
                    fontFamily: T.sans,
                  }}
                >
                  {isLoading
                    ? 'Processing...'
                    : currentTier === 'elite'
                      ? 'On Elite plan'
                      : currentTier === 'pro' && hasRealStripeSub
                        ? 'Manage subscription'
                        : currentTier === 'pro'
                          ? 'Subscribe to Pro'
                          : `Start ${trialDays}-day free trial`}
                  {currentTier !== 'elite' && !isLoading && <ArrowRight size={16} />}
                </button>
              )}

              <div style={{ fontSize: 12, color: T.ink4, textAlign: 'center', marginTop: 10 }}>
                {showEduPrices && !isEdu
                  ? `${trialDays} days free, then ${fmt(proStop.student)}/mo with a .edu email (${fmt(proStop.list)} without). Cancel anytime.`
                  : `${trialDays} days free, then ${fmt(proStop[audience])}/mo. Cancel anytime.`}
              </div>

              <div style={{ borderTop: `1px solid ${T.borderLight}`, marginTop: 14, paddingTop: 18, display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ fontSize: 11.5, fontWeight: 700, color: T.ink3, textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: -1 }}>
                  Everything in Free, plus
                </div>
                <FeatureRow checkColor={T.purple} textColor={T.ink} weight={500}>Up to 10 contacts per search</FeatureRow>
                <FeatureRow checkColor={T.purple} textColor={T.ink} weight={500}>Find hiring managers + firm search</FeatureRow>
                <FeatureRow checkColor={T.purple} textColor={T.ink} weight={500}>Single agent use</FeatureRow>
                <FeatureRow checkColor={T.purple} textColor={T.ink} weight={500}>Bulk drafting + CSV & Gmail export</FeatureRow>
                <FeatureRow checkColor={T.purple} textColor={T.ink} weight={500}>Save ~210 hours/mo on research</FeatureRow>
              </div>
            </div>

            {/* ELITE — night card */}
            <div
              className="of-card of-up"
              style={{
                position: 'relative',
                background: T.night,
                border: `1px solid ${T.night}`,
                borderRadius: 16,
                padding: '28px 26px',
                display: 'flex',
                flexDirection: 'column',
                boxShadow: '0 6px 20px rgba(15,37,69,0.16)',
                animationDelay: '.22s',
              }}
            >
              {currentTier === 'elite' && (
                <div style={{ position: 'absolute', top: -11, right: 20 }}>
                  <Chip color={T.greenFg} background={T.greenBg}>Active</Chip>
                </div>
              )}
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4, flexWrap: 'wrap' }}>
                <span
                  style={{
                    fontFamily: T.serif,
                    fontStyle: 'italic',
                    fontSize: 24,
                    fontWeight: 600,
                    background: ELITE_NAME_GRADIENT,
                    WebkitBackgroundClip: 'text',
                    backgroundClip: 'text',
                    WebkitTextFillColor: 'transparent',
                  }}
                >
                  Elite
                </span>
              </div>
              <div style={{ fontSize: 13, color: '#A9B2C4', marginBottom: 16 }}>For serious recruiting season</div>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 7, marginBottom: 2 }}>
                {showEduPrices && (
                  <span style={{ fontFamily: T.serif, fontSize: 22, fontWeight: 500, color: '#7C8595', textDecoration: 'line-through', fontVariantNumeric: 'tabular-nums' }}>
                    {fmt(eliteListPrice)}
                  </span>
                )}
                <span style={{ fontFamily: T.serif, fontSize: 40, fontWeight: 600, color: '#fff', letterSpacing: '-0.02em', fontVariantNumeric: 'tabular-nums' }}>
                  {fmt(eliteShown)}
                </span>
                <span style={{ fontSize: 14, color: '#A9B2C4' }}>{periodLabel}</span>
              </div>
              <div style={{ fontSize: 12.5, color: '#7C8595', marginBottom: 14, minHeight: 16 }}>
                <strong style={{ color: '#C3CAD6', fontWeight: 600 }}>~{eliteEmails} emails</strong> / month · {eliteStop.credits.toLocaleString()} credits
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 16 }}>
                {showEduPrices && (
                  <Chip color="#B9C4E4" background="rgba(156,168,205,0.16)" icon={<GraduationCap size={12} />}>
                    50% .edu discount
                  </Chip>
                )}
                <Chip color="#B9C4E4" background="rgba(156,168,205,0.16)">Cancel anytime</Chip>
              </div>

              {!ctaReady ? (
                <div aria-hidden style={{ padding: 13, borderRadius: 10, background: 'rgba(255,255,255,0.08)', color: 'transparent' }} className="animate-pulse select-none">&nbsp;</div>
              ) : (
                <button
                  onClick={
                    isLoading ? undefined :
                    currentTier === 'elite' && hasRealStripeSub
                      ? (e: React.MouseEvent) => {
                          if (e.shiftKey) {
                            handleResetCredits('elite');
                          } else {
                            handleManageSubscription();
                          }
                        }
                      : () => handleUpgrade('elite', 'pricing_page')
                  }
                  disabled={isLoading}
                  title={currentTier === 'elite' && hasRealStripeSub ? 'Click to manage subscription. Hold Shift+Click to reset credits.' : undefined}
                  className="of-cta of-cta-grad"
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: 7,
                    padding: 13,
                    borderRadius: 10,
                    background: ELITE_CTA_GRADIENT,
                    color: '#fff',
                    border: 'none',
                    fontWeight: 600,
                    fontSize: 15,
                    cursor: 'pointer',
                    boxShadow: '0 4px 14px rgba(124,58,237,0.35)',
                    opacity: isLoading ? 0.6 : 1,
                    fontFamily: T.sans,
                  }}
                >
                  {isLoading
                    ? 'Processing...'
                    : currentTier === 'elite' && hasRealStripeSub
                      ? 'Manage subscription'
                      : currentTier === 'elite'
                        ? 'Subscribe to Elite'
                        : currentTier === 'pro'
                          ? 'Upgrade to Elite'
                          : 'Get Elite'}
                  {!isLoading && <ArrowRight size={16} />}
                </button>
              )}

              <div style={{ borderTop: '1px solid rgba(255,255,255,0.1)', marginTop: 14, paddingTop: 18, display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ fontSize: 11.5, fontWeight: 700, color: '#7C8595', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: -1 }}>
                  Everything in Pro, plus
                </div>
                <FeatureRow checkColor={T.purpleLight} textColor="#E4E7EC">Run up to 5 agents simultaneously</FeatureRow>
                <FeatureRow checkColor={T.purpleLight} textColor="#E4E7EC">Up to 15 contacts per search</FeatureRow>
                <FeatureRow checkColor={T.purpleLight} textColor="#E4E7EC">Priority queue for contact generation</FeatureRow>
                <FeatureRow checkColor={T.purpleLight} textColor="#E4E7EC">Weekly personalized firm insights</FeatureRow>
                <FeatureRow checkColor={T.purpleLight} textColor="#E4E7EC">Priority support + early AI access</FeatureRow>
              </div>
            </div>
          </div>

          <button
            onClick={scrollToCompare}
            className="of-link"
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 7,
              margin: '30px auto 0',
              fontSize: 14,
              fontWeight: 600,
              color: T.ink3,
              background: 'none',
              border: 'none',
              cursor: 'pointer',
              fontFamily: T.sans,
            }}
          >
            Compare every feature
            <ChevronDown size={16} />
          </button>
        </div>
      </section>

      {/* ======= RECRUITING SEASON PASS ======= */}
      {seasonPassVisible(tierConfig.season_pass, true) && (
        <section style={{ padding: '8px 24px 20px' }}>
          <div
            style={{
              maxWidth: 1040,
              margin: '0 auto',
              background: NAVY_GRADIENT,
              borderRadius: 16,
              padding: '34px 40px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: 32,
              flexWrap: 'wrap',
              boxShadow: '0 16px 40px rgba(15,37,69,0.28)',
            }}
          >
            <div style={{ flex: '1 1 380px' }}>
              <span
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 6,
                  fontSize: 11,
                  fontWeight: 700,
                  letterSpacing: '0.06em',
                  textTransform: 'uppercase',
                  color: '#C9D3EE',
                  background: 'rgba(255,255,255,0.1)',
                  padding: '5px 11px',
                  borderRadius: 100,
                  marginBottom: 14,
                }}
              >
                <Zap size={11} />
                Recruiting season pass
              </span>
              <h3 style={{ fontFamily: T.serif, fontSize: 27, fontWeight: 600, color: '#fff', margin: '0 0 8px', letterSpacing: '-0.015em' }}>
                Four months. <Em color={T.purpleLight}>One charge.</Em>
              </h3>
              <p style={{ fontSize: 14, color: '#B7C0D4', margin: 0, maxWidth: 440, lineHeight: 1.6 }}>
                Recruiting season burns hot. Get Pro-level access, {tierConfig.season_pass.credits_per_month.toLocaleString()} credits
                refilled monthly, no renewal surprise. Pay once, hit the ground running.
              </p>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 14 }}>
              <div style={{ textAlign: 'right' }}>
                <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, justifyContent: 'flex-end' }}>
                  <span style={{ fontFamily: T.serif, fontSize: 46, fontWeight: 600, color: '#fff', letterSpacing: '-0.02em', fontVariantNumeric: 'tabular-nums' }}>
                    {fmt(tierConfig.season_pass[audience])}
                  </span>
                </div>
                <div style={{ fontSize: 12.5, color: '#9AA4B8', display: 'inline-flex', alignItems: 'center', gap: 5 }}>
                  <Clock size={12} />
                  {tierConfig.season_pass.months} months · ~{fmt(
                    tierConfig.season_pass[audience] / tierConfig.season_pass.months
                  )}/mo equivalent
                </div>
              </div>
              <button
                type="button"
                disabled={!seasonPassPriceId}
                onClick={() => {
                  trackUpgradeClick('season_pass', {
                    from_location: 'pricing_page',
                    plan_selected: 'season_pass',
                  });
                  trackSeasonPassClicked({ audience });
                  // One-time Season Pass checkout (grants season_pass tier
                  // via webhook). Gated on the SKU being wired in Stripe.
                  if (seasonPassPriceId) handleSeasonPassCheckout();
                }}
                title={!seasonPassPriceId ? 'Season Pass SKU coming soon in Stripe' : undefined}
                className="of-cta of-cta-white"
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 7,
                  padding: '13px 26px',
                  borderRadius: 10,
                  background: seasonPassPriceId ? '#fff' : 'rgba(255,255,255,0.4)',
                  color: T.heading,
                  border: 'none',
                  fontWeight: 600,
                  fontSize: 15,
                  cursor: seasonPassPriceId ? 'pointer' : 'not-allowed',
                  fontFamily: T.sans,
                }}
              >
                {seasonPassPriceId ? 'Get the Season Pass' : 'Coming soon'}
                {seasonPassPriceId && <ArrowRight size={16} />}
              </button>
            </div>
          </div>
        </section>
      )}

      {/* ======= TRUST BADGES ======= */}
      <section style={{ padding: '20px 24px 0' }}>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-5" style={{ maxWidth: 1040, margin: '0 auto' }}>
          <div style={{ background: BADGE_NAVY_GRADIENT, borderRadius: 16, padding: '22px 26px', boxShadow: '0 8px 24px rgba(15,37,69,0.2)' }}>
            <span
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                fontSize: 10.5,
                fontWeight: 700,
                letterSpacing: '0.06em',
                textTransform: 'uppercase',
                color: '#C9D3EE',
                background: 'rgba(255,255,255,0.1)',
                padding: '5px 10px',
                borderRadius: 100,
                marginBottom: 12,
              }}
            >
              <Clock size={12} />
              Free trial
            </span>
            <div style={{ fontFamily: T.serif, fontSize: 18, color: '#fff', fontWeight: 500 }}>
              <Em color={T.purpleLight}>{trialDays} days free</Em> on Pro
            </div>
          </div>
          <div style={{ background: BADGE_NAVY_GRADIENT, borderRadius: 16, padding: '22px 26px', boxShadow: '0 8px 24px rgba(15,37,69,0.2)' }}>
            <span
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                fontSize: 10.5,
                fontWeight: 700,
                letterSpacing: '0.06em',
                textTransform: 'uppercase',
                color: '#C9D3EE',
                background: 'rgba(255,255,255,0.1)',
                padding: '5px 10px',
                borderRadius: 100,
                marginBottom: 12,
              }}
            >
              <Shield size={12} />
              Money-back
            </span>
            <div style={{ fontFamily: T.serif, fontSize: 18, color: '#fff', fontWeight: 500 }}>
              <Em color={T.purpleLight}>7-day refund</Em> on Pro & Elite, 14 days on Season Pass
            </div>
          </div>
        </div>
      </section>

      {/* ======= COMPARISON TABLE ======= */}
      <section id="compare" style={{ padding: '60px 24px 70px' }}>
        <div style={{ maxWidth: 1040, margin: '0 auto' }}>
          <div style={{ textAlign: 'center', marginBottom: 40 }}>
            <Eyebrow>Side by side</Eyebrow>
            <h2 style={{ fontFamily: T.serif, fontSize: 32, fontWeight: 600, letterSpacing: '-0.02em', color: T.heading, margin: '0 0 10px' }}>
              Compare <Em>every feature</Em>
            </h2>
            <p style={{ fontSize: 15, color: T.ink3, margin: '0 auto', maxWidth: 460, lineHeight: 1.6 }}>
              The full picture of what Free, Pro, and Elite unlock across the workflow.
            </p>
          </div>

          <div style={{ background: T.paper, border: `1px solid ${T.border}`, borderRadius: 16, overflow: 'hidden', boxShadow: '0 6px 20px rgba(15,37,69,0.08)' }}>
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', minWidth: 620, borderCollapse: 'collapse', fontSize: 14 }}>
                <thead>
                  <tr>
                    <th style={{ textAlign: 'left', padding: '20px 26px 16px', fontFamily: T.sans, fontSize: 12, fontWeight: 600, color: T.ink3, textTransform: 'uppercase', letterSpacing: '0.05em', width: '42%' }}>Feature</th>
                    <th style={{ textAlign: 'center', padding: '20px 14px 16px', fontFamily: T.serif, fontSize: 17, fontWeight: 600, color: T.heading }}>Free</th>
                    <th style={{ textAlign: 'center', padding: '20px 14px 16px', fontFamily: T.serif, fontStyle: 'italic', fontSize: 17, fontWeight: 600, color: T.purple }}>Pro</th>
                    <th style={{ textAlign: 'center', padding: '20px 14px 16px', fontFamily: T.serif, fontStyle: 'italic', fontSize: 17, fontWeight: 600, color: T.purpleDeep }}>Elite</th>
                  </tr>
                </thead>
                <tbody>
                  {compareGroups.map(([groupLabel, rows]) => (
                    <Fragment key={groupLabel}>
                      <tr>
                        <td colSpan={4} style={{ padding: '20px 26px 8px', fontFamily: T.sans, fontSize: 11.5, fontWeight: 700, color: T.ink3, textTransform: 'uppercase', letterSpacing: '0.07em', background: T.paper2, borderTop: `1px solid ${T.borderLight}` }}>
                          {groupLabel}
                        </td>
                      </tr>
                      {rows.map(([name, free, pro, elite]) => (
                        <tr key={name} style={{ borderTop: `1px solid ${T.borderLight}` }}>
                          <td style={{ padding: '14px 26px', color: T.ink2, fontSize: 13.5 }}>{name}</td>
                          <td style={{ padding: '14px 14px', textAlign: 'center' }}><CompareCellValue value={free} /></td>
                          <td style={{ padding: '14px 14px', textAlign: 'center', background: 'rgba(124,58,237,0.035)' }}><CompareCellValue value={pro} isPro /></td>
                          <td style={{ padding: '14px 14px', textAlign: 'center' }}><CompareCellValue value={elite} /></td>
                        </tr>
                      ))}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {!hasActiveSubscription && (
            <div style={{ textAlign: 'center', marginTop: 40 }}>
              <button
                onClick={() => handleUpgrade('pro', 'compare_table')}
                disabled={isLoading}
                className="of-cta of-cta-grad"
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '15px 32px',
                  borderRadius: 10,
                  background: CTA_GRADIENT,
                  color: '#fff',
                  border: 'none',
                  fontWeight: 600,
                  fontSize: 16,
                  cursor: 'pointer',
                  boxShadow: '0 4px 14px rgba(124,58,237,0.32)',
                  opacity: isLoading ? 0.6 : 1,
                  fontFamily: T.sans,
                }}
              >
                Start your {trialDays}-day free trial
                <ArrowRight size={18} />
              </button>
              <div style={{ fontSize: 13, color: T.ink3, marginTop: 13 }}>{trialDays} days free, then {fmt(proMonthlyPrice)}/mo · cancel anytime</div>
            </div>
          )}
        </div>
      </section>

      {/* ======= CREDIT PACKS — Pro/Elite subscriber perk. Free users see a
          locked state that scrolls back up to start a trial. ======= */}
      <section style={{ padding: '0 24px 20px' }}>
        <div style={{ maxWidth: 1040, margin: '0 auto' }}>
          <div style={{ textAlign: 'center', marginBottom: 36 }}>
            <Eyebrow>Credit packs · Pro & Elite perk</Eyebrow>
            <h2 style={{ fontFamily: T.serif, fontSize: 32, fontWeight: 600, letterSpacing: '-0.02em', color: T.heading, margin: '0 0 10px' }}>
              Need more credits? <Em>Top up anytime.</Em>
            </h2>
            <p style={{ fontSize: 15, color: T.ink3, margin: '0 auto', maxWidth: 480, lineHeight: 1.6 }}>
              {hasActiveSubscription ? (
                <>One-time top-up packs, available to Pro & Elite subscribers. Credits you buy <strong style={{ color: T.ink2, fontWeight: 600 }}>never expire.</strong></>
              ) : (
                <>Credit packs are a perk for Pro & Elite subscribers. Start a Pro trial to unlock. And yes, <strong style={{ color: T.ink2, fontWeight: 600 }}>purchased credits never expire.</strong></>
              )}
            </p>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-5" style={{ paddingTop: 14 }}>
            {tierConfig.topup_packs.map((pack) => {
              const locked = !hasActiveSubscription;
              const featured = Boolean(pack.recommended);
              return (
                <button
                  key={pack.id}
                  type="button"
                  onClick={() => {
                    if (locked) {
                      // Scroll back to the pricing cards so the user can start a Pro trial.
                      window.scrollTo({ top: 0, behavior: 'smooth' });
                    } else {
                      setTopUpModalOpen(true);
                    }
                  }}
                  className="of-card"
                  style={{
                    position: 'relative',
                    textAlign: 'left',
                    borderRadius: 16,
                    padding: 24,
                    cursor: 'pointer',
                    opacity: locked ? 0.75 : 1,
                    fontFamily: T.sans,
                    ...(featured
                      ? {
                          border: '2px solid transparent',
                          backgroundImage: GRADIENT_BORDER_BG,
                          backgroundOrigin: 'border-box',
                          backgroundClip: 'padding-box, border-box',
                          boxShadow: '0 12px 30px rgba(124,58,237,0.14)',
                        }
                      : {
                          background: T.paper,
                          border: `1px solid ${T.border}`,
                          boxShadow: '0 1px 3px rgba(15,37,69,0.06)',
                        }),
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
                    <span style={{ fontSize: 11, fontWeight: 700, letterSpacing: '0.06em', textTransform: 'uppercase', color: featured ? T.purple : T.ink3 }}>
                      {pack.label}
                    </span>
                    {featured && (
                      <span style={{ fontSize: 10, fontWeight: 700, color: T.magentaDeep, background: T.pinkTint, padding: '3px 8px', borderRadius: 100 }}>
                        Best value
                      </span>
                    )}
                  </div>
                  <div style={{ fontFamily: T.serif, fontSize: 34, fontWeight: 600, color: T.heading, lineHeight: 1, fontVariantNumeric: 'tabular-nums' }}>
                    {pack.credits.toLocaleString()}
                  </div>
                  <div style={{ fontSize: 13, color: T.ink4, marginBottom: 20 }}>credits</div>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', borderTop: `1px solid ${T.borderLight}`, paddingTop: 16 }}>
                    <span style={{ fontSize: 18, fontWeight: 700, color: T.heading, fontVariantNumeric: 'tabular-nums' }}>${pack.price}</span>
                    <span
                      style={{
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: 5,
                        fontSize: 13,
                        fontWeight: 600,
                        color: featured ? T.purple : T.primary,
                      }}
                    >
                      {locked ? (<><Lock size={12} /> Upgrade</>) : (<><Plus size={13} /> Add</>)}
                    </span>
                  </div>
                </button>
              );
            })}
          </div>
        </div>
      </section>

      {/* ======= FAQ ======= */}
      <section style={{ padding: '60px 24px 60px' }}>
        <div style={{ maxWidth: 720, margin: '0 auto' }}>
          <div style={{ marginBottom: 32 }}>
            <Eyebrow>FAQ</Eyebrow>
            <h2 style={{ fontFamily: T.serif, fontSize: 32, fontWeight: 600, letterSpacing: '-0.02em', color: T.heading, margin: 0 }}>
              Things students <Em>ask us</Em>
            </h2>
          </div>
          <div>
            {faqs.map(([q, a], i) => (
              <FAQItem
                key={q}
                question={q}
                answer={a}
                isOpen={openFaq === i}
                onToggle={() => setOpenFaq(openFaq === i ? null : i)}
              />
            ))}
          </div>

          <p style={{ fontSize: 14, color: T.ink3, marginTop: 28 }}>
            Still unsure?{' '}
            <button
              onClick={() => window.open('mailto:support@offerloop.ai', '_blank')}
              style={{ background: 'none', border: 'none', padding: 0, cursor: 'pointer', color: T.primary, fontWeight: 600, fontSize: 14, fontFamily: T.sans }}
            >
              Talk to us
            </button>
          </p>
        </div>
      </section>

      {/* Top-Up Modal — one-time credit-pack purchase. Wired to /api/billing/
          create-topup-session in Wave 1 backend; cofounders enable Stripe SKUs. */}
      <TopUpModal
        open={topUpModalOpen}
        onClose={() => setTopUpModalOpen(false)}
        fromFeature="pricing_page"
        onPurchase={async (pack, priceId) => {
          if (!user) {
            navigate('/signin?next=/pricing');
            return;
          }
          if (!priceId) {
            // Cofounders haven't wired this top-up SKU in Stripe yet.
            alert(`The ${pack.label} pack will be available shortly.`);
            return;
          }
          try {
            const auth = getAuth();
            const fbUser = auth.currentUser;
            if (!fbUser) throw new Error('No Firebase user');
            const token = await fbUser.getIdToken();
            const res = await fetch(`${BACKEND_URL}/api/billing/create-topup-session`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
              body: JSON.stringify({
                packId: pack.id,
                priceId,
                successUrl: `${window.location.origin}/payment-success?topup=${pack.id}`,
                cancelUrl: `${window.location.origin}/pricing`,
              }),
            });
            if (!res.ok) throw new Error(`Top-up checkout failed: ${res.status}`);
            const { sessionId } = await res.json();
            const stripe = await stripePromise;
            if (!stripe) throw new Error('Stripe not initialized');
            await stripe.redirectToCheckout({ sessionId });
          } catch (err) {
            console.error('Top-up error:', err);
            alert(err instanceof Error ? err.message : 'Top-up failed');
          }
        }}
      />
    </div>
  );
};

export default Pricing;
