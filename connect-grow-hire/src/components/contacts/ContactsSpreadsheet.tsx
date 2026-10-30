// Full-width table of every contact. Click a row to open it in List view;
// checkboxes drive bulk share/delete. Styled to match the live My Network
// People table: logo column, bold name with mono email + LinkedIn under it,
// tiny uppercase headers, alternating row tint.
import { useMemo, useState } from "react";
import { ChevronDown, ChevronUp, Linkedin } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import { CompanyLogo } from "@/components/CompanyLogo";
import { formatDate, linkedInHref, type ContactRow } from "./contactsModel";
import { StagePill } from "./StagePill";

type Col = "name" | "company" | "role" | "school" | "stage" | "added";

const COLS: { id: Col; label: string }[] = [
  { id: "name", label: "Name" },
  { id: "company", label: "Company" },
  { id: "role", label: "Role" },
  { id: "school", label: "School" },
  { id: "stage", label: "Stage" },
  { id: "added", label: "Added" },
];

// checkbox | logo | name | company | role | school | stage | added
const GRID = "28px 36px minmax(180px,1.5fr) minmax(140px,1.1fr) minmax(140px,1fr) minmax(150px,1.1fr) 112px 96px";
const HEADER_LABEL = "font-sans text-[9px] font-medium uppercase tracking-[0.12em] text-ink-3";

function sortValue(r: ContactRow, col: Col): string | number {
  switch (col) {
    case "name": return r.name.toLowerCase();
    case "company": return r.company.toLowerCase();
    case "role": return r.role.toLowerCase();
    case "school": return r.school.toLowerCase();
    case "stage": return r.stageLabel;
    case "added": return Date.parse(r.createdAt) || 0;
  }
}

interface Props {
  rows: ContactRow[];
  selected: Set<string>;
  onSelectionChange: (next: Set<string>) => void;
  onOpen: (id: string) => void;
}

export function ContactsSpreadsheet({ rows, selected, onSelectionChange, onOpen }: Props) {
  const [sort, setSort] = useState<{ col: Col; dir: "asc" | "desc" } | null>(null);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const mul = sort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => {
      const va = sortValue(a, sort.col);
      const vb = sortValue(b, sort.col);
      return (va < vb ? -1 : va > vb ? 1 : 0) * mul;
    });
  }, [rows, sort]);

  const allSelected = rows.length > 0 && rows.every((r) => selected.has(r.id));
  const toggleAll = () => onSelectionChange(allSelected ? new Set() : new Set(rows.map((r) => r.id)));
  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id); else next.add(id);
    onSelectionChange(next);
  };
  const clickHeader = (col: Col) =>
    setSort((s) => (s?.col === col ? { col, dir: s.dir === "asc" ? "desc" : "asc" } : { col, dir: "asc" }));

  const rowBg = (r: ContactRow, idx: number) =>
    selected.has(r.id) ? "rgba(59,130,246,0.08)" : idx % 2 === 1 ? "var(--paper-2, #FAFBFF)" : "white";

  return (
    <div className="h-full overflow-auto bg-white px-6 py-5">
      <div className="min-w-[1000px] overflow-clip rounded-st-xl border border-line bg-white">
        <div
          className="sticky top-0 z-10 grid items-center border-b border-line"
          style={{ gridTemplateColumns: GRID, gap: 14, padding: "10px 16px", background: "var(--paper-2, #FAFBFF)" }}
        >
          <Checkbox checked={allSelected} onCheckedChange={toggleAll} aria-label="Select all" />
          <span />
          {COLS.map((c) => (
            <button key={c.id} type="button" className="text-left" onClick={() => clickHeader(c.id)}>
              <span className={HEADER_LABEL}>
                {c.label}
                {sort?.col === c.id && (sort.dir === "asc"
                  ? <ChevronUp className="ml-0.5 inline h-3 w-3" />
                  : <ChevronDown className="ml-0.5 inline h-3 w-3" />)}
              </span>
            </button>
          ))}
        </div>

        {sorted.length === 0 ? (
          <div className="py-16 text-center">
            <p className="font-serif text-[15px] italic text-ink-3">No contacts to show.</p>
          </div>
        ) : sorted.map((r, idx) => {
          const linkedIn = linkedInHref(r.linkedinUrl);
          return (
            <div
              key={r.id}
              role="button"
              tabIndex={0}
              onClick={() => onOpen(r.id)}
              onKeyDown={(e) => { if (e.key === "Enter") onOpen(r.id); }}
              className={`grid cursor-pointer items-center transition-colors ${idx === sorted.length - 1 ? "" : "border-b border-line-2"}`}
              style={{ gridTemplateColumns: GRID, gap: 14, padding: "12px 16px", background: rowBg(r, idx) }}
              onMouseEnter={(e) => { e.currentTarget.style.background = "rgba(91,119,153,0.08)"; }}
              onMouseLeave={(e) => { e.currentTarget.style.background = rowBg(r, idx); }}
            >
              <span onClick={(e) => e.stopPropagation()} className="flex items-center">
                <Checkbox checked={selected.has(r.id)} onCheckedChange={() => toggle(r.id)} aria-label={`Select ${r.name}`} />
              </span>

              <div className="flex items-center justify-center">
                {r.company ? (
                  <CompanyLogo company={r.company} size={32} rounded={6} />
                ) : (
                  <div style={{ width: 32, height: 32, borderRadius: 6, background: "var(--paper-2, #FAFBFF)", border: "1px dashed var(--line, #E2E8F0)" }} />
                )}
              </div>

              <div className="min-w-0">
                <div className="flex min-w-0 items-center gap-1.5">
                  <div className="truncate" style={{ fontSize: 13.5, fontWeight: 600, color: "var(--ink, #0F172A)" }}>{r.name}</div>
                  {r.hasUnreadReply && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-[#3B82F6]" aria-label="New reply" />}
                </div>
                <div className="mt-0.5 flex items-center gap-2">
                  {r.email && <span className="truncate font-mono text-[10.5px] text-ink-3">{r.email}</span>}
                  {linkedIn && (
                    <a
                      href={linkedIn}
                      target="_blank"
                      rel="noopener noreferrer"
                      onClick={(e) => e.stopPropagation()}
                      title="View LinkedIn profile"
                      aria-label="View LinkedIn profile"
                      className="inline-flex shrink-0 border-b border-[#CBD5E1] pb-px text-[#64748B] transition-colors hover:border-[#64748B]"
                    >
                      <Linkedin className="h-4 w-4" />
                    </a>
                  )}
                </div>
              </div>

              <div className="min-w-0 truncate text-[12px] text-ink-2">{r.company || " - "}</div>
              <div className="min-w-0 truncate text-[12px] text-ink-2">{r.role || " - "}</div>
              <div className="min-w-0 truncate text-[12px] text-ink-2">{r.school || " - "}</div>
              <div><StagePill contact={r} /></div>
              <div className="text-[12px] text-ink-3">{formatDate(r.createdAt)}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
