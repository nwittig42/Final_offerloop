import { useState } from "react";
import { ArrowRight, Check, GraduationCap } from "lucide-react";
import type { WalkthroughMatch } from "@/services/walkthrough";
import { OB } from "@/pages/onboardingTheme";

// The walkthrough's recommended people as editorial profile cards: vertical,
// A4-proportioned (1:√2), side by side from 900px, a swipeable scroll-snap
// carousel below that. Whole card is the button; hover lifts it, the person
// picked last stays marked as selected. No new dependencies: plain CSS in a
// scoped <style> block (the onboarding pages style inline, but hover, focus,
// breakpoints and scroll-snap need real CSS).

const CSS = `
.wt-gallery { list-style: none; margin: 0 0 22px; padding: 6px 0 14px; display: grid; gap: 18px; }
.wt-gallery > li { min-width: 0; display: flex; }
@media (min-width: 900px) {
  .wt-gallery { grid-template-columns: repeat(var(--wt-cols), minmax(0, 1fr)); }
}
@media (max-width: 899.98px) {
  .wt-gallery {
    display: flex; overflow-x: auto; scroll-snap-type: x mandatory; scroll-padding-inline: 16px;
    margin-inline: -16px; padding-inline: 16px; overscroll-behavior-x: contain; scrollbar-width: none;
  }
  .wt-gallery::-webkit-scrollbar { display: none; }
  .wt-gallery > li { flex: 0 0 min(72vw, 272px); scroll-snap-align: start; }
}
.wt-card {
  position: relative; width: 100%; aspect-ratio: 1 / 1.4142; display: flex; flex-direction: column;
  text-align: left; padding: 12px 12px 14px; border-radius: 16px; cursor: pointer; overflow: hidden;
  background: #fff; border: 1px solid ${OB.border}; font-family: ${OB.fontBody}; color: ${OB.heading};
  box-shadow: 0 1px 2px rgba(15, 23, 42, .04), 0 6px 18px rgba(15, 23, 42, .05);
  transition: transform .22s cubic-bezier(.16,1,.3,1), box-shadow .22s ease, border-color .22s ease;
}
.wt-card:hover:not(:disabled) {
  transform: translateY(-4px); border-color: ${OB.primary200};
  box-shadow: 0 2px 4px rgba(15, 23, 42, .05), 0 18px 40px rgba(37, 51, 90, .14);
}
.wt-card:focus-visible { outline: 3px solid ${OB.primary200}; outline-offset: 3px; }
.wt-card[aria-pressed="true"] {
  border: 2px solid ${OB.primary}; padding: 11px 11px 13px;
  box-shadow: 0 0 0 4px ${OB.primary100}, 0 14px 32px rgba(37, 51, 90, .12);
}
.wt-card:disabled { cursor: default; background: #FAFBFC; }
.wt-card:disabled .wt-portrait { filter: grayscale(1); opacity: .55; }
.wt-card:hover:not(:disabled) .wt-more { gap: 8px; }
@media (prefers-reduced-motion: reduce) {
  .wt-card, .wt-more { transition: none; }
  .wt-card:hover:not(:disabled) { transform: none; }
}
.wt-clamp2 { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.wt-swipe { display: none; }
@media (max-width: 899.98px) { .wt-swipe { display: block; } }
`;

export const initialsOf = (name: string) =>
  name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase())
    .join("");

/**
 * Their photo, or a monogram in the Offerloop palette when there's none (or
 * the LinkedIn hotlink has expired).
 */
