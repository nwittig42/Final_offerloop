// Data layer for the Contacts page. Joins three backend sources that already
// exist into one row per saved contact:
//   - Firestore contacts (users/{uid}/contacts) — the person
//   - /outbox/threads — the conversation; OutboxThread.id IS the contact doc id
//   - /coffee-chat-prep/all — meeting preps, matched by LinkedIn URL because
//     preps don't store a contact id
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { firebaseApi, type Contact } from "@/services/firebaseApi";
import { apiService, type CoffeeChatPrep, type OutboxThread, type PipelineStage } from "@/services/api";

export const STAGES = ["saved", "drafted", "contacted", "opened", "connected", "interviewing", "offer"] as const;
export type Stage = (typeof STAGES)[number];

export const STAGE_LABELS: Record<Stage, string> = {
  saved: "Saved",
  drafted: "Drafted",
  contacted: "Contacted",
  opened: "Opened",
  connected: "Connected",
  interviewing: "Interviewing",
  offer: "Offer",
};

// One color per stage, used for the list pill, the card pill and the
// progress bar. Terminal stages (no response / bounced / closed) share grey.
export interface StageStyle {
  dot: string;   // solid color: dots, filled progress steps
  pill: string;  // tinted background + text for pills
}

export const STAGE_STYLES: Record<Stage, StageStyle> = {
  saved:        { dot: "#94A3B8", pill: "bg-slate-100 text-slate-600" },
  drafted:      { dot: "#F59E0B", pill: "bg-amber-50 text-amber-700" },
  contacted:    { dot: "#3B82F6", pill: "bg-blue-50 text-blue-700" },
  opened:       { dot: "#6366F1", pill: "bg-indigo-50 text-indigo-700" },
  connected:    { dot: "#8B5CF6", pill: "bg-violet-50 text-violet-700" },
  interviewing: { dot: "#6366F1", pill: "bg-indigo-50 text-indigo-700" },
  offer:        { dot: "#10B981", pill: "bg-emerald-50 text-emerald-700" },
};

export const TERMINAL_STYLE: StageStyle = { dot: "#9CA3AF", pill: "bg-gray-100 text-gray-500" };

export function stageStyle(stage: Stage | null): StageStyle {
  return stage ? STAGE_STYLES[stage] : TERMINAL_STYLE;
}

const TERMINAL_LABELS: Partial<Record<PipelineStage, string>> = {
  no_response: "No response",
  bounced: "Bounced",
  closed: "Closed",
};

// Backend's 11 pipeline stages collapse into the seven the UI shows. Terminal
// stages (no_response / bounced / closed) return null and are labelled
// separately. A won thread (resolution=meeting_booked) is always Offer.
export function threadStage(t: OutboxThread | undefined): Stage | null {
  if (!t) return "saved";
  if (t.resolution === "meeting_booked") return "offer";
  switch (t.pipelineStage) {
    case "draft_created":
    case "draft_deleted":
      return "drafted";
    case "email_sent":
    case "waiting_on_reply":
      return "contacted";
    case "email_opened":
      return "opened";
    case "replied":
    case "connected":
      return "connected";
    case "meeting_scheduled":
      return "interviewing";
    case "no_response":
    case "bounced":
    case "closed":
      return null;
    default:
      return "saved";
  }
}

// Inverse for writes. Offer is not a pipelineStage — callers route it through
// markOutboxThreadWon instead.
export function stageToBackend(stage: Stage): PipelineStage {
  switch (stage) {
    case "saved": return "new";
    case "drafted": return "draft_created";
    case "contacted": return "email_sent";
    case "opened": return "email_opened";
    case "connected": return "connected";
    case "interviewing":
    case "offer": return "meeting_scheduled";
  }
}

