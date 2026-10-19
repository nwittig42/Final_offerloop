import { formatTimeAgo } from "@/lib/formatters";
import { PROTO_STAGES, PROTO_STAGE_LABELS, type ProtoStage } from "@/pages/trackerAdapter";

// Clickable pipeline. Visually live (hover glow, cursor pointer) but
// onStageClick is stubbed at the page level in PR1: it surfaces a toast,
// no patchOutboxStage write. The 11-to-5 mapping is read-only bucketing,
// so writes are deferred to the production-swap PR.

interface ProtoPipelineDotsProps {
  activeStage: ProtoStage | null;
  onStageClick: (stage: ProtoStage) => void;
  // Most recent open, for the Opened dot tooltip.
  openedAt?: string | null;
}

export function ProtoPipelineDots({ activeStage, onStageClick, openedAt }: ProtoPipelineDotsProps) {
  const activeIdx = activeStage ? PROTO_STAGES.indexOf(activeStage) : -1;
  const n = PROTO_STAGES.length;
  const fillPct = activeIdx >= 0 ? (activeIdx / (n - 1)) * 100 : 0;
  // Dots are equal-width flex cells, so the track runs from the first dot
  // centre to the last: half a cell inset on each side.
  const inset = `${50 / n}%`;

  return (
    <div className="pipeline-wrap">
      <div className="pipeline-dots-row">
        <div className="pipeline-track" style={{ left: inset, right: inset }}>
          <div className="pipeline-track-fill" style={{ width: `${fillPct}%` }} />
        </div>
        {PROTO_STAGES.map((stage, i) => {
          const done = activeIdx >= 0 && i < activeIdx;
          const active = i === activeIdx;
          const cls = `pipeline-dot${done ? " done" : ""}${active ? " active" : ""}`;
          // Opened is set only by the tracking pixel, so its dot is never a target.
          const locked = stage === "opened";
          const tip = locked ? (openedAt ? `Opened ${formatTimeAgo(openedAt)}` : "Set automatically when the email is opened") : undefined;
          return (
            <div key={stage} className="pipeline-dot-wrap">
              <div
                className={cls}
                {...(locked ? { "data-locked": "" } : { "data-clickable": "" })}
                role="button"
                tabIndex={locked ? -1 : 0}
                aria-disabled={locked || undefined}
                aria-label={locked ? "Opened (set automatically)" : `Move to ${PROTO_STAGE_LABELS[stage]}`}
                title={tip}
                onClick={locked ? undefined : () => onStageClick(stage)}
                onKeyDown={locked ? undefined : (e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onStageClick(stage);
                  }
                }}
              >
                {done && (
                  <svg viewBox="0 0 12 12" fill="none" xmlns="http://www.w3.org/2000/svg">
                    <path d="M10 3L4.5 8.5L2 6" stroke="white" strokeWidth="1.5" />
                  </svg>
                )}
              </div>
            </div>
          );
        })}
      </div>
      <div className="pipeline-stage-labels">
        {PROTO_STAGES.map((stage, i) => {
          // When nothing is selected (activeIdx === -1), no label is muted.
          const muted = activeIdx >= 0 && i > activeIdx;
          return (
            <span key={stage} className={`pipeline-stage-name${muted ? " muted" : ""}`}>
              {PROTO_STAGE_LABELS[stage]}
            </span>
          );
        })}
      </div>
    </div>
  );
}
