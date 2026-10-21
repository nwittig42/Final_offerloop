/**
 * First-use walkthrough (onboarding v2, Milestone 2): a few matched people,
 * pick one, and get an editable first email.
 *
 * Progress lives on users/{uid}.walkthrough (written by onboarding as
 * { status: "pending" }). It's UX state only: nothing billing-related ever
 * trusts it, so it's fine that the user can write it. The draft itself is the
 * contact doc the outreach route writes (users/{uid}/contacts/ranker_<id>),
 * so Inbox shows the same edited email and Milestone 3 sends that version.
 */
import { doc, getDoc, updateDoc } from "firebase/firestore";
import { db } from "@/lib/firebase";
import type { Person } from "@/components/find/people/peopleModel";

export const WALKTHROUGH_VERSION = 1;

/**
 * pending: never opened. in_progress: started, no draft yet. drafted: an
 * email is written and editable. completed: sent (Milestone 3).
 */
export type WalkthroughStatus = "pending" | "in_progress" | "drafted" | "completed";
export type WalkthroughStep = "matches" | "person" | "draft";

/** The bits of a recommended person the walkthrough shows (snapshot, so a refresh shows the same people). */
export interface WalkthroughMatch {
  id: string;
  name: string;
  firstName: string;
  title: string;
  company: string;
  school: string;
  location: string;
  linkedinUrl: string;
  photoUrl: string;
  /** Why they're a match, best first. */
  reasons: string[];
  briefing: string;
  deckId: string;
  rank: number;
}

