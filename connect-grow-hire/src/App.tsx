// src/App.tsx
import React, { Suspense, useEffect } from "react";
import { Toaster } from "@/components/ui/toaster";
import { Toaster as Sonner } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { jobFeedQueryKey, fetchJobFeed, JOB_FEED_STALE_MS } from "@/lib/jobFeedQuery";
import { BrowserRouter, Routes, Route, Navigate, useLocation } from "react-router-dom";
import posthog from "./lib/posthog";
import { FirebaseAuthProvider, useFirebaseAuth } from "./contexts/FirebaseAuthContext";
import PendingShareModal from "@/components/shares/PendingShareModal";
import { ScoutProvider } from "./contexts/ScoutContext";
import { ScoutChatProvider } from "./contexts/ScoutChatContext";
import { TourProvider } from "./contexts/TourContext";
import { HelmetProvider } from "react-helmet-async";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { LoadingContainer } from "./components/ui/LoadingBar";
import { IS_DEV_PREVIEW } from "./lib/devPreview";

// Keep critical auth pages non-lazy for faster initial load
import SignIn from "./pages/SignIn";
import AuthCallback from "./pages/AuthCallback";
import UscBeta from "@/pages/UscBeta";

// Landing page is lazy: authed users get redirected to /find and never
// render it, so its heavy image/component tree must stay out of the critical
// entry chunk.
const Index = React.lazy(() => import("./pages/Index"));
// The gradient background is landing-only and decorative (pure CSS/React, no
// WebGL). Lazy so it never ships on the critical path. Mounts with a null
// fallback only when an unauthenticated visitor actually sees the landing page.
const DynamicGradientBackground = React.lazy(() =>
  import("./components/background/DynamicGradientBackground").then((m) => ({
    default: m.DynamicGradientBackground,
  })),
);

// Lazy load heavy pages for code splitting
const AboutUs = React.lazy(() => import("./pages/AboutUs"));
const ConnectorSetup = React.lazy(() => import("./pages/ConnectorSetup"));
const ForStudentsPage = React.lazy(() => import("./pages/ForStudentsPage"));
const HowItWorks = React.lazy(() => import("./pages/HowItWorks"));
const PromoPage = React.lazy(() => import("./pages/PromoPage"));
const ContactUs = React.lazy(() => import("./pages/ContactUs"));
const PrivacyPolicy = React.lazy(() => import("./pages/PrivacyPolicy"));
const ExtensionPrivacyPolicy = React.lazy(() => import("./pages/ExtensionPrivacyPolicy"));
const TermsOfService = React.lazy(() => import("./pages/TermsOfService"));
const AccountSettings = React.lazy(() => import("./pages/AccountSettings"));
const IntegrationsPage = React.lazy(() => import("./pages/IntegrationsPage"));
const OAuthComplete = React.lazy(() => import("./pages/OAuthComplete"));
const McpServerPage = React.lazy(() => import("./pages/McpServerPage"));
const McpChatGptSetupPage = React.lazy(() => import("./pages/McpChatGptSetupPage"));
const UploadListPage = React.lazy(() => import("./pages/UploadListPage"));
const ApplicationsPage = React.lazy(() => import("./pages/ApplicationsPage"));
const ReferPage = React.lazy(() => import("./pages/ReferPage"));
const Pricing = React.lazy(() => import("./pages/Pricing"));
const DocumentationPage = React.lazy(() => import("./pages/DocumentationPage"));
const JobBoardPage = React.lazy(() => import("./pages/JobBoardPage"));
// Job board redesign is now the production component for /job-board.
// /dev/job-board-redesign is kept as a same-component fallback URL for one
// day of confirmation. The old JobBoardPage import above stays unrouted
// pending deletion after that confirmation window.
const JobBoardRedesign = React.lazy(() => import("./pages/JobBoardPage.redesign"));
// /job-board now hosts BOTH views (List = personalized board, Gallery =
// browse-everything cards) behind a toggle. The container owns the choice.
const JobBoardContainer = React.lazy(() => import("./pages/JobBoardContainer"));
// Contacts replaced My Network, Inbox and Meeting Prep. Their old URLs
// redirect here via ToContacts, keeping ?contact= and route state.
const ContactsPage = React.lazy(() => import("./pages/ContactsPage"));
const NotFound = React.lazy(() => import("./pages/NotFound"));
const PaymentSuccess = React.lazy(() => import("./pages/PaymentSuccess"));
// Feature Pages - These are the largest, most important to lazy load
const FindPage = React.lazy(() => import("./pages/FindPage"));
const OrgDirectoryPage = React.lazy(() => import("./pages/OrgDirectoryPage"));
const EmailTemplatesPage = React.lazy(() => import("./pages/EmailTemplatesPage"));

