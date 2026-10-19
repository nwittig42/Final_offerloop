// Search bar + LinkedIn-style filter pills. Each pill opens a small popover
// (type to add, pick from suggestions, Reset / Show results), "All filters"
// opens every group in a side sheet. Filters are the same dimensions the
// backend prompt parser understands (types/findFilters.ts).
import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowRight, ChevronDown, Plus, X } from "lucide-react";
import { Link } from "react-router-dom";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { EMPTY_PEOPLE_FILTERS, peopleFiltersActive, type PeopleFilters } from "@/types/findFilters";
import { companies } from "@/data/companies";
import { UNIVERSITIES } from "@/data/universities";
import { industries } from "@/data/industries";
import offerloopIcon from "@/assets/offerloopiconlogo.png";
import type { Org } from "@/services/api";
import { orgLogo } from "@/lib/orgLogos";
import { getUniversityShortName } from "@/lib/universityUtils";
import { APPLE_FONT } from "./PersonCard";

const COMPANY_NAMES = companies.map((c) => c.name);
const INDUSTRY_NAMES = industries.map((i) => i.name);

// Organizations are toggle pills (one per joined org), not free-text groups.
type Key = Exclude<keyof PeopleFilters, "organizations">;

const GROUPS: { key: Key; label: string; placeholder: string; suggestions: string[] }[] = [
  { key: "locations", label: "Locations", placeholder: "Add a location", suggestions: ["New York, NY", "San Francisco, CA", "Los Angeles, CA", "Chicago, IL", "Boston, MA", "Seattle, WA", "Washington, DC"] },
  { key: "companies", label: "Current companies", placeholder: "Add a company", suggestions: COMPANY_NAMES },
  { key: "schools", label: "Schools", placeholder: "Add a school", suggestions: UNIVERSITIES },
  { key: "industries", label: "Industries", placeholder: "Add an industry", suggestions: INDUSTRY_NAMES },
  { key: "titles", label: "Job titles", placeholder: "Add a job title", suggestions: ["Analyst", "Associate", "Consultant", "Software Engineer", "Product Manager", "Recruiter", "Vice President"] },
];

const MAX_VALUES = 5;

// Example searches typed out (then erased) in the empty box, so it's clear
// you describe who you want rather than type a name. Uses the student's school.
function exampleSearches(school: string | null): string[] {
  const s = getUniversityShortName(school) || "USC";
  return [
    `Analysts at Goldman Sachs who went to ${s}`,
    "Consultants at McKinsey in Los Angeles",
    `${s} alumni working in private equity`,
    "Recruiters who hire interns at Deloitte",
    "Product managers at Google in San Francisco",
  ];
}

// Placeholder typewriter: types an example, holds, erases, moves on.
// Static first example when the user prefers reduced motion.
function useTypedPlaceholder(examples: string[], active: boolean): string {
  const [text, setText] = useState(examples[0] ?? "");
  const reduce = useMemo(() => typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches, []);
  useEffect(() => {
    if (!active || reduce || examples.length === 0) { setText(examples[0] ?? ""); return; }
    let i = 0, j = 0, deleting = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = () => {
      const full = examples[i];
      setText(full.slice(0, j));
      if (!deleting && j < full.length) { j++; timer = setTimeout(tick, 42); }
      else if (!deleting) { deleting = true; timer = setTimeout(tick, 1800); }
      else if (j > 0) { j--; timer = setTimeout(tick, 16); }
      else { deleting = false; i = (i + 1) % examples.length; timer = setTimeout(tick, 350); }
    };
    tick();
    return () => clearTimeout(timer);
  }, [active, reduce, examples]);
  return text;
}

