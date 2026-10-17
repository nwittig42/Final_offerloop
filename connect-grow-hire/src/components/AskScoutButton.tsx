/**
 * AskScoutButton — paper-pin Ask-Scout button backed by the head-version
 * yeti from src/assets/scouts/scout-yeti-head.png. The same yeti renders on
 * every route; scoutTabForPath() is kept as a public helper because other
 * Scout surfaces still call it, but the SCOUTS map now maps every tab to
 * the same image.
 */

import React from "react";
import { useLocation } from "react-router-dom";
import { useScout } from "@/contexts/ScoutContext";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { useTour } from "@/contexts/TourContext";

import scoutYetiHead from "@/assets/scouts/scout-yeti-head.png";
import pinThumbtack from "@/assets/scouts/pin-thumbtack.png";

export type ScoutTab =
  | "home"
  | "loops"
  | "find"
  | "network"
  | "prep"
  | "outbox"
  | "jobs";

// Single head-version yeti from the Figma badge — same character on every
// surface. Tab keys remain so consumers that still call scoutTabForPath()
// don't have to change, but they all resolve to the same image now.
export const SCOUT_YETI_SRC = scoutYetiHead;
export const SCOUTS: Record<ScoutTab, string> = {
  home: scoutYetiHead,
  loops: scoutYetiHead,
  find: scoutYetiHead,
  network: scoutYetiHead,
  prep: scoutYetiHead,
  outbox: scoutYetiHead,
  jobs: scoutYetiHead,
};

export const PIN_SRC = pinThumbtack;

/**
 * Route -> tab. Every authenticated route resolves to a tab so any Scout
 * surface (button, panel, etc.) can display the matching character. Pages
 * where the floating button itself is suppressed (e.g. /dashboard) still
 * resolve here so the panel can use it when called via Cmd+K.
 */
export function scoutTabForPath(path: string): ScoutTab {
  if (path.startsWith("/agent")) return "loops";
  if (path.startsWith("/find")) return "find";
  if (path.startsWith("/my-network")) return "network";
  if (
    path.startsWith("/coffee-chat-prep") ||
    path.startsWith("/coffee-chat-library")
  ) {
    return "prep";
  }
  if (path.startsWith("/outbox") || path.startsWith("/tracker")) return "outbox";
  if (path.startsWith("/job-board")) return "jobs";
  return "home";
}

interface AskScoutButtonProps {
  /**
   * Which Scout character to show. If omitted, derives from the current
   * pathname via scoutTabForPath.
   */
  tab?: ScoutTab;
  /**
   * Click handler. The floating wrapper wires this to openPanel; embedded
   * call sites supply their own.
   */
  onClick: () => void;
}

/**
 * Paper-card button with a thumbtack pin at the top-right. Render inside a
 * `position: relative` parent if you want the pin to anchor to this exact
 * card (otherwise the absolute pin will fall back to the next positioned
 * ancestor). FloatingAskScoutButton below handles that for you.
 */
export const AskScoutButton: React.FC<AskScoutButtonProps> = ({
  tab,
  onClick,
}) => {
  const location = useLocation();
  const resolved = tab ?? scoutTabForPath(location.pathname);

  return (
    <div style={{ position: "relative", display: "inline-block" }}>
      <button
        onClick={onClick}
        aria-label="Ask Scout"
        className="scout-pin-card"
      >
        <img
          src={SCOUTS[resolved]}
          alt=""
          style={{
            width: 36,
            height: 36,
            objectFit: "contain",
            flexShrink: 0,
          }}
        />
        <span>Ask Scout</span>
      </button>

      {/* Thumbtack sits outside the rotating card so it stays still while the
          card swings from gravity. */}
      <img
        src={PIN_SRC}
        alt=""
        aria-hidden
        className="scout-pin-thumbtack"
      />
    </div>
  );
};

/**
 * Floating wrapper for App.tsx. Pins to the top-right corner and skips
 * routes where Scout is already rendered inline (the page-owned Scout
 * companion would otherwise overlap with this floating pin). Hidden on:
 *   • /dashboard — Scout prompt inline in the hero
 *   • / and /for-students — public landing pages, no pill
 *   • /agent and /agent/** — Loops surfaces have ScoutGuide / inline Scouts
 *   • /onboarding — signup flow; no app chrome until onboarding completes
 */
const HIDE_PILL_ON = new Set(["/", "/for-students"]);
const HIDE_PILL_PREFIXES = ["/agent", "/onboarding"];

const FloatingAskScoutButton: React.FC = () => {
  const { openPanel, isPanelOpen } = useScout();
  const { demoSurface } = useTour();
  const scoutDemoActive = demoSurface === 'scout';
  const location = useLocation();
  const { user } = useFirebaseAuth();

  // Logged-out visitors (public landing / marketing pages) never see the
  // floating Scout pill.
  if (!user) return null;

  // Stay visible during the Scout tour demo even when the panel is open, so
  // the tour's spotlight (anchored on the floating wrapper below) has an
  // element to land on. Outside the demo the original "hide while panel is
  // open" behavior is preserved.
  if (isPanelOpen && !scoutDemoActive) return null;
  if (HIDE_PILL_ON.has(location.pathname)) return null;
  if (HIDE_PILL_PREFIXES.some((p) => location.pathname === p || location.pathname.startsWith(p + "/"))) return null;

  return (
    <div
      data-tour="tour-scout-button"
      style={{
        position: "fixed",
        top: 24,
        right: 30,
        zIndex: 60,
      }}
    >
      <AskScoutButton onClick={openPanel} />
    </div>
  );
};

export default FloatingAskScoutButton;