// Old My Network / Inbox / Meeting Prep URLs land on Contacts, carrying the
// query string (?contact=<id>) and route state ({ focusEmail }) along.
function ToContacts() {
  const loc = useLocation();
  return <Navigate to={`/contacts${loc.search}`} state={loc.state} replace />;
}
const RecruitingTimelinePage = React.lazy(() => import("./pages/RecruitingTimelinePage"));
// New Lovable Onboarding Flow
const OnboardingFlow = React.lazy(() => import("./pages/OnboardingFlow").then(m => ({ default: m.OnboardingFlow })));
// Dev-only preview routes (no auth) for design iteration on the new Profile page
// and onboarding flow. See docs/PROFILE_ONBOARDING_SPEC.md.
const ProfilePreview = React.lazy(() => import("./pages/ProfilePreview"));
const DataStats = React.lazy(() => import("./pages/DataStats"));
// SEO Landing Pages
const CompareHandshake = React.lazy(() => import("./pages/CompareHandshake"));
const CompareLinkedIn = React.lazy(() => import("./pages/CompareLinkedIn"));
const CompareApollo = React.lazy(() => import("./pages/CompareApollo"));
const CompareChatGPT = React.lazy(() => import("./pages/CompareChatGPT"));
const ColdEmailConsulting = React.lazy(() => import("./pages/ColdEmailConsulting"));
const ColdEmailBanking = React.lazy(() => import("./pages/ColdEmailBanking"));
const ColdEmailTech = React.lazy(() => import("./pages/ColdEmailTech"));
const AlumniOutreach = React.lazy(() => import("./pages/AlumniOutreach"));
const CoffeeChatNetworking = React.lazy(() => import("./pages/CoffeeChatNetworking"));
const Glossary = React.lazy(() => import("./pages/Glossary"));
const Blog = React.lazy(() => import("./pages/Blog"));
const BlogPost = React.lazy(() => import("./pages/BlogPost"));
const NetworkingGuidePage = React.lazy(() => import("./pages/NetworkingGuidePage"));
const AlumniGuidePage = React.lazy(() => import("./pages/AlumniGuidePage"));
const ColdEmailGuidePage = React.lazy(() => import("./pages/ColdEmailGuidePage"));
const CoffeeChatGuidePage = React.lazy(() => import("./pages/CoffeeChatGuidePage"));
const RoleNetworkingGuidePage = React.lazy(() => import("./pages/RoleNetworkingGuidePage"));
const CompanyComparisonPage = React.lazy(() => import("./pages/CompanyComparisonPage"));

// Public free tools + widget sandboxes
const InterviewPrepFree = React.lazy(() => import("./pages/InterviewPrepFree"));
const CoverLetterFree = React.lazy(() => import("./pages/CoverLetterFree"));
const ResumeReviewFree = React.lazy(() => import("./pages/ResumeReviewFree"));
const WidgetSandbox = React.lazy(() => import("./pages/WidgetSandbox"));
const CoverLetterWidgetSandbox = React.lazy(() => import("./pages/CoverLetterWidgetSandbox"));
const InterviewPrepSandbox = React.lazy(() => import("./pages/InterviewPrepSandbox"));
const FindHiringManagerFree = React.lazy(() => import("./pages/FindHiringManagerFree"));
const FindHiringManagerWidgetSandbox = React.lazy(() => import("./pages/FindHiringManagerWidgetSandbox"));
const FindHiringManagerPreview = React.lazy(() => import("./pages/seo-preview/FindHiringManagerPreview"));
const FindCompaniesFree = React.lazy(() => import("./pages/FindCompaniesFree"));
const FindCompaniesWidgetSandbox = React.lazy(() => import("./pages/FindCompaniesWidgetSandbox"));
const FindCompaniesPreview = React.lazy(() => import("./pages/seo-preview/FindCompaniesPreview"));
const FindJobsFree = React.lazy(() => import("./pages/FindJobsFree"));
const FindJobsWidgetSandbox = React.lazy(() => import("./pages/FindJobsWidgetSandbox"));
const FindJobsPreview = React.lazy(() => import("./pages/seo-preview/FindJobsPreview"));
const FindPeopleFree = React.lazy(() => import("./pages/FindPeopleFree"));
const FindPeopleWidgetSandbox = React.lazy(() => import("./pages/FindPeopleWidgetSandbox"));
const FindPeopleUscGooglePreview = React.lazy(() => import("./pages/seo-preview/FindPeopleUscGooglePreview"));
const FindPeopleTemplate = React.lazy(() => import("./pages/seo-preview/templates/FindPeopleTemplate"));
const MeetingPrepFree = React.lazy(() => import("./pages/MeetingPrepFree"));
const MeetingPrepWidgetSandbox = React.lazy(() => import("./pages/MeetingPrepWidgetSandbox"));
const MeetingPrepFreePreview = React.lazy(() => import("./pages/seo-preview/MeetingPrepFreePreview"));

