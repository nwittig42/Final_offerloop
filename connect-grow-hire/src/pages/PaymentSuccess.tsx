import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useFirebaseAuth } from "../contexts/FirebaseAuthContext";
import { useEffect, useState, useRef } from "react";
import { getAuth } from 'firebase/auth';
import { trackCheckoutCompleted, trackError } from "../lib/analytics";
import { BACKEND_URL } from "@/services/api";
import { PostCheckoutUpsell } from "@/components/PostCheckoutUpsell";
import { isEduEligible } from "@/lib/eduDiscount";

export default function PaymentSuccess() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const { user, refreshUser } = useFirebaseAuth();
  const [status, setStatus] = useState<'processing' | 'success' | 'error' | 'upsell'>('processing');
  const [message, setMessage] = useState('Completing your upgrade...');
  const hasProcessedRef = useRef(false); // Prevent multiple calls
  // Tracks what tier the just-completed checkout landed on. Drives the upsell
  // gate — we only offer Pro→Elite, never Elite→anything.
  const [purchasedTier, setPurchasedTier] = useState<string | null>(null);

  // Try multiple parameter names in case Stripe uses different ones
  // Also check URL hash and full URL for session ID
  const sessionId = 
    params.get("session_id") || 
    params.get("sessionId") || 
    params.get("session") ||
    (window.location.hash.includes('session_id=') 
      ? new URLSearchParams(window.location.hash.split('?')[1] || '').get('session_id')
      : null) ||
    (window.location.search.includes('session_id=')
      ? new URLSearchParams(window.location.search).get('session_id')
      : null);

  useEffect(() => {
    async function completeUpgrade() {
      // Prevent multiple calls
      if (hasProcessedRef.current) {
        console.log('PaymentSuccess: Already processed, skipping...');
        return;
      }
      
      console.log('PaymentSuccess: Starting upgrade process', { 
        user: !!user, 
        sessionId,
        url: window.location.href,
        search: window.location.search,
        hash: window.location.hash
      });
      
      if (!user) {
        console.error('PaymentSuccess: No user found');
        setStatus('error');
        setMessage('You must be logged in to complete the upgrade. Please sign in and try again.');
        return;
      }
      
      // Check if user is already pro (might have been upgraded by webhook)
      if (user.tier === 'pro') {
        console.log('PaymentSuccess: User is already Pro, skipping upgrade call');
        setStatus('success');
        setMessage('You are already upgraded to Pro! 🎉');
        setTimeout(() => {
          navigate('/find');
        }, 2000);
        hasProcessedRef.current = true;
        return;
      }
      
      // Mark as processing to prevent duplicate calls
      hasProcessedRef.current = true;
      
      // If no session ID, check if user is already upgraded (webhook might have processed it)
      if (!sessionId) {
        console.warn('PaymentSuccess: No session ID in URL, checking if user is already upgraded...');
        
        try {
          const auth = getAuth();
          const firebaseUser = auth.currentUser;
          
          if (!firebaseUser) {
            throw new Error('Not authenticated');
          }

          const token = await firebaseUser.getIdToken();
          
          const API_URL = BACKEND_URL;

          // Check subscription status via API
          const statusResponse = await fetch(`${API_URL}/api/subscription-status`, {
            method: 'GET',
            headers: {
              'Authorization': `Bearer ${token}`,
            },
          });

          if (statusResponse.ok) {
            const statusData = await statusResponse.json();
            console.log('PaymentSuccess: Subscription status:', statusData);
            
            // If user is already pro, show success
            if (statusData.subscribed && statusData.tier === 'pro') {
              console.log('PaymentSuccess: User is already upgraded to Pro');
              await refreshUser(); // Refresh local state
              setStatus('success');
              setMessage('You are already upgraded to Pro! 🎉');
              setTimeout(() => {
                navigate('/find');
              }, 2000);
              return;
            }
          }
          
          // If not upgraded, show error but with helpful message
          console.error('PaymentSuccess: No session ID and user not upgraded');
          setStatus('error');
          setMessage('Missing session information. If you just completed payment, the upgrade may be processing. Please wait a moment and refresh, or contact support with your payment confirmation.');
          return;
        } catch (checkError) {
          console.error('PaymentSuccess: Error checking user status:', checkError);
          setStatus('error');
          setMessage('Missing session information. Please contact support with your payment confirmation.');
          return;
        }
      }

      try {
        setStatus('processing');
        setMessage('Completing your upgrade...');

        const auth = getAuth();
        const firebaseUser = auth.currentUser;
        
        if (!firebaseUser) {
          throw new Error('Not authenticated');
        }

        const token = await firebaseUser.getIdToken();
        
        const API_URL = BACKEND_URL;

        console.log('PaymentSuccess: Calling complete-upgrade endpoint...', { API_URL, sessionId });

        // Call the manual upgrade endpoint
        const response = await fetch(`${API_URL}/api/complete-upgrade`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${token}`,
          },
          body: JSON.stringify({ sessionId })
        });

        console.log('PaymentSuccess: Response status:', response.status);

        const result = await response.json();
        console.log('PaymentSuccess: Response data:', result);

        if (response.ok && result.success) {
          console.log('PaymentSuccess: Upgrade successful:', result);

          // Track checkout completion
          const plan = (result.tier as string) || 'pro';
          trackCheckoutCompleted(plan);
          setPurchasedTier(plan);

          // Refresh user data
          try {
            await refreshUser();
            console.log('PaymentSuccess: User data refreshed');
          } catch (refreshError) {
            console.error('PaymentSuccess: Error refreshing user:', refreshError);
            // Don't fail the upgrade if refresh fails
          }

          // Branch: offer the Pro→Elite upsell when:
          //   - the purchased tier is Pro (not Elite — no upsell out of Elite)
          //   - the user hasn't seen the upsell before (`upsellShownAt` empty)
          //   - money actually moved ('paid'). Trial checkouts are
          //     'no_payment_required' — pitching "$10 more" against a $0
          //     charge is incoherent, and the modal's renewal math assumes a
          //     paid first month.
          // Backend re-validates the existing-discount collision guard.
          const alreadySaw = Boolean(result.upsellShownAt);
          const paidNow = result.paymentStatus === 'paid';
          if (plan === 'pro' && !alreadySaw && paidNow) {
            setStatus('upsell');
            // Fire telemetry — landed on the upsell screen.
            try {
              const { trackUpgradeClick } = await import('@/lib/analytics');
              trackUpgradeClick('post_checkout_upsell_shown', {
                from_location: 'payment_success',
                plan_selected: 'pro',
              });
            } catch {
              /* analytics best-effort */
            }
          } else {
            setStatus('success');
            setMessage(
              result.subscriptionStatus === 'trialing'
                ? `Your 7-day free trial of ${plan === 'elite' ? 'Elite' : 'Pro'} has started!`
                : `Successfully upgraded to ${plan === 'elite' ? 'Elite' : 'Pro'}!`,
            );
            setTimeout(() => {
              navigate('/find');
            }, 2000);
          }
        } else {
          console.error('PaymentSuccess: Upgrade failed:', result);
          const errorMsg = result.error || result.message || 'Upgrade failed. Please contact support.';
          setStatus('error');
          setMessage(errorMsg);
          trackError('checkout', 'complete_upgrade', 'api_error');
        }

      } catch (e) {
        console.error("PaymentSuccess: Upgrade error:", e);
        const errorMessage = e instanceof Error ? e.message : 'Unknown error';
        setStatus('error');
        setMessage(`An error occurred during upgrade: ${errorMessage}. Please contact support if your account is not upgraded within 5 minutes.`);
        trackError('checkout', 'complete_upgrade', 'network_error');
      }
    }

    // Only run if we have a user and haven't processed yet
    if (user && !hasProcessedRef.current) {
      completeUpgrade();
    }
  }, [user, sessionId]); // Removed refreshUser and navigate from dependencies to prevent re-runs

  // Pro→Elite upsell screen. Brand-honest version of Higgsfield's post-checkout
  // next-tier offer: $10 more right now, $25 effective this month on Elite,
  // renews at $35 on Day 31 (date is plainly stated in the component copy).
  if (status === 'upsell' && purchasedTier === 'pro') {
    return (
      <PostCheckoutUpsell
        // .edu subscribers paid $4.99 for Pro and renew Elite at the $17.49
        // edu SKU (backend picks it in apply_post_checkout_upsell).
        effectiveFirstMonthPrice={isEduEligible(user) ? 14.99 : 19.99}
        fullElitePrice={isEduEligible(user) ? 17.49 : 34.99}
        onAccepted={async () => {
          try {
            await refreshUser();
          } catch {
            /* refresh best-effort */
          }
          navigate('/find');
        }}
        onDeclined={() => {
          navigate('/find');
        }}
      />
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-[#0F172A] text-white px-6">
      <div className="max-w-md w-full text-center">
        {status === 'processing' && (
          <div className="bg-[#1E293B] p-8 rounded-[3px]">
            <Loader2 className="h-16 w-16 text-[#3B82F6] mx-auto mb-4 animate-spin" />
            <h1 className="text-3xl font-bold mb-2">Processing Payment</h1>
            <p className="text-gray-400 mb-6">{message}</p>
          </div>
        )}

        {status === 'success' && (
          <div className="bg-[#1E293B] p-8 rounded-[3px]">
            <CheckCircle2 className="h-16 w-16 text-[#3B82F6] mx-auto mb-4" />
            <h1 className="text-3xl font-bold mb-2">Welcome to Pro!</h1>
            <p className="text-gray-400 mb-2">{message}</p>
            <p className="text-sm text-gray-500 mb-6">
              You now have access to all premium features.
            </p>
            <p className="text-sm text-gray-600">Redirecting to home...</p>
          </div>
        )}

        {status === 'error' && (
          <div className="bg-[#1E293B] p-8 rounded-[3px]">
            <XCircle className="h-16 w-16 text-red-400 mx-auto mb-4" />
            <h1 className="text-3xl font-bold mb-2">Upgrade Issue</h1>
            <p className="text-gray-400 mb-6">{message}</p>
            <div className="space-y-3">
              <Button 
                onClick={() => window.location.reload()} 
                className="w-full bg-[#3B82F6] hover:bg-[#2563EB]"
              >
                Try Again
              </Button>
              <Button 
                onClick={() => navigate("/find")}
                variant="outline"
                className="w-full"
              >
                Find people
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}