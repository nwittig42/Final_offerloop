import { describe, expect, it, vi } from "vitest";

import { advanceToReview, hasSavedDraft, runTourContact, staleDraftStep, type TourContactDeps } from "./findTourContact";

const person = { id: "p1", deckId: "d1", firstName: "Pat", name: "Pat Kim" };
const err = (message: string) => Object.assign(new Error(message), { status: 409 });

const makeDeps = (over: Partial<TourContactDeps> = {}) => {
  const calls: string[] = [];
  const deps: TourContactDeps = {
    goTo: vi.fn(async (step: string) => { calls.push(`goTo:${step}`); }),
    reveal: vi.fn(async () => ({ email: "pat@x.com" })),
    draft: vi.fn(async () => { calls.push("draft"); }),
    navigate: vi.fn((to: string) => { calls.push(`navigate:${to}`); }),
    ...over,
  };
  return { deps, calls };
};

describe("runTourContact", () => {
  it("success: draft step with the person, free reveal, in-app draft, review, then Contacts", async () => {
    const { deps, calls } = makeDeps();
    expect(await runTourContact(person, deps)).toEqual({ kind: "drafted" });
    expect(deps.goTo).toHaveBeenNthCalledWith(1, "draft", { tourContactId: "ranker_p1", tourPersonFirstName: "Pat" });
    expect(deps.reveal).toHaveBeenCalledWith("p1", "d1", true);
    expect(deps.draft).toHaveBeenCalledWith("p1", "d1");
    expect(deps.goTo).toHaveBeenNthCalledWith(2, "review", { tourContactId: "ranker_p1", tourPersonFirstName: "Pat" });
    expect(calls).toEqual(["goTo:draft", "draft", "goTo:review", "navigate:/contacts?contact=ranker_p1"]);
  });

  it("always asks for the free outreach first; the backend decides", async () => {
    const { deps } = makeDeps();
    await runTourContact(person, deps);
    expect(deps.reveal).toHaveBeenCalledTimes(1);
    expect(deps.reveal).toHaveBeenCalledWith("p1", "d1", true);
  });

  it("free outreach unavailable: no second (paid) reveal, back to pick asking to pay", async () => {
    const reveal = vi.fn(async () => { throw err("free_outreach_unavailable"); });
    const { deps } = makeDeps({ reveal });
    expect(await runTourContact(person, deps)).toEqual({ kind: "needs_payment", firstName: "Pat" });
    expect(reveal).toHaveBeenCalledTimes(1);
    expect(deps.draft).not.toHaveBeenCalled();
    expect(deps.goTo).toHaveBeenLastCalledWith("pick");
    expect(deps.navigate).not.toHaveBeenCalled();
  });

  it("paid (after the user chose Use 8 credits): one paid reveal, then the same draft flow", async () => {
    const { deps, calls } = makeDeps();
    expect(await runTourContact(person, deps, { paid: true })).toEqual({ kind: "drafted" });
    expect(deps.reveal).toHaveBeenCalledTimes(1);
    expect(deps.reveal).toHaveBeenCalledWith("p1", "d1", false);
    expect(calls).toEqual(["goTo:draft", "draft", "goTo:review", "navigate:/contacts?contact=ranker_p1"]);
  });

  it("free outreach in use: no paid call, back to pick", async () => {
    const reveal = vi.fn(async () => { throw err("free_outreach_in_use"); });
    const { deps } = makeDeps({ reveal });
    expect(await runTourContact(person, deps)).toEqual({ kind: "free_busy" });
    expect(reveal).toHaveBeenCalledTimes(1);
    expect(deps.draft).not.toHaveBeenCalled();
    expect(deps.goTo).toHaveBeenLastCalledWith("pick");
    expect(deps.navigate).not.toHaveBeenCalled();
  });

  it("no email, or an unknown candidate: back to pick with their name", async () => {
    for (const reveal of [
      vi.fn(async () => ({ email: null })),
      vi.fn(async () => { throw err("candidate_not_found"); }),
    ]) {
      const { deps } = makeDeps({ reveal });
      expect(await runTourContact(person, deps)).toEqual({ kind: "no_email", firstName: "Pat" });
      expect(deps.draft).not.toHaveBeenCalled();
      expect(deps.goTo).toHaveBeenLastCalledWith("pick");
    }
  });

  it("any other failure: back to pick, and the error goes to the caller", async () => {
    const { deps } = makeDeps({ draft: vi.fn(async () => { throw err("generation_failed"); }) });
    await expect(runTourContact(person, deps)).rejects.toThrow("generation_failed");
    expect(deps.goTo).toHaveBeenLastCalledWith("pick");
    expect(deps.navigate).not.toHaveBeenCalled();
  });
});

describe("advanceToReview", () => {
  it("records the person, moves to review, then opens them in Contacts", async () => {
    const { deps, calls } = makeDeps();
    await advanceToReview(person, deps);
    expect(deps.goTo).toHaveBeenCalledWith("review", { tourContactId: "ranker_p1", tourPersonFirstName: "Pat" });
    expect(calls).toEqual(["goTo:review", "navigate:/contacts?contact=ranker_p1"]);
  });
});

describe("staleDraftStep (a reload on the draft step)", () => {
  it("the draft was saved: review", () => {
    expect(staleDraftStep({ step: "draft", drafting: false, contactId: "ranker_p1", savedDraft: true })).toBe("review");
  });
  it("no saved draft, or nobody recorded: pick", () => {
    expect(staleDraftStep({ step: "draft", drafting: false, contactId: "ranker_p1", savedDraft: false })).toBe("pick");
    expect(staleDraftStep({ step: "draft", drafting: false, savedDraft: false })).toBe("pick");
  });
  it("nothing to decide while drafting here, or on another step", () => {
    expect(staleDraftStep({ step: "draft", drafting: true, contactId: "ranker_p1", savedDraft: true })).toBeNull();
    expect(staleDraftStep({ step: "pick", drafting: false, contactId: "ranker_p1", savedDraft: true })).toBeNull();
  });
});

describe("hasSavedDraft", () => {
  it("reads the contact doc's draft fields", () => {
    expect(hasSavedDraft(null)).toBe(false);
    expect(hasSavedDraft({})).toBe(false);
    expect(hasSavedDraft({ emailBody: "Hi" })).toBe(true);
    expect(hasSavedDraft({ draftCreatedAt: "t" })).toBe(true);
    expect(hasSavedDraft({ gmailDraftId: "g" })).toBe(true);
  });
});
