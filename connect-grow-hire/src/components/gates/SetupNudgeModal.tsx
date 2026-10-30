/**
 * SetupNudgeModal — CTA-moment popup for the two setup gaps that block or
 * degrade an action the user just tried:
 *   - "gmail": the action creates/sends email but Gmail isn't connected.
 *     Primary CTA routes to /integrations?connect=gmail (auto-starts OAuth).
 *   - "resume": the action works far better (or not at all, for auto-apply)
 *     with a resume on file. Primary CTA routes to /resume.
 *
 * Callers are responsible for only opening this when the gap actually exists
 * (gmail not connected / no resume saved) — never nag someone who's set up.
 * Pass `onContinue` to offer a "Continue without" path for soft nudges; omit
 * it for hard requirements (send emails, auto-apply).
 */
import { Mail, FileText, X } from "lucide-react";
import { useNavigate } from "react-router-dom";

export type SetupNudgeVariant = "gmail" | "resume";

const COPY: Record<
  SetupNudgeVariant,
  { title: string; body: string; cta: string; continueLabel: string; route: string }
> = {
  gmail: {
    title: "Connect your Gmail",
    body:
      "Connect Gmail so your drafts land right in your account, ready to send, and replies show up in your Inbox automatically.",
    cta: "Connect Gmail",
    continueLabel: "Continue without Gmail",
    route: "/integrations?connect=gmail",
  },
  resume: {
    title: "Add your resume first",
    body:
      "Your resume powers the good stuff: personalized emails that draw real connections between you and each contact, better job matches, and auto-apply.",
    cta: "Upload resume",
    continueLabel: "Continue without resume",
    route: "/account-settings",
  },
};

interface SetupNudgeModalProps {
  open: boolean;
  variant: SetupNudgeVariant;
  onClose: () => void;
  // When provided, renders a secondary "Continue without …" action that runs
  // the original action anyway. Omit for hard requirements.
  onContinue?: () => void;
  // Optional context-specific body override (e.g. auto-apply's hard message).
  body?: string;
}

export function SetupNudgeModal({ open, variant, onClose, onContinue, body }: SetupNudgeModalProps) {
  const navigate = useNavigate();
  if (!open) return null;
  const copy = COPY[variant];
  const Icon = variant === "gmail" ? Mail : FileText;

  return (
    <div
      className="fixed inset-0 z-[9000] flex items-center justify-center p-4"
      style={{ background: "rgba(15,23,42,0.45)" }}
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={copy.title}
    >
      <div
        className="bg-white rounded-2xl shadow-xl w-full text-center relative"
        style={{ maxWidth: 420, padding: "32px 28px 24px" }}
        onClick={(e) => e.stopPropagation()}
      >
        <button
          type="button"
          aria-label="Close"
          onClick={onClose}
          className="absolute"
          style={{ top: 14, right: 14, background: "none", border: "none", cursor: "pointer", color: "#94A3B8" }}
        >
          <X size={18} />
        </button>

        <span
          className="inline-flex items-center justify-center"
          style={{
            width: 48,
            height: 48,
            borderRadius: 14,
            background: "#EFF6FF",
            color: "#2563EB",
            marginBottom: 14,
          }}
        >
          <Icon size={24} strokeWidth={1.8} />
        </span>

        <h3
          style={{
            fontFamily: "'Libre Baskerville', Georgia, serif",
            fontSize: 21,
            fontWeight: 400,
            color: "#1E2D4D",
            margin: "0 0 8px",
          }}
        >
          {copy.title}
        </h3>
        <p style={{ fontSize: 14, lineHeight: 1.6, color: "#475569", margin: "0 0 20px" }}>
          {body || copy.body}
        </p>

        <button
          type="button"
          onClick={() => {
            onClose();
            navigate(copy.route);
          }}
          className="w-full transition-colors"
          style={{
            height: 44,
            borderRadius: 10,
            border: "none",
            background: "#2563EB",
            color: "#fff",
            fontSize: 14,
            fontWeight: 600,
            cursor: "pointer",
          }}
          onMouseEnter={(e) => (e.currentTarget.style.background = "#1D4ED8")}
          onMouseLeave={(e) => (e.currentTarget.style.background = "#2563EB")}
        >
          {copy.cta}
        </button>

        {onContinue && (
          <button
            type="button"
            onClick={() => {
              onClose();
              onContinue();
            }}
            style={{
              marginTop: 10,
              background: "none",
              border: "none",
              cursor: "pointer",
              fontSize: 13,
              fontWeight: 600,
              color: "#64748B",
            }}
          >
            {copy.continueLabel}
          </button>
        )}
      </div>
    </div>
  );
}
