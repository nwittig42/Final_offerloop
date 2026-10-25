import type { TourStep } from "@/services/walkthrough";

// What Scout says on Find for the tour's first two steps. Pure, so FindPage
// only decides when to show it. The Contacts steps show nothing here.

export const FIRST_CARDS = '[data-tour="tour-first-cards"]';
export const PROFILE_PRIMARY = '[data-tour="tour-profile-primary"]';

export function findTourView(
  step: TourStep | null,
  ctx: { profileOpen: boolean; noEmailFor?: string; needsPayment?: boolean; feedEmpty?: boolean },
): null | { target: string | null; title: string; body: string; action?: { label: string } } {
  if (step === "draft") {
    return { target: null, title: "Writing your email…", body: "Finding their email and drafting it. One moment." };
  }
  // No cards to spotlight yet: stay out of the way of the empty-feed prompt.
  if (step !== "pick" || ctx.feedEmpty) return null;
  if (ctx.profileOpen && ctx.needsPayment) {
    return {
      target: PROFILE_PRIMARY,
      title: "Write it for me",
      body: "Your free first email is used up. This one costs 8 credits, refunded if no email is found.",
      action: { label: "Use 8 credits" },
    };
  }
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

/** The profile's primary button tooltip while the tour picks (undefined = the normal price title). */
export function findTourPrimaryTitle(step: TourStep | null, ctx: { recommended: boolean; needsPayment?: boolean }): string | undefined {
  return step === "pick" && ctx.recommended && !ctx.needsPayment ? "Free: your first email is on us" : undefined;
}
