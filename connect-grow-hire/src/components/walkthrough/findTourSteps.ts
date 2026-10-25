import type { TourStep } from "@/services/walkthrough";

// What Scout says on Find for the tour's first two steps. Pure, so FindPage
// only decides when to show it. The Contacts steps show nothing here.

export const FIRST_CARDS = '[data-tour="tour-first-cards"]';
export const PROFILE_PRIMARY = '[data-tour="tour-profile-primary"]';

export function findTourView(
  step: TourStep | null,
  ctx: { profileOpen: boolean; noEmailFor?: string },
): null | { target: string | null; title: string; body: string } {
  if (step === "draft") {
    return { target: null, title: "Writing your email…", body: "Finding their email and drafting it. One moment." };
  }
  if (step !== "pick") return null;
  if (ctx.profileOpen) {
    return {
      target: PROFILE_PRIMARY,
      title: "Write it for me",
      body: "Hit this and I'll find their email and write a first draft for you.",
    };
  }
  return {
    target: FIRST_CARDS,
    title: "Your first email",
    body: ctx.noEmailFor
      ? `No email for ${ctx.noEmailFor}. Nothing was charged. Pick someone else.`
      : "Here are a few people worth emailing first. Click one to see why they fit.",
  };
}
