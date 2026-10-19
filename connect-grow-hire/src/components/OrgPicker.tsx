// Org alumni directory picker (OFF-42): one row per joinable org. Joining
// asks for the org's password inline; the right one makes you a member right
// away. Used by Account Settings and onboarding, which supply onJoin/onLeave.
import { useState } from "react";
import { Check, Loader2, Lock, Users } from "lucide-react";
import type { Org } from "@/services/api";

interface OrgPickerProps {
  orgs: Org[];
  joined: string[];
  // Rejects with the backend's error code as the message on a failed join.
  onJoin: (orgId: string, password: string) => Promise<void>;
  onLeave: (orgId: string) => Promise<void>;
  // Shown next to orgs the student has joined (Account Settings).
  onOpen?: (orgId: string) => void;
}

function joinError(code: string): string {
  if (code === "wrong_password") return "That password isn't right.";
  if (code === "too_many_attempts") return "Too many tries. Wait 15 minutes and try again.";
  if (code === "join_closed") return "This organization isn't open to join yet.";
  return "Couldn't join right now. Try again in a moment.";
}

export function OrgPicker({ orgs, joined, onJoin, onLeave, onOpen }: OrgPickerProps) {
  const [openId, setOpenId] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  if (orgs.length === 0) {
    return <p className="text-[14px] text-[#64748B]">No organizations are available yet.</p>;
  }

  const openForm = (id: string) => { setOpenId(id); setPassword(""); setError(null); };

  const submit = async (id: string) => {
    if (!password) return;
    setBusyId(id);
    setError(null);
    try {
      await onJoin(id, password);
      setOpenId(null);
      setPassword("");
    } catch (e: any) {
      setError(joinError(e?.message || ""));
    } finally {
      setBusyId(null);
    }
  };

  const leave = async (id: string) => {
    setBusyId(id);
    try { await onLeave(id); } finally { setBusyId(null); }
  };

  return (
    <ul className="space-y-2">
      {orgs.map((o) => {
        const on = joined.includes(o.id);
        const busy = busyId === o.id;
        const formOpen = openId === o.id && !on;
        return (
          <li key={o.id} className="rounded-[10px] border border-[#E2E8F0] bg-white px-4 py-3">
            <div className="flex items-center gap-3">
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
              {on ? (
                <button
                  type="button"
                  onClick={() => leave(o.id)}
                  disabled={busy}
                  title="Leave this organization"
                  className="inline-flex h-9 shrink-0 items-center gap-1.5 rounded-full bg-[#3A4E8C] px-4 text-[14px] font-semibold text-white transition-colors hover:bg-[#2F3F73] disabled:opacity-60"
                >
                  {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
                  Member
                </button>
              ) : !formOpen && (
                <button
                  type="button"
                  onClick={() => openForm(o.id)}
                  className="inline-flex h-9 shrink-0 items-center rounded-full border border-[#CBD5E1] bg-white px-4 text-[14px] font-semibold text-[#0F172A] transition-colors hover:bg-[#F8FAFC]"
                >
                  I'm a member
                </button>
              )}
            </div>

            {formOpen && (
              <form
                onSubmit={(e) => { e.preventDefault(); void submit(o.id); }}
                className="mt-3 border-t border-[#F1F5F9] pt-3"
              >
                <label htmlFor={`org-pw-${o.id}`} className="mb-1.5 block text-[13px] font-medium text-[#475569]">
                  Enter the {o.shortName} password to join
                </label>
                <div className="flex flex-wrap items-center gap-2">
                  <div className="flex h-10 min-w-0 flex-1 items-center gap-2 rounded-[8px] border border-[#CBD5E1] px-3 focus-within:border-[#3A4E8C]">
                    <Lock className="h-4 w-4 shrink-0 text-[#94A3B8]" />
                    <input
                      id={`org-pw-${o.id}`}
                      type="password"
                      autoComplete="off"
                      autoFocus
                      value={password}
                      onChange={(e) => { setPassword(e.target.value); setError(null); }}
                      className="min-w-0 flex-1 bg-transparent text-[15px] text-[#0F172A] outline-none"
                    />
                  </div>
                  <button
                    type="submit"
                    disabled={!password || busy}
                    className="inline-flex h-10 shrink-0 items-center gap-1.5 rounded-full bg-[#3A4E8C] px-5 text-[14px] font-semibold text-white hover:bg-[#2F3F73] disabled:opacity-50"
                  >
                    {busy && <Loader2 className="h-4 w-4 animate-spin" />}
                    Join
                  </button>
                  <button type="button" onClick={() => setOpenId(null)} className="h-10 shrink-0 px-2 text-[14px] text-[#64748B] hover:text-[#0F172A]">
                    Cancel
                  </button>
                </div>
                {error && <p role="alert" className="mt-2 text-[13px] text-[#B91C1C]">{error}</p>}
                <p className="mt-2 text-[12.5px] text-[#94A3B8]">Don't have it? Ask your chapter's leadership.</p>
              </form>
            )}
          </li>
        );
      })}
    </ul>
  );
}
