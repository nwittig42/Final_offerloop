import React, { useState, useEffect, useMemo, useCallback, useRef, useLayoutEffect } from "react";
import { AppSidebar } from "@/components/AppSidebar";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppHeader } from "@/components/AppHeader";
import { useFirebaseAuth } from "../contexts/FirebaseAuthContext";
import { useCreditsView } from "@/hooks/useCreditsView";
import { useScout } from "@/contexts/ScoutContext";
import { useTour } from "@/contexts/TourContext";
import {
  Linkedin, Loader2, ArrowRight, ArrowUp,
  User, Check, CheckCircle,
  FileText, Upload, Mail, Inbox, AlertCircle, X, ExternalLink, ChevronRight, Lock, Send, Info,
  Sparkles, Table2
} from "lucide-react";
import { useNavigate, useSearchParams, useLocation } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { apiService, BACKEND_URL, isErrorResponse, type EmailTemplate, getEmailTemplateLabel } from "@/services/api";
import { firebaseApi } from "../services/firebaseApi";
import type { Contact as ContactApi } from '../services/firebaseApi';
import { toast } from "@/hooks/use-toast";
import { TIER_CONFIGS, CREDIT_COSTS } from "@/lib/constants";
import { logActivity, generateContactSearchSummary } from "@/utils/activityLogger";
import { EliteGateModal } from "@/components/EliteGateModal";
import { MainContentWrapper } from "@/components/MainContentWrapper";
import { db, storage, auth } from '@/lib/firebase';
import { doc, getDoc, updateDoc } from 'firebase/firestore';
import { ref, uploadBytes, getDownloadURL } from 'firebase/storage';
import { trackFeatureActionCompleted, trackError } from "../lib/analytics";
import { ACCEPTED_RESUME_TYPES, isValidResumeFile } from "@/utils/resumeFileTypes";
import ContactImport from "@/components/ContactImport";
import { motion, AnimatePresence } from "framer-motion";
import DimensionChips from "@/components/find/DimensionChips";
import CompanyAlternatives from "@/components/find/CompanyAlternatives";
import RoleVariations from "@/components/find/RoleVariations";
import StarterChips from "@/components/find/StarterChips";
import { SearchPromptBox, PEOPLE_SEARCH_HELPER_PREVIEW } from "@/components/find/SearchPromptBox";
import { ResultActionButton } from "@/components/find/ResultActionButton";
import { SendConfirmDialog } from "@/components/SendConfirmDialog";
import DraftDeliveryActions from "@/components/DraftDeliveryActions";
import InboxConnectNudge from "@/components/InboxConnectNudge";
import { canUseOutreachMode } from "@/utils/featureAccess";
import { UpgradeModal } from "@/components/gates/UpgradeModal";
import { SetupNudgeModal, type SetupNudgeVariant } from "@/components/gates/SetupNudgeModal";
import { invalidateResumeStatusCache } from "@/hooks/useResumeStatus";
import { findCompletion, expandQueryForBackend } from "@/lib/specificity";
import { PEOPLE_TEMPLATE_CATEGORIES } from "@/data/searchTemplates";
import { TemplateButton } from "@/components/TemplateButton";

import { DEV_MOCK_USER } from "@/lib/devPreview";
import { getUniversityShortName } from "@/lib/universityUtils";
import { PeopleFilters, peopleFiltersActive } from "@/types/findFilters";
import {
  readScoutPrefillEnvelope,
  SCOUT_PREFILL_EVENT,
  SCOUT_SEARCH_COMPLETED_EVENT,
} from "@/lib/scoutBridge";

// Session storage key for Scout auto-populate
const SCOUT_AUTO_POPULATE_KEY = 'scout_auto_populate';

function getPeopleFallbackPlaceholders(schoolShort: string | null): string[] {
  // Each prompt uses a different shape (alum-to-company, role+season, hiring-manager,
  // location, sector) so cycling them nudges the user toward thinking about WHICH
  // angle of search they care about, rather than copying a single template.
  const school = schoolShort || 'USC';
  return [
    `${school} alumni at Goldman Sachs`,
    `Stripe engineers hiring for summer 2026 SWE interns`,
    `Hiring managers for product roles at Sequoia-backed startups`,
    `${school} grads now working at Google in product`,
    `Investment bankers in NYC who went to ${school}`,
    `McKinsey consultants who studied at ${school}`,
    `Founders of YC startups who went to ${school}`,
    `Recruiters at Meta hiring for new-grad SWE`,
  ];
}

// Typewriter cycler: types one phrase, holds, erases, advances to the next.
// Returns the currently visible substring. Pause when `paused` is true so we
// don't waste cycles while the user is typing into the textarea.
function useTypewriterCycle(
  phrases: string[],
  opts?: { typeMs?: number; eraseMs?: number; holdMs?: number; paused?: boolean }
): string {
  const { typeMs = 45, eraseMs = 22, holdMs = 1600, paused = false } = opts ?? {};
  const [idx, setIdx] = useState(0);
  const [display, setDisplay] = useState('');
  const [phase, setPhase] = useState<'typing' | 'erasing'>('typing');

  // Reset when the source phrases change (e.g. user's school loads in async).
  useEffect(() => {
    setIdx(0);
    setDisplay('');
    setPhase('typing');
  }, [phrases]);

  useEffect(() => {
    if (paused || phrases.length === 0) return;
    const current = phrases[idx % phrases.length];

    if (phase === 'typing') {
      if (display.length < current.length) {
        const t = setTimeout(
          () => setDisplay(current.slice(0, display.length + 1)),
          typeMs,
        );
        return () => clearTimeout(t);
      }
      const t = setTimeout(() => setPhase('erasing'), holdMs);
      return () => clearTimeout(t);
    }

    // erasing
    if (display.length > 0) {
      const t = setTimeout(
        () => setDisplay(display.slice(0, -1)),
        eraseMs,
      );
      return () => clearTimeout(t);
    }
    setIdx((i) => (i + 1) % phrases.length);
    setPhase('typing');
  }, [phase, display, idx, phrases, paused, typeMs, eraseMs, holdMs]);

  return display;
}

// Title case formatter for display (does not mutate Firestore data)
const SMALL_WORDS = new Set(['a', 'an', 'and', 'as', 'at', 'but', 'by', 'for', 'if', 'in', 'nor', 'of', 'on', 'or', 'so', 'the', 'to', 'up', 'via', 'vs']);
function toTitleCase(str: string): string {
  if (!str) return str;
  // Skip if already mixed case (not all-upper or all-lower)
  if (str !== str.toUpperCase() && str !== str.toLowerCase()) return str;
  return str
    .toLowerCase()
    .split(' ')
    .map((word, i) => {
      if (i === 0 || !SMALL_WORDS.has(word)) {
        return word.charAt(0).toUpperCase() + word.slice(1);
      }
      return word;
    })
    .join(' ');
}

// LinkedIn URL detection helper
function isLinkedInUrl(input: string): boolean {
  const trimmed = input.trim();
  if (!trimmed) return false;
  if (/^https?:\/\/(www\.)?linkedin\.com\/in\/[\w-]+\/?/.test(trimmed)) return true;
  if (/^(www\.)?linkedin\.com\/in\/[\w-]+\/?/.test(trimmed)) return true;
  if (/^\/in\/[\w-]+\/?$/.test(trimmed)) return true;
  return false;
}

// Normalize a LinkedIn URL to a fully-qualified https URL
function normalizeLinkedInUrl(input: string): string {
  let url = input.trim();
  if (!url.startsWith('http://') && !url.startsWith('https://')) {
    if (url.startsWith('linkedin.com') || url.startsWith('www.linkedin.com')) {
      url = `https://${url}`;
    } else if (url.startsWith('/in/')) {
      url = `https://linkedin.com${url}`;
    } else if (url.includes('linkedin') && url.includes('/in/')) {
      const match = url.match(/\/in\/[^\/\s]+/);
      if (match) url = `https://linkedin.com${match[0]}`;
    } else {
      url = `https://linkedin.com/in/${url}`;
    }
  }
  return url;
}


// Helper function for contact count guidance

// Stripe-style Tabs Component with animated underline
interface StripeTabsProps {
  activeTab: string;
  onTabChange: (tab: string) => void;
  tabs: { id: string; label: string }[];
}

