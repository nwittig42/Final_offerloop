import { beforeEach, describe, expect, it, vi } from "vitest";

const firestore = vi.hoisted(() => ({
  doc: vi.fn((_db: unknown, ...path: string[]) => ({ path: path.join("/") })),
  getDoc: vi.fn(),
  updateDoc: vi.fn(async () => {}),
}));
vi.mock("firebase/firestore", () => firestore);
vi.mock("@/lib/firebase", () => ({ db: {}, auth: { currentUser: null } }));

import type { Person } from "@/components/find/people/peopleModel";
import {
  WALKTHROUGH_VERSION,
  resumeWalkthrough,
  loadDraft,
  matchReasons,
  newWalkthrough,
  normalizeWalkthrough,
  pickMatches,
  saveDraft,
  saveWalkthrough,
  shouldAutoOpen,
  walkthroughIncomplete,
  type WalkthroughMatch,
} from "./walkthrough";

const person = (id: string, company: string, extra: Partial<Person> = {}): Person => ({
  id, source: "recommended", name: `Person ${id}`, firstName: `P${id}`, lastName: "X",
  title: "Analyst", company, location: "", school: "", linkedinUrl: "", photoUrl: "",
  email: "", reasons: [], briefing: "", matchScore: null, rank: Number(id), deckId: "d1", ...extra,
});

describe("pickMatches", () => {
  it("takes up to four people from different companies, in ranker order", () => {
    const out = pickMatches([
      person("1", "Goldman Sachs"), person("2", "goldman sachs"), person("3", "Evercore"),
      person("4", "Lazard"), person("5", "Evercore"), person("6", "Bain"), person("7", "McKinsey"),
    ]);
    expect(out.map((m) => m.id)).toEqual(["1", "3", "4", "6"]);
  });

  it("repeats a company only to reach three", () => {
    const out = pickMatches([person("1", "Goldman Sachs"), person("2", "Goldman Sachs"), person("3", "Goldman Sachs"), person("4", "Goldman Sachs")]);
    expect(out.map((m) => m.id)).toEqual(["1", "2", "3"]);
    expect(pickMatches([person("1", "A")]).map((m) => m.id)).toEqual(["1"]);
    expect(pickMatches([])).toEqual([]);
  });

  it("skips excluded people", () => {
    expect(pickMatches([person("1", "A"), person("2", "B")], ["1"]).map((m) => m.id)).toEqual(["2"]);
  });
});

describe("matchReasons", () => {
  const m = (extra: Partial<WalkthroughMatch>): WalkthroughMatch => ({
    ...pickMatches([person("1", "Goldman Sachs")])[0], ...extra,
  });
  const profile = { university: "University of Michigan", targetCompanies: ["Goldman Sachs"], roles: ["Investment Banking Analyst"] };

  it("leads with what we can see: same school, target company, target role", () => {
    expect(
      matchReasons(m({ school: "University of Michigan - Ann Arbor", title: "Investment Banking Associate", reasons: ["Started as an intern"] }), profile),
    ).toEqual([
      "Went to University of Michigan - Ann Arbor, like you",
      "Works at Goldman Sachs, one of your target companies",
      "Works close to the Investment Banking Analyst roles you're aiming for",
    ]);
  });

  it("falls back to the ranker's reasons, and is never empty", () => {
    expect(matchReasons(m({ company: "Acme", reasons: ["Same hometown"] }), profile)).toEqual(["Same hometown"]);
    expect(matchReasons(m({ company: "Acme", title: "Analyst" }), null)).toEqual(["Analyst at Acme"]);
  });

  it("skips ranker reasons that repeat what's already said", () => {
    expect(matchReasons(m({ school: "University of Michigan", reasons: ["Same school", "Started as an intern"] }), profile)).toEqual([
      "Went to University of Michigan, like you",
      "Works at Goldman Sachs, one of your target companies",
      "Started as an intern",
    ]);
  });

  it("doesn't count generic title words as a role match", () => {
    expect(matchReasons(m({ company: "Acme", title: "Marketing Analyst" }), profile)).toEqual(["Marketing Analyst at Acme"]);
  });
});

describe("normalizeWalkthrough", () => {
  const v = WALKTHROUGH_VERSION;
  const matches = pickMatches([person("1", "A"), person("2", "B")]);

  it("starts fresh from onboarding's pending marker or nothing", () => {
    expect(normalizeWalkthrough(null)).toEqual({ version: v, status: "pending", step: "matches", tourStep: "pick" });
    expect(normalizeWalkthrough({ status: "pending", createdAt: "t" })).toMatchObject({ status: "pending", step: "matches", createdAt: "t" });
  });

  it("resumes the saved step with its person", () => {
    const s = normalizeWalkthrough({ version: v, status: "drafted", step: "draft", matches, selectedId: "2", contactId: "ranker_2" });
    expect(s).toMatchObject({ step: "draft", selectedId: "2", contactId: "ranker_2", status: "drafted" });
  });

  it("goes back to the matches when the selected person is gone", () => {
    expect(normalizeWalkthrough({ version: v, status: "in_progress", step: "person", matches, selectedId: "9" })).toMatchObject({
      step: "matches", selectedId: undefined,
    });
  });

  it("keeps status and a dismissed reminder across an unknown shape", () => {
    expect(normalizeWalkthrough({ version: 0, status: "drafted", step: "weird", reminderDismissedAt: "t" })).toEqual({
      version: v, status: "drafted", step: "matches", tourStep: "pick", reminderDismissedAt: "t", createdAt: undefined,
    });
    expect(normalizeWalkthrough({ status: "bogus" }).status).toBe("pending");
  });

  it("is incomplete until sent", () => {
    expect(walkthroughIncomplete(normalizeWalkthrough({ status: "drafted" }))).toBe(true);
    expect(walkthroughIncomplete(normalizeWalkthrough({ status: "completed" }))).toBe(false);
    expect(walkthroughIncomplete(null)).toBe(false);
  });
});

