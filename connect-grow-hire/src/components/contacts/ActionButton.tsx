// Pill button (icon + label inside) for the action row under the contact
// name. "primary" is the filled main action; the rest are outlined so the row
// reads as real, clickable buttons.
import type { ReactNode } from "react";

interface ActionButtonProps {
  icon: ReactNode;
  label: string;
  onClick?: () => void;
  href?: string | null;
  disabled?: boolean;
  title?: string;
  tone?: "default" | "primary" | "success";
}

export function ActionButton({ icon, label, onClick, href, disabled, title, tone = "default" }: ActionButtonProps) {
  const look = disabled
    ? "cursor-not-allowed border-line bg-paper-2 text-ink-3"
    : tone === "primary"
      ? "border-[#1e2d4d] bg-[#1e2d4d] text-white shadow-[0_1px_2px_rgba(15,23,42,0.18)] hover:bg-[#2a3d63]"
      : tone === "success"
        ? "border-emerald-600 bg-emerald-600 text-white hover:bg-emerald-700"
        : "border-[#D4D4D4] bg-white text-ink hover:border-slate-400 hover:bg-slate-50";
  const cls = `inline-flex h-10 max-w-[220px] items-center gap-2 rounded-full border px-4 text-[13.5px] font-semibold transition-colors active:translate-y-px ${look}`;
  const inner = (
    <>
      <span className="shrink-0">{icon}</span>
      <span className="truncate">{label}</span>
    </>
  );

  if (href && !disabled) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" className={cls} title={title}>
        {inner}
      </a>
    );
  }
  return (
    <button type="button" onClick={disabled ? undefined : onClick} disabled={disabled} className={cls} title={title}>
      {inner}
    </button>
  );
}
