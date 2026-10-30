// Shared agent hooks — consumed by DashboardPage, AgentSnapshot, sidebar badge, etc.

import { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { useToast } from "@/hooks/use-toast";
import { doc, collection, query, orderBy, limit as fbLimit, onSnapshot } from "firebase/firestore";
import { db as firestoreDb } from "@/lib/firebase";
import {
  getAgentConfig,
  getAgentStats,
  getAgentPipeline,
  getAgentCycles,
  getAgentActivity,
  getAgentJobs,
  getAgentCompanies,
  getPendingApprovals,
  triggerAgentCycle,
  getCycleStatus,
  deployAgent,
  pauseAgent,
  stopAgent,
  updateAgentConfig,
  approveAction,
  rejectAction,
  type AgentConfig,
  type AgentAction,
} from "@/services/agent";

// ── Config ──────────────────────────────────────────────────────────────────

export function useAgentConfig() {
  const { user } = useFirebaseAuth();
  return useQuery({
    queryKey: ["agent", "config"],
    queryFn: getAgentConfig,
    enabled: !!user,
    retry: 1,
  });
}

// ── Stats (30s refetch) ─────────────────────────────────────────────────────

export function useAgentStats() {
  const { user } = useFirebaseAuth();
  return useQuery({
    queryKey: ["agent", "stats"],
    queryFn: getAgentStats,
    enabled: !!user,
    refetchInterval: 30_000,
  });
}

// ── Pipeline ────────────────────────────────────────────────────────────────

export function useAgentPipeline(enabled = true) {
  const { user } = useFirebaseAuth();
  return useQuery({
    queryKey: ["agent", "pipeline"],
    queryFn: getAgentPipeline,
    enabled: !!user && enabled,
  });
}

// ── Cycles ──────────────────────────────────────────────────────────────────

export function useAgentCycles(limit = 1, enabled = true) {
  const { user } = useFirebaseAuth();
  return useQuery({
    queryKey: ["agent", "cycles"],
    queryFn: () => getAgentCycles(limit),
    enabled: !!user && enabled,
  });
}

// ── Activity ────────────────────────────────────────────────────────────────

export function useAgentActivity(limit = 30, enabled = true) {
  const { user } = useFirebaseAuth();
  return useQuery({
    queryKey: ["agent", "activity"],
    queryFn: () => getAgentActivity(limit),
    enabled: !!user && enabled,
  });
}

// ── Jobs ────────────────────────────────────────────────────────────────────

export function useAgentJobs(limit = 10, enabled = true) {
  const { user } = useFirebaseAuth();
  return useQuery({
    queryKey: ["agent", "jobs"],
    queryFn: () => getAgentJobs(limit),
    enabled: !!user && enabled,
  });
}

// ── Companies ───────────────────────────────────────────────────────────────

export function useAgentCompanies(limit = 10, enabled = true) {
  const { user } = useFirebaseAuth();
  return useQuery({
    queryKey: ["agent", "companies"],
    queryFn: () => getAgentCompanies(limit),
    enabled: !!user && enabled,
  });
}

// ── Approvals ───────────────────────────────────────────────────────────────

export function useAgentApprovals(enabled = true) {
  const { user } = useFirebaseAuth();
  return useQuery({
    queryKey: ["agent", "approvals"],
    queryFn: getPendingApprovals,
    enabled: !!user && enabled,
    refetchInterval: 60_000,
  });
}

// ── Snapshot (bundles stats + pipeline + cycles + activity + jobs + companies) ──

export function useAgentSnapshot() {
  const stats = useAgentStats();
  const pipeline = useAgentPipeline();
  const cycles = useAgentCycles(1);
  const activity = useAgentActivity(30);
  const jobs = useAgentJobs(10);
  const companies = useAgentCompanies(10);

  return { stats, pipeline, cycles, activity, jobs, companies };
}

// ── Countdown timer ─────────────────────────────────────────────────────────

export function useCountdown(nextCycleAt: string | null | undefined) {
  const [countdown, setCountdown] = useState("");

  useEffect(() => {
    if (!nextCycleAt) {
      setCountdown("");
      return;
    }

    const tick = () => {
      const diff = new Date(nextCycleAt).getTime() - Date.now();
      if (diff <= 0) {
        setCountdown("Due now");
        return;
      }
      const h = Math.floor(diff / 3_600_000);
      const m = Math.floor((diff % 3_600_000) / 60_000);
      setCountdown(h > 0 ? `${h}h ${m}m` : `${m}m`);
    };
    tick();
    const id = setInterval(tick, 60_000);
    return () => clearInterval(id);
  }, [nextCycleAt]);

  return countdown;
}

// ── Cycle runner (Run Now + polling) ────────────────────────────────────────

// Phase D — Run-now cooldown. Prevents back-to-back manual cycles when a
// previous one finishes fast: spamming the button would charge time + tokens
// for the same brief in the same minute.
const RUN_NOW_COOLDOWN_MS = 5_000;

export function useCycleRunner() {
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const [runningCycleId, setRunningCycleId] = useState<string | null>(null);
  const [lastEndStatus, setLastEndStatus] = useState<string | null>(null);
  const [cooldownUntil, setCooldownUntil] = useState<number>(0);
  const [now, setNow] = useState<number>(() => Date.now());
  const [cycleProgress, setCycleProgress] = useState<{
    contactsFound: number;
    emailsDrafted: number;
    jobsFound: number;
    hmsFound: number;
  } | null>(null);

  // Tick once a second while a cooldown is active so the button re-enables
  // without waiting for an external re-render.
  useEffect(() => {
    if (cooldownUntil <= now) return;
    const id = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(id);
  }, [cooldownUntil, now]);

  const cycleStatusQuery = useQuery({
    queryKey: ["agent", "cycle", runningCycleId],
    queryFn: () => getCycleStatus(runningCycleId!),
    enabled: !!runningCycleId,
    refetchInterval: 2000,
  });

  useEffect(() => {
    const data = cycleStatusQuery.data;
    if (!data || !runningCycleId) return;

    setCycleProgress({
      contactsFound: data.results?.contactsFound ?? 0,
      emailsDrafted: data.results?.emailsDrafted ?? 0,
      jobsFound: data.results?.jobsFound ?? 0,
      hmsFound: data.results?.hmsFound ?? 0,
    });

    if (data.status === "completed" || data.status === "awaiting_approval") {
      setLastEndStatus(data.status);
      setRunningCycleId(null);
      setCooldownUntil(Date.now() + RUN_NOW_COOLDOWN_MS);
      const r = data.results;
      const parts: string[] = [];
      if (r?.contactsFound > 0) parts.push(`${r.contactsFound} contacts`);
      if (r?.jobsFound > 0) parts.push(`${r.jobsFound} jobs`);
      if (r?.hmsFound > 0) parts.push(`${r.hmsFound} HMs`);

      if (data.status === "awaiting_approval") {
        const actionCount = parts.length || 1;
        toast({
          title: "Actions queued for your approval",
          description: `${actionCount} action${actionCount !== 1 ? "s" : ""} ready to review in the Approvals tab.`,
        });
      } else {
        toast({
          title: parts.length > 0 ? `Found ${parts.join(", ")}` : "Cycle complete",
          description:
            r?.emailsDrafted > 0
              ? `Generated ${r.emailsDrafted} email drafts. Check your Inbox.`
              : "No new items this cycle.",
        });
      }
      queryClient.invalidateQueries({ queryKey: ["agent"] });
      setCycleProgress(null);
    }
  }, [cycleStatusQuery.data, runningCycleId, toast, queryClient]);

  const runNowMutation = useMutation({
    mutationFn: triggerAgentCycle,
    onSuccess: (data) => {
      setRunningCycleId(data.cycleId);
      setLastEndStatus(null);
      setCycleProgress({ contactsFound: 0, emailsDrafted: 0, jobsFound: 0, hmsFound: 0 });
      toast({
        title: "Agent running...",
        description: "Finding contacts, jobs, and hiring managers.",
      });
    },
    onError: (e: Error) =>
      toast({ title: "Cycle failed", description: e.message, variant: "destructive" }),
  });

  const cooldownRemainingMs = Math.max(0, cooldownUntil - now);
  const isOnCooldown = cooldownRemainingMs > 0;

  return {
    runNow: () => runNowMutation.mutate(),
    isRunNowPending: runNowMutation.isPending,
    isRunning: !!runningCycleId,
    cycleId: runningCycleId,
    cycleProgress,
    lastEndStatus,
    isOnCooldown,
    cooldownRemainingMs,
  };
}

// ── Lifecycle mutations ─────────────────────────────────────────────────────

// ── Sidebar status badge (D6 — Firestore listener) ──────────────────────────

export function useAgentSidebarStatus() {
  const { user } = useFirebaseAuth();
  const [status, setStatus] = useState<string>("setup");
  const [pendingCount, setPendingCount] = useState(0);

  useEffect(() => {
    if (!user?.uid) return;
    const ref = doc(firestoreDb, "users", user.uid, "settings", "agent_config");
    const unsub = onSnapshot(
      ref,
      (snap) => {
        if (!snap.exists()) return;
        const d = snap.data();
        setStatus(d?.status ?? "setup");
        setPendingCount(d?.pendingApprovals ?? 0);
      },
      (err) => console.warn("Agent sidebar listener:", err)
    );
    return () => unsub();
  }, [user?.uid]);

  return { status, pendingCount };
}

// ── Live activity feed (D3 — Firestore listener) ─────────────────────────────

export function useAgentActivityLive() {
  const { user } = useFirebaseAuth();
  const [actions, setActions] = useState<AgentAction[]>([]);

  useEffect(() => {
    if (!user?.uid) return;
    const q = query(
      collection(firestoreDb, "users", user.uid, "agent_actions"),
      orderBy("createdAt", "desc"),
      fbLimit(50)
    );
    const unsub = onSnapshot(
      q,
      (snap) => {
        setActions(
          snap.docs.map((d) => ({ id: d.id, ...d.data() } as AgentAction))
        );
      },
      (err) => console.warn("Agent activity listener:", err)
    );
    return () => unsub();
  }, [user?.uid]);

  return actions;
}

// ── Stepped progress (D5 — Firestore listener on cycle doc) ──────────────────

export interface CycleStep {
  action: string;
  label: string;
  resultSummary?: string;
  completedAt?: string;
}

export function useCycleProgress(cycleId: string | null) {
  const { user } = useFirebaseAuth();
  const [currentAction, setCurrentAction] = useState<string | null>(null);
  const [currentLabel, setCurrentLabel] = useState<string | null>(null);
  const [completedActions, setCompletedActions] = useState<CycleStep[]>([]);
  const [plannedActions, setPlannedActions] = useState<string[]>([]);

  useEffect(() => {
    if (!user?.uid || !cycleId) {
      setCurrentAction(null);
      setCurrentLabel(null);
      setCompletedActions([]);
      setPlannedActions([]);
      return;
    }
    const ref = doc(firestoreDb, "users", user.uid, "agent_cycles", cycleId);
    const unsub = onSnapshot(
      ref,
      (snap) => {
        if (!snap.exists()) return;
        const d = snap.data();
        setCurrentAction(d?.currentAction ?? null);
        setCurrentLabel(d?.currentLabel ?? null);
        setCompletedActions(d?.completedActions ?? []);
        setPlannedActions(d?.plannedActions ?? []);
      },
      (err) => console.warn("Cycle progress listener:", err)
    );
    return () => unsub();
  }, [user?.uid, cycleId]);

  return { currentAction, currentLabel, completedActions, plannedActions };
}

// ── Lifecycle mutations ─────────────────────────────────────────────────

export function useAgentLifecycle() {
  const { toast } = useToast();
  const queryClient = useQueryClient();

  const deployMutation = useMutation({
    mutationFn: deployAgent,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agent"] });
      toast({ title: "Agent deployed", description: "Your networking agent is now active." });
    },
    onError: (e: Error) =>
      toast({ title: "Deploy failed", description: e.message, variant: "destructive" }),
  });

  const pauseMutation = useMutation({
    mutationFn: pauseAgent,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agent"] });
      toast({ title: "Agent paused" });
    },
  });

  const stopMutation = useMutation({
    mutationFn: stopAgent,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agent"] });
      toast({ title: "Agent stopped", description: "Weekly queue has been resumed." });
    },
  });

  const approveMutation = useMutation({
    mutationFn: (id: string) => approveAction(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["agent", "approvals"] }),
  });

  const rejectMutation = useMutation({
    mutationFn: (id: string) => rejectAction(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["agent", "approvals"] }),
  });

  const updateConfigMutation = useMutation({
    mutationFn: (updates: Partial<AgentConfig>) => updateAgentConfig(updates),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["agent", "config"] });
      toast({ title: "Settings saved" });
    },
    onError: (e: Error) =>
      toast({ title: "Save failed", description: e.message, variant: "destructive" }),
  });

  return {
    deploy: deployMutation,
    pause: pauseMutation,
    stop: stopMutation,
    approve: approveMutation,
    reject: rejectMutation,
    updateConfig: updateConfigMutation,
  };
}
