// "Organizations you're in" (OFF-42): the one place a student adds an org.
// The dropdown lists every org A to Z and narrows as you type. Pick yours,
// enter its password in the pop-up (a miss lets them retry; dismissing
// closes it), and a checkmark confirms it. From then on Find
// recognizes the membership: the org's filter pill, its directory card, and
// "TKE alumni" searches all start working. Used by Account Settings and the
// onboarding org step.
import { useState } from "react";
import { Check, ChevronsUpDown, Loader2, Plus, X } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { Org } from "@/services/api";
import { OrgJoinForm } from "@/components/orgs/OrgJoinForm";
import { orgAccent, orgMonogram, sortOrgsByName, useOrgs } from "@/hooks/useOrgs";
import { orgLogo } from "@/lib/orgLogos";

interface OrgMembershipEditorProps {
  // Shown on each joined org (Account Settings); omitted in onboarding.
  onOpenDirectory?: (orgId: string) => void;
  onJoined?: (org: Org) => void;
  onLeft?: (org: Org) => void;
  // The pop-up's dismiss button. Onboarding passes "Skip for now" and moves
  // on; elsewhere it just closes the pop-up.
  skipLabel?: string;
  onSkip?: () => void;
}

function Monogram({ org, size = 36 }: { org: Org; size?: number }) {
  const logo = orgLogo(org.id);
  if (logo) {
    return <img src={logo} alt="" aria-hidden className="shrink-0 object-contain" style={{ width: size, height: size }} />;
  }
  return (
    <span
      className="flex shrink-0 items-center justify-center rounded-[10px] text-[12px] font-bold tracking-wide text-white"
      style={{ width: size, height: size, background: orgAccent(org) }}
      aria-hidden
    >
      {orgMonogram(org)}
    </span>
  );
}

