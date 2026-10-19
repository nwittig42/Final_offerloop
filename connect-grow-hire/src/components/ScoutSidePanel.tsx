/**
 * ScoutSidePanel - slide-out panel for the Scout assistant.
 *
 * Renders two modes:
 *  - Chat: Scout answers each turn with one tool (navigate / answer / clarify).
 *    A navigate is run through the three-rule decision (skip-approve, approve
 *    card, or in-place populate) and carried to the destination page via the
 *    route-keyed scoutBridge.
 *  - Search help: failed-search recovery, unchanged, on its own legacy channel.
 *
 * Mounted once in App.tsx as a sibling of the route switch, so it persists
 * across navigation. The conversation lives in useScoutChat (localStorage +
 * Firestore backed); the open/closed flag lives in ScoutContext.
 */
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { X, Loader2, Trash2, MessageSquarePlus, History, Lock } from 'lucide-react';
import { useScout, SearchHelpResponse } from '@/contexts/ScoutContext';
import { useScoutChatShared } from '@/contexts/ScoutChatContext';
import { ScoutChatThread } from '@/components/scout/ScoutChatThread';
import { useFirebaseAuth } from '@/contexts/FirebaseAuthContext';
// Tour demo orchestration — local addition, not in loops-setup-v2.
import { useTour } from '@/contexts/TourContext';
import { ScoutTriedFailedHint } from '@/components/ScoutChatExtras';
import { isSameScoutPage } from '@/lib/scoutBridge';
import ScoutYetiHead from '@/assets/scouts/scout-yeti-head.png';
import { BACKEND_URL } from '@/services/api';
import {
  listScoutChats,
  formatRelativeTime,
  type ScoutChatSummary,
} from '@/services/scoutChats';

// Legacy sessionStorage key. Still used only by the failed-search recovery
// flow below; the Scout chat navigate path uses scoutBridge instead.
const AUTO_POPULATE_KEY = 'scout_auto_populate';

// LocalStorage keys for the tried-and-failed hint (Change 3). We never want
// to surface the same dismissed prompt twice in the same session.
const TRIED_HINT_DISMISSED_KEY = 'scout_tried_hint_dismissed';
const TRIED_PROMPTS_KEY = 'ofl_tried_prompts';

// Picks the most recent zero-result prompt from localStorage that has not
// already been dismissed this session. Used by the proactive hint at the
// top of the panel. Returns null when nothing applies.
function pickTriedFailedHint(): string | null {
  try {
    const dismissed = new Set(
      JSON.parse(sessionStorage.getItem(TRIED_HINT_DISMISSED_KEY) || '[]') as string[],
    );
    const tried = JSON.parse(localStorage.getItem(TRIED_PROMPTS_KEY) || '{}') as Record<string, number>;
    const cutoff = Date.now() - 24 * 60 * 60 * 1000;
    const entries = Object.entries(tried)
      .filter(([p, ts]) => ts >= cutoff && !dismissed.has(p))
      .sort((a, b) => b[1] - a[1]);
    return entries.length ? entries[0][0] : null;
  } catch {
    return null;
  }
}

function markTriedFailedDismissed(prompt: string): void {
  try {
    const dismissed = new Set(
      JSON.parse(sessionStorage.getItem(TRIED_HINT_DISMISSED_KEY) || '[]') as string[],
    );
    dismissed.add(prompt);
    sessionStorage.setItem(TRIED_HINT_DISMISSED_KEY, JSON.stringify(Array.from(dismissed)));
  } catch {
    /* sessionStorage may be disabled */
  }
}

