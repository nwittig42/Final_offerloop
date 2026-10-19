// Stage progress on the contact card: an Apple Fitness-style ring that fills
// to the current step (Saved → Offer) in the stage's color, with the six steps
// beside it. Clicking a step moves the contact there. Contacts with no tracked conversation yet are read-only (the backend
// stage endpoint works on outbox threads).
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiService } from "@/services/api";
import { toast } from "@/hooks/use-toast";
import { STAGES, STAGE_LABELS, THREADS_KEY, stageStyle, stageToBackend, type ContactRow, type Stage } from "./contactsModel";

export function StageTracker({ contact }: { contact: ContactRow }) {
  const queryClient = useQueryClient();
  const editable = !!contact.thread;
  const currentIdx = contact.stage ? STAGES.indexOf(contact.stage) : -1;
  const color = stageStyle(contact.stage).dot;

  const mutation = useMutation({
    mutationFn: async (stage: Stage) => {
      const res = stage === "offer"
        ? await apiService.markOutboxThreadWon(contact.id)
        : await apiService.patchOutboxStage(contact.id, stageToBackend(stage));
      if ("error" in res) throw new Error(res.error);
    },
    onSuccess: (_d, stage) => {
      queryClient.invalidateQueries({ queryKey: THREADS_KEY });
      toast({ title: `Moved to ${STAGE_LABELS[stage]}` });
    },
    onError: (e: Error) => toast({ title: "Couldn't update stage", description: e.message, variant: "destructive" }),
  });

  const total = STAGES.length;
  const progress = currentIdx < 0 ? 0 : (currentIdx + 1) / total;
  const next = currentIdx >= 0 && currentIdx < total - 1 ? STAGES[currentIdx + 1] : null;

  return (
    <div className="mt-7">
      <div className="mb-3 flex items-baseline justify-between">
        <span className="text-[11px] font-semibold uppercase tracking-[0.08em] text-ink-3">Stage</span>
        {editable && contact.stage && <span className="text-[11.5px] text-ink-3">Click a step to change it</span>}
      </div>

      <div className="flex items-center gap-5">
        <ProgressRing progress={progress} color={color} step={currentIdx + 1} total={total} />

        <div className="min-w-0 flex-1">
          <div className="text-[17px] font-semibold leading-tight" style={{ color: contact.stage ? color : undefined }}>
            {contact.stage ? STAGE_LABELS[contact.stage] : contact.stageLabel}
          </div>
          <div className="mt-0.5 text-[12.5px] text-ink-3">
            {contact.stage
              ? next ? `Step ${currentIdx + 1} of ${total} · Next: ${STAGE_LABELS[next]}` : "Loop closed. Nice work."
              : "Draft an email to start tracking this conversation"}
          </div>

          <div className="mt-3 flex flex-wrap gap-1.5">
            {STAGES.map((s, i) => {
              const reached = i <= currentIdx;
              const isCurrent = i === currentIdx;
              return (
                <button
                  key={s}
                  type="button"
                  disabled={!editable || mutation.isPending || isCurrent}
                  onClick={() => mutation.mutate(s)}
                  title={editable ? `Move to ${STAGE_LABELS[s]}` : "Draft an email to start tracking this conversation"}
                  className={`inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-[12px] transition-colors ${
                    isCurrent ? "font-semibold" : reached ? "text-ink-2" : "text-ink-3"
                  } ${editable && !isCurrent ? "cursor-pointer hover:border-slate-400 hover:text-ink-2" : "cursor-default"}`}
                  style={{
                    borderColor: isCurrent ? color : "#E2E8F0",
                    background: isCurrent ? `${color}14` : "#fff",
                    color: isCurrent ? color : undefined,
                  }}
                >
                  <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: reached ? color : "#CBD5E1" }} />
                  {STAGE_LABELS[s]}
                </button>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

// Thick, round-capped ring on a faint track of the same color; the arc animates
// when the stage changes.
function ProgressRing({ progress, color, step, total }: { progress: number; color: string; step: number; total: number }) {
  const size = 84;
  const stroke = 11;
  const r = (size - stroke) / 2;
  const circumference = 2 * Math.PI * r;
  const gradientId = `stage-ring-${color.replace("#", "")}`;
  return (
    <div className="relative shrink-0" style={{ width: size, height: size }}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90" aria-hidden>
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="1" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity={0.7} />
            <stop offset="100%" stopColor={color} />
          </linearGradient>
        </defs>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={`${color}26`} strokeWidth={stroke} />
        {progress > 0 && (
          <circle
            cx={size / 2}
            cy={size / 2}
            r={r}
            fill="none"
            stroke={`url(#${gradientId})`}
            strokeWidth={stroke}
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={circumference * (1 - progress)}
            style={{ transition: "stroke-dashoffset 600ms cubic-bezier(.22,1,.36,1)" }}
          />
        )}
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center leading-none">
        <span className="text-[20px] font-semibold text-ink">{Math.max(step, 0)}</span>
        <span className="mt-0.5 text-[10.5px] text-ink-3">of {total}</span>
      </div>
    </div>
  );
}
