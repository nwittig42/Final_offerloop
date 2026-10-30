// src/pages/SignIn.tsx
import React, { useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ArrowLeft, Eye, EyeOff } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useFirebaseAuth, friendlyAuthError } from "@/contexts/FirebaseAuthContext";
import OfferloopLogo from '@/assets/offerloop_logo2.png';



type Tab = "signin" | "signup";

// Password field with an eye button to show or hide what's typed.
const PasswordInput: React.FC<{
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  autoComplete: string;
  shown: boolean;
  onToggle: () => void;
  invalid?: boolean;
}> = ({ value, onChange, placeholder, autoComplete, shown, onToggle, invalid }) => (
  <div className="relative">
    <input
      type={shown ? "text" : "password"}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      aria-label={placeholder}
      aria-invalid={invalid || undefined}
      required
      minLength={6}
      autoComplete={autoComplete}
      className="w-full py-3 pl-4 pr-11 rounded-[10px] text-sm"
      style={{ border: `1px solid ${invalid ? '#DC2626' : 'var(--border-light)'}`, fontFamily: 'var(--font-body)', fontSize: '15px' }}
    />
    <button
      type="button"
      onClick={onToggle}
      aria-label={shown ? "Hide password" : "Show password"}
      className="absolute right-3 top-1/2 -translate-y-1/2 p-1"
      style={{ color: 'var(--text-tertiary)', background: 'none', border: 'none', cursor: 'pointer' }}
    >
      {shown ? <EyeOff size={18} /> : <Eye size={18} />}
    </button>
  </div>
);

