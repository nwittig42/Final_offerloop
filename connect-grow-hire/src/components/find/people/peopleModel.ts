// One card shape for every person on the Find page, whether they came from
// the ranker (recommended, no email yet) or from a typed search (prompt
// search in preview mode, email already found).
import type { Contact, DirectoryMember } from "@/services/api";

export type PersonSource = "recommended" | "search" | "sample" | "directory";

export interface Person {
  id: string;
  source: PersonSource;
  name: string;
  firstName: string;
  lastName: string;
  title: string;
  company: string;
  location: string;
  school: string;
  linkedinUrl: string;
  // LinkedIn profile photo from the people source; "" when there is none.
  photoUrl: string;
  email: string;
  reasons: string[];
  briefing: string;
  // 1.0-10.0 when the ranker sent a score, else null (never invented).
  matchScore: number | null;
  rank: number;
  deckId: string;
  // Org alumni directory badge, e.g. "USC TKE Alumni" (OFF-42).
  orgBadge?: string;
  // Real graduation year from an org directory; absent when it doesn't list one.
  gradYear?: number | null;
}

function str(v: unknown): string {
  return typeof v === "string" ? v.trim() : "";
}

// Ranker data is lowercase from the people source; title-case words for
// display without mangling short words like "of".
const SMALL = new Set(["of", "and", "the", "at", "in", "for", "&"]);
export function titleCase(v: string): string {
  if (!v || v !== v.toLowerCase()) return v;
  return v
    .split(/\s+/)
    .map((w, i) => (i > 0 && SMALL.has(w) ? w : w.charAt(0).toUpperCase() + w.slice(1)))
    .join(" ");
}

function matchScoreFrom(raw: Record<string, any>): number | null {
  const v = raw.match_score ?? raw.matchScore ?? raw.similarity ?? raw.score;
  if (typeof v !== "number" || !Number.isFinite(v) || v <= 0) return null;
  const ten = v <= 1 ? v * 10 : v <= 10 ? v : v <= 100 ? v / 10 : NaN;
  if (!Number.isFinite(ten)) return null;
  return Math.round(Math.min(10, Math.max(1, ten)) * 10) / 10;
}

// Live ranker nests the human under `person`; score, reasons and briefing
// are siblings of it. Reasons arrive as objects ({hook, detail, ...}) or
// strings. Returns null when there's no id or name to render.
export function personFromRanker(raw: Record<string, any>, index: number, deckId: string): Person | null {
  const p: Record<string, any> = raw.person && typeof raw.person === "object" ? raw.person : raw;
  const id = str(p.candidate_id) || str(p.id) || str(p.person_id) || str(raw.candidate_id);
  const firstName = titleCase(str(p.firstName) || str(p.first_name));
  const lastName = titleCase(str(p.lastName) || str(p.last_name));
  const name = titleCase(str(p.name)) || `${firstName} ${lastName}`.trim();
  if (!id || !name) return null;

  const reasonSource = Array.isArray(raw.reasons) ? raw.reasons : Array.isArray(p.reasons) ? p.reasons : [];
  const reasons = reasonSource
    .map((r: unknown) => {
      if (typeof r === "string") return r.trim();
      if (r && typeof r === "object") {
        const o = r as Record<string, unknown>;
        return str(o.label) || str(o.hook) || str(o.detail);
      }
      return "";
    })
    .filter(Boolean)
    .map((r: string) => r.charAt(0).toUpperCase() + r.slice(1));

  return {
    id,
    source: "recommended",
    name,
    firstName: firstName || name.split(" ")[0],
    lastName: lastName || name.split(" ").slice(1).join(" "),
    title: titleCase(str(p.title) || str(p.job_title)),
    company: titleCase(str(p.company) || str(p.company_name)),
    location: titleCase(str(p.location) || [str(p.city), str(p.state)].filter(Boolean).join(", ")),
    school: titleCase(str(p.college) || str(p.school) || str(p.university)),
    linkedinUrl: str(p.linkedinUrl) || str(p.linkedin_url) || str(p.linkedin),
    photoUrl: str(p.photo_url) || str(p.photoUrl) || str(p.picture),
    email: "",
    reasons,
    briefing: str(raw.briefing) || str(p.briefing),
    matchScore: matchScoreFrom(raw) ?? matchScoreFrom(p),
    rank: typeof raw.rank === "number" ? raw.rank : index,
    deckId,
  };
}

export function personFromSearch(c: Contact, index: number): Person {
  const firstName = str(c.FirstName);
  const lastName = str(c.LastName);
  const email = str(c.Email) || str(c.WorkEmail) || str(c.PersonalEmail);
  return {
    id: str(c.LinkedIn) || email || `search-${index}-${firstName}-${lastName}`,
    source: "search",
    name: `${firstName} ${lastName}`.trim() || "Unknown",
    firstName,
    lastName,
    title: str(c.Title),
    company: str(c.Company),
    location: [str(c.City), str(c.State)].filter(Boolean).join(", "),
    school: str(c.College),
    linkedinUrl: str(c.LinkedIn),
    photoUrl: str((c as Contact & { PhotoUrl?: string }).PhotoUrl),
    email,
    reasons: str(c.Similarity) ? [str(c.Similarity)] : [],
    briefing: str(c.WorkSummary),
    matchScore: null,
    rank: index,
    deckId: "",
    orgBadge: str(c.OrgBadge),
    gradYear: typeof c.OrgGradYear === "number" ? c.OrgGradYear : null,
  };
}

// Org directory row (OFF-42). `email` stays "" until the student unlocks it.
export function personFromDirectory(m: DirectoryMember, index: number, revealedEmail = ""): Person {
  const name = `${m.firstName} ${m.lastName}`.trim() || "Unknown";
  return {
    id: m.personKey,
    source: "directory",
    name,
    firstName: m.firstName,
    lastName: m.lastName,
    title: m.title,
    company: m.company,
    location: "",
    school: m.school,
    linkedinUrl: m.linkedinUrl,
    photoUrl: m.photoUrl,
    email: revealedEmail,
    // The raw directory headline is the best "where they are now" we have
    // when there's no clean company/title.
    reasons: !m.company && !m.title && m.headline ? [m.headline] : [],
    briefing: m.headline,
    matchScore: null,
    rank: index,
    deckId: "",
    orgBadge: m.badge,
    gradYear: m.gradYear,
  };
}

export function initials(name: string): string {
  const parts = name.trim().split(/\s+/);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || "?";
}

// Recruiter / hiring manager / employee, from the title. Display only.
export function relationFor(title: string): string {
  const t = title.toLowerCase();
  if (/recruit|talent|campus|university relations/.test(t)) return "Recruiter";
  if (/director|head of|vp|vice president|manager|partner|principal|lead/.test(t)) return "Hiring manager";
  return "Employee";
}