// "Jim Pallotta" style input: 2-3 capitalized words, nothing descriptive.
const DESCRIPTIVE = /\b(at|in|from|who|with|alumni|alum|grads?|analysts?|associates?|consultants?|recruiters?|engineers?|managers?|vps?|directors?|partners?|interns?|people|students|founders?)\b/i;
function looksLikeName(v: string): boolean {
  const t = v.trim();
  if (!t || DESCRIPTIVE.test(t)) return false;
  const words = t.split(/\s+/);
  return words.length >= 2 && words.length <= 3 && words.every((w) => /^[A-Z][a-z'’.-]+$/.test(w));
}

// Chip list + text input with a filtered suggestion list. Controlled.
function TagEditor({ values, onChange, placeholder, suggestions }: {
  values: string[]; onChange: (v: string[]) => void; placeholder: string; suggestions: string[];
}) {
  const [text, setText] = useState("");
  const q = text.trim().toLowerCase();
  const matches = q
    ? suggestions.filter((s) => s.toLowerCase().includes(q) && !values.includes(s)).slice(0, 6)
    : [];
  const add = (v: string) => {
    const t = v.trim();
    if (!t || values.some((x) => x.toLowerCase() === t.toLowerCase())) { setText(""); return; }
    onChange([...values, t].slice(0, MAX_VALUES));
    setText("");
  };
  return (
    <div>
      {values.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {values.map((v) => (
            <span key={v} className="inline-flex items-center gap-1 rounded-full bg-[#E8F3EE] px-2.5 py-1 text-[12.5px] font-medium text-[#01754F]">
              {v}
              <button type="button" onClick={() => onChange(values.filter((x) => x !== v))} aria-label={`Remove ${v}`}>
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
        </div>
      )}
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); add(matches[0] && text.length > 1 && matches[0].toLowerCase().startsWith(q) ? matches[0] : text); } }}
        placeholder={values.length >= MAX_VALUES ? "Limit reached" : placeholder}
        disabled={values.length >= MAX_VALUES}
        className="h-9 w-full rounded-md border border-line px-3 text-[13.5px] outline-none focus:border-[var(--accent,#4A60A8)]"
      />
      {matches.length > 0 && (
        <div className="mt-1 overflow-hidden rounded-md border border-line">
          {matches.map((m) => (
            <button key={m} type="button" onClick={() => add(m)} className="block w-full truncate px-3 py-1.5 text-left text-[13px] hover:bg-paper-2">
              {m}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function pillClass(active: boolean) {
  return `inline-flex h-9 shrink-0 items-center gap-1.5 rounded-full border px-4 text-[14px] font-semibold transition-colors ${
    active
      ? "border-[#01754F] bg-[#01754F] text-white hover:bg-[#015c3e]"
      : "border-[#A3A3A3] bg-white text-ink-2 hover:border-ink-2 hover:bg-paper-2"
  }`;
}

// One dropdown pill. Edits a draft copy; nothing applies until Show results.
function FilterPill({ group, values, onApply }: { group: typeof GROUPS[number]; values: string[]; onApply: (v: string[]) => void }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(values);
  useEffect(() => { if (open) setDraft(values); }, [open, values]);
  const label = values.length === 0 ? group.label : values.length === 1 ? values[0] : `${group.label} · ${values.length}`;
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button type="button" className={pillClass(values.length > 0)}>
          <span className="max-w-[160px] truncate">{label}</span>
          <ChevronDown className="h-4 w-4" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 p-4">
        <TagEditor values={draft} onChange={setDraft} placeholder={group.placeholder} suggestions={group.suggestions} />
        <div className="mt-4 flex justify-end gap-2 border-t border-line pt-3">
          <button type="button" onClick={() => setDraft([])} className="h-8 rounded-full px-3 text-[13px] font-semibold text-ink-2 hover:bg-paper-2">
            Reset
          </button>
          <button
            type="button"
            onClick={() => { onApply(draft); setOpen(false); }}
            className="h-8 rounded-full bg-[#0A66C2] px-4 text-[13px] font-semibold text-white hover:bg-[#004182]"
          >
            Show results
          </button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

interface PeopleSearchBarProps {
  query: string;
  onQueryChange: (q: string) => void;
  filters: PeopleFilters;
  // Applying a filter or submitting runs the search with these values.
  onSearch: (query: string, filters: PeopleFilters) => void;
  userSchool: string | null;
  // Org alumni directories the student has joined (OFF-42).
  orgs?: Org[];
  // Not in any org yet: a "Join organization" pill sits where the org pill
  // would be, linking to Account Settings (where the password is entered).
  showJoinOrg?: boolean;
  searching: boolean;
}

export function PeopleSearchBar({ query, onQueryChange, filters, onSearch, userSchool, orgs = [], showJoinOrg = false, searching }: PeopleSearchBarProps) {
  const [allOpen, setAllOpen] = useState(false);
  const [allDraft, setAllDraft] = useState<PeopleFilters>(filters);
  useEffect(() => { if (allOpen) setAllDraft(filters); }, [allOpen, filters]);

  const apply = (next: PeopleFilters) => onSearch(query, next);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const examples = useMemo(() => exampleSearches(userSchool), [userSchool]);
  // Keep cycling examples while the box is empty, focused or not.
  const typed = useTypedPlaceholder(examples, !query);
  const nameLike = looksLikeName(query);
  const alumniOn = !!userSchool && filters.schools.some((s) => s.toLowerCase() === userSchool.toLowerCase());
  const toggleAlumni = () => {
    if (!userSchool) return;
    apply({
      ...filters,
      schools: alumniOn
        ? filters.schools.filter((s) => s.toLowerCase() !== userSchool.toLowerCase())
        : [...filters.schools, userSchool].slice(0, MAX_VALUES),
    });
  };

  const toggleOrg = (id: string) =>
    apply({
      ...filters,
      organizations: filters.organizations.includes(id)
        ? filters.organizations.filter((x) => x !== id)
        : [...filters.organizations, id],
    });

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <img src={offerloopIcon} alt="Offerloop" className="h-9 w-9 shrink-0 object-contain" />
        <div className="text-[28px] font-semibold leading-tight tracking-[-0.02em] text-[#1D1D1F]" style={{ fontFamily: APPLE_FONT }}>
          Who do you want to meet?
        </div>
      </div>

      {/* Composer: reads like writing a message, not a name lookup. */}
      <form
        onSubmit={(e) => { e.preventDefault(); onSearch(query, filters); }}
        className="rounded-[22px] bg-white px-5 pb-3 pt-4 shadow-[0_1px_2px_rgba(0,0,0,0.04),0_4px_14px_rgba(0,0,0,0.05)] ring-1 ring-transparent transition-shadow focus-within:ring-[#0071E3]/40"
        style={{ fontFamily: APPLE_FONT }}
      >
        <textarea
          ref={inputRef}
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
          onKeyDown={(e) => {
            // Enter runs the search; Shift+Enter adds a line.
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); onSearch(query, filters); }
          }}
          rows={2}
          placeholder={typed}
          aria-label="Describe who you want to meet"
          className="block w-full resize-none bg-transparent text-[17px] leading-[1.45] text-[#1D1D1F] outline-none placeholder:text-[#86868B]"
        />
        <div className="mt-2 flex items-center justify-between gap-3">
          <span className="truncate text-[12.5px] text-[#86868B]">
            Describe them: role, company, school, city. Any mix works.
          </span>
          <div className="flex shrink-0 items-center gap-2">
            {query && (
              <button type="button" onClick={() => { onQueryChange(""); inputRef.current?.focus(); }} aria-label="Clear" className="flex h-8 w-8 items-center justify-center rounded-full text-[#86868B] hover:bg-[#F5F5F7]">
                <X className="h-4 w-4" />
              </button>
            )}
            <button
              type="submit"
              disabled={searching || (!query.trim() && !peopleFiltersActive(filters))}
              className="inline-flex h-9 items-center gap-1.5 rounded-full bg-[#0071E3] px-4 text-[14px] font-medium text-white transition-colors hover:bg-[#0077ED] disabled:bg-[#F5F5F7] disabled:text-[#AEAEB2]"
            >
              {searching ? "Finding people…" : "Find people"}
              {!searching && <ArrowRight className="h-4 w-4" strokeWidth={2.4} />}
            </button>
          </div>
        </div>
      </form>

      {nameLike && (
        <p role="status" className="px-1 text-[13px] text-[#6E6E73]" style={{ fontFamily: APPLE_FONT }}>
          Looking for one specific person? We find people by description, so try their role and company instead, like
          {" "}<span className="text-[#1D1D1F]">"VPs at Goldman Sachs who went to {getUniversityShortName(userSchool) || "USC"}"</span>.
        </p>
      )}

      <div className="flex items-center gap-2 overflow-x-auto pb-1">
        <span className={pillClass(true)} aria-hidden>
          People
        </span>
        <span className="mx-1 h-7 w-px shrink-0 bg-line" />
        {userSchool && (
          <button type="button" onClick={toggleAlumni} aria-pressed={alumniOn} className={pillClass(alumniOn)}>
            Alumni
          </button>
        )}
        {showJoinOrg && (
          <Link
            to="/account-settings#organizations"
            title="Add your fraternity, sorority, club or team to search its alumni"
            className="inline-flex h-9 shrink-0 items-center gap-1.5 rounded-full border border-dashed border-[#A3A3A3] bg-white px-4 text-[14px] font-semibold text-ink-2 transition-colors hover:border-ink-2 hover:bg-paper-2"
          >
            <Plus className="h-4 w-4" /> Join organization
          </Link>
        )}
        {orgs.map((o) => {
          const on = filters.organizations.includes(o.id);
          return (
            <button key={o.id} type="button" onClick={() => toggleOrg(o.id)} aria-pressed={on} className={pillClass(on)}>
              {orgLogo(o.id) && <img src={orgLogo(o.id)!} alt="" aria-hidden className="h-[18px] w-[18px] object-contain" />}
              {o.shortName} alumni
            </button>
          );
        })}
        {GROUPS.map((g) => (
          <FilterPill key={g.key} group={g} values={filters[g.key]} onApply={(v) => apply({ ...filters, [g.key]: v })} />
        ))}
        <button type="button" onClick={() => setAllOpen(true)} className="h-9 shrink-0 rounded-full px-3 text-[14px] font-semibold text-ink-2 hover:bg-paper-2">
          All filters
        </button>
        {peopleFiltersActive(filters) && (
          <button type="button" onClick={() => apply(EMPTY_PEOPLE_FILTERS)} className="h-9 shrink-0 px-2 text-[13px] text-ink-3 hover:text-ink-2 hover:underline">
            Reset
          </button>
        )}
      </div>

      <Sheet open={allOpen} onOpenChange={setAllOpen}>
        <SheetContent side="right" className="flex w-[380px] flex-col p-0 sm:max-w-[380px]">
          <SheetHeader className="border-b border-line px-5 py-4">
            <SheetTitle>All filters</SheetTitle>
          </SheetHeader>
          <div className="flex-1 space-y-6 overflow-y-auto px-5 py-5">
            {GROUPS.map((g) => (
              <section key={g.key}>
                <h4 className="mb-2 text-[15px] font-semibold text-ink">{g.label}</h4>
                <TagEditor
                  values={allDraft[g.key]}
                  onChange={(v) => setAllDraft((d) => ({ ...d, [g.key]: v }))}
                  placeholder={g.placeholder}
                  suggestions={g.suggestions}
                />
              </section>
            ))}
          </div>
          <div className="flex justify-end gap-2 border-t border-line px-5 py-3">
            <button type="button" onClick={() => setAllDraft(EMPTY_PEOPLE_FILTERS)} className="h-9 rounded-full px-4 text-[13.5px] font-semibold text-ink-2 hover:bg-paper-2">
              Reset
            </button>
            <button
              type="button"
              onClick={() => { apply(allDraft); setAllOpen(false); }}
              className="h-9 rounded-full bg-[#0A66C2] px-5 text-[13.5px] font-semibold text-white hover:bg-[#004182]"
            >
              Show results
            </button>
          </div>
        </SheetContent>
      </Sheet>
    </div>
  );
}