export function OrgMembershipEditor({ onOpenDirectory, onJoined, onLeft, skipLabel, onSkip }: OrgMembershipEditorProps) {
  const { orgs, memberships, joined, loading, isError, refetch, join, leave } = useOrgs();
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState<Org | null>(null);
  // Set when the password was right: the pop-up shows a checkmark, then closes.
  const [justJoined, setJustJoined] = useState(false);
  const [leavingId, setLeavingId] = useState<string | null>(null);

  const addable = sortOrgsByName(orgs.filter((o) => !memberships.includes(o.id)));

  const closePopup = () => {
    setPicked(null);
    setJustJoined(false);
  };

  const remove = async (org: Org) => {
    setLeavingId(org.id);
    try {
      await leave.mutateAsync(org.id);
      onLeft?.(org);
    } finally {
      setLeavingId(null);
    }
  };

  if (loading) return <p className="text-[14px] text-[#64748B]">Loading organizations…</p>;
  if (isError) {
    return (
      <p className="text-[14px] text-[#64748B]">
        Couldn't load organizations.{" "}
        <button type="button" onClick={() => refetch()} className="font-semibold text-[#3A4E8C] hover:underline">Try again</button>
      </p>
    );
  }

  return (
    <div className="space-y-5">
      {joined.length > 0 && (
        <ul className="space-y-2">
          {joined.map((o) => (
            <li key={o.id} className="flex items-center gap-3 rounded-[10px] border border-[#E2E8F0] bg-white px-4 py-3">
              <Monogram org={o} />
              <div className="min-w-0 flex-1">
                <div className="truncate text-[15px] font-semibold text-[#0F172A]">{o.name}</div>
                <div className="flex items-center gap-1 truncate text-[13px] font-medium text-[#15803D]">
                  <Check className="h-3.5 w-3.5 shrink-0" strokeWidth={3} /> You're a member
                </div>
              </div>
              {onOpenDirectory && (
                <button
                  type="button"
                  onClick={() => onOpenDirectory(o.id)}
                  className="shrink-0 rounded-full px-3 py-1.5 text-[14px] font-semibold text-white transition-opacity hover:opacity-90"
                  style={{ background: orgAccent(o) }}
                >
                  Open directory
                </button>
              )}
              <button
                type="button"
                onClick={() => remove(o)}
                disabled={leavingId === o.id}
                aria-label={`Remove ${o.name}`}
                title="Remove"
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[#94A3B8] hover:bg-[#F1F5F9] hover:text-[#475569] disabled:opacity-50"
              >
                {leavingId === o.id ? <Loader2 className="h-4 w-4 animate-spin" /> : <X className="h-4 w-4" />}
              </button>
            </li>
          ))}
        </ul>
      )}

      <div>
        <label className="mb-1.5 block text-[14px] font-semibold text-[#0F172A]">
          {joined.length ? "Add another organization" : "Add an organization you're in"}
        </label>
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <button
              type="button"
              role="combobox"
              aria-expanded={open}
              className="flex h-12 w-full items-center justify-between rounded-[3px] border border-[rgba(59,130,246,0.12)] bg-[#FAFBFF] px-4 text-left text-[15px] text-[#0F172A]"
            >
              <span className="text-[#94A3B8]">Search for your fraternity, sorority, club or team</span>
              <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
            </button>
          </PopoverTrigger>
          <PopoverContent className="w-[var(--radix-popover-trigger-width)] p-0" align="start">
            <Command>
              <CommandInput placeholder="Type your organization's name…" />
              <CommandList>
                <CommandEmpty>No match. Your organization may not be on Offerloop yet.</CommandEmpty>
                <CommandGroup>
                  {addable.map((o) => (
                    <CommandItem
                      key={o.id}
                      // cmdk filters on this, so nicknames like "tke" match too.
                      value={[o.name, o.shortName, o.school, ...o.aliases].join(" ")}
                      onSelect={() => { setPicked(o); setOpen(false); }}
                      className="flex items-center gap-3 py-2"
                    >
                      <Monogram org={o} size={28} />
                      <span className="min-w-0">
                        <span className="block truncate text-[14px] font-medium">{o.name}</span>
                        {o.school && <span className="block truncate text-[12px] text-[#64748B]">{o.school}</span>}
                      </span>
                    </CommandItem>
                  ))}
                </CommandGroup>
              </CommandList>
            </Command>
          </PopoverContent>
        </Popover>

        <Dialog open={!!picked} onOpenChange={(o) => { if (!o) closePopup(); }}>
          <DialogContent className="max-w-md rounded-[12px]">
            {picked && justJoined && (
              <div className="flex flex-col items-center py-4 text-center">
                <span className="flex h-14 w-14 items-center justify-center rounded-full bg-[#16A34A] text-white animate-in zoom-in-50 duration-300">
                  <Check className="h-8 w-8" strokeWidth={3} />
                </span>
                <DialogTitle className="mt-4 text-[18px]">You're in</DialogTitle>
                <DialogDescription className="mt-1 text-[14px]">
                  {picked.name} alumni now show up in Find.
                </DialogDescription>
              </div>
            )}
            {picked && !justJoined && (
              <>
                <DialogHeader className="flex-row items-center gap-3 space-y-0 text-left">
                  <Monogram org={picked} />
                  <div className="min-w-0">
                    <DialogTitle className="truncate text-[17px]">Join {picked.name}</DialogTitle>
                    <DialogDescription className="truncate text-[13px]">
                      {picked.school || "Members get the full alumni directory"}
                    </DialogDescription>
                  </div>
                </DialogHeader>
                <OrgJoinForm
                  org={picked}
                  accent={orgAccent(picked)}
                  cancelLabel={skipLabel}
                  onJoin={async (password) => {
                    await join.mutateAsync({ id: picked.id, password });
                    onJoined?.(picked);
                    setJustJoined(true);
                    setTimeout(closePopup, 1400);
                  }}
                  onCancel={() => {
                    closePopup();
                    onSkip?.();
                  }}
                />
              </>
            )}
          </DialogContent>
        </Dialog>
        {!picked && addable.length === 0 && orgs.length > 0 && (
          <p className="mt-2 flex items-center gap-1.5 text-[13px] text-[#64748B]">
            <Plus className="h-3.5 w-3.5" /> You've added every organization on Offerloop so far.
          </p>
        )}
      </div>
    </div>
  );
}
