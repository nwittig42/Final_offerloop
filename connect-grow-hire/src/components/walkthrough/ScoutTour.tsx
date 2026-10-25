import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode, type SyntheticEvent } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { OB } from "@/pages/onboardingTheme";
import scoutYeti from "@/assets/scouts/scout-yeti-full.png";

// The first-email tour's overlay: the page stays visible under a dim layer
// with a rounded cutout around the real element the step is about, and the
// Scout yeti stands beside it with a speech bubble. It's presentational; the
// tour hook decides the target, copy and actions.
//
// Clicks: four transparent rectangles around the cutout swallow clicks, so
// only the spotlighted element (and the bubble) can be used. The dimming
// itself is the ring's huge box-shadow, which gives the cutout round corners
// and never catches pointer events.

export interface ScoutTourProps {
  /** CSS selector, e.g. '[data-tour="tour-first-cards"]'; null = centered bubble, no cutout. */
  target: string | null;
  title: string;
  body: ReactNode;
  action?: { label: string; onClick: () => void; disabled?: boolean };
  /** e.g. "Not now". */
  secondary?: { label: string; onClick: () => void };
  /** "Skip tour". */
  onSkip: () => void;
  /** ✕ and Escape. */
  onClose: () => void;
}

interface Box {
  top: number;
  left: number;
  width: number;
  height: number;
}

type Placement = "right" | "left" | "below" | "above" | "corner" | "bottom" | "center";

const PAD = 8;
const RADIUS = 16;
const WAIT_MS = 5000;
const NARROW = 640;
const GAP = 16;
const EDGE = 16;
const YETI_W = 140;
const BUBBLE_W = 340;
const GROUP_W = YETI_W + 8 + BUBBLE_W;
const GROUP_H_GUESS = 240;
const DIM = "rgba(15,23,42,0.45)";

const sameBox = (a: Box | null, b: Box | null) =>
  a === b || (!!a && !!b && a.top === b.top && a.left === b.left && a.width === b.width && a.height === b.height);

const clamp = (v: number, min: number, max: number) => Math.max(min, Math.min(v, max));

/** Where the yeti + bubble go, given the cutout and the viewport. */
function place(box: Box | null, vw: number, vh: number, groupH: number): { placement: Placement; style: CSSProperties } {
  if (vw < NARROW) return { placement: "bottom", style: { left: EDGE, right: EDGE, bottom: EDGE } };
  if (!box) return { placement: "center", style: {} };

  const right = vw - (box.left + box.width);
  const left = box.left;
  const below = vh - (box.top + box.height);
  const above = box.top;
  const centeredTop = clamp(box.top + box.height / 2 - groupH / 2, EDGE, Math.max(EDGE, vh - groupH - EDGE));
  const centeredLeft = clamp(box.left + box.width / 2 - GROUP_W / 2, EDGE, Math.max(EDGE, vw - GROUP_W - EDGE));

  if (Math.max(right, left) >= GROUP_W + GAP + EDGE) {
    return right >= left
      ? { placement: "right", style: { top: centeredTop, left: box.left + box.width + GAP } }
      : { placement: "left", style: { top: centeredTop, left: box.left - GAP - GROUP_W } };
  }
  if (Math.max(below, above) >= groupH + GAP + EDGE) {
    return below >= above
      ? { placement: "below", style: { top: box.top + box.height + GAP, left: centeredLeft } }
      : { placement: "above", style: { top: box.top - GAP - groupH, left: centeredLeft } };
  }
  // The target fills the screen (e.g. a whole conversation): tuck Scout into the corner.
  return { placement: "corner", style: { right: EDGE + 8, bottom: EDGE + 8 } };
}

