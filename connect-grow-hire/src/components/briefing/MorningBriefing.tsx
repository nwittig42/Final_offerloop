import { useState, useEffect, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import {
  MessageSquareReply,
  Clock,
  CalendarClock,
  Target,
  Search,
  Mail,
  ChevronRight,
  ArrowUpRight,
  TrendingUp,
} from "lucide-react";
import { apiService } from "@/services/api";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { useScout } from "@/contexts/ScoutContext";

interface BriefingReply {
  contactId: string;
  contactName: string;
  company: string;
  snippet: string;
  replyDraftBody?: string;
  replyDraftStatus?: string;
}

interface BriefingFollowUp {
  contactId: string;
  contactName: string;
  company: string;
  daysSinceEmail: number;
}

interface BriefingDeadline {
  industry: string;
  event: string;
  date: string;
  urgency: "urgent" | "upcoming" | "future";
}

interface BriefingData {
  replies: BriefingReply[];
  followUps: BriefingFollowUp[];
  roadmapProgress: {
    currentWeek: number;
    weekTheme: string;
    emailsSent: number;
    emailTarget: number;
    repliesReceived: number;
    replyTarget: number;
    status: "ahead" | "on_track" | "behind";
  } | null;
  deadlines: BriefingDeadline[];
  pipelineStats: {
    active: number;
    needsAttention: number;
    done: number;
    totalContacts: number;
  };
  meta: {
    tier: string;
    hasRoadmap: boolean;
    hasContacts: boolean;
    isNewUser: boolean;
  };
}

// ── Helpers ────────────────────────────────────────────────────────────────

function timeOfDayGreeting(): string {
  const h = new Date().getHours();
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  return "Good evening";
}

/**
 * Compute "what changed since last visit" by diffing current counts against a
 * localStorage snapshot from the previous render. Returns either a phrase like
 * "2 new replies, 1 new follow-up" or an empty string for no positive change.
 */
function buildWhatsNew(data: BriefingData): { text: string; sinceLabel: string } | null {
  const KEY = "ofl_briefing_last_seen";
  let prev: { replies: number; followUps: number; total: number; ts: number } | null = null;
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) prev = JSON.parse(raw);
  } catch {}
  const currentTotal = data.pipelineStats?.totalContacts || 0;
  const current = {
    replies: data.replies.length,
    followUps: data.followUps.length,
    total: currentTotal,
    ts: Date.now(),
  };
  // Always update the snapshot for next visit.
  try {
    localStorage.setItem(KEY, JSON.stringify(current));
  } catch {}
  if (!prev) return null;

  const newReplies = Math.max(0, current.replies - prev.replies);
  const newFollowUps = Math.max(0, current.followUps - prev.followUps);
  const newContacts = Math.max(0, current.total - prev.total);
  if (newReplies === 0 && newFollowUps === 0 && newContacts === 0) return null;

  const parts: string[] = [];
  if (newReplies > 0) parts.push(`${newReplies} new ${newReplies === 1 ? "reply" : "replies"}`);
  if (newFollowUps > 0) parts.push(`${newFollowUps} new follow-up${newFollowUps === 1 ? "" : "s"}`);
  if (newContacts > 0) parts.push(`${newContacts} new contact${newContacts === 1 ? "" : "s"} saved`);

  // Phrase the time gap
  const gapMs = current.ts - (prev.ts || current.ts);
  const gapH = Math.round(gapMs / (60 * 60 * 1000));
  let sinceLabel: string;
  if (gapH < 1) sinceLabel = "since you were just here";
  else if (gapH < 24) sinceLabel = `since ${gapH} hour${gapH === 1 ? "" : "s"} ago`;
  else if (gapH < 48) sinceLabel = "since yesterday";
  else sinceLabel = `since ${Math.round(gapH / 24)} days ago`;

  return { text: parts.join(" · "), sinceLabel };
}

/**
 * Pick the single most-actionable next step the user should take, based on
 * whatever the briefing data says. Used as the "Today's focus" hero card.
 */
function pickTodayFocus(data: BriefingData): {
  label: string;
  caption: string;
  onClick: () => void;
  navigate: ReturnType<typeof useNavigate>;
} | null {
  // Filled in by the caller — placeholder shape.
  return null;
}