const StripeTabs: React.FC<StripeTabsProps> = ({ activeTab, onTabChange, tabs }) => {
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [indicatorStyle, setIndicatorStyle] = useState({ left: 0, width: 0 });

  // Update indicator position when active tab changes
  useLayoutEffect(() => {
    const activeIndex = tabs.findIndex(tab => tab.id === activeTab);
    const activeTabRef = tabRefs.current[activeIndex];

    if (activeTabRef) {
      const { offsetLeft, offsetWidth } = activeTabRef;
      setIndicatorStyle({ left: offsetLeft, width: offsetWidth });
    }
  }, [activeTab, tabs]);

  return (
    <div className="relative">
      {/* Tab buttons */}
      <div className="flex items-center gap-8">
        {tabs.map((tab, index) => (
          <button
            key={tab.id}
            ref={(el) => { tabRefs.current[index] = el; }}
            onClick={() => onTabChange(tab.id)}
            className={`
              relative pb-3 text-sm font-medium transition-colors duration-150
              focus:outline-none focus-visible:outline-none
              ${activeTab === tab.id
                ? 'text-[var(--accent, #4A60A8)]'
                : 'text-[#6B7280] hover:text-[#0F172A]'
              }
            `}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Full-width divider line */}
      <div className="absolute bottom-0 left-0 right-0 h-[1px] bg-[#E2E8F0]" />

      {/* Animated underline indicator - sits on top of divider */}
      <div
        className="absolute bottom-0 h-[2px] bg-[var(--accent, #4A60A8)] transition-all duration-200 ease-out"
        style={{
          left: indicatorStyle.left,
          width: indicatorStyle.width,
        }}
      />
    </div>
  );
};

// Find People default batch size per tier (where the slider initially sits).
// Free defaults to its max (3); Pro and Elite default to 5 (their max stays 8/15).
const PEOPLE_BATCH_DEFAULTS: Record<"free" | "pro" | "elite", number> = { free: 3, pro: 5, elite: 5 };

// Rotating status lines shown under the action buttons while drafting/sending.
// They narrate the real work (read profile → find commonality → write → place
// in Gmail) so the wait feels alive. The last line holds until the call returns.
const DRAFT_PROGRESS_STEPS = [
  "Reading your profile",
  "Finding what you have in common with each person",
  "Writing personalized emails",
  "Placing drafts in your Gmail",
];
const SEND_PROGRESS_STEPS = [
  "Reading your profile",
  "Finding what you have in common with each person",
  "Writing personalized emails",
  "Sending from your Gmail",
];

const ContactSearchPage: React.FC<{
  embedded?: boolean; hideSubTabs?: boolean; parentEmailTemplate?: EmailTemplate | null;
  isDevPreview?: boolean; initialQuery?: string;
  railFilters?: PeopleFilters; railFiltersNonce?: number;
  onParsedQuery?: (f: PeopleFilters) => void;
}> = ({ embedded = false, hideSubTabs = false, parentEmailTemplate, isDevPreview = false, initialQuery,
        railFilters, railFiltersNonce = 0, onParsedQuery }) => {
  const { user: authUser, checkCredits, updateCredits } = useFirebaseAuth();
  const user = isDevPreview ? DEV_MOCK_USER as any : authUser;
  const { openPanelWithSearchHelp } = useScout();
  const { demoSurface } = useTour();
  const peopleDemoActive = demoSurface === 'people';
  const navigate = useNavigate();
  const routerLocation = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const effectiveUser = user || {
    credits: 0,
    maxCredits: 0,
    name: "User",
    email: "user@example.com",
    tier: "free" as "free" | "pro" | "elite",
  };
  // Trial-aware credit balance for display (daily pool during a trial).
  const creditsView = useCreditsView();

  const userTier: "free" | "pro" | "elite" = useMemo(() => {
    // Use the actual tier from the user object, default to "free"
    const tier = effectiveUser?.tier;
    if (tier === "pro" || tier === "elite") return tier;
    return "free";
  }, [effectiveUser?.tier]);

  // Outreach is now decoupled from search. Every search just returns contacts
  // (preview). After results land, the user clicks Draft Outreach or Send Emails
  // to act on ALL found contacts at once.
  const [draftingAll, setDraftingAll] = useState(false);
  const [sendingAll, setSendingAll] = useState(false);
  // Hard confirm gate shown before a batch send (send is irreversible).
  const [showSendAllConfirm, setShowSendAllConfirm] = useState(false);
  // Upgrade prompt shown when a locked action button is clicked. Carries the
  // props UpgradeModal needs to show the right tier + trial CTA.
  const [upgradeGate, setUpgradeGate] = useState<
    { feature: string; label: string; requiredTier: 'pro' | 'elite'; reason: string } | null
  >(null);
  // Hover state for the Draft Emails info tooltip.
  const [showDraftInfo, setShowDraftInfo] = useState(false);
  // Rotating progress step shown under the buttons while drafting/sending.
  const [progressStep, setProgressStep] = useState(0);
  useEffect(() => {
    if (!draftingAll && !sendingAll) { setProgressStep(0); return; }
    const steps = sendingAll ? SEND_PROGRESS_STEPS : DRAFT_PROGRESS_STEPS;
    setProgressStep(0);
    const id = setInterval(() => {
      setProgressStep((s) => Math.min(s + 1, steps.length - 1));
    }, 2400);
    return () => clearInterval(id);
  }, [draftingAll, sendingAll]);

  // Per-draft send state for the inline Send button on reviewed drafts.
  // confirmSendDraftIdx: index in lastResults for which the confirm dialog is open.
  // sendingDraftIdx: index currently in-flight (drives loading state on the button).
  const [confirmSendDraftIdx, setConfirmSendDraftIdx] = useState<number | null>(null);
  const [sendingDraftIdx, setSendingDraftIdx] = useState<number | null>(null);

  async function handleSendReviewedDraft(idx: number) {
    const c = lastResults[idx];
    const draftId = c?.gmailDraftId;
    if (!draftId) {
      toast({ description: 'No draft ID on this contact.' });
      return;
    }
    setSendingDraftIdx(idx);
    try {
      const result = await apiService.sendDraft(draftId);
      // 410 (draft_not_found) means the draft was already sent — treat as success.
      const sent = result?.success || result?.error === 'draft_not_found';
      if (sent) {
        setLastResults((prev) => prev.map((row, i) => (i === idx ? { ...row, emailSent: true } : row)));
        toast({ description: 'Email sent.' });
      } else {
        toast({ description: result?.message || 'Send failed. Try Open in Gmail.' });
      }
    } catch (err: any) {
      console.error('[SendDraft] failed', err);
      toast({ description: err?.message || 'Send failed. Try Open in Gmail.' });
    } finally {
      setSendingDraftIdx(null);
      setConfirmSendDraftIdx(null);
    }
  }

  function isSearchResult(x: any): x is { contacts: any[]; successful_drafts?: number } {
    return x && Array.isArray(x.contacts);
  }

  // Create Gmail drafts for any found contact that doesn't have one yet, merging
  // the generated subject/body/draftId back onto lastResults. Returns the merged
  // array, or null if drafting failed. Matches drafts to contacts by email so
  // it's robust to the filtered subset we send.
  async function ensureDraftsForAll(): Promise<any[] | null> {
    const toDraft = lastResults.filter(
      (c: any) => !(c.gmailDraftId || c.emailSubject || c.emailBody) && (c.Email || c.email)
    );
    if (toDraft.length === 0) return lastResults;

    const res = await apiService.generateAndDraftEmails({ contacts: toDraft, emailTemplate: activeEmailTemplate });
    if (!res || (res as any).error) {
      toast({
        title: 'Couldn’t create drafts',
        description: (res as any)?.message || (res as any)?.error || 'Reconnect your Gmail account and try again.',
        variant: 'destructive',
      });
      return null;
    }
    setFallbackDelivery((res as any).deliveryMode === 'fallback');

    const byEmail = new Map<string, any>();
    ((res as any).drafts || []).forEach((d: any) => {
      if (d?.to) byEmail.set(String(d.to).toLowerCase(), d);
    });
    const merged = lastResults.map((c: any) => {
      const key = String(c.Email || c.email || '').toLowerCase();
      const d = key ? byEmail.get(key) : undefined;
      if (!d) return c;
      return {
        ...c,
        gmailDraftId: d.draftId ?? c.gmailDraftId,
        gmailDraftUrl: d.gmailUrl ?? c.gmailDraftUrl,
        emailSubject: d.subject ?? c.emailSubject,
        emailBody: d.body ?? c.emailBody,
      };
    });
    setLastResults(merged);
    return merged;
  }

  // Draft Outreach button: draft personalized emails to every found contact.
  async function handleDraftAll() {
    if (!canUseOutreachMode(userTier, 'draft')) {
      setUpgradeGate({
        feature: 'bulkDrafting',
        label: 'Draft Outreach',
        requiredTier: 'pro',
        reason: 'Draft a personalized email to every contact you find — in one click.',
      });
      return;
    }
    if (lastResults.length === 0) return;
    // Setup nudges, once per session each, never stacked: Gmail first (the
    // drafts otherwise degrade to compose links), then resume (personalization
    // quality). "Continue without" runs the draft anyway.
    if (gmailConnected === false && !sessionStorage.getItem('ol_nudge_gmail')) {
      sessionStorage.setItem('ol_nudge_gmail', '1');
      setSetupNudge({ variant: 'gmail', onContinue: () => void runDraftAll() });
      return;
    }
    if (gmailConnected !== false && !savedResumeUrl && !sessionStorage.getItem('ol_nudge_resume')) {
      sessionStorage.setItem('ol_nudge_resume', '1');
      setSetupNudge({ variant: 'resume', onContinue: () => void runDraftAll() });
      return;
    }
    await runDraftAll();
  }

  async function runDraftAll() {
    if (lastResults.length === 0) return;
    setDraftingAll(true);
    try {
      const merged = await ensureDraftsForAll();
      if (merged) {
        const drafted = merged.filter((c: any) => c.gmailDraftId || c.emailSubject).length;
        toast({ title: 'Drafts ready', description: `${drafted} draft${drafted === 1 ? '' : 's'} created in your Gmail.` });
      }
    } catch (err: any) {
      console.error('[DraftAll] failed', err);
      toast({ title: 'Couldn’t create drafts', description: err?.message || 'Please try again.', variant: 'destructive' });
    } finally {
      setDraftingAll(false);
    }
  }

  // Send Emails button: draft (if needed) then send to every found contact.
  // Irreversible, so it routes through the hard-confirm dialog first.
  async function handleSendAll(confirmed = false) {
    if (!canUseOutreachMode(userTier, 'send')) {
      setUpgradeGate({
        feature: 'prioritySend',
        label: 'Send Emails',
        requiredTier: 'elite',
        reason: 'Send personalized emails to every contact you find — automatically.',
      });
      return;
    }
    if (lastResults.length === 0) return;
    // Sending goes through the user's own Gmail: hard requirement, no
    // "continue without" path.
    if (gmailConnected === false) {
      setSetupNudge({ variant: 'gmail' });
      return;
    }
    if (!confirmed) {
      setShowSendAllConfirm(true);
      return;
    }
    setSendingAll(true);
    try {
      const merged = await ensureDraftsForAll();
      if (!merged) return;
      const draftIds = merged.map((c: any) => c.gmailDraftId).filter(Boolean);
      if (draftIds.length === 0) {
        toast({ description: 'No drafts available to send.' });
        return;
      }
      // Batch send — one HTTP round trip, backend fans out with max 5 concurrent
      // Gmail sends. Reads exact draft bytes; email content is unchanged.
      const batch = await apiService.sendDraftsBatch(draftIds);
      const sent = batch?.sent_count ?? 0;
      // Mark only the drafts that actually sent (or were already gone) —
      // hard failures stay un-flipped so the user can retry per-row.
      const sentIds = new Set(
        (batch?.results ?? [])
          .filter((r) => r.success || r.error === 'draft_not_found')
          .map((r) => r.draftId)
      );
      setLastResults((prev) =>
        prev.map((c: any) =>
          c.gmailDraftId && sentIds.has(c.gmailDraftId) ? { ...c, emailSent: true } : c
        )
      );
      toast({
        title: sent > 0 ? 'Emails sent' : 'Send failed',
        description: `${sent} of ${draftIds.length} sent from your Gmail.`,
        variant: sent > 0 ? undefined : 'destructive',
      });
    } catch (err: any) {
      console.error('[SendAll] failed', err);
      toast({ title: 'Send failed', description: err?.message || 'Please try again.', variant: 'destructive' });
    } finally {
      setSendingAll(false);
    }
  }

  const searchSuccessRef = useRef<HTMLDivElement>(null);
  const promptInputRef = useRef<HTMLTextAreaElement>(null);

  // Overrides apply only after a rail edit (nonce > 0) AND only while the
  // prompt text is unchanged — typing a new prompt hands control back to the
  // parser and the rail repopulates from its output.
  const lastSearchedPromptRef = useRef<string>("");
  const lastRailNonceRef = useRef(0);
  // Search state
  // Declared here (ahead of its historical spot further down) because the
  // rail-nonce effect below reads it in its dependency array, which is
  // evaluated eagerly during render — a forward reference there would be a
  // TDZ error, unlike the effect *body*, which only runs after the whole
  // component function has finished executing.
  const [isSearching, setIsSearching] = useState(false);

  useEffect(() => {
    if (railFiltersNonce === 0 || railFiltersNonce === lastRailNonceRef.current) return;
    if (isSearching) return; // leave the nonce pending; this effect re-fires when isSearching flips false
    lastRailNonceRef.current = railFiltersNonce;
    handleSearch();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [railFiltersNonce, isSearching]);

  // Form state (prompt-based search).
  // Initial value may come from the landing-page hero: HeroSearchCTA stashes
  // the visitor's query in localStorage under `offerloop_pending_query` before
  // redirecting them into the sign-up flow. We consume it once on mount so
  // their first in-app experience is the exact search they asked for.
  const pendingAutoSearch = useRef(false);
  // Tracks the last initialQuery we consumed from the Getting Started launcher,
  // so a hand-off runs exactly once even though this page stays mounted.
  const consumedInitialQuery = useRef<string | null>(null);
  const [searchPrompt, setSearchPrompt] = useState(() => {
    try {
      const pending = typeof window !== 'undefined'
        ? window.localStorage.getItem('offerloop_pending_query')
        : null;
      if (pending) {
        window.localStorage.removeItem('offerloop_pending_query');
        return pending;
      }
    } catch {
      // Private mode / disabled storage — just fall through to empty
    }
    return "";
  });
  const [hoveredChipPrompt, setHoveredChipPrompt] = useState<string | null>(null);
  const [showTemplateTooltip, setShowTemplateTooltip] = useState(false);
  const templateTooltipTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [showCsvTooltip, setShowCsvTooltip] = useState(false);
  const csvTooltipTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [uploadedFile, setUploadedFile] = useState<File | null>(null);
  const [savedResumeUrl, setSavedResumeUrl] = useState<string | null>(null);
  const [savedResumeFileName, setSavedResumeFileName] = useState<string | null>(null);
  const [isUploadingResume, setIsUploadingResume] = useState(false);
  const [currentFitContext, setCurrentFitContext] = useState<any>(null); // Track fit context for UI display

  // isSearching is declared earlier (near the rail-nonce effect that reads
  // it in a dependency array) — see the comment there.
  // Rotation seed — drives randomization in the right-rail recommendations so
  // users who click Network multiple times get fresh suggestions each pass.
  // Initialized to a random page-load value, incremented every search submission.
  const [rotationSeed, setRotationSeed] = useState(() => Math.floor(Math.random() * 1_000_000));
  const [progressValue, setProgressValue] = useState(0);
  const [searchComplete, setSearchComplete] = useState(false);
  const [lastResults, setLastResults] = useState<any[]>([]);
  // True when the last draft batch was delivered in fallback mode (no Gmail
  // connection): drives the inbox-connect nudge above the results list.
  const [fallbackDelivery, setFallbackDelivery] = useState(false);
  const [expandedEmailIdx, setExpandedEmailIdx] = useState<number | null>(null);
  const [smartPlaceholder, setSmartPlaceholder] = useState<string | null>(null);
  const [alreadySavedResults, setAlreadySavedResults] = useState<any[]>([]);
  // Backend-provided message for unusual result shapes (e.g. "All N matching contact(s)
  // are already in your tracker..."). Prefer this over hardcoded frontend copy.
  const [resultMessage, setResultMessage] = useState<string>("");
  // Backend signals when its retry chain dropped filters to find matches. We
  // surface this honestly above the result list so the user knows the system
  // worked harder for them — and which constraints loosened — instead of
  // pretending the original specific query yielded those people.
  const [searchSuggestions, setSearchSuggestions] = useState<any[]>([]);
  const [searchBroadened, setSearchBroadened] = useState(false);
  // Filters the provider that actually served these results could not honour.
  // Hunter's Domain Search only filters by company, so a search for alumni at
  // a firm with none silently returns colleagues instead — these drive the
  // notice that says so.
  const [unsupportedFilters, setUnsupportedFilters] = useState<string[]>([]);
  const [lastSearchStats, setLastSearchStats] = useState<{
    successful_drafts: number;
    total_contacts: number;
  } | null>(null);

  // ── Tour demo state ──────────────────────────────────────────────────────
  // When the product tour reaches the Find People step it sets `demoSurface`
  // to 'people'. This effect runs a fully local demo: types the example query
  // into the prompt state, flashes a short "Searching…" hold, then seeds a
  // single inert card. Every state write here is to the same setters the real
  // search uses, so the visual matches a real result exactly — but no API
  // call is ever made. The cleanup return wipes every piece of demo residue
  // (query, results, searching flags, the landing-page handoff localStorage
  // key) so the page is bone-clean the moment the tour advances or aborts.
  const PEOPLE_DEMO_QUERY = 'founder of offerloop';
  // Three inert demo cards — the Offerloop founding team. Each carries
  // `demo: true`, which the per-card action handlers in the results map
  // (Copy email, etc.) already guard against, so all three render fully
  // styled but every action no-ops uniformly.
  const PEOPLE_DEMO_CARDS = [
    {
      demo: true as const,
      FirstName: 'Nick',
      LastName: 'Wittig',
      JobTitle: 'Cofounder',
      Company: 'Offerloop',
      Email: 'nickwittig@offerloop.ai',
      emailSubject: 'Coffee chat about Offerloop',
      emailBody:
        "Hi Nick,\n\nI came across Offerloop and love the take on AI-driven networking for students. I'd love to learn how you've thought about positioning the product for the college-recruiting use case.\n\nWould you be open to a 20-minute call next week?\n\nBest,\n[Your name]",
    },
    {
      demo: true as const,
      FirstName: 'Rylan',
      LastName: 'Bohnett',
      JobTitle: 'CMO',
      Company: 'Offerloop',
      Email: 'rylan@offerloop.ai',
      emailSubject: 'Quick question about Offerloop',
      emailBody:
        "Hi Rylan,\n\nI've been following Offerloop's launch and the storytelling stands out. I'd love to hear how you've thought about reaching college students at scale.\n\nWould you be open to a 20-minute call next week?\n\nBest,\n[Your name]",
    },
    {
      demo: true as const,
      FirstName: 'Deena',
      LastName: 'Bandi',
      JobTitle: 'CTO',
      Company: 'Offerloop',
      Email: 'deena@offerloop.ai',
      emailSubject: 'A quick chat about Offerloop',
      emailBody:
        "Hi Deena,\n\nOfferloop's product caught my eye — the agent layer over the contact graph feels novel. I'd love to learn how you've architected it for the scale you're seeing.\n\nWould you be open to a 20-minute call next week?\n\nBest,\n[Your name]",
    },
  ];

  useEffect(() => {
    if (!peopleDemoActive) return;
    let cancelled = false;
    const timers: ReturnType<typeof setTimeout>[] = [];
    const TYPE_DELAY_MS = 32;
    const SEARCHING_HOLD_MS = 1500;
    const POST_TYPE_PAUSE_MS = 250;

    // Reset to a clean slate before the animation begins.
    setSearchPrompt('');
    setLastResults([]);
    setSearchComplete(false);
    setIsSearching(false);
    setProgressValue(0);

    // Type one character per tick into the real `searchPrompt` state. Safe
    // because nothing watches `searchPrompt` to fire a search.
    for (let i = 1; i <= PEOPLE_DEMO_QUERY.length; i++) {
      timers.push(
        setTimeout(() => {
          if (cancelled) return;
          setSearchPrompt(PEOPLE_DEMO_QUERY.slice(0, i));
        }, i * TYPE_DELAY_MS),
      );
    }

    const typingDoneAt = PEOPLE_DEMO_QUERY.length * TYPE_DELAY_MS + POST_TYPE_PAUSE_MS;
    timers.push(
      setTimeout(() => {
        if (cancelled) return;
        setIsSearching(true);
        setProgressValue(45);
      }, typingDoneAt),
    );
    timers.push(
      setTimeout(() => {
        if (cancelled) return;
        setIsSearching(false);
        setProgressValue(100);
        setLastResults(PEOPLE_DEMO_CARDS);
        setSearchComplete(true);
      }, typingDoneAt + SEARCHING_HOLD_MS),
    );

    return () => {
      cancelled = true;
      timers.forEach(clearTimeout);
      setSearchPrompt('');
      setLastResults([]);
      setIsSearching(false);
      setSearchComplete(false);
      setProgressValue(0);
      try {
        if (typeof window !== 'undefined') {
          window.localStorage.removeItem('offerloop_pending_query');
        }
      } catch {
        // ignore — private mode / disabled storage
      }
    };
    // PEOPLE_DEMO_QUERY and PEOPLE_DEMO_CARDS are stable literals; only the
    // active flag drives this effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [peopleDemoActive]);
  const hasResults = lastResults.length > 0 || alreadySavedResults.length > 0;

  // Auto-scroll to success state after search completes
  useEffect(() => {
    if (hasResults && !isSearching && searchSuccessRef.current) {
      searchSuccessRef.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
  }, [hasResults, isSearching]);

  // Batch size state. Default sits at a tier-specific value (free 5, pro 10,
  // elite 20). For free, the default equals the max (5), so the slider sits at
  // its maximum. The clamp effect below keeps it within maxBatchSize.
  const userAdjustedBatchSize = useRef(false);
  const [batchSize, setBatchSize] = useState<number>(() => PEOPLE_BATCH_DEFAULTS[userTier]);

  // Flash effect on the quantity slider — vibrant blue while the user is
  // actively dragging, fades back to slate ~700ms after they stop. Each
  // onChange resets the timer so the bright state persists during a drag.
  const [sliderFlashing, setSliderFlashing] = useState(false);
  const sliderFlashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const flashSlider = () => {
    setSliderFlashing(true);
    if (sliderFlashTimer.current) clearTimeout(sliderFlashTimer.current);
    sliderFlashTimer.current = setTimeout(() => setSliderFlashing(false), 700);
  };
  useEffect(() => () => {
    if (sliderFlashTimer.current) clearTimeout(sliderFlashTimer.current);
  }, []);

  // When the tier becomes known or changes and the user has not moved the
  // slider yet, snap the default to that tier's value.
  useEffect(() => {
    if (!userAdjustedBatchSize.current) {
      setBatchSize(PEOPLE_BATCH_DEFAULTS[userTier]);
    }
  }, [userTier]);

  // Typewriter rotating examples (types + deletes between rotations)
  const [typedText, setTypedText] = useState('');
  const [twIdx, setTwIdx] = useState(0);
  const [twPhase, setTwPhase] = useState<'typing' | 'pausing' | 'deleting'>('typing');
  const [profileHints, setProfileHints] = useState<string[]>([]);
  // Strings worth offering as ghost-completion: explicit target firms, target
  // locations, target industries, extracted roles. Sourced from the user's profile
  // so completions feel like the system already knows them.
  const [profileCompletionTokens, setProfileCompletionTokens] = useState<string[]>([]);
  // Structured profile facts — used by QuickStarters to assemble click-to-fill
  // queries that match the user's actual targets. Mirrors the personalization
  // chips on the Profile page.
  const [profileFacts, setProfileFacts] = useState<{
    schoolShort?: string;
    schoolFull?: string;
    targetFirms?: string[];
    targetIndustries?: string[];
    preferredLocations?: string[];
    extractedRoles?: string[];
  }>({});
  const [inputFocused, setInputFocused] = useState(false);
  // Focus model is now strictly tied to inputFocused: recommendations show whenever
  // the user is NOT actively in the input. No manual override or outside-click handler.
  const [userSchoolShort, setUserSchoolShort] = useState<string | null>(null);
  const peopleFallbackPlaceholders = useMemo(() => getPeopleFallbackPlaceholders(userSchoolShort), [userSchoolShort]);
  const typedPlaceholder = useTypewriterCycle(peopleFallbackPlaceholders);

  useEffect(() => {
    if (!user?.uid) return;
    if (isDevPreview) {
      const school = getUniversityShortName((user as any)?.university);
      setUserSchoolShort(school);
      const hints: string[] = [];
      if (school) {
        hints.push(`${school} alumni in Data Science`);
        hints.push('Who\'s hiring for AI/ML?');
        hints.push(`${school} grads at McKinsey`);
      } else {
        hints.push('Alumni in Data Science');
        hints.push('Who\'s hiring for AI/ML?');
      }
      hints.push('Analysts in Los Angeles');
      hints.push('Try a company name, role, or school');
      setProfileHints(hints);
      return;
    }
    firebaseApi.getUserOnboardingData(user.uid).then((data) => {
      const uni = data.university || '';
      const shortUni = uni.replace(/^(University of |The )/, '').split(' - ')[0].split(',')[0].trim();
      setUserSchoolShort(getUniversityShortName(uni));
      const industries = data.targetIndustries || [];
      const locs = data.preferredLocations || [];
      const firms = data.targetFirms || [];
      const roles = data.extractedRoles || [];
      const hints: string[] = [];
      if (shortUni && industries[0]) hints.push(`${shortUni} alumni in ${industries[0]}`);
      if (industries[0]) hints.push(`Who's hiring for ${industries[0]}?`);
      if (shortUni && firms[0]) hints.push(`${shortUni} alumni at ${firms[0]}`);
      else if (shortUni) hints.push(`${shortUni} grads at McKinsey`);
      if (locs[0]) hints.push(`Analysts in ${locs[0]}`);
      hints.push("Paste a LinkedIn URL to import a contact");
      hints.push("Who do you want to meet today?");
      if (shortUni && firms[1]) hints.push(`${shortUni} alumni at ${firms[1]}`);
      else if (shortUni) hints.push(`${shortUni} alumni at Goldman Sachs`);
      hints.push("Try a company name, role, or school");
      if (hints.length > 0) setProfileHints(hints);

      // Build profile completion tokens — used to bias the ghost-text autocomplete
      // toward strings the user has already told us they care about.
      const tokens: string[] = [];
      for (const f of firms) if (typeof f === 'string') tokens.push(f);
      for (const l of locs) if (typeof l === 'string') tokens.push(l);
      for (const i of industries) if (typeof i === 'string') tokens.push(i);
      for (const r of roles) if (typeof r === 'string') tokens.push(r);
      if (uni) tokens.push(uni);
      if (shortUni) tokens.push(shortUni);
      setProfileCompletionTokens(tokens);

      // Structured profile facts for QuickStarters
      setProfileFacts({
        schoolShort: getUniversityShortName(uni) || shortUni,
        schoolFull: uni,
        targetFirms: firms,
        targetIndustries: industries,
        preferredLocations: locs,
        extractedRoles: roles,
      });
    }).catch(() => {});
  }, [user?.uid, isDevPreview]);

  // Typewriter examples — derived from profile hints, with fallback list
  const typewriterExamples = useMemo(() => {
    const filtered = profileHints.filter((h) =>
      !h.toLowerCase().startsWith('paste') &&
      !h.toLowerCase().startsWith('try a') &&
      !h.endsWith('?')
    );
    if (filtered.length > 0) return filtered;
    const fallback = [
      'Software engineers at Google',
      'Investment bankers in NYC',
      'Marketing managers in LA',
      'Data scientists at Meta',
      'Product managers at Stripe',
    ];
    if (userSchoolShort) fallback.unshift(`${userSchoolShort} alumni at Goldman Sachs`);
    return fallback;
  }, [profileHints, userSchoolShort]);

  useEffect(() => {
    if (inputFocused || searchPrompt) return;
    if (typewriterExamples.length === 0) return;
    const current = typewriterExamples[twIdx % typewriterExamples.length];
    if (twPhase === 'typing') {
      if (typedText.length < current.length) {
        const t = setTimeout(() => setTypedText(current.slice(0, typedText.length + 1)), 55);
        return () => clearTimeout(t);
      }
      const t = setTimeout(() => setTwPhase('pausing'), 1400);
      return () => clearTimeout(t);
    }
    if (twPhase === 'pausing') {
      const t = setTimeout(() => setTwPhase('deleting'), 80);
      return () => clearTimeout(t);
    }
    if (twPhase === 'deleting') {
      if (typedText.length > 0) {
        const t = setTimeout(() => setTypedText(current.slice(0, typedText.length - 1)), 28);
        return () => clearTimeout(t);
      }
      const t = setTimeout(() => {
        setTwIdx((i) => (i + 1) % typewriterExamples.length);
        setTwPhase('typing');
      }, 200);
      return () => clearTimeout(t);
    }
  }, [twPhase, typedText, twIdx, typewriterExamples, inputFocused, searchPrompt]);

  // Typewriter shows when input is blurred + empty (greeting state). Once the
  // user focuses or types, it gets out of the way — the QuickStarters row
  // below the input takes over the "what could I try?" job.
  const showAnimatedPlaceholder = !searchPrompt;
  // Ghost-text autocomplete: predicts the rest of the word/phrase the user is typing
  // against the dimension lexicons. Tab or Right-arrow (when cursor at end) accepts.
  const ghostCompletion = useMemo(
    () => findCompletion(searchPrompt, profileCompletionTokens),
    [searchPrompt, profileCompletionTokens],
  );

  // Hover preview from the right-column suggestion popovers. When set, the prompt
  // overlay renders the previewed swap (highlighted) without modifying searchPrompt.
  // Click on a suggestion commits via onAcceptSuggestion (existing path).
  const [previewSwap, setPreviewSwap] = useState<{ matched: string; chosen: string } | null>(null);
  const previewedPrompt = useMemo(() => {
    if (!previewSwap) return null;
    const escaped = previewSwap.matched.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const regex = new RegExp(`\\b${escaped}\\b`, 'i');
    const match = regex.exec(searchPrompt);
    if (!match) return null;
    const before = searchPrompt.slice(0, match.index);
    const after = searchPrompt.slice(match.index + match[0].length);
    return { before, replacement: previewSwap.chosen.toLowerCase(), after };
  }, [previewSwap, searchPrompt]);

  const maxBatchSize = useMemo(() => {
    const tierMax = userTier === 'free' ? 3 : userTier === 'pro' ? 8 : 15;
    const creditMax = Math.floor(creditsView.balance / 10);
    return Math.min(tierMax, creditMax);
  }, [userTier, creditsView.balance]);

  useEffect(() => {
    if (batchSize > maxBatchSize) {
      setBatchSize(Math.max(1, maxBatchSize));
    }
  }, [maxBatchSize, batchSize]);

  // Gmail state
  const [gmailConnected, setGmailConnected] = useState<boolean | null>(null);
  // Setup nudge popup (Gmail / resume) shown at draft/send CTA moments.
  // Only opened when the gap actually exists; soft nudges pass onContinue.
  const [setupNudge, setSetupNudge] = useState<null | {
    variant: SetupNudgeVariant;
    onContinue?: () => void;
  }>(null);
  const [gmailBannerDismissed, setGmailBannerDismissed] = useState(() => {
    try { return localStorage.getItem('offerloop-gmail-banner-dismissed') === 'true'; } catch { return false; }
  });
  const dismissGmailBanner = () => {
    setGmailBannerDismissed(true);
    try { localStorage.setItem('offerloop-gmail-banner-dismissed', 'true'); } catch {}
  };

  // Tab state
  const [activeTab, setActiveTab] = useState<string>("contact-search");
  const [showEliteGate, setShowEliteGate] = useState(false);
  const [showImportDialog, setShowImportDialog] = useState(false);
  // Outreach setup card is collapsed into the settings summary row; Edit expands it.
  const [outreachSetupOpen, setOutreachSetupOpen] = useState(false);

  const isElite = user?.tier === "elite";

  // LinkedIn Import state
  const [linkedInUrl, setLinkedInUrl] = useState('');
  const [linkedInLoading, setLinkedInLoading] = useState(false);
  const [linkedInError, setLinkedInError] = useState<string | null>(null);
  const [linkedInSuccess, setLinkedInSuccess] = useState<string | null>(null);
  const [linkedInLastDraftUrl, setLinkedInLastDraftUrl] = useState<string | null>(null);

  // Email template state (saved default + session override)
  const [savedEmailTemplate, setSavedEmailTemplate] = useState<EmailTemplate | null>(null);
  const [sessionEmailTemplate, setSessionEmailTemplate] = useState<EmailTemplate | null>(null);
  const activeEmailTemplate = parentEmailTemplate ?? sessionEmailTemplate ?? savedEmailTemplate;

  // Fallback to 'free' config if tier not found (safety for new tiers)
  const currentTierConfig = TIER_CONFIGS[userTier] || TIER_CONFIGS.free;

  // Sync activeTab from URL ?tab= (for product tour)
  useEffect(() => {
    const tabParam = searchParams.get('tab');
    if (tabParam === 'linkedin-email') {
      setActiveTab('contact-search');
    } else if (tabParam === 'contact-search' || tabParam === 'contact-library' || tabParam === 'import') {
      setActiveTab(tabParam);
    }
  }, [searchParams]);

  // Read URL parameters (e.g., from "View Contacts" in Firm Library, suggestion cards)
  useEffect(() => {
    const companyParam = searchParams.get('company');
    const locationParam = searchParams.get('location');
    const roleParam = searchParams.get('role');

    if (companyParam || locationParam || roleParam) {
      const parts = [];
      if (roleParam) parts.push(roleParam);
      if (companyParam) parts.push(`at ${companyParam}`);
      if (locationParam) parts.push(`in ${locationParam}`);
      setSearchPrompt(parts.join(' ') || '');
      pendingAutoSearch.current = true;

      setSearchParams({}, { replace: true });
    }
  }, [searchParams]); // React to param changes from suggestion cards and other navigations

  // Prefill + auto-run from the Getting Started launcher hand-off (initialQuery).
  // Reuses the same pendingAutoSearch path as the URL-param intake below.
  useEffect(() => {
    const q = (initialQuery || '').trim();
    if (q && consumedInitialQuery.current !== initialQuery) {
      consumedInitialQuery.current = initialQuery ?? null;
      setSearchPrompt(q);
      pendingAutoSearch.current = true;
    }
  }, [initialQuery]);

  // Auto-trigger search when pre-filled from URL params (e.g. job board "Find Contact")
  useEffect(() => {
    if (pendingAutoSearch.current && searchPrompt.trim() && user) {
      pendingAutoSearch.current = false;
      handleSearch();
    }
  }, [searchPrompt, user]);

  // Load saved email template on mount (contact search tab)
  useEffect(() => {
    if (!user?.uid || isDevPreview) return;
    apiService.getEmailTemplate().then((t) => {
      setSavedEmailTemplate({
        purpose: t.purpose ?? null,
        stylePreset: t.stylePreset ?? null,
        customInstructions: t.customInstructions ?? "",
        name: t.name,
        subject: t.subject,
        savedTemplateId: t.savedTemplateId,
      });
    }).catch(() => {});
  }, [user?.uid]);

  // When returning from Email Templates page with "Apply to this search", apply the template and clear state
  useEffect(() => {
    const state = (routerLocation.state as { appliedEmailTemplate?: EmailTemplate } | undefined)?.appliedEmailTemplate;
    if (state) {
      setSessionEmailTemplate(state);
      try {
        sessionStorage.removeItem("offerloop_applied_email_template");
      } catch {
        // ignore
      }
      navigate("/find", { replace: true, state: {} });
      return;
    }
    // Fallback: template may have been stored in sessionStorage if navigation state was lost
    try {
      const raw = sessionStorage.getItem("offerloop_applied_email_template");
      if (raw) {
        const parsed = JSON.parse(raw) as EmailTemplate;
        if (parsed && (parsed.purpose != null || parsed.stylePreset != null || (parsed.customInstructions && parsed.customInstructions.trim()))) {
          setSessionEmailTemplate(parsed);
        }
        sessionStorage.removeItem("offerloop_applied_email_template");
      }
    } catch {
      // ignore
    }
  }, [routerLocation.state]);

  // Shared Scout populate: fills the search box from Scout-provided fields.
  // Used by both the legacy scout_auto_populate bridge (failed-search
  // recovery) and the scout_prefill bridge (chat navigate / CTA / plan step).
  const applyScoutPopulate = useCallback(
    (populateData: {
      job_title?: string;
      company?: string;
      location?: string;
      prompt?: string;
      autoSubmit?: boolean;
    }) => {
      // Prompt-mode: a full natural-language prompt goes straight into the
      // search bar. autoSubmit runs the search immediately so the user sees
      // results without a second click.
      if (populateData.prompt && populateData.prompt.trim()) {
        setSearchPrompt(populateData.prompt.trim());
        if (populateData.autoSubmit) {
          pendingAutoSearch.current = true;
        } else {
          toast({
            title: "Search pre-filled",
            description: "Scout has filled in your search. Click Search to find contacts.",
          });
        }
        return;
      }

      const { job_title, company: autoCompany, location: autoLocation } = populateData;
      const parts = [];
      if (job_title != null && job_title !== '') parts.push(job_title);
      if (autoCompany != null && autoCompany !== '') parts.push(`at ${autoCompany}`);
      if (autoLocation != null && autoLocation !== '') parts.push(`in ${autoLocation}`);
      if (parts.length) {
        setSearchPrompt(parts.join(' '));
        if (populateData.autoSubmit) {
          pendingAutoSearch.current = true;
        } else {
          toast({
            title: "Search pre-filled",
            description: "Scout has filled in your search. Click Search to find contacts.",
          });
        }
      }
    },
    [],
  );

  // Handle Scout auto-populate from failed search, chat "Take me there", or navigation state
  useEffect(() => {
    const applyPopulate = applyScoutPopulate;

    const handleAutoPopulate = () => {
      try {
        // Prefer navigation state (set when clicking "Take me there" from Scout panel)
        const stateData = (routerLocation.state as { scoutAutoPopulate?: { search_type?: string; job_title?: string; company?: string; location?: string } } | undefined)?.scoutAutoPopulate;
        if (stateData?.search_type === 'contact') {
          applyPopulate(stateData);
          sessionStorage.removeItem(SCOUT_AUTO_POPULATE_KEY);
          navigate(routerLocation.pathname, { replace: true, state: {} });
          return;
        }

        const stored = sessionStorage.getItem(SCOUT_AUTO_POPULATE_KEY);
        if (stored) {
          const data = JSON.parse(stored);

          // Handle both formats: search help format (nested) and chat format (flat)
          let populateData: { job_title?: string; company?: string; location?: string };
          if (data.search_type === 'contact') {
            if (data.auto_populate) {
              populateData = data.auto_populate;
            } else {
              populateData = data;
            }
            applyPopulate(populateData);
            sessionStorage.removeItem(SCOUT_AUTO_POPULATE_KEY);
          }
        }
      } catch (e) {
        console.error('[Scout] Auto-populate error:', e);
      }
    };

    handleAutoPopulate();

    window.addEventListener('scout-auto-populate', handleAutoPopulate);
    return () => window.removeEventListener('scout-auto-populate', handleAutoPopulate);
  }, [routerLocation.state, routerLocation.pathname, navigate, applyScoutPopulate]);

  // Scout chat prefill bridge (scout_prefill in sessionStorage): the panel's
  // navigate / CTA chip / plan step writes it keyed to this route, then
  // navigates (or dispatches the in-place event when already here). Consume on
  // mount and on the event. Gated to the People tab because all three Find
  // tabs stay mounted and the envelope is consume-on-read: without the gate
  // a hidden tab could swallow a prefill addressed to a sibling.
  useEffect(() => {
    const applyFromBridge = () => {
      // Fixed page identity: this embedded page IS the Find People tab, so it
      // reads only envelopes addressed to /find (people). The bridge's
      // identity matching keeps companies/hiring-manager prefill out.
      const env = readScoutPrefillEnvelope('/find');
      if (!env) return;
      const p = env.prefill || {};
      applyScoutPopulate({
        prompt: p.prompt,
        job_title: p.job_title,
        company: p.company,
        location: p.location,
        autoSubmit: env.auto_submit,
      });
    };
    applyFromBridge();
    window.addEventListener(SCOUT_PREFILL_EVENT, applyFromBridge);
    return () => window.removeEventListener(SCOUT_PREFILL_EVENT, applyFromBridge);
  }, [routerLocation.pathname, searchParams, applyScoutPopulate]);

  // Helper function to trigger Scout on 0 results. Forwards the parsed query +
  // retry-chain context so the backend can generate concrete refined prompts
  // ("try Mediobanca instead — Bocconi pipeline") rather than generic title
  // alternatives. The extra signals are optional; the backend falls through to
  // the legacy structured path if any are missing.
  //
  // Critical: we ALSO pass `tried_prompts` — the list of prompts that have
  // already failed in this session — so Scout doesn't recommend a refined
  // prompt the user just clicked through and bombed on. The LLM is instructed
  // to never suggest one of these. List is rolling, 24h, 30 most recent.
  const triggerScoutForNoResults = useCallback(
    (extra?: {
      parsedQuery?: any;
      retryLevel?: number;
      broadenedDimensions?: string[];
    }) => {
      let triedPrompts: string[] = [];
      try {
        const stored = JSON.parse(
          localStorage.getItem('ofl_tried_prompts') || '{}',
        ) as Record<string, number>;
        const cutoff = Date.now() - 24 * 60 * 60 * 1000;
        triedPrompts = Object.entries(stored)
          .filter(([, ts]) => ts >= cutoff)
          .sort((a, b) => b[1] - a[1])
          .slice(0, 30)
          .map(([p]) => p);
      } catch {
        // Non-fatal — localStorage may be disabled.
      }
      openPanelWithSearchHelp({
        searchType: 'contact',
        failedSearchParams: {
          prompt: searchPrompt.trim(),
          parsed_query: extra?.parsedQuery,
          retry_level_used: extra?.retryLevel,
          broadened_dimensions: extra?.broadenedDimensions,
          tried_prompts: triedPrompts,
        },
        errorType: 'no_results',
      });
    },
    [openPanelWithSearchHelp, searchPrompt],
  );

  // Helper functions
  const stripUndefined = <T extends Record<string, any>>(obj: T) =>
    Object.fromEntries(Object.entries(obj).filter(([, v]) => v !== undefined)) as T;

  // Memoize getUserProfileData to avoid recreating on every render
  const getUserProfileData = useCallback(async () => {
    if (!user) return null;
    try {
      if (user?.uid) {
        const professionalInfo = await firebaseApi.getProfessionalInfo(user.uid);
        if (professionalInfo) {
          return {
            name: `${professionalInfo.firstName || ""} ${professionalInfo.lastName || ""}`.trim() || user.name || "",
            university: professionalInfo.university || "",
            major: professionalInfo.fieldOfStudy || "",
            year: professionalInfo.graduationYear || "",
            graduationYear: professionalInfo.graduationYear || "",
            degree: professionalInfo.currentDegree || "",
            careerInterests: professionalInfo.targetIndustries || [],
          };
        }
      }
      const professionalInfo = localStorage.getItem("professionalInfo");
      const resumeData = localStorage.getItem("resumeData");
      const prof = professionalInfo ? JSON.parse(professionalInfo) : {};
      const resume = resumeData ? JSON.parse(resumeData) : {};
      return {
        name: `${(prof.firstName || "")} ${(prof.lastName || "")}`.trim() || resume.name || user.name || "",
        university: prof.university || resume.university || "",
        major: prof.fieldOfStudy || resume.major || "",
        year: prof.graduationYear || resume.year || "",
        graduationYear: prof.graduationYear || resume.year || "",
        degree: prof.currentDegree || resume.degree || "",
        careerInterests: prof.targetIndustries || [],
      };
    } catch {
      return null;
    }
  }, [user]);

  const autoSaveToDirectory = async (contacts: any[], searchLocation?: string) => {
    if (!user) return;
    try {
      const today = new Date().toLocaleDateString('en-US');
      const mapped: Omit<ContactApi, 'id'>[] = contacts.map((c: any) => {
        const derivedLocation = [c.City ?? '', c.State ?? ''].filter(Boolean).join(', ') || c.location || searchLocation || '';
        const mappedContact = stripUndefined({
          firstName: c.FirstName ?? c.firstName ?? '',
          lastName: c.LastName ?? c.lastName ?? '',
          linkedinUrl: c.LinkedIn ?? c.linkedinUrl ?? '',
          email: c.Email ?? c.email ?? '',
          company: c.Company ?? c.company ?? '',
          jobTitle: c.Title ?? c.jobTitle ?? '',
          college: c.College ?? c.college ?? '',
          location: derivedLocation,
          firstContactDate: today,
          status: 'Not Contacted',
          lastContactDate: today,
          emailSubject: c.email_subject ?? c.emailSubject ?? undefined,
          emailBody: c.email_body ?? c.emailBody ?? undefined,
          gmailThreadId: c.gmailThreadId ?? c.gmail_thread_id ?? undefined,
          gmailMessageId: c.gmailMessageId ?? c.gmail_message_id ?? undefined,
          gmailDraftId: c.gmailDraftId ?? c.gmail_draft_id ?? undefined,
          gmailDraftUrl: c.gmailDraftUrl ?? c.gmail_draft_url ?? undefined,
          hasUnreadReply: false,
          notificationsMuted: false,
          warmthScore: c.warmth_score ?? undefined,
          warmthTier: c.warmth_tier ?? undefined,
          warmthSignals: c.warmth_signals ?? undefined,
          personalizationLabel: c.personalization?.label ?? undefined,
          personalizationType: c.personalization?.commonality_type ?? undefined,
          briefing: c.briefing ?? undefined,
        });

        // DEBUG: Log first mapped contact to see email fields
        if (contacts.indexOf(c) === 0) {
          console.log('[DEBUG] autoSaveToDirectory - Original contact:', {
            emailSubject: c.emailSubject || c.email_subject || 'MISSING',
            emailBody: c.emailBody || c.email_body ? `${(c.emailBody || c.email_body).substring(0, 100)}...` : 'MISSING',
            allKeys: Object.keys(c).filter(k => k.toLowerCase().includes('email')),
          });
          console.log('[DEBUG] autoSaveToDirectory - Mapped contact:', {
            emailSubject: mappedContact.emailSubject || 'MISSING',
            emailBody: mappedContact.emailBody ? `${mappedContact.emailBody.substring(0, 100)}...` : 'MISSING',
          });
        }

        return mappedContact;
      });
      await firebaseApi.bulkCreateContacts(user.uid, mapped);
    } catch (error) {
      const isDev = import.meta.env.DEV;
      if (isDev) console.error('Error in autoSaveToDirectory:', error);
      throw error;
    }
  };

  const checkNeedsGmailConnection = async (): Promise<boolean> => {
    try {
      if (!user) return false;
      const data = await apiService.gmailStatus();
      return !data.connected;
    } catch (error) {
      const isDev = import.meta.env.DEV;
      if (isDev) console.error("Error checking Gmail status:", error);
      return false; // Don't show banner if we can't determine status
    }
  };

  const initiateGmailOAuth = async () => {
    try {
      const authUrl = await apiService.startGmailOAuth(window.location.pathname);
      if (authUrl) {
        window.location.href = authUrl;
      }
    } catch (error) {
      if (import.meta.env.DEV) console.error("Error initiating Gmail OAuth:", error);
    }
  };


  // Check Gmail status on mount
  useEffect(() => {
    const checkGmailStatus = async () => {
      if (!user) return;
      try {
        const connected = await checkNeedsGmailConnection();
        setGmailConnected(!connected);
      } catch {
        setGmailConnected(false);
      }
    };
    checkGmailStatus();
  }, [user]);

  // Check for fit context on mount (from Scout)
  useEffect(() => {
    try {
      const storedContext = localStorage.getItem('scout_fit_context');
      if (storedContext) {
        const fitContext = JSON.parse(storedContext);
        setCurrentFitContext(fitContext);
      }
    } catch (e) {
      // Ignore parse errors
    }
  }, []);

  // Scout job title suggestion handler (sets prompt from role/company/location)
  const handleJobTitleSuggestion = (title: string, company?: string, location?: string) => {
    const parts = [title];
    if (company) parts.push(`at ${company}`);
    if (location) parts.push(`in ${location}`);
    setSearchPrompt(parts.join(' '));
  };

  // Clear fit context when user manually edits search (handled on explicit Clear or after search)
  useEffect(() => {
    // Fit context will be cleared after email generation completes or on Clear
  }, [searchPrompt]);

  // Load saved resume from Firestore
  const loadSavedResume = useCallback(async () => {
    if (!user?.uid || isDevPreview) return;
    try {
      const userRef = doc(db, 'users', user.uid);
      const snap = await getDoc(userRef);
      if (snap.exists()) {
        const data = snap.data();
        const resumeUrl = data.resumeUrl || null;
        const resumeFileName = data.resumeFileName || null;
        setSavedResumeUrl(resumeUrl);
        setSavedResumeFileName(resumeFileName);
        // If we have a saved resume, we can optionally load it as uploadedFile for search
        // But we'll keep uploadedFile separate for the current search session
      }
    } catch (error) {
      console.error('Failed to load saved resume:', error);
    }
  }, [user?.uid]);

  // Load resume on mount
  useEffect(() => {
    loadSavedResume();
  }, [loadSavedResume]);

  // Save resume to account settings (Firestore)
  const saveResumeToAccountSettings = async (file: File) => {
    if (!user?.uid) {
      throw new Error('User not authenticated');
    }

    setIsUploadingResume(true);
    try {
      // 1) Parse resume via backend
      const formData = new FormData();
      formData.append('resume', file);

      const API_URL = BACKEND_URL;

      const token = auth.currentUser ? await auth.currentUser.getIdToken() : null;

      const response = await fetch(`${API_URL}/api/parse-resume`, {
        method: 'POST',
        headers: token ? { 'Authorization': `Bearer ${token}` } : {},
        body: formData,
      });

      const result = await response.json();
      if (!response.ok) {
        throw new Error(result.error || 'Failed to parse resume');
      }

      // 2) Upload the PDF to Firebase Storage
      const ts = Date.now();
      const storagePath = `resumes/${user.uid}/${ts}-${file.name}`;
      const storageRef = ref(storage, storagePath);
      await uploadBytes(storageRef, file);

      // 3) Get download URL and write to Firestore
      const downloadUrl = await getDownloadURL(storageRef);
      const userRef = doc(db, 'users', user.uid);
      await updateDoc(userRef, {
        resumeUrl: downloadUrl,
        resumeFileName: file.name,
        resumeUpdatedAt: new Date().toISOString(),
        resumeParsed: {
          name: result.data.name || '',
          university: result.data.university || '',
          major: result.data.major || '',
          year: result.data.year || '',
        },
      });

      // 4) Update local state
      setSavedResumeUrl(downloadUrl);
      setSavedResumeFileName(file.name);
      setUploadedFile(file);
      // Refresh the cached has-resume flag so setup nudges stop immediately.
      invalidateResumeStatusCache();

      toast({
        title: "Resume saved",
        description: "Your resume has been uploaded and saved to your account.",
      });
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : 'Failed to save resume';
      toast({
        title: "Upload failed",
        description: errorMessage,
        variant: "destructive",
      });
      throw error;
    } finally {
      setIsUploadingResume(false);
    }
  };

  // File upload handler (click)
  const handleFileUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;

    if (!isValidResumeFile(file)) {
      toast({
        title: "Invalid file type",
        description: "Please upload a PDF, DOCX, or DOC file.",
        variant: "destructive",
      });
      return;
    }

    if (file.size > 10 * 1024 * 1024) {
      toast({
        title: "File Too Large",
        description: "Please upload a file smaller than 10MB.",
        variant: "destructive",
      });
      return;
    }

    try {
      await saveResumeToAccountSettings(file);
    } catch (error) {
      // Error already handled in saveResumeToAccountSettings
    }

    // Reset input
    event.target.value = '';
  };


  // LinkedIn Import handler
  const handleLinkedInImport = async (urlOverride?: string) => {
    const rawUrl = urlOverride || linkedInUrl;
    if (!rawUrl.trim()) return;

    // Normalize LinkedIn URL (accepts URLs with or without protocol)
    let normalizedUrl = rawUrl.trim();

    // If it doesn't start with http, try to normalize it
    if (!normalizedUrl.startsWith('http://') && !normalizedUrl.startsWith('https://')) {
      // If it starts with linkedin.com or www.linkedin.com, add https://
      if (normalizedUrl.startsWith('linkedin.com') || normalizedUrl.startsWith('www.linkedin.com')) {
        normalizedUrl = `https://${normalizedUrl}`;
      } else if (normalizedUrl.startsWith('/in/')) {
        // If it's just a path like /in/username, add the full domain
        normalizedUrl = `https://linkedin.com${normalizedUrl}`;
      } else if (normalizedUrl.includes('linkedin') && normalizedUrl.includes('/in/')) {
        // Extract the /in/username part and rebuild
        const match = normalizedUrl.match(/\/in\/[^\/\s]+/);
        if (match) {
          normalizedUrl = `https://linkedin.com${match[0]}`;
        }
      } else {
        // Otherwise, assume it's just a username and add the full path
        normalizedUrl = `https://linkedin.com/in/${normalizedUrl}`;
      }
    }

    // Validate the normalized URL format
    const linkedInRegex = /^https?:\/\/(www\.)?linkedin\.com\/in\/[\w-]+\/?$/;
    if (!linkedInRegex.test(normalizedUrl)) {
      setLinkedInError('Please enter a valid LinkedIn profile URL (e.g., https://linkedin.com/in/username)');
      return;
    }

    if (!user?.uid) {
      setLinkedInError('Please sign in to import contacts');
      return;
    }

    setLinkedInLoading(true);
    setLinkedInError(null);
    setLinkedInSuccess(null);
    setLinkedInLastDraftUrl(null);

    try {
      // Get user resume text from Firestore
      let userResumeText = '';
      try {
        const userRef = doc(db, 'users', user.uid);
        const snap = await getDoc(userRef);
        if (snap.exists()) {
          const data = snap.data();
          if (data.resumeText) {
            userResumeText = data.resumeText;
          } else if (data.resumeParsed) {
            userResumeText = JSON.stringify(data.resumeParsed);
          }
        }
      } catch (error) {
        console.error('Failed to load resume:', error);
      }

      // Get Firebase auth token
      const firebaseUser = auth.currentUser;
      if (!firebaseUser) {
        throw new Error('User not authenticated');
      }
      const idToken = await firebaseUser.getIdToken();

      const API_BASE = BACKEND_URL;

      const response = await fetch(`${API_BASE}/api/contacts/import-linkedin`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${idToken}`,
        },
        body: JSON.stringify({
          linkedin_url: normalizedUrl, // Use normalized URL
          user_id: user.uid,
          user_resume: userResumeText,
          // Preview only: find the contact + email, then let the user draft/send
          // from the result card — exactly like a normal Find People search.
          create_draft: false,
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.message || data.error || 'Failed to import contact');
      }

      if (data.status === 'ok') {
        const c = data.contact || {};
        // Build a card in the same shape the Find People results render, then
        // route it through the unified results display (lastResults) so the
        // imported person gets the same card + action buttons (Open Gmail
        // drafts / Tracker / View in Spreadsheet) as a normal search.
        const cardContact = {
          firstName: c.firstName || '',
          lastName: c.lastName || '',
          FirstName: c.firstName || '',
          LastName: c.lastName || '',
          Title: c.jobTitle || '',
          jobTitle: c.jobTitle || '',
          Company: c.company || '',
          company: c.company || '',
          Email: c.email || '',
          email: c.email || '',
          LinkedIn: c.linkedinUrl || c.linkedin_url || normalizedUrl,
          linkedinUrl: c.linkedinUrl || c.linkedin_url || normalizedUrl,
          emailSubject: c.emailSubject || '',
          emailBody: c.emailBody || '',
          gmailDraftUrl: c.gmailDraftUrl || data.gmail_draft_url || '',
          warmth_label: c.warmth_label || '',
          warmth_tier: c.warmth_tier || '',
          warmth_signals: c.warmth_signals || [],
        };

        setLinkedInSuccess(null);        // don't show the old success banner
        setLinkedInLastDraftUrl(null);
        setLastResults([cardContact]);
        setAlreadySavedResults([]);
        setResultMessage('');
        setSearchSuggestions([]);
        setSearchBroadened(false);
        setSearchComplete(true);
        setLinkedInUrl('');               // clear the input field
        setSearchPrompt('');

        // Update credits if provided
        if (data.credits_remaining !== undefined && updateCredits) {
          await updateCredits(data.credits_remaining);
        }

        // Preview import: the card is found and ready. The toast just confirms
        // the find; drafting happens from the result card's Draft/Send buttons,
        // mirroring a normal search.
        const emailFound = data.email_found !== false; // Default to true if not specified

        const toastDescription = emailFound
          ? `${c.full_name || 'Contact'} found — draft or send when you're ready.`
          : `${c.full_name || 'Contact'} found, but no email address was available — you can add one manually later.`;

        toast({
          title: "Contact found",
          description: toastDescription,
        });
      } else {
        setLinkedInError(data.message || 'Failed to import contact');
      }
    } catch (error: any) {
      console.error('LinkedIn import error:', error);
      setLinkedInError(error.message || 'An error occurred while importing. Please try again.');
    } finally {
      setLinkedInLoading(false);
    }
  };

  // Search handler (prompt-based, single flow for all tiers)
  const handleSearch = async () => {
    // Hard short-circuit while the tour's demo is active on this surface.
    // Catches every trigger path — form submit, button click, the
    // pendingAutoSearch chip path — with a single guard. No API call fires.
    if (peopleDemoActive) return;

    // If the user typed nothing but the filter rail has active filters, synthesize
    // a prompt from them so the search (and the validation below) still has text
    // to work with — this is the rail-only search path (nonce-triggered re-search
    // or a first search driven entirely by rail edits).
    const railActive = railFilters && peopleFiltersActive(railFilters);
    let effectivePrompt = searchPrompt.trim();
    if (!effectivePrompt && railActive) {
      const f = railFilters!;
      effectivePrompt = [
        f.titles[0] ? f.titles.join(" or ") : "People",
        f.companies.length ? `at ${f.companies.join(" or ")}` : "",
        f.locations.length ? `in ${f.locations.join(" or ")}` : "",
        f.schools.length ? `who went to ${f.schools.join(" or ")}` : "",
        f.industries.length ? `in ${f.industries.join(" / ")}` : "",
      ].filter(Boolean).join(" ");
    }

    if (!effectivePrompt) {
      toast({
        title: "Enter a search",
        description: "Describe who you want to connect with (e.g. role, company, location).",
        variant: "destructive",
      });
      return;
    }

    if (!user) {
      toast({
        title: "Authentication Required",
        description: "Please sign in to search for contacts.",
        variant: "destructive",
      });
      navigate("/signin");
      return;
    }

    // Attach the rail's filters as explicit overrides only when this run was
    // triggered by a rail edit (or a re-search of the exact same effective
    // prompt) — a freshly typed/changed prompt hands control back to the
    // parser instead.
    const sendOverrides = !!railActive && railFiltersNonce > 0 && effectivePrompt === lastSearchedPromptRef.current;
    lastSearchedPromptRef.current = effectivePrompt;

    setIsSearching(true);
    setProgressValue(10);
    setSearchComplete(false);
    // Rotate the recommendation seed so users who run multiple searches get
    // different sub-rail suggestions each time (anchors stay stable).
    setRotationSeed((s) => s + 1);
    setResultMessage("");
    setSearchSuggestions([]);
    setSearchBroadened(false);

    let progressInterval: NodeJS.Timeout | null = null;
    let searchSucceeded = false;
    const startProgressSimulation = () => {
      let currentProgress = 10;
      progressInterval = setInterval(() => {
        if (currentProgress < 85) {
          currentProgress += Math.random() * 5 + 2;
          currentProgress = Math.min(currentProgress, 85);
          setProgressValue(Math.floor(currentProgress));
        }
      }, 500);
    };

    try {
      startProgressSimulation();

      const [userProfile, currentCredits] = await Promise.all([
        getUserProfileData(),
        checkCredits ? checkCredits() : Promise.resolve(effectiveUser.credits ?? 0)
      ]);
      setProgressValue(20);

      if (currentCredits < 5) {
        if (progressInterval) clearInterval(progressInterval);
        setIsSearching(false);
        setProgressValue(0);
        toast({
          title: "Insufficient Credits",
          description: `You have ${currentCredits} credits. You need at least 10 credits to search.`,
          variant: "destructive",
        });
        return;
      }

      setProgressValue(40);
      // Expand school acronyms before sending to PDL so "USC" → "University of Southern
      // California". The frontend chip continues displaying the short label; only the
      // backend payload is rewritten so PDL's school matcher hits the right institution.
      const expandedPrompt = expandQueryForBackend(effectivePrompt);
      // Search always runs in preview mode now: it returns contacts only, fast.
      // Drafting/sending happens afterward via the result action buttons.
      const result = await apiService.runPromptSearch({
        prompt: expandedPrompt, batchSize, emailTemplate: activeEmailTemplate, mode: 'preview',
        ...(sendOverrides ? { filters: railFilters } : {}),
      });

      if (!isSearchResult(result)) {
        if (progressInterval) clearInterval(progressInterval);
        setIsSearching(false);
        setProgressValue(0);
        const errMsg = (result as any)?.error || "Please try again.";
        if ((result as any)?.error?.toLowerCase().includes("insufficient")) {
          if (checkCredits) await checkCredits();
        }
        trackError('contact_search', 'search', 'api_error', errMsg);
        toast({
          title: "Search Failed",
          description: errMsg,
          variant: "destructive",
        });
        return;
      }

      if (progressInterval) clearInterval(progressInterval);
      setProgressValue(90);

      const creditsUsed = (result as any)?.credits_used ?? result.contacts.length * 5;
      if ((result as any)?.credits_remaining !== undefined && updateCredits) {
        await updateCredits((result as any).credits_remaining).catch(() => {});
      } else if (updateCredits) {
        const newCredits = Math.max(0, currentCredits - creditsUsed);
        await updateCredits(newCredits).catch(() => {});
      }
      setProgressValue(100);
      const alreadySavedFromServer = (result as any)?.already_saved_contacts || [];
      const backendMessage = (result as any)?.message || "";
      const broadenedDims: string[] = Array.isArray((result as any)?.broadened_dimensions)
        ? (result as any).broadened_dimensions
        : [];
      setLastResults(result.contacts);
      setAlreadySavedResults(alreadySavedFromServer);
      setResultMessage(backendMessage);
      setUnsupportedFilters(
        Array.isArray((result as any)?.unsupported_filters)
          ? (result as any).unsupported_filters
          : (result as any)?.adjacency_metadata?.unsupported_filters || [],
      );

      // Tell Scout's panel the search it kicked off finished, so the chat can
      // post its "Found N contacts" follow-up (scoutBridge contract). Names
      // ride along so the chat cites who was found, not just a count.
      // Harmless no-op when the panel is closed or the search was
      // user-initiated.
      try {
        const names = (result.contacts || []).slice(0, 4).map((c: any) => {
          const first = c.FirstName || c.firstName || '';
          const last = c.LastName || c.lastName || '';
          const company = c.Company || c.company || '';
          const full = `${first} ${last}`.trim() || 'Unnamed contact';
          return company ? `${full} (${company})` : full;
        });
        window.dispatchEvent(
          new CustomEvent(SCOUT_SEARCH_COMPLETED_EVENT, {
            detail: { count: result.contacts?.length || 0, route: '/find', names },
          }),
        );
      } catch {
        // non-fatal
      }

      // Append every completed search to a rolling localStorage list so Scout's
      // chat can reference "you searched for X yesterday and got 5 results"
      // across reloads / new tabs / new devices. Capped at 30 entries; older
      // ones FIFO-evict.
      try {
        const trimmedPrompt = searchPrompt.trim();
        if (trimmedPrompt) {
          const prior = JSON.parse(
            localStorage.getItem('ofl_recent_searches') || '[]',
          ) as Array<{ prompt: string; results: number; ts: number }>;
          const arr = Array.isArray(prior) ? prior : [];
          arr.unshift({
            prompt: trimmedPrompt,
            results: result.contacts?.length || 0,
            ts: Date.now(),
          });
          // De-dup by prompt — keep the most recent occurrence only.
          const seen = new Set<string>();
          const deduped = arr.filter((e) => {
            const k = e.prompt.toLowerCase();
            if (seen.has(k)) return false;
            seen.add(k);
            return true;
          });
          localStorage.setItem(
            'ofl_recent_searches',
            JSON.stringify(deduped.slice(0, 30)),
          );
        }
      } catch {
        // Non-fatal — localStorage may be disabled.
      }

      // Track empty-result (school, company) pairs in localStorage so the
      // right rail can deprioritize firms we already know are thin for the
      // user's school (e.g. Bocconi × Morgan Stanley). We only persist when
      // the retry chain bottomed out at level >= 4 — meaning even after
      // dropping the company filter we still found no new alumni in the
      // role family. That's the strongest signal the firm × school combo
      // is unreachable via PDL.
      try {
        const retryLevel = Number((result as any)?.retry_level_used ?? 0);
        const newCountAfter = result.contacts?.length || 0;
        const parsedFromServer = (result as any)?.parsed_query || {};
        // Report the server's parse back to the rail so it reflects what was
        // actually searched. parsed_query.companies is a list of
        // { name, matched_titles } objects, not plain strings — unwrap to names.
        onParsedQuery?.({
          titles: parsedFromServer.title_variations ?? [],
          companies: (parsedFromServer.companies ?? []).map((c: any) => typeof c === "string" ? c : c?.name).filter(Boolean),
          locations: parsedFromServer.locations ?? [],
          schools: parsedFromServer.schools ?? [],
          industries: parsedFromServer.industries ?? [],
        });
        const companies: any[] = parsedFromServer?.companies || [];
        const firstCompanyName: string =
          (companies?.[0]?.name as string | undefined)?.trim().toLowerCase() || '';
        // School is taken from the user's profile (the rail uses the same).
        const userSchool: string =
          (profileFacts?.schoolFull || profileFacts?.schoolShort || '')
            .trim()
            .toLowerCase();
        if (
          newCountAfter === 0 &&
          retryLevel >= 4 &&
          userSchool &&
          firstCompanyName
        ) {
          const key = `${userSchool}|${firstCompanyName}`;
          const existing = JSON.parse(
            localStorage.getItem('ofl_thin_pairs') || '{}',
          ) as Record<string, number>;
          existing[key] = Date.now();
          const entries = Object.entries(existing).sort((a, b) => b[1] - a[1]);
          const trimmed = Object.fromEntries(entries.slice(0, 100));
          localStorage.setItem('ofl_thin_pairs', JSON.stringify(trimmed));
        }
      } catch {
        // Non-fatal — localStorage may be disabled in private browsing.
      }
      setSearchSuggestions((result as any)?.suggestions || []);
      setSearchBroadened((result as any)?.search_broadened || false);

      // Open Scout with refined-prompt suggestions in two cases:
      //   - genuine zero results (nothing to surface)
      //   - all matches already saved AND the chain had to broaden (retry >= 2,
      //     i.e. title/industry got dropped) — that's the "I want fresh contacts
      //     and the system gave me what it could" moment where suggestions
      //     genuinely help.
      const retryLevelFromServer = Number((result as any)?.retry_level_used ?? 0);
      const allHitsAlreadySaved =
        result.contacts.length === 0 && alreadySavedFromServer.length > 0;
      const isZeroResults =
        result.contacts.length === 0 && alreadySavedFromServer.length === 0;
      const shouldOfferRefinement =
        isZeroResults || (allHitsAlreadySaved && retryLevelFromServer >= 2);

      // Stamp the prompt as "tried and failed" before opening Scout, so the
      // very same suggestion can't bounce back at the user on the next pass.
      // Persists to localStorage with a 24h TTL; trimmed to 30 entries.
      if (isZeroResults) {
        try {
          const promptKey = searchPrompt.trim().toLowerCase();
          if (promptKey) {
            const existing = JSON.parse(
              localStorage.getItem('ofl_tried_prompts') || '{}',
            ) as Record<string, number>;
            existing[promptKey] = Date.now();
            const entries = Object.entries(existing).sort((a, b) => b[1] - a[1]);
            const trimmed = Object.fromEntries(entries.slice(0, 30));
            localStorage.setItem('ofl_tried_prompts', JSON.stringify(trimmed));
          }
        } catch {
          // Non-fatal — localStorage may be disabled.
        }
      }

      if (shouldOfferRefinement) {
        triggerScoutForNoResults({
          parsedQuery: (result as any)?.parsed_query,
          retryLevel: retryLevelFromServer,
          broadenedDimensions: broadenedDims,
        });
        setSearchComplete(true);
      }

      trackFeatureActionCompleted('contact_search', 'search', true, {
        results_count: result.contacts.length,
        credits_spent: creditsUsed,
      });

      if (user?.uid && result.contacts.length > 0) {
        try {
          const summary = generateContactSearchSummary({
            jobTitle: searchPrompt.trim().slice(0, 80),
            contactCount: result.contacts.length,
          });
          await logActivity(user.uid, 'contactSearch', summary, {
            prompt: searchPrompt.trim(),
            contactCount: result.contacts.length,
            tier: userTier,
          });
        } catch (error) {
          const isDev = import.meta.env.DEV;
          if (isDev) console.error('Failed to log contact search activity:', error);
        }
      }

      // Backend already saves contacts to Firestore (runs.py save loop) — no frontend save needed

      setLastSearchStats({
        successful_drafts: 0,
        total_contacts: result.contacts.length + ((result as any)?.already_saved_contacts?.length || 0),
      });
      setSearchComplete(true);
      searchSucceeded = true;

      const savedCount = alreadySavedFromServer.length;
      const newCount = result.contacts.length;
      if (newCount === 0 && savedCount === 0) {
        // Genuine no-results — Scout panel already opened above; a short toast
        // confirms the outcome without claiming "Contacts Found!".
        toast({
          title: "No matching contacts found",
          description: backendMessage || "Try broadening your search.",
          duration: 5000,
        });
      } else if (newCount === 0 && savedCount > 0) {
        // All matches already saved. Use the backend's exact message so the UI
        // copy matches what the API contract promises (and gives the user a
        // specific next step: open them in the network or broaden the search).
        toast({
          title: "Already in your inbox",
          description: backendMessage
            || `All ${savedCount} matching contact(s) are already in your inbox. Open them in your network, or broaden your search to find new people.`,
          duration: 7000,
        });
      } else if ((result as any)?.mode === "send") {
        // Send mode: confirm what went out and what got routed to drafts by
        // the backend guardrails (quality gate + daily cap).
        const sentCount = Number((result as any)?.successful_sends ?? 0);
        const draftedFallback = Number((result as any)?.successful_drafts ?? 0);
        const qualityBlocked = Number((result as any)?.send_blocked_by_quality ?? 0);
        const capBlocked = Number((result as any)?.send_blocked_by_daily_cap ?? 0);
        const fallbackBits: string[] = [];
        if (qualityBlocked > 0) {
          fallbackBits.push(
            `${qualityBlocked} needed your eyes (now in Gmail drafts)`
          );
        }
        if (capBlocked > 0) {
          fallbackBits.push(
            `${capBlocked} hit your daily send cap (now in Gmail drafts)`
          );
        }
        const fallbackLine = fallbackBits.length > 0
          ? ` ${fallbackBits.join(", ")}.`
          : draftedFallback > 0
            ? ` ${draftedFallback} routed to Gmail drafts.`
            : "";
        toast({
          title: sentCount > 0 ? "Emails sent" : draftedFallback > 0 ? "Routed to drafts" : "Contacts Found!",
          description: sentCount > 0
            ? `Sent ${sentCount} ${sentCount === 1 ? "email" : "emails"}.${fallbackLine} Track replies in your Inbox.`
            : draftedFallback > 0
              ? `No sends went out.${fallbackLine} Review and send from Gmail.`
              : `Found ${newCount} contacts, but no emails could be sent. Check your Gmail connection.`,
          duration: 7000,
        });
      } else {
        toast({
          title: "Contacts Found!",
          description: savedCount > 0
            ? `Found ${newCount + savedCount} contacts (${newCount} new, ${savedCount} already saved) — view in your Inbox.`
            : `Found ${newCount} contacts — view them in your Inbox to start outreach.`,
          duration: 5000,
        });
      }
    } catch (error: any) {
      // Clear progress interval on error
      if (progressInterval) clearInterval(progressInterval);
      const isDev = import.meta.env.DEV;
      if (isDev) console.error("Search failed:", error);
      if (error?.needsAuth || error?.require_reauth) {
        const authUrl = error.authUrl;
        // Preserve contacts so user doesn't lose search results (drafts failed but contacts are returned)
        if (error.contacts?.length) {
          setLastResults(error.contacts);
        }
        if (authUrl) {
          toast({
            title: "Gmail Connection Expired",
            description: error.message || "Please reconnect your Gmail account to create drafts.",
            variant: "destructive",
            duration: 5000,
            action: (
              <Button size="sm" variant="outline" onClick={() => { window.location.href = authUrl; }}>
                Reconnect Gmail
              </Button>
            ),
          });
          window.location.href = authUrl;
          return;
        }
        toast({
          title: "Gmail Connection Expired",
          description: error.message || "Please reconnect your Gmail account to create drafts.",
          variant: "destructive",
          duration: 8000,
          action: (
            <Button size="sm" variant="outline" onClick={() => navigate("/account-settings")}>
              Reconnect Gmail
            </Button>
          ),
        });
        setSearchComplete(false);
        setProgressValue(0);
        return;
      }
      if (error?.code === "PDL_OUTAGE") {
        toast({
          title: "Temporarily Unavailable",
          description: "Find features are temporarily down due to a data provider update. We expect full service within 1–2 days.",
          duration: 8000,
        });
      } else {
        toast({
          title: "Search Failed",
          description: error instanceof Error ? error.message : "Please try again.",
          variant: "destructive",
          duration: 5000,
        });
      }
      setSearchComplete(false);
      setProgressValue(0);
    } finally {
      // Ensure progress interval is cleared
      if (progressInterval) clearInterval(progressInterval);
      setIsSearching(false);
      if (searchSucceeded) {
        setTimeout(() => {
          setProgressValue(0);
          setSearchComplete(false);
        }, 2000);
      } else {
        setTimeout(() => setProgressValue(0), 500);
      }
    }
  };

  // Unified submit handler — routes to LinkedIn import or search
  const handleSubmit = () => {
    const input = searchPrompt.trim();
    if (!input) return;
    if (isLinkedInUrl(input)) {
      const normalized = normalizeLinkedInUrl(input);
      setLinkedInUrl(normalized);
      handleLinkedInImport(normalized);
    } else {
      handleSearch();
    }
  };

  // CSV Export function for Contact Search Results (currently unused)
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const handleExportCsv = () => {
    if (!lastResults || lastResults.length === 0) {
      return;
    }

    // Define CSV headers based on Contact interface
    const headers = [
      'First Name',
      'Last Name',
      'Email',
      'Work Email',
      'Personal Email',
      'LinkedIn',
      'Job Title',
      'Company',
      'City',
      'State',
      'College',
      'Phone',
      'Email Subject',
      'Email Body'
    ] as const;

    const headerRow = headers.join(',');

    // Map contacts to CSV rows
    const rows = lastResults.map((contact: any) => {
      const escapeCsv = (val: string | undefined | null) => {
        if (!val) return '';
        const str = String(val);
        if (str.includes(',') || str.includes('"') || str.includes('\n')) {
          return `"${str.replace(/"/g, '""')}"`;
        }
        return str;
      };

      return [
        escapeCsv(contact.FirstName || contact.firstName),
        escapeCsv(contact.LastName || contact.lastName),
        escapeCsv(contact.Email || contact.email),
        escapeCsv(contact.WorkEmail || contact.workEmail),
        escapeCsv(contact.PersonalEmail || contact.personalEmail),
        escapeCsv(contact.LinkedIn || contact.linkedinUrl),
        escapeCsv(contact.Title || contact.jobTitle),
        escapeCsv(contact.Company || contact.company),
        escapeCsv(contact.City || contact.city),
        escapeCsv(contact.State || contact.state),
        escapeCsv(contact.College || contact.college),
        escapeCsv(contact.Phone || contact.phone),
        escapeCsv(contact.email_subject || contact.emailSubject),
        escapeCsv(contact.email_body || contact.emailBody)
      ].join(',');
    });

    const csvContent = [headerRow, ...rows].join('\n');
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    const url = URL.createObjectURL(blob);

    link.setAttribute('href', url);
    link.setAttribute('download', `contacts_${new Date().toISOString().split('T')[0]}.csv`);
    link.style.visibility = 'hidden';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);

    toast({
      title: "CSV Exported!",
      description: `Exported ${lastResults.length} contacts to CSV.`,
    });
  };

  // --- Embedded content (rendered inside FindPage wrapper) ---
  const embeddedContent = (
    <>
      {setupNudge && (
        <SetupNudgeModal
          open
          variant={setupNudge.variant}
          onClose={() => setSetupNudge(null)}
          onContinue={setupNudge.onContinue}
        />
      )}

      {/* Gmail hint — subtle inline text, not a warning banner */}
      {gmailConnected === false && !gmailBannerDismissed && (
        <div style={{ maxWidth: '860px', margin: '0 auto', padding: '8px 32px 0', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
          <span style={{ fontSize: 12, color: 'var(--ink-3, #8A8F9A)' }}>
            <button
              type="button"
              onClick={initiateGmailOAuth}
              style={{ background: 'none', border: 'none', cursor: 'pointer', fontSize: 12, color: 'var(--ink-2, #4A4F5B)', textDecoration: 'underline', textUnderlineOffset: 2, fontFamily: 'inherit' }}
            >
              Connect Gmail
            </button>
            {' '}to auto-create email drafts
          </span>
          <button
            type="button"
            aria-label="Dismiss"
            onClick={dismissGmailBanner}
            style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--ink-3, #8A8F9A)', fontSize: 14, lineHeight: 1, padding: 2 }}
          >
            &times;
          </button>
        </div>
      )}

      <div
        data-tour="tour-search-form"
        style={{ padding: '24px 32px 32px', maxWidth: '860px', margin: '0 auto' }}
      >
        <input
          type="file"
          accept={ACCEPTED_RESUME_TYPES.accept}
          onChange={handleFileUpload}
          className="hidden"
          id="resume-upload"
          disabled={isSearching || isUploadingResume}
        />

        {/* Progress Bar */}
        {isSearching && (
          <div style={{ marginBottom: 16 }}>
            <Progress value={progressValue} className="h-0.5 rounded-none" />
          </div>
        )}

        {/* Targeted context banner */}
        {currentFitContext && currentFitContext.job_title && (
          <div
            style={{
              marginBottom: 16,
              background: '#FAFBFF',
              border: '0.5px solid #E2E8F0',
              borderRadius: 3,
              padding: '10px 14px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
            }}
          >
            <div className="flex items-center gap-3">
              <span className="text-sm text-[#6B7280]">
                Targeting <span className="font-medium text-[#0F172A]">{currentFitContext.job_title}</span> at {currentFitContext.company || 'target companies'}
              </span>
            </div>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                localStorage.removeItem('scout_fit_context');
                setCurrentFitContext(null);
              }}
              className="h-7 text-[#94A3B8] hover:text-[#0F172A]"
            >
              Clear
            </Button>
          </div>
        )}

        {/* Batch send confirm. Fires from the Send Emails result-action button.
            Send is irreversible, so it routes through this hard confirm. */}
        <SendConfirmDialog
          open={showSendAllConfirm}
          count={lastResults.length}
          loading={sendingAll}
          onCancel={() => setShowSendAllConfirm(false)}
          onConfirm={() => {
            setShowSendAllConfirm(false);
            handleSendAll(true);
          }}
        />

        {/* Single-draft confirm. Fires when the inline Send button on a reviewed
            draft is clicked. Same hard-confirm pattern as the batch dialog
            because send is irreversible. */}
        <SendConfirmDialog
          open={confirmSendDraftIdx !== null}
          count={1}
          loading={sendingDraftIdx !== null}
          title={
            confirmSendDraftIdx !== null
              ? `Send this email to ${
                  ((lastResults[confirmSendDraftIdx]?.FirstName || lastResults[confirmSendDraftIdx]?.firstName || '') as string).trim() ||
                  'this contact'
                }?`
              : undefined
          }
          description="This sends the reviewed draft from your Gmail account. This cannot be undone."
          confirmLabel={sendingDraftIdx !== null ? 'Sending...' : 'Send now'}
          onCancel={() => setConfirmSendDraftIdx(null)}
          onConfirm={() => {
            if (confirmSendDraftIdx !== null) {
              handleSendReviewedDraft(confirmSendDraftIdx);
            }
          }}
        />

        {/* Hero search bar — full-width prompt. Company Alternatives sidebar is
            absolutely positioned in the empty space to the right of the form (outside
            the prompt bubble) so the prompt keeps its full width for typing. */}
        <div style={{ marginTop: 0, marginBottom: 16, position: 'relative' }}>
            <SearchPromptBox
              onSubmit={handleSubmit}
              submitDisabled={isSearching || linkedInLoading || !user}
              inputValue={searchPrompt}
              submitAriaLabel={isLinkedInUrl(searchPrompt) ? "Import from LinkedIn" : "Search"}
              helper={PEOPLE_SEARCH_HELPER_PREVIEW}
              submitIcon={
                isSearching || linkedInLoading ? <Loader2 className="w-4 h-4 animate-spin" />
                : isLinkedInUrl(searchPrompt) ? <Linkedin className="w-4 h-4" />
                : <ArrowUp className="w-4 h-4" />
              }
              footer={
                <>
              {/* Role-variations pill row — Grammarly-style, sits directly below the
                  typed sentence. Hover any pill to preview the swap, click to commit. */}
              <div style={{ paddingLeft: 26, marginTop: 0 }}>
                <RoleVariations
                  prompt={searchPrompt}
                  isSearching={isSearching}
                  hasResults={hasResults}
                  isLinkedIn={isLinkedInUrl(searchPrompt)}
                  inputFocused={inputFocused}
                  onAcceptSuggestion={(originalMatched, chosen) => {
                    const escaped = originalMatched.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                    const regex = new RegExp(`\\b${escaped}\\b`, 'i');
                    setSearchPrompt((prev) => prev.replace(regex, chosen.toLowerCase()));
                    setPreviewSwap(null);
                  }}
                  onPreviewSuggestion={(originalMatched, chosen) =>
                    setPreviewSwap({ matched: originalMatched, chosen })
                  }
                  onClearPreview={() => setPreviewSwap(null)}
                />
              </div>

              {/* Row 2 — bottom of the prompt box. When the user has focus-but-empty,
                  this slot shows the click-to-fill quick-starters (mental starting line).
                  When the user has typed something, dimension chips take over. */}
              <div style={{ paddingLeft: 26 }}>
                {!searchPrompt.trim() ? (
                  <StarterChips
                    visible
                    categories={PEOPLE_TEMPLATE_CATEGORIES}
                    disabled={isSearching || linkedInLoading}
                    onPickPlain={(seed) => {
                      // Treat the click as if the user had typed the seed verbatim:
                      // fill the prompt, place caret at end, keep focus on the input,
                      // and let the existing pipeline (DimensionChips, RoleVariations,
                      // CompanyAlternatives, ghost completion) re-derive everything.
                      setSearchPrompt(seed);
                      requestAnimationFrame(() => {
                        const el = promptInputRef.current;
                        if (el) {
                          el.focus();
                          try {
                            el.setSelectionRange(seed.length, seed.length);
                          } catch {
                            // Safari may throw on hidden/transparent inputs, non-fatal.
                          }
                        }
                      });
                    }}
                    onPickTemplate={(pattern) => {
                      // Insert the fill-in pattern and select the first bracketed
                      // placeholder so typing replaces it; Tab jumps to the next one
                      // (handled in the textarea keydown).
                      setSearchPrompt(pattern);
                      requestAnimationFrame(() => {
                        const el = promptInputRef.current;
                        if (!el) return;
                        el.focus();
                        const start = pattern.indexOf('[');
                        const end = start >= 0 ? pattern.indexOf(']', start) : -1;
                        try {
                          if (start >= 0 && end > start) el.setSelectionRange(start, end + 1);
                          else el.setSelectionRange(pattern.length, pattern.length);
                        } catch {
                          // Safari may throw on hidden/transparent inputs, non-fatal.
                        }
                      });
                    }}
                    schoolShort={profileFacts.schoolShort}
                    schoolFull={profileFacts.schoolFull}
                    targetFirms={profileFacts.targetFirms}
                    targetIndustries={profileFacts.targetIndustries}
                    preferredLocations={profileFacts.preferredLocations}
                    extractedRoles={profileFacts.extractedRoles}
                  />
                ) : (
                  <DimensionChips
                    prompt={searchPrompt}
                    isSearching={isSearching}
                    hasResults={hasResults}
                    isLinkedIn={isLinkedInUrl(searchPrompt)}
                    inputFocused={inputFocused}
                  />
                )}
              </div>

              {/* Card footer: count slider, live cost, and the visible primary submit.
                  Hidden for LinkedIn URLs (import mode has no batch size). */}
              {!isLinkedInUrl(searchPrompt) && (
                <div style={{
                  display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 14,
                  marginTop: 16, paddingTop: 14, borderTop: '1px solid var(--line, #E5E5E0)',
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 12, flex: '1 1 240px', minWidth: 220 }}>
                    <span style={{ fontSize: 11, color: '#8A8F97', minWidth: 12 }}>1</span>
                    <div className="slider-input-wrapper" style={{ flex: 1, position: 'relative', height: 4, background: '#E5E3DE', borderRadius: 2 }}>
                      <div style={{
                        position: 'absolute', left: 0, top: 0, height: 4,
                        width: maxBatchSize > 1 ? `${((batchSize - 1) / (maxBatchSize - 1)) * 100}%` : '0%',
                        background: sliderFlashing ? 'var(--brand-blue, #3B82F6)' : 'var(--accent, #1B2A44)',
                        borderRadius: 2,
                        transition: 'background .35s ease',
                      }} />
                      <input
                        type="range"
                        min={1}
                        max={maxBatchSize}
                        step={1}
                        value={batchSize}
                        onChange={(e) => {
                          userAdjustedBatchSize.current = true;
                          setBatchSize(Math.min(Number(e.target.value), maxBatchSize));
                          flashSlider();
                        }}
                        disabled={isSearching}
                        className="slider-custom"
                        aria-label="Number of contacts to find"
                        style={{
                          position: 'absolute', inset: 0, width: '100%', height: '100%',
                          opacity: 0, cursor: 'pointer', margin: 0,
                        }}
                      />
                      <div style={{
                        position: 'absolute',
                        left: `calc(${maxBatchSize > 1 ? ((batchSize - 1) / (maxBatchSize - 1)) * 100 : 0}% - 7px)`,
                        top: -5, width: 14, height: 14, borderRadius: '50%',
                        background: sliderFlashing ? 'var(--brand-blue, #3B82F6)' : 'var(--accent, #1B2A44)',
                        boxShadow: sliderFlashing
                          ? '0 1px 6px rgba(59,130,246,0.55)'
                          : '0 1px 4px rgba(27,42,68,0.4)',
                        pointerEvents: 'none',
                        transition: 'background .35s ease, box-shadow .35s ease',
                      }} />
                    </div>
                    <span style={{ fontSize: 11, color: '#8A8F97', minWidth: 16, textAlign: 'right' }}>{maxBatchSize}</span>
                  </div>
                  <div style={{
                    fontFamily: "var(--serif, 'Instrument Serif', Georgia, serif)",
                    fontStyle: 'italic', fontSize: 13.5, color: '#111418', whiteSpace: 'nowrap',
                  }}>
                    {batchSize} contact{batchSize !== 1 ? 's' : ''}
                  </div>
                  <div style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 11, color: '#4A4F57' }}>
                    <span style={{
                      display: 'inline-flex', padding: '3px 8px',
                      background: '#FAFAF8', border: '1px solid #E5E3DE', borderRadius: 4,
                      fontFamily: "'JetBrains Mono', monospace", fontSize: 10, color: '#111418',
                    }}>
                      {batchSize * CREDIT_COSTS.find_contact} credits
                    </span>
                    <span style={{ color: '#8A8F97' }}>
                      of {creditsView.balance.toLocaleString()}
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={handleSubmit}
                    disabled={isSearching || linkedInLoading || !user}
                    style={{
                      marginLeft: 'auto',
                      background: 'var(--accent, #4A60A8)', color: '#fff', border: 'none',
                      borderRadius: 10, padding: '9px 18px', fontSize: 13.5, fontWeight: 600,
                      fontFamily: 'inherit', whiteSpace: 'nowrap',
                      cursor: (isSearching || linkedInLoading || !user) ? 'not-allowed' : 'pointer',
                      opacity: (isSearching || linkedInLoading || !user) ? 0.75 : 1,
                      transition: 'background .15s ease, opacity .15s ease',
                    }}
                    onMouseEnter={(e) => {
                      e.currentTarget.style.background = 'var(--brand-blue, #3B82F6)';
                    }}
                    onMouseLeave={(e) => {
                      e.currentTarget.style.background = 'var(--accent, #4A60A8)';
                    }}
                  >
                    {isSearching ? 'Finding...' : 'Find people'}
                  </button>
                </div>
              )}
                </>
              }
            >
              {/* Row 1 — search icon + input + LinkedIn pill */}
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
              <div style={{ flex: 1, position: 'relative', paddingRight: 40 }}>
                {/* Pseudo-element styling for the transparent input:
                    - ::placeholder — keep it visible despite color:transparent on the host
                    - ::selection — keep selected text transparent (the visible rendering
                      lives in the overlay above; without this rule, the input's selected
                      text re-emerges and double-renders against the overlay). */}
                <style>{`.ofl-search-input::placeholder{color:var(--ink-3, #8A8F9A);opacity:1;}.ofl-search-input::selection{color:transparent;background:rgba(74,96,168,0.20);}.ofl-search-input::-moz-selection{color:transparent;background:rgba(74,96,168,0.20);}`}</style>
                <textarea
                  ref={promptInputRef}
                  className="ofl-search-input"
                  spellCheck={false}
                  autoCorrect="off"
                  autoCapitalize="off"
                  autoComplete="off"
                  rows={1}
                  value={searchPrompt}
                  onChange={(e) => {
                    const next = e.target.value;
                    setSearchPrompt(next);
                    setLinkedInError(null);
                    setLinkedInSuccess(null);
                    // Auto-grow vertically as the user types — keeps long queries readable.
                    const el = e.currentTarget;
                    el.style.height = 'auto';
                    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
                  }}
                  onKeyDown={(e) => {
                    // Template chips insert bracketed [placeholders]; while any remain,
                    // Tab cycles the selection through them (takes priority over the
                    // ghost-completion Tab below).
                    if (e.key === 'Tab' && !e.shiftKey && /\[[^\]]*\]/.test(searchPrompt)) {
                      const el = e.currentTarget;
                      const from = el.selectionEnd ?? 0;
                      const re = /\[[^\]]*\]/g;
                      let m: RegExpExecArray | null;
                      let first: { start: number; end: number } | null = null;
                      let next: { start: number; end: number } | null = null;
                      while ((m = re.exec(searchPrompt))) {
                        const tok = { start: m.index, end: m.index + m[0].length };
                        if (!first) first = tok;
                        if (tok.start >= from && !next) next = tok;
                      }
                      const target = next ?? first;
                      if (target) {
                        e.preventDefault();
                        try {
                          el.setSelectionRange(target.start, target.end);
                        } catch {
                          // Safari may throw on hidden/transparent inputs, non-fatal.
                        }
                        return;
                      }
                    }
                    if (e.key === 'Tab' && ghostCompletion && !e.shiftKey) {
                      e.preventDefault();
                      setSearchPrompt((prev) => prev + ghostCompletion);
                      return;
                    }
                    if (
                      e.key === 'ArrowRight' &&
                      ghostCompletion &&
                      e.currentTarget.selectionStart === searchPrompt.length
                    ) {
                      e.preventDefault();
                      setSearchPrompt((prev) => prev + ghostCompletion);
                      return;
                    }
                    // Enter submits, Shift+Enter inserts a newline.
                    if (e.key === 'Enter' && !e.shiftKey) {
                      e.preventDefault();
                      handleSubmit();
                    }
                  }}
                  onFocus={() => setInputFocused(true)}
                  onBlur={() => setInputFocused(false)}
                  // Native placeholder is intentionally empty — the typewriter overlay below
                  // owns ALL placeholder rendering (focused or not), so we don't double-show.
                  placeholder={undefined}
                  disabled={isSearching || linkedInLoading || peopleDemoActive}
                  style={{
                    width: '100%',
                    border: 'none',
                    background: 'none',
                    fontSize: 14,
                    // Native textarea text is invisible. The visible text + ghost suffix render
                    // in the overlay below so they're guaranteed to share metrics and align.
                    color: 'transparent',
                    caretColor: '#0F172A',
                    outline: 'none',
                    fontFamily: 'inherit',
                    lineHeight: 1.5,
                    padding: 0,
                    resize: 'none',
                    overflow: 'hidden',
                    minHeight: 21, // matches one line at fontSize 14 × lineHeight 1.5
                  }}
                />
                {/* Typewriter placeholder overlay — types + deletes between examples in quotes */}
                {showAnimatedPlaceholder && (
                  <div
                    style={{
                      position: 'absolute',
                      top: 0,
                      left: 0,
                      right: 0,
                      pointerEvents: 'none',
                      fontSize: 14,
                      fontFamily: 'inherit',
                      lineHeight: 1.5,
                      color: 'var(--ink-3, #8A8F9A)',
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                    }}
                  >
                    e.g. {typedPlaceholder}
                    <span
                      aria-hidden
                      style={{
                        display: 'inline-block',
                        width: 1,
                        marginLeft: 2,
                        background: 'currentColor',
                        // Match the line-height so the caret tracks the text baseline.
                        height: '1em',
                        verticalAlign: 'text-bottom',
                        animation: 'cgh-typewriter-caret 1s steps(2) infinite',
                      }}
                    />
                    <style>{`@keyframes cgh-typewriter-caret { 50% { opacity: 0; } }`}</style>
                  </div>
                )}
                {/* Combined text overlay — typed text in primary ink + optional ghost suffix
                    in faded ink. Both rendered in the same DOM node so they share font metrics
                    and align pixel-perfect with each other (no offset between typed and ghost). */}
                {searchPrompt && !showAnimatedPlaceholder && (
                  <div
                    style={{
                      position: 'absolute',
                      top: 0,
                      left: 0,
                      right: 0,
                      pointerEvents: 'none',
                      fontSize: 14,
                      fontFamily: 'inherit',
                      lineHeight: 1.5,
                      // pre-wrap so long prompts wrap to multiple lines instead of cutting off.
                      // The textarea grows in tandem; together they stay aligned pixel-perfect.
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-word',
                      overflow: 'hidden',
                    }}
                  >
                    {previewedPrompt ? (
                      // Hover-preview: render the prompt with the matched word swapped in
                      // a faded blue highlight so the user sees the result before clicking.
                      <>
                        <span style={{ color: '#0F172A' }}>{previewedPrompt.before}</span>
                        <span
                          style={{
                            color: 'var(--accent, #4A60A8)',
                            background: 'rgba(74,96,168,0.12)',
                            borderRadius: 3,
                            padding: '0 1px',
                          }}
                        >
                          {previewedPrompt.replacement}
                        </span>
                        <span style={{ color: '#0F172A' }}>{previewedPrompt.after}</span>
                      </>
                    ) : (
                      <>
                        <span style={{ color: '#0F172A' }}>{searchPrompt}</span>
                        {ghostCompletion && inputFocused && (
                          <span style={{ color: 'var(--ink-3, #8A8F9A)', opacity: 0.55 }}>
                            {ghostCompletion}
                          </span>
                        )}
                      </>
                    )}
                  </div>
                )}
              </div>
              {isLinkedInUrl(searchPrompt) && (
                <div style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 4,
                  background: 'rgba(74,96,168,0.10)',
                  color: 'var(--accent, #4A60A8)',
                  fontSize: 12,
                  fontWeight: 500,
                  padding: '4px 10px',
                  borderRadius: 100,
                  whiteSpace: 'nowrap',
                  marginTop: 2,
                }}>
                  <Linkedin className="h-3 w-3" />
                  LinkedIn
                </div>
              )}
              </div>
            </SearchPromptBox>

            {/* RIGHT — Company Alternatives, absolutely positioned in the empty space
                outside the form's max-width container. Doesn't affect the prompt's
                full-width layout. */}
            <div
              style={{
                position: 'absolute',
                top: 0,
                left: 'calc(100% + 20px)',
                width: 168,
              }}
            >
              <CompanyAlternatives
                prompt={searchPrompt}
                isSearching={isSearching}
                hasResults={hasResults}
                isLinkedIn={isLinkedInUrl(searchPrompt)}
                rotationSeed={rotationSeed}
                inputFocused={inputFocused}
                onAcceptSuggestion={(originalMatched, chosen) => {
                  const escaped = originalMatched.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                  const regex = new RegExp(`\\b${escaped}\\b`, 'i');
                  setSearchPrompt((prev) => prev.replace(regex, chosen.toLowerCase()));
                  setPreviewSwap(null);
                }}
                onPreviewSuggestion={(originalMatched, chosen) =>
                  setPreviewSwap({ matched: originalMatched, chosen })
                }
                onClearPreview={() => setPreviewSwap(null)}
                onAppendCompany={(company) => {
                  // No specific company in the prompt yet — append "at <company>"
                  // so the search picks up the user's intent.
                  setSearchPrompt((prev) => {
                    const trimmed = prev.replace(/\s+$/, '');
                    return /\bat\b/i.test(trimmed)
                      ? `${trimmed} ${company}`
                      : `${trimmed} at ${company}`;
                  });
                }}
                onAppendLocation={(loc) => {
                  setSearchPrompt((prev) => {
                    const trimmed = prev.replace(/\s+$/, '');
                    return /\bin\b/i.test(trimmed)
                      ? `${trimmed} ${loc}`
                      : `${trimmed} in ${loc}`;
                  });
                }}
              />
            </div>
        </div>

        {linkedInError && (
          <div className="p-3 bg-red-50 text-red-700 text-sm rounded-[3px] flex items-center gap-2 border border-red-200 mb-4">
            <AlertCircle className="w-4 h-4 flex-shrink-0" />
            {linkedInError}
          </div>
        )}

        {/* Outreach settings summary row; Edit expands the full setup card inline */}
        <div style={{ marginTop: 14, marginBottom: 4 }}>
          <div style={{
            display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 10,
            fontSize: 12.5, color: 'var(--ink-2, #4A4F5B)',
          }}>
            <span>
              Email template:{' '}
              <span style={{ fontWeight: 600, color: 'var(--ink, #111318)' }}>
                {getEmailTemplateLabel(activeEmailTemplate)}
              </span>
            </span>
            <span aria-hidden="true" style={{ color: 'var(--ink-3, #8A8F9A)' }}>&middot;</span>
            {savedResumeUrl ? (
              <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5 }}>
                <Check aria-hidden="true" className="text-green-600" style={{ width: 13, height: 13 }} />
                Resume attached
              </span>
            ) : (
              <button
                type="button"
                onClick={() => document.getElementById('resume-upload')?.click()}
                disabled={isSearching || isUploadingResume}
                style={{
                  display: 'inline-flex', alignItems: 'center', gap: 6,
                  padding: '5px 12px', border: '1.5px dashed #C9CDD6', borderRadius: 8,
                  background: 'var(--paper, #FFFFFF)', cursor: 'pointer', fontFamily: 'inherit',
                  fontSize: 12.5, fontWeight: 600, color: 'var(--ink, #111318)',
                  transition: 'all .15s',
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.borderColor = 'var(--brand-blue, #3B82F6)';
                  e.currentTarget.style.boxShadow = '0 0 0 3px rgba(59,130,246,0.08)';
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.borderColor = '#C9CDD6';
                  e.currentTarget.style.boxShadow = 'none';
                }}
              >
                <Upload style={{ width: 13, height: 13, color: 'var(--ink-2, #4A4F5B)' }} />
                {isUploadingResume ? 'Uploading...' : 'Attach resume'}
              </button>
            )}
            <span aria-hidden="true" style={{ color: 'var(--ink-3, #8A8F9A)' }}>&middot;</span>
            <button
              type="button"
              onClick={() => setOutreachSetupOpen((o) => !o)}
              aria-expanded={outreachSetupOpen}
              aria-controls="outreach-setup-panel"
              style={{
                fontSize: 12.5, fontWeight: 500, color: 'var(--accent, #4A60A8)',
                background: 'none', border: 'none', cursor: 'pointer', fontFamily: 'inherit',
                padding: '2px 4px', borderRadius: 6,
              }}
            >
              {outreachSetupOpen ? 'Done' : 'Edit'}
            </button>
            <button
              type="button"
              onClick={() => setShowImportDialog(true)}
              style={{
                fontSize: 12, color: 'var(--ink-2, #4A4F5B)',
                background: 'none', border: 'none', cursor: 'pointer', fontFamily: 'inherit',
                padding: '4px 6px', borderRadius: 6, transition: 'color .12s, background .12s',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.color = 'var(--accent, #4A60A8)';
                e.currentTarget.style.background = 'rgba(74,96,168,0.06)';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.color = 'var(--ink-2, #4A4F5B)';
                e.currentTarget.style.background = 'transparent';
              }}
            >
              Import contacts
            </button>
          </div>

          {/* Expanded outreach setup card, announced via aria-expanded on Edit */}
          <AnimatePresence initial={false}>
            {outreachSetupOpen && (
              <motion.div
                id="outreach-setup-panel"
                key="outreach-setup"
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
                style={{ overflow: 'hidden' }}
              >
                <div style={{
                  background: 'var(--paper, #FFFFFF)',
                  border: '1px solid var(--line, #E5E5E0)',
                  borderRadius: 12,
                  padding: '12px 16px 14px',
                  marginTop: 10,
                }}>
            <div style={{
              fontFamily: '"Libre Baskerville", Georgia, serif',
              fontSize: 10, letterSpacing: '0.12em', color: '#8A8F97',
            }}>
              OUTREACH SETUP
            </div>
            <div style={{ fontSize: 11.5, color: 'var(--ink-3, #8A8F9A)', marginTop: 2 }}>
              Applied to every email drafted from this search.
            </div>
            <div style={{ display: 'flex', alignItems: 'stretch', flexWrap: 'wrap', gap: 10, marginTop: 10 }}>
          <TemplateButton
            template={activeEmailTemplate}
            onClick={() => navigate("/find/templates")}
          />
          {/* Resume control: reads as confirmed status when attached, flips to an
              upload CTA when empty. Both states open the same hidden file input. */}
          {savedResumeUrl ? (
            <button
              type="button"
              onClick={() => document.getElementById('resume-upload')?.click()}
              disabled={isSearching || isUploadingResume}
              title={savedResumeFileName || undefined}
              aria-label="Resume attached. Change resume"
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 10,
                padding: '9px 14px',
                border: '1px solid transparent',
                borderRadius: 10,
                background: 'var(--paper-2, #FAFBFF)',
                cursor: 'pointer',
                fontFamily: 'inherit',
                textAlign: 'left',
                transition: 'all .15s',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.background = '#F3F5FA';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = 'var(--paper-2, #FAFBFF)';
              }}
            >
              <Check aria-hidden="true" className="text-green-600" style={{ width: 16, height: 16, flexShrink: 0 }} />
              <span style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                <span style={{ fontSize: 10.5, color: 'var(--ink-3, #8A8F9A)', lineHeight: 1.2 }}>
                  Resume
                </span>
                <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--ink, #111318)', lineHeight: 1.25 }}>
                  {isUploadingResume ? 'Uploading...' : 'Attached'}
                </span>
              </span>
              <span style={{ marginLeft: 6, fontSize: 11.5, fontWeight: 500, color: 'var(--ink-2, #4A4F5B)' }}>
                Change
              </span>
            </button>
          ) : (
            <button
              type="button"
              onClick={() => document.getElementById('resume-upload')?.click()}
              disabled={isSearching || isUploadingResume}
              aria-label="Attach resume"
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 10,
                padding: '9px 14px',
                border: '1.5px dashed #C9CDD6',
                borderRadius: 10,
                background: 'var(--paper, #FFFFFF)',
                cursor: 'pointer',
                fontFamily: 'inherit',
                textAlign: 'left',
                transition: 'all .15s',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.borderColor = 'var(--brand-blue, #3B82F6)';
                e.currentTarget.style.boxShadow = '0 0 0 3px rgba(59,130,246,0.08)';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.borderColor = '#C9CDD6';
                e.currentTarget.style.boxShadow = 'none';
              }}
            >
              <Upload style={{ width: 16, height: 16, color: 'var(--ink-2, #4A4F5B)', flexShrink: 0 }} />
              <span style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--ink, #111318)', lineHeight: 1.25 }}>
                  {isUploadingResume ? 'Uploading...' : 'Attach resume'}
                </span>
                <span style={{ fontSize: 10.5, color: 'var(--ink-3, #8A8F9A)', lineHeight: 1.2 }}>
                  Referenced in drafted outreach
                </span>
              </span>
            </button>
          )}
            </div>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        {/* Resume upload card — shown when no results and no resume */}
        {!hasResults && !isSearching && !savedResumeUrl && (
          <div
            className="max-sm:flex-col max-sm:items-start"
            style={{
              marginTop: 20,
              background: '#FFFFFF',
              border: '1px solid var(--line, #E5E5E0)',
              borderRadius: 12,
              padding: '18px 20px',
              display: 'flex',
              alignItems: 'center',
              gap: 16,
            }}
          >
            <div style={{
              width: 44,
              height: 44,
              borderRadius: 10,
              background: 'var(--paper-2, #FAFBFF)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexShrink: 0,
            }}>
              <FileText style={{ width: 20, height: 20, color: 'var(--ink-2, #4A4F5B)' }} />
            </div>
            <div style={{ flex: 1 }}>
              <div style={{ fontSize: 14, fontWeight: 500, color: 'var(--ink, #111318)', marginBottom: 3 }}>
                Upload your resume for better matches
              </div>
              <div style={{ fontSize: 12, color: 'var(--ink-3, #8A8F9A)', lineHeight: 1.5 }}>
                We'll find people who hired for roles like yours and tailor your outreach automatically.
              </div>
            </div>
            <button
              className="max-sm:w-full"
              onClick={() => document.getElementById('resume-upload')?.click()}
              style={{
                background: 'var(--ink, #111318)',
                color: '#fff',
                border: 'none',
                borderRadius: 8,
                padding: '9px 16px',
                fontSize: 13,
                fontWeight: 500,
                cursor: 'pointer',
                whiteSpace: 'nowrap',
                flexShrink: 0,
              }}
            >
              Attach resume
            </button>
          </div>
        )}

        {/* Results section */}
        {hasResults && !isSearching && !linkedInSuccess && (
          <div
            ref={searchSuccessRef}
            style={{
              marginTop: 24,
              paddingTop: 24,
              borderTop: '0.5px solid #EEF2F8',
            }}
          >
            {/* Success header — just the centered count pill. */}
            {(() => {
              const total = lastResults.length + alreadySavedResults.length;
              return (
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', textAlign: 'center', marginBottom: 18 }}>
                  {/* Count pill */}
                  <div style={{
                    display: 'inline-flex', alignItems: 'center', gap: 5,
                    padding: '5px 12px',
                    background: '#DCFCE7', color: '#15803D',
                    border: '0.5px solid #BBF7D0', borderRadius: 100,
                    fontSize: 12, fontWeight: 600,
                  }}>
                    <CheckCircle style={{ width: 13, height: 13 }} />
                    {total} {total === 1 ? 'person' : 'people'} found
                  </div>
                </div>
              );
            })()}

            {/* Outreach actions — act on ALL found contacts. Search just returns
                contacts; the user chooses to draft or send here. Locked buttons
                stay clickable so the click surfaces the upgrade/trial prompt. */}
            {lastResults.length > 0 && (() => {
              const canDraftTier = canUseOutreachMode(userTier, 'draft');
              const canSendTier = canUseOutreachMode(userTier, 'send');
              const busy = draftingAll || sendingAll;
              const allSent = lastResults.every((c: any) => c.emailSent);
              const draftShadow = '0 1px 2px rgba(74,96,168,0.18), 0 8px 20px rgba(74,96,168,0.26)';
              const draftShadowHover = '0 2px 4px rgba(74,96,168,0.22), 0 12px 26px rgba(74,96,168,0.34)';
              return (
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 12, marginTop: 4, marginBottom: 20 }}>
                  {/* Draft Emails — primary, dark-blue hero gradient */}
                  <div style={{ position: 'relative', display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={handleDraftAll}
                      style={{
                        display: 'inline-flex', alignItems: 'center', gap: 9,
                        padding: '13px 28px', borderRadius: 10,
                        background: 'var(--accent, #4A60A8)',
                        color: '#fff', border: 'none', fontSize: 15, fontWeight: 600,
                        cursor: busy ? 'default' : 'pointer', opacity: busy ? 0.75 : 1,
                        boxShadow: draftShadow,
                        transition: 'transform .12s, box-shadow .12s, opacity .12s',
                      }}
                      onMouseEnter={(e) => {
                        if (busy) return;
                        (e.currentTarget as HTMLButtonElement).style.transform = 'translateY(-1px)';
                        (e.currentTarget as HTMLButtonElement).style.boxShadow = draftShadowHover;
                      }}
                      onMouseLeave={(e) => {
                        (e.currentTarget as HTMLButtonElement).style.transform = 'none';
                        (e.currentTarget as HTMLButtonElement).style.boxShadow = draftShadow;
                      }}
                    >
                      {draftingAll
                        ? <Loader2 style={{ width: 17, height: 17 }} className="animate-spin" />
                        : <Mail style={{ width: 17, height: 17 }} />}
                      Draft {lastResults.length} {lastResults.length === 1 ? 'email' : 'emails'}
                      {!canDraftTier && <Lock style={{ width: 13, height: 13 }} />}
                    </button>

                    {/* Info tooltip — explains what Draft Emails does */}
                    <span
                      style={{ position: 'relative', display: 'inline-flex', alignItems: 'center' }}
                      onMouseEnter={() => setShowDraftInfo(true)}
                      onMouseLeave={() => setShowDraftInfo(false)}
                    >
                      <Info style={{ width: 18, height: 18, color: '#94A3B8', cursor: 'help' }} />
                      {showDraftInfo && (
                        <div style={{
                          position: 'absolute', bottom: 'calc(100% + 10px)', left: '50%', transform: 'translateX(-50%)',
                          width: 244, padding: '10px 13px',
                          background: '#0F172A', color: '#F8FAFC',
                          fontSize: 12.5, lineHeight: 1.45, fontWeight: 400,
                          borderRadius: 9, boxShadow: '0 10px 28px rgba(15,23,42,0.30)',
                          zIndex: 60, textAlign: 'left', pointerEvents: 'none',
                        }}>
                          Writes a personalized email to each person using your selected email template — Networking by default.
                          <span style={{
                            position: 'absolute', top: '100%', left: '50%', transform: 'translateX(-50%)',
                            width: 0, height: 0,
                            borderLeft: '6px solid transparent', borderRight: '6px solid transparent',
                            borderTop: '6px solid #0F172A',
                          }} />
                        </div>
                      )}
                    </span>
                  </div>

                  {/* Send Emails — bigger, secondary */}
                  <button
                    type="button"
                    disabled={busy || allSent}
                    onClick={() => handleSendAll(false)}
                    style={{
                      display: 'inline-flex', alignItems: 'center', gap: 9,
                      padding: '13px 28px', borderRadius: 10,
                      background: '#fff', color: '#0F172A',
                      border: '1px solid #E2E8F0', fontSize: 15, fontWeight: 600,
                      cursor: (busy || allSent) ? 'default' : 'pointer',
                      opacity: (busy || allSent) ? 0.6 : 1,
                      boxShadow: '0 1px 2px rgba(15,37,69,0.05)',
                      transition: 'all .12s',
                    }}
                  >
                    {sendingAll
                      ? <Loader2 style={{ width: 17, height: 17 }} className="animate-spin" />
                      : <Send style={{ width: 17, height: 17 }} />}
                    {allSent ? 'Emails sent' : 'Send emails'}
                    {!canSendTier && <Lock style={{ width: 13, height: 13 }} />}
                  </button>
                </div>
              );
            })()}

            {/* Live progress — sits between the buttons and the results while
                drafting/sending, narrating the work with a rotating status line
                and a thin progress bar. */}
            {(draftingAll || sendingAll) && (() => {
              const steps = sendingAll ? SEND_PROGRESS_STEPS : DRAFT_PROGRESS_STEPS;
              const idx = Math.min(progressStep, steps.length - 1);
              return (
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 9, marginTop: -8, marginBottom: 20 }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, fontWeight: 500, color: '#334155' }}>
                    <Loader2 className="animate-spin" style={{ width: 14, height: 14, color: '#102E5C' }} />
                    <span>{steps[idx]}…</span>
                  </div>
                  <div style={{ width: 240, height: 3, background: '#E2E8F0', borderRadius: 100, overflow: 'hidden' }}>
                    <div style={{
                      width: `${((idx + 1) / steps.length) * 100}%`,
                      height: '100%',
                      background: 'linear-gradient(90deg, #102E5C, #2563EB)',
                      borderRadius: 100,
                      transition: 'width .7s ease',
                    }} />
                  </div>
                </div>
              );
            })()}

            {/* Backend message — persistent inline callout for "all already saved" or adjacency explanation.
                Shows when there are no new contacts to explain why and what to do next. */}
            {resultMessage && lastResults.length === 0 && (
              <div style={{
                padding: '10px 14px',
                background: alreadySavedResults.length > 0 ? 'rgba(245,158,11,0.06)' : 'rgba(74,96,168,0.05)',
                border: alreadySavedResults.length > 0 ? '0.5px solid #FDE68A' : '0.5px solid #BFDBFE',
                borderRadius: 6,
                fontSize: 13,
                color: alreadySavedResults.length > 0 ? '#713F12' : '#1E40AF',
                lineHeight: 1.5,
                marginBottom: 12,
              }}>
                {resultMessage}
              </div>
            )}

            {/* Broadening notice — shown when the backend retry chain dropped one
                or more constraints to find these contacts. Tells the user honestly
                that the system expanded its search rather than pretending the
                original specific query yielded these people. */}
            {unsupportedFilters.length > 0 && lastResults.length > 0 && (
              <div style={{
                padding: '10px 14px',
                background: 'rgba(245,158,11,0.06)',
                border: '0.5px solid #FDE68A',
                borderRadius: 6,
                fontSize: 13,
                color: '#713F12',
                lineHeight: 1.5,
                marginBottom: 12,
              }}>
                <strong style={{ fontWeight: 600 }}>
                  {unsupportedFilters.includes('school')
                    ? 'These people are not alumni matches.'
                    : 'These results ignore some of your filters.'}
                </strong>{' '}
                We couldn’t find anyone matching your{' '}
                {unsupportedFilters.join(' and ')} filter
                {unsupportedFilters.length > 1 ? 's' : ''}, so this is everyone
                we could find at the company instead. Check each person before
                you mention a shared school or city.
              </div>
            )}

            {/* Cross-tab suggestions (e.g. "Find recruiters at X") */}
            {searchSuggestions.length > 0 && lastResults.length === 0 && (
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
                {searchSuggestions.map((suggestion: any, idx: number) => (
                  <button
                    key={idx}
                    onClick={() => {
                      if (suggestion.type === 'switch_tab') {
                        navigate(`/find?tab=${suggestion.tab}${suggestion.prefill?.company ? `&company=${encodeURIComponent(suggestion.prefill.company)}` : ''}`);
                      } else if (suggestion.type === 'broaden_query' && suggestion.query) {
                        setSearchPrompt(suggestion.query);
                      }
                    }}
                    style={{
                      padding: '8px 14px',
                      background: '#fff',
                      border: '1px solid #E2E8F0',
                      borderRadius: 6,
                      fontSize: 13,
                      color: 'var(--accent, #4A60A8)',
                      cursor: 'pointer',
                      fontWeight: 500,
                      transition: 'all .12s',
                    }}
                  >
                    {suggestion.label} <ArrowRight style={{ width: 12, height: 12, display: 'inline', marginLeft: 4 }} />
                  </button>
                ))}
              </div>
            )}

            {/* Inbox-connect nudge — shown when drafts were delivered in fallback mode */}
            <InboxConnectNudge show={fallbackDelivery} />

            {/* Section label — these are the fresh people, ready for outreach */}
            {lastResults.length > 0 && (
              <div style={{
                fontSize: 11, fontWeight: 700, letterSpacing: '0.08em',
                color: 'var(--accent, #4A60A8)', textTransform: 'uppercase',
                marginBottom: 12,
              }}>
                New · Ready for outreach
              </div>
            )}

            {/* Contact cards */}
            {lastResults.map((c: any, i: number) => {
              const name = toTitleCase([c.FirstName || c.firstName, c.LastName || c.lastName].filter(Boolean).join(' ') || (c.Email || c.email || '').split('@')[0] || 'Unknown');
              const title = toTitleCase(c.JobTitle || c.jobTitle || c.Title || '');
              const company = toTitleCase(c.Company || c.company || '');
              const email = c.Email || c.email || '';
              const linkedin = c.LinkedIn || c.linkedinUrl || '';
              const initials = name.split(' ').map((n: string) => n[0]).slice(0, 2).join('').toUpperCase();
              // Per-contact email state derived from what the search returned,
              // not a mode flag. hasDraft and isSent drive the right-side action.
              const hasDraft = !!(c.emailSubject || c.emailBody);
              const isSent = !!c.emailSent;
              const isExpanded = expandedEmailIdx === i;
              const linkedinHref = linkedin
                ? (linkedin.startsWith('http') ? linkedin : `https://${linkedin}`)
                : '';
              // Each new person gets a saturated avatar so the list reads as a
              // row of distinct people, not a grey ledger.
              const avatarColors = ['#5965D8', '#8B5CF6', '#0D9488', '#D97706', '#DB2777', '#2563EB'];
              const avatarBg = avatarColors[i % avatarColors.length];
              const warmthLabel = c.warmth_label || (c.warmth_tier === 'warm' ? 'Strong match' : c.warmth_tier === 'neutral' ? 'Good fit' : '');
              const isStrongFit = warmthLabel === 'Strong fit' || warmthLabel === 'Strong match';
              const isRoleMismatch = warmthLabel === 'Right company, different role';
              return (
                <div
                  key={i}
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    padding: '16px 18px',
                    background: '#FFFFFF',
                    border: '1px solid #ECEEF3',
                    borderRadius: 16,
                    marginBottom: 10,
                    cursor: 'pointer',
                    boxShadow: '0 1px 2px rgba(15,23,42,0.04)',
                    transition: 'all .12s',
                  }}
                  onMouseEnter={(e) => {
                    (e.currentTarget as HTMLDivElement).style.borderColor = 'var(--accent, #4A60A8)';
                    (e.currentTarget as HTMLDivElement).style.boxShadow = '0 4px 14px rgba(74,96,168,.12)';
                  }}
                  onMouseLeave={(e) => {
                    (e.currentTarget as HTMLDivElement).style.borderColor = '#ECEEF3';
                    (e.currentTarget as HTMLDivElement).style.boxShadow = '0 1px 2px rgba(15,23,42,0.04)';
                  }}
                >
                  {/* Top row: a coloured avatar, the identity stack (name + fit
                      pill, a middot meta line, then a sparkle "why this person"
                      line), and a right rail with LinkedIn + the draft action. */}
                  <div style={{ display: 'flex', alignItems: 'flex-start', gap: 14 }}>
                    <div style={{
                      width: 42,
                      height: 42,
                      borderRadius: '50%',
                      background: avatarBg,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      fontSize: 13,
                      fontWeight: 600,
                      color: '#FFFFFF',
                      flexShrink: 0,
                    }}>
                      {initials}
                    </div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      {/* Name + fit pill share the first line */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: 9, minWidth: 0 }}>
                        <span style={{ fontSize: 15, fontWeight: 700, color: '#0F172A', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                          {name}
                        </span>
                        {warmthLabel && (
                          <span style={{
                            padding: '2px 9px',
                            borderRadius: 100,
                            background: isStrongFit ? 'rgba(34,197,94,0.12)' : isRoleMismatch ? 'rgba(148,163,184,0.18)' : 'rgba(245,158,11,0.14)',
                            color: isStrongFit ? '#15803D' : isRoleMismatch ? '#475569' : '#B45309',
                            fontSize: 11, fontWeight: 600, whiteSpace: 'nowrap', flexShrink: 0,
                          }}>
                            {warmthLabel}
                          </span>
                        )}
                      </div>
                      {/* Meta line: role · company · email, middot-separated */}
                      <div style={{ fontSize: 13, color: '#64748B', marginTop: 3, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {title}
                        {title && company && <span style={{ color: '#CBD5E1' }}> · </span>}
                        {company && <span style={{ color: '#334155', fontWeight: 600 }}>{company}</span>}
                        {email && (title || company) && <span style={{ color: '#CBD5E1' }}> · </span>}
                        {email && <span style={{ color: '#94A3B8' }}>{email}</span>}
                      </div>
                      {/* Sparkle "why this person" line */}
                      {c.briefing && (
                        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 8, minWidth: 0 }}>
                          <Sparkles style={{ width: 13, height: 13, color: 'var(--accent, #4A60A8)', flexShrink: 0 }} />
                          <span style={{ fontSize: 13, color: '#64748B', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                            {c.briefing}
                          </span>
                        </div>
                      )}
                    </div>

                    {/* Right rail: LinkedIn as a quiet icon button, then the one
                        coloured action — Draft when nothing exists yet, otherwise
                        View draft / View sent to open the message inline. */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0, alignSelf: 'center' }}>
                      {linkedinHref && (
                        <a
                          href={linkedinHref}
                          target="_blank"
                          rel="noopener noreferrer"
                          title="View LinkedIn profile"
                          aria-label="View LinkedIn profile"
                          onClick={(e: React.MouseEvent) => e.stopPropagation()}
                          style={{
                            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
                            width: 38, height: 38, borderRadius: 9,
                            background: '#fff', border: '1px solid #E2E8F0', color: '#0A66C2',
                            flexShrink: 0, transition: 'all .12s',
                          }}
                          onMouseEnter={(e) => { (e.currentTarget as HTMLAnchorElement).style.background = '#F1F5F9'; }}
                          onMouseLeave={(e) => { (e.currentTarget as HTMLAnchorElement).style.background = '#fff'; }}
                        >
                          <Linkedin style={{ width: 16, height: 16 }} />
                        </a>
                      )}
                      {isSent ? (
                        <button
                          type="button"
                          onClick={(e) => { e.stopPropagation(); setExpandedEmailIdx(isExpanded ? null : i); }}
                          style={{
                            display: 'inline-flex', alignItems: 'center', gap: 6,
                            height: 38, padding: '0 14px', borderRadius: 9,
                            background: '#fff', border: '1px solid #BBF7D0', color: '#15803D',
                            fontSize: 13.5, fontWeight: 600, cursor: 'pointer', fontFamily: 'inherit',
                            whiteSpace: 'nowrap', flexShrink: 0,
                          }}
                        >
                          <CheckCircle style={{ width: 15, height: 15 }} />
                          {isExpanded ? 'Hide' : 'View sent'}
                        </button>
                      ) : (
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            if (hasDraft) { setExpandedEmailIdx(isExpanded ? null : i); }
                            else { handleDraftAll(); }
                          }}
                          style={{
                            display: 'inline-flex', alignItems: 'center', gap: 6,
                            height: 38, padding: '0 16px', borderRadius: 9,
                            background: '#fff', border: '1px solid #C7CCF0', color: 'var(--accent, #4A60A8)',
                            fontSize: 13.5, fontWeight: 600,
                            cursor: (draftingAll || sendingAll) ? 'default' : 'pointer',
                            opacity: (draftingAll || sendingAll) && !hasDraft ? 0.6 : 1,
                            fontFamily: 'inherit', whiteSpace: 'nowrap', flexShrink: 0,
                            transition: 'all .12s',
                          }}
                          onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.background = '#F2F3FC'; }}
                          onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.background = '#fff'; }}
                        >
                          {hasDraft ? (
                            <>
                              {isExpanded ? 'Hide draft' : 'View draft'}
                              <ChevronRight style={{
                                width: 14, height: 14,
                                transform: isExpanded ? 'rotate(90deg)' : 'none',
                                transition: 'transform .15s',
                              }} />
                            </>
                          ) : (
                            <>
                              <Mail style={{ width: 15, height: 15 }} />
                              Draft
                            </>
                          )}
                        </button>
                      )}
                    </div>
                  </div>

                  {/* Expanded email panel: the card grows only when this is open */}
                  {isExpanded && (hasDraft || isSent) && (
                    <div style={{
                      marginTop: 10, padding: '12px 14px',
                      background: '#F8FAFC', borderRadius: 6, border: '0.5px solid #E2E8F0',
                    }}>
                      {c.emailSubject && (
                        <div style={{ fontSize: 13, fontWeight: 600, color: '#0F172A', marginBottom: 6 }}>
                          {c.emailSubject}
                        </div>
                      )}
                      {/* Sender context so the panel reads like a real email */}
                      <div style={{ fontSize: 11, color: '#94A3B8', marginBottom: 10, lineHeight: 1.5 }}>
                        {email && <div>To: {email}</div>}
                        {user?.email && <div>From: {user.email}</div>}
                      </div>
                      <div style={{ fontSize: 12, color: '#334155', lineHeight: 1.6, whiteSpace: 'pre-wrap' }}>
                        {c.emailBody}
                      </div>
                      <div style={{ display: 'flex', gap: 12, marginTop: 12, alignItems: 'center' }}>
                        {!isSent && (
                          <span onClick={(e: React.MouseEvent) => e.stopPropagation()}>
                            <DraftDeliveryActions
                              draft={{
                                to: email,
                                subject: c.emailSubject,
                                body: c.emailBody,
                                gmailUrl: c.gmailDraftUrl || undefined,
                                firstName: c.FirstName,
                                company: c.Company,
                              }}
                            />
                          </span>
                        )}
                        {!isSent && c.gmailDraftId && (
                          <ResultActionButton
                            variant="primary"
                            size="sm"
                            disabled={sendingDraftIdx === i}
                            onClick={(e: React.MouseEvent) => {
                              e.stopPropagation();
                              setConfirmSendDraftIdx(i);
                            }}
                            style={{ background: '#16A34A' }}
                          >
                            {sendingDraftIdx === i ? (
                              <>
                                <Loader2 style={{ width: 13, height: 13 }} className="animate-spin" /> Sending...
                              </>
                            ) : (
                              <>
                                <Send style={{ width: 13, height: 13 }} /> Send
                              </>
                            )}
                          </ResultActionButton>
                        )}
                        {isSent && (
                          <a
                            href="https://mail.google.com/mail/u/0/#sent"
                            target="_blank"
                            rel="noopener noreferrer"
                            onClick={(e) => e.stopPropagation()}
                            style={{
                              display: 'inline-flex', alignItems: 'center', gap: 5,
                              color: '#15803D', fontSize: 12, fontWeight: 600, textDecoration: 'none',
                            }}
                          >
                            <CheckCircle style={{ width: 12, height: 12 }} /> Sent, view in Gmail
                          </a>
                        )}
                        <ResultActionButton
                          variant="secondary"
                          size="sm"
                          onClick={(e) => {
                            e.stopPropagation();
                            if (c.demo) return;
                            const text = `Subject: ${c.emailSubject || ''}\n\n${c.emailBody || ''}`;
                            navigator.clipboard.writeText(text);
                            toast({ description: 'Email copied to clipboard' });
                          }}
                        >
                          <FileText style={{ width: 13, height: 13 }} /> Copy email
                        </ResultActionButton>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}

            {/* Already saved contacts */}
            {alreadySavedResults.length > 0 && (
              <>
                {/* Section label — people the search re-surfaced that are
                    already living in the inbox. Muted so the eye stays on New. */}
                <div style={{
                  fontSize: 11, fontWeight: 700, letterSpacing: '0.08em',
                  color: '#94A3B8', textTransform: 'uppercase',
                  marginTop: 22, marginBottom: 12,
                }}>
                  Already in your inbox · {alreadySavedResults.length}
                </div>
                {alreadySavedResults.map((c: any, i: number) => {
                  const name = toTitleCase([c.FirstName || c.firstName, c.LastName || c.lastName].filter(Boolean).join(' ') || (c.Email || c.email || '').split('@')[0] || 'Unknown');
                  const title = toTitleCase(c.Title || c.JobTitle || c.jobTitle || '');
                  const company = toTitleCase(c.Company || c.company || '');
                  const linkedin = c.LinkedIn || c.linkedinUrl || '';
                  const initials = name.split(' ').map((n: string) => n[0]).slice(0, 2).join('').toUpperCase();
                  const linkedinHref = linkedin ? (linkedin.startsWith('http') ? linkedin : `https://${linkedin}`) : '';
                  return (
                    <div
                      key={`saved-${i}`}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 14,
                        padding: '13px 18px',
                        background: '#F8FAFC',
                        border: '1px solid #ECEEF3',
                        borderRadius: 16,
                        marginBottom: 8,
                        opacity: 0.82,
                      }}
                    >
                      <div style={{
                        width: 42,
                        height: 42,
                        borderRadius: '50%',
                        background: 'rgba(148,163,184,0.18)',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        fontSize: 13,
                        fontWeight: 600,
                        color: '#94A3B8',
                        flexShrink: 0,
                      }}>
                        {initials}
                      </div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
                          <span style={{ fontSize: 15, fontWeight: 600, color: '#475569', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{name}</span>
                          <span style={{
                            padding: '2px 9px',
                            borderRadius: 100,
                            background: 'rgba(148,163,184,0.16)',
                            color: '#94A3B8',
                            fontSize: 11,
                            fontWeight: 600,
                            whiteSpace: 'nowrap',
                            flexShrink: 0,
                          }}>
                            Already saved
                          </span>
                        </div>
                        <div style={{ fontSize: 13, color: '#94A3B8', marginTop: 3, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                          {title}
                          {title && company && <span style={{ color: '#CBD5E1' }}> · </span>}
                          {company}
                        </div>
                      </div>
                      {linkedinHref && (
                        <a
                          href={linkedinHref}
                          target="_blank"
                          rel="noopener noreferrer"
                          onClick={(e: React.MouseEvent) => e.stopPropagation()}
                          style={{
                            display: 'inline-flex', alignItems: 'center', gap: 7,
                            height: 38, padding: '0 14px', borderRadius: 9,
                            background: '#fff', border: '1px solid #E2E8F0', color: '#334155',
                            fontSize: 13.5, fontWeight: 600, textDecoration: 'none',
                            whiteSpace: 'nowrap', flexShrink: 0, transition: 'all .12s',
                          }}
                          onMouseEnter={(e) => { (e.currentTarget as HTMLAnchorElement).style.background = '#F1F5F9'; }}
                          onMouseLeave={(e) => { (e.currentTarget as HTMLAnchorElement).style.background = '#fff'; }}
                        >
                          <Linkedin style={{ width: 16, height: 16, color: '#0A66C2' }} /> LinkedIn
                        </a>
                      )}
                    </div>
                  );
                })}
              </>
            )}

            {/* Where everything lives — two destination cards that close the
                loop: the full sortable table of everyone found, and the inbox
                where the drafted emails wait. Mirrors the mockup footer. */}
            {(() => {
              const cardBase: React.CSSProperties = {
                flex: 1,
                display: 'flex', alignItems: 'center', gap: 14,
                padding: '18px 20px', borderRadius: 16,
                cursor: 'pointer', textDecoration: 'none', textAlign: 'left',
                fontFamily: 'inherit', border: 'none',
                transition: 'transform .12s, box-shadow .12s, background .12s',
              };
              const iconBox = (bg: string, color: string): React.CSSProperties => ({
                width: 44, height: 44, borderRadius: 12, flexShrink: 0,
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                background: bg, color,
              });
              const totalContacts = lastResults.length + alreadySavedResults.length;
              const goToInbox = () => {
                // Deep-link the just-drafted contact so the tracker expands its
                // (collapsed-by-default) group, selects it, and refetches fresh.
                const drafted = lastResults.find(
                  (c: any) => (c.gmailDraftUrl || c.emailSubject || c.emailBody) && (c.Email || c.email)
                );
                const anyWithEmail = lastResults.find((c: any) => c.Email || c.email);
                const focusEmail =
                  (drafted && (drafted.Email || drafted.email)) ||
                  (anyWithEmail && (anyWithEmail.Email || anyWithEmail.email)) ||
                  undefined;
                navigate('/outbox', { state: { focusEmail, segment: 'people' } });
              };
              return (
                <div style={{ marginTop: 26 }}>
                  <div style={{
                    fontSize: 11, fontWeight: 700, letterSpacing: '0.08em',
                    color: '#94A3B8', textTransform: 'uppercase', marginBottom: 12,
                  }}>
                    Where everything lives
                  </div>
                  <div style={{ display: 'flex', gap: 12 }}>
                    {/* View in spreadsheet — primary, filled indigo */}
                    <button
                      type="button"
                      onClick={() => navigate('/contact-directory')}
                      style={{
                        ...cardBase,
                        background: 'var(--accent, #4A60A8)',
                        color: '#fff',
                        boxShadow: '0 1px 2px rgba(74,96,168,0.18), 0 8px 20px rgba(74,96,168,0.24)',
                      }}
                      onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.transform = 'translateY(-1px)'; }}
                      onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.transform = 'none'; }}
                    >
                      <span style={iconBox('rgba(255,255,255,0.18)', '#fff')}>
                        <Table2 style={{ width: 20, height: 20 }} />
                      </span>
                      <span style={{ flex: 1, minWidth: 0 }}>
                        <span style={{ display: 'block', fontSize: 16, fontWeight: 700 }}>View in spreadsheet</span>
                        <span style={{ display: 'block', fontSize: 13, color: 'rgba(255,255,255,0.82)', marginTop: 2 }}>
                          All {totalContacts} {totalContacts === 1 ? 'contact' : 'contacts'} in a sortable table
                        </span>
                      </span>
                      <ChevronRight style={{ width: 20, height: 20, color: 'rgba(255,255,255,0.85)', flexShrink: 0 }} />
                    </button>

                    {/* Open inbox — secondary, white */}
                    <button
                      type="button"
                      onClick={goToInbox}
                      style={{
                        ...cardBase,
                        background: '#fff', color: '#0F172A',
                        border: '1px solid #ECEEF3',
                        boxShadow: '0 1px 2px rgba(15,23,42,0.04)',
                      }}
                      onMouseEnter={(e) => {
                        (e.currentTarget as HTMLButtonElement).style.transform = 'translateY(-1px)';
                        (e.currentTarget as HTMLButtonElement).style.boxShadow = '0 4px 14px rgba(15,23,42,0.08)';
                      }}
                      onMouseLeave={(e) => {
                        (e.currentTarget as HTMLButtonElement).style.transform = 'none';
                        (e.currentTarget as HTMLButtonElement).style.boxShadow = '0 1px 2px rgba(15,23,42,0.04)';
                      }}
                    >
                      <span style={iconBox('rgba(74,96,168,0.10)', 'var(--accent, #4A60A8)')}>
                        <Inbox style={{ width: 20, height: 20 }} />
                      </span>
                      <span style={{ flex: 1, minWidth: 0 }}>
                        <span style={{ display: 'block', fontSize: 16, fontWeight: 700 }}>Open inbox</span>
                        <span style={{ display: 'block', fontSize: 13, color: '#64748B', marginTop: 2 }}>
                          Read &amp; manage your drafted emails
                        </span>
                      </span>
                      <ChevronRight style={{ width: 20, height: 20, color: '#CBD5E1', flexShrink: 0 }} />
                    </button>
                  </div>
                </div>
              );
            })()}
          </div>
        )}

        {/* LinkedIn success */}
        {linkedInSuccess && (
          <div style={{
            marginTop: 24,
            paddingTop: 24,
            borderTop: '0.5px solid #EEF2F8',
          }}>
            <div style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              marginBottom: 12,
            }}>
              <div style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 5,
                padding: '4px 10px',
                background: '#DCFCE7',
                color: '#15803D',
                border: '0.5px solid #BBF7D0',
                borderRadius: 100,
                fontSize: 11,
                fontWeight: 500,
              }}>
                <CheckCircle className="w-3 h-3" />
                {linkedInSuccess}
              </div>
            </div>
            <div style={{ display: 'flex', gap: 7 }}>
              {linkedInLastDraftUrl && (
                <button
                  onClick={() => window.open(linkedInLastDraftUrl!, '_blank')}
                  style={{
                    flex: 1,
                    height: 37,
                    borderRadius: 3,
                    background: 'var(--accent, #4A60A8)',
                    color: '#fff',
                    border: 'none',
                    fontSize: 12,
                    fontWeight: 500,
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: 5,
                    fontFamily: 'inherit',
                  }}
                >
                  <ExternalLink className="w-3 h-3" />
                  Open Gmail Draft
                </button>
              )}
              <button
                onClick={() => navigate('/contact-directory')}
                style={{
                  flex: 1,
                  height: 37,
                  borderRadius: 3,
                  background: '#fff',
                  border: '0.5px solid #E2E8F0',
                  color: '#6B7280',
                  fontSize: 12,
                  cursor: 'pointer',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  gap: 5,
                  fontFamily: 'inherit',
                }}
              >
                <User className="w-3 h-3" />
                View in Spreadsheet
              </button>
            </div>
          </div>
        )}
      </div>

      <EliteGateModal open={showEliteGate} onClose={() => setShowEliteGate(false)} />

      {/* Upgrade/trial prompt for locked outreach actions (Draft Outreach / Send Emails). */}
      <UpgradeModal
        open={upgradeGate !== null}
        onOpenChange={(o) => { if (!o) setUpgradeGate(null); }}
        feature={upgradeGate?.feature || ''}
        featureLabel={upgradeGate?.label}
        requiredTier={upgradeGate?.requiredTier}
        reason={upgradeGate?.reason}
        currentTier={userTier}
      />

      {/* Import CSV dialog */}
      {showImportDialog && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 50,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
          }}
        >
          <div
            style={{ position: 'absolute', inset: 0, background: 'rgba(0,0,0,.4)' }}
            onClick={() => setShowImportDialog(false)}
          />
          <div
            style={{
              position: 'relative',
              background: '#fff',
              borderRadius: 3,
              width: '100%',
              maxWidth: 600,
              maxHeight: '85vh',
              overflowY: 'auto',
              margin: 16,
              boxShadow: '0 4px 12px rgba(0,0,0,.08)',
            }}
          >
            <button
              type="button"
              onClick={() => setShowImportDialog(false)}
              style={{
                position: 'absolute',
                top: 12,
                right: 12,
                background: 'none',
                border: 'none',
                cursor: 'pointer',
                padding: 4,
                color: '#94A3B8',
                zIndex: 1,
              }}
            >
              <X className="h-4 w-4" />
            </button>
            <div style={{ padding: '24px' }}>
              <ContactImport
                onImportComplete={() => setShowImportDialog(false)}
              />
            </div>
          </div>
        </div>
      )}
    </>
  );

  if (embedded) return embeddedContent;

  // --- Standalone page with full shell ---
  return (
    <>
    <SidebarProvider>
      <div className="flex min-h-screen w-full bg-[#FFFFFF] text-foreground font-sans">
        <AppSidebar />

        <MainContentWrapper>
          <AppHeader
            title=""
            onJobTitleSuggestion={handleJobTitleSuggestion}
            rightContent={
              <>
                <button
                  onClick={() => navigate("/find/templates")}
                  className="hidden md:flex items-center gap-1 text-xs text-[#6B7280] hover:text-[#0F172A] cursor-pointer transition-colors"
                >
                  Template:&nbsp;<span className="font-medium text-[#0F172A]">
                    {getEmailTemplateLabel(activeEmailTemplate)}
                  </span>
                </button>
                <Button
                  type="button"
                  onClick={() => isElite ? navigate("/find/templates") : setShowEliteGate(true)}
                  className="h-8 px-4 rounded-[3px] bg-[var(--accent, #4A60A8)] hover:bg-[var(--primary-600, #4C62A8)] text-white text-sm font-medium transition-all"
                  data-tour="tour-templates-button"
                >
                  <FileText className="h-4 w-4" />
                  <span className="hidden sm:inline whitespace-nowrap ml-2">
                    Email Template
                  </span>
                </Button>
              </>
            }
          />

          <main style={{ background: '#FFFFFF', flex: 1, overflowY: 'auto' }} className="px-4 py-8 pb-24 sm:px-8 sm:py-12 sm:pb-24">
            {/* Header */}
            <div className="w-full mb-8" style={{ maxWidth: '720px', margin: '0 auto' }}>
              <h1 className="text-2xl sm:text-3xl font-semibold tracking-tight text-[#0F172A] mb-2" style={{ fontFamily: "'Lora', Georgia, serif" }}>
                {(activeTab === 'contact-search' || activeTab === 'import') && 'Find People'}
                {activeTab === 'contact-library' && 'Your Contacts'}
                {!['contact-search', 'import', 'contact-library'].includes(activeTab) && 'Find'}
              </h1>
              <p className="text-sm text-[#6B7280] leading-relaxed">
                {(activeTab === 'contact-search' || activeTab === 'import') && 'Search by name, role, or company — or paste a LinkedIn URL.'}
                {activeTab === 'contact-library' && 'Everyone you find lands here. Track status, view emails, and export.'}
                {!['contact-search', 'import', 'contact-library'].includes(activeTab) && 'Discover professionals at your target companies.'}
              </p>
            </div>

            {embeddedContent}
          </main>
        </MainContentWrapper>
      </div>
    </SidebarProvider>
    </>
  );

};

export default ContactSearchPage;