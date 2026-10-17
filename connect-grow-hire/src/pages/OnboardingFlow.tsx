import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ArrowLeft, Check } from "lucide-react";
import { OnboardingSlides } from "./OnboardingSlides";
import { OnboardingSource, type SourceResult } from "./OnboardingSource";
import { OnboardingBuilder } from "./OnboardingBuilder";
import { OnboardingInbox } from "./OnboardingInbox";
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
import { OB } from "./onboardingTheme";
import { trackFeatureActionCompleted } from "@/lib/analytics";

// Resume-first onboarding: intro slides, then a single resume/LinkedIn page.
// "builder" (the free resume generator) shares the resume phase on the rail.
type Step = "slides" | "source" | "builder" | "inbox";
const STEP_INDEX: Record<Step, number> = { slides: 0, source: 1, builder: 1, inbox: 2 };

const RAIL_STEPS: { label: string; optional?: boolean }[] = [
  { label: "What Offerloop does" },
  { label: "Your resume" },
  { label: "Connect your inbox", optional: true },
];

interface OnboardingFlowProps {
  onComplete: (data: unknown) => void;
}

export const OnboardingFlow = ({ onComplete }: OnboardingFlowProps) => {
  const navigate = useNavigate();
  const location = useLocation();
  const { completeOnboarding, refreshUser, user, signOut } = useFirebaseAuth();

  const sp = useMemo(() => new URLSearchParams(location.search), [location.search]);
  const returnTo = useMemo(() => sp.get("returnTo") || "", [sp]);

  const [currentStep, setCurrentStep] = useState<Step>("slides");
  const [submitting, setSubmitting] = useState(false);
  const [source, setSource] = useState<SourceResult | null>(null);
  // Completion is deferred until the inbox step resolves (connect or skip).
  const [pendingCompletion, setPendingCompletion] = useState<{
    prefill: ResumePrefill;
    linkedinUrl: string;
    step: string;
  } | null>(null);

  // Onboarding analytics: fire-and-forget step events.
  const loggedSteps = useRef<Set<string>>(new Set());
  const logOnboardingEvent = (event: "viewed" | "completed", step: string, skipped = false) => {
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
  const buildFinalData = (prefill: ResumePrefill, linkedinUrl: string, inboxSkipped: boolean) => {
    const fullName = prefill.name || user?.name || "";
    const nameParts = fullName.trim().split(/\s+/);
    const firstName = prefill.firstName || nameParts[0] || "";
    const lastName = prefill.lastName || (nameParts.length > 1 ? nameParts.slice(1).join(" ") : "");
    const email = user?.email || prefill.email || "";
    // A .edu unlocks student pricing (Pricing.tsx reads `isStudent`). With the
    // trial step gone this now derives from the sign-up email only.
    const isStudent = email.toLowerCase().trim().endsWith(".edu");
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
      onboarding: { completedAt: new Date().toISOString() },
      // Read by the Settings surface to badge the Gmail connect card later.
      ...(inboxSkipped ? { inboxConnectSkipped: true } : {}),
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
    inboxSkipped: boolean,
  ) => {
    if (submitting) return;
    setSubmitting(true);
    try {
      logOnboardingEvent("completed", step);
      const finalData = buildFinalData(prefill, linkedinUrl, inboxSkipped);
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
  const handleSource = (data: SourceResult) => {
    setSource(data);
    setPendingCompletion({ prefill: data.resolved, linkedinUrl: data.linkedinUrl, step: "source" });
    setCurrentStep("inbox");
  };
  const handleBuilderComplete = (prefill: ResumePrefill) => {
    setPendingCompletion({ prefill, linkedinUrl: "", step: "resume_builder" });
    setCurrentStep("inbox");
  };
  const handleInboxDone = (skipped: boolean) => {
    if (!pendingCompletion) return;
    logOnboardingEvent("completed", "inbox", skipped);
    void completeWithPrefill(
      pendingCompletion.prefill,
      pendingCompletion.linkedinUrl,
      pendingCompletion.step,
      skipped,
    );
  };

  const handleBack = async () => {
    // Backing out of the first step abandons the session entirely: sign out so
    // the next "Sign in" starts fresh at Google instead of resuming this
    // half-onboarded account. ?signedOut=true stops PublicRoute from bouncing
    // back to /onboarding while auth state clears.
    if (currentStep === "slides") {
      await signOut();
      navigate("/?signedOut=true", { replace: true });
    } else if (currentStep === "source") setCurrentStep("slides");
    else if (currentStep === "builder") setCurrentStep("source");
    else if (currentStep === "inbox")
      setCurrentStep(pendingCompletion?.step === "resume_builder" ? "builder" : "source");
  };

  // Slides bypass the Slate Split shell entirely (full-bleed navy).
  if (currentStep === "slides") {
    return (
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
    inbox: {
      headline: <>Where should {em("your drafts")} go?</>,
      sub: "Offerloop writes personalized outreach emails for you. Pick how you want to receive them.",
      footer: {
        hint: (
          <>
            Connect Gmail and every draft lands in your inbox, ready to send. Skip and your
            emails arrive as one-tap downloads that open in any mail app, resume attached.
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
            />
          )}
          {currentStep === "builder" && (
            <OnboardingBuilder onComplete={handleBuilderComplete} submitting={submitting} />
          )}
          {currentStep === "inbox" && (
            <OnboardingInbox onDone={handleInboxDone} submitting={submitting} />
          )}
        </div>
      </div>
    </div>
  );
};
