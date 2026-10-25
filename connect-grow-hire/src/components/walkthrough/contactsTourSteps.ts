import type { TourStep } from "@/services/walkthrough";

// What Scout says on Contacts for the tour's last three steps. Pure, so the
// hook only wires up the buttons. Find's steps show nothing here.

export const TOUR_DRAFT = '[data-tour="tour-draft"]';
export const TOUR_GMAIL_BUTTON = '[data-tour="tour-gmail-button"]';

export interface ContactsTourView {
  target: string | null;
  title: string;
  body: string;
  action: { label: string; disabled: boolean } | null;
  secondary?: { label: string };
}

/** Scout's goodbye after the Gmail button is clicked. */
export const ALL_SET_VIEW = { target: null, title: "You're all set", body: "Your first email is waiting in Gmail." } as const;

export function contactsTourView(
  step: TourStep | null,
  ctx: { firstName: string; gmailError?: string | null; pushing?: boolean; pushError?: string | null; connected?: boolean },
): ContactsTourView | null {
  const pushing = !!ctx.pushing;
  if (step === "review") {
    return {
      target: TOUR_DRAFT,
      title: ctx.firstName ? `Your email to ${ctx.firstName}` : "Your email",
      body: "Read it over. You can tweak it in Gmail before you send.",
      action: { label: "Looks good", disabled: pushing },
    };
  }
  if (step === "gmail") {
    // Trying again can't fix these: only Not now.
    const dead = !ctx.gmailError && (ctx.pushError === "recipient_suppressed" || ctx.pushError === "no_saved_draft");
    if (dead && !pushing) {
      return {
        target: null,
        title: "Connect Gmail",
        body: ctx.pushError === "recipient_suppressed"
          ? "Gmail won't take a draft to this address. It bounced before."
          : "Something went wrong putting it in your Gmail drafts.",
        action: null,
        secondary: { label: "Not now" },
      };
    }
    const retry = !!ctx.gmailError || !!ctx.pushError;
    return {
      target: null,
      title: "Connect Gmail",
      body: pushing
        ? "Putting it in your Gmail drafts…"
        : ctx.gmailError
          ? "Google didn't give us permission to create drafts. Connect again and tick every box."
          : ctx.pushError
            ? "Something went wrong putting it in your Gmail drafts."
            : "Connect Gmail so this goes out from your own inbox. Google will ask you to tick a few boxes. Tick all of them.",
      action: { label: retry ? "Try again" : ctx.connected ? "Put it in Gmail" : "Connect Gmail", disabled: pushing },
      secondary: { label: "Not now" },
    };
  }
  if (step === "open") {
    return {
      target: TOUR_GMAIL_BUTTON,
      title: "It's in your Gmail drafts",
      body: "Open it, give it a last look and hit send.",
      action: null,
    };
  }
  return null;
}
