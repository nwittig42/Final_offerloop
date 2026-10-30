// McpServerPage — /mcp-server
// Pick an AI client (Claude or ChatGPT) and jump to its Offerloop MCP
// connector setup guide. Claude's guide is the existing /connector page;
// ChatGPT's lives at /mcp-server/chatgpt.

import { useNavigate } from "react-router-dom";
import { AppSidebar } from "@/components/AppSidebar";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppHeader } from "@/components/AppHeader";
import { MainContentWrapper } from "@/components/MainContentWrapper";

const CLIENTS = [
  {
    name: "Claude",
    logo: "/logos/claude.svg",
    blurb: "Find contacts, pull company intel, and draft outreach inside Claude.",
    to: "/connector",
  },
  {
    name: "ChatGPT",
    logo: "/logos/openai.svg",
    blurb: "Connect Offerloop to ChatGPT with developer-mode connectors.",
    to: "/mcp-server/chatgpt",
  },
];

const McpServerPage = () => {
  const navigate = useNavigate();

  return (
    <SidebarProvider>
      <div className="flex min-h-screen w-full font-sans" style={{ color: "var(--ink)" }}>
        <AppSidebar />
        <MainContentWrapper>
          <AppHeader title="Claude & ChatGPT" />

          <div className="flex-1 overflow-y-auto" style={{ background: "#FBFCFE" }}>
            <div className="mx-auto w-full max-w-[820px] px-5 py-6 sm:px-10 sm:py-8">
              <h1
                style={{
                  fontFamily: "'Libre Baskerville', Georgia, serif",
                  fontSize: 30,
                  fontWeight: 400,
                  lineHeight: "36px",
                  color: "#1e2d4d",
                  margin: 0,
                }}
              >
                Connect to Claude and ChatGPT
              </h1>
              <p className="mt-2 text-sm text-muted-foreground">
                Use Offerloop from your AI assistant. Pick where you want to set it up.
              </p>

              <div className="mt-8 grid grid-cols-1 gap-6 sm:grid-cols-2">
                {CLIENTS.map((client) => (
                  <div
                    key={client.name}
                    role="button"
                    tabIndex={0}
                    onClick={() => navigate(client.to)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") navigate(client.to);
                    }}
                    className="flex cursor-pointer flex-col items-center rounded-2xl border border-gray-200 bg-white p-8 text-center shadow-sm transition-shadow hover:shadow-md"
                    style={{ aspectRatio: "1 / 1", minHeight: 280 }}
                  >
                    <div className="flex flex-1 flex-col items-center justify-center gap-4">
                      <img
                        src={client.logo}
                        alt={`${client.name} logo`}
                        className="h-16 w-16"
                      />
                      <div>
                        <div className="text-lg font-semibold" style={{ color: "#1e2d4d" }}>
                          {client.name}
                        </div>
                        <p className="mt-1 text-sm text-muted-foreground">{client.blurb}</p>
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation();
                        navigate(client.to);
                      }}
                      className="mt-6 w-full rounded-lg bg-[#0F172A] px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-[#1E293B]"
                    >
                      Set up now
                    </button>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </MainContentWrapper>
      </div>
    </SidebarProvider>
  );
};

export default McpServerPage;