export interface WalkthroughState {
  version: number;
  status: WalkthroughStatus;
  step: WalkthroughStep;
  matches?: WalkthroughMatch[];
  selectedId?: string;
  /** users/{uid}/contacts doc holding the draft. */
  contactId?: string;
  /** People we couldn't find an email for (never charged). */
  noEmailIds?: string[];
  /** The Find reminder was closed. */
  reminderDismissedAt?: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface EmailDraft {
  to: string;
  subject: string;
  body: string;
}

/** Same as config.CREDIT_COSTS["reveal_email"] on the backend. */
export const REVEAL_COST = 8;
export const MAX_MATCHES = 4;
const MIN_MATCHES = 3;

/** The outreach route's deterministic contact doc id. */
export const contactIdFor = (candidateId: string) => `ranker_${candidateId}`;

const STEPS: WalkthroughStep[] = ["matches", "person", "draft"];

/** A saved state, normalized; anything unusable starts over at the matches. */
export function normalizeWalkthrough(raw: unknown): WalkthroughState {
  const fresh: WalkthroughState = { version: WALKTHROUGH_VERSION, status: "pending", step: "matches" };
  if (!raw || typeof raw !== "object") return fresh;
  const s = raw as Partial<WalkthroughState>;
  const status: WalkthroughStatus = ["pending", "in_progress", "drafted", "completed"].includes(s.status as string)
    ? (s.status as WalkthroughStatus)
    : "pending";
  const base: WalkthroughState = { ...fresh, ...s, status };
  // Older or partial shapes keep the status (and the Find reminder) but restart the steps.
  if (s.version !== WALKTHROUGH_VERSION || !STEPS.includes(s.step as WalkthroughStep)) {
    return { ...fresh, status, reminderDismissedAt: s.reminderDismissedAt, createdAt: s.createdAt };
  }
  const matches = Array.isArray(s.matches) ? s.matches : [];
  const selected = matches.find((m) => m.id === s.selectedId);
  // A person or draft step needs the person it was about.
  if ((base.step === "person" || base.step === "draft") && !selected) {
    return { ...base, step: "matches", selectedId: undefined };
  }
  return { ...base, matches };
}

/** Still something to finish (drives the Find reminder). */
export const walkthroughIncomplete = (s: WalkthroughState | null | undefined): boolean =>
  !!s && s.status !== "completed";

function toMatch(p: Person): WalkthroughMatch {
  return {
    id: p.id,
    name: p.name,
    firstName: p.firstName,
    title: p.title,
    company: p.company,
    school: p.school,
    location: p.location,
    linkedinUrl: p.linkedinUrl,
    photoUrl: p.photoUrl,
    reasons: p.reasons,
    briefing: p.briefing,
    deckId: p.deckId,
    rank: p.rank,
  };
}

/**
 * Up to four people, in the ranker's order, from different companies. Only
 * when that leaves fewer than three does a second person from a company
 * already shown fill in.
 */
export function pickMatches(people: Person[], exclude: string[] = [], max = MAX_MATCHES): WalkthroughMatch[] {
  const skip = new Set(exclude);
  const pool = people.filter((p) => p.id && !skip.has(p.id));
  const picked: Person[] = [];
  const companies = new Set<string>();
  for (const p of pool) {
    const key = (p.company || "").trim().toLowerCase();
    if (picked.length >= max) break;
    if (key && companies.has(key)) continue;
    if (key) companies.add(key);
    picked.push(p);
  }
  for (const p of pool) {
    if (picked.length >= Math.min(MIN_MATCHES, max)) break;
    if (!picked.includes(p)) picked.push(p);
  }
  return picked.map(toMatch);
}

export interface MatchProfile {
  university: string;
  targetCompanies: string[];
  roles: string[];
}

const GENERIC_TITLE_WORDS = new Set(["analyst", "associate", "intern", "manager", "senior", "junior", "lead"]);
const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
const sameSchool = (a: string, b: string) => {
  const x = norm(a);
  const y = norm(b);
  return !!x && !!y && (x === y || x.includes(y) || y.includes(x));
};

/**
 * "Why them", best first: what we can see directly (same school, a target
 * company, a target role), then the ranker's own reasons. At most three,
 * never empty.
 */
export function matchReasons(m: WalkthroughMatch, profile: MatchProfile | null): string[] {
  const out: string[] = [];
  if (profile) {
    if (m.school && sameSchool(m.school, profile.university)) out.push(`Went to ${m.school}, like you`);
    const company = norm(m.company);
    if (company && profile.targetCompanies.some((c) => norm(c) === company)) {
      out.push(`Works at ${m.company}, one of your target companies`);
    }
    // A target role sharing a distinctive word with their title ("Investment
    // Banking Analyst" ~ "Investment Banking Associate"). Generic words don't count.
    const title = norm(m.title);
    const role = profile.roles.find((r) =>
      norm(r).split(" ").some((w) => w.length > 3 && !GENERIC_TITLE_WORDS.has(w) && title.includes(w)),
    );
    if (role) out.push(`Works close to the ${role} roles you're aiming for`);
  }
  for (const r of m.reasons) {
    if (out.length >= 3) break;
    const k = norm(r);
    if (k && !out.some((o) => norm(o) === k || norm(o).includes(k))) out.push(r);
  }
  if (!out.length) {
    out.push(m.title && m.company ? `${m.title} at ${m.company}` : "Recommended from your background");
  }
  return out.slice(0, 3);
}

// ── Persistence ──────────────────────────────────────────────────────────────

export async function loadWalkthrough(uid: string): Promise<WalkthroughState | null> {
  const snap = await getDoc(doc(db, "users", uid));
  if (!snap.exists()) return null;
  const raw = (snap.data() as Record<string, unknown>).walkthrough;
  return raw ? normalizeWalkthrough(raw) : null;
}

/** Firestore rejects undefined field values; drop them before writing. */
const withoutUndefined = <T extends object>(v: T): T => JSON.parse(JSON.stringify(v)) as T;

export async function saveWalkthrough(uid: string, state: WalkthroughState): Promise<void> {
  await updateDoc(doc(db, "users", uid), {
    walkthrough: withoutUndefined({ ...state, version: WALKTHROUGH_VERSION, updatedAt: new Date().toISOString() }),
  });
}

/** The draft the outreach route saved (and any edits since). */
export async function loadDraft(uid: string, contactId: string): Promise<EmailDraft | null> {
  const snap = await getDoc(doc(db, "users", uid, "contacts", contactId));
  if (!snap.exists()) return null;
  const d = snap.data() as Record<string, unknown>;
  const body = typeof d.emailBody === "string" ? d.emailBody : "";
  if (!body) return null;
  return {
    to: (typeof d.draftToEmail === "string" && d.draftToEmail) || (typeof d.email === "string" ? d.email : ""),
    subject: typeof d.emailSubject === "string" ? d.emailSubject : "",
    body,
  };
}

/** Save edits onto the same contact doc, so Inbox and the send step see them. */
export async function saveDraft(uid: string, contactId: string, draft: Pick<EmailDraft, "subject" | "body">): Promise<void> {
  await updateDoc(doc(db, "users", uid, "contacts", contactId), {
    emailSubject: draft.subject,
    emailBody: draft.body,
    emailEditedAt: new Date().toISOString(),
  });
}