export function normalizeLinkedIn(url?: string | null): string {
  return (url || "")
    .trim()
    .toLowerCase()
    .replace(/^https?:\/\//, "")
    .replace(/^www\./, "")
    .replace(/[?#].*$/, "")
    .replace(/\/+$/, "");
}

// LinkedIn URLs from imports often arrive without a scheme; a schemeless href
// is treated as a relative path, so make it absolute before rendering.
export function linkedInHref(url?: string | null): string | null {
  const raw = (url || "").trim();
  if (!raw) return null;
  return /^https?:\/\//i.test(raw) ? raw : `https://${raw.replace(/^\/+/, "")}`;
}

export interface ContactRow {
  id: string;
  name: string;
  firstName: string;
  lastName: string;
  email: string;
  linkedinUrl: string;
  role: string;
  company: string;
  school: string;
  location: string;
  notes: string;
  createdAt: string;
  source: string;
  thread?: OutboxThread;
  stage: Stage | null;
  stageLabel: string;
  hasUnreadReply: boolean;
  archived: boolean;
  prep?: CoffeeChatPrep;
  // Set for people bookmarked from Search's recommendations. They have no
  // email until Contact runs the paid reveal + draft against this id.
  rankerCandidateId: string;
  rankerDeckId: string;
  // Bookmarked and never emailed (no draft, no thread): removable from Search.
  bookmarkedOnly: boolean;
  photoUrl: string;
}

// Contact doc id for a recommended person. The backend's ranker outreach
// writes to this same id, so bookmark and Contact never make two rows.
export function rankerContactId(candidateId: string): string {
  return `ranker_${candidateId}`;
}

export function toRow(c: Contact & Record<string, any>, thread: OutboxThread | undefined, prep: CoffeeChatPrep | undefined): ContactRow {
  const firstName = c.firstName || "";
  const lastName = c.lastName || "";
  const stage = threadStage(thread);
  return {
    id: c.id!,
    name: `${firstName} ${lastName}`.trim() || c.name || c.email || "Unknown",
    firstName,
    lastName,
    email: c.email || "",
    linkedinUrl: c.linkedinUrl || c.linkedin_url || "",
    role: c.jobTitle || c.title || "",
    company: c.company || "",
    school: c.college || "",
    location: c.location || "",
    notes: c.notes || "",
    createdAt: c.createdAt || c.firstContactDate || "",
    source: c.source || "",
    thread,
    stage,
    stageLabel: stage ? STAGE_LABELS[stage] : TERMINAL_LABELS[thread?.pipelineStage as PipelineStage] || "Closed",
    hasUnreadReply: !!thread?.hasUnreadReply,
    archived: !!thread?.archivedAt,
    prep,
    rankerCandidateId: c.rankerCandidateId || c.candidateId || (c.id?.startsWith("ranker_") ? c.id.slice(7) : ""),
    rankerDeckId: c.rankerDeckId || "",
    bookmarkedOnly: !c.emailBody && !c.gmailDraftId && !c.gmailThreadId && !thread?.emailSentAt && !thread?.draftCreatedAt,
    photoUrl: c.photoUrl || "",
  };
}

// Newest prep wins when a person was prepped more than once.
function latestPrepByLinkedIn(preps: CoffeeChatPrep[]): Map<string, CoffeeChatPrep> {
  const out = new Map<string, CoffeeChatPrep>();
  for (const p of preps) {
    const key = normalizeLinkedIn(p.linkedinUrl);
    if (!key) continue;
    const prev = out.get(key);
    if (!prev || Date.parse(p.createdAt || "") > Date.parse(prev.createdAt || "")) out.set(key, p);
  }
  return out;
}

export const CONTACTS_KEY = ["contacts"] as const;
// Prefixed with "trackerContacts" so every existing invalidate of that key
// (send, draft, stage change elsewhere in the app) also refreshes this page.
export const THREADS_KEY = ["trackerContacts", "withArchived"] as const;
export const PREPS_KEY = ["coffeeChatPreps"] as const;

export function useContactsData() {
  const { user } = useFirebaseAuth();
  const uid = user?.uid;

  const contactsQuery = useQuery({
    queryKey: [...CONTACTS_KEY, uid],
    queryFn: () => firebaseApi.getContacts(uid!),
    enabled: !!uid,
  });

  const threadsQuery = useQuery({
    queryKey: THREADS_KEY,
    queryFn: async () => {
      const res = await apiService.getOutboxThreads({ include_archived: true });
      if ("error" in res) throw new Error(res.error);
      return res.threads;
    },
    enabled: !!uid,
    refetchInterval: 30_000,
  });

  const prepsQuery = useQuery({
    queryKey: PREPS_KEY,
    queryFn: async () => {
      const res = await apiService.getAllCoffeeChatPreps();
      if ("error" in res) throw new Error(res.error);
      return res.preps;
    },
    enabled: !!uid,
  });

  const rows = useMemo<ContactRow[]>(() => {
    const threadsById = new Map((threadsQuery.data ?? []).map((t) => [t.id, t]));
    const prepsByLinkedIn = latestPrepByLinkedIn(prepsQuery.data ?? []);
    return (contactsQuery.data ?? [])
      .filter((c) => !!c.id)
      .map((c) => toRow(c, threadsById.get(c.id!), prepsByLinkedIn.get(normalizeLinkedIn(c.linkedinUrl))));
  }, [contactsQuery.data, threadsQuery.data, prepsQuery.data]);

  return {
    rows,
    preps: prepsQuery.data ?? [],
    isLoading: contactsQuery.isLoading,
    isError: contactsQuery.isError,
    gmailThreadsError: threadsQuery.isError,
  };
}

// RFC-4180 CSV download.
export function downloadCsv(filename: string, headers: string[], rows: Array<Array<string | number | undefined>>) {
  const esc = (v: string | number | undefined) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const csv = [headers, ...rows].map((r) => r.map(esc).join(",")).join("\n");
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8;" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export function formatDate(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}
