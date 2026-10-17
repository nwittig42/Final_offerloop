// ConnectAiAssistantsCard — always-on prompt on Profile and Account Settings
// that sends the user to /mcp-server to hook Offerloop up to Claude or
// ChatGPT. Replaces the old "MCP Server" sidebar entry as the way in.
import { useNavigate } from "react-router-dom";
import { ArrowRight } from "lucide-react";

const LOGOS = [
  { src: "/logos/claude.svg", alt: "Claude" },
  { src: "/logos/openai.svg", alt: "ChatGPT" },
];

export function ConnectAiAssistantsCard({ style }: { style?: React.CSSProperties }) {
  const navigate = useNavigate();

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 16,
        flexWrap: "wrap",
        padding: "16px 18px",
        background: "#FFFFFF",
        border: "1px solid #DCE3F1",
        borderRadius: 12,
        textAlign: "left",
        fontFamily: "'DM Sans', system-ui, sans-serif",
        ...style,
      }}
    >
      <div style={{ display: "flex", flexShrink: 0 }}>
        {LOGOS.map((logo, i) => (
          <div
            key={logo.alt}
            style={{
              width: 36,
              height: 36,
              borderRadius: "50%",
              background: "#F4F6FB",
              border: "2px solid #FFFFFF",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              marginLeft: i === 0 ? 0 : -8,
            }}
          >
            <img src={logo.src} alt={logo.alt} style={{ width: 20, height: 20 }} />
          </div>
        ))}
      </div>

      <div style={{ flex: "1 1 220px", minWidth: 0 }}>
        <div style={{ fontSize: 15, fontWeight: 600, color: "#0F172A" }}>
          Connect to Claude and ChatGPT
        </div>
        <div style={{ fontSize: 13, color: "#6B7280", marginTop: 2, lineHeight: 1.5 }}>
          Find contacts, pull company intel, and draft outreach without leaving your AI assistant.
        </div>
      </div>

      <button
        type="button"
        onClick={() => navigate("/mcp-server")}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          padding: "9px 16px",
          borderRadius: 999,
          background: "#4A60A8",
          border: "none",
          color: "#FFFFFF",
          fontSize: 13,
          fontWeight: 500,
          cursor: "pointer",
          flexShrink: 0,
        }}
      >
        Connect
        <ArrowRight style={{ width: 14, height: 14 }} />
      </button>
    </div>
  );
}
