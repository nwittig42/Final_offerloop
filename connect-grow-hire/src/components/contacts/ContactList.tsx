// Left column: search, sort, group-by-company, and the people list. Rows show
// the company logo, name, role · company, stage, and an unread-reply dot.
import { useEffect, useMemo } from "react";
import { Download, Layers, Plus, Search, Share2 } from "lucide-react";
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

function Row({ row, selected, onSelect }: { row: ContactRow; selected: boolean; onSelect: () => void }) {
  return (
    <button
      type="button"
      data-contact-id={row.id}
      onClick={onSelect}
      className={`mx-2 flex w-[calc(100%-16px)] items-center gap-3 rounded-lg px-2.5 py-2 text-left transition-colors ${
        selected ? "bg-[var(--accent,#4A60A8)] text-white" : "hover:bg-paper-2"
      }`}
    >
      <CompanyLogo company={row.company || row.name} size={32} rounded={8} />
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className={`truncate text-[14px] ${row.hasUnreadReply ? "font-semibold" : "font-medium"}`}>{row.name}</span>
          {row.hasUnreadReply && (
            <span className={`h-2 w-2 shrink-0 rounded-full ${selected ? "bg-white" : "bg-[#3B82F6]"}`} aria-label="Unread reply" />
          )}
        </span>
        <span className={`block truncate text-[12px] ${selected ? "text-white/80" : "text-ink-3"}`}>
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
      <div className="space-y-2 px-3 pb-2 pt-3">
        <div className="flex h-9 items-center gap-2 rounded-lg border border-line bg-white px-3">
          <Search className="h-3.5 w-3.5 text-ink-3" />
          <input
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
            placeholder="Search"
            aria-label="Search contacts"
            className="flex-1 bg-transparent text-[14px] text-ink outline-none placeholder:text-ink-3"
          />
        </div>
        <div className="flex items-center gap-1.5">
          <select
            value={props.filter}
            onChange={(e) => props.onFilterChange(e.target.value as ListFilter)}
            aria-label="Show"
            className="h-7 flex-1 rounded-md border border-line bg-paper-2/60 px-2 text-[12px] text-ink outline-none"
          >
            <option value="all">All contacts</option>
            <option value="saved">Saved ({props.savedCount})</option>
          </select>
          <select
            value={sortDir}
            onChange={(e) => onSortDirChange(e.target.value as SortDir)}
            aria-label="Sort"
            className="h-7 flex-1 rounded-md border border-line bg-paper-2/60 px-2 text-[12px] text-ink outline-none"
          >
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
          </select>
          <button
            type="button"
            onClick={onToggleGroup}
            aria-pressed={groupByCompany}
            className={`inline-flex h-7 items-center gap-1 rounded-md border px-2 text-[12px] ${
              groupByCompany ? "border-[#64748B] bg-[#64748B]/10 text-ink" : "border-line bg-paper-2/60 text-ink"
            }`}
          >
            <Layers className="h-3.5 w-3.5" /> By company
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto pb-2" data-tour="tour-contacts-list">
        {rows.length === 0 ? (
          <p className="px-5 py-8 text-center text-[13px] text-ink-3">
            {query ? "No contacts match your search." : "No contacts yet. Find people on Search, or add one with +."}
          </p>
        ) : groups ? (
          groups.map(([company, people]) => (
            <div key={company} className="mb-1">
              <div className="mx-4 mb-1 mt-3 flex items-baseline justify-between border-b border-line pb-1">
                <span className="truncate text-[12px] font-semibold text-ink-2">{company}</span>
                <span className="text-[11px] text-ink-3">{people.length}</span>
              </div>
              {people.map((r) => <Row key={r.id} row={r} selected={r.id === selectedId} onSelect={() => onSelect(r.id)} />)}
            </div>
          ))
        ) : (
          rows.map((r) => <Row key={r.id} row={r} selected={r.id === selectedId} onSelect={() => onSelect(r.id)} />)
        )}
      </div>

      {/* Bottom toolbar, like macOS Contacts */}
      <div className="flex items-center justify-between border-t border-line px-3 py-2">
        <button type="button" onClick={props.onAdd} title="Add person" aria-label="Add person"
          className="flex h-8 w-8 items-center justify-center rounded-md border border-line bg-white hover:bg-paper-2">
          <Plus className="h-4 w-4" />
        </button>
        <span className="text-[11.5px] text-ink-3">{rows.length} {rows.length === 1 ? "contact" : "contacts"}</span>
        <div className="flex gap-1.5">
          <button type="button" onClick={props.onExport} title="Export CSV" aria-label="Export CSV"
            className="flex h-8 w-8 items-center justify-center rounded-md border border-line bg-white hover:bg-paper-2">
            <Download className="h-4 w-4" />
          </button>
          <button type="button" onClick={props.onShareList} title="Share list" aria-label="Share list"
            className="flex h-8 w-8 items-center justify-center rounded-md border border-line bg-white hover:bg-paper-2">
            <Share2 className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
