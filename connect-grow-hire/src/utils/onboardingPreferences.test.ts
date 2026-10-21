import { describe, expect, it } from "vitest";
import type { ProfileDraft } from "@/services/onboardingProgress";
import {
  ONBOARDING_INFERRED,
  applySavedPreferences,
  keepUserEdits,
  savedCareerTrack,
  savedPreferences,
} from "./onboardingPreferences";

const inferred: ProfileDraft = {
  firstName: "Ana", lastName: "Lee", phone: "",
  university: "University of Michigan", major: "Economics", graduationYear: "2027",
  roles: ["Investment Banking Analyst"], industries: ["Finance"],
  targetCompanies: ["Goldman Sachs"], interests: ["Club tennis"], notInSchool: false,
};

describe("savedPreferences", () => {
  it("is empty for a brand-new user doc", () => {
    expect(savedPreferences(null)).toEqual({});
    expect(savedPreferences({ email: "a@b.edu", needsOnboarding: true, credits: 300 })).toEqual({});
  });

  it("reads what the user set in Settings, the app or Find", () => {
    expect(
      savedPreferences({
        academics: { university: "UCLA", major: "Math", graduationYear: "2026" },
        targetRoles: ["Strategy Analyst"],
        targetIndustries: ["Consulting"],
        dreamCompanies: ["Bain & Company"],
        profile: { interests: ["Chess"] },
        universityOptOut: true,
      }),
    ).toEqual({
      university: "UCLA", major: "Math", graduationYear: "2026",
      roles: ["Strategy Analyst"], industries: ["Consulting"],
      targetCompanies: ["Bain & Company"], interests: ["Chess"], notInSchool: true,
    });
  });

  it("skips values an earlier onboarding run inferred and the user never touched", () => {
    expect(
      savedPreferences({
        goals: { dreamCompanies: ["Goldman Sachs"], dreamCompaniesSource: ONBOARDING_INFERRED },
        targetRoles: ["Investment Banking Analyst"],
        fieldProvenance: { targetRoles: ONBOARDING_INFERRED },
      }),
    ).toEqual({});
  });

  it("prefers a Settings-saved list over an inferred goals list", () => {
    expect(
      savedPreferences({
        goals: { dreamCompanies: ["Goldman Sachs"], dreamCompaniesSource: ONBOARDING_INFERRED },
        dreamCompanies: ["Bain & Company"],
      }).targetCompanies,
    ).toEqual(["Bain & Company"]);
  });
});

describe("applySavedPreferences / keepUserEdits", () => {
  it("saved preferences beat inference and are marked as the user's own", () => {
    const out = applySavedPreferences(inferred, { targetCompanies: ["Bain & Company"] });
    expect(out.targetCompanies).toEqual(["Bain & Company"]);
    expect(out.roles).toEqual(inferred.roles);
    expect(out.edited).toEqual(["targetCompanies"]);
  });

  it("a fresh inference keeps every field the user edited", () => {
    const prev: ProfileDraft = { ...inferred, major: "Statistics", targetCompanies: [], edited: ["major", "targetCompanies"] };
    const next: ProfileDraft = { ...inferred, major: "Business", targetCompanies: ["McKinsey & Company"], roles: ["Consultant"] };
    const out = keepUserEdits(prev, next);
    expect(out.major).toBe("Statistics");
    // Removing every company is an edit too.
    expect(out.targetCompanies).toEqual([]);
    // Untouched fields take the new inference.
    expect(out.roles).toEqual(["Consultant"]);
    expect(out.edited).toEqual(expect.arrayContaining(["major", "targetCompanies"]));
  });

  it("returns the new inference as-is when nothing was edited", () => {
    expect(keepUserEdits(null, inferred)).toBe(inferred);
    expect(keepUserEdits({ ...inferred, edited: [] }, inferred)).toBe(inferred);
  });
});

describe("savedCareerTrack", () => {
  it("keeps the user's own track, ignores one onboarding inferred", () => {
    expect(savedCareerTrack({ goals: { careerTrack: "Venture Capital" } })).toBe("Venture Capital");
    expect(savedCareerTrack({ careerTrack: "Marketing" })).toBe("Marketing");
    expect(savedCareerTrack({ careerTrack: "Marketing", fieldProvenance: { careerTrack: ONBOARDING_INFERRED } })).toBe("");
    expect(savedCareerTrack(null)).toBe("");
  });
});
