/**
 * Onboarding profile inference.
 *
 * Turns the parsed resume (users/{uid}.resumeParsed, the same v2 shape the
 * resume upload, LinkedIn enrichment and the resume builder all save) into the
 * fields onboarding asks the user to confirm: school, major, grad year, roles,
 * industries, target companies and interests. There is no career-goals
 * questionnaire; everything here is inferred and then confirmed.
 *
 * Rules first, LLM optional: inferFromResume() is free and deterministic,
 * built on the same industry lanes the Find feed seeds from. The optional
 * /api/extract-direction result (mergeDirection) only refines roles and
 * industries. It never adds companies: that endpoint only returns firms the
 * text names, which for a resume are past employers, not targets.
 */
import { LANES, type Lane } from "@/components/find/people/feedSeeds";
import { extractGraduationYear } from "@/utils/onboardingPrefill";

/** The subset of resumeParsed (v2) that inference reads. Every field is optional. */
export interface ParsedResume {
  name?: string;
  objective?: string | null;
  education?: {
    university?: string | null;
    major?: string | null;
    minor?: string | null;
    degree?: string | null;
    graduation?: string | null;
    coursework?: string[] | null;
  } | null;
  experience?: Array<{ company?: string | null; title?: string | null }> | null;
  extracurriculars?: Array<{ activity?: string | null; organization?: string | null; role?: string | null }> | null;
  skills?: Record<string, unknown> | null;
}

export interface InferredProfile {
  university: string;
  major: string;
  graduationYear: string;
  roles: string[];
  industries: string[];
  targetCompanies: string[];
  interests: string[];
}

/** Result of POST /api/extract-direction (see routes/enrichment.py). */
export interface DirectionExtract {
  industries?: string[];
  roles?: string[];
}

export const MAX_ROLES = 5;
export const MAX_INDUSTRIES = 3;
export const MAX_COMPANIES = 8;
export const MAX_INTERESTS = 5;

const str = (v: unknown): string => (typeof v === "string" ? v.trim() : "");

const dedupe = (items: string[]): string[] => {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of items) {
    const item = raw.trim();
    const key = item.toLowerCase();
    if (item && !seen.has(key)) {
      seen.add(key);
      out.push(item);
    }
  }
  return out;
};

/**
 * Score each lane against the resume. Job titles say the most about where a
 * student is aimed, then the major and objective; employers and activities
 * are weaker hints. A past employer on a lane's firm list counts as a title.
 */
export function rankLanes(parsed: ParsedResume | null | undefined): Lane[] {
  const p = parsed || {};
  const edu = p.education || {};
  const weighted: Array<[string, number]> = [
    [str(edu.major), 2],
    [str(edu.minor), 1],
    [str(p.objective), 2],
    ...(p.experience || []).map((e) => [str(e?.title), 3] as [string, number]),
    ...(p.experience || []).map((e) => [str(e?.company), 1] as [string, number]),
    ...(p.extracurriculars || []).map(
      (x) => [[str(x?.activity), str(x?.organization), str(x?.role)].join(" "), 1] as [string, number],
    ),
  ];
  const employers = (p.experience || []).map((e) => str(e?.company).toLowerCase()).filter(Boolean);

  const scored = LANES.map((lane, order) => {
    let score = 0;
    for (const [text, weight] of weighted) if (text && lane.match.test(text)) score += weight;
    if (lane.firms.some((f) => employers.includes(f.toLowerCase()))) score += 3;
    return { lane, score, order };
  });
  return scored
    .filter((s) => s.score > 0)
    .sort((a, b) => b.score - a.score || a.order - b.order)
    .map((s) => s.lane);
}

/** Round-robin the lanes' firms so one lane doesn't take every slot. */
function companiesFromLanes(lanes: Lane[], max = MAX_COMPANIES): string[] {
  const out: string[] = [];
  const depth = Math.max(0, ...lanes.map((l) => l.firms.length));
  for (let i = 0; i < depth && out.length < max; i++) {
    for (const lane of lanes) {
      const firm = lane.firms[i];
      if (firm && !out.some((x) => x.toLowerCase() === firm.toLowerCase())) out.push(firm);
      if (out.length >= max) break;
    }
  }
  return out;
}

function interestsFrom(parsed: ParsedResume): string[] {
  const activities = (parsed.extracurriculars || []).map((x) => str(x?.organization) || str(x?.activity));
  return dedupe(activities).slice(0, MAX_INTERESTS);
}

