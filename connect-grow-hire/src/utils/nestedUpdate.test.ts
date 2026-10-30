import { describe, expect, it } from "vitest";
import { mergeNestedMaps } from "./nestedUpdate";

describe("mergeNestedMaps", () => {
  it("turns the named maps into dotted paths and leaves everything else alone", () => {
    expect(
      mergeNestedMaps(
        { goals: { careerTrack: "IB", dreamCompanies: ["Evercore"] }, onboardingProgress: null, targetRoles: ["Analyst"], walkthrough: { status: "pending" } },
        ["goals"],
      ),
    ).toEqual({
      "goals.careerTrack": "IB",
      "goals.dreamCompanies": ["Evercore"],
      onboardingProgress: null,
      targetRoles: ["Analyst"],
      walkthrough: { status: "pending" },
    });
  });

  it("ignores missing, null and array values", () => {
    expect(mergeNestedMaps({ profile: null, academics: ["x"] }, ["goals", "profile", "academics"])).toEqual({
      profile: null,
      academics: ["x"],
    });
  });
});
