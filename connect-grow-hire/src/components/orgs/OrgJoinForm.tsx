// Inline "enter the org's password" form (OFF-42). The right password makes
// the student a member right away; onJoin rejects with the backend's error
// code as the message on a miss.
import { useState } from "react";
import { Loader2, Lock } from "lucide-react";
import type { Org } from "@/services/api";

export function joinErrorText(code: string): string {
  if (code === "wrong_password") return "That password isn't right.";
  if (code === "too_many_attempts") return "Too many tries. Wait 15 minutes and try again.";
  if (code === "join_closed") return "This organization isn't open to join yet.";
  return "Couldn't join right now. Try again in a moment.";
}

interface OrgJoinFormProps {
  org: Org;
  onJoin: (password: string) => Promise<void>;
  onCancel: () => void;
  // Button color; the org's own color in its card.
  accent?: string;
}

export function OrgJoinForm({ org, onJoin, onCancel, accent = "#3A4E8C" }: OrgJoinFormProps) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!password || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onJoin(password);
    } catch (e: any) {
      setError(joinErrorText(e?.message || ""));
      setBusy(false);
    }
  };

  return (
    <form onSubmit={(e) => { e.preventDefault(); void submit(); }}>
      <label htmlFor={`org-pw-${org.id}`} className="mb-1.5 block text-[13px] font-medium text-[#475569]">
        Enter the {org.shortName} password to join
      </label>
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex h-10 min-w-0 flex-1 items-center gap-2 rounded-[8px] border border-[#CBD5E1] bg-white px-3 focus-within:border-[#64748B]">
          <Lock className="h-4 w-4 shrink-0 text-[#94A3B8]" />
          <input
            id={`org-pw-${org.id}`}
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
          className="inline-flex h-10 shrink-0 items-center gap-1.5 rounded-full px-5 text-[14px] font-semibold text-white transition-opacity hover:opacity-90 disabled:opacity-50"
          style={{ background: accent }}
        >
          {busy && <Loader2 className="h-4 w-4 animate-spin" />}
          Join
        </button>
        <button type="button" onClick={onCancel} className="h-10 shrink-0 px-2 text-[14px] text-[#64748B] hover:text-[#0F172A]">
          Cancel
        </button>
      </div>
      {error && <p role="alert" className="mt-2 text-[13px] text-[#B91C1C]">{error}</p>}
      <p className="mt-2 text-[12.5px] text-[#94A3B8]">Don't have it? Ask your chapter's leadership.</p>
    </form>
  );
}