// SEO preview pages (marketing landing variants embedding the widgets)
const MeetingPrepPreview = React.lazy(() => import("./pages/seo-preview/MeetingPrepPreview"));
const ColdEmailPreview = React.lazy(() => import("./pages/seo-preview/ColdEmailPreview"));
const FindAlumniPreview = React.lazy(() => import("./pages/seo-preview/FindAlumniPreview"));
const ResumeCheckerPreview = React.lazy(() => import("./pages/seo-preview/ResumeCheckerPreview"));
const RecruitingTimelinePreview = React.lazy(() => import("./pages/seo-preview/RecruitingTimelinePreview"));
const NetworkingEmailGeneratorPreview = React.lazy(() => import("./pages/seo-preview/NetworkingEmailGeneratorPreview"));
const ResumeReviewGoldmanIBPreview = React.lazy(() => import("./pages/seo-preview/ResumeReviewGoldmanIBPreview"));
const WhatIsAnATSPreview = React.lazy(() => import("./pages/seo-preview/WhatIsAnATSPreview"));
const CoverLetterMckinseyBAPreview = React.lazy(() => import("./pages/seo-preview/CoverLetterMckinseyBAPreview"));
const InterviewPrepMckinseyCasePreview = React.lazy(() => import("./pages/seo-preview/InterviewPrepMckinseyCasePreview"));
const ResumeReviewTemplate = React.lazy(() => import("./pages/seo-preview/templates/ResumeReviewTemplate"));
const CoverLetterTemplate = React.lazy(() => import("./pages/seo-preview/templates/CoverLetterTemplate"));
const InterviewPrepTemplate = React.lazy(() => import("./pages/seo-preview/templates/InterviewPrepTemplate"));
const ATSGuideTemplate = React.lazy(() => import("./pages/seo-preview/templates/ATSGuideTemplate"));
const FreeToolsHub = React.lazy(() => import("./pages/FreeToolsHub"));
const AppFinishSignIn = React.lazy(() => import("./pages/AppFinishSignIn"));
const CheckoutHandoff = React.lazy(() => import("./pages/CheckoutHandoff"));

// Optimized QueryClient with caching
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5 * 60 * 1000, // 5 minutes
      gcTime: 10 * 60 * 1000, // 10 minutes (formerly cacheTime)
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

// Unified full-screen loader — used for BOTH auth resolution (route guards)
// and lazy-route Suspense fallbacks. Sharing one component keeps the two
// phases visually continuous instead of swapping between a spinner and a
// skeleton, which read as a "double load".
const FullScreenLoader = () => (
  <div className="flex items-center justify-center min-h-screen bg-gradient-to-br from-slate-50 to-blue-50">
    <LoadingContainer label="Loading Offerloop..." sublabel="Please wait" />
  </div>
);

// Suspense fallback (name kept for existing call sites).
const PageLoader = FullScreenLoader;

// Phase 3 stationery aesthetic — always-on (shipped).
// Formerly gated by VITE_FLAG_NEW_AESTHETIC env var during dev preview.
const NEW_AESTHETIC = true;

// Environment-based logging helper
const isDev = import.meta.env.DEV;
const devLog = (...args: any[]) => {
  if (isDev) console.log(...args);
};

/* ---------------- Route Guards ---------------- */
const ProtectedRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { user, isLoading } = useFirebaseAuth();
  const loc = useLocation();

  // Check if coming from sign-out - don't redirect if so
  const params = new URLSearchParams(loc.search);
  const isSignedOut = params.get('signedOut') === 'true';

  devLog("🔒 [PROTECTED ROUTE] Route check:", {
    path: loc.pathname,
    search: loc.search,
    isLoading,
    hasUser: !!user,
    userEmail: user?.email || "none",
    needsOnboarding: user?.needsOnboarding || false,
    isSignedOut
  });

  // Dev preview bypass — skip all auth checks in dev mode with ?devpreview=true
  if (IS_DEV_PREVIEW) {
    devLog("🔒 [PROTECTED ROUTE] Dev preview bypass active, skipping auth");
    return <>{children}</>;
  }

  if (isLoading) {
    devLog("🔒 [PROTECTED ROUTE] Still loading auth state, showing loading bar");
    return <FullScreenLoader />;
  }

  // If signed out flag is present, redirect to landing page instead of signin
  if (isSignedOut) {
    devLog("🔒 [PROTECTED ROUTE] signedOut=true detected, redirecting to landing page");
    return <Navigate to="/?signedOut=true" replace />;
  }

  if (!user) {
    const returnTo = encodeURIComponent(loc.pathname + loc.search + loc.hash);
    devLog("🔒 [PROTECTED ROUTE] No user, redirecting to signin with returnTo:", returnTo);
    return <Navigate to={`/signin?mode=signin&returnTo=${returnTo}`} replace />;
  }

  if (user.needsOnboarding) {
    if (loc.pathname === "/onboarding") {
      devLog("🔒 [PROTECTED ROUTE] User needs onboarding and is on onboarding page, allowing access");
      return <>{children}</>;
    }
    const returnTo = encodeURIComponent(loc.pathname + loc.search + loc.hash);
    devLog("🔒 [PROTECTED ROUTE] User needs onboarding, redirecting to /onboarding");
    return <Navigate to={`/onboarding?returnTo=${returnTo}`} replace />;
  }

  devLog("🔒 [PROTECTED ROUTE] User authenticated, allowing access");
  return <>{children}</>;
};

