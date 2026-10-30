import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ArrowLeft, Check } from "lucide-react";
import { OnboardingSlides } from "./OnboardingSlides";
import { OnboardingSource, type SourceResult } from "./OnboardingSource";
import { OnboardingBuilder } from "./OnboardingBuilder";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { BACKEND_URL } from "@/services/api";
import { toast } from "sonner";
import { auth } from "@/lib/firebase";
import { type ResumePrefill } from "@/utils/onboardingPrefill";
import ScoutYeti from "@/assets/scout-wave-removebg-preview.png";
// Hand-drawn circled step numbers (sliced from the design reference, bg removed).
import StepNum1 from "@/assets/step-num-1.png";
import StepNum2 from "@/assets/step-num-2.png";
import StepNum3 from "@/assets/step-num-3.png";

const STEP_NUM_IMAGES = [StepNum1, StepNum2, StepNum3];
// All-white monochrome variant of offerloop_logo2.png (padding trimmed) — the
// standard dark-surface lockup, legible directly on the navy rail.
import OfferloopLogo from "@/assets/offerloop_logo2_allwhite.png";
import { OB, obPrimaryButton } from "./onboardingTheme";
import { OrgMembershipEditor } from "@/components/orgs/OrgMembershipEditor";
import { useOrgs } from "@/hooks/useOrgs";
import { trackFeatureActionCompleted } from "@/lib/analytics";
import { newWalkthrough } from "@/services/walkthrough";

// Resume-first onboarding: intro slides, then a single resume/LinkedIn page.
// "builder" (the free resume generator) shares the resume phase on the rail.
// "orgs" (OFF-42, OFF-50) is the last step before the site, and only appears
// when there are org alumni directories to join. Gmail connect isn't part of
// onboarding (OFF-50); it's offered later, inside the site.
type Step = "slides" | "source" | "builder" | "orgs";
const STEP_INDEX: Record<Step, number> = { slides: 0, source: 1, builder: 1, orgs: 2 };

const BASE_RAIL_STEPS: { label: string; optional?: boolean }[] = [
  { label: "What Offerloop does" },
  { label: "Your resume" },
];
const ORGS_RAIL_STEP = { label: "Your organizations", optional: true };

interface OnboardingFlowProps {
  onComplete: (data: unknown) => void;
  // /dev/onboarding-preview: a dry run as a brand-new user. Nothing is saved
  // (profile, credits, resume, orgs), and finishing starts over.
  preview?: boolean;
}

// Pinned over every step of the preview so it's never mistaken for the real thing.
const PREVIEW_BADGE = (
  <div className="pointer-events-none fixed left-1/2 top-3 z-[60] -translate-x-1/2 rounded-full bg-[#1D1D1F]/90 px-4 py-1.5 text-[12px] font-semibold text-white shadow-lg">
    Preview · nothing is saved
  </div>
);