// ── Sub-components ─────────────────────────────────────────────────────────

function HeroCard({
  number,
  unit,
  caption,
  onClick,
  loading,
}: {
  number: number | string;
  unit: string;
  caption: string;
  onClick?: () => void;
  loading?: boolean;
}) {
  const interactive = !!onClick && !loading;
  // When the "number" is actually a text label (e.g. "Find more contacts"),
  // we drop to a smaller font + tighter weight so it doesn't dwarf the rest of
  // the card. Numbers stay big as the focal element; text titles read like a
  // headline.
  const isNumeric = typeof number === "number";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!interactive}
      style={{
        flex: 1,
        textAlign: "left",
        padding: "16px 18px",
        background: "var(--warm-surface, #FAFBFF)",
        border: "1px solid var(--line, #E8E4DE)",
        borderRadius: 8,
        cursor: interactive ? "pointer" : "default",
        transition: "border-color .15s, background .15s",
        position: "relative",
        minHeight: 108,
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
      onMouseEnter={(e) => {
        if (!interactive) return;
        e.currentTarget.style.borderColor = "#5B7799";
        e.currentTarget.style.background = "white";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = "var(--line, #E8E4DE)";
        e.currentTarget.style.background = "var(--warm-surface, #FAFBFF)";
      }}
    >
      <span
        style={{
          fontSize: 10,
          fontWeight: 500,
          letterSpacing: "0.08em",
          textTransform: "uppercase",
          color: "var(--ink-3, #8A8F9A)",
          fontFamily: "'JetBrains Mono', monospace",
        }}
      >
        {unit}
      </span>
      <span
        style={{
          fontSize: isNumeric ? 28 : 16,
          fontWeight: isNumeric ? 600 : 500,
          color: "var(--ink, #0F172A)",
          lineHeight: isNumeric ? 1 : 1.25,
          fontFamily: "'Inter', system-ui, sans-serif",
          letterSpacing: isNumeric ? "-0.02em" : "-0.01em",
        }}
      >
        {loading ? "—" : number}
      </span>
      <span
        style={{
          fontSize: 12,
          color: "var(--ink-3, #8A8F9A)",
          lineHeight: 1.4,
          marginTop: 2,
        }}
      >
        {caption}
      </span>
      {interactive && (
        <ArrowUpRight
          style={{
            position: "absolute",
            top: 14,
            right: 14,
            width: 12,
            height: 12,
            color: "var(--ink-3, #8A8F9A)",
            opacity: 0.4,
          }}
        />
      )}
    </button>
  );
}

function PipelineBar({
  active,
  needsAttention,
  done,
  total,
  onClick,
}: {
  active: number;
  needsAttention: number;
  done: number;
  total: number;
  onClick: () => void;
}) {
  if (total === 0) return null;
  const activePct = (active / total) * 100;
  const needsPct = (needsAttention / total) * 100;
  const donePct = (done / total) * 100;
  // Muted palette — a touch of blue lives in the "active" segment so the
  // pipeline still has some life, while the rest stays in warm/cool greys.
  const COLORS = {
    active: "#5B7799",       // muted slate-blue (the one bit of color)
    needs: "#A8A29E",        // warm-grey-400
    done: "#94A3B8",         // slate-400 (lightest, for completed)
  };
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        background: "var(--warm-surface, #FAFBFF)",
        border: "1px solid var(--line, #E8E4DE)",
        borderRadius: 8,
        padding: "14px 18px",
        cursor: "pointer",
        textAlign: "left",
        width: "100%",
        transition: "border-color .15s, background .15s",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = "#5B7799";
        e.currentTarget.style.background = "white";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = "var(--line, #E8E4DE)";
        e.currentTarget.style.background = "var(--warm-surface, #FAFBFF)";
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
        <span
          style={{
            fontSize: 10,
            fontWeight: 500,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            color: "var(--ink-3, #8A8F9A)",
            fontFamily: "'JetBrains Mono', monospace",
          }}
        >
          PIPELINE
        </span>
        <span style={{ fontSize: 11, color: "var(--ink-3, #8A8F9A)" }}>
          {total} total
        </span>
      </div>
      <div
        style={{
          display: "flex",
          height: 6,
          borderRadius: 2,
          overflow: "hidden",
          background: "var(--line, #F0F0ED)",
          marginBottom: 10,
        }}
      >
        {active > 0 && (
          <div style={{ width: `${activePct}%`, background: COLORS.active }} title={`${active} active`} />
        )}
        {needsAttention > 0 && (
          <div style={{ width: `${needsPct}%`, background: COLORS.needs }} title={`${needsAttention} needs attention`} />
        )}
        {done > 0 && (
          <div style={{ width: `${donePct}%`, background: COLORS.done }} title={`${done} done`} />
        )}
      </div>
      <div style={{ display: "flex", gap: 16, fontSize: 11, color: "var(--ink-2, #4A4F5B)" }}>
        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ width: 6, height: 6, borderRadius: 1, background: COLORS.active }} />
          {active} active
        </span>
        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ width: 6, height: 6, borderRadius: 1, background: COLORS.needs }} />
          {needsAttention} needs attention
        </span>
        <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ width: 6, height: 6, borderRadius: 1, background: COLORS.done }} />
          {done} done
        </span>
      </div>
    </button>
  );
}

