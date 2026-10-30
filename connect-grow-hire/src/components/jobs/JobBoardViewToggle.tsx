// Segmented control that switches the Job Board between its two layouts:
//   • "list"    — the personalized two-pane board (JobBoardPage.redesign)
//   • "gallery" — the browse-everything card gallery (BrowseJobsPage)
//
// Rendered in the same spot in both boards so switching is one obvious click.
// The container (JobBoardContainer) owns the actual view state + persistence.
import React from "react";
import { List, LayoutGrid } from "lucide-react";

export type JobBoardView = "list" | "gallery";

interface JobBoardViewToggleProps {
  view: JobBoardView;
  onChange: (view: JobBoardView) => void;
}

export const JobBoardViewToggle: React.FC<JobBoardViewToggleProps> = ({ view, onChange }) => {
  const options: { key: JobBoardView; label: string; Icon: typeof List }[] = [
    { key: "list", label: "List", Icon: List },
    { key: "gallery", label: "Gallery", Icon: LayoutGrid },
  ];

  // Pill-shaped to sit beside the filter pills (same 36px height). A navy
  // thumb slides under the active option instead of each button restyling.
  const activeIndex = options.findIndex((o) => o.key === view);

  return (
    <div
      role="tablist"
      aria-label="Job board view"
      className="relative inline-grid h-9 shrink-0 grid-cols-2 rounded-full border border-[#D4D4D4] bg-white p-[3px]"
    >
      <span
        aria-hidden
        className="absolute bottom-[3px] left-[3px] top-[3px] w-[calc(50%-3px)] rounded-full bg-[#1e2d4d] shadow-[0_1px_2px_rgba(15,23,42,0.18)] transition-transform duration-200 ease-out"
        style={{ transform: `translateX(${activeIndex * 100}%)` }}
      />
      {options.map(({ key, label, Icon }) => {
        const active = view === key;
        return (
          <button
            key={key}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => !active && onChange(key)}
            className={`relative z-10 inline-flex items-center justify-center gap-1.5 rounded-full px-3.5 text-[13px] font-semibold transition-colors duration-200 ${
              active ? "cursor-default text-white" : "cursor-pointer text-[#6B7280] hover:text-[#1e2d4d]"
            }`}
          >
            <Icon size={15} strokeWidth={2} />
            {label}
          </button>
        );
      })}
    </div>
  );
};

export default JobBoardViewToggle;
