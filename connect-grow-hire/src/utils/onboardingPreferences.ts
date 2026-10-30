/**
 * Keeps onboarding's automatic inference from silently replacing anything the
 * user chose themselves.
 *
 * Two sources count as the user's own:
 *   - values already saved on users/{uid} before onboarding (Settings, the
 *     mobile app, Find's school prompt), unless an earlier onboarding run
 *     wrote them as inferred and the user never touched them;
 *   - fields edited on the confirm screen (ProfileDraft.edited).
 *
 * Both are recorded in `edited`, so a later re-inference (a different resume,
 * a refresh mid-inference) keeps them. On completion, fieldProvenance records
 * which fields are explicit and which were accepted as inferred, for any
 * future automatic update to check before overwriting.
 */
import type { DraftField, ProfileDraft } from "@/services/onboardingProgress";

/** fieldProvenance value for a field onboarding inferred and the user accepted unedited. */
export const ONBOARDING_INFERRED = "onboarding_inferred";

type UserDoc = Record<string, any>;

const str = (v: unknown): string => (typeof v === "string" ? v.trim() : typeof v === "number" ? String(v) : "");
const list = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter((x): x is string => typeof x === "string" && !!x.trim()) : [];

/** Provenance keys, matching the backend's /api/users/profile-confirm. */
const PROVENANCE_KEY: Partial<Record<DraftField, string>> = {
  university: "school",
  major: "major",
  graduationYear: "graduationYear",
  targetCompanies: "targetCompanies",
  industries: "targetIndustries",
  roles: "targetRoles",
  interests: "interests",
};

export const provenanceKey = (field: DraftField): string | undefined => PROVENANCE_KEY[field];

/** Preferences already saved on the user doc that the user set themselves. */
export function savedPreferences(doc: UserDoc | null | undefined): Partial<ProfileDraft> {
  if (!doc) return {};
  const prov = (doc.fieldProvenance || {}) as Record<string, string>;
  const goals = doc.goals || {};
  const academics = doc.academics || {};
  const profile = doc.profile || {};
  const own = (field: DraftField) => {
    const key = PROVENANCE_KEY[field];
    return !key || prov[key] !== ONBOARDING_INFERRED;
  };

  const out: Partial<ProfileDraft> = {};
  if (doc.universityOptOut === true) out.notInSchool = true;
  const university = str(academics.university) || str(doc.university);
  if (university && own("university")) out.university = university;
  const major = str(academics.major) || str(doc.major);
  if (major && own("major")) out.major = major;
  const year = str(academics.graduationYear) || str(doc.graduationYear);
  if (/^(19|20)\d{2}$/.test(year) && own("graduationYear")) out.graduationYear = year;

  const roles = list(doc.targetRoles);
  if (roles.length && own("roles")) out.roles = roles;
  const industries = list(doc.targetIndustries);
  if (industries.length && own("industries")) out.industries = industries;
  // Settings saves the top-level list; an earlier onboarding run tags its own.
  const goalCompanies = goals.dreamCompaniesSource === ONBOARDING_INFERRED ? [] : list(goals.dreamCompanies);
  const companies = goalCompanies.length ? goalCompanies : list(doc.dreamCompanies);
  if (companies.length && own("targetCompanies")) out.targetCompanies = companies;
  const interests = list(profile.interests);
  if (interests.length && own("interests")) out.interests = interests;
  return out;
}

/** Saved preferences beat inference, and are marked as the user's own. */
export function applySavedPreferences(draft: ProfileDraft, saved: Partial<ProfileDraft>): ProfileDraft {
  const fields = Object.keys(saved) as DraftField[];
  if (!fields.length) return draft;
  return { ...draft, ...saved, edited: mergeEdited(draft.edited, fields) };
}

/**
 * A fresh inference, keeping every field the user set in the previous draft.
 * `prev` is the draft from before (null on a first inference).
 */
export function keepUserEdits(prev: ProfileDraft | null | undefined, next: ProfileDraft): ProfileDraft {
  if (!prev?.edited?.length) return next;
  const kept: Partial<Record<DraftField, unknown>> = {};
  for (const field of prev.edited) kept[field] = prev[field];
  return { ...next, ...(kept as Partial<ProfileDraft>), edited: mergeEdited(next.edited, prev.edited) };
}

export function mergeEdited(a: DraftField[] | undefined, b: DraftField[] | undefined): DraftField[] {
  return Array.from(new Set([...(a || []), ...(b || [])]));
}

/** The user's own career track (Settings, the app), if any. */
export function savedCareerTrack(doc: UserDoc | null | undefined): string {
  if (!doc) return "";
  if ((doc.fieldProvenance || {}).careerTrack === ONBOARDING_INFERRED) return "";
  return str((doc.goals || {}).careerTrack) || str(doc.careerTrack);
}