function WeeklyProgress({
  emailsSent,
  emailTarget,
  repliesReceived,
  replyTarget,
  status,
}: {
  emailsSent: number;
  emailTarget: number;
  repliesReceived: number;
  replyTarget: number;
  status: "ahead" | "on_track" | "behind";
}) {
  const emailPct = Math.min((emailsSent / Math.max(emailTarget, 1)) * 100, 100);
  const replyPct = Math.min((repliesReceived / Math.max(replyTarget, 1)) * 100, 100);
  const statusLabel = status === "ahead" ? "Ahead of plan" : status === "behind" ? "Behind plan" : "On track";
  return (
    <div
      style={{
        background: "var(--warm-surface, #FAFBFF)",
        border: "1px solid var(--line, #E8E4DE)",
        borderRadius: 8,
        padding: "14px 18px",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
        <span
          style={{
            fontSize: 10,
            fontWeight: 500,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            color: "var(--ink-3, #8A8F9A)",
            fontFamily: "'JetBrains Mono', monospace",
          }}
        >
          THIS WEEK
        </span>
        <span style={{ fontSize: 11, color: "var(--ink-3, #8A8F9A)" }}>
          {statusLabel}
        </span>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
        <div>
          <div style={{ fontSize: 11, color: "var(--ink-3, #8A8F9A)", marginBottom: 4 }}>
            Emails sent
          </div>
          <div style={{ fontSize: 18, fontWeight: 600, color: "var(--ink, #0F172A)", lineHeight: 1 }}>
            {emailsSent}
            <span style={{ fontSize: 12, fontWeight: 400, color: "var(--ink-3, #8A8F9A)" }}>
              {" "}/ {emailTarget}
            </span>
          </div>
          <div style={{ height: 3, background: "var(--line, #F0F0ED)", borderRadius: 1, marginTop: 6, overflow: "hidden" }}>
            <div style={{ height: "100%", width: `${emailPct}%`, background: "#5B7799", transition: "width .4s" }} />
          </div>
        </div>
        <div>
          <div style={{ fontSize: 11, color: "var(--ink-3, #8A8F9A)", marginBottom: 4 }}>
            Replies received
          </div>
          <div style={{ fontSize: 18, fontWeight: 600, color: "var(--ink, #0F172A)", lineHeight: 1 }}>
            {repliesReceived}
            <span style={{ fontSize: 12, fontWeight: 400, color: "var(--ink-3, #8A8F9A)" }}>
              {" "}/ {replyTarget}
            </span>
          </div>
          <div style={{ height: 3, background: "var(--line, #F0F0ED)", borderRadius: 1, marginTop: 6, overflow: "hidden" }}>
            <div style={{ height: "100%", width: `${replyPct}%`, background: "#94A3B8", transition: "width .4s" }} />
          </div>
        </div>
      </div>
    </div>
  );
}

function AskScoutChips({
  data,
  onAsk,
}: {
  data: BriefingData;
  onAsk: (q: string) => void;
}) {
  // Build chip prompts dynamically from the briefing state. The questions
  // get auto-sent to Scout, which sees the full briefing snapshot in user_memory
  // and can answer with concrete references.
  const chips = useMemo<string[]>(() => {
    const out: string[] = [];
    if (data.replies.length > 0) {
      const first = data.replies[0];
      out.push(
        first?.contactName
          ? `Help me reply to ${first.contactName}${first.company ? ` at ${first.company}` : ""}`
          : `Help me draft replies to my ${data.replies.length} waiting messages`,
      );
    }
    if (data.followUps.length > 0) {
      out.push(`What should I say in my follow-ups today?`);
    }
    out.push(`What should I focus on right now?`);
    if (data.pipelineStats.totalContacts < 5) {
      out.push(`Find me alumni I should reach out to`);
    } else {
      out.push(`Find me more contacts like the ones I've saved`);
    }
    return out.slice(0, 4);
  }, [data]);

  // Muted blue for Scout-area accents — present but never pops.
  const SCOUT_BLUE = "#5B7799";
  return (
    <div
      style={{
        background: "var(--warm-surface, #FAFBFF)",
        border: "1px solid var(--line, #E8E4DE)",
        borderRadius: 8,
        padding: "14px 18px",
      }}
    >
      <div style={{ marginBottom: 10, display: "flex", alignItems: "center", gap: 7 }}>
        <span
          style={{
            width: 5,
            height: 5,
            borderRadius: 1,
            background: SCOUT_BLUE,
            display: "inline-block",
          }}
        />
        <span
          style={{
            fontSize: 10,
            fontWeight: 500,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            color: SCOUT_BLUE,
            fontFamily: "'JetBrains Mono', monospace",
          }}
        >
          ASK SCOUT
        </span>
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
        {chips.map((q) => (
          <button
            key={q}
            type="button"
            onClick={() => onAsk(q)}
            style={{
              padding: "7px 12px",
              fontSize: 12,
              fontWeight: 400,
              color: "var(--ink-2, #4A4F5B)",
              background: "white",
              border: "1px solid var(--line, #E8E4DE)",
              borderRadius: 6,
              cursor: "pointer",
              transition: "border-color .12s, color .12s, background .12s",
              fontFamily: "inherit",
            }}
            onMouseEnter={(e) => {
              e.currentTarget.style.borderColor = SCOUT_BLUE;
              e.currentTarget.style.color = SCOUT_BLUE;
              e.currentTarget.style.background = "rgba(91,119,153,0.04)";
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.borderColor = "var(--line, #E8E4DE)";
              e.currentTarget.style.color = "var(--ink-2, #4A4F5B)";
              e.currentTarget.style.background = "white";
            }}
          >
            {q}
          </button>
        ))}
      </div>
    </div>
  );
}

function EmptyWelcome({ onGoToSearch }: { onGoToSearch: () => void }) {
  return (
    <div
      style={{
        padding: "32px 24px",
        textAlign: "center",
        borderRadius: 12,
        border: "1px solid var(--line)",
        background: "var(--surface)",
      }}
    >
      <h3 style={{ fontSize: 16, fontWeight: 600, margin: "0 0 8px", color: "var(--ink)" }}>
        Your briefing fills in as you network
      </h3>
      <p style={{ fontSize: 13, color: "var(--ink-3)", margin: "0 0 20px", lineHeight: 1.5 }}>
        Once you start reaching out to contacts, you'll see replies, follow-ups, and weekly progress here.
      </p>
      <div style={{ display: "flex", flexDirection: "column", gap: 8, maxWidth: 280, margin: "0 auto" }}>
        <button
          onClick={onGoToSearch}
          style={{
            display: "flex", alignItems: "center", gap: 8,
            padding: "10px 16px", borderRadius: 8,
            border: "1px solid var(--line)", background: "var(--surface)",
            cursor: "pointer", fontSize: 13, color: "var(--ink)",
            textAlign: "left", width: "100%",
          }}
        >
          <Search style={{ width: 14, height: 14, color: "#3B82F6", flexShrink: 0 }} />
          <span>Find your first contact</span>
          <ChevronRight style={{ width: 12, height: 12, marginLeft: "auto", color: "var(--ink-3)" }} />
        </button>
        <button
          onClick={onGoToSearch}
          style={{
            display: "flex", alignItems: "center", gap: 8,
            padding: "10px 16px", borderRadius: 8,
            border: "1px solid var(--line)", background: "var(--surface)",
            cursor: "pointer", fontSize: 13, color: "var(--ink)",
            textAlign: "left", width: "100%",
          }}
        >
          <Mail style={{ width: 14, height: 14, color: "#10B981", flexShrink: 0 }} />
          <span>Set up Gmail integration</span>
          <ChevronRight style={{ width: 12, height: 12, marginLeft: "auto", color: "var(--ink-3)" }} />
        </button>
      </div>
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────

export function MorningBriefing() {
  const [data, setData] = useState<BriefingData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const navigate = useNavigate();
  const { user } = useFirebaseAuth();
  const { openPanelWithMessage } = useScout();

  useEffect(() => {
    if (!user?.uid) return;
    setLoading(true);
    apiService
      .getBriefing()
      .then((d) => {
        setData(d);
        // Snapshot for Scout's user_memory so chats from any page can reference
        // outstanding briefing items. 6h freshness window is enforced by the
        // reader in useScoutChat.
        try {
          localStorage.setItem(
            "ofl_briefing_snapshot",
            JSON.stringify({ ts: Date.now(), data: d }),
          );
        } catch {}
      })
      .catch(() => setError(true))
      .finally(() => setLoading(false));
  }, [user?.uid]);

  const whatsNew = useMemo(() => (data ? buildWhatsNew(data) : null), [data]);

  if (loading) {
    return (
      <div style={{ padding: "40px 0", display: "flex", justifyContent: "center" }}>
        <div style={{ fontSize: 13, color: "var(--ink-3)" }}>Loading your briefing...</div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div style={{ padding: "40px 0", textAlign: "center", fontSize: 13, color: "var(--ink-3)" }}>
        Unable to load briefing. Try refreshing.
      </div>
    );
  }

  const goToSearch = () => {
    const params = new URLSearchParams(window.location.search);
    params.set("tab", "people");
    navigate(`/find?${params.toString()}`, { replace: true });
  };

  const goToTracker = (filter?: "replies" | "followups") => {
    const url = "/contacts";
    navigate(url);
  };

  const goToContact = (contactId: string) => {
    navigate(`/contacts?contact=${contactId}`);
  };

  // Empty state for brand new users with no data
  if (data.meta.isNewUser && !data.meta.hasContacts && data.replies.length === 0) {
    return <EmptyWelcome onGoToSearch={goToSearch} />;
  }

  const isPro = data.meta.tier === "pro" || data.meta.tier === "elite";
  const firstName = (user as any)?.firstName || (user as any)?.name?.split(" ")?.[0] || "";

  // Today's focus — the single most actionable next step.
  let focusTitle: string;
  let focusCaption: string;
  let focusOnClick: () => void;
  if (data.replies.length > 0) {
    const first = data.replies[0];
    focusTitle = first.contactName || "Reply";
    focusCaption = first.company
      ? `replied · at ${first.company}`
      : "replied — draft a response";
    focusOnClick = () => goToContact(first.contactId);
  } else if (data.followUps.length > 0) {
    const first = data.followUps[0];
    focusTitle = first.contactName || "Follow up";
    focusCaption = `follow up · ${first.daysSinceEmail}d since last email`;
    focusOnClick = () => goToContact(first.contactId);
  } else if (data.deadlines.length > 0 && data.deadlines[0].urgency === "urgent") {
    focusTitle = data.deadlines[0].event;
    focusCaption = `${data.deadlines[0].industry} · in your calendar`;
    focusOnClick = () => goToSearch();
  } else if (!data.meta.hasContacts) {
    focusTitle = "Find first contact";
    focusCaption = "build your pipeline";
    focusOnClick = () => goToSearch();
  } else {
    focusTitle = "Find more contacts";
    focusCaption = "your inbox is quiet — keep building";
    focusOnClick = () => goToSearch();
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 18, paddingTop: 8 }}>
      {/* Greeting strip */}
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 12 }}>
        <div>
          <h2
            style={{
              fontSize: 18,
              fontWeight: 600,
              color: "var(--ink, #0F172A)",
              margin: 0,
              lineHeight: 1.3,
              fontFamily: "'Inter', system-ui, sans-serif",
              letterSpacing: "-0.01em",
            }}
          >
            {timeOfDayGreeting()}{firstName ? `, ${firstName}` : ""}.
          </h2>
          <p
            style={{
              fontSize: 13,
              color: "var(--ink-3, #8A8F9A)",
              margin: "2px 0 0",
              lineHeight: 1.4,
            }}
          >
            {whatsNew
              ? `${whatsNew.text} ${whatsNew.sinceLabel}.`
              : data.replies.length + data.followUps.length > 0
                ? "Here's what's outstanding."
                : "Inbox is quiet — let's queue up the next moves."}
          </p>
        </div>
      </div>

      {/* Hero row — 3 cards */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(3, 1fr)",
          gap: 12,
        }}
      >
        <HeroCard
          number={data.replies.length}
          unit="REPLIES"
          caption={
            data.replies.length === 0
              ? "no waiting replies"
              : data.replies.length === 1
                ? "waiting · draft a response"
                : "waiting · draft responses"
          }
          onClick={data.replies.length > 0 ? () => goToTracker("replies") : undefined}
        />
        <HeroCard
          number={data.followUps.length}
          unit="FOLLOW-UPS"
          caption={
            data.followUps.length === 0
              ? "all caught up"
              : data.followUps.length === 1
                ? "due to send"
                : "due to send"
          }
          onClick={data.followUps.length > 0 ? () => goToTracker("followups") : undefined}
        />
        <HeroCard
          number={focusTitle}
          unit="TODAY'S FOCUS"
          caption={focusCaption}
          onClick={focusOnClick}
        />
      </div>

      {/* Pipeline visualization */}
      {data.meta.hasContacts && data.pipelineStats && (
        <PipelineBar
          active={data.pipelineStats.active}
          needsAttention={data.pipelineStats.needsAttention}
          done={data.pipelineStats.done}
          total={data.pipelineStats.totalContacts}
          onClick={() => goToTracker()}
        />
      )}

      {/* Weekly progress (Pro/Elite when roadmap available) */}
      {isPro && data.roadmapProgress && (
        <WeeklyProgress
          emailsSent={data.roadmapProgress.emailsSent}
          emailTarget={data.roadmapProgress.emailTarget}
          repliesReceived={data.roadmapProgress.repliesReceived}
          replyTarget={data.roadmapProgress.replyTarget}
          status={data.roadmapProgress.status}
        />
      )}

      {/* Ask Scout chips — context-grounded */}
      <AskScoutChips data={data} onAsk={openPanelWithMessage} />

      {/* Recruiting calendar — pushed below, smaller, fully muted */}
      {data.deadlines.length > 0 && (
        <div
          style={{
            background: "var(--warm-surface, #FAFBFF)",
            border: "1px solid var(--line, #E8E4DE)",
            borderRadius: 8,
            padding: "14px 18px",
          }}
        >
          <div style={{ marginBottom: 10 }}>
            <span
              style={{
                fontSize: 10,
                fontWeight: 500,
                letterSpacing: "0.08em",
                textTransform: "uppercase",
                color: "var(--ink-3, #8A8F9A)",
                fontFamily: "'JetBrains Mono', monospace",
              }}
            >
              CALENDAR
            </span>
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
            {data.deadlines.map((d, i) => (
              <div
                key={i}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  fontSize: 12,
                  color: "var(--ink-2, #4A4F5B)",
                }}
              >
                <span
                  style={{
                    fontSize: 9,
                    fontWeight: 500,
                    textTransform: "uppercase",
                    letterSpacing: "0.06em",
                    padding: "2px 6px",
                    borderRadius: 3,
                    background:
                      d.urgency === "urgent"
                        ? "rgba(120, 53, 15, 0.06)"
                        : d.urgency === "upcoming"
                          ? "rgba(120, 53, 15, 0.04)"
                          : "rgba(15, 23, 42, 0.04)",
                    color:
                      d.urgency === "urgent"
                        ? "#7C2D12"
                        : d.urgency === "upcoming"
                          ? "#78350F"
                          : "var(--ink-3, #8A8F9A)",
                    fontFamily: "'JetBrains Mono', monospace",
                  }}
                >
                  {d.urgency}
                </span>
                <span>{d.event}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
