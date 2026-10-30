// Left column: search, sort, group-by-company, and the people list. Rows show
// the company logo, name, role · company, stage, and an unread-reply dot.
import { useEffect, useMemo } from "react";
import { ChevronDown, Download, Layers, Plus, Search, Share2 } from "lucide-react";
import { CompanyLogo } from "@/components/CompanyLogo";
import type { ContactRow } from "./contactsModel";
import { StagePill } from "./StagePill";

export type SortDir = "newest" | "oldest";
// "saved" = bookmarked or saved but never emailed: the shortlist.
export type ListFilter = "all" | "saved";

interface ContactListProps {
  rows: ContactRow[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  query: string;
  onQueryChange: (q: string) => void;
  sortDir: SortDir;
  onSortDirChange: (d: SortDir) => void;
  filter: ListFilter;
  onFilterChange: (f: ListFilter) => void;
  savedCount: number;
  groupByCompany: boolean;
  onToggleGroup: () => void;
  onAdd: () => void;
  onExport: () => void;
  onShareList: () => void;
}

// A native select dressed as a compact gray pill with its own chevron, so it
// keeps the OS menu and keyboard behavior.
function PillSelect({ value, onChange, label, options }: {
  value: string;
  onChange: (v: string) => void;
  label: string;
  options: [string, string][];
}) {
  return (
    <span className="relative inline-flex shrink-0">
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={label}
        className="h-7 cursor-pointer appearance-none whitespace-nowrap rounded-full bg-[rgba(118,118,128,0.12)] pl-2.5 pr-6 text-[12.5px] font-medium text-[#1d1d1f] outline-none hover:bg-[rgba(118,118,128,0.18)] focus-visible:ring-2 focus-visible:ring-[#0071e3]/40"
      >
        {options.map(([v, text]) => <option key={v} value={v}>{text}</option>)}
      </select>
      <ChevronDown className="pointer-events-none absolute right-2 top-1/2 h-3 w-3 -translate-y-1/2 text-[#6e6e73]" strokeWidth={2.5} />
    </span>
  );
}

function Row({ row, selected, onSelect }: { row: ContactRow; selected: boolean; onSelect: () => void }) {
  return (
    <button
      type="button"
      data-contact-id={row.id}
      onClick={onSelect}
      className={`group mx-2 flex w-[calc(100%-16px)] items-center gap-3 rounded-[10px] px-2.5 py-2 text-left transition-colors ${
        selected ? "bg-[#0071e3] text-white" : "hover:bg-black/[0.04]"
      }`}
    >
      <CompanyLogo company={row.company || row.name} size={36} rounded={18} />
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className={`truncate text-[14px] tracking-[-0.01em] ${row.hasUnreadReply ? "font-semibold" : "font-medium"}`}>{row.name}</span>
          {row.hasUnreadReply && (
            <span className={`h-2 w-2 shrink-0 rounded-full ${selected ? "bg-white" : "bg-[#0071e3]"}`} aria-label="Unread reply" />
          )}
        </span>
        <span className={`block truncate text-[12.5px] ${selected ? "text-white/80" : "text-[#6e6e73]"}`}>
          {[row.role, row.company].filter(Boolean).join(" · ") || row.email || " "}
        </span>
      </span>
      {selected ? (
        <span className="inline-flex shrink-0 items-center gap-1 rounded-full bg-white/20 px-2 py-0.5 text-[11px] font-medium text-white">
          <span className="h-1.5 w-1.5 rounded-full bg-white" />
          {row.stageLabel}
        </span>
      ) : (
        <StagePill contact={row} />
      )}
    </button>
  );
}

export function ContactList(props: ContactListProps) {
  const { rows, selectedId, onSelect, query, onQueryChange, sortDir, onSortDirChange, groupByCompany, onToggleGroup } = props;

  const groups = useMemo(() => {
    if (!groupByCompany) return null;
    const map = new Map<string, ContactRow[]>();
    for (const r of rows) {
      const key = r.company.trim() || "No company";
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(r);
    }
    return [...map.entries()].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]));
  }, [rows, groupByCompany]);

  // Keep the selected row on screen after deep-links and group toggles.
  useEffect(() => {
    if (!selectedId) return;
    document.querySelector(`[data-contact-id="${selectedId}"]`)?.scrollIntoView({ block: "nearest" });
  }, [selectedId, groupByCompany]);

  return (
    <div className="flex h-full flex-col">
      <div className="space-y-2 px-3 pb-2 pt-2">
        {/* Apple Music-style search: filled gray, no border. */}
        <label className="flex h-8 items-center gap-1.5 rounded-[10px] bg-[rgba(118,118,128,0.12)] px-2.5 focus-within:ring-2 focus-within:ring-[#0071e3]/40">
          <Search className="h-4 w-4 shrink-0 text-[#8e8e93]" strokeWidth={2.25} />
          <input
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
            placeholder="Search"
            aria-label="Search contacts"
            className="min-w-0 flex-1 bg-transparent text-[14px] text-[#1d1d1f] outline-none placeholder:text-[#8e8e93]"
          />
        </label>
        <div className="flex items-center gap-1.5">
          <PillSelect
            value={props.filter}
            onChange={(v) => props.onFilterChange(v as ListFilter)}
            label="Show"
            options={[["all", "All"], ["saved", "Saved"]]}
          />
          <PillSelect
            value={sortDir}
            onChange={(v) => onSortDirChange(v as SortDir)}
            label="Sort"
            options={[["newest", "Newest"], ["oldest", "Oldest"]]}
          />
          <button
            type="button"
            onClick={onToggleGroup}
            aria-pressed={groupByCompany}
            className={`inline-flex h-7 shrink-0 items-center gap-1 whitespace-nowrap rounded-full px-2.5 text-[12.5px] font-medium transition-colors ${
              groupByCompany ? "bg-[#0071e3] text-white" : "bg-[rgba(118,118,128,0.12)] text-[#1d1d1f] hover:bg-[rgba(118,118,128,0.18)]"
            }`}
          >
            <Layers className="h-3.5 w-3.5" /> By company
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto pb-2" data-tour="tour-contacts-list">
        {rows.length === 0 ? (
          <p className="px-5 py-8 text-center text-[13px] text-[#8e8e93]">
            {query ? "No contacts match your search." : "No contacts yet. Find people on Search, or add one with +."}
          </p>
        ) : groups ? (
          groups.map(([company, people]) => (
            <div key={company} className="mb-1">
              <div className="sticky top-0 z-10 flex items-baseline justify-between bg-white px-5 pb-1 pt-3">
                <span className="truncate text-[12.5px] font-semibold text-[#6e6e73]">{company}</span>
                <span className="text-[12px] text-[#8e8e93]">{people.length}</span>
              </div>
              {people.map((r) => <Row key={r.id} row={r} selected={r.id === selectedId} onSelect={() => onSelect(r.id)} />)}
            </div>
          ))
        ) : (
          rows.map((r) => <Row key={r.id} row={r} selected={r.id === selectedId} onSelect={() => onSelect(r.id)} />)
        )}
      </div>

      {/* Bottom toolbar, like macOS Contacts */}
      <div className="flex items-center justify-between border-t border-black/[0.06] px-2 py-1.5">
        <button type="button" onClick={props.onAdd} title="Add person" aria-label="Add person"
          className="flex h-8 w-8 items-center justify-center rounded-[8px] text-[#6e6e73] hover:bg-black/[0.05] hover:text-[#1d1d1f]">
          <Plus className="h-4 w-4" />
        </button>
        <span className="text-[12px] text-[#8e8e93]">{rows.length} {rows.length === 1 ? "contact" : "contacts"}</span>
        <div className="flex gap-1.5">
          <button type="button" onClick={props.onExport} title="Export CSV" aria-label="Export CSV"
            className="flex h-8 w-8 items-center justify-center rounded-[8px] text-[#6e6e73] hover:bg-black/[0.05] hover:text-[#1d1d1f]">
            <Download className="h-4 w-4" />
          </button>
          <button type="button" onClick={props.onShareList} title="Share list" aria-label="Share list"
            className="flex h-8 w-8 items-center justify-center rounded-[8px] text-[#6e6e73] hover:bg-black/[0.05] hover:text-[#1d1d1f]">
            <Share2 className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
