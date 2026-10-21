import { type ReactNode } from "react";
import { Crown, LockKeyhole } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { trackUpgradeClick } from "@/lib/analytics";

interface EliteGateProps {
  feature: string;
  description: string;
  children: ReactNode;
}

// Elite-only paywall: blurs the feature and puts an upgrade card in front.
// The backend enforces the same gate, so this is presentation only.
export function EliteGate({ feature, description, children }: EliteGateProps) {
  const { user } = useFirebaseAuth();
  const navigate = useNavigate();

  if (user?.tier === "elite") {
    return <>{children}</>;
  }

  return (
    <div className="relative flex-1 flex flex-col min-h-0 overflow-hidden">
      <div
        className="flex-1 flex flex-col min-h-0 pointer-events-none select-none"
        aria-hidden="true"
        style={{ filter: "blur(4px)" }}
      >
        {children}
      </div>

      <div
        className="absolute inset-0 z-40 flex flex-col items-center justify-start overflow-y-auto pt-16 px-4"
        style={{ backgroundColor: "rgba(255, 255, 255, 0.55)" }}
      >
        <div className="inline-flex items-center gap-2 rounded-full bg-[#0F172A] px-4 py-1.5 mb-6 shadow-sm">
          <Crown className="h-3.5 w-3.5 text-amber-300" />
          <span className="text-sm font-medium text-white">Only for Elite users</span>
        </div>

        <div className="w-full max-w-sm rounded-[3px] border border-gray-200 bg-white p-8 text-center shadow-lg">
          <LockKeyhole className="h-10 w-10 text-[#3B82F6] mx-auto mb-4" />
          <h2
            className="text-2xl font-normal text-gray-900 mb-2"
            style={{ fontFamily: "'Instrument Serif', Georgia, serif" }}
          >
            {feature} is an Elite feature
          </h2>
          <p className="text-sm text-gray-500 mb-6">{description}</p>
          <button
            type="button"
            onClick={() => {
              trackUpgradeClick(feature.toLowerCase().replace(/\s+/g, "_"), {
                from_location: "elite_gate",
              });
              navigate("/pricing");
            }}
            className="w-full h-11 rounded-[3px] bg-[#0F172A] hover:bg-[#1E293B] text-white font-medium shadow-md transition-all"
          >
            Upgrade Plan
          </button>
        </div>
      </div>
    </div>
  );
}
