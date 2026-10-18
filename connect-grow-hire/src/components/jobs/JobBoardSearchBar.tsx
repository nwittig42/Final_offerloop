// JobBoardSearchBar — the Job Board's header in both the List and Gallery
// views. Same treatment as Find's PeopleSearchBar: Offerloop icon, pill search
// bar with a Search button. Each board keeps its own live search state; the
// button just commits the current text.
import { Search, X } from "lucide-react";
import offerloopIcon from "@/assets/offerloopiconlogo.png";

interface JobBoardSearchBarProps {
  value: string;
  onChange: (value: string) => void;
}

export function JobBoardSearchBar({ value, onChange }: JobBoardSearchBarProps) {
  return (
    <div className="flex items-center gap-4">
      <img src={offerloopIcon} alt="Offerloop" className="h-10 w-10 shrink-0 object-contain" />
      <form
        onSubmit={(e) => { e.preventDefault(); onChange(value.trim()); }}
        className="flex h-12 min-w-0 flex-1 items-center gap-3 rounded-full border border-line bg-white px-5 shadow-[0_1px_2px_rgba(15,23,42,0.05)] focus-within:border-[var(--accent,#4A60A8)]"
      >
        <Search className="h-5 w-5 shrink-0 text-ink-2" />
        <input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder="Search jobs, e.g. summer analyst internships in New York"
          aria-label="Search jobs"
          className="min-w-0 flex-1 bg-transparent text-[15px] text-ink outline-none placeholder:text-ink-3"
        />
        {value && (
          <button type="button" onClick={() => onChange("")} aria-label="Clear search" className="text-ink-3 hover:text-ink-2">
            <X className="h-4 w-4" />
          </button>
        )}
        <button
          type="submit"
          disabled={!value.trim()}
          className="h-8 shrink-0 rounded-full bg-[var(--accent,#4A60A8)] px-4 text-[13px] font-semibold text-white disabled:opacity-40"
        >
          Search
        </button>
      </form>
    </div>
  );
}

// Filter pill styling, copied from Find's PeopleSearchBar so the boards read
// as one product.
export function jobPillClass(active: boolean) {
  return `inline-flex h-9 shrink-0 items-center gap-1.5 rounded-full border px-4 text-[14px] font-semibold transition-colors disabled:opacity-50 ${
    active
      ? "border-[#01754F] bg-[#01754F] text-white hover:bg-[#015c3e]"
      : "border-[#A3A3A3] bg-white text-ink-2 hover:border-ink-2 hover:bg-paper-2"
  }`;
}
