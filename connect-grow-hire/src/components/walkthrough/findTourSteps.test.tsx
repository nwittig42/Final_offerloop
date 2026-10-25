import { describe, expect, it } from "vitest";

import { findTourPrimaryTitle, findTourView } from "./findTourSteps";

describe("findTourView", () => {
  it("pick: spotlights the first cards", () => {
    expect(findTourView("pick", { profileOpen: false })).toEqual({
      target: '[data-tour="tour-first-cards"]',
      title: "Your first email",
      body: "Here are a few people worth emailing first. Click one to see why they fit.",
    });
  });

  it("pick after a miss: says so, still on the cards", () => {
    expect(findTourView("pick", { profileOpen: false, noEmailFor: "Pat" })).toEqual({
      target: '[data-tour="tour-first-cards"]',
      title: "Your first email",
      body: "No email for Pat. Nothing was charged. Pick someone else.",
    });
  });

  it("pick with the profile open: spotlights its primary button", () => {
    expect(findTourView("pick", { profileOpen: true })).toEqual({
      target: '[data-tour="tour-profile-primary"]',
      title: "Write it for me",
      body: "Hit this and I'll find their email and write a first draft for you.",
    });
  });

  it("pick with the profile open after the free email was refused: asks before charging", () => {
    expect(findTourView("pick", { profileOpen: true, needsPayment: true })).toEqual({
      target: '[data-tour="tour-profile-primary"]',
      title: "Write it for me",
      body: "Your free first email is used up. This one costs 8 credits, refunded if no email is found.",
      action: { label: "Use 8 credits" },
    });
  });

  it("pick on an empty feed: nothing yet (the tour waits for cards)", () => {
    expect(findTourView("pick", { profileOpen: false, feedEmpty: true })).toBeNull();
    expect(findTourView("draft", { profileOpen: false, feedEmpty: true })?.title).toBe("Writing your email…");
  });

  it("draft: a centered bubble while it writes", () => {
    for (const profileOpen of [false, true]) {
      expect(findTourView("draft", { profileOpen })).toEqual({
        target: null,
        title: "Writing your email…",
        body: "Finding their email and drafting it. One moment.",
      });
    }
  });

  it("nothing on Find for the Contacts steps, or with no tour", () => {
    for (const step of ["review", "gmail", "open", null] as const) {
      expect(findTourView(step, { profileOpen: false })).toBeNull();
    }
  });
});

describe("findTourPrimaryTitle", () => {
  it("free while the tour picks a recommended person", () => {
    expect(findTourPrimaryTitle("pick", { recommended: true })).toBe("Free: your first email is on us");
  });
  it("the normal price title otherwise", () => {
    expect(findTourPrimaryTitle("pick", { recommended: true, needsPayment: true })).toBeUndefined();
    expect(findTourPrimaryTitle("pick", { recommended: false })).toBeUndefined();
    for (const step of ["draft", "review", null] as const) {
      expect(findTourPrimaryTitle(step, { recommended: true })).toBeUndefined();
    }
  });
});