const PublicRoute: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { user, isLoading } = useFirebaseAuth();
  const location = useLocation();
  
  // Check if coming from sign-out - skip redirect if so
  const params = new URLSearchParams(location.search);
  const isSignedOut = params.get('signedOut') === 'true';
  
  devLog("🛣️ [PUBLIC ROUTE] Route check:", {
    path: location.pathname,
    search: location.search,
    isSignedOut,
    isLoading,
    hasUser: !!user,
    userEmail: user?.email || "none",
    needsOnboarding: user?.needsOnboarding || false
  });
  
  // Use the same full-screen loader as the protected routes and Suspense
  // fallbacks so auth resolution never visually swaps loaders.
  if (isLoading) {
    devLog("🛣️ [PUBLIC ROUTE] Still loading auth state, showing loader");
    return <FullScreenLoader />;
  }

  // If explicitly signed out, always show the landing page regardless of user state
  // This prevents the race condition where user state hasn't fully cleared yet
  if (isSignedOut) {
    devLog("🛣️ [PUBLIC ROUTE] signedOut=true detected, showing landing page (ignoring user state)");
    return <>{children}</>;
  }

  // Only redirect authenticated users if they're not coming from sign-out.
  // Everyday users land on Search; new/long-absent users get Getting Started.
  // A safe internal ?next=/?redirect= param wins (e.g. an already-signed-in
  // visitor following a "start trial" link to /signin?next=/pricing keeps
  // their purchase intent). Onboarding still takes precedence.
  if (user) {
    const rawNext = params.get('next') || params.get('redirect') || '';
    const safeNext = rawNext.startsWith('/') && !rawNext.startsWith('//') ? rawNext : null;
    const redirectPath = user.needsOnboarding ? "/onboarding" : (safeNext ?? "/find");
    devLog("🛣️ [PUBLIC ROUTE] User authenticated, redirecting to:", redirectPath);
    return <Navigate to={redirectPath} replace />;
  }
  
  devLog("🛣️ [PUBLIC ROUTE] No user, showing public content");
  return <>{children}</>;
};

