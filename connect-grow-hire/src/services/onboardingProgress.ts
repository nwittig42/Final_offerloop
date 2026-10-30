/**
 * Onboarding progress, persisted on users/{uid}.onboardingProgress so a
 * refresh (or a new device) resumes where the user left off instead of
 * restarting at the intro.
 *
 * The user doc is the user's own: Firestore rules let them update any field
 * except the protected billing/credit/ranking list, so no rules change is
 * needed. The resume itself is already saved server-side by the parse,
 * LinkedIn and builder routes; progress only records the step and the
 * not-yet-confirmed profile draft.
 */
import { doc, getDoc, updateDoc } from "firebase/firestore";
import { db } from "@/lib/firebase";
import type { InferredProfile, ParsedResume } from "@/utils/onboardingInference";

export const ONBOARDING_PROGRESS_VERSION = 2;

export type OnboardingStep = "intro" | "source" | "builder" | "confirm" | "orgs";
export type EntryPath = "resume" | "linkedin" | "builder";

/** Confirm-screen fields a user can set by hand. */
export type DraftField = keyof InferredProfile | "firstName" | "lastName" | "notInSchool";

/** What the confirm screen edits, and what onboarding finally saves. */
export interface ProfileDraft extends InferredProfile {
  firstName: string;
  lastName: string;
  phone: string;
  /** "Not in school / not listed" — saved as universityOptOut. */
  notInSchool: boolean;
  /**
   * Fields the user set themselves: edited on the confirm screen, or already
   * saved on their profile before onboarding. Re-running inference (a new
   * resume, a refresh mid-inference) never replaces these.
   */
  edited?: DraftField[];
}

export interface OnboardingProgress {
  version: number;
  step: OnboardingStep;
  entryPath?: EntryPath;
  linkedinUrl?: string;
  draft?: ProfileDraft;
  /** The resume couldn't be read, so the confirm screen shows its notice. */
  sparse?: boolean;
  /** Inference had started but not finished; a refresh re-runs it. */
  inferPending?: boolean;
  updatedAt?: string;
}

const STEPS: OnboardingStep[] = ["intro", "source", "builder", "confirm", "orgs"];

/**
 * Where to resume. Stale versions and unknown steps restart at the intro.
 * Confirm and orgs need the profile draft. Without one (a refresh while the
 * resume was being read), confirm re-runs inference from the resume the
 * source step already saved; with no resume step recorded, back to source.
 */
export function resumeStep(progress: OnboardingProgress | null | undefined): OnboardingStep {
  if (!progress || progress.version !== ONBOARDING_PROGRESS_VERSION) return "intro";
  if (!STEPS.includes(progress.step)) return "intro";
  if ((progress.step === "confirm" || progress.step === "orgs") && !progress.draft) {
    return progress.entryPath ? "confirm" : "source";
  }
  return progress.step;
}

export async function loadOnboardingProgress(uid: string): Promise<{
  progress: OnboardingProgress | null;
  resumeParsed: ParsedResume | null;
}> {
  const snap = await getDoc(doc(db, "users", uid));
  const data = snap.exists() ? (snap.data() as Record<string, unknown>) : {};
  const progress = (data.onboardingProgress as OnboardingProgress | undefined) ?? null;
  const resumeParsed = (data.resumeParsed as ParsedResume | undefined) ?? null;
  return { progress, resumeParsed };
}

/**
 * What inference and completion read: the parsed resume the source step's
 * backend route just saved, and the rest of the user doc (preferences the
 * user already set, which inference must not replace).
 */
export async function loadProfileSources(uid: string): Promise<{
  resumeParsed: ParsedResume | null;
  userDoc: Record<string, unknown> | null;
}> {
  const snap = await getDoc(doc(db, "users", uid));
  if (!snap.exists()) return { resumeParsed: null, userDoc: null };
  const data = snap.data() as Record<string, unknown>;
  return { resumeParsed: (data.resumeParsed as ParsedResume | undefined) ?? null, userDoc: data };
}

/** Firestore rejects undefined field values; drop them before writing. */
function withoutUndefined<T extends object>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

export async function saveOnboardingProgress(uid: string, progress: OnboardingProgress): Promise<void> {
  await updateDoc(doc(db, "users", uid), {
    onboardingProgress: withoutUndefined({ ...progress, updatedAt: new Date().toISOString() }),
  });
}

// ── Resume builder draft (this browser only) ─────────────────────────────────
// The builder's typed sections and generated resume are kept in localStorage,
// saved on every change, so a refresh doesn't lose them. Local rather than
// Firestore: it changes on every keystroke and the generated HTML is large,
// and the builder's finalize step already saves the finished resume
// server-side. Kept until onboarding completes, so going back to the builder
// shows the same work.

export const BUILDER_DRAFT_VERSION = 1;

export interface BuilderDraft {
  version: number;
  sections: Record<string, string>;
  prompt: string;
  history: string[];
  resume: object | null;
  html: string;
}

const builderKey = (uid: string) => `offerloop:onboarding-builder:${uid}`;

export function loadBuilderDraft(uid: string): BuilderDraft | null {
  try {
    const raw = localStorage.getItem(builderKey(uid));
    if (!raw) return null;
    const draft = JSON.parse(raw) as BuilderDraft;
    return draft?.version === BUILDER_DRAFT_VERSION ? draft : null;
  } catch {
    return null;
  }
}

export function saveBuilderDraft(uid: string, draft: BuilderDraft | null): void {
  try {
    if (draft) localStorage.setItem(builderKey(uid), JSON.stringify(draft));
    else localStorage.removeItem(builderKey(uid));
  } catch {
    /* storage full or blocked: the builder still works, it just won't survive a refresh */
  }
}
