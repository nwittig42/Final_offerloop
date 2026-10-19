// Org alumni directory picker (OFF-42): one row per joinable org with a
// Join / Joined toggle. Presentational only; Account Settings joins
// immediately, onboarding collects picks and joins after the profile saves.
import { Check, Loader2, Users } from "lucide-react";
import type { Org } from "@/services/api";

interface OrgPickerProps {
  orgs: Org[];
  selected: string[];
  onToggle: (orgId: string) => void;
  busyId?: string | null;
  // Shown next to orgs the student has joined (Account Settings).
  onOpen?: (orgId: string) => void;
}

export function OrgPicker({ orgs, selected, onToggle, busyId, onOpen }: OrgPickerProps) {
  if (orgs.length === 0) {
    return <p className="text-[14px] text-[#64748B]">No organizations are available yet.</p>;
  }
  return (
    <ul className="space-y-2">
      {orgs.map((o) => {
        const on = selected.includes(o.id);
        const busy = busyId === o.id;
        return (
          <li key={o.id} className="flex items-center gap-3 rounded-[10px] border border-[#E2E8F0] bg-white px-4 py-3">
            <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[#EEF1FA] text-[#3A4E8C]">
              <Users className="h-4 w-4" />
            </span>
            <div className="min-w-0 flex-1">
              <div className="truncate text-[15px] font-semibold text-[#0F172A]">{o.name}</div>
              <div className="truncate text-[13px] text-[#64748B]">
                {[o.school, o.memberCount ? `${o.memberCount.toLocaleString()} members` : ""].filter(Boolean).join(" · ")}
              </div>
            </div>
            {on && onOpen && (
              <button type="button" onClick={() => onOpen(o.id)} className="shrink-0 text-[14px] font-semibold text-[#3A4E8C] hover:underline">
                Open directory
              </button>
            )}
            <button
              type="button"
              onClick={() => onToggle(o.id)}
              disabled={busy}
              aria-pressed={on}
              className={`inline-flex h-9 shrink-0 items-center gap-1.5 rounded-full px-4 text-[14px] font-semibold transition-colors disabled:opacity-60 ${
                on ? "bg-[#3A4E8C] text-white hover:bg-[#2F3F73]" : "border border-[#CBD5E1] bg-white text-[#0F172A] hover:bg-[#F8FAFC]"
              }`}
            >
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : on ? <Check className="h-4 w-4" /> : null}
              {on ? "Member" : "I'm a member"}
            </button>
          </li>
        );
      })}
    </ul>
  );
}
