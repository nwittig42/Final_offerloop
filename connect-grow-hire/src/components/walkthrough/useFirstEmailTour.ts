import { useCallback, useRef } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  loadWalkthrough,
  resumeWalkthrough,
  saveWalkthrough,
  shouldAutoOpen,
  type TourStep,
  type WalkthroughState,
} from "@/services/walkthrough";

// Scout's first-email tour state, shared by Find and Contacts through the
// React Query cache (["walkthrough", uid]) so a navigation between them
// doesn't reload it. Every write re-reads the latest saved state and builds on
// that, so another surface's progress is never rolled back.

export function useFirstEmailTour(
  uid: string | undefined,
  deps: { load?: typeof loadWalkthrough; save?: typeof saveWalkthrough } = {},
) {
  const loadRef = useRef(deps.load ?? loadWalkthrough);
  loadRef.current = deps.load ?? loadWalkthrough;
  const saveRef = useRef(deps.save ?? saveWalkthrough);
  saveRef.current = deps.save ?? saveWalkthrough;

  const client = useQueryClient();
  const key = ["walkthrough", uid];
  const query = useQuery<WalkthroughState | null>({
    queryKey: key,
    queryFn: () => loadRef.current(uid as string).catch(() => null),
    enabled: !!uid,
    staleTime: Infinity,
  });
  const state = query.data ?? null;
  const loaded = !!uid && query.isSuccess;

  const write = useCallback(
    async (change: (base: WalkthroughState, now: string) => WalkthroughState) => {
      if (!uid) return;
      const queryKey = ["walkthrough", uid];
      const latest = await loadRef.current(uid).catch(() => null);
      const base = latest ?? client.getQueryData<WalkthroughState | null>(queryKey) ?? null;
      if (!base) return;
      const next = change(base, new Date().toISOString());
      client.setQueryData(queryKey, next);
      await saveRef.current(uid, next).catch(() => {});
    },
    [uid, client],
  );

  const active = loaded && shouldAutoOpen(state);
  const step: TourStep | null = active ? (state?.tourStep ?? "pick") : null;

  const goTo = useCallback(
    (to: TourStep, patch: Partial<WalkthroughState> = {}) =>
      write((base) => ({ ...base, ...patch, tourStep: to, status: "in_progress" })),
    [write],
  );
  const complete = useCallback(() => write((base) => ({ ...base, status: "completed" })), [write]);
  const skip = useCallback(() => write((base, now) => ({ ...base, skippedAt: base.skippedAt ?? now })), [write]);
  const close = useCallback(() => write((base, now) => ({ ...base, closedAt: base.closedAt ?? now })), [write]);
  const resume = useCallback(() => write((base) => resumeWalkthrough(base)), [write]);

  return { loaded, state, active, step, goTo, complete, skip, close, resume };
}
