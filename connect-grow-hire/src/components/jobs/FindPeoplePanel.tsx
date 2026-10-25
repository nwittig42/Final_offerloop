import { useState } from "react";
import {
  IconLock,
  IconPerson,
  IconRocket,
  IconSearchSmall,
} from "./icons";

// Inline Find People panel that lives inside JobDetail. Two stacked actions:
//   1. Find contacts -> onFind() (the /find-contacts flow: team leader, likely
//      interviewer and recruiter, run in FindContactsModal).
//   2. Find People -> onFindEmployees(count) (the /find-employee flow), with a
//      1 to 5 quantity slider for how many teammates to surface.
// Costs mirror the backend: contacts 5/person (RECRUITER_CREDIT_COST, charged
// once per job, so the modal handles a short balance), employee 4/contact.
// Find contacts is open to every tier, same as the job card button; Find People
// is gated to non-free tiers. The employee search runs in FindHumansModal.

const CONTACT_CREDITS = 5;   // find_contacts, per person
const EMPLOYEE_CREDITS = 4;  // find_employee
const MIN_PEOPLE = 1;
const MAX_PEOPLE = 5;
const DEFAULT_PEOPLE = 3;

interface FindPeoplePanelProps {
  userPlan?: "free" | "pro" | "elite" | "premium";
  currentCredits?: number;
  onFind: () => void;
  onFindEmployees?: (count: number) => void;
  onUpgradeClick?: () => void;
}

export function FindPeoplePanel({
  userPlan = "free",
  currentCredits = 0,
  onFind,
  onFindEmployees,
  onUpgradeClick,
}: FindPeoplePanelProps) {
  const hasPremiumAccess = userPlan !== "free";
  const notEnoughEmployee = hasPremiumAccess && currentCredits < EMPLOYEE_CREDITS;
  const [count, setCount] = useState(DEFAULT_PEOPLE);

  // Filled portion of the slider track, in percent, so the navy fill grows as
  // the count rises. The shared .slider-custom track is transparent, so without
  // this the slider is invisible on the white panel.
  const sliderPct = ((count - MIN_PEOPLE) / (MAX_PEOPLE - MIN_PEOPLE)) * 100;

  const peopleLabel = !hasPremiumAccess
    ? "Upgrade to Find People"
    : notEnoughEmployee
      ? "Not enough credits"
      : "Find People";

  return (
    <div className="jb-fp" data-tour="tour-job-board-find-people">
      <div>
        <h3 className="jb-fp-eyebrow">FIND PEOPLE</h3>
      </div>

      {/* Two actions side by side. Columns stretch to equal height and each
          action block is pinned to the bottom (see .jb-fp-col-action) so the two
          buttons share a baseline even though the right column is taller. The
          row wraps to stacked when the pane is too narrow for two columns. */}
      <div className="jb-fp-actions">
        {/* Left column: the hiring team (leader, interviewer, recruiter) */}
        <div className="jb-fp-col">
          <div className="jb-fp-role">
            <div className="jb-fp-role-meta">
              <div className="jb-fp-role-head">
                <span className="jb-fp-role-icon"><IconRocket /></span>
                <span className="jb-fp-role-name">Hiring team</span>
              </div>
              <div className="jb-fp-role-credits">Team leader, likely interviewer and recruiter</div>
            </div>
          </div>

          <div className="jb-fp-col-action">
            <button className="jb-fp-cta" type="button" onClick={onFind}>
              <IconSearchSmall />
              Find contacts
            </button>
            <span style={{ fontSize: 12, color: "var(--ink-7)", textAlign: "center" }}>
              {CONTACT_CREDITS} credits each
            </span>
          </div>
        </div>

        {/* Right column: People on the team. Slider sits above the button and
            fills the extra vertical space the left column does not have. */}
        <div className="jb-fp-col">
          <div className="jb-fp-role">
            <div className="jb-fp-role-meta">
              <div className="jb-fp-role-head">
                <span className="jb-fp-role-icon"><IconPerson /></span>
                <span className="jb-fp-role-name">People on the team</span>
                <span className="jb-fp-premium-tag">Premium</span>
              </div>
              <div className="jb-fp-role-credits">Peers and teammates worth a coffee chat</div>
            </div>
            {!hasPremiumAccess && (
              <span className="jb-fp-counter locked"><IconLock /></span>
            )}
          </div>

          {hasPremiumAccess && (
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <input
                type="range"
                className="slider-custom"
                min={MIN_PEOPLE}
                max={MAX_PEOPLE}
                step={1}
                value={count}
                onChange={(e) => setCount(Number(e.target.value))}
                aria-label="Number of people to find"
                style={{
                  flex: 1,
                  background: `linear-gradient(to right, var(--ink-2) 0%, var(--ink-2) ${sliderPct}%, #E2E8F0 ${sliderPct}%, #E2E8F0 100%)`,
                }}
              />
              <span
                style={{
                  fontSize: 14,
                  fontWeight: 600,
                  color: "var(--ink-2)",
                  minWidth: 18,
                  textAlign: "right",
                }}
              >
                {count}
              </span>
            </div>
          )}

          <div className="jb-fp-col-action">
            <button
              className="jb-fp-cta"
              type="button"
              disabled={notEnoughEmployee}
              onClick={() => {
                if (!hasPremiumAccess) {
                  onUpgradeClick?.();
                  return;
                }
                if (!notEnoughEmployee) onFindEmployees?.(count);
              }}
            >
              <IconSearchSmall />
              {peopleLabel}
            </button>
            <span style={{ fontSize: 12, color: "var(--ink-7)", textAlign: "center" }}>
              {EMPLOYEE_CREDITS} credits each
            </span>
          </div>
        </div>
      </div>

      {!hasPremiumAccess && (
        <p style={{ fontSize: 12, color: "var(--ink-5)", margin: 0 }}>
          Finding people on the team requires a Premium plan.{" "}
          <button
            type="button"
            onClick={onUpgradeClick}
            style={{
              background: "none",
              border: "none",
              color: "var(--slate)",
              cursor: "pointer",
              fontSize: 12,
              fontWeight: 500,
              padding: 0,
            }}
          >
            Upgrade
          </button>
        </p>
      )}
    </div>
  );
}
