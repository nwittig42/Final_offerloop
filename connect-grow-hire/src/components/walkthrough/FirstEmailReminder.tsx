import { useState } from "react";
import { Mail, X } from "lucide-react";
import { saveWalkthrough, walkthroughIncomplete, type WalkthroughState } from "@/services/walkthrough";

// "Finish your first email" on Find while the first-use walkthrough is
// incomplete. Only accounts that went through onboarding v2 have a
// walkthrough, so everyone else never sees it. Continue resumes Scout's
// tour at its saved step; closing the reminder only hides the reminder.

interface FirstEmailReminderProps {
  uid: string;
  /** From useFirstEmailTour; null when there's no walkthrough (or it hasn't loaded). */
  state: WalkthroughState | null;
  onContinue: () => void;
  /** Test seam. */
  save?: (uid: string, state: WalkthroughState) => Promise<void>;
}

export function FirstEmailReminder({ uid, state, onContinue, save = saveWalkthrough }: FirstEmailReminderProps) {
  const [dismissed, setDismissed] = useState(false);

  if (!state || !walkthroughIncomplete(state) || state.reminderDismissedAt || dismissed) return null;

  const selected = state.matches?.find((m) => m.id === state.selectedId);
  // The tour's draft (tourContactId), else the old pop-up's.
  const draftedTo = state.tourContactId ? state.tourPersonFirstName : state.status === "drafted" ? selected?.firstName : undefined;
  const detail =
    draftedTo
      ? `Your email to ${draftedTo} is saved. Pick up where you left off.`
      : "We picked a few people who fit your background. It takes about two minutes.";

  const dismiss = () => {
    setDismissed(true);
    save(uid, { ...state, reminderDismissedAt: new Date().toISOString() }).catch(() => {});
  };

  return (
    <div
      role="region"
      aria-label="Finish your first email"
      className="mt-4 flex items-center gap-3 rounded-[18px] bg-white px-4 py-3 shadow-[0_1px_2px_rgba(0,0,0,0.04),0_4px_14px_rgba(0,0,0,0.05)]"
    >
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[#EEF1FA] text-[#3A4E8C]">
        <Mail className="h-4 w-4" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-[14.5px] font-semibold text-[#1D1D1F]">Finish your first email</span>
        <span className="block text-[12.5px] text-[#6E6E73]">{detail}</span>
      </span>
      <button
        type="button"
        onClick={onContinue}
        className="shrink-0 rounded-full bg-[#4A60A8] px-4 py-1.5 text-[13px] font-semibold text-white hover:bg-[#3C4F8E]"
      >
        Continue
      </button>
      <button
        type="button"
        onClick={dismiss}
        aria-label="Dismiss reminder"
        className="shrink-0 rounded-full p-1.5 text-[#6E6E73] hover:bg-[#F5F5F7]"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