describe("persistence", () => {
  beforeEach(() => vi.clearAllMocks());

  it("writes only the walkthrough field on the user's own doc", async () => {
    await saveWalkthrough("u1", { version: 1, status: "in_progress", step: "matches", selectedId: undefined });
    expect(firestore.doc).toHaveBeenCalledWith({}, "users", "u1");
    const [, payload] = firestore.updateDoc.mock.calls[0] as unknown as [unknown, Record<string, any>];
    expect(Object.keys(payload)).toEqual(["walkthrough"]);
    expect("selectedId" in payload.walkthrough).toBe(false);
    expect(payload.walkthrough.updatedAt).toEqual(expect.any(String));
  });

  it("saves draft edits onto the same contact doc, touching only the email fields", async () => {
    await saveDraft("u1", "ranker_c1", { subject: "Hi", body: "Hello" });
    expect(firestore.doc).toHaveBeenCalledWith({}, "users", "u1", "contacts", "ranker_c1");
    const [, payload] = firestore.updateDoc.mock.calls[0] as unknown as [unknown, Record<string, any>];
    expect(Object.keys(payload).sort()).toEqual(["emailBody", "emailEditedAt", "emailSubject"]);
  });

  it("reads the draft back, and treats an empty body as no draft", async () => {
    firestore.getDoc.mockResolvedValueOnce({ exists: () => true, data: () => ({ emailSubject: "Hi", emailBody: "Hello", draftToEmail: "pat@x.com" }) });
    expect(await loadDraft("u1", "ranker_c1")).toEqual({ to: "pat@x.com", subject: "Hi", body: "Hello" });
    firestore.getDoc.mockResolvedValueOnce({ exists: () => true, data: () => ({ emailSubject: "", emailBody: "" }) });
    expect(await loadDraft("u1", "ranker_c1")).toBeNull();
  });
});

describe("shouldAutoOpen / newWalkthrough", () => {
  const t = "2026-10-25T00:00:00Z";

  it("builds the pending record onboarding writes", () => {
    expect(newWalkthrough(t)).toEqual({ version: WALKTHROUGH_VERSION, status: "pending", step: "matches", tourStep: "pick", createdAt: t });
  });

  it("opens for a fresh walkthrough", () => {
    expect(shouldAutoOpen(newWalkthrough(t))).toBe(true);
  });

  it("stays closed once closed, skipped, completed, or missing", () => {
    expect(shouldAutoOpen({ ...newWalkthrough(t), closedAt: t })).toBe(false);
    expect(shouldAutoOpen({ ...newWalkthrough(t), skippedAt: t })).toBe(false);
    expect(shouldAutoOpen({ ...newWalkthrough(t), status: "completed" })).toBe(false);
    expect(shouldAutoOpen(null)).toBe(false);
    expect(shouldAutoOpen(undefined)).toBe(false);
  });

  it("resets a v1 state to the first tour step, keeping closedAt, skippedAt, reminder and createdAt", () => {
    expect(normalizeWalkthrough({ version: 1, status: "in_progress", step: "matches", closedAt: "c", skippedAt: "s", reminderDismissedAt: "r", createdAt: "k" })).toEqual({
      version: WALKTHROUGH_VERSION, status: "in_progress", step: "matches", tourStep: "pick",
      closedAt: "c", skippedAt: "s", reminderDismissedAt: "r", createdAt: "k",
    });
  });

  it("keeps a saved v2 tour step and falls back to pick for an unknown one", () => {
    expect(normalizeWalkthrough({ version: WALKTHROUGH_VERSION, status: "in_progress", step: "matches", tourStep: "review", tourContactId: "ranker_1" })).toMatchObject({
      tourStep: "review", tourContactId: "ranker_1",
    });
    expect(normalizeWalkthrough({ version: WALKTHROUGH_VERSION, status: "in_progress", step: "matches", tourStep: "nope" }).tourStep).toBe("pick");
  });

  it("resumeWalkthrough clears closedAt and skippedAt", () => {
    const s = resumeWalkthrough({ ...newWalkthrough("t"), closedAt: "c", skippedAt: "s", tourStep: "gmail" });
    expect(s.closedAt).toBeUndefined();
    expect(s.skippedAt).toBeUndefined();
    expect(s.tourStep).toBe("gmail");
  });

  it("keeps closedAt and skippedAt through the reset branch", () => {
    expect(normalizeWalkthrough({ version: 0, status: "pending", step: "bogus", closedAt: "x", skippedAt: "y" })).toMatchObject({
      closedAt: "x", skippedAt: "y",
    });
  });
});