const SignIn: React.FC = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const { toast } = useToast();
  const {
    user,
    isLoading,
    signIn,
    signInWithApple,
    signUpWithEmail,
    signInWithEmail,
    resetPassword,
  } = useFirebaseAuth();

  const initialTab: Tab = useMemo(() => {
    const sp = new URLSearchParams(location.search);
    return sp.get("mode") === "signup" ? "signup" : "signin";
  }, [location.search]);

  const [activeTab, setActiveTab] = useState<Tab>(initialTab);
  const [submitting, setSubmitting] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [fullName, setFullName] = useState("");
  const [resetSent, setResetSent] = useState(false);

  const passwordsDiffer = activeTab === "signup" && password !== confirmPassword;
  // Only flag a mismatch once they've started typing the confirmation.
  const showMismatch = passwordsDiffer && confirmPassword.length > 0;
  const emailSubmitBlocked = submitting || isLoading || passwordsDiffer;

  const forceNavigate = (dest: string) => {
    navigate(dest, { replace: true });
    setTimeout(() => {
      const at = window.location.pathname;
      if (at !== dest) {
        console.warn("[signin] router nav didn't apply, forcing hard redirect", { at, dest });
        window.location.replace(dest);
      }
    }, 600);
  };

  // Honor a ?next= or ?redirect= return path (e.g. /pricing when the visitor
  // clicked "Start free trial" while signed out) so purchase intent survives
  // the sign-in round-trip. Internal paths only — never protocol-relative or
  // absolute URLs. Onboarding still wins for brand-new accounts.
  const returnPath = useMemo(() => {
    const sp = new URLSearchParams(location.search);
    const raw = sp.get("next") || sp.get("redirect") || "";
    return raw.startsWith("/") && !raw.startsWith("//") ? raw : null;
  }, [location.search]);

  const finishAuth = (next: "onboarding" | "home") =>
    forceNavigate(next === "onboarding" ? "/onboarding" : (returnPath ?? "/find"));

  useEffect(() => setActiveTab(initialTab), [initialTab]);

  // Capture referral code from ?ref= query param and persist in localStorage
  useEffect(() => {
    const ref = new URLSearchParams(window.location.search).get('ref');
    if (ref) {
      localStorage.setItem('offerloop_ref', ref.trim().toUpperCase());
    }
  }, []);


  // Already-signed-in visitors go straight to the app. The Gmail consent
  // step now lives in onboarding, so /signin no longer redirects to OAuth.
  useEffect(() => {
    if (location.pathname !== '/signin') return;
    if (isLoading || !user) return;

    // Coming from sign-out: don't auto-navigate back into the app.
    const params = new URLSearchParams(location.search);
    if (params.get("signout") === "true") return;

    forceNavigate(user.needsOnboarding ? "/onboarding" : (returnPath ?? "/find"));
  }, [user, isLoading, location.search, location.pathname, returnPath]);

  const handleGoogleAuth = async () => {
    if (submitting || isLoading) return;
    setSubmitting(true);
    try {
      // select_account forces Google's account picker every time — after
      // backing out of onboarding (which signs the user out), sign-in must
      // start fresh instead of silently reusing the last Google session.
      const next = await signIn({ prompt: "select_account" });
      finishAuth(next);
    } catch (err) {
      console.error("[signin] failed:", err);
      setSubmitting(false);
      toast({
        variant: "destructive",
        title: "Sign-in failed",
        description: friendlyAuthError(err),
      });
    }
  };

  const handleEmailSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (emailSubmitBlocked) return;
    setSubmitting(true);
    try {
      const next =
        activeTab === "signup"
          ? await signUpWithEmail(fullName, email.trim(), password)
          : await signInWithEmail(email.trim(), password);
      finishAuth(next);
    } catch (err) {
      setSubmitting(false);
      toast({ variant: "destructive", title: "Sign-in failed", description: friendlyAuthError(err) });
    }
  };

  const handleAppleAuth = async () => {
    if (submitting || isLoading) return;
    setSubmitting(true);
    try {
      finishAuth(await signInWithApple());
    } catch (err) {
      setSubmitting(false);
      toast({ variant: "destructive", title: "Sign-in failed", description: friendlyAuthError(err) });
    }
  };

  const handleForgotPassword = async () => {
    if (!email.trim()) {
      toast({ title: "Enter your email first", description: "Type your email above, then tap Forgot password." });
      return;
    }
    try {
      await resetPassword(email.trim());
      setResetSent(true);
    } catch (err) {
      toast({ variant: "destructive", title: "Could not send reset email", description: friendlyAuthError(err) });
    }
  };

  return (
    <div
      className="min-h-screen w-full flex flex-col items-center justify-center relative overflow-hidden"
      style={{
        background: 'var(--bg-white)',
        fontFamily: 'var(--font-body)',
      }}
    >
      {/* Background glow */}
      <div
        className="absolute top-[-200px] right-[-150px] w-[600px] h-[600px] rounded-full pointer-events-none"
        style={{
          background: 'radial-gradient(circle, rgba(59, 130, 246, 0.06) 0%, transparent 70%)',
        }}
      />
      <div
        className="absolute bottom-[-150px] left-[-100px] w-[400px] h-[400px] rounded-full pointer-events-none"
        style={{
          background: 'radial-gradient(circle, rgba(59, 130, 246, 0.04) 0%, transparent 70%)',
        }}
      />

      {/* Back to Home */}
      <div className="absolute top-6 left-6 md:top-8 md:left-12">
        <button
          onClick={() => navigate("/")}
          className="inline-flex items-center gap-2 text-sm transition-colors"
          style={{
            fontFamily: 'var(--font-body)',
            fontSize: '14px',
            fontWeight: 500,
            color: 'var(--text-tertiary)',
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.color = 'var(--text-secondary)';
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.color = 'var(--text-tertiary)';
          }}
        >
          <ArrowLeft className="h-4 w-4" />
          Back to Home
        </button>
      </div>

      {/* Sign-in Card */}
      <div
        className="relative w-full max-w-[440px] mx-auto px-6"
        style={{ position: 'relative', zIndex: 1 }}
      >
        <div
          className="rounded-[3px] transition-all"
          style={{
            background: 'var(--bg-white)',
            border: '1px solid var(--border-light)',
            boxShadow: '0 1px 2px rgba(0,0,0,0.02), 0 4px 12px rgba(0,0,0,0.04), 0 16px 40px rgba(0,0,0,0.04)',
            padding: '48px 40px',
          }}
        >
          {/* Logo */}
          <div className="text-center mb-8">
            <img
              src={OfferloopLogo}
              alt="Offerloop"
              className="h-16 mx-auto mb-6"
            />
            <h1
              style={{
                fontFamily: "'Instrument Serif', Georgia, serif",
                fontSize: '36px',
                fontWeight: 400,
                letterSpacing: '-0.025em',
                color: 'var(--text-primary)',
                lineHeight: 1.1,
                marginBottom: '8px',
              }}
            >
              {activeTab === "signup" ? "Create your account" : "Welcome back"}
            </h1>
            <p
              style={{
                fontFamily: 'var(--font-body)',
                fontSize: '15px',
                color: 'var(--text-secondary)',
              }}
            >
              {activeTab === "signup"
                ? "Get started with Offerloop in seconds"
                : "Sign in to continue to your account"}
            </p>
          </div>

          {/* Tabs */}
          <div
            className="flex gap-2 mb-8"
            style={{
              background: 'var(--bg-off)',
              borderRadius: '10px',
              padding: '4px',
            }}
          >
            <button
              onClick={() => setActiveTab("signin")}
              disabled={submitting}
              className="flex-1 py-2 rounded-[8px] text-sm font-medium transition-all"
              style={{
                fontFamily: 'var(--font-body)',
                fontSize: '14px',
                fontWeight: 500,
                background: activeTab === "signin" ? 'var(--bg-white)' : 'transparent',
                color: activeTab === "signin" ? 'var(--text-primary)' : 'var(--text-tertiary)',
                boxShadow: activeTab === "signin" ? '0 1px 3px rgba(0,0,0,0.06)' : 'none',
                border: 'none',
                cursor: 'pointer',
              }}
            >
              Sign in
            </button>
            <button
              onClick={() => setActiveTab("signup")}
              disabled={submitting}
              className="flex-1 py-2 rounded-[8px] text-sm font-medium transition-all"
              style={{
                fontFamily: 'var(--font-body)',
                fontSize: '14px',
                fontWeight: 500,
                background: activeTab === "signup" ? 'var(--bg-white)' : 'transparent',
                color: activeTab === "signup" ? 'var(--text-primary)' : 'var(--text-tertiary)',
                boxShadow: activeTab === "signup" ? '0 1px 3px rgba(0,0,0,0.06)' : 'none',
                border: 'none',
                cursor: 'pointer',
              }}
            >
              Create account
            </button>
          </div>

          {/* Google Sign-In Button (recommended path, shown first) */}
          <div style={{ position: 'relative', marginTop: 4 }}>
            <span
              style={{
                position: 'absolute',
                top: -9,
                right: 14,
                zIndex: 1,
                padding: '2px 10px',
                borderRadius: 999,
                background: '#2563EB',
                color: 'white',
                fontSize: '11px',
                fontWeight: 600,
                fontFamily: 'var(--font-body)',
                boxShadow: '0 1px 3px rgba(37, 99, 235, 0.3)',
              }}
            >
              Recommended
            </span>
            <button
              onClick={handleGoogleAuth}
              disabled={submitting || isLoading}
              className="w-full flex items-center justify-center gap-2.5 py-3.5 rounded-[10px] text-sm font-medium transition-all"
              style={{
                background: 'white',
                color: '#0F172A',
                fontFamily: 'var(--font-body)',
                fontSize: '15px',
                fontWeight: 600,
                border: '1px solid var(--border-light)',
                boxShadow: '0 1px 2px rgba(0,0,0,0.04)',
                cursor: submitting || isLoading ? 'not-allowed' : 'pointer',
                opacity: submitting || isLoading ? 0.6 : 1,
              }}
              onMouseEnter={(e) => {
                if (!submitting && !isLoading) {
                  e.currentTarget.style.background = '#F8FAFC';
                  e.currentTarget.style.borderColor = '#CBD5E1';
                }
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = 'white';
                e.currentTarget.style.borderColor = 'var(--border-light)';
              }}
            >
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" style={{ width: 18, height: 18, flexShrink: 0 }} aria-hidden="true">
                <path
                  fill="#FFC107"
                  d="M43.611 20.083H42V20H24v8h11.303C33.96 32.99 29.453 36 24 36c-6.627 0-12-5.373-12-12s5.373-12 12-12c3.06 0 5.84 1.154 7.949 3.042l5.657-5.657C34.869 6.057 29.706 4 24 4 12.954 4 4 12.954 4 24s8.954 20 20 20c10.493 0 19.128-8.08 19.128-20 0-1.341-.138-2.651-.4-3.917z"
                />
                <path
                  fill="#FF3D00"
                  d="M6.306 14.691l6.571 4.817C14.39 16.564 18.879 14 24 14c3.06 0 5.84 1.154 7.949 3.042l5.657-5.657C34.869 6.057 29.706 4 24 4c-7.668 0-14.266 4.343-17.694 10.691z"
                />
                <path
                  fill="#4CAF50"
                  d="M24 44c5.453 0 10.01-1.787 13.49-4.852l-6.23-5.253C29.207 35.385 26.78 36 24 36c-5.438 0-10.028-3.668-11.66-8.67l-6.5 5.01C8.257 38.926 15.44 44 24 44z"
                />
                <path
                  fill="#1976D2"
                  d="M43.611 20.083H42V20H24v8h11.303c-1.098 3.24-3.48 5.773-6.043 7.091l6.23 5.253C37.147 38.47 40 32.943 40 26c0-2.055-.222-3.92-.611-5.917z"
                />
              </svg>
              {submitting ? "Connecting..." : activeTab === "signup" ? "Continue with Google" : "Sign in with Google"}
            </button>
          </div>

          {/* Apple Sign-In Button (flagged off until Apple Developer setup is done) */}
          {import.meta.env.VITE_ENABLE_APPLE_SIGNIN === "true" && (
            <button
              onClick={handleAppleAuth}
              disabled={submitting || isLoading}
              className="w-full flex items-center justify-center gap-2.5 py-3.5 rounded-[10px] text-sm font-medium transition-all mt-3"
              style={{
                background: 'white',
                color: '#0F172A',
                fontFamily: 'var(--font-body)',
                fontSize: '15px',
                fontWeight: 600,
                border: '1px solid var(--border-light)',
                boxShadow: '0 1px 2px rgba(0,0,0,0.04)',
                cursor: submitting || isLoading ? 'not-allowed' : 'pointer',
              }}
            >
              <svg viewBox="0 0 24 24" style={{ width: 18, height: 18, flexShrink: 0 }} fill="currentColor" aria-hidden="true">
                <path d="M17.05 20.28c-.98.95-2.05.8-3.08.35-1.09-.46-2.09-.48-3.24 0-1.44.62-2.2.44-3.06-.35C2.79 15.25 3.51 7.59 9.05 7.31c1.35.07 2.29.74 3.08.8 1.18-.24 2.31-.93 3.57-.84 1.51.12 2.65.72 3.4 1.8-3.12 1.87-2.38 5.98.48 7.13-.57 1.5-1.31 2.99-2.53 4.08zM12.03 7.25c-.15-2.23 1.66-4.07 3.74-4.25.29 2.58-2.34 4.5-3.74 4.25z" />
              </svg>
              Continue with Apple
            </button>
          )}

          {/* Divider */}
          <div className="flex items-center gap-3 my-5">
            <div style={{ flex: 1, height: 1, background: 'var(--border-light)' }} />
            <span style={{ fontFamily: 'var(--font-body)', fontSize: '12px', color: 'var(--text-tertiary)' }}>
              {activeTab === "signup" ? "or sign up with email" : "or sign in with email"}
            </span>
            <div style={{ flex: 1, height: 1, background: 'var(--border-light)' }} />
          </div>

          {/* Email + password form */}
          <form onSubmit={handleEmailSubmit} className="space-y-3">
            {activeTab === "signup" && (
              <input
                type="text"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                placeholder="Full name"
                autoComplete="name"
                required
                className="w-full py-3 px-4 rounded-[10px] text-sm"
                style={{ border: '1px solid var(--border-light)', fontFamily: 'var(--font-body)', fontSize: '15px' }}
              />
            )}
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="Email address"
              autoComplete="email"
              required
              className="w-full py-3 px-4 rounded-[10px] text-sm"
              style={{ border: '1px solid var(--border-light)', fontFamily: 'var(--font-body)', fontSize: '15px' }}
            />
            <PasswordInput
              value={password}
              onChange={setPassword}
              placeholder="Password"
              autoComplete={activeTab === "signup" ? "new-password" : "current-password"}
              shown={showPassword}
              onToggle={() => setShowPassword((v) => !v)}
            />
            {activeTab === "signup" && (
              <>
                <PasswordInput
                  value={confirmPassword}
                  onChange={setConfirmPassword}
                  placeholder="Confirm password"
                  autoComplete="new-password"
                  shown={showPassword}
                  onToggle={() => setShowPassword((v) => !v)}
                  invalid={showMismatch}
                />
                {showMismatch && (
                  <p role="alert" style={{ fontFamily: 'var(--font-body)', fontSize: '13px', color: '#DC2626' }}>
                    Passwords don't match.
                  </p>
                )}
              </>
            )}
            {activeTab === "signin" && (
              <div className="text-right">
                <button
                  type="button"
                  onClick={handleForgotPassword}
                  style={{ fontFamily: 'var(--font-body)', fontSize: '13px', color: '#2563EB', background: 'none', border: 'none', cursor: 'pointer' }}
                >
                  {resetSent ? "Reset email sent. Check your inbox." : "Forgot password?"}
                </button>
              </div>
            )}
            <button
              type="submit"
              disabled={emailSubmitBlocked}
              className="w-full py-3.5 rounded-[10px] text-sm font-medium transition-all"
              style={{
                background: '#2563EB',
                color: 'white',
                fontFamily: 'var(--font-body)',
                fontSize: '15px',
                fontWeight: 600,
                border: 'none',
                cursor: emailSubmitBlocked ? 'not-allowed' : 'pointer',
                opacity: emailSubmitBlocked ? 0.6 : 1,
              }}
            >
              {submitting ? "Working..." : activeTab === "signup" ? "Create account" : "Sign in"}
            </button>
          </form>

          {/* Trust signals */}
          <div className="mt-6 space-y-2">
            <p
              style={{
                fontFamily: 'var(--font-body)',
                fontSize: '13px',
                color: 'var(--text-tertiary)',
                lineHeight: 1.6,
              }}
            >
              Use any email. Connecting Gmail later puts drafts directly in your inbox.
            </p>
            <div
              style={{
                display: 'flex',
                flexDirection: 'column',
                gap: '6px',
                marginTop: '12px',
              }}
            >
              {[
                "Works with any email address",
                "We never send emails without your permission",
                "You review and send everything yourself",
              ].map((item, i) => (
                <div
                  key={i}
                  className="flex items-center gap-2"
                  style={{
                    fontFamily: 'var(--font-body)',
                    fontSize: '12.5px',
                    color: 'var(--text-tertiary)',
                  }}
                >
                  <div
                    style={{
                      width: 14,
                      height: 14,
                      borderRadius: '50%',
                      background: 'rgba(59, 130, 246, 0.08)',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      flexShrink: 0,
                    }}
                  >
                    <svg width="8" height="8" viewBox="0 0 12 12" fill="none">
                      <path d="M2 6L5 9L10 3" stroke="#3B82F6" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                    </svg>
                  </div>
                  {item}
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Footer text */}
        <p
          className="text-center mt-6"
          style={{
            fontFamily: 'var(--font-body)',
            fontSize: '12px',
            color: 'var(--text-tertiary)',
            opacity: 0.7,
          }}
        >
          By continuing, you agree to our Terms of Service and Privacy Policy.
        </p>
      </div>
    </div>
  );
};

export default SignIn;