/* ---------------- Routes ---------------- */
const AppRoutes: React.FC = () => {
  return (
    <Routes>
      {/* Public Landing */}
      <Route path="/" element={<PublicRoute><Suspense fallback={<PageLoader />}><Index /></Suspense></PublicRoute>} />
      <Route path="/usc-beta" element={<UscBeta />} />

      {/* Auth */}
      <Route path="/signin" element={<PublicRoute><SignIn /></PublicRoute>} />
      <Route path="/signup" element={<Navigate to="/signin?mode=signup" replace />} />
      <Route path="/auth/callback" element={<PublicRoute><AuthCallback /></PublicRoute>} />
      {/* Guardless: OAuth popups land here and close themselves. Wrapping it
          in a route guard would bounce the popup into the app and strand the
          opener waiting on popup.closed. */}
      <Route path="/oauth/complete" element={<Suspense fallback={<PageLoader />}><OAuthComplete /></Suspense>} />

      {/* Mobile app magic-link sign-in handoff (public, no auth guard) */}
      <Route path="/app/finish-signin" element={<Suspense fallback={<PageLoader />}><AppFinishSignIn /></Suspense>} />

      {/* Mobile app -> web checkout handoff: burns the one-time code, signs
          the browser in as the app user, forwards to /pricing (public, no
          auth guard; see backend/app/routes/mobile_handoff.py) */}
      <Route path="/checkout" element={<Suspense fallback={<PageLoader />}><CheckoutHandoff /></Suspense>} />

      {/* Onboarding */}
      <Route
        path="/onboarding"
        element={
          <ProtectedRoute>
            <Suspense fallback={<PageLoader />}>
              <OnboardingFlow onComplete={() => { /* handled in component */ }} />
            </Suspense>
          </ProtectedRoute>
        }
      />
      <Route path="/onboarding/*" element={<Navigate to="/onboarding" replace />} />

      {/* Profile (Phase 2) — the dedicated free-flow profile page. Same
          component as the dev preview, just protected and routed at /profile.
          The home page "what we know about you" widget links here. */}
      <Route
        path="/profile"
        element={
          <ProtectedRoute>
            <Suspense fallback={<PageLoader />}>
              <ProfilePreview />
            </Suspense>
          </ProtectedRoute>
        }
      />

      {/* Dev preview routes — no auth, no protection. For visual iteration only.
          See docs/PROFILE_ONBOARDING_SPEC.md. */}
      <Route
        path="/dev/profile-preview"
        element={
          <Suspense fallback={<PageLoader />}>
            <ProfilePreview />
          </Suspense>
        }
      />
      <Route
        path="/dev/onboarding-preview"
        element={
          <Suspense fallback={<PageLoader />}>
            <OnboardingFlow onComplete={() => { /* preview only — no save */ }} />
          </Suspense>
        }
      />
      <Route
        path="/dev/job-board-redesign"
        element={
          <ProtectedRoute>
            <Suspense fallback={<PageLoader />}>
              <JobBoardRedesign />
            </Suspense>
          </ProtectedRoute>
        }
      />

      {/* Protected App Pages - Wrapped in Suspense for lazy loading */}
      <Route path="/dashboard" element={<Navigate to="/find" replace />} />
      <Route path="/find" element={<ProtectedRoute><Suspense fallback={<PageLoader />}><FindPage /></Suspense></ProtectedRoute>} />
      <Route path="/directory/:orgId" element={<ProtectedRoute><Suspense fallback={<PageLoader />}><OrgDirectoryPage /></Suspense></ProtectedRoute>} />
      <Route path="/contacts" element={<ProtectedRoute><Suspense fallback={<PageLoader />}><ContactsPage /></Suspense></ProtectedRoute>} />
      <Route path="/my-network" element={<ToContacts />} />
      <Route path="/my-network/:tab" element={<ToContacts />} />
      <Route path="/contact-search" element={<Navigate to="/find" replace />} />
      <Route path="/outbox" element={<ToContacts />} />
      <Route path="/tracker" element={<ToContacts />} />
      {/* Legacy /home redirect to contact search */}
      <Route path="/home" element={<Navigate to="/find" replace />} />
      <Route path="/contact-directory" element={<ToContacts />} />
      <Route path="/coffee-chat-library" element={<ToContacts />} />
      <Route path="/account-settings" element={<ProtectedRoute><Suspense fallback={<PageLoader />}><AccountSettings /></Suspense></ProtectedRoute>} />
      <Route path="/integrations" element={<ProtectedRoute><Suspense fallback={<PageLoader />}><IntegrationsPage /></Suspense></ProtectedRoute>} />
      <Route path="/mcp-server" element={<ProtectedRoute><Suspense fallback={<PageLoader />}><McpServerPage /></Suspense></ProtectedRoute>} />
      <Route path="/mcp-server/chatgpt" element={<ProtectedRoute><Suspense fallback={<PageLoader />}><McpChatGptSetupPage /></Suspense></ProtectedRoute>} />
      <Route path="/upload-list" element={<ProtectedRoute><Suspense fallback={<PageLoader />}><UploadListPage /></Suspense></ProtectedRoute>} />
      <Route path="/applications" element={<ProtectedRoute><Suspense fallback={<PageLoader />}><ApplicationsPage /></Suspense></ProtectedRoute>} />
      {/* Resume Workshop and Cover Letter were removed; old links land on Search. */}
      <Route path="/resume" element={<Navigate to="/find" replace />} />
      <Route path="/cover-letter" element={<Navigate to="/find" replace />} />
      <Route path="/refer" element={<ProtectedRoute><Suspense fallback={<PageLoader />}><ReferPage /></Suspense></ProtectedRoute>} />
      <Route path="/pricing" element={<Suspense fallback={<PageLoader />}><Pricing /></Suspense>} />
      <Route path="/documentation" element={<ProtectedRoute><Suspense fallback={<PageLoader />}><DocumentationPage /></Suspense></ProtectedRoute>} />
      <Route path="/payment-success" element={<ProtectedRoute><Suspense fallback={<PageLoader />}><PaymentSuccess /></Suspense></ProtectedRoute>} />
      
      {/* Feature Pages - Largest pages, most important to lazy load */}
      <Route path="/coffee-chat-prep" element={<ToContacts />} />
      <Route path="/contact-search/templates" element={<Navigate to="/find/templates" replace />} />
      <Route path="/find/templates" element={<ProtectedRoute><Suspense fallback={<PageLoader />}><EmailTemplatesPage /></Suspense></ProtectedRoute>} />
      <Route path="/firm-search" element={<Navigate to="/find" replace />} />
      {/* Old standalone gallery URL now deep-links into the Job Board's gallery view. */}
      <Route path="/jobs" element={<Navigate to="/job-board?view=gallery" replace />} />
      <Route path="/job-board" element={<ProtectedRoute><Suspense fallback={<PageLoader />}><JobBoardContainer /></Suspense></ProtectedRoute>} />
      <Route path="/recruiter-spreadsheet" element={<Navigate to="/find" replace />} />
      <Route path="/hiring-manager-tracker" element={<Navigate to="/find" replace />} />
      <Route path="/company-tracker" element={<Navigate to="/find" replace />} />
      {/* Scout is no longer on the website; old /scout links land on Search. */}
      <Route path="/scout" element={<Navigate to="/find" replace />} />
      <Route path="/recruiting-timeline" element={<ProtectedRoute><Suspense fallback={<PageLoader />}><RecruitingTimelinePage /></Suspense></ProtectedRoute>} />
      {/* Data & Stats */}
      <Route path="/data" element={<Suspense fallback={<PageLoader />}><DataStats /></Suspense>} />

      {/* Competitor Comparison Pages */}
      <Route path="/compare/handshake" element={<Suspense fallback={<PageLoader />}><CompareHandshake /></Suspense>} />
      <Route path="/compare/linkedin" element={<Suspense fallback={<PageLoader />}><CompareLinkedIn /></Suspense>} />
      <Route path="/compare/apollo" element={<Suspense fallback={<PageLoader />}><CompareApollo /></Suspense>} />
      <Route path="/compare/chatgpt" element={<Suspense fallback={<PageLoader />}><CompareChatGPT /></Suspense>} />

      {/* SEO Landing Pages */}
      <Route path="/cold-email-consulting" element={<Suspense fallback={<PageLoader />}><ColdEmailConsulting /></Suspense>} />
      <Route path="/cold-email-investment-banking" element={<Suspense fallback={<PageLoader />}><ColdEmailBanking /></Suspense>} />
      <Route path="/cold-email-tech-internships" element={<Suspense fallback={<PageLoader />}><ColdEmailTech /></Suspense>} />
      <Route path="/alumni-outreach" element={<Suspense fallback={<PageLoader />}><AlumniOutreach /></Suspense>} />
      <Route path="/coffee-chat-networking" element={<Suspense fallback={<PageLoader />}><CoffeeChatNetworking /></Suspense>} />
      <Route path="/glossary" element={<Suspense fallback={<PageLoader />}><Glossary /></Suspense>} />
      <Route path="/blog" element={<Suspense fallback={<PageLoader />}><Blog /></Suspense>} />
      <Route path="/blog/:slug" element={<Suspense fallback={<PageLoader />}><BlogPost /></Suspense>} />
      <Route path="/networking/:slug" element={<Suspense fallback={<PageLoader />}><NetworkingGuidePage /></Suspense>} />
      <Route path="/alumni/:slug" element={<Suspense fallback={<PageLoader />}><AlumniGuidePage /></Suspense>} />
      <Route path="/cold-email/:slug" element={<Suspense fallback={<PageLoader />}><ColdEmailGuidePage /></Suspense>} />
      <Route path="/coffee-chat/:slug" element={<Suspense fallback={<PageLoader />}><CoffeeChatGuidePage /></Suspense>} />
      <Route path="/networking-for/:slug" element={<Suspense fallback={<PageLoader />}><RoleNetworkingGuidePage /></Suspense>} />
      <Route path="/compare/:comparison" element={<Suspense fallback={<PageLoader />}><CompanyComparisonPage /></Suspense>} />

      {/* Public informational pages */}
      <Route path="/about" element={<Suspense fallback={<PageLoader />}><AboutUs /></Suspense>} />
      <Route path="/connector" element={<Suspense fallback={<PageLoader />}><ConnectorSetup /></Suspense>} />
      <Route path="/for-students" element={<Suspense fallback={<PageLoader />}><ForStudentsPage /></Suspense>} />
      <Route path="/how-it-works" element={<Suspense fallback={<PageLoader />}><HowItWorks /></Suspense>} />
      <Route path="/promo" element={<Suspense fallback={<PageLoader />}><PromoPage /></Suspense>} />
      <Route path="/contact" element={<Navigate to="/contact-us" replace />} />
      <Route path="/contact-us" element={<Suspense fallback={<PageLoader />}><ContactUs /></Suspense>} />

      {/* Legal pages + canonical redirects */}
      <Route path="/privacy" element={<Suspense fallback={<PageLoader />}><PrivacyPolicy /></Suspense>} />
      <Route path="/extension-privacy" element={<Suspense fallback={<PageLoader />}><ExtensionPrivacyPolicy /></Suspense>} />
      <Route path="/privacy-policy" element={<Navigate to="/privacy" replace />} />
      <Route path="/terms-of-service" element={<Suspense fallback={<PageLoader />}><TermsOfService /></Suspense>} />
      <Route path="/terms" element={<Navigate to="/terms-of-service" replace />} />

      {/* 404 */}
      {/* Public free tools */}
      <Route path="/tools/interview-prep" element={<Suspense fallback={<PageLoader />}><InterviewPrepFree /></Suspense>} />
      <Route path="/tools/cover-letter" element={<Suspense fallback={<PageLoader />}><CoverLetterFree /></Suspense>} />
      <Route path="/tools/resume-review" element={<Suspense fallback={<PageLoader />}><ResumeReviewFree /></Suspense>} />
      <Route path="/tools/find-hiring-manager" element={<Suspense fallback={<PageLoader />}><FindHiringManagerFree /></Suspense>} />
      <Route path="/tools/find-companies" element={<Suspense fallback={<PageLoader />}><FindCompaniesFree /></Suspense>} />
      <Route path="/tools/find-jobs" element={<Suspense fallback={<PageLoader />}><FindJobsFree /></Suspense>} />
      <Route path="/tools/find-people" element={<Suspense fallback={<PageLoader />}><FindPeopleFree /></Suspense>} />
      <Route path="/tools/meeting-prep-free" element={<Suspense fallback={<PageLoader />}><MeetingPrepFree /></Suspense>} />

      {/* Widget sandboxes (internal preview, not linked from nav) */}
      <Route path="/sandbox/resume-widget" element={<Suspense fallback={<PageLoader />}><WidgetSandbox /></Suspense>} />
      <Route path="/sandbox/cover-letter-widget" element={<Suspense fallback={<PageLoader />}><CoverLetterWidgetSandbox /></Suspense>} />
      <Route path="/sandbox/interview-prep-widget" element={<Suspense fallback={<PageLoader />}><InterviewPrepSandbox /></Suspense>} />
      <Route path="/sandbox/find-hiring-manager-widget" element={<Suspense fallback={<PageLoader />}><FindHiringManagerWidgetSandbox /></Suspense>} />
      <Route path="/sandbox/find-companies-widget" element={<Suspense fallback={<PageLoader />}><FindCompaniesWidgetSandbox /></Suspense>} />
      <Route path="/sandbox/find-jobs-widget" element={<Suspense fallback={<PageLoader />}><FindJobsWidgetSandbox /></Suspense>} />
      <Route path="/sandbox/find-people-widget" element={<Suspense fallback={<PageLoader />}><FindPeopleWidgetSandbox /></Suspense>} />
      <Route path="/sandbox/meeting-prep-widget" element={<Suspense fallback={<PageLoader />}><MeetingPrepWidgetSandbox /></Suspense>} />

      {/* SEO preview pages */}
      <Route path="/seo-preview/meeting-mckinsey" element={<Suspense fallback={<PageLoader />}><MeetingPrepPreview /></Suspense>} />
      <Route path="/seo-preview/meeting-prep-free" element={<Suspense fallback={<PageLoader />}><MeetingPrepFreePreview /></Suspense>} />
      <Route path="/seo-preview/cold-email-goldman" element={<Suspense fallback={<PageLoader />}><ColdEmailPreview /></Suspense>} />
      <Route path="/seo-preview/find-usc-goldman" element={<Suspense fallback={<PageLoader />}><FindAlumniPreview /></Suspense>} />
      <Route path="/seo-preview/resume-checker" element={<Suspense fallback={<PageLoader />}><ResumeCheckerPreview /></Suspense>} />
      <Route path="/seo-preview/ib-recruiting-timeline" element={<Suspense fallback={<PageLoader />}><RecruitingTimelinePreview /></Suspense>} />
      <Route path="/seo-preview/networking-email-generator" element={<Suspense fallback={<PageLoader />}><NetworkingEmailGeneratorPreview /></Suspense>} />
      <Route path="/seo-preview/resume-review-goldman-ib" element={<Suspense fallback={<PageLoader />}><ResumeReviewGoldmanIBPreview /></Suspense>} />
      <Route path="/seo-preview/what-is-an-ats" element={<Suspense fallback={<PageLoader />}><WhatIsAnATSPreview /></Suspense>} />
      <Route path="/seo-preview/cover-letter-mckinsey-ba" element={<Suspense fallback={<PageLoader />}><CoverLetterMckinseyBAPreview /></Suspense>} />
      <Route path="/seo-preview/interview-prep-mckinsey-case" element={<Suspense fallback={<PageLoader />}><InterviewPrepMckinseyCasePreview /></Suspense>} />
      <Route path="/seo-preview/resume-review/:slug" element={<Suspense fallback={<PageLoader />}><ResumeReviewTemplate /></Suspense>} />
      <Route path="/seo-preview/cover-letter/:slug" element={<Suspense fallback={<PageLoader />}><CoverLetterTemplate /></Suspense>} />
      <Route path="/seo-preview/interview-prep/:slug" element={<Suspense fallback={<PageLoader />}><InterviewPrepTemplate /></Suspense>} />
      <Route path="/seo-preview/ats/:slug" element={<Suspense fallback={<PageLoader />}><ATSGuideTemplate /></Suspense>} />
      {/* Clean, indexable production prefixes for live (published) SEO rows. The /seo-preview/* twins above stay noindex and canonicalize here. */}
      <Route path="/free-tools" element={<Suspense fallback={<PageLoader />}><FreeToolsHub /></Suspense>} />
      <Route path="/resume-review/:slug" element={<Suspense fallback={<PageLoader />}><ResumeReviewTemplate /></Suspense>} />
      <Route path="/cover-letter/:slug" element={<Suspense fallback={<PageLoader />}><CoverLetterTemplate /></Suspense>} />
      <Route path="/interview-prep/:slug" element={<Suspense fallback={<PageLoader />}><InterviewPrepTemplate /></Suspense>} />
      <Route path="/ats/:slug" element={<Suspense fallback={<PageLoader />}><ATSGuideTemplate /></Suspense>} />
      <Route path="/seo-preview/find-hiring-manager" element={<Suspense fallback={<PageLoader />}><FindHiringManagerPreview /></Suspense>} />
      <Route path="/seo-preview/find-companies" element={<Suspense fallback={<PageLoader />}><FindCompaniesPreview /></Suspense>} />
      <Route path="/seo-preview/find-jobs" element={<Suspense fallback={<PageLoader />}><FindJobsPreview /></Suspense>} />
      <Route path="/seo-preview/find-people-usc-google" element={<Suspense fallback={<PageLoader />}><FindPeopleUscGooglePreview /></Suspense>} />
      <Route path="/seo-preview/find-people/:slug" element={<Suspense fallback={<PageLoader />}><FindPeopleTemplate /></Suspense>} />
      <Route path="/people/:slug" element={<Suspense fallback={<PageLoader />}><FindPeopleTemplate /></Suspense>} />

      <Route path="*" element={<Suspense fallback={<PageLoader />}><NotFound /></Suspense>} />
    </Routes>
  );
};

