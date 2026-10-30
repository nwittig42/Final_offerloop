import { describe, expect, it } from "vitest";
import {
  MAX_COMPANIES,
  buildDirectionNarrative,
  careerTrackFor,
  inferFromResume,
  mergeDirection,
  rankLanes,
  type ParsedResume,
} from "./onboardingInference";

const bankingResume: ParsedResume = {
  objective: "Seeking a summer analyst role in investment banking.",
  education: { university: "University of Michigan", major: "Economics", graduation: "May 2027" },
  experience: [
    { title: "Summer Analyst", company: "Evercore" },
    { title: "Peer Tutor", company: "UMich Learning Center" },
  ],
  extracurriculars: [
    { activity: "Finance club", organization: "Michigan Investment Group", role: "Analyst" },
    { activity: "Club tennis" },
  ],
};

describe("inferFromResume", () => {
  it("reads school, major and a 4-digit grad year from the parse", () => {
    const p = inferFromResume(bankingResume);
    expect(p.university).toBe("University of Michigan");
    expect(p.major).toBe("Economics");
    expect(p.graduationYear).toBe("2027");
  });

  it("infers the finance lane from titles, objective and a lane employer", () => {
    const p = inferFromResume(bankingResume);
    expect(p.industries[0]).toBe("Finance");
    expect(p.roles).toContain("Investment Banking Analyst");
    expect(p.targetCompanies).toContain("Goldman Sachs");
    expect(p.targetCompanies.length).toBeLessThanOrEqual(MAX_COMPANIES);
  });

  it("takes interests from activities, preferring the organization name", () => {
    expect(inferFromResume(bankingResume).interests).toEqual(["Michigan Investment Group", "Club tennis"]);
  });

  it("returns an empty profile instead of throwing on a missing or empty parse", () => {
    for (const input of [null, undefined, {}, { education: null, experience: null }]) {
      expect(inferFromResume(input as ParsedResume)).toEqual({
        university: "",
        major: "",
        graduationYear: "",
        roles: [],
        industries: [],
        targetCompanies: [],
        interests: [],
      });
    }
  });

  it("mixes companies across lanes instead of letting one lane take every slot", () => {
    const p = inferFromResume({
      education: { major: "Computer Science" },
      experience: [{ title: "Strategy Consulting Intern", company: "Acme" }],
    });
    expect(p.industries).toEqual(expect.arrayContaining(["Technology", "Consulting"]));
    expect(p.targetCompanies.slice(0, 2).sort()).toEqual(["Google", "McKinsey & Company"].sort());
  });

  it("ranks job titles above a weaker activity match", () => {
    const lanes = rankLanes({
      experience: [{ title: "Software Engineering Intern", company: "Startup" }],
      extracurriculars: [{ activity: "Marketing club" }],
    });
    expect(lanes[0].industry).toBe("Technology");
  });
});

describe("mergeDirection", () => {
  const base = inferFromResume(bankingResume);

  it("returns the rules-based result unchanged when the LLM gave nothing", () => {
    expect(mergeDirection(base, null)).toBe(base);
    expect(mergeDirection(base, { industries: [], roles: [] })).toBe(base);
  });

  it("puts the LLM's more specific industries and roles first and re-derives companies", () => {
    const merged = mergeDirection(base, {
      industries: ["Consulting (Strategy)"],
      roles: ["Strategy Analyst"],
    });
    expect(merged.industries[0]).toBe("Consulting (Strategy)");
    expect(merged.roles[0]).toBe("Strategy Analyst");
    expect(merged.targetCompanies).toContain("McKinsey & Company");
    // School and grad year are never touched by the LLM step.
    expect(merged.university).toBe(base.university);
  });
});

describe("careerTrackFor", () => {
  it("maps roles before industries to a Settings career track", () => {
    expect(careerTrackFor({ roles: ["Investment Banking Analyst"], industries: ["Finance"] })).toBe("Investment Banking");
    expect(careerTrackFor({ roles: [], industries: ["Consulting"] })).toBe("Management Consulting");
    expect(careerTrackFor({ roles: ["Software Engineer"], industries: [] })).toBe("Tech / Software Engineering");
  });

  it("is empty when nothing maps", () => {
    expect(careerTrackFor({ roles: [], industries: [] })).toBe("");
    expect(careerTrackFor({ roles: ["Chef"], industries: ["Hospitality"] })).toBe("");
  });
});

describe("buildDirectionNarrative", () => {
  it("summarizes school, jobs and activities in the first person", () => {
    const text = buildDirectionNarrative(bankingResume);
    expect(text).toContain("I'm studying Economics at University of Michigan.");
    expect(text).toContain("Summer Analyst at Evercore");
    expect(text).toContain("Michigan Investment Group");
  });

  it("is empty when there's nothing to say (the endpoint rejects short text)", () => {
    expect(buildDirectionNarrative(null)).toBe("");
    expect(buildDirectionNarrative({})).toBe("");
  });
});
