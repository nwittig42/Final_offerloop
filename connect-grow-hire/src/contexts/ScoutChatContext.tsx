/**
 * ScoutChatContext - THE single Scout conversation.
 *
 * One useScoutChat() instance lives here so every surface that renders the
 * chat (the Ask Scout side panel, the Getting Started page) shows the same
 * live thread. Everything that must fire exactly once per event regardless
 * of how many views are mounted also lives here: navigate auto-execution,
 * the post-search celebration listener, and the pending-message auto-send.
 */
import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { useScout } from '@/contexts/ScoutContext';
import {
  useScoutChat,
  type UseScoutChatReturn,
  type ScoutNavigate,
  type ScoutMode,
  type ScoutCta,
  type ScoutPlanStep,
  type ScoutActiveStrategy,
} from '@/hooks/useScoutChat';
import { toast } from '@/hooks/use-toast';
import {
  writeScoutPrefill,
  SCOUT_PREFILL_EVENT,
  SCOUT_SEARCH_COMPLETED_EVENT,
  isSameScoutPage,
  scoutPageIdentity,
  type ScoutSearchCompletedDetail,
} from '@/lib/scoutBridge';

// ---------------------------------------------------------------------------
// Three-rule decision for a navigate tool call (moved from ScoutSidePanel).
// Mode is the primary signal (Change 7 - the Haiku intent classifier); the
// legacy 0.9+imperative rule remains a fallback so local dev still works
// when the classifier is unavailable.
// ---------------------------------------------------------------------------
export type NavAction = 'in-place' | 'skip-approve' | 'approve-card';

export function decideNavAction(nav: ScoutNavigate, mode?: ScoutMode | null): NavAction {
  if (nav.already_on_page) return 'in-place';
  // DO mode means the user gave a directive; executing it IS the product,
  // credit-spending searches included (product call 2026-07-07: no approve
  // card between an explicit ask and the action). The undo toast and the
  // destination page's own controls are the escape hatches.
  if (mode === 'do') return 'skip-approve';
  // Fallback: when the classifier was unavailable the mode pill defaults to
  // 'chat' regardless of navigate intent; honor the model's own
  // user_was_imperative + confidence so a clear command still skips the card.
  if (!mode && nav.user_was_imperative && nav.confidence >= 0.9) {
    return 'skip-approve';
  }
  return 'approve-card';
}

function summarizePrefill(prefill: Record<string, string>): string {
  const vals = Object.values(prefill || {}).filter(Boolean);
  return vals.join(', ');
}

export interface ScoutChatSharedValue extends UseScoutChatReturn {
  /** pathname (+ ?tab=) sent to Scout as page context; also keys chips. */
  scoutCurrentPage: string;
  /** Most recent active_strategy payload in the thread, or null. */
  activeStrategy: ScoutActiveStrategy | null;
  /** Navigate messages already acted on (approve card renders collapsed). */
  resolvedIds: Set<string>;
  handleApprove: (id: string, nav: ScoutNavigate, prefill: Record<string, string>) => void;
  handleCtaAction: (cta: ScoutCta) => void;
  handlePlanStep: (step: ScoutPlanStep) => void;
  /** Click on an inline data-scout-link chip inside assistant prose. */
  handleInlineLink: (href: string) => void;
}

const ScoutChatSharedContext = createContext<ScoutChatSharedValue | undefined>(undefined);

export function useScoutChatShared(): ScoutChatSharedValue {
  const ctx = useContext(ScoutChatSharedContext);
  if (!ctx) throw new Error('useScoutChatShared must be used within a ScoutChatProvider');
  return ctx;
}

