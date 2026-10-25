import { describe, expect, it } from "vitest";

import { findTourView } from "./findTourSteps";

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