/** Deterministic inference from the parsed resume. Never throws. */
export function inferFromResume(parsed: ParsedResume | null | undefined): InferredProfile {
  const p = parsed || {};
  const edu = p.education || {};
  const lanes = rankLanes(p).slice(0, MAX_INDUSTRIES);
  return {
    university: str(edu.university),
    major: str(edu.major),
    graduationYear: extractGraduationYear(str(edu.graduation)),
    roles: dedupe(lanes.flatMap((l) => l.roles)).slice(0, MAX_ROLES),
    industries: lanes.map((l) => l.industry),
    targetCompanies: companiesFromLanes(lanes),
    interests: interestsFrom(p),
  };
}

/**
 * Fold an /api/extract-direction result into the rules-based inference.
 * The LLM's industries and roles are more specific, so they lead; companies
 * are re-derived from lanes matching the combined industries and roles.
 */
export function mergeDirection(base: InferredProfile, extract: DirectionExtract | null | undefined): InferredProfile {
  if (!extract) return base;
  const llmIndustries = dedupe((extract.industries || []).filter((s) => typeof s === "string"));
  const llmRoles = dedupe((extract.roles || []).filter((s) => typeof s === "string"));
  if (!llmIndustries.length && !llmRoles.length) return base;

  const industries = dedupe([...llmIndustries, ...base.industries]).slice(0, MAX_INDUSTRIES);
  const roles = dedupe([...llmRoles, ...base.roles]).slice(0, MAX_ROLES);
  const signal = [...industries, ...roles].join(" | ");
  const lanes = LANES.filter((l) => l.match.test(signal));
  const fromLanes = companiesFromLanes(lanes);
  return {
    ...base,
    industries,
    roles,
    targetCompanies: dedupe([...fromLanes, ...base.targetCompanies]).slice(0, MAX_COMPANIES),
  };
}

// Industries/roles → one of Account Settings' careerTrackOptions. Order
// matters: the specific tracks are checked before the broad finance one.
const CAREER_TRACKS: Array<[RegExp, string]> = [
  [/invest(ment)? bank/i, "Investment Banking"],
  [/private equity/i, "Private Equity"],
  [/venture/i, "Venture Capital"],
  [/consult/i, "Management Consulting"],
  [/product manag/i, "Product Management"],
  [/data scien|analytics/i, "Data Science / Analytics"],
  [/software|engineer|tech|\bai\b|machine learning|developer/i, "Tech / Software Engineering"],
  [/market|brand|media|advertis/i, "Marketing"],
  [/account|audit|\btax\b/i, "Accounting"],
  [/financ|bank|trading|hedge fund|asset management|capital markets/i, "Finance / Corporate Finance"],
];

/**
 * The single careerTrack the rest of the app reads (Find's goals banner, the
 * ranker fingerprint, Settings). Empty when nothing maps; the user can set
 * it in Settings.
 */
export function careerTrackFor(profile: Pick<InferredProfile, "industries" | "roles">): string {
  const signals = [...profile.roles, ...profile.industries];
  for (const signal of signals) {
    for (const [re, track] of CAREER_TRACKS) if (re.test(signal)) return track;
  }
  return "";
}

/**
 * A short first-person summary of the resume for /api/extract-direction,
 * which interprets a career narrative. Empty when there's too little to say
 * (the endpoint rejects anything under 8 characters).
 */
export function buildDirectionNarrative(parsed: ParsedResume | null | undefined): string {
  const p = parsed || {};
  const edu = p.education || {};
  const parts: string[] = [];
  const major = str(edu.major);
  const school = str(edu.university);
  if (major || school) {
    parts.push(`I'm studying ${major || "at university"}${school ? ` at ${school}` : ""}.`);
  }
  const jobs = (p.experience || [])
    .map((e) => [str(e?.title), str(e?.company)].filter(Boolean).join(" at "))
    .filter(Boolean)
    .slice(0, 5);
  if (jobs.length) parts.push(`Experience: ${jobs.join("; ")}.`);
  const activities = interestsFrom(p);
  if (activities.length) parts.push(`Activities: ${activities.join(", ")}.`);
  const objective = str(p.objective);
  if (objective) parts.push(objective);
  const text = parts.join(" ").slice(0, 1500);
  return text.length >= 8 ? text : "";
}
