import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Mail, X } from "lucide-react";
import {
  loadWalkthrough,
  saveWalkthrough,
  walkthroughIncomplete,
  type WalkthroughState,
} from "@/services/walkthrough";

// "Finish your first email" on Find while the first-use walkthrough is
// incomplete. Only accounts that went through onboarding v2 have a
// walkthrough, so everyone else never sees it. Closing it only hides the
// reminder; /get-started still resumes the walkthrough.

interface FirstEmailReminderProps {
  uid: string;
  /** Test seams. */
  load?: (uid: string) => Promise<WalkthroughState | null>;
  save?: (uid: string, state: WalkthroughState) => Promise<void>;
}

export function FirstEmailReminder({ uid, load = loadWalkthrough, save = saveWalkthrough }: FirstEmailReminderProps) {
  const [state, setState] = useState<WalkthroughState | null>(null);
  useEffect(() => {
    let cancelled = false;
    load(uid)
      .then((s) => {
        if (!cancelled) setState(s);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [uid, load]);

  if (!state || !walkthroughIncomplete(state) || state.reminderDismissedAt) return null;

  const selected = state.matches?.find((m) => m.id === state.selectedId);
  const detail =
    state.status === "drafted" && selected
      ? `Your email to ${selected.firstName} is saved. Pick up where you left off.`
      : "We picked a few people who fit your background. It takes about two minutes.";

  const dismiss = () => {
    const next = { ...state, reminderDismissedAt: new Date().toISOString() };
    setState(next);
    save(uid, next).catch(() => {});
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
      <Link
        to="/get-started"
        className="shrink-0 rounded-full bg-[#4A60A8] px-4 py-1.5 text-[13px] font-semibold text-white hover:bg-[#3C4F8E]"
      >
        Continue
      </Link>
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
