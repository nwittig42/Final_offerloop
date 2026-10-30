// Compact job card for the Browse Jobs home — used in both the horizontal
// category carousels and the Handshake-style grid. Consumes the shared
// ProtoJob shape (via feedJobToProto), so it stays in lockstep with the rest
// of the job UI. Width is 100% of its container: the carousel wraps each card
// in a fixed-width slot, the grid lays them out with CSS grid.
//
// Card actions (all stopPropagation so they don't open the detail drawer):
//   • Save (bookmark, top-right)
//   • Find contacts (team leader, interviewer, recruiter)
//   • Auto-apply when the job's ATS supports it, otherwise Apply (opens the
//     posting in a new tab)
import React, { useMemo, useState } from "react";
import type { ProtoJob } from "@/pages/jobBoardAdapter";
import {
  IconBookmark,
  IconLocation,
  IconRecruiterSearch,
  IconExternalLink,
  IconRocket,
} from "@/components/jobs/icons";

// One handler bundle threaded down from BrowseJobsPage through the row/grid so
// each card can trigger the shared modals / flows.
export interface BrowseCardActions {
  onOpen: (job: ProtoJob) => void;
  onToggleSave: (job: ProtoJob) => void;
  onFindContacts: (job: ProtoJob) => void;
  onAutoApply: (job: ProtoJob) => void;
}

interface BrowseJobCardProps {
  job: ProtoJob;
  saved: boolean;
  autoApplyBusy?: boolean;
  actions: BrowseCardActions;
}

export const BrowseJobCard: React.FC<BrowseJobCardProps> = ({
  job,
  saved,
  autoApplyBusy,
  actions,
}) => {
  // Stop the card's own click (which opens the detail drawer) when an action
  // button is pressed.
  const act = (fn: () => void) => (e: React.MouseEvent) => {
    e.stopPropagation();
    fn();
  };

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => actions.onOpen(job)}
      onKeyDown={(e) => {
        if (e.key === "Enter") actions.onOpen(job);
      }}
      className="browse-card"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 10,
        width: "100%",
        height: "100%",
        textAlign: "left",
        padding: 16,
        borderRadius: 14,
        border: "1px solid var(--line, #E5E5E5)",
        background: "var(--paper, #fff)",
        cursor: "pointer",
        fontFamily: "inherit",
        transition: "box-shadow .15s, border-color .15s",
      }}
      onMouseEnter={(e) => {
        const el = e.currentTarget;
        el.style.boxShadow = "0 6px 20px rgba(15,23,42,0.08)";
        el.style.borderColor = "var(--brand-blue, #3B82F6)";
      }}
      onMouseLeave={(e) => {
        const el = e.currentTarget;
        el.style.boxShadow = "none";
        el.style.borderColor = "var(--line, #E5E5E5)";
      }}
    >
      {/* Header: company logo + title + bookmark */}
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
        <CompanyLogo company={job.company} logoUrl={job.logoUrl} monogram={job.logoMonogram} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              fontWeight: 600,
              fontSize: 14,
              color: "var(--ink-1, #0F172A)",
              lineHeight: 1.3,
              display: "-webkit-box",
              WebkitLineClamp: 2,
              WebkitBoxOrient: "vertical",
              overflow: "hidden",
            }}
          >
            {job.title}
          </div>
          <div
            style={{
              fontSize: 13,
              color: "var(--ink-2, #475569)",
              marginTop: 2,
              whiteSpace: "nowrap",
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
          >
            {job.company}
          </div>
        </div>
        <button
          type="button"
          aria-label={saved ? "Remove bookmark" : "Save job"}
          title={saved ? "Saved" : "Save job"}
          onClick={act(() => actions.onToggleSave(job))}
          style={{
            flexShrink: 0,
            display: "inline-flex",
            padding: 4,
            border: "none",
            background: "none",
            borderRadius: 6,
            color: saved ? "var(--brand-blue, #3B82F6)" : "var(--ink-3, #94A3B8)",
            cursor: "pointer",
          }}
        >
          <IconBookmark filled={saved} color={saved ? "#3B82F6" : "currentColor"} />
        </button>
      </div>

      {/* Meta row */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          fontSize: 12,
          color: "var(--ink-3, #94A3B8)",
          flexWrap: "wrap",
        }}
      >
        {job.location && (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 3 }}>
            <IconLocation />
            <span
              style={{
                maxWidth: 140,
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {job.location}
            </span>
          </span>
        )}
        {job.jobType && <span>· {job.jobType}</span>}
        {job.posted && <span>· {job.posted}</span>}
      </div>

      {/* Badges: salary + match */}
      {(job.salary || job.match != null) && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          {job.salary && (
            <span style={{ fontSize: 12, fontWeight: 600, color: "#16A34A" }}>{job.salary}</span>
          )}
          {job.match != null && (
            <span
              style={{
                fontSize: 11,
                fontWeight: 600,
                padding: "2px 8px",
                borderRadius: 999,
                background: "var(--primary-50, #EEF1F9)",
                color: "var(--accent, #4A60A8)",
              }}
            >
              {job.match}% match
            </span>
          )}
        </div>
      )}

      {/* Action row */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          marginTop: "auto",
          paddingTop: 4,
          flexWrap: "wrap",
        }}
      >
        <CardAction
          icon={<IconRecruiterSearch />}
          label="Find contacts"
          title="Find the people to reach for this role"
          onClick={act(() => actions.onFindContacts(job))}
        />
        {job.autoApplyEligible ? (
          <CardAction
            icon={<IconRocket />}
            label={autoApplyBusy ? "Applying…" : "Auto-apply"}
            title="Let Offerloop apply for you"
            variant="primary"
            disabled={autoApplyBusy}
            onClick={act(() => actions.onAutoApply(job))}
          />
        ) : (
          <CardAction
            icon={<IconExternalLink />}
            label="Apply"
            title="Open the job posting in a new tab"
            variant="secondary"
            href={job.applyUrl || undefined}
            disabled={!job.applyUrl}
            onClick={(e) => e.stopPropagation()}
          />
        )}
      </div>
    </div>
  );
};