export function ScoutSidePanel() {
  const navigate = useNavigate();
  const location = useLocation();
  const { user } = useFirebaseAuth();
  const {
    isPanelOpen,
    openPanel,
    closePanel,
    searchHelpContext,
    searchHelpResponse,
    setSearchHelpResponse,
    clearSearchHelp,
  } = useScout();
  // Tour demo orchestration — local addition, not in loops-setup-v2.
  // `demoSurface === 'scout'` means the onboarding tour reached the Ask-Scout
  // step; the effect below seeds a synthetic demo thread while it's active.
  const { demoSurface } = useTour();
  const scoutDemoActive = demoSurface === 'scout';
  const panelRef = useRef<HTMLDivElement>(null);
  const [isLoadingSearchHelp, setIsLoadingSearchHelp] = useState(false);

  // The conversation itself lives in ScoutChatProvider (shared with the
  // Getting Started page); the panel only needs the slices its chrome uses.
  const {
    messages,
    sendMessage,
    clearChat,
    chatId,
    startNewChat,
    loadChat,
    isLoadingChat,
    appendSyntheticAssistant,
    appendSyntheticUser,
  } = useScoutChatShared();

  // The strategist briefing is OPT-IN only (the BriefingButton in the empty
  // state, rendered by ScoutChatThread). A chat never starts with
  // unrequested output: the cold open is the greeting plus suggestion chips.

  // -------------------------------------------------------------------------
  // Sidebar (Phase 5 Stage 3): persisted chat history
  // -------------------------------------------------------------------------
  // Free tier: shows just the current chat row + upgrade affordance, no past
  // chat list (the backend caps list_chats to one for Free). Pro/Elite: shows
  // up to 20 recent chats with a relative timestamp and a strategy dot.
  const userTier = (user?.tier as 'free' | 'pro' | 'elite' | undefined) ?? 'free';
  const isPaidTier = userTier === 'pro' || userTier === 'elite';
  const [chats, setChats] = useState<ScoutChatSummary[]>([]);
  const [chatsLoading, setChatsLoading] = useState(false);
  const [chatsError, setChatsError] = useState(false);
  const [activeRowId, setActiveRowId] = useState<string | null>(null);

  const refreshChats = useCallback(async () => {
    if (!user?.uid) {
      setChats([]);
      return;
    }
    setChatsLoading(true);
    setChatsError(false);
    try {
      const list = await listScoutChats(20);
      setChats(list);
    } catch (e) {
      console.error('[Scout] sidebar list failed:', e);
      setChatsError(true);
    } finally {
      setChatsLoading(false);
    }
  }, [user?.uid]);

  // Refresh the sidebar each time the panel opens, and again whenever the
  // active chat_id changes (so a fresh thread shows up immediately after the
  // first turn lands and the title generation finishes).
  useEffect(() => {
    if (!isPanelOpen) return;
    // Tour demo orchestration — local addition, not in loops-setup-v2.
    // Suppress the real chat-history refetch while the seeded demo is active
    // so the user's actual chats don't bleed into the tour's demo thread.
    if (scoutDemoActive) return;
    void refreshChats();
  }, [isPanelOpen, refreshChats, scoutDemoActive]);

  useEffect(() => {
    if (!isPanelOpen) return;
    // Tour demo orchestration — local addition, not in loops-setup-v2.
    if (scoutDemoActive) return;
    // Small debounce: the backend writes the title asynchronously after the
    // turn responds, so wait briefly before refetching so we pick the new
    // title up on the same render that surfaced the chat_id.
    const t = setTimeout(() => {
      void refreshChats();
    }, 1500);
    return () => clearTimeout(t);
  }, [chatId, isPanelOpen, refreshChats, scoutDemoActive]);

  const handleSidebarChatClick = useCallback(
    async (id: string) => {
      if (id === chatId) return;
      setActiveRowId(id);
      try {
        await loadChat(id);
      } finally {
        setActiveRowId(null);
      }
    },
    [chatId, loadChat],
  );

  const handleNewChatClick = useCallback(() => {
    startNewChat();
  }, [startNewChat]);

  // ── Tour Scout demo orchestration ──────────────────────────────────────
  // Local addition, NOT in loops-setup-v2 — re-ported on top of the branch
  // overwrite (depends on appendSyntheticUser, also re-added to useScoutChat).
  // When the tour reaches the Ask-Scout step, open the panel programmatically
  // and seed a two-turn strategist conversation about reaching Mark Cuban.
  // The seed uses the appendSyntheticUser / appendSyntheticAssistant helpers
  // (purely local, never persisted), and the sidebar history refetch is gated
  // above so the user's real chat list won't pop in alongside the demo.
  // Cleanup closes the panel and clears the seeded thread.
  useEffect(() => {
    if (!scoutDemoActive) return;

    // Open the panel if it isn't already, and start from a clean thread so
    // the demo isn't mixed with a real in-progress chat.
    openPanel();
    clearChat();
    appendSyntheticUser('I want to talk to Mark Cuban');
    appendSyntheticAssistant(
      "Smart goal. Mark's reachable but the angle has to be sharp. Here's how I'd run this:\n\n" +
      "1. Map the path. I'll surface mutual LinkedIn connections with bias toward Shark Tank investors, Cost Plus Drugs operators, and Mavericks-adjacent execs.\n\n" +
      "2. Pick the channel. Skip the public Mavs email (zero signal). Target a referral through someone he's engaged with in the last 30 days.\n\n" +
      "3. Frame the ask. One concrete idea aligned with what he's publicly focused on right now, kept under five sentences.\n\n" +
      "Want me to start mapping connections?"
    );

    return () => {
      // Wipe the seeded thread and close the panel. clearChat is local-only
      // (setMessages([]) + setChatId(null)), so no backend write fires.
      clearChat();
      closePanel();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scoutDemoActive]);

  // Navigate execution, approve/CTA/plan handlers, and the celebration
  // listener all moved to ScoutChatProvider (shared with the Getting Started
  // page, and singleton so a dual mount can't double-fire them).

  // -------------------------------------------------------------------------
  // Tried-and-failed proactive hint (Change 3)
  // -------------------------------------------------------------------------
  const [triedHint, setTriedHint] = useState<string | null>(null);
  useEffect(() => {
    if (!isPanelOpen) {
      setTriedHint(null);
      return;
    }
    // Only show the hint on a fresh empty chat where the user hasn't typed
    // anything yet. After they engage, the hint stops being relevant.
    if (messages.length === 0) {
      setTriedHint(pickTriedFailedHint());
    }
  }, [isPanelOpen, messages.length]);

  const handleHintWiden = useCallback((prompt: string) => {
    setTriedHint(null);
    markTriedFailedDismissed(prompt);
    void sendMessage(`Last time I tried "${prompt}" and got nothing. Widen it for me.`);
  }, [sendMessage]);

  const handleHintDismiss = useCallback(() => {
    if (triedHint) markTriedFailedDismissed(triedHint);
    setTriedHint(null);
  }, [triedHint]);

  // -------------------------------------------------------------------------
  // Search help (failed-search recovery) - unchanged, legacy channel
  // -------------------------------------------------------------------------
  useEffect(() => {
    if (isPanelOpen && searchHelpContext && !searchHelpResponse) {
      fetchSearchHelp();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPanelOpen, searchHelpContext, searchHelpResponse]);

  useEffect(() => {
    if (!isPanelOpen) {
      const timer = setTimeout(() => {
        clearSearchHelp();
      }, 300);
      return () => clearTimeout(timer);
    }
  }, [isPanelOpen, clearSearchHelp]);

  const fetchSearchHelp = async () => {
    if (!searchHelpContext) return;
    setIsLoadingSearchHelp(true);
    try {
      const { auth } = await import('@/lib/firebase');
      const firebaseUser = auth.currentUser;
      const token = firebaseUser ? await firebaseUser.getIdToken() : null;

      const response = await fetch(`${BACKEND_URL}/api/scout-assistant/search-help`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({
          search_type: searchHelpContext.searchType,
          failed_search_params: searchHelpContext.failedSearchParams,
          error_type: searchHelpContext.errorType,
          user_info: { name: user?.name || 'there' },
        }),
      });

      if (!response.ok) {
        throw new Error(`API returned ${response.status}`);
      }

      const data: SearchHelpResponse = await response.json();
      setSearchHelpResponse(data);
    } catch (error) {
      console.error('[Scout] Search help error:', error);
      setSearchHelpResponse({
        message:
          searchHelpContext.searchType === 'contact'
            ? "I couldn't find contacts matching your search. Try different job titles or a broader location."
            : "I couldn't find firms matching your search. Try different industry terms or a broader location.",
        suggestions: [],
        auto_populate: searchHelpContext.failedSearchParams,
        search_type: searchHelpContext.searchType,
        action: 'retry_search',
      });
    } finally {
      setIsLoadingSearchHelp(false);
    }
  };

  const handleContinue = () => {
    if (!searchHelpResponse) return;
    sessionStorage.setItem(
      AUTO_POPULATE_KEY,
      JSON.stringify({
        search_type: searchHelpResponse.search_type,
        auto_populate: searchHelpResponse.auto_populate,
      }),
    );
    const targetRoute = '/find';
    closePanel();
    if (location.pathname !== targetRoute) {
      navigate(targetRoute);
    } else {
      window.dispatchEvent(new CustomEvent('scout-auto-populate'));
    }
  };

  // -------------------------------------------------------------------------
  // Panel chrome
  // -------------------------------------------------------------------------
  useEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isPanelOpen) closePanel();
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [isPanelOpen, closePanel]);

  useEffect(() => {
    if (isPanelOpen) {
      const originalOverflow = document.body.style.overflow;
      document.body.style.overflow = 'hidden';
      return () => {
        document.body.style.overflow = originalOverflow;
      };
    }
  }, [isPanelOpen]);

  // Input focus lives in ScoutChatThread (it autofocuses on mount, and the
  // panel mounts a fresh thread on every open). The pending-message auto-send
  // moved to ScoutChatProvider.

  const isSearchHelpMode = !!searchHelpContext;

  if (!isPanelOpen) return null;

  return (
    <>
      {/* Overlay - closes panel on click */}
      <div
        className="fixed inset-0 z-40 bg-black/30 transition-opacity duration-200"
        onClick={closePanel}
        aria-hidden="true"
      />

      {/* Panel. Chat mode is wider to accommodate the persisted-chat sidebar
          AND briefing prose with deep-link URLs that don't wrap mid-token.
          Search-help mode keeps the legacy width. */}
      <div
        ref={panelRef}
        className={
          'fixed right-0 top-0 z-50 h-full w-full bg-white shadow-xl flex flex-col transform transition-transform duration-300 ease-out rounded-l-2xl ' +
          (isSearchHelpMode
            ? 'sm:w-[420px]'
            : 'sm:w-[640px] md:w-[760px] lg:w-[860px]')
        }
        style={{ animation: 'slideIn 0.3s ease-out forwards' }}
        onClick={(e) => e.stopPropagation()}
      >
        <style>{`
          @keyframes slideIn {
            from { transform: translateX(100%); }
            to { transform: translateX(0); }
          }
        `}</style>

        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 flex-shrink-0">
          <h1 className="text-base font-medium text-gray-900">Ask Scout</h1>
          <div className="flex items-center gap-1">
            {!isSearchHelpMode && messages.length > 0 && (
              <button
                onClick={clearChat}
                className="p-2 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
                aria-label="Clear chat"
              >
                <Trash2 className="h-4 w-4" />
              </button>
            )}
            <button
              onClick={closePanel}
              className="p-2 text-gray-400 hover:text-gray-600 hover:bg-gray-100 rounded-lg transition-colors"
              aria-label="Close"
            >
              <X className="h-5 w-5" />
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 flex flex-col overflow-hidden">
          {/* Search help mode */}
          {isSearchHelpMode && (
            <div className="flex-1 overflow-y-auto px-5 py-4">
              {isLoadingSearchHelp ? (
                <div className="flex flex-col items-center justify-center min-h-[300px]">
                  <div className="w-12 h-12 rounded-full bg-[#FFF7EA] flex items-center justify-center mb-4 overflow-hidden">
                    <img src={ScoutYetiHead} alt="" className="w-full h-full object-contain" />
                  </div>
                  <div className="flex items-center gap-2 text-gray-500">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    <span className="text-sm">Analyzing your search...</span>
                  </div>
                </div>
              ) : searchHelpResponse ? (
                <div className="space-y-4">
                  <div className="flex gap-3">
                    <div className="w-7 h-7 rounded-full bg-[#FFF7EA] flex-shrink-0 flex items-center justify-center overflow-hidden">
                      <img src={ScoutYetiHead} alt="" className="w-full h-full object-contain" />
                    </div>
                    <div className="flex-1 max-w-[85%]">
                      <div className="bg-gray-100 rounded-3xl rounded-bl-md px-4 py-2.5">
                        <p className="text-sm text-gray-900 leading-relaxed">{searchHelpResponse.message}</p>
                      </div>

                      {searchHelpResponse.refined_prompts && searchHelpResponse.refined_prompts.length > 0 ? (
                        <div className="mt-3 space-y-2">
                          {searchHelpResponse.refined_prompts.map((rp, idx) => (
                            <button
                              key={idx}
                              type="button"
                              onClick={() => {
                                try {
                                  sessionStorage.setItem(
                                    AUTO_POPULATE_KEY,
                                    JSON.stringify({
                                      search_type: 'contact',
                                      auto_populate: { prompt: rp.prompt, autoSubmit: true },
                                    }),
                                  );
                                } catch {
                                  /* sessionStorage may be disabled - non-fatal */
                                }
                                closePanel();
                                if (!isSameScoutPage(location.pathname + location.search, '/find')) {
                                  // Also switches the Find tab back to People
                                  // when the user is on companies/hiring-managers.
                                  navigate('/find');
                                } else {
                                  window.dispatchEvent(new CustomEvent('scout-auto-populate'));
                                }
                              }}
                              className="w-full text-left flex flex-col gap-1.5 px-3.5 py-3 bg-white rounded-xl border border-[#EEF2F8] hover:border-[#3B82F6] hover:bg-[#FAFBFF] transition-colors"
                            >
                              <div className="flex items-start gap-2">
                                <span className="w-5 h-5 rounded-full bg-[rgba(59,130,246,0.10)] text-[#3B82F6] text-xs font-medium flex items-center justify-center flex-shrink-0 mt-0.5">
                                  {idx + 1}
                                </span>
                                <span className="text-sm text-gray-900 font-medium leading-snug">{rp.prompt}</span>
                              </div>
                              {rp.rationale && (
                                <span className="text-xs text-gray-500 leading-snug pl-7">{rp.rationale}</span>
                              )}
                            </button>
                          ))}
                        </div>
                      ) : (
                        searchHelpResponse.suggestions.length > 0 && (
                          <div className="mt-3 space-y-2">
                            {searchHelpResponse.suggestions.map((suggestion, idx) => (
                              <div key={idx} className="flex items-center gap-2 px-3 py-2 bg-[#FAFBFF] rounded-xl border border-[#EEF2F8]">
                                <span className="w-5 h-5 rounded-full bg-[rgba(59,130,246,0.10)] text-[#3B82F6] text-xs font-medium flex items-center justify-center">
                                  {idx + 1}
                                </span>
                                <span className="text-sm text-gray-800">{suggestion}</span>
                              </div>
                            ))}
                          </div>
                        )
                      )}

                      {(!searchHelpResponse.refined_prompts || searchHelpResponse.refined_prompts.length === 0) && (
                        <div className="mt-4">
                          <button
                            onClick={handleContinue}
                            className="px-4 py-2 rounded-xl bg-[#0F172A] text-white text-sm font-medium hover:bg-[#1E293B] transition-colors"
                          >
                            Continue
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              ) : null}
            </div>
          )}

          {/* Chat mode */}
          {!isSearchHelpMode && (
            <div className="flex-1 flex overflow-hidden">
              {/* Sidebar (chat history). Always rendered, even on Free, so the
                  current chat row stays visible and the upgrade affordance has
                  somewhere to live. */}
              <aside className="w-44 flex-shrink-0 border-r border-gray-100 bg-white flex flex-col">
                <div className="px-3 pt-3 pb-2 flex items-center justify-between">
                  <div className="flex items-center gap-1.5 text-xs font-medium text-gray-500 uppercase tracking-wide">
                    <History className="h-3.5 w-3.5" />
                    <span>Chats</span>
                  </div>
                  <button
                    type="button"
                    onClick={handleNewChatClick}
                    className="p-1 text-gray-400 hover:text-[#3B82F6] hover:bg-[#FAFBFF] rounded-md transition-colors"
                    aria-label="Start a new chat"
                    title="New chat"
                  >
                    <MessageSquarePlus className="h-4 w-4" />
                  </button>
                </div>

                <div className="flex-1 overflow-y-auto px-2 pb-2">
                  {chatsLoading && chats.length === 0 ? (
                    <div className="px-2 py-3 text-xs text-gray-400">Loading...</div>
                  ) : chatsError ? (
                    <div className="px-2 py-3 text-xs text-gray-500">
                      Could not load history.{' '}
                      <button
                        type="button"
                        onClick={() => void refreshChats()}
                        className="text-[#3B82F6] hover:underline"
                      >
                        Retry
                      </button>
                    </div>
                  ) : chats.length === 0 ? (
                    <div className="px-2 py-3 text-xs text-gray-400">
                      No chats yet.
                    </div>
                  ) : (
                    <ul className="space-y-0.5">
                      {chats.map((row) => {
                        const isActive = row.chat_id === chatId;
                        const isLoadingRow = activeRowId === row.chat_id && isLoadingChat;
                        return (
                          <li key={row.chat_id}>
                            <button
                              type="button"
                              onClick={() => void handleSidebarChatClick(row.chat_id)}
                              disabled={isLoadingRow}
                              className={
                                'w-full text-left px-2.5 py-2 rounded-md transition-colors group ' +
                                (isActive
                                  ? 'bg-[#FAFBFF] border border-[#E0EAFF]'
                                  : 'border border-transparent hover:bg-gray-50')
                              }
                              title={row.title}
                            >
                              <div className="flex items-start gap-1.5">
                                {row.active_strategy_id ? (
                                  <span
                                    className="mt-1.5 h-1.5 w-1.5 rounded-full bg-[#3B82F6] flex-shrink-0"
                                    aria-label="Chat had an active strategy"
                                  />
                                ) : (
                                  <span className="mt-1.5 h-1.5 w-1.5 flex-shrink-0" />
                                )}
                                <div className="flex-1 min-w-0">
                                  <div className="text-xs font-medium text-gray-900 truncate">
                                    {row.title || 'New chat'}
                                  </div>
                                  <div className="text-[11px] text-gray-400 flex items-center gap-1">
                                    {isLoadingRow ? (
                                      <Loader2 className="h-2.5 w-2.5 animate-spin" />
                                    ) : (
                                      <span>{formatRelativeTime(row.last_active_at)}</span>
                                    )}
                                  </div>
                                </div>
                              </div>
                            </button>
                          </li>
                        );
                      })}
                    </ul>
                  )}
                </div>

                {!isPaidTier && (
                  <div className="px-3 py-2.5 border-t border-gray-100 flex items-start gap-1.5 text-[11px] text-gray-500 leading-snug">
                    <Lock className="h-3 w-3 mt-0.5 flex-shrink-0" />
                    <span>
                      Upgrade to Pro to keep chat history beyond today.{' '}
                      <button
                        type="button"
                        onClick={() => {
                          closePanel();
                          navigate('/pricing');
                        }}
                        className="text-[#3B82F6] hover:underline font-medium"
                      >
                        See plans
                      </button>
                    </span>
                  </div>
                )}
              </aside>

              {/* Chat column - the shared thread (same conversation as the
                  Getting Started page). */}
              <ScoutChatThread
                variant="panel"
                emptyStateExtra={
                  messages.length === 0 && triedHint ? (
                    <ScoutTriedFailedHint
                      triedPrompt={triedHint}
                      onWiden={handleHintWiden}
                      onDismiss={handleHintDismiss}
                    />
                  ) : null
                }
              />
            </div>
          )}
        </div>
      </div>
    </>
  );
}

export default ScoutSidePanel;