/* ---------------- Conditional Background Wrapper ----------------
   Decides whether to mount the landing background tree BEFORE rendering it.
   Authed users are redirected off "/" by PublicRoute, and during auth
   resolution we show a full-screen loader, so in neither case should the
   decorative background mount. Gate it on a visitor who will actually
   see the landing: on "/", not loading, and either signed out or no user. */
const ConditionalBackground: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const location = useLocation();
  const { user, isLoading } = useFirebaseAuth();

  const params = new URLSearchParams(location.search);
  const isSignedOut = params.get('signedOut') === 'true';
  const isLandingPage = location.pathname === '/';
  const showLandingBackground =
    isLandingPage && !isLoading && (!user || isSignedOut);

  return (
    <div className="relative min-h-screen">
      {showLandingBackground && (
        <Suspense fallback={null}>
          <DynamicGradientBackground />
        </Suspense>
      )}
      <div className="relative z-10">
        {children}
      </div>
    </div>
  );
};

/* ---------------- Pageview Tracker ----------------
   capture_pageview is false in posthog.ts, so PostHog does not auto-fire.
   This fires exactly one $pageview per location: once on initial mount and
   once per route change. No double-fire. */
const PageviewTracker: React.FC = () => {
  const location = useLocation();
  useEffect(() => {
    posthog.capture('$pageview');
  }, [location.pathname, location.search]);
  return null;
};

