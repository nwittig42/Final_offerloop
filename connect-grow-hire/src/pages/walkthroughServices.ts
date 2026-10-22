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

export type RevealResult =
  | { ok: true; free: boolean }
  | { ok: false; reason: "no_email" | "insufficient_credits" | "free_unavailable" | "error" };
export type DraftResult = { ok: true } | { ok: false; reason: "generation_failed" | "no_email" | "error" };

export interface GmailState {
  connected: boolean;
  /** Connected with a permission that can send (compose / send). */
  canSend: boolean;
}

export interface SendReview {
  recipient: string;
  subject: string;
  /** Server hash of the saved draft; sending requires it back. */
  hash: string;
  blocking: string[];
  warnings: string[];
  alreadySent: boolean;
}

export type SendError =
  | "gmail_not_connected" | "gmail_token_expired" | "gmail_scope_missing"
  | "draft_changed" | "send_in_progress" | "send_unconfirmed"
  | "blocked" | "daily_cap_reached" | "send_failed";
export type SendResult = { ok: true; alreadySent: boolean } | { ok: false; reason: SendError };

const SEND_SCOPES = [
  "https://www.googleapis.com/auth/gmail.compose",
  "https://www.googleapis.com/auth/gmail.send",
  "https://mail.google.com/",
];
/** Mirrors the backend: connected with no scope list recorded counts as able to send. */
export const canSendWith = (scopes: string[] | undefined) =>
  !scopes || scopes.length === 0 || scopes.some((s) => SEND_SCOPES.includes(s));

export interface WalkthroughServices {
  loadState(uid: string): Promise<WalkthroughState | null>;
  saveState(uid: string, state: WalkthroughState): Promise<void>;
  /** School, target companies and roles, for match explanations. */
  loadProfile(uid: string): Promise<MatchProfile | null>;
  /** Recommended people (the ranker deck). Never starts paid prefetches or digs. */
  loadPeople(exclude: string[]): Promise<{ people: Person[]; reason?: string }>;
  /** Whether the one-time free first outreach can be used (decided by the backend). */
  loadFirstOutreach(): Promise<boolean>;
  /**
   * Find their email. useFree spends the free first outreach (only used up
   * when an email is found); otherwise REVEAL_COST credits, refunded on a miss.
   */
  revealEmail(candidateId: string, deckId: string, useFree: boolean): Promise<RevealResult>;
  /** Write the email (in the app only; no Gmail draft). */
  writeDraft(candidateId: string, deckId: string): Promise<DraftResult>;
  loadDraft(uid: string, contactId: string): Promise<EmailDraft | null>;
  saveDraft(uid: string, contactId: string, draft: Pick<EmailDraft, "subject" | "body">): Promise<void>;
  gmailStatus(): Promise<GmailState>;
  /**
   * Start Gmail OAuth, coming back to returnTo. Live: leaves the page for
   * Google ("redirecting"). Preview: connects in place ("connected").
   */
  connectGmail(returnTo: string): Promise<"redirecting" | "connected">;
  /** What would be sent (server view of the saved draft). */
  reviewSend(contactId: string): Promise<SendReview>;
  sendEmail(contactId: string, confirmedHash: string, opts?: { confirmNotSent?: boolean }): Promise<SendResult>;
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
  async loadFirstOutreach() {
    try {
      return (await apiService.getFirstOutreachStatus()).eligible === true;
    } catch {
      return false;
    }
  },
  async revealEmail(candidateId, deckId, useFree) {
    try {
      const res = await apiService.revealRankerEmail(candidateId, deckId, WALKTHROUGH_SURFACE, { useFirstOutreach: useFree });
      return res.email ? { ok: true, free: res.free === true } : { ok: false, reason: "no_email" };
    } catch (e) {
      const msg = errorMessage(e);
      if (errorStatus(e) === 402) return { ok: false, reason: "insufficient_credits" };
      if (msg === "free_outreach_unavailable" || msg === "free_outreach_in_use") return { ok: false, reason: "free_unavailable" };
      return { ok: false, reason: msg === "candidate_not_found" ? "no_email" : "error" };
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
  async gmailStatus() {
    const res = await apiService.gmailStatus();
    return { connected: !!res.connected, canSend: !!res.connected && canSendWith(res.scopes) };
  },
  async connectGmail(returnTo) {
    const url = await apiService.startGmailOAuth(returnTo);
    window.location.assign(url);
    return "redirecting";
  },
  async reviewSend(contactId) {
    const res = await apiService.sendFirstEmail(contactId, { dryRun: true });
    return {
      recipient: res.recipient || "",
      subject: res.subject || "",
      hash: res.hash || "",
      blocking: res.blocking || [],
      warnings: res.warnings || [],
      alreadySent: !!res.alreadySent,
    };
  },
  async sendEmail(contactId, confirmedHash, opts) {
    try {
      const res = await apiService.sendFirstEmail(contactId, {
        confirmedHash,
        surface: WALKTHROUGH_SURFACE,
        confirmNotSent: opts?.confirmNotSent,
      });
      return res.sent ? { ok: true, alreadySent: !!res.alreadySent } : { ok: false, reason: "send_failed" };
    } catch (e) {
      const msg = errorMessage(e) as SendError;
      const known: SendError[] = [
        "gmail_not_connected", "gmail_token_expired", "gmail_scope_missing", "draft_changed",
        "send_in_progress", "send_unconfirmed", "blocked", "daily_cap_reached",
      ];
      return { ok: false, reason: known.includes(msg) ? msg : "send_failed" };
    }
  },
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
  let gmailConnected = false;
  let freeUsed = false;
  const sent = new Set<string>();
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
    loadFirstOutreach: async () => !freeUsed,
    // The second sample person shows the "no email" path (which keeps the free outreach).
    revealEmail: async (id, _deck, useFree) => {
      await delay();
      if (id === "preview-1") return { ok: false, reason: "no_email" };
      if (useFree) freeUsed = true;
      return { ok: true, free: useFree };
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
    gmailStatus: async () => ({ connected: gmailConnected, canSend: gmailConnected }),
    connectGmail: async () => {
      await delay();
      gmailConnected = true;
      return "connected";
    },
    reviewSend: async (contactId) => {
      const d = drafts.get(contactId);
      const words = (d?.body || "").trim().split(/\s+/).filter(Boolean).length;
      return {
        recipient: d?.to || "",
        subject: d?.subject || "",
        hash: `preview-${d?.subject.length}-${d?.body.length}`,
        blocking: /\[[A-Za-z ]+\]|\{\{/.test(d?.body || "") ? ["placeholder"] : [],
        warnings: words < 60 ? ["too_short"] : [],
        alreadySent: sent.has(contactId),
      };
    },
    sendEmail: async (contactId) => {
      await delay();
      const already = sent.has(contactId);
      sent.add(contactId);
      return { ok: true, alreadySent: already };
    },
  };
}