export function ScoutChatProvider({ children }: { children: React.ReactNode }) {
  const navigate = useNavigate();
  const location = useLocation();
  const { isPanelOpen, closePanel, pendingMessage, clearPendingMessage } = useScout();

  // Page context sent to Scout. The bare pathname collapses the Find tabs
  // (people / companies / hiring-managers) into one page, so carry the tab
  // param when present. Other query params are deliberately dropped: they are
  // noise for the model and would churn the backend's context cache.
  const tabParam = new URLSearchParams(location.search).get('tab');
  const scoutCurrentPage = tabParam
    ? `${location.pathname}?tab=${tabParam}`
    : location.pathname;

  const chat = useScoutChat(scoutCurrentPage);
  const { messages, sendMessage, appendSyntheticAssistant } = chat;

  // Navigate messages that have been acted on (approved, skipped, or
  // populated in place), so the auto-execute effect does not re-fire and the
  // approve card renders collapsed. Lives here (not in a view) so a dual
  // mount can never double-fire the auto-execute effect.
  const [resolvedIds, setResolvedIds] = useState<Set<string>>(new Set());

  // The most-recent message carrying an active_strategy payload becomes the
  // source for the header card. Looking at messages in reverse so a fresh
  // briefing supersedes an older one without tracking strategy separately.
  const activeStrategy: ScoutActiveStrategy | null = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      const s = messages[i]?.activeStrategy;
      if (s) return s;
    }
    return null;
  })();

  /** Carry a navigate to its destination: write the bridge, then either
   *  navigate or (if in place) tell the current page to re-read the bridge.
   *  Auto-fired actions (skip-approve, in-place) drop a 5s undo toast - the
   *  trust hook that lets us be aggressive about skip-approve. Approve-card
   *  actions skip the toast since they were already an explicit confirmation. */
  const runNavigate = (nav: ScoutNavigate, prefill: Record<string, string>, action: NavAction) => {
    const previousPath = location.pathname + location.search;
    writeScoutPrefill(nav.route, prefill, { auto_submit: !!nav.auto_submit });
    const summary = summarizePrefill(prefill);
    const wasInPlace = action === 'in-place';
    if (wasInPlace) {
      window.dispatchEvent(new CustomEvent(SCOUT_PREFILL_EVENT));
    } else {
      navigate(nav.route);
    }
    if (action === 'approve-card') return;
    const title = nav.auto_submit
      ? `Scout is running your search`
      : wasInPlace
      ? `Scout filled in ${nav.route}`
      : `Scout took you to ${nav.route}`;
    toast({
      title,
      description: summary || undefined,
      duration: 5000,
      action: (
        <button
          type="button"
          onClick={() => {
            // Clear the prefill envelope first so a stray re-mount of the
            // destination page does not pick it up after we leave.
            try {
              sessionStorage.removeItem('scout_prefill');
            } catch {
              /* sessionStorage may be disabled */
            }
            if (!wasInPlace && previousPath) {
              navigate(previousPath);
            }
          }}
          className="text-xs font-medium text-[var(--brand-blue)] hover:underline"
        >
          Undo
        </button>
      ) as React.ReactElement,
    });
  };

  // Auto-run the navigates that need no card: in-place populate and
  // skip-approve. Fires once per navigate message; approve-card navigates
  // wait for the user to click Approve on the card.
  useEffect(() => {
    const last = messages[messages.length - 1];
    if (!last || last.role !== 'assistant' || last.isStreaming) return;
    if (last.tool !== 'navigate' || !last.navigate) return;
    if (resolvedIds.has(last.id)) return;
    const action = decideNavAction(last.navigate, last.mode);
    if (action === 'approve-card') return;
    setResolvedIds((prev) => new Set(prev).add(last.id));
    runNavigate(last.navigate, last.navigate.prefill, action);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages, resolvedIds]);

  /** Approve-card click: the user OK'd (and maybe edited) the prefill. */
  const handleApprove = (id: string, nav: ScoutNavigate, prefill: Record<string, string>) => {
    setResolvedIds((prev) => new Set(prev).add(id));
    runNavigate(nav, prefill, 'approve-card');
  };

  /** CTA chip click (Change 6): single-chip bridge to a workflow. */
  const handleCtaAction = useCallback((cta: ScoutCta) => {
    // Chat-action chips stay in the conversation: clicking one sends the
    // chip's message as the user's next turn (e.g. "Find people at
    // Databricks" runs the find in chat) instead of navigating away.
    if (cta.chat_message) {
      sendMessage(cta.chat_message);
      return;
    }
    writeScoutPrefill(cta.route, cta.prefill || {});
    // A route carrying query params beyond `tab` is a deep link the
    // destination page reads from the URL; those must always navigate, even
    // when the user is already on the page, or the param never lands.
    const query = new URLSearchParams(cta.route.split('?')[1] || '');
    query.delete('tab');
    const hasDeepLinkParams = [...query.keys()].length > 0;
    if (!hasDeepLinkParams && isSameScoutPage(location.pathname + location.search, cta.route)) {
      window.dispatchEvent(new CustomEvent(SCOUT_PREFILL_EVENT));
    } else {
      navigate(cta.route);
    }
    closePanel();
  }, [location.pathname, location.search, navigate, closePanel, sendMessage]);

  /** Plan-checklist "Do this" click (Change 5): take a single step to its
   *  page. The step's route is the same shape as a navigate route. */
  const handlePlanStep = useCallback((step: ScoutPlanStep) => {
    if (!step.route) return;
    writeScoutPrefill(step.route, {});
    if (isSameScoutPage(location.pathname + location.search, step.route)) {
      window.dispatchEvent(new CustomEvent(SCOUT_PREFILL_EVENT));
    } else {
      navigate(step.route);
    }
    closePanel();
  }, [location.pathname, location.search, navigate, closePanel]);

  /** Inline data-scout-link chip inside assistant prose: route via
   *  react-router instead of a full page reload. */
  const handleInlineLink = useCallback((href: string) => {
    closePanel();
    navigate(href);
  }, [closePanel, navigate]);

  // Post-result celebration: when a Scout-driven workflow (auto_submit
  // contact / firm search) lands its results on the destination page, the
  // page dispatches SCOUT_SEARCH_COMPLETED_EVENT. We listen here and post a
  // synthetic assistant message into the chat with a CTA chip back to the
  // results page. Singleton: lives here so a dual mount (panel open over the
  // Getting Started page) can't append the message twice.
  useEffect(() => {
    const onCompleted = (e: Event) => {
      const detail = (e as CustomEvent<ScoutSearchCompletedDetail>).detail;
      if (!detail) return;
      const count = typeof detail.count === 'number' ? detail.count : 0;
      // Results land in Contacts. Identity-based: the firm search was
      // /find?tab=companies; every other source is people.
      const sourceId = scoutPageIdentity(detail.route || '');
      const wasContacts = !(sourceId.path === '/find' && sourceId.tab === 'companies');
      const resultsRoute = detail.results_route
        || '/contacts';
      const subject = wasContacts ? 'contact' : 'firm';
      const subjectPlural = wasContacts ? 'contacts' : 'firms';
      const chipLabel = wasContacts ? 'Open your network' : 'Open your companies';
      // Cite who was found when the page told us, not just a count.
      const names = Array.isArray(detail.names) ? detail.names.filter(Boolean) : [];
      const namesLine = names.length
        ? ` ${names.slice(0, 3).join(', ')}${count > 3 ? ` and ${count - 3} more` : ''}.`
        : '';
      const content = count === 0
        ? `Search ran, no ${subjectPlural} this time. Want me to widen it?`
        : count === 1
        ? `Found 1 ${subject}:${namesLine || ' pick who to reach out to or open your full list.'}`
        : `Found ${count} ${subjectPlural}:${namesLine} Pick who to reach out to or open your full list.`;
      appendSyntheticAssistant(content, {
        mode: 'do',
        cta: count === 0
          ? null
          : {
              label: chipLabel,
              route: resultsRoute,
              prefill: {},
              credit_spending: false,
              credit_cost: null,
            },
      });
    };
    window.addEventListener(SCOUT_SEARCH_COMPLETED_EVENT, onCompleted);
    return () => window.removeEventListener(SCOUT_SEARCH_COMPLETED_EVENT, onCompleted);
  }, [appendSyntheticAssistant]);

  // Auto-send a pending message (the briefing's "Ask Scout" prompt chips,
  // openPanelWithMessage). The message is captured and sent synchronously:
  // clearPendingMessage sets pendingMessage to null, which re-runs this
  // effect; the re-run hits the early return, so the send happens once.
  useEffect(() => {
    if (!isPanelOpen || !pendingMessage) return;
    const msg = pendingMessage;
    clearPendingMessage();
    void sendMessage(msg);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPanelOpen, pendingMessage]);

  return (
    <ScoutChatSharedContext.Provider
      value={{
        ...chat,
        scoutCurrentPage,
        activeStrategy,
        resolvedIds,
        handleApprove,
        handleCtaAction,
        handlePlanStep,
        handleInlineLink,
      }}
    >
      {children}
    </ScoutChatSharedContext.Provider>
  );
}