/* ---------------- Agent Global Notifier ---------------- */
/* ---------------- Job Feed Prefetch ----------------
   The job feed is the slowest data fetch in the app (the server gathers +
   ranks jobs for the user). Fetching it here — the moment auth resolves to an
   onboarded user — means the request runs in the background while the user is
   on whatever page they landed on. By the time they click "Job Board", the
   feed is already sitting in the React Query cache and the page paints
   instantly instead of showing a 3s skeleton. Uses the SAME query key the
   page reads, so it warms the exact cache entry the page consumes. Failures
   are swallowed: this is a pure optimization, the page refetches on its own. */
const JobFeedPrefetch: React.FC = () => {
  const { user, isLoading } = useFirebaseAuth();
  const queryClient = useQueryClient();
  useEffect(() => {
    if (isLoading || !user || user.needsOnboarding) return;
    queryClient
      .prefetchQuery({
        queryKey: jobFeedQueryKey(user.uid),
        queryFn: fetchJobFeed,
        staleTime: JOB_FEED_STALE_MS,
      })
      .catch(() => {
        /* optimization only — the page will fetch normally if this misses */
      });
  }, [isLoading, user, queryClient]);
  return null;
};

/* ---------------- App Root ---------------- */
const App: React.FC = () => {
  useEffect(() => {
    document.documentElement.dataset.theme = NEW_AESTHETIC ? 'stationery' : 'legacy';
  }, []);

  return (
    <HelmetProvider>
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <FirebaseAuthProvider>
          <ErrorBoundary>
            <JobFeedPrefetch />
            <BrowserRouter
              future={{
                v7_startTransition: true,
                v7_relativeSplatPath: true,
              }}
            >
              <ConditionalBackground>
                <Toaster />
                <Sonner />
                <ScoutProvider>
                  <ScoutChatProvider>
                    <TourProvider>
                      <PageviewTracker />
                      <PendingShareModal />
                      <AppRoutes />
                    </TourProvider>
                  </ScoutChatProvider>
                </ScoutProvider>
              </ConditionalBackground>
            </BrowserRouter>
          </ErrorBoundary>
        </FirebaseAuthProvider>
      </TooltipProvider>
    </QueryClientProvider>
    </HelmetProvider>
  );
};

export default App;