export function ProfilePortrait({ match, style }: { match: Pick<WalkthroughMatch, "name" | "photoUrl">; style?: React.CSSProperties }) {
  const [broken, setBroken] = useState(false);
  const frame: React.CSSProperties = {
    position: "relative",
    overflow: "hidden",
    borderRadius: 10,
    background: `linear-gradient(160deg, ${OB.primary50} 0%, ${OB.primary100} 100%)`,
    ...style,
  };
  if (match.photoUrl && !broken) {
    return (
      <div className="wt-portrait" style={frame}>
        <img
          src={match.photoUrl}
          alt=""
          onError={() => setBroken(true)}
          style={{ position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover", objectPosition: "50% 30%" }}
        />
      </div>
    );
  }
  return (
    <div className="wt-portrait" aria-hidden style={{ ...frame, display: "flex", alignItems: "center", justifyContent: "center" }}>
      {/* A hairline inner frame, like a mat around a print. */}
      <span style={{ position: "absolute", inset: 8, border: "1px solid rgba(74,96,168,.18)", borderRadius: 6 }} />
      <span
        style={{
          fontFamily: OB.fontDisplay,
          fontWeight: 600,
          fontSize: "clamp(30px, 3.4vw, 42px)",
          letterSpacing: "0.02em",
          color: OB.primary,
        }}
      >
        {initialsOf(match.name)}
      </span>
    </div>
  );
}

interface MatchCardProps {
  match: WalkthroughMatch;
  index: number;
  reason: string;
  selected: boolean;
  unavailable: boolean;
  onSelect: () => void;
}

function MatchCard({ match, index, reason, selected, unavailable, onSelect }: MatchCardProps) {
  const role = [match.title, match.company].filter(Boolean).join(" at ");
  return (
    <button
      type="button"
      className="wt-card"
      onClick={onSelect}
      disabled={unavailable}
      aria-pressed={selected}
      aria-label={`${match.name}, ${role}`}
    >
      <span style={{ position: "relative", flex: "1 1 auto", minHeight: 96, display: "flex" }}>
        <ProfilePortrait match={match} style={{ flex: 1 }} />
        <span
          style={{
            position: "absolute",
            left: 10,
            bottom: 8,
            fontFamily: OB.fontDisplay,
            fontStyle: "italic",
            fontSize: 12.5,
            color: match.photoUrl ? "#fff" : OB.ink3,
            textShadow: match.photoUrl ? "0 1px 3px rgba(0,0,0,.45)" : "none",
          }}
        >
          No. {String(index + 1).padStart(2, "0")}
        </span>
        {selected && (
          <span
            style={{
              position: "absolute",
              top: 8,
              right: 8,
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              background: OB.primary,
              color: "#fff",
              fontSize: 11.5,
              fontWeight: 700,
              borderRadius: 999,
              padding: "3px 9px 3px 7px",
            }}
          >
            <Check size={12} strokeWidth={3} /> Selected
          </span>
        )}
      </span>

      <span
        className="wt-clamp2"
        style={{ fontFamily: OB.fontDisplay, fontWeight: 600, fontSize: 18.5, lineHeight: 1.2, letterSpacing: "-0.01em", margin: "11px 0 3px" }}
      >
        {match.name}
      </span>
      {/* Role and school keep a fixed height so every portrait in the row is the same size. */}
      <span className="wt-clamp2" style={{ fontSize: 13, color: OB.ink2, lineHeight: 1.4, minHeight: "2.8em" }}>
        {role}
      </span>
      <span
        style={{
          display: "flex",
          alignItems: "center",
          gap: 5,
          fontSize: 12.5,
          color: OB.ink3,
          marginTop: 4,
          minHeight: 18,
          whiteSpace: "nowrap",
          overflow: "hidden",
        }}
      >
        {match.school && (
          <>
            <GraduationCap size={13} style={{ flexShrink: 0 }} aria-hidden />
            <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>{match.school}</span>
          </>
        )}
      </span>

      <span aria-hidden style={{ height: 1, background: OB.border, margin: "8px 0 7px", flexShrink: 0 }} />

      {unavailable ? (
        <span style={{ fontSize: 13, color: OB.ink3, fontWeight: 600 }}>No email found</span>
      ) : (
        <>
          <span style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: "0.1em", textTransform: "uppercase", color: OB.primary, marginBottom: 3 }}>
            Why them
          </span>
          <span className="wt-clamp2" style={{ fontSize: 13, lineHeight: 1.45, color: OB.ink, minHeight: "2.9em" }}>
            {reason}
          </span>
          <span
            className="wt-more"
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
              marginTop: "auto",
              paddingTop: 6,
              fontSize: 12.5,
              fontWeight: 700,
              color: OB.primary,
              transition: "gap .2s ease",
            }}
          >
            View profile <ArrowRight size={13} aria-hidden />
          </span>
        </>
      )}
    </button>
  );
}

interface MatchGalleryProps {
  matches: WalkthroughMatch[];
  reasonFor: (m: WalkthroughMatch) => string;
  selectedId?: string;
  unavailableIds: Set<string>;
  onSelect: (m: WalkthroughMatch) => void;
}

export function MatchGallery({ matches, reasonFor, selectedId, unavailableIds, onSelect }: MatchGalleryProps) {
  return (
    <>
      <style>{CSS}</style>
      <ul
        aria-label="Your matches"
        className="wt-gallery"
        style={{ ["--wt-cols" as string]: String(Math.max(3, Math.min(4, matches.length))) }}
      >
        {matches.map((m, i) => (
          <li key={m.id}>
            <MatchCard
              match={m}
              index={i}
              reason={reasonFor(m)}
              selected={m.id === selectedId}
              unavailable={unavailableIds.has(m.id)}
              onSelect={() => onSelect(m)}
            />
          </li>
        ))}
      </ul>
      {matches.length > 1 && (
        <p className="wt-swipe" style={{ fontSize: 12.5, color: OB.ink3, margin: "-12px 0 16px" }}>
          Swipe to see all {matches.length}
        </p>
      )}
    </>
  );
}