export function ScoutTour({ target, title, body, action, secondary, onSkip, onClose }: ScoutTourProps) {
  const [box, setBox] = useState<Box | null>(null);
  const [timedOut, setTimedOut] = useState(false);
  const [viewport, setViewport] = useState(() => ({ w: window.innerWidth, h: window.innerHeight }));
  const [groupH, setGroupH] = useState(GROUP_H_GUESS);
  const groupRef = useRef<HTMLDivElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  // Find and measure the target; re-run on scroll, resize and DOM changes.
  // While it's missing, wait up to WAIT_MS, then fall back to a centered bubble.
  useLayoutEffect(() => {
    setBox(null);
    setTimedOut(false);
    if (!target) return;

    let timer: ReturnType<typeof setTimeout> | null = null;
    let frame: number | null = null;
    let scrolledTo: Element | null = null;

    const measure = () => {
      setViewport((v) => (v.w === window.innerWidth && v.h === window.innerHeight ? v : { w: window.innerWidth, h: window.innerHeight }));
      const el = document.querySelector(target);
      if (!el) {
        setBox(null);
        if (!timer) timer = setTimeout(() => setTimedOut(true), WAIT_MS);
        return;
      }
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      setTimedOut(false);
      const r = el.getBoundingClientRect();
      if (scrolledTo !== el) {
        scrolledTo = el;
        if (r.top < 0 || r.bottom > window.innerHeight) el.scrollIntoView?.({ block: "center", behavior: "smooth" });
      }
      const next = { top: r.top - PAD, left: r.left - PAD, width: r.width + PAD * 2, height: r.height + PAD * 2 };
      setBox((prev) => (sameBox(prev, next) ? prev : next));
    };
    const schedule = () => {
      if (typeof requestAnimationFrame !== "function") return measure();
      if (frame !== null) return;
      frame = requestAnimationFrame(() => {
        frame = null;
        measure();
      });
    };

    measure();
    window.addEventListener("scroll", schedule, true);
    window.addEventListener("resize", schedule);
    const observer = new MutationObserver(schedule);
    observer.observe(document.body, { childList: true, subtree: true, attributes: true, characterData: true });
    return () => {
      window.removeEventListener("scroll", schedule, true);
      window.removeEventListener("resize", schedule);
      observer.disconnect();
      if (timer) clearTimeout(timer);
      if (frame !== null && typeof cancelAnimationFrame === "function") cancelAnimationFrame(frame);
    };
  }, [target]);

  // Untargeted steps still follow the viewport size (narrow vs. wide layout).
  useEffect(() => {
    if (target) return;
    const onResize = () => setViewport({ w: window.innerWidth, h: window.innerHeight });
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [target]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const visible = !target || !!box || timedOut;
  const spot = target && !timedOut ? box : null;

  // Keep the real height of the yeti + bubble for placement math.
  useLayoutEffect(() => {
    const h = groupRef.current?.offsetHeight;
    if (h && h !== groupH) setGroupH(h);
  });

  // Move focus into the bubble when it first shows (and when the step changes).
  useEffect(() => {
    if (visible) dialogRef.current?.focus({ preventScroll: true });
  }, [visible, title]);

  const swallow = useCallback((e: SyntheticEvent) => {
    e.preventDefault();
    e.stopPropagation();
  }, []);

  if (!visible) return null;

  const { placement, style: groupStyle } = place(spot, viewport.w, viewport.h, groupH);
  const narrow = placement === "bottom";
  const bubbleFirst = placement === "left";

  const dimRect = (style: CSSProperties, key: string) => (
    <div
      key={key}
      data-testid="scout-tour-dim"
      aria-hidden="true"
      onClick={swallow}
      onMouseDown={swallow}
      onPointerDown={swallow}
      style={{ position: "fixed", pointerEvents: "auto", ...style }}
    />
  );

  const dims = spot
    ? [
        dimRect({ top: 0, left: 0, right: 0, height: Math.max(0, spot.top) }, "top"),
        dimRect({ top: spot.top + spot.height, left: 0, right: 0, bottom: 0 }, "bottom"),
        dimRect({ top: spot.top, height: spot.height, left: 0, width: Math.max(0, spot.left) }, "left"),
        dimRect({ top: spot.top, height: spot.height, left: spot.left + spot.width, right: 0 }, "right"),
      ]
    : [dimRect({ inset: 0, background: DIM }, "all")];

  const yeti = (
    <img
      src={scoutYeti}
      alt="Scout"
      draggable={false}
      style={{
        width: narrow ? 84 : YETI_W,
        height: "auto",
        flexShrink: 0,
        userSelect: "none",
        transform: bubbleFirst ? "scaleX(-1)" : undefined,
        filter: "drop-shadow(0 10px 18px rgba(15,23,42,0.25))",
      }}
    />
  );

  // The bubble's tail points at Scout, who's doing the talking.
  const tail = (
    <span
      aria-hidden="true"
      style={{
        position: "absolute",
        bottom: narrow ? 28 : 40,
        [bubbleFirst ? "right" : "left"]: -7,
        width: 14,
        height: 14,
        background: "#fff",
        transform: "rotate(45deg)",
        ...(bubbleFirst
          ? { borderTop: `1px solid ${OB.border}`, borderRight: `1px solid ${OB.border}` }
          : { borderBottom: `1px solid ${OB.border}`, borderLeft: `1px solid ${OB.border}` }),
      }}
    />
  );

  const bubble = (
    <div
      ref={dialogRef}
      role="dialog"
      aria-label={title}
      aria-modal="false"
      tabIndex={-1}
      data-placement={placement}
      style={{
        position: "relative",
        width: narrow ? undefined : BUBBLE_W,
        flex: narrow ? 1 : undefined,
        minWidth: 0,
        background: "#fff",
        border: `1px solid ${OB.border}`,
        borderRadius: 18,
        padding: narrow ? "16px 16px 14px" : "20px 20px 16px",
        boxShadow: "0 18px 48px rgba(15,23,42,0.20), 0 2px 6px rgba(15,23,42,0.06)",
        fontFamily: OB.fontBody,
        outline: "none",
      }}
    >
      {tail}
      <button
        type="button"
        onClick={onClose}
        aria-label="Close tour"
        className="absolute right-2.5 top-2.5 rounded-full p-1.5 transition-colors hover:bg-[#F1F4FA]"
        style={{ color: OB.ink3 }}
      >
        <X className="h-4 w-4" />
      </button>
      <div
        style={{
          fontSize: 11,
          fontWeight: 600,
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          color: OB.primary,
          marginBottom: 6,
        }}
      >
        Scout
      </div>
      <h2
        style={{
          fontFamily: OB.fontDisplay,
          fontSize: narrow ? 18 : 20,
          fontWeight: 600,
          lineHeight: 1.25,
          color: OB.heading,
          margin: "0 28px 8px 0",
        }}
      >
        {title}
      </h2>
      <div style={{ fontSize: 14.5, lineHeight: 1.55, color: OB.ink2 }}>{body}</div>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 16 }}>
        <button
          type="button"
          onClick={onSkip}
          className="rounded-md px-1 py-1 text-[13px] transition-colors hover:text-[#1E2D4D]"
          style={{ color: OB.ink3, marginRight: "auto", background: "none", border: "none" }}
        >
          Skip tour
        </button>
        {secondary && (
          <button
            type="button"
            onClick={secondary.onClick}
            className="rounded-[10px] px-3.5 text-[14px] font-semibold transition-colors hover:bg-[#EEF1F9]"
            style={{ height: 44, color: OB.primary, background: "transparent", border: "none" }}
          >
            {secondary.label}
          </button>
        )}
        {action && (
          <button
            type="button"
            onClick={action.onClick}
            disabled={action.disabled}
            className="rounded-[10px] px-[18px] text-[14px] font-semibold text-white transition-colors hover:bg-[#3C4F8E] disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-[#4A60A8]"
            style={{ height: 44, minWidth: 44, background: OB.primary, border: "none", boxShadow: OB.shadowBlue }}
          >
            {action.label}
          </button>
        )}
      </div>
    </div>
  );

  const group = (
    <div
      ref={groupRef}
      key={target ?? "center"}
      // Presses on Scout aren't "outside" clicks for whatever popover is open underneath.
      onPointerDown={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
      className="motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-2 motion-safe:duration-300"
      style={{
        display: "flex",
        alignItems: "flex-end",
        gap: 8,
        pointerEvents: "auto",
        ...(placement === "center"
          ? { position: "relative", maxWidth: `calc(100vw - ${EDGE * 2}px)` }
          : { position: "fixed", ...groupStyle }),
      }}
    >
      {bubbleFirst ? (
        <>
          {bubble}
          {yeti}
        </>
      ) : (
        <>
          {yeti}
          {bubble}
        </>
      )}
    </div>
  );

  return createPortal(
    <div style={{ position: "fixed", inset: 0, zIndex: 1000, pointerEvents: "none" }}>
      {dims}
      {spot && (
        <div
          data-testid="scout-tour-ring"
          aria-hidden="true"
          className="motion-safe:animate-in motion-safe:fade-in motion-safe:duration-300"
          style={{
            position: "fixed",
            top: spot.top,
            left: spot.left,
            width: spot.width,
            height: spot.height,
            borderRadius: RADIUS,
            pointerEvents: "none",
            boxShadow: `0 0 0 2px ${OB.primary200}, 0 0 0 6px rgba(182,195,232,0.35), 0 0 0 9999px ${DIM}`,
            transition: "top .25s cubic-bezier(0.16,1,0.3,1), left .25s cubic-bezier(0.16,1,0.3,1), width .25s cubic-bezier(0.16,1,0.3,1), height .25s cubic-bezier(0.16,1,0.3,1)",
          }}
        />
      )}
      {placement === "center" ? (
        <div style={{ position: "fixed", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", pointerEvents: "none" }}>
          {group}
        </div>
      ) : (
        group
      )}
    </div>,
    document.body,
  );
}
