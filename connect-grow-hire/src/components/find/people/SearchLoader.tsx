// Shown while a people search runs: a green ring sweeping around the
// Offerloop mark, a status line that steps through what the search is doing,
// and one shimmering placeholder card per person asked for.
import { useEffect, useState } from "react";
import offerloopIcon from "@/assets/offerloopiconlogo.png";
import { APPLE_FONT } from "./PersonCard";

const GREEN = "#01754F";

// Roughly the order the backend works in; timed, not reported by it.
const STEPS = [
  "Reading your search",
  "Scanning millions of profiles",
  "Matching your filters",
  "Finding verified emails",
  "Saving them to your Contacts",
];
const STEP_MS = 2600;

const KEYFRAMES = `
@keyframes ol-ring-spin { to { transform: rotate(360deg); } }
@keyframes ol-ring-pulse { 0%, 100% { opacity: .35; transform: scale(1); } 50% { opacity: .7; transform: scale(1.08); } }
@keyframes ol-shimmer { 0% { background-position: -320px 0; } 100% { background-position: 320px 0; } }
@keyframes ol-step-in { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@media (prefers-reduced-motion: reduce) {
  .ol-ring, .ol-pulse, .ol-shim { animation: none !important; }
}
`;

function ShimmerCard({ delay }: { delay: number }) {
  const bar = "ol-shim rounded-full";
  const shim: React.CSSProperties = {
    background: "linear-gradient(90deg, #EEF6F1 0%, #DCEFE4 40%, #EEF6F1 80%)",
    backgroundSize: "640px 100%",
    animation: `ol-shimmer 1.6s linear ${delay}ms infinite`,
  };
  return (
    <div
      className="h-[244px] rounded-[22px] bg-white p-[18px]"
      style={{ boxShadow: `0 0 0 1.5px ${GREEN}33, 0 4px 14px rgba(1,117,79,0.06)` }}
    >
      <div className="flex gap-3">
        <div className="ol-shim h-[52px] w-[52px] rounded-full" style={shim} />
        <div className="flex-1 space-y-2 pt-1.5">
          <div className={`${bar} h-3.5 w-2/3`} style={shim} />
          <div className={`${bar} h-3 w-1/2`} style={shim} />
        </div>
      </div>
      <div className="ol-shim mt-5 h-8 w-3/4 rounded-lg" style={shim} />
      <div className={`${bar} mt-3 h-6 w-1/2`} style={shim} />
    </div>
  );
}

export function SearchLoader({ count, grid, onCancel }: { count: number; grid: string; onCancel: () => void }) {
  const [step, setStep] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setStep((s) => Math.min(s + 1, STEPS.length - 1)), STEP_MS);
    return () => clearInterval(t);
  }, []);

  return (
    <div style={{ fontFamily: APPLE_FONT }}>
      <style>{KEYFRAMES}</style>
      <div className="mb-6 flex flex-col items-center py-6" role="status" aria-live="polite">
        <div className="relative h-[96px] w-[96px]">
          {/* soft glow */}
          <div className="ol-pulse absolute inset-0 rounded-full" style={{ background: `radial-gradient(circle, ${GREEN}40 0%, transparent 70%)`, animation: "ol-ring-pulse 2.4s ease-in-out infinite" }} />
          {/* sweeping ring: a conic gradient masked to a 5px band */}
          <div
            className="ol-ring absolute inset-[6px] rounded-full"
            style={{
              background: `conic-gradient(from 0deg, transparent 0deg, #34C77B33 90deg, #34C77B 250deg, ${GREEN} 350deg, transparent 360deg)`,
              WebkitMask: "radial-gradient(farthest-side, transparent calc(100% - 5px), #000 calc(100% - 4px))",
              mask: "radial-gradient(farthest-side, transparent calc(100% - 5px), #000 calc(100% - 4px))",
              animation: "ol-ring-spin 1.1s linear infinite",
            }}
          />
          <div className="absolute inset-[18px] flex items-center justify-center rounded-full bg-white shadow-[0_2px_10px_rgba(0,0,0,0.06)]">
            <img src={offerloopIcon} alt="" aria-hidden className="h-8 w-8 object-contain" />
          </div>
        </div>
        <div key={step} className="mt-4 text-[15px] font-medium text-[#1D1D1F]" style={{ animation: "ol-step-in .35s ease-out" }}>
          {STEPS[step]}…
        </div>
        <div className="mt-1 text-[12.5px] text-[#86868B]">
          Finding {count} {count === 1 ? "person" : "people"}. This usually takes 10 to 20 seconds.
        </div>
        <div className="mt-3 flex gap-1.5" aria-hidden>
          {STEPS.map((_, i) => (
            <span key={i} className="h-1.5 rounded-full transition-all duration-500" style={{ width: i === step ? 18 : 6, background: i <= step ? GREEN : "#E0E0E5" }} />
          ))}
        </div>
        <button
          type="button"
          onClick={onCancel}
          className="mt-4 inline-flex h-8 items-center gap-2 rounded-full bg-white px-3.5 text-[13px] font-medium text-[#1D1D1F] shadow-[0_1px_2px_rgba(0,0,0,0.06)] hover:bg-[#FAFAFA]"
        >
          Cancel search
          <kbd className="rounded bg-[#F5F5F7] px-1.5 py-px font-sans text-[11px] text-[#86868B]">Esc</kbd>
        </button>
      </div>
      <div className={grid}>
        {Array.from({ length: count }).map((_, i) => <ShimmerCard key={i} delay={i * 120} />)}
      </div>
    </div>
  );
}