export const OnboardingFlow = ({ onComplete, preview = false }: OnboardingFlowProps) => {
  const navigate = useNavigate();
  const location = useLocation();
  const { completeOnboarding, refreshUser, user, signOut } = useFirebaseAuth();

  const sp = useMemo(() => new URLSearchParams(location.search), [location.search]);
  const returnTo = useMemo(() => sp.get("returnTo") || "", [sp]);

  const [currentStep, setCurrentStep] = useState<Step>("slides");
  const [submitting, setSubmitting] = useState(false);
  const [source, setSource] = useState<SourceResult | null>(null);
  // Completion is deferred until the orgs step resolves (when it shows).
  const [pendingCompletion, setPendingCompletion] = useState<{
    prefill: ResumePrefill;
    linkedinUrl: string;
    step: string;
  } | null>(null);

  // Org alumni directories: joining checks the org's password right here
  // (the user doc already exists from sign-up), and the shared orgs query
  // updates so Find shows the org as soon as onboarding ends.
  // While the list is still loading (or failed), show the step anyway: the
  // editor has its own loading and retry states, and "Skip for now" still works.
  const { orgs, memberships, loading: orgsLoading, isError: orgsError } = useOrgs(!!user, preview);
  const hasOrgStep = orgsLoading || orgsError || orgs.length > 0;
  const RAIL_STEPS = hasOrgStep ? [...BASE_RAIL_STEPS, ORGS_RAIL_STEP] : BASE_RAIL_STEPS;

  // Onboarding analytics: fire-and-forget step events.
  const loggedSteps = useRef<Set<string>>(new Set());
  const logOnboardingEvent = (event: "viewed" | "completed", step: string, skipped = false) => {
    if (preview) return;
    auth.currentUser
      ?.getIdToken()
      .then((token) => {
        fetch(`${BACKEND_URL}/api/users/onboarding-event`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
          body: JSON.stringify({ event, step, skipped }),
        }).catch(() => {});
      })
      .catch(() => {});
  };

  useEffect(() => {
    // Slides log per-slide via onViewSlide (slides_1..slides_N).
    if (currentStep !== "slides" && !loggedSteps.current.has(currentStep)) {
      loggedSteps.current.add(currentStep);
      logOnboardingEvent("viewed", currentStep);
    }
  }, [currentStep]);

  // ── Final Firestore write (same shape the 4-step flow wrote) ──────────────
  // Name/email always resolve: parsed prefill first, then the Google account.
  // userType is intentionally NOT written: without the manual step's
  // student/professional question, the backend infers stage from the resume
  // instead of a hardcoded "student" locking professionals into the wrong voice.
  const buildFinalData = (prefill: ResumePrefill, linkedinUrl: string) => {
    const fullName = prefill.name || user?.name || "";
    const nameParts = fullName.trim().split(/\s+/);
    const firstName = prefill.firstName || nameParts[0] || "";
    const lastName = prefill.lastName || (nameParts.length > 1 ? nameParts.slice(1).join(" ") : "");
    const email = user?.email || prefill.email || "";
    // A .edu unlocks student pricing (Pricing.tsx reads `isStudent`). With the
    // trial step gone this now derives from the sign-up email only.
    const isStudent = email.toLowerCase().trim().endsWith(".edu");
    const now = new Date().toISOString();
    return {
      isStudent,
      profile: { fullName, firstName, lastName, email, phone: prefill.phone || "", linkedinUrl },
      university: prefill.university || "",
      academics: {
        university: prefill.university || "",
        college: prefill.university || "",
        degree: "",
        major: prefill.major || "",
        graduationYear: prefill.graduationYear || "",
      },
      // Career tracks were removed from onboarding (added later from settings) —
      // keep the empty shape downstream readers expect.
      careerTrack: "",
      careerTracks: [],
      careerTrackLabels: [],
      targetIndustries: [],
      goals: { careerTrack: "", careerTracks: [], targetIndustries: [] },
      onboarding: { completedAt: now },
      // Queues the first-email walkthrough, which opens on Find.
      walkthrough: newWalkthrough(now),
      // Gmail isn't connected during onboarding anymore (OFF-50); this badges
      // the Settings Gmail card so connecting it is the next nudge.
      inboxConnectSkipped: true,
    };
  };

  const resolveDestination = () => {
    let dest = "/find";
    if (returnTo) {
      try {
        let decoded = returnTo;
        while (decoded !== decodeURIComponent(decoded)) decoded = decodeURIComponent(decoded);
        if (!decoded.includes("/onboarding") && !decoded.includes("/signin")) dest = decoded;
      } catch {
        /* ignore decode errors */
      }
    }
    return dest;
  };

  // Single completion path: every entry (resume, LinkedIn, builder) ends here.
  const completeWithPrefill = async (
    prefill: ResumePrefill,
    linkedinUrl: string,
    step: string,
  ) => {
    if (submitting) return;
    if (preview) {
      toast.success("Preview finished. Nothing was saved.");
      setPendingCompletion(null);
      setSource(null);
      setCurrentStep("slides");
      return;
    }
    setSubmitting(true);
    try {
      logOnboardingEvent("completed", step);
      const finalData = buildFinalData(prefill, linkedinUrl);
      await completeOnboarding(finalData);
      await new Promise((r) => setTimeout(r, 300));
      await refreshUser();
      trackFeatureActionCompleted("onboarding", "complete", true);
      try {
        onComplete(finalData);
      } catch (e) {
        console.error("Analytics error:", e);
      }
      navigate(resolveDestination(), { replace: true });
    } catch (e) {
      console.error("Onboarding failed:", e);
      toast.error("Failed to finish onboarding. Please try again.");
      setSubmitting(false);
    }
  };

  // ── Step handlers ─────────────────────────────────────────────────────────
  const handleSlidesDone = (skipped: boolean) => {
    logOnboardingEvent("completed", "slides", skipped);
    setCurrentStep("source");
  };
  // After the resume: the orgs step when there is one, else straight in.
  const afterResume = (pending: { prefill: ResumePrefill; linkedinUrl: string; step: string }) => {
    setPendingCompletion(pending);
    if (hasOrgStep) setCurrentStep("orgs");
    else void completeWithPrefill(pending.prefill, pending.linkedinUrl, pending.step);
  };
  const handleSource = (data: SourceResult) => {
    setSource(data);
    afterResume({ prefill: data.resolved, linkedinUrl: data.linkedinUrl, step: "source" });
  };
  const handleBuilderComplete = (prefill: ResumePrefill) => {
    afterResume({ prefill, linkedinUrl: "", step: "resume_builder" });
  };
  const handleOrgsDone = () => {
    if (!pendingCompletion) return;
    logOnboardingEvent("completed", "orgs", memberships.length === 0);
    void completeWithPrefill(pendingCompletion.prefill, pendingCompletion.linkedinUrl, pendingCompletion.step);
  };

  const handleBack = async () => {
    // Backing out of the first step abandons the session entirely: sign out so
    // the next "Sign in" starts fresh at Google instead of resuming this
    // half-onboarded account. ?signedOut=true stops PublicRoute from bouncing
    // back to /onboarding while auth state clears.
    if (currentStep === "slides" && preview) navigate("/find");
    else if (currentStep === "slides") {
      await signOut();
      navigate("/?signedOut=true", { replace: true });
    } else if (currentStep === "source") setCurrentStep("slides");
    else if (currentStep === "builder") setCurrentStep("source");
    else if (currentStep === "orgs")
      setCurrentStep(pendingCompletion?.step === "resume_builder" ? "builder" : "source");
  };

  // Slides bypass the Slate Split shell entirely (full-bleed navy).
  if (currentStep === "slides") {
    return (
      <>
      {preview && PREVIEW_BADGE}
      <OnboardingSlides
        onDone={handleSlidesDone}
        onViewSlide={(i) => {
          const key = `slides_${i + 1}`;
          if (!loggedSteps.current.has(key)) {
            loggedSteps.current.add(key);
            logOnboardingEvent("viewed", key);
          }
        }}
      />
      </>
    );
  }

  const currentIndex = STEP_INDEX[currentStep];

  // Per-step right-pane header + rail footer (hint card on source, mascot on builder).
  const em = (text: string) => <em style={{ fontStyle: "italic", color: OB.primary }}>{text}</em>;
  const STEP_META: Record<
    Exclude<Step, "slides">,
    { headline: React.ReactNode; sub: string; footer: { mascot?: number; hint?: React.ReactNode } }
  > = {
    source: {
      headline: <>Drop your {em("resume or LinkedIn")} to get started</>,
      sub: "",
      footer: {
        hint: (
          <>
            Your resume powers everything here: recommended jobs, people to talk to, personalized
            emails, and auto-apply. No resume? We'll write you one, free.
          </>
        ),
      },
    },
    builder: {
      headline: <>Let's build {em("your resume")}</>,
      sub: "Tell us what you've done. We'll turn it into a clean one-page resume, free.",
      footer: { mascot: 150 },
    },
    orgs: {
      headline: <>Are you part of {em("an organization")}?</>,
      sub: "Pick yours from the list and enter its password. Members get their organization's full alumni directory in Find.",
      footer: {
        hint: (
          <>
            Your organization's alumni show up in Find with a badge, ready to email. Type its name
            plus "alumni" in the search, or tap its pill under the search bar.
          </>
        ),
      },
    },
  };
  const meta = STEP_META[currentStep];
  const panePad = "clamp(24px, 5vw, 48px)";

  return (
    // Full-page Slate Split: the rail and pane fill the whole viewport (no card container).
    <div className="min-h-screen flex" style={{ background: "#fff", fontFamily: OB.fontBody }}>
      {preview && PREVIEW_BADGE}
      <style>{`
        @keyframes obPaneIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
        .ob-pane-anim { animation: obPaneIn .35s cubic-bezier(0.16,1,0.3,1); }
        @media (prefers-reduced-motion: reduce) { .ob-pane-anim { animation: none; } }
      `}</style>
      {/* ── Left rail ─────────────────────────────────────────────────── */}
      <div
        className="hidden md:flex"
        style={{
          width: 308,
          flexShrink: 0,
          background: OB.railGradient,
          color: "#fff",
          padding: "32px 28px",
          flexDirection: "column",
        }}
      >
        <img
          src={OfferloopLogo}
          alt="Offerloop"
          style={{ height: 38, display: "block", alignSelf: "flex-start", marginBottom: 36 }}
        />

        <div style={{ display: "flex", flexDirection: "column", gap: 20, flex: 1 }}>
          {RAIL_STEPS.map((s, i) => {
            const state = i < currentIndex ? "done" : i === currentIndex ? "current" : "upcoming";
            return (
              <div
                key={s.label}
                style={{
                  display: "flex",
                  gap: 12,
                  alignItems: "center",
                  opacity: state === "done" ? 0.6 : state === "upcoming" ? 0.5 : 1,
                }}
              >
                {state === "done" ? (
                  <span
                    style={{
                      width: 28,
                      height: 28,
                      borderRadius: "50%",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      flexShrink: 0,
                      background: "rgba(123,143,201,.25)",
                      color: OB.railPeriwinkle,
                    }}
                  >
                    <Check size={13} strokeWidth={3} />
                  </span>
                ) : (
                  <img
                    src={STEP_NUM_IMAGES[i]}
                    alt={`Step ${i + 1}`}
                    style={{ width: 28, height: 27, flexShrink: 0, display: "block" }}
                  />
                )}
                <span style={{ fontSize: 14, fontWeight: state === "current" ? 600 : 400 }}>
                  {s.label}
                  {s.optional && (
                    <span style={{ fontWeight: 400, color: OB.railPeriwinkle }}> · optional</span>
                  )}
                </span>
              </div>
            );
          })}
        </div>

        {/* Rail footer: mascot or hint card */}
        {meta.footer.mascot ? (
          <img
            src={ScoutYeti}
            alt="Scout"
            style={{
              alignSelf: "center",
              marginTop: 20,
              width: meta.footer.mascot + 105,
              display: "block",
              filter: "drop-shadow(0 6px 18px rgba(0,0,0,.3))",
            }}
          />
        ) : (
          <div
            style={{
              background: "rgba(255,255,255,.06)",
              borderRadius: 12,
              padding: "16px 18px",
              fontSize: 14.5,
              lineHeight: 1.55,
              fontWeight: 600,
              color: "#fff",
            }}
          >
            {meta.footer.hint}
          </div>
        )}
      </div>

      {/* ── Right pane ────────────────────────────────────────────────── */}
      <div
        key={currentStep}
        className="ob-pane-anim"
        style={{
          flex: 1,
          minWidth: 0,
          background: "#fff",
          padding: panePad,
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          position: "relative",
        }}
      >
        <button
          type="button"
          onClick={handleBack}
          style={{
            position: "absolute",
            top: 24,
            left: panePad,
            display: "flex",
            alignItems: "center",
            gap: 6,
            color: OB.ink3,
            fontWeight: 600,
            fontSize: 14,
            background: "none",
            border: "none",
            cursor: "pointer",
            padding: 0,
            fontFamily: OB.fontBody,
          }}
        >
          <ArrowLeft size={17} strokeWidth={1.8} /> Back
        </button>

        {/* Mobile progress (rail hidden below md) */}
        {/* display comes from Tailwind (flex + md:hidden) — an inline display would override the md:hidden breakpoint */}
        <div className="flex md:hidden" style={{ position: "absolute", top: 28, right: panePad, gap: 5 }}>
          {RAIL_STEPS.map((s, i) => (
            <span
              key={s.label}
              style={{
                width: 18,
                height: 4,
                borderRadius: 99,
                background: i <= currentIndex ? OB.primary : OB.primary100,
              }}
            />
          ))}
        </div>

        {/* Readable column centered in the full-width pane (wider for the builder's two-pane grid) */}
        <div
          style={{
            paddingTop: 40,
            width: "100%",
            maxWidth: currentStep === "builder" ? 980 : 640,
            margin: "0 auto",
          }}
        >
          <h2
            style={{
              fontFamily: OB.fontDisplay,
              fontWeight: 600,
              fontSize: "clamp(24px, 3.2vw, 30px)",
              letterSpacing: "-0.02em",
              color: OB.heading,
              margin: "0 0 8px",
              // Even out multi-line headline wraps ("to get started" alone on
              // line two reads broken).
              textWrap: "balance",
            }}
          >
            {meta.headline}
          </h2>
          {meta.sub ? (
            <p style={{ fontSize: 15, color: OB.ink2, margin: "0 0 24px", lineHeight: 1.65 }}>
              {meta.sub}
            </p>
          ) : (
            <div style={{ height: 24 }} />
          )}

          {currentStep === "source" && (
            <OnboardingSource
              onNext={handleSource}
              onBuild={() => {
                logOnboardingEvent("completed", "source", true);
                setCurrentStep("builder");
              }}
              initialLinkedinUrl={source?.linkedinUrl}
              submitting={submitting}
              preview={preview}
            />
          )}
          {currentStep === "builder" && (
            <OnboardingBuilder onComplete={handleBuilderComplete} submitting={submitting} preview={preview} />
          )}
          {currentStep === "orgs" && (
            <div>
              <OrgMembershipEditor
                skipLabel="Skip for now"
                onSkip={handleOrgsDone}
                preview={preview}
              />
              <button
                type="button"
                onClick={handleOrgsDone}
                disabled={submitting}
                style={{ ...obPrimaryButton, marginTop: 24, opacity: submitting ? 0.6 : 1 }}
              >
                {memberships.length > 0 ? "Continue" : "Skip for now"}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
