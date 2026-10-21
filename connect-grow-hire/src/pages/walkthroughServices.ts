/**
 * Everything the first-use walkthrough does outside React, behind one
 * interface so the dev preview (/dev/get-started-preview) and tests can swap
 * in fakes that make no network calls, spend nothing and write nothing.
 */
import { apiService } from "@/services/api";
import { firebaseApi } from "@/services/firebaseApi";
import { personFromRanker, type Person } from "@/components/find/people/peopleModel";
import {
  loadDraft,
  loadWalkthrough,
  saveDraft,
  saveWalkthrough,
  type EmailDraft,
  type MatchProfile,
  type WalkthroughState,
} from "@/services/walkthrough";

export const WALKTHROUGH_SURFACE = "web_walkthrough";

export type RevealResult = { ok: true } | { ok: false; reason: "no_email" | "insufficient_credits" | "error" };
export type DraftResult = { ok: true } | { ok: false; reason: "generation_failed" | "no_email" | "error" };

export interface WalkthroughServices {
  loadState(uid: string): Promise<WalkthroughState | null>;
  saveState(uid: string, state: WalkthroughState): Promise<void>;
  /** School, target companies and roles, for match explanations. */
  loadProfile(uid: string): Promise<MatchProfile | null>;
  /** Recommended people (the ranker deck). Never starts paid prefetches or digs. */
  loadPeople(exclude: string[]): Promise<{ people: Person[]; reason?: string }>;
  /** Find their email. Spends REVEAL_COST credits (refunded on a miss) unless firstOutreachFree. */
  revealEmail(candidateId: string, deckId: string): Promise<RevealResult>;
  /** Write the email (in the app only; no Gmail draft). */
  writeDraft(candidateId: string, deckId: string): Promise<DraftResult>;
  loadDraft(uid: string, contactId: string): Promise<EmailDraft | null>;
  saveDraft(uid: string, contactId: string, draft: Pick<EmailDraft, "subject" | "body">): Promise<void>;
  /**
   * The first email is free for new users. Pending the backend entitlement
   * (see the Milestone 2 notes): false until it ships, so the page states
   * the real cost.
   */
  firstOutreachFree: boolean;
}

const errorStatus = (e: unknown): number | undefined => (e as { status?: number })?.status;
const errorMessage = (e: unknown): string => (e instanceof Error ? e.message : String(e ?? ""));

export const liveWalkthroughServices: WalkthroughServices = {
  loadState: loadWalkthrough,
  saveState: saveWalkthrough,
  async loadProfile(uid) {
    const d = await firebaseApi.getUserOnboardingData(uid);
    return {
      university: d.university || "",
      targetCompanies: [...(d.dreamCompanies || []), ...(d.targetFirms || [])],
      roles: d.extractedRoles || [],
    };
  },
  async loadPeople(exclude) {
    const res = await apiService.getRankerCandidates({
      limit: 20,
      exclude,
      surface: WALKTHROUGH_SURFACE,
      allowPaidLookups: false,
    });
    const people = (res.candidates || [])
      .map((c, i) => personFromRanker(c, i, res.deck_id || ""))
      .filter((p): p is Person => p !== null);
    return { people, reason: res.reason };
  },
  async revealEmail(candidateId, deckId) {
    try {
      const res = await apiService.revealRankerEmail(candidateId, deckId, WALKTHROUGH_SURFACE);
      return res.email ? { ok: true } : { ok: false, reason: "no_email" };
    } catch (e) {
      if (errorStatus(e) === 402) return { ok: false, reason: "insufficient_credits" };
      return { ok: false, reason: errorMessage(e) === "candidate_not_found" ? "no_email" : "error" };
    }
  },
  async writeDraft(candidateId, deckId) {
    try {
      const res = await apiService.rankerOutreach(candidateId, "draft", deckId, {
        createGmailDraft: false,
        surface: WALKTHROUGH_SURFACE,
      });
      return res.drafted ? { ok: true } : { ok: false, reason: "generation_failed" };
    } catch (e) {
      const msg = errorMessage(e);
      if (msg === "no_verified_email") return { ok: false, reason: "no_email" };
      return { ok: false, reason: msg === "generation_failed" ? "generation_failed" : "error" };
    }
  },
  loadDraft,
  saveDraft,
  firstOutreachFree: false,
};

// ── Preview: sample people, fake email, nothing leaves the browser ──────────

const sample = (i: number, first: string, last: string, title: string, company: string, school: string, reasons: string[]): Person => ({
  id: `preview-${i}`,
  source: "recommended",
  name: `${first} ${last}`,
  firstName: first,
  lastName: last,
  title,
  company,
  location: "New York, NY",
  school,
  linkedinUrl: "",
  photoUrl: "",
  email: "",
  reasons,
  briefing: `${first} joined ${company} after graduating and works with students recruiting for ${title.toLowerCase()} roles.`,
  matchScore: null,
  rank: i,
  deckId: "preview-deck",
});

/** Sample people for the preview. Not real people. */
export const PREVIEW_PEOPLE: Person[] = [
  sample(0, "Riley", "Sample", "Investment Banking Analyst", "Goldman Sachs", "University of Southern California", ["Same school"]),
  sample(1, "Morgan", "Example", "Associate", "Evercore", "University of Michigan", ["Started as a summer analyst"]),
  sample(2, "Casey", "Placeholder", "Investment Banking Analyst", "Goldman Sachs", "NYU", ["Same role you're targeting"]),
  sample(3, "Jamie", "Demo", "Strategy Consultant", "McKinsey & Company", "University of Southern California", ["Same school"]),
  sample(4, "Avery", "Mock", "Private Equity Analyst", "Blackstone", "Georgetown University", []),
];

export function createPreviewWalkthroughServices(): WalkthroughServices {
  let state: WalkthroughState | null = { version: 1, status: "pending", step: "matches" };
  const drafts = new Map<string, EmailDraft>();
  const delay = () => new Promise((r) => setTimeout(r, 600));
  return {
    loadState: async () => state,
    saveState: async (_uid, s) => {
      state = s;
    },
    loadProfile: async () => ({
      university: "University of Southern California",
      targetCompanies: ["Goldman Sachs", "McKinsey & Company"],
      roles: ["Investment Banking Analyst"],
    }),
    loadPeople: async (exclude) => ({ people: PREVIEW_PEOPLE.filter((p) => !exclude.includes(p.id)) }),
    // The second sample person shows the "no email" path.
    revealEmail: async (id) => {
      await delay();
      return id === "preview-1" ? { ok: false, reason: "no_email" } : { ok: true };
    },
    writeDraft: async (id) => {
      await delay();
      const p = PREVIEW_PEOPLE.find((x) => x.id === id);
      drafts.set(`ranker_${id}`, {
        to: `${(p?.firstName || "them").toLowerCase()}@example.com`,
        subject: "USC student interested in your path",
        body: `Hi ${p?.firstName || "there"},\n\nI'm a sophomore at USC studying business administration, and I'm interested in ${p?.title.toLowerCase() || "your work"}. I'd love to hear how you got to ${p?.company || "your team"} and what you'd do differently.\n\nWould you be open to a 15-minute call in the next couple of weeks?\n\nBest,\nJordan`,
      });
      return { ok: true };
    },
    loadDraft: async (_uid, contactId) => drafts.get(contactId) ?? null,
    saveDraft: async (_uid, contactId, d) => {
      const cur = drafts.get(contactId);
      if (cur) drafts.set(contactId, { ...cur, ...d });
    },
    firstOutreachFree: false,
  };
}
