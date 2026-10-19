// Find → Organizations (OFF-42). Its own space inside Find: a dark band and
// a tinted page instead of the People gallery. Look up your org, join it with
// its password, then open its whole alumni directory.
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronRight, Search } from "lucide-react";
import type { Org } from "@/services/api";
import { OrgJoinForm } from "@/components/orgs/OrgJoinForm";
import { orgAccent, orgMonogram, sortOrgsForSchool } from "@/hooks/useOrgs";
import { APPLE_FONT } from "@/components/find/people/PersonCard";

export const ORGS_PAGE_BG = "#ECEFF6";
const BAND = "linear-gradient(165deg, #25335A 0%, #0F172A 100%)";

interface OrgsViewProps {
  orgs: Org[];
  memberships: string[];
  loading: boolean;
  school: string | null;
  // Opens this org's password form and scrolls to it (from a search nudge).
  focusOrgId: string | null;
  onJoin: (orgId: string, password: string) => Promise<void>;
  onShowPeople: () => void;
}

export function OrgsView({ orgs, memberships, loading, school, focusOrgId, onJoin, onShowPeople }: OrgsViewProps) {
  const [text, setText] = useState("");
  const [openId, setOpenId] = useState<string | null>(focusOrgId);
  useEffect(() => { if (focusOrgId) setOpenId(focusOrgId); }, [focusOrgId]);

  const shown = useMemo(() => {
    const q = text.trim().toLowerCase();
    const list = sortOrgsForSchool(orgs, school);
    if (!q) return list;
    return list.filter((o) => [o.name, o.shortName, o.school, ...o.aliases].join(" ").toLowerCase().includes(q));
  }, [orgs, school, text]);

  return (
    <div style={{ fontFamily: APPLE_FONT }}>
      <section className="rounded-[26px] px-5 pb-7 pt-5 text-white sm:px-8" style={{ background: BAND }}>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onShowPeople}
            className="inline-flex h-9 items-center rounded-full border border-white/25 px-4 text-[14px] font-semibold text-white/80 transition-colors hover:bg-white/10 hover:text-white"
          >
            People
          </button>
          <span aria-current="page" className="inline-flex h-9 items-center rounded-full bg-white px-4 text-[14px] font-semibold text-[#0F172A]">
            Organizations
          </span>
        </div>
        <h1 className="mt-6 text-[30px] font-semibold leading-tight tracking-[-0.02em]">Organizations</h1>
        <p className="mt-1.5 max-w-[620px] text-[15px] leading-[1.5] text-white/70">
          Your fraternity, sorority, club or team, with its whole alumni network. Join with your chapter's password, then open the directory.
        </p>
        <label className="mt-5 flex h-11 max-w-[520px] items-center gap-2 rounded-full bg-white/10 px-4 ring-1 ring-white/15 focus-within:bg-white/15 focus-within:ring-white/40">
          <Search className="h-4 w-4 shrink-0 text-white/60" />
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Find your organization"
            aria-label="Find your organization"
            className="min-w-0 flex-1 bg-transparent text-[15px] text-white outline-none placeholder:text-white/50"
          />
        </label>
      </section>

      <div className="mt-6 grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(300px,1fr))]">
        {loading && Array.from({ length: 3 }).map((_, i) => (
          <div key={i} className="h-[168px] animate-pulse rounded-[22px] bg-white/70" />
        ))}
        {shown.map((o) => (
          <OrgCard
            key={o.id}
            org={o}
            member={memberships.includes(o.id)}
            formOpen={openId === o.id}
            focused={focusOrgId === o.id}
            onOpenForm={() => setOpenId(o.id)}
            onCloseForm={() => setOpenId(null)}
            onJoin={async (pw) => { await onJoin(o.id, pw); setOpenId(null); }}
          />
        ))}
      </div>
      {!loading && shown.length === 0 && (
        <p className="mt-8 text-center text-[14px] text-[#6E6E73]">
          {text ? `No organization matches "${text}". Yours may not be on Offerloop yet.` : "No organizations are on Offerloop yet."}
        </p>
      )}
    </div>
  );
}

function OrgCard({ org, member, formOpen, focused, onOpenForm, onCloseForm, onJoin }: {
  org: Org; member: boolean; formOpen: boolean; focused: boolean;
  onOpenForm: () => void; onCloseForm: () => void; onJoin: (password: string) => Promise<void>;
}) {
  const accent = orgAccent(org);
  const ref = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (focused) ref.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [focused]);

  return (
    <article
      ref={ref}
      className="flex flex-col overflow-hidden rounded-[22px] bg-white shadow-[0_1px_2px_rgba(0,0,0,0.04),0_4px_14px_rgba(0,0,0,0.05)]"
      style={focused ? { boxShadow: `0 0 0 2px ${accent}, 0 4px 14px rgba(0,0,0,0.05)` } : undefined}
    >
      <div className="h-1.5" style={{ background: accent }} />
      <div className="flex flex-1 flex-col p-5">
        <div className="flex items-center gap-3">
          <span
            className="flex h-12 w-12 shrink-0 items-center justify-center rounded-[14px] text-[15px] font-bold tracking-wide text-white"
            style={{ background: accent }}
            aria-hidden
          >
            {orgMonogram(org)}
          </span>
          <div className="min-w-0">
            <div className="truncate text-[16px] font-semibold text-[#1D1D1F]">{org.name}</div>
            <div className="truncate text-[13px] text-[#6E6E73]">
              {[org.school, org.memberCount ? `${org.memberCount.toLocaleString()} members` : ""].filter(Boolean).join(" · ")}
            </div>
          </div>
        </div>

        <div className="mt-auto pt-5">
          {member ? (
            <div className="flex items-center justify-between gap-3">
              <span className="text-[13px] font-medium text-[#1D7A3E]">You're a member</span>
              <Link
                to={`/directory/${org.id}`}
                className="inline-flex h-9 items-center gap-1 rounded-full px-4 text-[14px] font-semibold text-white transition-opacity hover:opacity-90"
                style={{ background: accent }}
              >
                Open directory <ChevronRight className="h-4 w-4" />
              </Link>
            </div>
          ) : formOpen ? (
            <OrgJoinForm org={org} onJoin={onJoin} onCancel={onCloseForm} accent={accent} />
          ) : (
            <div className="flex items-center justify-between gap-3">
              <span className="text-[13px] text-[#6E6E73]">Members only</span>
              <button
                type="button"
                onClick={onOpenForm}
                className="inline-flex h-9 items-center rounded-full border border-[#CBD5E1] px-4 text-[14px] font-semibold text-[#1D1D1F] transition-colors hover:bg-[#F5F5F7]"
              >
                I'm a member
              </button>
            </div>
          )}
        </div>
      </div>
    </article>
  );
}
