import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ArrowLeft, Check, Loader2 } from "lucide-react";
import { OnboardingIntro } from "./OnboardingIntro";
import { OnboardingSource, type SourceResult } from "./OnboardingSource";
import { OnboardingBuilder } from "./OnboardingBuilder";
import { OnboardingConfirm } from "./OnboardingConfirm";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { toast } from "sonner";
import { EMPTY_PREFILL, resumePrefillFromParse, type ResumePrefill } from "@/utils/onboardingPrefill";
import {
  buildDirectionNarrative,
  careerTrackFor,
  inferFromResume,
  mergeDirection,
  type ParsedResume,
} from "@/utils/onboardingInference";
import {
  ONBOARDING_INFERRED,
  applySavedPreferences,
  keepUserEdits,
  provenanceKey,
  savedCareerTrack,
  savedPreferences,
} from "@/utils/onboardingPreferences";
import {
  ONBOARDING_PROGRESS_VERSION,
  resumeStep,
  type BuilderDraft,
  type DraftField,
  type EntryPath,
  type OnboardingProgress,
  type OnboardingStep,
  type ProfileDraft,
} from "@/services/onboardingProgress";
import { liveOnboardingServices, createPreviewServices, PREVIEW_RESUME, type OnboardingServices } from "./onboardingServices";
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

// Onboarding v2 (Milestone 1): one intro screen, then resume / LinkedIn /
// resume builder (required), then a quick confirm of everything inferred from
// it, then the optional org step. Progress is saved on users/{uid} at every
// step so a refresh resumes in place. Finishing hands off to the first-use
// walkthrough at /get-started. Gmail isn't part of onboarding (OFF-50).
//
// "builder" shares the resume phase on the rail.
const STEP_INDEX: Record<Exclude<OnboardingStep, "intro">, number> = {
  source: 0,
  builder: 0,
  confirm: 1,
  orgs: 2,
};

const BASE_RAIL_STEPS: { label: string; optional?: boolean }[] = [
  { label: "Your resume" },
  { label: "Confirm your profile" },
];
const ORGS_RAIL_STEP = { label: "Your organizations", optional: true };

export const WALKTHROUGH_PATH = "/get-started";

interface OnboardingFlowProps {
  onComplete: (data: unknown) => void;
  /** /dev/onboarding-preview: fake services, no auth, no writes. */
  preview?: boolean;
  /** Test seam; defaults to the live (or preview) services. */
  services?: OnboardingServices;
}

const PREVIEW_UID = "preview";

/** The confirm screen's starting point: inference, with the parse prefill filling gaps. */
export function draftFromInference(
  prefill: ResumePrefill,
  inferred: ReturnType<typeof inferFromResume>,
  fallbackName: string,
): ProfileDraft {
  const nameParts = (prefill.name || fallbackName || "").trim().split(/\s+/).filter(Boolean);
  return {
    ...inferred,
    firstName: prefill.firstName || nameParts[0] || "",
    lastName: prefill.lastName || nameParts.slice(1).join(" "),
    phone: prefill.phone || "",
    university: inferred.university || prefill.university || "",
    major: inferred.major || prefill.major || "",
    graduationYear: inferred.graduationYear || prefill.graduationYear || "",
    notInSchool: false,
  };
}

const PROVENANCE_FIELDS: DraftField[] = [
  "university", "major", "graduationYear", "roles", "industries", "targetCompanies", "interests",
];

/**
 * The users/{uid} fields onboarding writes on completion. `saved` is the
 * user doc as it stands: a career track the user set themselves is kept, and
 * fieldProvenance records which fields are theirs ("explicit") and which they
 * accepted as inferred. completeOnboarding merges the nested maps field by
 * field, so nothing else in goals/profile/academics is touched.
 */
