import { useEffect, useRef, useState } from "react";

// Sort options ported verbatim from network-tracker.html (line 1652-1668).
export type SortKey =
  | "name-asc" | "name-desc"
  | "date-newest" | "date-oldest"
  | "company-asc";

const SORT_OPTIONS: { key: SortKey; label: string }[] = [
  { key: "name-asc", label: "Name (A-Z)" },
  { key: "name-desc", label: "Name (Z-A)" },
  { key: "date-newest", label: "Date added (Newest)" },
  { key: "date-oldest", label: "Date added (Oldest)" },
  { key: "company-asc", label: "Company (A-Z)" },
];

// Tracker-relevant filter list. The Job Board prototype's salary / quickApply
// rows are intentionally not ported, they would be dead UI on a contact tracker.
export const FILTER_LABELS: Record<string, string> = {
  "stage:saved": "Saved",
  "stage:contacted": "Contacted",
  "stage:opened": "Opened",
  "stage:connected": "Connected",
  "stage:interviewing": "Interviewing",
  "stage:offer": "Offer",
  "has-reply": "Has unread reply",
};

const STAGE_FILTERS = ["stage:saved", "stage:contacted", "stage:opened", "stage:connected", "stage:interviewing", "stage:offer"];
const SIGNAL_FILTERS = ["has-reply"];

interface MoreFiltersDropdownProps {
  sortKey: SortKey | null;
  activeFilters: Set<string>;
  onSortChange: (key: SortKey | null) => void;
  onFilterToggle: (id: string) => void;
  onClearAll: () => void;
}

export function MoreFiltersDropdown({
  sortKey,
  activeFilters,
  onSortChange,
  onFilterToggle,
  onClearAll,
}: MoreFiltersDropdownProps) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const count = activeFilters.size + (sortKey ? 1 : 0);

  // Click-outside-to-close (proto pattern, line 2550-2552)
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (!containerRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("click", handler);
    return () => document.removeEventListener("click", handler);
  }, [open]);

  return (
    <div className="filter-dropdown" ref={containerRef}>
      <button
        type="button"
        className={`toolbar-pill${count > 0 ? " has-filter" : ""}`}
        aria-haspopup="true"
        aria-expanded={open}
        onClick={(e) => { e.stopPropagation(); setOpen((v) => !v); }}
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
          <path d="M2.66667 14V9.33333" stroke="#475569" strokeWidth="1.33333" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M2.66667 6.66667V2" stroke="#475569" strokeWidth="1.33333" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M8 14V8" stroke="#475569" strokeWidth="1.33333" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M8 5.33333V2" stroke="#475569" strokeWidth="1.33333" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M13.3333 14V10.6667" stroke="#475569" strokeWidth="1.33333" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M13.3333 8V2" stroke="#475569" strokeWidth="1.33333" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M1.33333 9.33333H4" stroke="#475569" strokeWidth="1.33333" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M6.66667 5.33333H9.33333" stroke="#475569" strokeWidth="1.33333" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M12 10.6667H14.6667" stroke="#475569" strokeWidth="1.33333" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        More Filters
        <div className={`filter-badge${count > 0 ? "" : " empty"}`}>{count}</div>
      </button>
      <div className={`filter-menu${open ? " open" : ""}`} role="menu">
        <div className="filter-menu-label">Sort by</div>
        {SORT_OPTIONS.map((opt) => (
          <button
            key={opt.key}
            type="button"
            className={`filter-menu-item${sortKey === opt.key ? " active" : ""}`}
            role="menuitemradio"
            aria-checked={sortKey === opt.key}
            onClick={() => { onSortChange(sortKey === opt.key ? null : opt.key); setOpen(false); }}
          >
            <span>{opt.label}</span>
            <span className="filter-check">✓</span>
          </button>
        ))}

        <div className="filter-menu-divider" />
        <div className="filter-menu-label">Filter by stage</div>
        {STAGE_FILTERS.map((id) => (
          <label key={id} className="filter-checkbox-row">
            <input
              type="checkbox"
              checked={activeFilters.has(id)}
              onChange={() => onFilterToggle(id)}
              data-filter={id}
            />
            <span>{FILTER_LABELS[id]}</span>
          </label>
        ))}

        <div className="filter-menu-divider" />
        <div className="filter-menu-label">Other</div>
        {SIGNAL_FILTERS.map((id) => (
          <label key={id} className="filter-checkbox-row">
            <input
              type="checkbox"
              checked={activeFilters.has(id)}
              onChange={() => onFilterToggle(id)}
              data-filter={id}
            />
            <span>{FILTER_LABELS[id]}</span>
          </label>
        ))}

        <div className="filter-menu-divider" />
        <button
          type="button"
          className="filter-menu-clear"
          onClick={() => { onClearAll(); setOpen(false); }}
        >
          Clear all
        </button>
      </div>
    </div>
  );
}
