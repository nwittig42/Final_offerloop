import { useEffect } from "react";
import { Search, PenLine, Send } from "lucide-react";
import { OB, obPrimaryButton } from "./onboardingTheme";
import OfferloopLogo from "@/assets/offerloop_logo2_trimmed.png";

// One concise introduction (replaces the five video slides). It sets up the
// activation goal: from resume to a first personalized email in minutes.
const POINTS = [
  {
    icon: Search,
    title: "We find the right people",
    body: "Alumni from your school and people at the firms you're aiming for.",
  },
  {
    icon: PenLine,
    title: "We write the email",
    body: "A personalized note drawn from your resume. You edit it before anything goes out.",
  },
  {
    icon: Send,
    title: "You send it and track replies",
    body: "Send from your own Gmail and follow every conversation in one place.",
  },
];

interface OnboardingIntroProps {
  onContinue: () => void;
  onBack: () => void;
}

export const OnboardingIntro = ({ onContinue, onBack }: OnboardingIntroProps) => {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Enter") onContinue();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onContinue]);

  return (
    <div
      className="min-h-screen flex flex-col"
      style={{ background: OB.pageBg, color: OB.heading, fontFamily: OB.fontBody }}
    >
      <div className="flex items-center justify-between" style={{ padding: "20px clamp(16px, 5vw, 40px)" }}>
        <img src={OfferloopLogo} alt="Offerloop" style={{ height: 34 }} />
        <button
          type="button"
          onClick={onBack}
          style={{
            background: "transparent",
            border: "none",
            color: OB.ink3,
            fontSize: 14,
            fontWeight: 600,
            cursor: "pointer",
            fontFamily: OB.fontBody,
          }}
        >
          Sign out
        </button>
      </div>

      <main className="flex-1 flex items-center justify-center" style={{ padding: "24px 16px 48px" }}>
        <div style={{ width: "100%", maxWidth: 560 }}>
          <h1
            style={{
              fontFamily: OB.fontDisplay,
              fontWeight: 600,
              fontSize: "clamp(28px, 4.5vw, 40px)",
              letterSpacing: "-0.02em",
              lineHeight: 1.15,
              margin: "0 0 12px",
              textWrap: "balance",
            }}
          >
            Your first coffee chat starts with <em style={{ color: OB.primary }}>one good email</em>
          </h1>
          <p style={{ fontSize: 16, color: OB.ink2, lineHeight: 1.6, margin: "0 0 28px" }}>
            Share your resume or LinkedIn and we'll set everything up. It takes about two minutes.
          </p>

          <ul style={{ listStyle: "none", padding: 0, margin: "0 0 32px", display: "grid", gap: 14 }}>
            {POINTS.map(({ icon: Icon, title, body }) => (
              <li
                key={title}
                style={{
                  display: "flex",
                  gap: 14,
                  alignItems: "flex-start",
                  background: "#fff",
                  border: `1px solid ${OB.border}`,
                  borderRadius: 12,
                  padding: "14px 16px",
                }}
              >
                <span
                  style={{
                    flexShrink: 0,
                    width: 36,
                    height: 36,
                    borderRadius: 10,
                    background: OB.primary50,
                    color: OB.primary,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                  }}
                >
                  <Icon size={18} strokeWidth={1.8} aria-hidden />
                </span>
                <span>
                  <span style={{ display: "block", fontWeight: 600, fontSize: 15 }}>{title}</span>
                  <span style={{ display: "block", fontSize: 14, color: OB.ink3, lineHeight: 1.5 }}>{body}</span>
                </span>
              </li>
            ))}
          </ul>

          <button type="button" onClick={onContinue} style={obPrimaryButton}>
            Get started
          </button>
        </div>
      </main>
    </div>
  );
};