export function buildCompletionData(
  draft: ProfileDraft,
  opts: { email: string; fallbackName: string; linkedinUrl: string; now?: string; saved?: Record<string, any> | null },
) {
  const now = opts.now ?? new Date().toISOString();
  const saved = opts.saved || {};
  const edited = new Set(draft.edited || []);
  const fullName = [draft.firstName, draft.lastName].filter(Boolean).join(" ") || opts.fallbackName;
  const email = opts.email;
  // A .edu unlocks student pricing (Pricing.tsx reads `isStudent`).
  const isStudent = email.toLowerCase().trim().endsWith(".edu");
  const university = draft.notInSchool ? "" : draft.university;
  const ownTrack = savedCareerTrack(saved);
  const careerTrack = ownTrack || careerTrackFor(draft);
  const careerTracks =
    ownTrack && Array.isArray(saved.careerTracks) && saved.careerTracks.length
      ? saved.careerTracks
      : careerTrack ? [careerTrack] : [];

  const fieldProvenance: Record<string, string> = {};
  for (const field of PROVENANCE_FIELDS) {
    fieldProvenance[provenanceKey(field)!] = edited.has(field) ? "explicit" : ONBOARDING_INFERRED;
  }
  if (edited.has("notInSchool")) fieldProvenance.school = "explicit";
  fieldProvenance.careerTrack = ownTrack ? "explicit" : ONBOARDING_INFERRED;

  return {
    isStudent,
    profile: {
      fullName,
      firstName: draft.firstName,
      lastName: draft.lastName,
      email,
      phone: draft.phone,
      linkedinUrl: opts.linkedinUrl,
      interests: draft.interests,
    },
    university,
    universityOptOut: draft.notInSchool,
    academics: {
      university,
      college: university,
      major: draft.major,
      graduationYear: draft.graduationYear,
    },
    // Inferred and confirmed, never asked as a questionnaire. These are the
    // fields the people ranker and its deck fingerprint read, so the first
    // recommendations reflect them.
    careerTrack,
    careerTracks,
    careerTrackLabels: careerTracks,
    targetIndustries: draft.industries,
    targetRoles: draft.roles,
    extractedRoles: draft.roles,
    targetFirms: draft.targetCompanies,
    // Top-level too: Settings reads and saves this one.
    dreamCompanies: draft.targetCompanies,
    goals: {
      careerTrack,
      careerTracks,
      targetIndustries: draft.industries,
      dreamCompanies: draft.targetCompanies,
      dreamCompaniesSource: edited.has("targetCompanies") ? "explicit" : ONBOARDING_INFERRED,
    },
    fieldProvenance,
    onboarding: { completedAt: now, version: ONBOARDING_PROGRESS_VERSION, profileConfirmedAt: now },
    onboardingProgress: null,
    // Milestone 2's first-use walkthrough picks up from here.
    walkthrough: { status: "pending", createdAt: now },
    // Gmail isn't connected during onboarding (OFF-50); this badges the
    // Settings Gmail card until the walkthrough asks for it.
    inboxConnectSkipped: true,
  };
}

