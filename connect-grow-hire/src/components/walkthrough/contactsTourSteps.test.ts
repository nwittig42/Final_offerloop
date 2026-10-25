import { describe, expect, it } from "vitest";

import { ALL_SET_VIEW, contactsTourView } from "./contactsTourSteps";

describe("contactsTourView", () => {
  it("review: spotlights the draft, never says edit it here", () => {
    const v = contactsTourView("review", { firstName: "Pat" });
    expect(v).toEqual({
      target: '[data-tour="tour-draft"]',
      title: "Your email to Pat",
      body: "Read it over. You can tweak it in Gmail before you send.",
      action: { label: "Looks good", disabled: false },
    });
    expect(v?.body).not.toMatch(/edit it here/i);
  });

  it("review: the button waits while it checks Gmail and pushes", () => {
    expect(contactsTourView("review", { firstName: "Pat", pushing: true })?.action).toEqual({ label: "Looks good", disabled: true });
  });

  it("gmail: centered, asks to connect, with Not now", () => {
    expect(contactsTourView("gmail", { firstName: "Pat" })).toEqual({
      target: null,
      title: "Connect Gmail",
      body: "Connect Gmail so this goes out from your own inbox. Google will ask you to tick a few boxes. Tick all of them.",
      action: { label: "Connect Gmail", disabled: false },
      secondary: { label: "Not now" },
    });
  });

  it("gmail with a permission error: explains and offers Try again", () => {
    expect(contactsTourView("gmail", { firstName: "Pat", gmailError: "scopes_declined" })).toEqual({
      target: null,
      title: "Connect Gmail",
      body: "Google didn't give us permission to create drafts. Connect again and tick every box.",
      action: { label: "Try again", disabled: false },
      secondary: { label: "Not now" },
    });
  });

  it("gmail while pushing: says so and disables the button", () => {
    const v = contactsTourView("gmail", { firstName: "Pat", pushing: true, gmailError: "scopes_declined" });
    expect(v?.body).toBe("Putting it in your Gmail drafts…");
    expect(v?.action?.disabled).toBe(true);
  });

  it("gmail after a push failure: Try again", () => {
    const v = contactsTourView("gmail", { firstName: "Pat", pushError: "error" });
    expect(v?.body).toBe("Something went wrong putting it in your Gmail drafts.");
    expect(v?.action).toEqual({ label: "Try again", disabled: false });
  });

  it("open: spotlights the Gmail button", () => {
    expect(contactsTourView("open", { firstName: "Pat" })).toEqual({
      target: '[data-tour="tour-gmail-button"]',
      title: "It's in your Gmail drafts",
      body: "Open it, give it a last look and hit send.",
      action: null,
    });
  });

  it("nothing on Contacts for Find's steps, or with no tour", () => {
    for (const step of ["pick", "draft", null] as const) {
      expect(contactsTourView(step, { firstName: "Pat" })).toBeNull();
    }
  });

  it("the all-set bubble is centered", () => {
    expect(ALL_SET_VIEW.target).toBeNull();
    expect(ALL_SET_VIEW.title).toBe("You're all set");
  });
});
