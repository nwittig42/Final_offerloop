import { contactIdFor, type TourStep, type WalkthroughState } from "@/services/walkthrough";

// Find's contact(p) while the tour is picking: reveal (asking for the free
// first outreach; the backend decides and charges nothing when it refuses),
// write the draft in the app only, then on to Contacts. A paid reveal only
// ever follows the user's explicit "Use 8 credits". The API calls come in as deps so this can be tested on its own.

export interface TourPerson {
  id: string;
  deckId: string;
  firstName: string;
  name: string;
}

export interface TourContactDeps {
  goTo: (step: TourStep, patch?: Partial<WalkthroughState>) => Promise<void>;
  /** useFree spends the free first outreach; otherwise the normal paid reveal. */
  reveal: (id: string, deckId: string, useFree: boolean) => Promise<{ email: string | null }>;
  /** In-app draft only (createGmailDraft: false). */
  draft: (id: string, deckId: string) => Promise<unknown>;
  navigate: (to: string) => void;
  /** The email was found; writing starts. */
  onWriting?: () => void;
  /** The draft is saved, before the move to Contacts. */
  onDrafted?: () => void;
}

export type TourContactResult =
  | { kind: "drafted" }
  | { kind: "no_email"; firstName: string }
  | { kind: "free_busy" }
  /** The free first outreach is used up; ask before the 8-credit reveal. */
  | { kind: "needs_payment"; firstName: string };

const code = (e: unknown) => (e instanceof Error ? e.message : "");
const tourPatch = (p: TourPerson) => ({ tourContactId: contactIdFor(p.id), tourPersonFirstName: p.firstName });

/** Record the person, move to the review step, open them in Contacts. */
export async function advanceToReview(p: TourPerson, deps: Pick<TourContactDeps, "goTo" | "navigate">) {
  const patch = tourPatch(p);
  await deps.goTo("review", patch);
  deps.navigate("/contacts?contact=" + encodeURIComponent(patch.tourContactId));
}

/**
 * Any failure other than "no email", "free outreach busy" or "needs payment"
 * returns the tour to pick and rethrows, so the caller shows its usual error.
 * opts.paid: the user chose to pay; the normal reveal (refunded on a miss).
 */
export async function runTourContact(p: TourPerson, deps: TourContactDeps, opts: { paid?: boolean } = {}): Promise<TourContactResult> {
  const backToPick = () => deps.goTo("pick").catch(() => {});
  const useFree = !opts.paid;
  try {
    // Who was picked is saved with the step, so a reload mid-draft can resume.
    await deps.goTo("draft", tourPatch(p));
    let reveal: { email: string | null };
    try {
      reveal = await deps.reveal(p.id, p.deckId, useFree);
    } catch (e) {
      const c = code(e);
      if (c === "candidate_not_found") {
        reveal = { email: null };
      } else if (useFree && c === "free_outreach_unavailable") {
        // Never a silent charge: show the price and let them choose.
        await backToPick();
        return { kind: "needs_payment", firstName: p.firstName || p.name };
      } else if (c === "free_outreach_in_use") {
        // Another reveal holds it right now. Never a silent charge: try again later.
        await backToPick();
        return { kind: "free_busy" };
      } else {
        throw e;
      }
    }
    if (!reveal.email) {
      await backToPick();
      return { kind: "no_email", firstName: p.firstName || p.name };
    }
    deps.onWriting?.();
    await deps.draft(p.id, p.deckId);
    deps.onDrafted?.();
    await advanceToReview(p, deps);
    return { kind: "drafted" };
  } catch (e) {
    await backToPick();
    throw e;
  }
}

/** A contact doc already holds a written draft. */
export const hasSavedDraft = (c: { emailBody?: unknown; draftCreatedAt?: unknown; gmailDraftId?: unknown } | null | undefined) =>
  !!(c && (c.emailBody || c.draftCreatedAt || c.gmailDraftId));

/**
 * The tour is on "draft" but nothing is drafting on this page (a reload):
 * on to review when the picked person's draft got saved, else back to pick.
 */
export function staleDraftStep(s: { step: TourStep | null; drafting: boolean; contactId?: string; savedDraft: boolean }): "review" | "pick" | null {
  if (s.step !== "draft" || s.drafting) return null;
  return s.contactId && s.savedDraft ? "review" : "pick";
}
