import { useMemo } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { OB, obPrimaryButton } from "./onboardingTheme";
import OfferloopLogo from "@/assets/offerloop_logo2_trimmed.png";

// Hand-off from onboarding into the first-use walkthrough. Milestone 1 ships
// this entry point only: users/{uid}.walkthrough stays { status: "pending" }
// so Milestone 2's walkthrough (pick a person → draft → connect Gmail → send)
// can start here and resume later. Until then it continues to Find.

const safeReturnTo = (raw: string | null): string => {
  if (!raw) return "";
  return raw.startsWith("/") && !raw.startsWith("//") && !raw.startsWith("/onboarding") && !raw.startsWith("/get-started")
    ? raw
    : "";
};

export default function GetStartedPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { user } = useFirebaseAuth();
  const returnTo = useMemo(() => safeReturnTo(new URLSearchParams(location.search).get("returnTo")), [location.search]);
  const firstName = (user?.name || "").trim().split(/\s+/)[0];

  return (
    <div className="min-h-screen flex flex-col" style={{ background: OB.pageBg, color: OB.heading, fontFamily: OB.fontBody }}>
      <div style={{ padding: "20px clamp(16px, 5vw, 40px)" }}>
        <img src={OfferloopLogo} alt="Offerloop" style={{ height: 34 }} />
      </div>
      <main className="flex-1 flex items-center justify-center" style={{ padding: "24px 16px 48px" }}>
        <div style={{ width: "100%", maxWidth: 520 }}>
          <p style={{ fontSize: 13, fontWeight: 600, letterSpacing: "0.04em", textTransform: "uppercase", color: OB.primary, margin: "0 0 10px" }}>
            Profile saved · Next: your first connection
          </p>
          <h1
            style={{
              fontFamily: OB.fontDisplay,
              fontWeight: 600,
              fontSize: "clamp(26px, 4vw, 34px)",
              letterSpacing: "-0.02em",
              margin: "0 0 12px",
            }}
          >
            {firstName ? `${firstName}, now` : "Now"} let's find your first connection
          </h1>
          <p style={{ fontSize: 16, color: OB.ink2, lineHeight: 1.6, margin: "0 0 28px" }}>
            We'll show you a few people who fit your background. Pick one, and we'll help you write
            your first email to them.
          </p>
          <button type="button" onClick={() => navigate(returnTo || "/find", { replace: true })} style={obPrimaryButton}>
            Show me who to reach out to
          </button>
        </div>
      </main>
    </div>
  );
}