// Company logo with a self-healing fallback chain so every card shows a real
// logo whenever one can be found:
//   1. employer_logo from the feed (when present)
//   2. Google favicon service for a domain guessed off the company name
//      (returns 404 for unknown domains, which advances the chain)
//   3. DuckDuckGo icon service for the same domain
//   4. the monogram letter (only if every image source fails)
const LOGO_NOISE =
  /\b(inc|llc|ltd|corp|corporation|co|company|group|holdings|technologies|technology|labs|solutions|the)\b/gi;

function guessLogoDomain(company: string): string | null {
  const slug = (company || "")
    .toLowerCase()
    .replace(LOGO_NOISE, "")
    .replace(/[^a-z0-9]/g, "");
  return slug ? `${slug}.com` : null;
}

const CompanyLogo: React.FC<{ company: string; logoUrl: string | null; monogram: string }> = ({
  company,
  logoUrl,
  monogram,
}) => {
  const candidates = useMemo(() => {
    const list: string[] = [];
    if (logoUrl) list.push(logoUrl);
    const domain = guessLogoDomain(company);
    if (domain) {
      list.push(`https://www.google.com/s2/favicons?domain=${domain}&sz=128`);
      list.push(`https://icons.duckduckgo.com/ip3/${domain}.ico`);
    }
    return list;
  }, [company, logoUrl]);

  const [idx, setIdx] = useState(0);
  const src = candidates[idx];

  return (
    <div
      style={{
        width: 44,
        height: 44,
        borderRadius: 10,
        flexShrink: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "var(--paper-2, #F1F5F9)",
        overflow: "hidden",
        fontWeight: 700,
        fontSize: 16,
        color: "var(--ink-2, #475569)",
      }}
    >
      {src ? (
        <img
          src={src}
          alt={company}
          loading="lazy"
          style={{ width: "100%", height: "100%", objectFit: "contain" }}
          onError={() => setIdx((i) => i + 1)}
        />
      ) : (
        monogram
      )}
    </div>
  );
};

// default: quiet outline. primary: filled brand blue (Auto-apply).
// secondary: dark-ink outline (Apply), so it never reads as Auto-apply.
const CARD_ACTION_COLORS = {
  default: { border: "var(--line, #E5E5E5)", background: "var(--paper, #fff)", color: "var(--ink-2, #475569)" },
  primary: { border: "var(--brand-blue, #3B82F6)", background: "var(--brand-blue, #3B82F6)", color: "#fff" },
  secondary: { border: "var(--ink-1, #0F172A)", background: "var(--paper, #fff)", color: "var(--ink-1, #0F172A)" },
} as const;

const CardAction: React.FC<{
  icon: React.ReactNode;
  label: string;
  title: string;
  variant?: keyof typeof CARD_ACTION_COLORS;
  disabled?: boolean;
  // When set (and not disabled), renders a link that opens in a new tab.
  href?: string;
  onClick: (e: React.MouseEvent) => void;
}> = ({ icon, label, title, variant = "default", disabled, href, onClick }) => {
  const colors = CARD_ACTION_COLORS[variant];
  const style: React.CSSProperties = {
    display: "inline-flex",
    alignItems: "center",
    gap: 4,
    padding: "5px 9px",
    borderRadius: 8,
    fontSize: 11,
    fontWeight: 600,
    fontFamily: "inherit",
    textDecoration: "none",
    cursor: disabled ? "default" : "pointer",
    border: `1px solid ${colors.border}`,
    background: colors.background,
    color: colors.color,
    opacity: disabled ? 0.6 : 1,
  };
  const content = (
    <>
      <span style={{ display: "inline-flex", width: 13, height: 13 }}>{icon}</span>
      {label}
    </>
  );
  if (href && !disabled) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" title={title} aria-label={title} onClick={onClick} style={style}>
        {content}
      </a>
    );
  }
  return (
    <button type="button" title={title} aria-label={title} disabled={disabled} onClick={onClick} style={style}>
      {content}
    </button>
  );
};

export default BrowseJobCard;
