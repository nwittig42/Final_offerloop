// Agent dashboard — redesigned to match Offerloop's visual language.
// Centered single column, serif italic headers, underline tabs, flat rows.
// Running state: full-content takeover with terminal stream + flip counters.

import { useState, useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import { Loader2, ExternalLink, Settings, CheckCircle2, XCircle, Check } from "lucide-react";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import {
  useAgentSnapshot,
  useAgentApprovals,
  useAgentStats,
  useCountdown,
  useCycleRunner,
  useAgentLifecycle,
  useAgentActivityLive,
  useCycleProgress,
} from "@/hooks/useAgent";
import { useSimulatedStream } from "@/hooks/useSimulatedStream";
import type {
  AgentConfig,
  AgentAction,
  AgentJob,
  AgentCompany,
  CompanyPipeline,
} from "@/services/agent";
import type { CycleStep } from "@/hooks/useAgent";

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 60_000) return "just now";
  const mins = Math.floor(diff / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

function Initial({ name, size = 28 }: { name: string; size?: number }) {
  const ch = (name || "?").trim().charAt(0).toUpperCase();
  return (
    <div
      className="rounded-full flex items-center justify-center shrink-0"
      style={{
        width: size,
        height: size,
        background: "#f3f3f6",
        color: "#4b5567",
        fontSize: size * 0.42,
        fontWeight: 600,
      }}
    >
      {ch}
    </div>
  );
}

// ── Animations (injected once) ──────────────────────────────────────────────

const AGENT_KEYFRAMES = `
@keyframes om-pulse {
  0%   { box-shadow: 0 0 0 0   rgba(34,197,94,.55); }
  70%  { box-shadow: 0 0 0 8px rgba(34,197,94,0);   }
  100% { box-shadow: 0 0 0 0   rgba(34,197,94,0);   }
}
@keyframes om-scan {
  0%   { transform: translateX(-100%); }
  100% { transform: translateX(100%);  }
}
@keyframes om-bars {
  0%, 100% { transform: scaleY(0.35); }
  50%      { transform: scaleY(1); }
}
`;

function AgentStyles() {
  return <style dangerouslySetInnerHTML={{ __html: AGENT_KEYFRAMES }} />;
}

// ── Primitives ────────────────────────────────────────────────────────────

function PulseDot({ small, color = "#22c55e" }: { small?: boolean; color?: string }) {
  const size = small ? 6 : 7;
  return (
    <span style={{ position: "relative", width: size, height: size, display: "inline-block" }}>
      <span style={{
        position: "absolute", inset: 0, borderRadius: "50%",
        background: color,
        animation: "om-pulse 1.6s ease-out infinite",
      }} />
    </span>
  );
}

function ActivityBars({ count = 5, height = 12 }: { count?: number; height?: number }) {
  return (
    <span style={{
      display: "inline-flex", alignItems: "flex-end", gap: 2,
      height, verticalAlign: "middle",
    }}>
      {Array.from({ length: count }, (_, i) => (
        <span key={i} style={{
          width: 2, height, background: "#8089a0", borderRadius: 1,
          animation: `om-bars ${1.2 + i * 0.1}s ease-in-out ${i * 0.13}s infinite`,
          transformOrigin: "bottom",
        }} />
      ))}
    </span>
  );
}

// ── Action-to-kind mapping ───────────────────────────────────────────────

const ACTION_KIND: Record<string, string> = {
  plan: "research",
  find: "scan",
  find_jobs: "discover",
  discover_companies: "discover",
  find_hiring_managers: "scan",
  draft: "draft",
  send: "verify",
  follow_up: "queue",
  monitor: "watch",
  skip: "watch",
};

const KIND_META: Record<string, { color: string; label: string }> = {
  scan:     { color: "#7d8ba6", label: "scan" },
  score:    { color: "#7d8ba6", label: "score" },
  draft:    { color: "#d4a017", label: "draft" },
  verify:   { color: "#16a34a", label: "verify" },
  discover: { color: "#7c5cff", label: "discover" },
  watch:    { color: "#7d8ba6", label: "watch" },
  research: { color: "#7d8ba6", label: "research" },
  match:    { color: "#16a34a", label: "match" },
  queue:    { color: "#7d8ba6", label: "queue" },
  tool:     { color: "#0ea5e9", label: "tool" },
};

// ── Main component ──────────────────────────────────────────────────────────

export function AgentSnapshot({
  config,
  onOpenSettings,
}: {
  config: AgentConfig;
  onOpenSettings: () => void;
}) {
  const { user } = useFirebaseAuth();
  const snapshot = useAgentSnapshot();
  const approvalsQuery = useAgentApprovals();
  const countdown = useCountdown(snapshot.stats.data?.nextCycleAt);
  const cycleRunner = useCycleRunner();
  const lifecycle = useAgentLifecycle();
  const liveActions = useAgentActivityLive();
  const stepProgress = useCycleProgress(cycleRunner.cycleId);
  const streamLines = useSimulatedStream(
    cycleRunner.isRunning,
    stepProgress.currentAction,
    stepProgress.completedActions
  );

  const stats = snapshot.stats.data;
  const isActive = config.status === "active";
  const isPaused = config.status === "paused";
  const [activeTab, setActiveTab] = useState("today");

  const firstName =
    user?.name?.split(" ")[0] || user?.email?.split("@")[0] || "there";

  // Derive display data
  const draftsReady = stats?.pendingDrafts ?? 0;
  const repliesWaiting = stats?.unreadReplies ?? 0;
  const contactsThisWeek = stats?.contactsThisWeek ?? 0;
  const weeklyTarget = stats?.weeklyContactTarget ?? 5;
  const recentJobs = snapshot.jobs.data?.jobs ?? [];
  const recentCompanies = snapshot.companies.data?.companies ?? [];
  const pipelineCompanies = snapshot.pipeline.data?.companies ?? [];

  // Extract contacts from last cycle
  const lastCycle = snapshot.cycles.data?.cycles?.[0];
  const cycleActions = snapshot.activity.data?.actions ?? [];
  const lastCycleActions = lastCycle
    ? cycleActions.filter((a) => a.cycleId === lastCycle.id)
    : [];

  const allContacts: Array<{
    name: string;
    role: string;
    company: string;
    subject: string;
    preview: string;
    isHm: boolean;
    time: string;
  }> = [];
  for (const ca of lastCycleActions) {
    if (
      (ca.action === "find" || ca.action === "find_hiring_managers") &&
      ca.status === "completed"
    ) {
      const result = ca.result as Record<string, unknown> | null;
      const contacts =
        (result?.contacts as Array<Record<string, unknown>>) ?? [];
      for (const c of contacts) {
        allContacts.push({
          name: (c.name as string) || "Unknown",
          role: (c.title as string) || (c.role as string) || "",
          company: (c.company as string) || "",
          subject: (c.emailSubject as string) || "",
          preview: (c.emailBodyPreview as string) || "",
          isHm: !!(c.isHiringManager as boolean),
          time: ca.createdAt ? relativeTime(ca.createdAt) : "",
        });
      }
    }
  }

  const cycleJobs = lastCycle
    ? recentJobs.filter((j) => j.cycleId === lastCycle.id)
    : [];
  const cycleCompanies = lastCycle
    ? recentCompanies.filter((co) => co.cycleId === lastCycle.id)
    : [];

  const pendingApprovals = approvalsQuery.data?.approvals ?? [];

  // Tab definitions — order matches the Variant C handoff
  // (Overview · Drafts · Replies · Jobs · Pipeline · Activity · Approvals)
  const repliesCount = pipelineCompanies.reduce((s, c) => s + (c.replies || 0), 0);
  const tabs = [
    { id: "today", label: "Overview" },
    { id: "drafts", label: "Drafts", count: allContacts.length || undefined },
    { id: "replies", label: "Replies", count: repliesCount || undefined },
    { id: "jobs", label: "Jobs", count: recentJobs.length || undefined },
    { id: "pipeline", label: "Pipeline", count: pipelineCompanies.length || undefined },
    { id: "activity", label: "Activity" },
    ...(pendingApprovals.length > 0
      ? [{ id: "approvals", label: "Approvals", count: pendingApprovals.length }]
      : []),
  ];

  // ── Running state: full-content takeover ──
  if (cycleRunner.isRunning) {
    const progress = cycleRunner.cycleProgress;
    return (
      <div className="pt-10 sm:pt-14">
        {/* Header */}
        <div className="text-center">
          <h1
            className="font-serif text-[32px] sm:text-[36px] leading-[1.1] tracking-[-0.02em]"
            style={{ color: "var(--ink, #0F172A)" }}
          >
            <em className="font-serif" style={{ fontWeight: 400 }}>
              Running...
            </em>
          </h1>
          <p className="mt-3 text-[13.5px] text-muted-foreground tracking-[-0.01em]">
            {stepProgress.currentLabel || "Planning actions"}
          </p>
        </div>

        {/* Flip counters */}
        <div className="flex justify-center gap-8 mt-8">
          <FlipCounter label="Contacts" value={progress?.contactsFound ?? 0} />
          <FlipCounter label="Jobs" value={progress?.jobsFound ?? 0} />
          <FlipCounter label="Emails" value={progress?.emailsDrafted ?? 0} />
          <FlipCounter label="HMs" value={progress?.hmsFound ?? 0} />
        </div>

        {/* Terminal stream */}
        <div className="mt-8 mx-auto max-w-lg">
          <div
            className="bg-[#0f1219] rounded-lg p-4 h-[280px] overflow-hidden relative"
            style={{ fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace" }}
          >
            <div className="absolute top-3 left-4 flex gap-1.5">
              <span className="w-2.5 h-2.5 rounded-full bg-[#ff5f56] opacity-70" />
              <span className="w-2.5 h-2.5 rounded-full bg-[#ffbd2e] opacity-70" />
              <span className="w-2.5 h-2.5 rounded-full bg-[#27c93f] opacity-70" />
            </div>
            <div className="pt-5 space-y-1 flex flex-col justify-end h-full overflow-hidden">
              {streamLines.map((line) => (
                <div
                  key={line.id}
                  className="text-[11.5px] leading-[1.6] animate-in fade-in slide-in-from-bottom-1 duration-300"
                  style={{
                    color:
                      line.type === "success"
                        ? "#4ade80"
                        : line.type === "info"
                          ? "#60a5fa"
                          : line.type === "dim"
                            ? "#4b5567"
                            : "#94a3b8",
                  }}
                >
                  <span className="text-[#4b5567] mr-2">{line.timestamp}</span>
                  {line.type === "success" && <span className="mr-1">✓</span>}
                  {line.text}
                </div>
              ))}
              <div className="text-[11.5px] text-[#94a3b8] animate-pulse">
                <span className="text-[#4b5567] mr-2">
                  {new Date().toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false })}
                </span>
                <span className="inline-block w-1.5 h-3 bg-[#94a3b8] animate-[cursor-blink_1s_infinite]" />
              </div>
            </div>
          </div>
        </div>

        {/* Step progress below terminal */}
        {(stepProgress.completedActions.length > 0 || stepProgress.currentAction) && (
          <div className="mt-6 mx-auto max-w-sm">
            <StepList
              completedActions={stepProgress.completedActions}
              currentAction={stepProgress.currentAction}
              currentLabel={stepProgress.currentLabel}
              plannedActions={stepProgress.plannedActions}
            />
          </div>
        )}
      </div>
    );
  }

  // Latest live action becomes the typewriter-style ticker line in the hero
  const tickerLine: string | null = (() => {
    const a = liveActions[0];
    if (!a) return null;
    const verb = ACTION_VERBS[a.action] || a.action;
    const target = a.company || "";
    return `${verb}${target ? ` ${target}` : ""}`.trim();
  })();

  return (
    <div>
      <AgentStyles />
      {/* ── Hero (editorial masthead — Variant C) ── */}
      <div className="pt-10 sm:pt-12 pb-7 border-b border-[#e9eaef]">
        <div className="flex items-start justify-between gap-8 flex-wrap">
          {/* Left: kicker + headline + live ticker */}
          <div className="flex-1 min-w-0" style={{ minWidth: 280 }}>
            <div
              className="mb-3"
              style={{
                fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                fontSize: 10,
                color: "#8089a0",
                letterSpacing: "0.16em",
                textTransform: "uppercase",
              }}
            >
              Agent · {new Date().toLocaleDateString(undefined, { weekday: "long" })}
            </div>
            <h1
              className="font-serif leading-[1.05] tracking-[-0.025em]"
              style={{
                color: "var(--ink, #0F172A)",
                fontSize: "clamp(28px, 4vw, 38px)",
                fontWeight: 400,
              }}
            >
              {cycleRunner.lastEndStatus === "awaiting_approval" ? (
                <>
                  Actions queued <em className="font-serif italic" style={{ fontWeight: 400 }}>for your approval</em>, {firstName}.
                </>
              ) : draftsReady > 0 || repliesWaiting > 0 ? (
                <>
                  Good {greetingPart()}, {firstName}. You have{" "}
                  <em className="font-serif italic" style={{ fontWeight: 400 }}>
                    {draftsReady === 1 ? "one draft" : `${draftsReady} drafts`}
                  </em>
                  {" "}and{" "}
                  <em className="font-serif italic" style={{ fontWeight: 400 }}>
                    {repliesWaiting === 1 ? "one reply" : `${repliesWaiting} replies`}
                  </em>
                  .
                </>
              ) : (
                <>
                  All caught up, {firstName}.{" "}
                  <em className="font-serif italic" style={{ fontWeight: 400 }}>The agent will keep watching.</em>
                </>
              )}
            </h1>

            {/* Live ticker — typewriter line, animates on each new action */}
            <div
              className="mt-4 flex items-center gap-2.5"
              style={{
                fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                fontSize: 12,
                color: "#4b5567",
              }}
            >
              {isActive && <PulseDot small />}
              {isPaused && <PulseDot small color="#f59e0b" />}
              {!isActive && !isPaused && <PulseDot small color="#8089a0" />}
              <span
                key={liveActions[0]?.id || "idle"}
                className="animate-in fade-in slide-in-from-bottom-1 duration-300"
                style={{ color: tickerLine ? "#4b5567" : "#8089a0" }}
              >
                {tickerLine ||
                  (isActive
                    ? "Watching for replies and new jobs"
                    : isPaused
                      ? "Paused — resume to continue"
                      : "Idle. Deploy your agent to start.")}
              </span>
              {isActive && tickerLine && (
                <>
                  <span style={{ opacity: 0.4 }}>·</span>
                  <ActivityBars />
                </>
              )}
            </div>
          </div>

          {/* Right: big serif counters + status/pause */}
          <div className="flex items-start gap-6">
            <BigCounter n={draftsReady} label="drafts ready" />
            <BigCounter n={repliesWaiting} label="replies waiting" />
            <div
              className="pl-5"
              style={{ borderLeft: "1px solid #f1f1f4", minWidth: 110 }}
            >
              <div
                style={{
                  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                  fontSize: 10,
                  color: "#8089a0",
                  letterSpacing: "0.12em",
                  textTransform: "uppercase",
                  marginBottom: 6,
                }}
              >
                Status
              </div>
              <div className="flex items-center gap-2 mb-3">
                <PulseDot
                  color={isActive ? "#22c55e" : isPaused ? "#f59e0b" : "#8089a0"}
                />
                <span className="text-[13px] font-medium" style={{ color: "var(--ink, #0F172A)" }}>
                  {isActive ? "Active" : isPaused ? "Paused" : "Idle"}
                </span>
              </div>
              {isActive && (
                <button
                  onClick={() => lifecycle.pause.mutate()}
                  disabled={lifecycle.pause.isPending}
                  className="bg-white text-[var(--ink,#0F172A)] border border-[#e9eaef] rounded-md px-3 py-1.5 text-[12px] font-medium cursor-pointer hover:bg-[#fafafa] transition-colors disabled:opacity-50"
                >
                  Pause agent
                </button>
              )}
              {isPaused && (
                <button
                  onClick={() => lifecycle.deploy.mutate()}
                  disabled={lifecycle.deploy.isPending}
                  className="bg-[var(--ink,#0F172A)] text-white border border-[var(--ink,#0F172A)] rounded-md px-3 py-1.5 text-[12px] font-medium cursor-pointer hover:opacity-90 transition-opacity disabled:opacity-50"
                >
                  Resume
                </button>
              )}
            </div>
          </div>
        </div>

        {/* Sub-status sentence — small, below the masthead */}
        {(isActive || isPaused) && (
          <p
            className="mt-5 text-[12.5px] text-muted-foreground tracking-[-0.01em]"
            style={{ maxWidth: 560 }}
          >
            {isActive
              ? `${contactsThisWeek} of ${weeklyTarget} contacts this week${countdown ? ` · next cycle ${countdown}` : ""}. Drafting outreach, finding jobs, watching for replies. Nothing sends without your approval.`
              : "Agent paused. Drafts and replies are preserved; resume to continue cycles."}
          </p>
        )}
      </div>

      {/* ── Action buttons (review CTA + run-now + configure) ── */}
      <div className="flex items-center gap-2.5 mt-5 flex-wrap">
        {cycleRunner.lastEndStatus === "awaiting_approval" && (
          <button
            onClick={() => setActiveTab("approvals")}
            className="bg-[var(--ink,#0F172A)] text-white border border-[var(--ink,#0F172A)] rounded-md px-4 py-2 text-[13px] font-medium tracking-[-0.01em] cursor-pointer hover:opacity-90 transition-opacity"
          >
            Review approvals
          </button>
        )}
        {draftsReady > 0 && cycleRunner.lastEndStatus !== "awaiting_approval" && (
          <button
            onClick={() => setActiveTab("drafts")}
            className="bg-[var(--ink,#0F172A)] text-white border border-[var(--ink,#0F172A)] rounded-md px-4 py-2 text-[13px] font-medium tracking-[-0.01em] cursor-pointer hover:opacity-90 transition-opacity"
          >
            Review {draftsReady} draft{draftsReady !== 1 ? "s" : ""}
          </button>
        )}
        {(isActive || isPaused) && (
          <button
            onClick={() => cycleRunner.runNow()}
            disabled={
              cycleRunner.isRunNowPending ||
              cycleRunner.isRunning ||
              cycleRunner.isOnCooldown
            }
            title={
              cycleRunner.isOnCooldown
                ? `Cooling down… ${Math.ceil(cycleRunner.cooldownRemainingMs / 1000)}s`
                : "Trigger a manual cycle"
            }
            className="bg-white text-[var(--ink,#0F172A)] border border-[#e9eaef] rounded-md px-4 py-2 text-[13px] font-medium tracking-[-0.01em] cursor-pointer hover:bg-[#fafafa] transition-colors disabled:opacity-50"
          >
            {cycleRunner.isOnCooldown
              ? `Run now (${Math.ceil(cycleRunner.cooldownRemainingMs / 1000)}s)`
              : "Run now"}
          </button>
        )}
        <button
          onClick={onOpenSettings}
          className="bg-white text-[var(--ink,#0F172A)] border border-[#e9eaef] rounded-md px-4 py-2 text-[13px] font-medium tracking-[-0.01em] cursor-pointer hover:bg-[#fafafa] transition-colors flex items-center gap-1.5"
        >
          <Settings className="h-3.5 w-3.5" /> Configure
        </button>
      </div>

      {/* ── Telemetry strip ── */}
      {isActive && stats && (
        <div className="mt-5 grid grid-cols-4 border border-[#e9eaef] rounded-lg overflow-hidden"
          style={{ background: "var(--paper-2, #fafafa)" }}>
          {[
            { label: "contacts found", value: contactsThisWeek, sub: "this week" },
            { label: "drafts pending", value: draftsReady, sub: "awaiting review" },
            { label: "jobs matched", value: recentJobs.length, sub: "this cycle" },
            { label: "credits spent", value: stats.creditsSpentThisWeek ?? 0, sub: `of ${stats.creditBudgetPerWeek ?? 0}` },
          ].map((it, i, arr) => (
            <div key={it.label} className="flex flex-col gap-0.5" style={{
              padding: "10px 14px",
              borderRight: i < arr.length - 1 ? "1px solid #e9eaef" : "none",
            }}>
              <div className="text-[10px] tracking-[0.05em] uppercase text-[#8089a0] font-medium">
                {it.label}
              </div>
              <div className="flex items-baseline gap-1.5">
                <span style={{
                  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                  fontSize: 18, fontWeight: 500, fontVariantNumeric: "tabular-nums",
                  color: "var(--ink, #0F172A)",
                }}>{it.value}</span>
                <span className="text-[11px] text-[#8089a0]">{it.sub}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* ── Tabs (underline style — left-aligned per Variant C) ── */}
      <div className="flex gap-7 mt-8 border-b border-[#e9eaef]">
        {tabs.map((t) => {
          const on = t.id === activeTab;
          return (
            <button
              key={t.id}
              onClick={() => setActiveTab(t.id)}
              className="relative pb-2.5 text-[13px] tracking-[-0.01em] cursor-pointer bg-transparent border-0 flex items-center gap-1.5 transition-colors"
              style={{
                fontWeight: on ? 500 : 400,
                color: on ? "var(--ink, #0F172A)" : "#8089a0",
                borderBottom: on
                  ? "1.5px solid var(--ink, #0F172A)"
                  : "1.5px solid transparent",
                marginBottom: -1,
              }}
            >
              {t.label}
              {t.count != null && (
                <span
                  className="text-[11px] text-[#b3b8c7]"
                  style={{ fontWeight: 400 }}
                >
                  {t.count}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* ── Tab content ── */}

      {activeTab === "today" && (
        <OverviewContent
          allContacts={allContacts}
          recentJobs={
            cycleJobs.length > 0 ? cycleJobs : recentJobs.slice(0, 3)
          }
          recentCompanies={
            cycleCompanies.length > 0
              ? cycleCompanies
              : recentCompanies.slice(0, 3)
          }
          pipelineCompanies={pipelineCompanies}
          repliesWaiting={repliesWaiting}
          draftsReady={draftsReady}
        />
      )}

      {activeTab === "drafts" && (
        <div className="pt-9">
          <SectionHead title="Email drafts" />
          {allContacts.length > 0 ? (
            allContacts.map((d, i) => (
              <NumberedDraftRow
                key={i}
                d={d}
                index={i + 1}
                last={i === allContacts.length - 1}
              />
            ))
          ) : (
            <EmptyText>
              No drafts yet. Run a cycle to generate outreach emails.
            </EmptyText>
          )}
        </div>
      )}

      {activeTab === "replies" && (
        <div className="pt-9">
          <SectionHead
            title="Replies"
            action={<ViewAll to="/contacts">Open contacts →</ViewAll>}
          />
          <RepliesPanel pipelineCompanies={pipelineCompanies} />
        </div>
      )}

      {activeTab === "jobs" && (
        <div className="pt-9">
          <SectionHead title="Jobs matched to you" />
          {recentJobs.length > 0 ? (
            <EditorialJobList jobs={recentJobs} />
          ) : (
            <EmptyText>
              No jobs found yet. Run a cycle to discover matching roles.
            </EmptyText>
          )}
        </div>
      )}

      {activeTab === "pipeline" && (
        <div className="pt-9">
          <SectionHead
            title="Pipeline"
            action={<ViewAll to="/contacts">Open contacts →</ViewAll>}
          />
          {pipelineCompanies.length > 0 ? (
            <PipelineKanban companies={pipelineCompanies} />
          ) : (
            <EmptyText>
              No pipeline yet. Once contacts are drafted or replied, they appear here.
            </EmptyText>
          )}
        </div>
      )}

      {/* Companies tab removed in Variant C — companies now fold into Pipeline.
          recentCompanies data is still surfaced inside Overview's "Companies you might like" section. */}

      {activeTab === "activity" && (
        <div className="pt-9">
          <SectionHead title="Activity" />
          {liveActions.length > 0 ? (
            <ActivityTimeline items={liveActions} />
          ) : (
            <EmptyText>No activity yet. Deploy your agent to start.</EmptyText>
          )}
        </div>
      )}

      {activeTab === "approvals" && (
        <div className="pt-9 grid grid-cols-1 lg:grid-cols-[1fr_240px] gap-8">
          <div>
            <SectionHead title="Pending approvals" />
            {pendingApprovals.length > 0 ? (
              pendingApprovals.map((a, i) => (
                <ApprovalRow
                  key={a.id}
                  action={a}
                  onApprove={() => lifecycle.approve.mutateAsync(a.id).then(() => approvalsQuery.refetch())}
                  onReject={() => lifecycle.reject.mutateAsync(a.id).then(() => approvalsQuery.refetch())}
                  last={i === pendingApprovals.length - 1}
                />
              ))
            ) : (
              <EmptyText>No pending approvals.</EmptyText>
            )}
          </div>
          <ApprovalsPolicyRail config={config} stats={stats} />
        </div>
      )}

      {/* ── Footer meta ── */}
      <div className="mt-10 flex items-center gap-4 text-[11px] text-[#b3b8c7] px-1">
        <span>
          Credits: {stats?.creditsSpentThisWeek ?? 0}/
          {stats?.creditBudgetPerWeek ?? 0} this week
        </span>
        <span>
          Last cycle:{" "}
          {stats?.lastCycleAt ? relativeTime(stats.lastCycleAt) : "Never"}
        </span>
      </div>
    </div>
  );
}

// ── Overview content ──────────────────────────────────────────────────────

function OverviewContent({
  allContacts,
  recentJobs,
  recentCompanies,
  pipelineCompanies,
  repliesWaiting,
  draftsReady,
}: {
  allContacts: Array<{
    name: string;
    role: string;
    company: string;
    subject: string;
    preview: string;
    isHm: boolean;
    time: string;
  }>;
  recentJobs: AgentJob[];
  recentCompanies: AgentCompany[];
  pipelineCompanies: CompanyPipeline[];
  repliesWaiting: number;
  draftsReady: number;
}) {
  return (
    <div className="pt-9">
      {/* Summary tiles */}
      <div className="grid grid-cols-3 gap-3.5 mb-9">
        <SummaryTile
          label="Drafts ready"
          value={draftsReady}
          sub="Saved to Gmail, awaiting your review."
          scanning={draftsReady > 0}
          delta={draftsReady > 0 ? `+${draftsReady} today` : undefined}
        />
        <SummaryTile
          label="Jobs matched"
          value={recentJobs.length}
          sub="Roles aligned with your targets."
          delta={recentJobs.length > 0 ? `+${recentJobs.length} this cycle` : undefined}
        />
        <SummaryTile
          label="Replies"
          value={repliesWaiting}
          sub="Conversations now active in your inbox."
          scanning={repliesWaiting > 0}
          delta={repliesWaiting > 0 ? "watching" : undefined}
        />
      </div>

      {/* Drafts list */}
      {allContacts.length > 0 && (
        <div className="mb-10">
          <SectionHead
            title="Drafts ready to review"
            action={<ViewAll to="/contacts" />}
          />
          {allContacts.slice(0, 3).map((d, i, arr) => (
            <DraftRow key={i} d={d} last={i === arr.length - 1} />
          ))}
        </div>
      )}

      {/* Jobs */}
      {recentJobs.length > 0 && (
        <div className="mb-10">
          <SectionHead
            title="Jobs matched to you"
            action={<ViewAll to="/job-board" />}
          />
          {recentJobs.slice(0, 3).map((j, i, arr) => (
            <JobRow key={j.id} j={j} last={i === arr.length - 1} />
          ))}
        </div>
      )}

      {/* Companies */}
      {recentCompanies.length > 0 && (
        <div className="mb-10">
          <SectionHead title="Companies you might like" />
          {recentCompanies.slice(0, 3).map((c, i, arr) => (
            <CompanyRow key={c.id} c={c} last={i === arr.length - 1} />
          ))}
        </div>
      )}

      {/* Pipeline (compact) */}
      {pipelineCompanies.length > 0 && (
        <div className="mb-4">
          <SectionHead title="Company pipeline" />
          <div className="flex gap-3 overflow-x-auto pb-2 -mx-1 px-1">
            {pipelineCompanies.map((co) => (
              <Link
                key={co.name}
                to={`/contacts?q=${encodeURIComponent(co.name)}`}
                className="shrink-0 border border-[#e9eaef] rounded-lg p-3.5 hover:shadow-sm transition-shadow"
                style={{ minWidth: 180 }}
              >
                <div className="flex items-center gap-2.5 mb-1.5">
                  <Initial name={co.name} size={24} />
                  <span className="text-[13.5px] font-semibold tracking-[-0.01em]">
                    {co.name}
                  </span>
                </div>
                <div className="text-[11.5px] text-[#8089a0]">
                  {co.contacts} contacts
                  {co.hms > 0 && ` · ${co.hms} HM`}
                  {co.jobs > 0 && ` · ${co.jobs} jobs`}
                </div>
                {(co.draftsReady > 0 || co.replies > 0) && (
                  <div className="text-[11.5px] mt-1">
                    {co.draftsReady > 0 && (
                      <span className="text-[#b45309] font-medium">
                        {co.draftsReady} drafts
                      </span>
                    )}
                    {co.draftsReady > 0 && co.replies > 0 && " · "}
                    {co.replies > 0 && (
                      <span className="text-[#16a34a] font-medium">
                        {co.replies} replied
                      </span>
                    )}
                  </div>
                )}
              </Link>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Shared components ────────────────────────────────────────────────────

function SummaryTile({
  label,
  value,
  sub,
  scanning,
  delta,
}: {
  label: string;
  value: number;
  sub: string;
  scanning?: boolean;
  delta?: string;
}) {
  return (
    <div className="border border-[#e9eaef] rounded-[10px] p-4 relative overflow-hidden">
      {scanning && (
        <span className="absolute left-0 right-0 top-0 h-[1.5px]" style={{
          background: "linear-gradient(90deg, transparent, var(--ink, #15233a), transparent)",
          animation: "om-scan 2.4s ease-in-out infinite",
        }} />
      )}
      <div className="flex justify-between items-baseline">
        <div className="text-[11.5px] text-[#8089a0] font-medium tracking-[0.04em] uppercase">
          {label}
        </div>
        {delta && (
          <div style={{
            fontSize: 10.5, color: "#8089a0",
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
          }}>{delta}</div>
        )}
      </div>
      <div
        className="font-serif text-[30px] leading-none mt-2 tracking-[-0.02em]"
        style={{ color: "var(--ink, #0F172A)" }}
      >
        {value}
      </div>
      <div className="text-[12.5px] text-muted-foreground mt-2 leading-snug">
        {sub}
      </div>
    </div>
  );
}

function SectionHead({
  title,
  action,
}: {
  title: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex justify-between items-baseline mb-3.5">
      <h3
        className="text-[14px] font-semibold tracking-[-0.01em]"
        style={{ color: "var(--ink, #0F172A)" }}
      >
        {title}
      </h3>
      {action}
    </div>
  );
}

function ViewAll({
  to,
  children = "View all \u2192",
}: {
  to: string;
  children?: string;
}) {
  return (
    <Link
      to={to}
      className="text-[12px] text-[#8089a0] hover:text-[#4b5567] transition-colors"
    >
      {children}
    </Link>
  );
}

function EmptyText({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <p className="text-[13px] text-[#8089a0]">{children}</p>
    </div>
  );
}

// ── Row components ───────────────────────────────────────────────────────

function DraftRow({
  d,
  last,
}: {
  d: {
    name: string;
    role: string;
    company: string;
    subject: string;
    preview: string;
    isHm: boolean;
    time: string;
  };
  last: boolean;
}) {
  return (
    <div
      className="flex gap-3.5 py-3.5"
      style={{ borderBottom: last ? "none" : "1px solid #f1f1f4" }}
    >
      <Initial name={d.name} size={32} />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap mb-0.5">
          <span className="text-[13.5px] font-semibold tracking-[-0.01em]">
            {d.name}
          </span>
          <span className="text-[12.5px] text-[#8089a0]">
            {d.role}
            {d.company && `, ${d.company}`}
          </span>
          {d.isHm && (
            <span
              className="font-serif text-[12px] italic text-muted-foreground"
              style={{ fontWeight: 400 }}
            >
              · hiring manager
            </span>
          )}
        </div>
        {d.subject && (
          <div className="text-[13px] font-medium tracking-[-0.01em] mb-0.5">
            {d.subject}
          </div>
        )}
        {d.preview && (
          <div className="text-[12.5px] text-[#8089a0] leading-snug line-clamp-1">
            {d.preview}
          </div>
        )}
      </div>
      <div className="flex flex-col items-end gap-1.5 shrink-0">
        {d.time && (
          <span className="text-[11.5px] text-[#b3b8c7] whitespace-nowrap">
            {d.time}
          </span>
        )}
        <div className="flex gap-1.5">
          <Link to="/contacts">
            <button className="bg-white text-[var(--ink,#0F172A)] border border-[#e9eaef] rounded-md px-3 py-1.5 text-[12px] font-medium cursor-pointer hover:bg-[#fafafa] transition-colors">
              Edit
            </button>
          </Link>
          <Link to="/contacts">
            <button className="bg-[var(--ink,#0F172A)] text-white border border-[var(--ink,#0F172A)] rounded-md px-3 py-1.5 text-[12px] font-medium cursor-pointer hover:opacity-90 transition-opacity">
              Send
            </button>
          </Link>
        </div>
      </div>
    </div>
  );
}

function JobRow({ j, last }: { j: AgentJob; last: boolean }) {
  return (
    <div
      className="flex items-start gap-3.5 py-3.5"
      style={{ borderBottom: last ? "none" : "1px solid #f1f1f4" }}
    >
      <Initial name={j.company} size={30} />
      <div className="flex-1 min-w-0">
        <div className="text-[13.5px] font-semibold tracking-[-0.01em] leading-snug">
          {j.title}
        </div>
        <div className="text-[12.5px] text-[#8089a0] mt-1">
          {j.company}
          {j.location && ` · ${j.location}`}
        </div>
        {j.matchReasons?.length > 0 && (
          <div className="text-[11.5px] text-[#b3b8c7] mt-1">
            {j.matchReasons.slice(0, 2).join(" · ")}
          </div>
        )}
      </div>
      {j.applyLink ? (
        <a
          href={j.applyLink}
          target="_blank"
          rel="noopener noreferrer"
          className="shrink-0 mt-0.5 bg-white text-[var(--ink,#0F172A)] border border-[#e9eaef] rounded-md px-3 py-1.5 text-[12px] font-medium hover:bg-[#fafafa] transition-colors flex items-center gap-1"
        >
          View <ExternalLink className="h-3 w-3" />
        </a>
      ) : (
        <button className="shrink-0 mt-0.5 bg-white text-[var(--ink,#0F172A)] border border-[#e9eaef] rounded-md px-3 py-1.5 text-[12px] font-medium cursor-pointer hover:bg-[#fafafa] transition-colors">
          View
        </button>
      )}
    </div>
  );
}

function CompanyRow({ c, last }: { c: AgentCompany; last: boolean }) {
  return (
    <div
      className="flex items-center gap-3.5 py-3"
      style={{ borderBottom: last ? "none" : "1px solid #f1f1f4" }}
    >
      <Initial name={c.name} size={30} />
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline gap-2">
          <span className="text-[13.5px] font-semibold tracking-[-0.01em]">
            {c.name}
          </span>
          {c.industry && (
            <span className="text-[11.5px] text-[#b3b8c7]">{c.industry}</span>
          )}
        </div>
        {c.reason && (
          <div className="text-[12.5px] text-[#8089a0] mt-0.5">{c.reason}</div>
        )}
      </div>
      <Link to={`/find?q=${encodeURIComponent(`People at ${c.name}`)}`}>
        <button className="shrink-0 bg-white text-[var(--ink,#0F172A)] border border-[#e9eaef] rounded-md px-3 py-1.5 text-[12px] font-medium cursor-pointer hover:bg-[#fafafa] transition-colors">
          Find people
        </button>
      </Link>
    </div>
  );
}

// ── Approval row ─────────────────────────────────────────────────────────

function ApprovalRow({
  action,
  onApprove,
  onReject,
  last,
}: {
  action: AgentAction;
  onApprove: () => void;
  onReject: () => void;
  last: boolean;
}) {
  const [loading, setLoading] = useState(false);
  const verb = ACTION_VERBS[action.action] || action.action;

  const handleApprove = async () => {
    setLoading(true);
    try {
      await onApprove();
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      className="flex gap-3.5 py-3.5"
      style={{ borderBottom: last ? "none" : "1px solid #f1f1f4" }}
    >
      <Initial name={action.company || action.action} size={32} />
      <div className="flex-1 min-w-0">
        <div className="text-[13.5px] font-semibold tracking-[-0.01em] mb-0.5">
          {verb}{" "}
          <span style={{ color: "var(--ink, #0F172A)" }}>
            {action.company || ""}
          </span>
        </div>
        {action.reason && (
          <div className="text-[12.5px] text-[#8089a0] leading-snug">
            {action.reason}
          </div>
        )}
        {action.creditsSpent > 0 && (
          <div className="text-[11.5px] text-[#b3b8c7] mt-0.5">
            ~{action.creditsSpent} credits
          </div>
        )}
      </div>
      <div className="flex gap-1.5 shrink-0 items-start">
        <button
          onClick={onReject}
          disabled={loading}
          title="Skip this action only — the company stays in your targets"
          className="bg-white text-[#8089a0] border border-[#e9eaef] rounded-md px-3 py-1.5 text-[12px] font-medium cursor-pointer hover:bg-[#fafafa] transition-colors disabled:opacity-50 flex items-center gap-1"
        >
          <XCircle className="h-3 w-3" /> Skip this one
        </button>
        <button
          onClick={handleApprove}
          disabled={loading}
          className="bg-[var(--ink,#0F172A)] text-white border border-[var(--ink,#0F172A)] rounded-md px-3 py-1.5 text-[12px] font-medium cursor-pointer hover:opacity-90 transition-opacity disabled:opacity-50 flex items-center gap-1"
        >
          {loading ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <CheckCircle2 className="h-3 w-3" />
          )}
          Approve
        </button>
      </div>
    </div>
  );
}

// ── Activity ─────────────────────────────────────────────────────────────

const ACTION_VERBS: Record<string, string> = {
  plan: "Planning actions",
  find: "Found contacts at",
  find_jobs: "Discovered job",
  discover_companies: "Discovered company",
  find_hiring_managers: "Found hiring manager at",
  draft: "Drafted email to",
  send: "Sent email to",
  follow_up: "Followed up with",
  monitor: "Monitored",
  skip: "Skipped",
};

// (Legacy flat ActivityList removed — replaced by ActivityTimeline with day grouping)

// ── Flip counter (animated number with roll transition) ──────────────────

function FlipCounter({ label, value }: { label: string; value: number }) {
  const prevRef = useRef(value);
  const [animating, setAnimating] = useState(false);

  useEffect(() => {
    if (value !== prevRef.current) {
      prevRef.current = value;
      setAnimating(true);
      const t = setTimeout(() => setAnimating(false), 300);
      return () => clearTimeout(t);
    }
  }, [value]);

  return (
    <div className="text-center">
      <div
        className="text-[28px] font-semibold tabular-nums transition-transform duration-200"
        style={{
          color: "var(--ink, #0F172A)",
          transform: animating ? "translateY(-2px)" : "translateY(0)",
          opacity: animating ? 0.7 : 1,
        }}
      >
        {value}
      </div>
      <div className="text-[11px] text-muted-foreground mt-0.5 tracking-wide uppercase">
        {label}
      </div>
    </div>
  );
}

// ── Step list (D5 — vertical progress during "Run now") ──────────────────

const ACTION_LABELS: Record<string, string> = {
  plan: "Planning actions",
  find: "Searching for contacts",
  find_jobs: "Finding matching jobs",
  discover_companies: "Discovering companies",
  find_hiring_managers: "Finding hiring managers",
  follow_up: "Preparing follow-ups",
  draft: "Drafting emails",
};

function StepList({
  completedActions,
  currentAction,
  currentLabel,
  plannedActions,
}: {
  completedActions: CycleStep[];
  currentAction: string | null;
  currentLabel: string | null;
  plannedActions: string[];
}) {
  // Derive upcoming from planned minus completed and current
  const doneSet = new Set(completedActions.map((a) => a.action));
  const upcoming = plannedActions.filter(
    (a) => !doneSet.has(a) && a !== currentAction
  );

  return (
    <div className="space-y-2.5">
      {completedActions.map((step, i) => (
        <div key={i} className="flex items-start gap-2.5 animate-in slide-in-from-top duration-300">
          <Check className="h-3.5 w-3.5 text-green-500 mt-0.5 shrink-0" />
          <div>
            <span className="text-[13px] text-[var(--ink,#0F172A)]">
              {step.label || ACTION_LABELS[step.action] || step.action}
            </span>
            {step.resultSummary && (
              <span className="text-[11.5px] text-[#8089a0] ml-1.5">
                — {step.resultSummary}
              </span>
            )}
          </div>
        </div>
      ))}
      {currentAction && (
        <div className="flex items-start gap-2.5">
          <Loader2 className="h-3.5 w-3.5 animate-spin text-blue-500 mt-0.5 shrink-0" />
          <span className="text-[13px] text-[var(--ink,#0F172A)]">
            {currentLabel || ACTION_LABELS[currentAction] || currentAction}
          </span>
        </div>
      )}
      {upcoming.map((action, i) => (
        <div key={i} className="flex items-start gap-2.5 opacity-40">
          <div className="h-3.5 w-3.5 rounded-full border border-[#d1d5dc] mt-0.5 shrink-0" />
          <span className="text-[13px]">{ACTION_LABELS[action] || action}</span>
        </div>
      ))}
    </div>
  );
}

// ── Right rail — agent activity tracker ─────────────────────────────────────

function MiniStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="border border-[#e9eaef] rounded-md p-2.5" style={{ background: "#fff" }}>
      <div className="text-[9.5px] uppercase tracking-[0.06em] text-[#8089a0] font-medium">
        {label}
      </div>
      <div style={{
        fontSize: 16, fontWeight: 500,
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        fontVariantNumeric: "tabular-nums", marginTop: 2,
        color: "var(--ink, #0F172A)",
      }}>{value}</div>
    </div>
  );
}

export function AgentActivityRail() {
  const liveActions = useAgentActivityLive();
  const stats = useAgentStats();
  const countdown = useCountdown(stats.data?.nextCycleAt);

  const pendingCount = liveActions.filter((a) => a.status === "executing" || a.status === "pending_approval").length;
  const runningCount = liveActions.filter((a) => a.status === "executing").length;

  return (
    <aside className="hidden lg:flex w-[300px] shrink-0 border-l border-[#e9eaef] flex-col sticky top-0 h-screen overflow-y-auto"
      style={{ background: "var(--paper-2, #fafafa)", padding: "16px 18px" }}>
      <AgentStyles />
      <div className="flex items-center gap-2 mb-1">
        <PulseDot />
        <span className="text-[11px] uppercase tracking-[0.06em] text-[#4b5567] font-medium">
          Agent · running
        </span>
      </div>

      <div className="grid grid-cols-2 gap-2 mb-4 mt-3">
        <MiniStat label="queued" value={pendingCount} />
        <MiniStat label="running" value={runningCount} />
      </div>

      <div className="text-[11px] uppercase tracking-[0.06em] text-[#8089a0] font-medium mb-2">
        Activity
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto">
        {liveActions.length === 0 && (
          <div className="text-[12px] text-[#8089a0] py-4">No activity yet.</div>
        )}
        {liveActions.slice(0, 20).map((a, i, arr) => {
          const kind = ACTION_KIND[a.action] || "watch";
          const meta = KIND_META[kind];
          const verb = ACTION_VERBS[a.action] || a.action;
          return (
            <div key={a.id} style={{
              padding: "8px 0",
              borderBottom: i === arr.length - 1 ? "none" : "1px solid #f1f1f4",
            }}>
              <div className="flex items-center gap-2 mb-0.5">
                <span className="rounded-full shrink-0" style={{ width: 5, height: 5, background: meta.color }} />
                <span style={{
                  fontSize: 9.5,
                  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                  color: meta.color, textTransform: "uppercase", letterSpacing: "0.06em",
                }}>{meta.label}</span>
                <span className="ml-auto" style={{
                  fontSize: 10, color: "#b3b8c7",
                  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                }}>{a.createdAt ? relativeTime(a.createdAt) : ""}</span>
              </div>
              <div className="text-[12px] text-[#4b5567] leading-snug pl-[13px]">
                {verb} {a.company || ""}
                {a.status === "failed" && <span className="text-red-500 ml-1">· failed</span>}
                {a.status === "executing" && <span className="text-blue-500 ml-1">· running</span>}
              </div>
            </div>
          );
        })}
      </div>

      {countdown && (
        <div className="mt-3 pt-3 border-t border-[#e9eaef] text-[11px] text-[#8089a0]">
          Next cycle in <span className="text-[#4b5567] font-medium">{countdown}</span>
        </div>
      )}
    </aside>
  );
}

// ── New components ported from the Variant C handoff ────────────────────────

function greetingPart(): string {
  const h = new Date().getHours();
  if (h < 12) return "morning";
  if (h < 18) return "afternoon";
  return "evening";
}

function BigCounter({ n, label }: { n: number; label: string }) {
  return (
    <div style={{ minWidth: 90 }}>
      <div
        className="font-serif tracking-[-0.03em]"
        style={{
          color: "var(--ink, #0F172A)",
          fontSize: "clamp(40px, 5vw, 56px)",
          lineHeight: 0.95,
          fontVariantNumeric: "tabular-nums",
          fontWeight: 400,
        }}
      >
        {n}
      </div>
      <div
        className="mt-1.5 text-[11.5px] text-muted-foreground"
        style={{ letterSpacing: "-0.01em" }}
      >
        {label}
      </div>
    </div>
  );
}

function NumberedDraftRow({
  d,
  index,
  last,
}: {
  d: {
    name: string;
    role: string;
    company: string;
    subject: string;
    preview: string;
    isHm: boolean;
    time: string;
  };
  index: number;
  last: boolean;
}) {
  return (
    <div
      className="flex gap-5 py-4"
      style={{ borderBottom: last ? "none" : "1px solid #f1f1f4" }}
    >
      <span
        className="font-serif shrink-0"
        style={{
          fontSize: 28,
          color: "#b3b8c7",
          width: 36,
          fontVariantNumeric: "tabular-nums",
          lineHeight: 1,
          paddingTop: 4,
          fontWeight: 400,
        }}
      >
        {String(index).padStart(2, "0")}
      </span>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap mb-1">
          <span className="text-[13.5px] font-semibold tracking-[-0.01em]">
            {d.name}
          </span>
          <span className="text-[12.5px] text-[#8089a0]">
            {d.role}
            {d.company && `, ${d.company}`}
          </span>
          {d.isHm && (
            <span
              className="font-serif text-[12px] italic text-muted-foreground"
              style={{ fontWeight: 400 }}
            >
              · hiring manager
            </span>
          )}
        </div>
        {d.subject && (
          <div
            className="font-serif tracking-[-0.01em] mb-1"
            style={{ fontSize: 17, color: "var(--ink, #0F172A)", fontWeight: 400 }}
          >
            {d.subject}
          </div>
        )}
        {d.preview && (
          <div className="text-[12.5px] text-[#8089a0] leading-snug line-clamp-2">
            {d.preview}
          </div>
        )}
      </div>
      <div className="flex flex-col items-end gap-1.5 shrink-0">
        {d.time && (
          <span className="text-[11.5px] text-[#b3b8c7] whitespace-nowrap">
            {d.time}
          </span>
        )}
        <div className="flex gap-1.5">
          <Link to="/contacts">
            <button className="bg-white text-[var(--ink,#0F172A)] border border-[#e9eaef] rounded-md px-3 py-1.5 text-[12px] font-medium cursor-pointer hover:bg-[#fafafa] transition-colors">
              Edit
            </button>
          </Link>
          <Link to="/contacts">
            <button className="bg-[var(--ink,#0F172A)] text-white border border-[var(--ink,#0F172A)] rounded-md px-3 py-1.5 text-[12px] font-medium cursor-pointer hover:opacity-90 transition-opacity">
              Send
            </button>
          </Link>
        </div>
      </div>
    </div>
  );
}

function RepliesPanel({
  pipelineCompanies,
}: {
  pipelineCompanies: CompanyPipeline[];
}) {
  const withReplies = pipelineCompanies.filter((c) => (c.replies || 0) > 0);
  if (withReplies.length === 0) {
    return (
      <EmptyText>
        No replies yet. When contacts respond, they'll surface here — open in inbox for the full thread.
      </EmptyText>
    );
  }
  return (
    <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-6">
      <aside
        className="border border-[#e9eaef] rounded-lg overflow-hidden"
        style={{ background: "#fff" }}
      >
        <div
          className="px-3.5 py-3 border-b border-[#f1f1f4]"
          style={{
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
            fontSize: 10,
            letterSpacing: "0.14em",
            textTransform: "uppercase",
            color: "#8089a0",
          }}
        >
          {withReplies.length} {withReplies.length === 1 ? "company" : "companies"} replied
        </div>
        {withReplies.map((c, i) => (
          <Link
            key={c.name}
            to={`/contacts?q=${encodeURIComponent(c.name)}`}
            className="block px-3.5 py-3 hover:bg-[#fafafa] transition-colors"
            style={{
              borderBottom:
                i === withReplies.length - 1 ? "none" : "1px solid #f1f1f4",
            }}
          >
            <div className="flex items-center gap-2.5 mb-1">
              <Initial name={c.name} size={26} />
              <span className="text-[13px] font-semibold tracking-[-0.01em]">
                {c.name}
              </span>
              <span className="ml-auto text-[16a34a]" style={{ color: "#16a34a", fontSize: 11, fontWeight: 600 }}>
                {c.replies} {c.replies === 1 ? "reply" : "replies"}
              </span>
            </div>
            <div className="text-[11.5px] text-[#8089a0] pl-[34px]">
              {c.contacts} contact{c.contacts !== 1 ? "s" : ""}
              {c.draftsReady > 0 && ` · ${c.draftsReady} drafts`}
            </div>
          </Link>
        ))}
      </aside>
      <section
        className="border border-[#e9eaef] rounded-lg p-6"
        style={{ background: "#fff" }}
      >
        <div
          style={{
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
            fontSize: 10,
            letterSpacing: "0.14em",
            textTransform: "uppercase",
            color: "#8089a0",
            marginBottom: 14,
          }}
        >
          How replies work
        </div>
        <p
          className="font-serif italic"
          style={{
            fontSize: 18,
            color: "var(--ink, #0F172A)",
            fontWeight: 400,
            lineHeight: 1.4,
            marginBottom: 10,
          }}
        >
          When a contact responds, Offerloop drafts a suggested reply and moves them into your tracker.
        </p>
        <p className="text-[13px] text-muted-foreground leading-relaxed">
          Pick a company on the left to jump into the full conversation, see the agent-drafted response, and approve or edit before sending.
        </p>
        <Link
          to="/contacts"
          className="inline-block mt-4 text-[12px] font-medium"
          style={{ color: "var(--ink, #0F172A)" }}
        >
          Open contacts →
        </Link>
      </section>
    </div>
  );
}

function EditorialJobList({ jobs }: { jobs: AgentJob[] }) {
  return (
    <div>
      {jobs.map((j, i) => (
        <div
          key={j.id}
          className="flex items-start gap-5 py-4"
          style={{ borderBottom: i === jobs.length - 1 ? "none" : "1px solid #f1f1f4" }}
        >
          <span
            className="font-serif shrink-0"
            style={{
              fontSize: 44,
              color: i < 3 ? "var(--ink, #0F172A)" : "#b3b8c7",
              width: 60,
              lineHeight: 1,
              letterSpacing: "-0.03em",
              fontVariantNumeric: "tabular-nums",
              fontWeight: 400,
            }}
          >
            {String(i + 1).padStart(2, "0")}
          </span>
          <div className="flex-1 min-w-0">
            <div
              className="font-serif tracking-[-0.015em] mb-1.5"
              style={{ fontSize: 18, color: "var(--ink, #0F172A)", fontWeight: 400 }}
            >
              {j.title}
            </div>
            <div className="text-[12.5px] text-[#8089a0] mb-2">
              {j.company}
              {j.location && ` · ${j.location}`}
            </div>
            {j.matchReasons?.length > 0 && (
              <div className="flex gap-1.5 flex-wrap">
                {j.matchReasons.slice(0, 3).map((r) => (
                  <span
                    key={r}
                    className="rounded-full"
                    style={{
                      fontSize: 10.5,
                      padding: "2.5px 9px",
                      background: "#fafbff",
                      border: "1px solid #e9eaef",
                      color: "#4b5567",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {r}
                  </span>
                ))}
              </div>
            )}
          </div>
          {j.applyLink ? (
            <a
              href={j.applyLink}
              target="_blank"
              rel="noopener noreferrer"
              className="shrink-0 mt-1.5 text-[12px] font-medium hover:underline"
              style={{ color: "var(--ink, #0F172A)" }}
            >
              View →
            </a>
          ) : (
            <span className="shrink-0 text-[12px] text-[#8089a0]">no link</span>
          )}
        </div>
      ))}
    </div>
  );
}

function PipelineKanban({ companies }: { companies: CompanyPipeline[] }) {
  // Derive stage from existing per-company counts:
  //   replies > 0          → Replied
  //   draftsReady > 0      → Drafted
  //   contacts > 0         → Researching
  //   otherwise            → Closed
  const columns: Record<"Researching" | "Drafted" | "Replied" | "Closed", CompanyPipeline[]> = {
    Researching: [],
    Drafted: [],
    Replied: [],
    Closed: [],
  };
  for (const c of companies) {
    if ((c.replies || 0) > 0) columns.Replied.push(c);
    else if ((c.draftsReady || 0) > 0) columns.Drafted.push(c);
    else if ((c.contacts || 0) > 0) columns.Researching.push(c);
    else columns.Closed.push(c);
  }
  const order: Array<keyof typeof columns> = ["Researching", "Drafted", "Replied", "Closed"];
  return (
    <div className="grid gap-3.5" style={{ gridTemplateColumns: "repeat(4, minmax(0, 1fr))" }}>
      {order.map((col) => (
        <div
          key={col}
          className="border border-[#e9eaef] rounded-lg p-3.5"
          style={{ background: "#fff", minHeight: 240 }}
        >
          <div
            className="flex items-baseline justify-between pb-2.5 mb-3"
            style={{ borderBottom: "1px solid #f1f1f4" }}
          >
            <span className="text-[13px] font-semibold tracking-[-0.01em]" style={{ color: "var(--ink, #0F172A)" }}>
              {col}
            </span>
            <span
              style={{
                fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                fontSize: 11,
                color: "#8089a0",
              }}
            >
              {columns[col].length}
            </span>
          </div>
          {columns[col].length === 0 ? (
            <div className="text-[12px] text-[#b3b8c7] italic py-2">—</div>
          ) : (
            <div className="space-y-2">
              {columns[col].map((c) => (
                <Link
                  key={c.name}
                  to={`/contacts?q=${encodeURIComponent(c.name)}`}
                  className="block rounded-md p-2.5 hover:bg-[#fafbff] transition-colors"
                  style={{ border: "1px solid #f1f1f4", background: "#fafafa" }}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <Initial name={c.name} size={20} />
                    <span className="text-[12.5px] font-semibold tracking-[-0.01em]">
                      {c.name}
                    </span>
                  </div>
                  <div className="text-[11px] text-[#8089a0] pl-[28px]">
                    {c.contacts} contact{c.contacts !== 1 ? "s" : ""}
                    {c.hms > 0 && ` · ${c.hms} HM`}
                    {c.jobs > 0 && ` · ${c.jobs} job${c.jobs !== 1 ? "s" : ""}`}
                  </div>
                </Link>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function ActivityTimeline({ items }: { items: AgentAction[] }) {
  // Group items by calendar day (Today / Yesterday / Date)
  const groups: Array<[string, AgentAction[]]> = [];
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);
  const labelFor = (iso: string | undefined): string => {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(+d)) return "—";
    const day = new Date(d);
    day.setHours(0, 0, 0, 0);
    if (+day === +today) return "Today";
    if (+day === +yesterday) return "Yesterday";
    return day.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  };
  for (const a of items) {
    const label = labelFor(a.createdAt);
    const last = groups[groups.length - 1];
    if (last && last[0] === label) last[1].push(a);
    else groups.push([label, [a]]);
  }
  return (
    <div className="space-y-7">
      {groups.map(([day, entries]) => (
        <div key={day}>
          <div
            className="flex items-center gap-3 mb-3.5"
            style={{
              fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
              fontSize: 10,
              color: "#8089a0",
              letterSpacing: "0.14em",
              textTransform: "uppercase",
            }}
          >
            <span style={{ flex: 1, height: 1, background: "#f1f1f4" }} />
            {day}
            <span style={{ flex: 1, height: 1, background: "#f1f1f4" }} />
          </div>
          <div
            className="pl-5 ml-1.5"
            style={{ borderLeft: "1px solid #f1f1f4" }}
          >
            {entries.map((a, i) => {
              const kind = ACTION_KIND[a.action] || "watch";
              const meta = KIND_META[kind];
              const verb = ACTION_VERBS[a.action] || a.action;
              const ts = a.createdAt
                ? new Date(a.createdAt).toLocaleTimeString([], {
                    hour: "2-digit",
                    minute: "2-digit",
                    hour12: false,
                  })
                : "";
              return (
                <div key={a.id} className="relative pb-3.5" style={{ paddingLeft: 4 }}>
                  <span
                    style={{
                      position: "absolute",
                      left: -27,
                      top: 6,
                      width: 9,
                      height: 9,
                      borderRadius: "50%",
                      background: meta.color,
                      border: "2px solid #fff",
                    }}
                  />
                  <div className="flex items-baseline gap-3">
                    <span
                      style={{
                        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                        fontSize: 11,
                        color: "#8089a0",
                        width: 56,
                        flexShrink: 0,
                      }}
                    >
                      {ts}
                    </span>
                    <div className="flex-1 min-w-0">
                      <div
                        className="text-[13px] tracking-[-0.01em]"
                        style={{ color: "var(--ink, #0F172A)" }}
                      >
                        <span className="font-semibold">{verb}</span>
                        {a.company && (
                          <span style={{ color: "#4b5567" }}> {a.company}</span>
                        )}
                        {a.status === "failed" && (
                          <span className="text-red-500 ml-1">· failed</span>
                        )}
                        {a.status === "executing" && (
                          <span className="text-blue-500 ml-1">· running</span>
                        )}
                      </div>
                      {a.reason && (
                        <div className="text-[11.5px] text-[#8089a0] mt-0.5">
                          {a.reason}
                        </div>
                      )}
                    </div>
                    {a.creditsSpent > 0 && (
                      <span
                        style={{
                          fontFamily:
                            "ui-monospace, SFMono-Regular, Menlo, monospace",
                          fontSize: 10.5,
                          color: "#8089a0",
                          padding: "2px 7px",
                          borderRadius: 100,
                          background: "#fafbff",
                          border: "1px solid #f1f1f4",
                        }}
                      >
                        {a.creditsSpent}c
                      </span>
                    )}
                  </div>
                  {i === entries.length - 1 && day !== groups[groups.length - 1][0] && (
                    <span /> /* spacer */
                  )}
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

function ApprovalsPolicyRail({
  config,
  stats,
}: {
  config: AgentConfig;
  stats: ReturnType<typeof useAgentSnapshot>["stats"]["data"];
}) {
  const cadence = (() => {
    const days = (config as { cycleScheduleDays?: number[] }).cycleScheduleDays;
    if (Array.isArray(days)) {
      if (days.length === 7) return "daily";
      if (days.length === 5) return "weekday";
      if (days.length === 1) return "weekly";
      return `${days.length}×/week`;
    }
    return "daily";
  })();
  return (
    <aside
      className="lg:border-l lg:pl-6 lg:border-[#f1f1f4]"
      style={{ alignSelf: "flex-start" }}
    >
      <div
        className="mb-4"
        style={{
          fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
          fontSize: 10,
          color: "#8089a0",
          letterSpacing: "0.16em",
          textTransform: "uppercase",
        }}
      >
        Budget · Policy
      </div>
      <PolicyRow label="Cadence" value={cadence} />
      <PolicyRow
        label="Credit budget / week"
        value={`${config.creditBudgetPerWeek || 0}c`}
      />
      <PolicyRow
        label="Credits spent / week"
        value={`${stats?.creditsSpentThisWeek ?? 0}c`}
      />
      <PolicyRow
        label="Target companies"
        value={String(config.targetCompanies?.length || 0)}
      />
      <PolicyRow
        label="Approval mode"
        value={config.approvalMode === "autopilot" ? "autopilot" : "review first"}
      />
      <PolicyRow
        label="Last cycle"
        value={config.lastCycleAt ? relativeTime(config.lastCycleAt) : "never"}
      />
    </aside>
  );
}

function PolicyRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="mb-3.5">
      <div
        style={{
          fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
          fontSize: 10,
          color: "#8089a0",
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          marginBottom: 4,
        }}
      >
        {label}
      </div>
      <div
        className="text-[13px] font-medium"
        style={{ color: "var(--ink, #0F172A)" }}
      >
        {value}
      </div>
    </div>
  );
}