export const OnboardingFlow = ({ onComplete, preview = false, services: injected }: OnboardingFlowProps) => {
  const navigate = useNavigate();
  const location = useLocation();
  const { completeOnboarding, refreshUser, user, signOut } = useFirebaseAuth();
  const services = useMemo(
    () => injected ?? (preview ? createPreviewServices() : liveOnboardingServices),
    [injected, preview],
  );
  const uid = preview ? PREVIEW_UID : user?.uid;

  const sp = useMemo(() => new URLSearchParams(location.search), [location.search]);
  const returnTo = useMemo(() => sp.get("returnTo") || "", [sp]);

  const [currentStep, setCurrentStep] = useState<OnboardingStep | null>(null);
  const [draft, setDraft] = useState<ProfileDraft | null>(null);
  const [entryPath, setEntryPath] = useState<EntryPath | undefined>();
  const [linkedinUrl, setLinkedinUrl] = useState("");
  const [inferring, setInferring] = useState(false);
  const [sparse, setSparse] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  // Org alumni directories: joining checks the org's password right here
  // (the user doc already exists from sign-up), and the shared orgs query
  // updates so Find shows the org as soon as onboarding ends.
  // While the list is still loading (or failed), show the step anyway: the
  // editor has its own loading and retry states, and "Skip for now" still works.
  const { orgs, memberships, loading: orgsLoading, isError: orgsError } = useOrgs(!!user && !preview);
  const hasOrgStep = !preview && (orgsLoading || orgsError || orgs.length > 0);
  const RAIL_STEPS = hasOrgStep ? [...BASE_RAIL_STEPS, ORGS_RAIL_STEP] : BASE_RAIL_STEPS;

  // ── Resume where the user left off ────────────────────────────────────────
  useEffect(() => {
    if (!uid || currentStep !== null) return;
    let cancelled = false;
    services
      .loadProgress(uid)
      .catch(() => null)
      .then((progress) => {
        if (cancelled) return;
        const step = resumeStep(progress);
        if (progress && step !== "intro") {
          setEntryPath(progress.entryPath);
          setLinkedinUrl(progress.linkedinUrl || "");
          setSparse(!!progress.sparse);
          if (progress.draft) setDraft(progress.draft);
          // Refreshed while the resume was being read: read it again, keeping
          // anything the user had already edited.
          if (step === "confirm" && progress.entryPath && (progress.inferPending || !progress.draft)) {
            void inferDraft(EMPTY_PREFILL, progress.entryPath, progress.linkedinUrl || "", progress.draft ?? null);
            return;
          }
        }
        setCurrentStep(step);
      });
    return () => {
      cancelled = true;
    };
  }, [uid, services, currentStep]);

  // Fire-and-forget step-view analytics, once per step.
  const loggedSteps = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!currentStep || loggedSteps.current.has(currentStep)) return;
    loggedSteps.current.add(currentStep);
    services.logEvent("viewed", currentStep);
  }, [currentStep, services]);

  // Progress writes never block the flow; the worst case is a refresh that
  // resumes one step earlier.
  const persist = useCallback(
    (progress: Omit<OnboardingProgress, "version">) => {
      if (!uid) return;
      services
        .saveProgress(uid, { version: ONBOARDING_PROGRESS_VERSION, ...progress })
        .catch((e) => console.warn("[onboarding] progress save failed", e));
    },
    [uid, services],
  );

  const goTo = (step: OnboardingStep, extra: Partial<OnboardingProgress> = {}) => {
    window.clearTimeout(editSaveTimer.current);
    setCurrentStep(step);
    persist({ step, entryPath, linkedinUrl: linkedinUrl || undefined, draft: draft ?? undefined, sparse, ...extra });
  };

  // Confirm-screen edits are saved shortly after the user stops typing, so a
  // refresh keeps them.
  const editSaveTimer = useRef<number | undefined>();
  useEffect(() => () => window.clearTimeout(editSaveTimer.current), []);
  const handleDraftEdit = useCallback(
    (edited: ProfileDraft) => {
      setDraft(edited);
      window.clearTimeout(editSaveTimer.current);
      editSaveTimer.current = window.setTimeout(() => {
        persist({ step: "confirm", entryPath, linkedinUrl: linkedinUrl || undefined, draft: edited, sparse });
      }, 500);
    },
    [persist, entryPath, linkedinUrl, sparse],
  );

  // Resume builder work lives in this browser only (see saveBuilderDraft).
  const loadBuilderDraft = useCallback(() => (uid ? services.loadBuilderDraft(uid) : null), [uid, services]);
  const handleBuilderDraft = useCallback(
    (d: BuilderDraft | null) => {
      if (uid) services.saveBuilderDraft(uid, d);
    },
    [uid, services],
  );

  // Restored on the org step but there are no orgs to join after all: back to
  // confirm, whose button finishes onboarding.
  useEffect(() => {
    if (currentStep === "orgs" && !preview && !orgsLoading && !orgsError && orgs.length === 0) {
      setCurrentStep("confirm");
    }
  }, [currentStep, preview, orgsLoading, orgsError, orgs.length]);

  const resolveReturnTo = () => {
    if (!returnTo) return "";
    try {
      let decoded = returnTo;
      while (decoded !== decodeURIComponent(decoded)) decoded = decodeURIComponent(decoded);
      if (decoded.startsWith("/") && !decoded.startsWith("//") && !decoded.includes("/onboarding") && !decoded.includes("/signin")) {
        return decoded;
      }
    } catch {
      /* ignore decode errors */
    }
    return "";
  };

  // ── Inference: resume → confirm draft ─────────────────────────────────────
  // Rules first, then the optional LLM refinement. Preferences the user
  // already saved, and anything they edited in `prev`, beat the inference.
  const inferDraft = async (prefill: ResumePrefill, path: EntryPath, url: string, prev: ProfileDraft | null) => {
    if (!uid) return;
    setEntryPath(path);
    setLinkedinUrl(url);
    setCurrentStep("confirm");
    setInferring(true);
    // Recorded first, so a refresh mid-inference re-runs it instead of
    // returning to the source step (the resume is already saved).
    persist({ step: "confirm", entryPath: path, linkedinUrl: url || undefined, draft: prev ?? undefined, inferPending: true });
    let parsed: ParsedResume | null = null;
    let userDoc: Record<string, unknown> | null = null;
    try {
      ({ resumeParsed: parsed, userDoc } = await services.loadProfileSources(uid));
    } catch {
      parsed = null;
    }
    let inferred = inferFromResume(parsed);
    const narrative = buildDirectionNarrative(parsed);
    if (narrative) {
      inferred = mergeDirection(inferred, await services.extractDirection(narrative).catch(() => null));
    }
    // The source step's prefill can be empty after a refresh; the saved parse fills it.
    const fromParse = resumePrefillFromParse(parsed);
    const mergedPrefill = Object.fromEntries(
      Object.entries(prefill).map(([k, v]) => [k, v || fromParse[k as keyof ResumePrefill]]),
    ) as unknown as ResumePrefill;
    const inferredDraft = draftFromInference(mergedPrefill, inferred, user?.name || "");
    const next = keepUserEdits(prev, applySavedPreferences(inferredDraft, savedPreferences(userDoc)));
    const isSparse = !parsed && !mergedPrefill.university && !mergedPrefill.name;
    setSparse(isSparse);
    setDraft(next);
    setInferring(false);
    persist({ step: "confirm", entryPath: path, linkedinUrl: url || undefined, draft: next, sparse: isSparse });
  };

  // ── Completion ────────────────────────────────────────────────────────────
  const complete = async (finalDraft: ProfileDraft) => {
    if (submitting) return;
    setSubmitting(true);
    services.logEvent("completed", hasOrgStep ? "orgs" : "confirm", hasOrgStep && memberships.length === 0);
    const dest = resolveReturnTo();
    const target = `${WALKTHROUGH_PATH}${dest ? `?returnTo=${encodeURIComponent(dest)}` : ""}`;
    if (preview) {
      navigate("/dev/get-started-preview", { replace: true });
      return;
    }
    try {
      // Read fresh: the user may have changed a preference elsewhere since inference ran.
      const saved = uid ? (await services.loadProfileSources(uid).catch(() => null))?.userDoc : null;
      const finalData = buildCompletionData(finalDraft, {
        email: user?.email || "",
        fallbackName: user?.name || "",
        linkedinUrl,
        saved,
      });
      await completeOnboarding(finalData);
      if (uid) services.saveBuilderDraft(uid, null);
      await new Promise((r) => setTimeout(r, 300));
      await refreshUser();
      trackFeatureActionCompleted("onboarding", "complete", true);
      try {
        onComplete(finalData);
      } catch (e) {
        console.error("Analytics error:", e);
      }
      navigate(target, { replace: true });
    } catch (e) {
      console.error("Onboarding failed:", e);
      toast.error("Failed to finish onboarding. Please try again.");
      setSubmitting(false);
    }
  };

  // ── Step handlers ─────────────────────────────────────────────────────────
  const handleIntroDone = () => {
    services.logEvent("completed", "intro");
    goTo("source");
  };
  const handleSource = (data: SourceResult) => {
    services.logEvent("completed", "source");
    void inferDraft(data.resolved, data.entryPath, data.linkedinUrl, draft);
  };
  const handleBuilderComplete = (prefill: ResumePrefill) => {
    services.logEvent("completed", "resume_builder");
    void inferDraft(prefill, "builder", "", draft);
  };
  const handleConfirm = (confirmed: ProfileDraft) => {
    services.logEvent("completed", "confirm");
    window.clearTimeout(editSaveTimer.current);
    setDraft(confirmed);
    if (hasOrgStep) {
      setCurrentStep("orgs");
      persist({ step: "orgs", entryPath, linkedinUrl: linkedinUrl || undefined, draft: confirmed, sparse });
    } else {
      void complete(confirmed);
    }
  };
  const handleOrgsDone = () => {
    if (draft) void complete(draft);
  };

  const handleBack = async () => {
    // Backing out of the first step abandons the session entirely: sign out so
    // the next "Sign in" starts fresh at Google instead of resuming this
    // half-onboarded account. ?signedOut=true stops PublicRoute from bouncing
    // back to /onboarding while auth state clears.
    if (currentStep === "intro") {
      if (preview) return;
      await signOut();
      navigate("/?signedOut=true", { replace: true });
    } else if (currentStep === "source") goTo("intro");
    else if (currentStep === "builder") goTo("source");
    else if (currentStep === "confirm") goTo("source");
    else if (currentStep === "orgs") goTo("confirm");
  };

  if (currentStep === null) {
    return (
      <div className="min-h-screen flex items-center justify-center" style={{ background: OB.pageBg }}>
        <Loader2 className="animate-spin" size={26} style={{ color: OB.primary }} aria-label="Loading" />
      </div>
    );
  }

  // The intro bypasses the Slate Split shell entirely.
  if (currentStep === "intro") {
    return <OnboardingIntro onContinue={handleIntroDone} onBack={handleBack} />;
  }

  const currentIndex = STEP_INDEX[currentStep];

  // Per-step right-pane header + rail footer (hint card on source, mascot on builder).
  const em = (text: string) => <em style={{ fontStyle: "italic", color: OB.primary }}>{text}</em>;
  const STEP_META: Record<
    Exclude<OnboardingStep, "intro">,
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
    confirm: {
      headline: <>Here's what we {em("picked up")}</>,
      sub: "Fix anything that's off. This decides who we suggest and how your emails are written.",
      footer: {
        hint: (
          <>
            We read your school, roles and target firms from your resume, so there's no questionnaire.
            You can change any of it later in Settings.
          </>
        ),
      },
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
            <>
              <OnboardingSource
                onNext={handleSource}
                onBuild={() => {
                  services.logEvent("completed", "source", true);
                  goTo("builder");
                }}
                initialLinkedinUrl={linkedinUrl}
                submitting={submitting}
              />
              {preview && (
                <button
                  type="button"
                  onClick={() => void inferDraft({ ...EMPTY_PREFILL, name: PREVIEW_RESUME.name || "" }, "resume", "", draft)}
                  style={{ marginTop: 16, width: "100%", fontSize: 13, color: OB.ink3, background: "none", border: `1px dashed ${OB.border}`, borderRadius: 10, padding: 10, cursor: "pointer" }}
                >
                  Preview only: use a sample resume
                </button>
              )}
            </>
          )}
          {currentStep === "builder" && (
            <OnboardingBuilder
              onComplete={handleBuilderComplete}
              submitting={submitting}
              loadDraft={loadBuilderDraft}
              onDraftChange={handleBuilderDraft}
            />
          )}
          {currentStep === "confirm" &&
            (inferring || !draft ? (
              <div role="status" style={{ display: "flex", alignItems: "center", gap: 10, color: OB.ink2, fontSize: 15, padding: "24px 0" }}>
                <Loader2 className="animate-spin" size={18} style={{ color: OB.primary }} />
                Reading your background…
              </div>
            ) : (
              <OnboardingConfirm
                initial={draft}
                onConfirm={handleConfirm}
                onChange={handleDraftEdit}
                submitting={submitting}
                sparse={sparse}
              />
            ))}
          {currentStep === "orgs" && (
            <div>
              <OrgMembershipEditor
                school={draft?.university || null}
                skipLabel="Skip for now"
                onSkip={handleOrgsDone}
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
