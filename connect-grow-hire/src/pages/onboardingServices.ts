/**
 * Everything OnboardingFlow does outside React, behind one interface so the
 * dev preview (/dev/onboarding-preview) and tests can swap in fakes that make
 * no network calls and write nothing.
 */
import { BACKEND_URL, extractDirection } from "@/services/api";
import { auth } from "@/lib/firebase";
import {
  loadBuilderDraft,
  loadOnboardingProgress,
  loadProfileSources,
  saveBuilderDraft,
  saveOnboardingProgress,
  type BuilderDraft,
  type OnboardingProgress,
} from "@/services/onboardingProgress";
import type { DirectionExtract, ParsedResume } from "@/utils/onboardingInference";

export interface OnboardingServices {
  loadProgress(uid: string): Promise<OnboardingProgress | null>;
  saveProgress(uid: string, progress: OnboardingProgress): Promise<void>;
  /** The resumeParsed the source step's backend route saved, plus the rest of the user doc. */
  loadProfileSources(uid: string): Promise<{ resumeParsed: ParsedResume | null; userDoc: Record<string, unknown> | null }>;
  /** The resume builder's in-progress work; null clears it. */
  loadBuilderDraft(uid: string): BuilderDraft | null;
  saveBuilderDraft(uid: string, draft: BuilderDraft | null): void;
  /** Optional LLM refinement; resolves null on any failure. */
  extractDirection(narrative: string): Promise<DirectionExtract | null>;
  logEvent(event: "viewed" | "completed", step: string, skipped?: boolean): void;
}

export const liveOnboardingServices: OnboardingServices = {
  async loadProgress(uid) {
    return (await loadOnboardingProgress(uid)).progress;
  },
  saveProgress: saveOnboardingProgress,
  loadProfileSources,
  loadBuilderDraft,
  saveBuilderDraft,
  extractDirection,
  logEvent(event, step, skipped = false) {
    auth.currentUser
      ?.getIdToken()
      .then((token) =>
        fetch(`${BACKEND_URL}/api/users/onboarding-event`, {
          method: "POST",
          headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
          body: JSON.stringify({ event, step, skipped }),
        }),
      )
      .catch(() => {});
  },
};

/** Sample parsed resume for the dev preview. Not a real person. */
export const PREVIEW_RESUME: ParsedResume = {
  name: "Jordan Sample",
  objective: "Interested in investment banking and strategy roles.",
  education: {
    university: "University of Southern California",
    major: "Business Administration",
    graduation: "May 2027",
  },
  experience: [
    { title: "Summer Analyst", company: "Regional Investment Bank" },
    { title: "Finance Intern", company: "Campus Startup" },
  ],
  extracurriculars: [
    { activity: "Investment club", organization: "Trojan Investment Society", role: "Analyst" },
    { activity: "Case competitions", organization: "Consulting Club" },
  ],
};

/** In-memory services for /dev/onboarding-preview: no Firestore, no backend. */
export function createPreviewServices(): OnboardingServices {
  let saved: OnboardingProgress | null = null;
  let builder: BuilderDraft | null = null;
  return {
    loadProgress: async () => saved,
    saveProgress: async (_uid, progress) => {
      saved = progress;
    },
    loadProfileSources: async () => ({ resumeParsed: PREVIEW_RESUME, userDoc: null }),
    loadBuilderDraft: () => builder,
    saveBuilderDraft: (_uid, draft) => {
      builder = draft;
    },
    extractDirection: async () => null,
    logEvent: () => {},
  };
}
