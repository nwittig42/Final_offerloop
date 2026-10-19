import { useState, useMemo, useCallback, useEffect, useRef } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AppSidebar } from "@/components/AppSidebar";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppHeader } from "@/components/AppHeader";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { useTour } from "@/contexts/TourContext";
import { useToast } from "@/hooks/use-toast";
import { apiService } from "@/services/api";
import { firebaseApi } from "@/services/firebaseApi";
import {
  outboxThreadToProto,
  groupedByStage,
  protoStageToBackend,
  type ProtoStage,
  type ProtoSegment,
  type ProtoContact,
} from "@/pages/trackerAdapter";
import { ProtoHeader } from "@/components/tracker/redesign/ProtoHeader";
import { ProtoToolbar } from "@/components/tracker/redesign/ProtoToolbar";
import { SegmentTabs } from "@/components/tracker/redesign/SegmentTabs";
import { ContactListAccordion } from "@/components/tracker/redesign/ContactListAccordion";
import { ProtoDetailHeader } from "@/components/tracker/redesign/ProtoDetailHeader";
import { ProtoPipelineDots } from "@/components/tracker/redesign/ProtoPipelineDots";
import { ProtoEmailBlock } from "@/components/tracker/redesign/ProtoEmailBlock";
import type { OutboxThread, ThreadMessage, ThreadMessagesResponse } from "@/services/api";
import { ProtoSpreadsheet, type SpreadsheetSort, type SpreadsheetSortKey } from "@/components/tracker/redesign/ProtoSpreadsheet";
import { SetupNudgeModal } from "@/components/gates/SetupNudgeModal";
import { useResumeStatus } from "@/hooks/useResumeStatus";
import { FILTER_LABELS, type SortKey } from "@/components/tracker/redesign/MoreFiltersDropdown";
import "./NetworkTrackerRedesign.css";

// Write paths live here:
//   - stageMutation (real backend write — see below)
//   - toggleBookmark(id) — flips an in-memory Set, lost on refresh
//   - toggleRow(id) — same for spreadsheet row checkboxes
//   - stubAction(label) — remaining placeholders (Save Draft / Send via Gmail
//     / Import CSV / Export / Edit Template / Draft email) that aren't wired
//     yet; surface a "${label} wired in a later PR" toast.

export default function NetworkTrackerRedesign() {
  const { user } = useFirebaseAuth();
  const { toast } = useToast();
  const queryClient = useQueryClient();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  // Tour inbox-demo state. When the tour reaches step 9 (Inbox) the surface
  // flips to 'inbox'; both data queries below disable themselves via
  // `enabled` and the orchestration effect lower in this file seeds a fake
  // Nick Wittig thread into React Query's cache. The query disable closes
  // the 30s refetchInterval race that would otherwise overwrite the seed.
  const { demoSurface } = useTour();
  const inboxDemoActive = demoSurface === 'inbox';
  // Deep-link from a result card "see in inbox": route state carries the
  // drafted contact's email and which segment it belongs to. Two refs keep the
  // handling idempotent per navigation (keyed on location.key): one gates the
  // one-time refetch/segment switch, the other gates expand + select once the
  // refetched list actually contains the target.
  const focusInitKeyRef = useRef<string | null>(null);
  const focusAppliedKeyRef = useRef<string | null>(null);
  // Gates the one-time auto-select of the first Contacted person on initial
  // load (effect below). Once set, later deselects, navigation, or background
  // refetches never re-force a selection.
  const autoSelectDoneRef = useRef(false);
  // Gates the ?contact=<id> deep-link from a Loop activity card so it applies
  // once per target id, not on every render or background refetch.
  const contactParamAppliedRef = useRef<string | null>(null);

  // The active-list data query. Same key as the production /tracker page so
  // they share the React Query cache. 30s refetch matches existing behavior.
  const { data: threadsData, isLoading, isError } = useQuery({
    queryKey: ["trackerContacts"],
    queryFn: async () => {
      const res = await apiService.getOutboxThreads();
      if ("error" in res) throw new Error(res.error);
      return res.threads;
    },
    // Disabled during the tour inbox demo. React Query still returns the
    // cached data we wrote via setQueryData, but the 30s refetchInterval
    // and any mount/focus refetches are suppressed, so the seeded Nick
    // thread can't be overwritten by a background fetch.
    enabled: !!user && !inboxDemoActive,
    refetchInterval: 30_000,
  });

  // ── Local UI state ──────────────────────────────────────────────────────
  const [view, setView] = useState<"default" | "spreadsheet">("default");
  const [segment, setSegment] = useState<ProtoSegment>("people");
  const [selectedContactId, setSelectedContactId] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [activeFilters, setActiveFilters] = useState<Set<string>>(new Set());
  const [bookmarkedOnly, setBookmarkedOnly] = useState(false);
  const [bookmarkedIds, setBookmarkedIds] = useState<Set<string>>(new Set());
  const [openGroups, setOpenGroups] = useState<Record<ProtoStage, boolean>>({
    saved: true,
    drafted: true,
    contacted: false,
    opened: false,
    connected: false,
    interviewing: false,
    offer: false,
  });
  const [spreadsheetPage, setSpreadsheetPage] = useState(1);
  const [spreadsheetSort, setSpreadsheetSort] = useState<SpreadsheetSort | null>(null);
  const [selectedRows, setSelectedRows] = useState<Set<string>>(new Set());

  // ── The three write paths ───────────────────────────────────────────────
  const stubAction = useCallback(
    (label: string) => () => {
      toast({ title: `${label} wired in a later PR` });
    },
    [toast]
  );

  // Pipeline-stage click: most stages → PUT /outbox/threads/<id>/stage with
  // the inverse-mapped backend stage. "offer" goes through markOutboxThreadWon
  // because the adapter recognises Offer via resolution=meeting_booked, not
  // a pipelineStage value.
  const stageMutation = useMutation({
    mutationFn: async ({ contactId, stage }: { contactId: string; stage: ProtoStage }) => {
      const res =
        stage === "offer"
          ? await apiService.markOutboxThreadWon(contactId)
          : await apiService.patchOutboxStage(contactId, protoStageToBackend(stage));
      if ("error" in res) throw new Error(res.error);
      return res.thread;
    },
    onSuccess: (_thread, { stage }) => {
      queryClient.invalidateQueries({ queryKey: ["trackerContacts"] });
      toast({ title: `Moved to ${stage.charAt(0).toUpperCase()}${stage.slice(1)}` });
    },
    onError: (err: Error) => {
      toast({ title: "Couldn't update stage", description: err.message, variant: "destructive" });
    },
  });

  const toggleBookmark = useCallback((id: string) => {
    setBookmarkedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const toggleRow = useCallback((id: string) => {
    setSelectedRows((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  // Saved hiring managers (the recruiters subcollection). Used only to classify
  // which outbox conversations belong to the Hiring Managers segment, matched by
  // email. These are static cards with no thread state, so the email is the only
  // link to the outbox feed. 60s refetch is plenty since this list rarely moves.
  const { data: recruiterList } = useQuery({
    queryKey: ["recruitersForSegment"],
    queryFn: () => firebaseApi.getRecruiters(user!.uid),
    enabled: !!user,
    refetchInterval: 60_000,
  });

  const hiringManagerEmails = useMemo(() => {
    const set = new Set<string>();
    for (const r of recruiterList ?? []) {
      const raw = (r as { email?: string; Email?: string }).email
        || (r as { Email?: string }).Email
        || "";
      const email = raw.toLowerCase().trim();
      if (email) set.add(email);
    }
    return set;
  }, [recruiterList]);

  // ── Pure read-side derivation ───────────────────────────────────────────
  const protoContacts = useMemo<ProtoContact[]>(
    () => (threadsData ?? []).map(outboxThreadToProto),
    [threadsData]
  );

  // Counts for the active-filter pills, computed off the unfiltered list.
  const filterCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const c of protoContacts) {
      if (c.stage) {
        const key = `stage:${c.stage}`;
        counts[key] = (counts[key] ?? 0) + 1;
      }
      if (c.status === "Reply") {
        counts["has-reply"] = (counts["has-reply"] ?? 0) + 1;
      }
    }
    return counts;
  }, [protoContacts]);

  const filteredContacts = useMemo(() => {
    let result = protoContacts;

    // Segment split: Hiring Managers shows conversations whose email matches a
    // saved hiring manager, People shows everyone else. Matched by email since
    // that is the only link between the recruiters list and the outbox feed.
    if (segment === "hiringManagers") {
      result = result.filter((c) => hiringManagerEmails.has(c.email.toLowerCase()));
    } else {
      result = result.filter((c) => !hiringManagerEmails.has(c.email.toLowerCase()));
    }

    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      result = result.filter(
        (c) =>
          c.name.toLowerCase().includes(q) ||
          c.company.toLowerCase().includes(q) ||
          c.email.toLowerCase().includes(q)
      );
    }

    const stageFilters = Array.from(activeFilters).filter((f) => f.startsWith("stage:"));
    if (stageFilters.length > 0) {
      const allowed = new Set(stageFilters.map((f) => f.replace("stage:", "")));
      result = result.filter((c) => c.stage && allowed.has(c.stage));
    }

    if (activeFilters.has("has-reply")) {
      result = result.filter((c) => c.status === "Reply");
    }

    if (bookmarkedOnly) {
      result = result.filter((c) => bookmarkedIds.has(c.id));
    }

    if (sortKey) {
      const sorted = [...result];
      switch (sortKey) {
        case "name-asc":
          sorted.sort((a, b) => a.name.localeCompare(b.name));
          break;
        case "name-desc":
          sorted.sort((a, b) => b.name.localeCompare(a.name));
          break;
        case "date-newest":
          sorted.sort((a, b) => a.daysAgo - b.daysAgo);
          break;
        case "date-oldest":
          sorted.sort((a, b) => b.daysAgo - a.daysAgo);
          break;
        case "company-asc":
          sorted.sort((a, b) => a.company.localeCompare(b.company));
          break;
      }
      result = sorted;
    }

    return result;
  }, [protoContacts, segment, hiringManagerEmails, searchQuery, activeFilters, bookmarkedOnly, bookmarkedIds, sortKey]);

  const stageGroups = useMemo(() => groupedByStage(filteredContacts), [filteredContacts]);

  // Lookup against the FULL list so a selected contact does not disappear
  // when the user adds a filter that would exclude it from the visible list.
  const selectedContact = useMemo(
    () => protoContacts.find((c) => c.id === selectedContactId) ?? null,
    [protoContacts, selectedContactId]
  );

  // Raw outbox thread for the selected contact — used to derive the Gmail URL
  // for the "View in Gmail" footer button. ProtoContact drops the Gmail fields
  // at adapter time, so we look back at threadsData instead of re-threading
  // them through the adapter.
  const selectedOutboxThread = useMemo(
    () => (threadsData ?? []).find((t) => t.id === selectedContactId) ?? null,
    [threadsData, selectedContactId]
  );

  const selectedGmailUrl = useMemo(() => {
    if (!selectedOutboxThread) return null;
    if (selectedOutboxThread.gmailThreadId) {
      return `https://mail.google.com/mail/u/0/#inbox/${selectedOutboxThread.gmailThreadId}`;
    }
    if (selectedOutboxThread.gmailDraftUrl) {
      return selectedOutboxThread.gmailDraftUrl;
    }
    // Fall back to a Gmail search-by-recipient. Surfaces any past
    // correspondence (or empty results) so the user is never stranded with
    // no Gmail entry point on contacts the agent created without ever
    // drafting in Gmail.
    if (selectedOutboxThread.email) {
      return `https://mail.google.com/mail/u/0/#search/to:${encodeURIComponent(selectedOutboxThread.email)}`;
    }
    return null;
  }, [selectedOutboxThread]);

  // ── Deep-link focus from "see in inbox" ─────────────────────────────────
  // Step 1: on a fresh deep-link navigation, switch to the target segment and
  // force a fresh fetch so a just-created draft is not hidden behind the
  // 5-minute-stale React Query cache. Runs once per location.key.
  useEffect(() => {
    const state = location.state as { focusEmail?: string; segment?: ProtoSegment } | null;
    if (!state?.focusEmail) return;
    if (focusInitKeyRef.current === location.key) return;
    focusInitKeyRef.current = location.key;
    if (state.segment) setSegment(state.segment);
    queryClient.invalidateQueries({ queryKey: ["trackerContacts"] });
    // Also refetch the saved-recruiters list that drives the Hiring Managers
    // segment split, so a just-found hiring manager is classified into that
    // segment before the deep-link lands instead of falling into People.
    queryClient.invalidateQueries({ queryKey: ["recruitersForSegment"] });
  }, [location.key, location.state, queryClient]);

  // Step 2: once the (refetched) list contains the targeted contact, expand its
  // stage group (draft_created maps to "contacted", which defaults collapsed)
  // and select it. protoContacts is a dep, so this re-runs as data arrives and
  // applies exactly once per navigation.
  useEffect(() => {
    const state = location.state as { focusEmail?: string; segment?: ProtoSegment } | null;
    if (!state?.focusEmail) return;
    if (focusAppliedKeyRef.current === location.key) return;
    const target = protoContacts.find(
      (c) => c.email && c.email.toLowerCase() === state.focusEmail!.toLowerCase()
    );
    if (!target) return;
    focusAppliedKeyRef.current = location.key;
    if (target.stage) setOpenGroups((prev) => ({ ...prev, [target.stage!]: true }));
    setSelectedContactId(target.id);
  }, [location.key, location.state, protoContacts]);

  // ── Deep-link focus from a Loop activity card (/outbox?contact=<id>) ──────
  // Loop feed cards link by contact id (not email/route-state), so we read the
  // query param here. Once the list contains the target, expand its stage
  // group, select it, and scroll it into view. Applies once per target id.
  useEffect(() => {
    const target = searchParams.get("contact");
    if (!target) return;
    if (contactParamAppliedRef.current === target) return;
    const match = protoContacts.find((c) => c.id === target);
    if (!match) return;
    contactParamAppliedRef.current = target;
    if (match.stage) setOpenGroups((prev) => ({ ...prev, [match.stage!]: true }));
    setSelectedContactId(target);
    requestAnimationFrame(() => {
      document
        .querySelector(`[data-contact-id="${target}"]`)
        ?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
  }, [searchParams, protoContacts]);

  // ── Auto-select the first sensible contact on initial load ─────────────
  // Once both data queries have resolved, pick the highest-priority bucket
  // with people in it (Contacted first — those have an in-flight send;
  // Drafted second — those are ready to send) and select its first person so
  // the detail pane lands on a live conversation instead of the empty state.
  // Fires exactly once (autoSelectDoneRef), so a later deselect, tab switch,
  // navigation, or 30s/60s background refetch never forces the user back. We
  // wait for recruiterList too, because the People / Hiring Managers split
  // is derived from it; selecting before it resolves could pick a hiring
  // manager mis-classified into People. We skip when a contact is already
  // selected (back-nav) or a deep-link is pending, since that path owns
  // selection.
  useEffect(() => {
    if (autoSelectDoneRef.current) return;
    if (isLoading || isError) return;
    if (threadsData === undefined || recruiterList === undefined) return;
    autoSelectDoneRef.current = true;
    if (selectedContactId !== null) return;
    const state = location.state as { focusEmail?: string } | null;
    if (state?.focusEmail) return;
    // A ?contact= deep-link owns selection — don't override it with the
    // first-Contacted auto-pick.
    if (searchParams.get("contact")) return;
    const firstContacted = stageGroups.contacted[0] ?? stageGroups.drafted[0];
    if (!firstContacted) return;
    const targetStage = firstContacted.stage ?? "contacted";
    setOpenGroups((prev) => ({ ...prev, [targetStage]: true }));
    setSelectedContactId(firstContacted.id);
  }, [isLoading, isError, threadsData, recruiterList, stageGroups, selectedContactId, location.state, searchParams]);

  // ── Local UI handlers (no writes) ───────────────────────────────────────
  const toggleFilter = useCallback((id: string) => {
    setActiveFilters((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const clearFilters = useCallback(() => {
    setActiveFilters(new Set());
    setSortKey(null);
  }, []);

  const toggleGroup = useCallback((stage: ProtoStage) => {
    setOpenGroups((prev) => ({ ...prev, [stage]: !prev[stage] }));
  }, []);

  const handleSpreadsheetSort = useCallback((key: SpreadsheetSortKey) => {
    setSpreadsheetSort((prev) => {
      if (prev && prev.key === key) {
        return { key, dir: prev.dir === "asc" ? "desc" : "asc" };
      }
      return { key, dir: "asc" };
    });
  }, []);

  const toggleAllVisible = useCallback((visible: ProtoContact[]) => {
    setSelectedRows((prev) => {
      const allSelected = visible.length > 0 && visible.every((c) => prev.has(c.id));
      const next = new Set(prev);
      if (allSelected) visible.forEach((c) => next.delete(c.id));
      else visible.forEach((c) => next.add(c.id));
      return next;
    });
  }, []);

  const userName = user?.displayName || "";
  const userEmail = user?.email || "";

  // ── Thread view: real conversation chain + recommended reply ───────────
  // The chain replaces the old hardcoded buildTemplates() in ProtoEmailBlock.
  // Cached per contact in React Query so flipping between contacts feels
  // instant; 5-minute stale matches the rest of the page.
  const threadMessagesQuery = useQuery({
    queryKey: ["outboxThreadMessages", selectedContactId],
    queryFn: async () => {
      if (!selectedContactId) throw new Error("no contact");
      const res = await apiService.getOutboxThreadMessages(selectedContactId);
      if ("error" in res) throw new Error(res.error);
      return res;
    },
    // Disabled during demo so the seeded two-message thread for 'demo-nick'
    // isn't refetched (and so getOutboxThreadMessages never hits the
    // backend with a fake contact id).
    enabled: !!selectedContactId && !inboxDemoActive,
    staleTime: 5 * 60 * 1000,
  });

  const threadMessages: ThreadMessage[] = threadMessagesQuery.data?.messages ?? [];
  const threadSource: "gmail" | "local" = threadMessagesQuery.data?.source ?? "local";
  const threadReason = threadMessagesQuery.data?.reason;
  const gmailDisconnected = threadSource === "local" && threadReason === "gmail_disconnected";

  // Recommended-reply state, keyed by contactId so edits survive flips
  // between contacts inside the same session. The Generate click below
  // overwrites the entry for the active contact.
  const [draftsByContact, setDraftsByContact] = useState<Record<string, string>>({});
  const [draftLoadingId, setDraftLoadingId] = useState<string | null>(null);
  const [draftErrorByContact, setDraftErrorByContact] = useState<Record<string, string | null>>({});

  const activeDraftBody = selectedContactId ? (draftsByContact[selectedContactId] ?? "") : "";
  const activeDraftLoading = draftLoadingId !== null && draftLoadingId === selectedContactId;
  const activeDraftError = selectedContactId ? (draftErrorByContact[selectedContactId] ?? null) : null;

  const handleChangeDraftBody = useCallback((next: string) => {
    if (!selectedContactId) return;
    setDraftsByContact((prev) => ({ ...prev, [selectedContactId]: next }));
  }, [selectedContactId]);

  // Prefill the textarea with the existing Gmail draft body when the user
  // opens a Drafted-stage contact. Two product reasons:
  //   1. The point of the Drafted bucket is "you started but never sent" —
  //      surfacing the unsent text in the editor lets them tweak + send
  //      without first clicking Generate (which would overwrite it).
  //   2. The chain endpoint already returns the unsent draft as a single
  //      local message, so we have the body for free.
  // Conditions are strict to avoid clobbering edits or generated drafts:
  //   - Contact is in the "drafted" stage.
  //   - draftsByContact[id] is undefined — the user hasn't typed or
  //     generated anything for this contact yet in this session. Once they
  //     touch the textarea, even to empty it, the key exists and we won't
  //     re-prefill.
  //   - The chain has a single message with content (the local emailBody).
  useEffect(() => {
    if (!selectedContactId) return;
    if (selectedContact?.stage !== "drafted") return;
    if (draftsByContact[selectedContactId] !== undefined) return;
    if (threadSource !== "local") return;
    const first = threadMessages[0];
    const body = first?.body?.trim();
    if (!body) return;
    setDraftsByContact((prev) => ({ ...prev, [selectedContactId]: first.body }));
  }, [selectedContactId, selectedContact?.stage, threadSource, threadMessages, draftsByContact]);

  // ── Tour inbox demo orchestration ──────────────────────────────────────
  // When the tour reaches step 9 (Inbox), seed a fake Nick Wittig thread
  // directly into React Query's cache so the existing inbox UI renders it
  // through its normal data path — no parallel mocked components. Both
  // queries above are disabled while `inboxDemoActive` is true, so the
  // seeded cache cannot be overwritten by getOutboxThreads' 30s
  // refetchInterval or by getOutboxThreadMessages' mount/focus refetch.
  // Teardown uses removeQueries (NOT invalidateQueries) so we don't briefly
  // flicker through stale-but-still-shown Nick data while the real fetch
  // is in flight: cache → empty → loading → real threads. One clean fetch.
  useEffect(() => {
    if (!inboxDemoActive) return;

    const now = Date.now();
    const sentIso = new Date(now - 1000 * 60 * 60 * 24).toISOString();    // ~1 day ago
    const repliedIso = new Date(now - 1000 * 60 * 60 * 2).toISOString();  // ~2 hours ago

    const userEmail = user?.email ?? 'you@example.com';
    const userDisplayName = (user as { displayName?: string | null } | null)?.displayName ?? 'You';

    const nickThread: OutboxThread = {
      id: 'demo-nick',
      name: 'Nick Wittig',
      email: 'nickwittig@offerloop.ai',
      company: 'Offerloop',
      title: 'Cofounder',
      // External-nav surfaces stay inert by simply not seeding their URLs:
      // no linkedinUrl → no LinkedIn pill; no gmailThreadId/gmailDraftUrl
      // → no "View in Gmail" anchor.
      linkedinUrl: null,
      pipelineStage: 'replied',
      inOutbox: true,
      hasUnreadReply: false,
      gmailThreadId: null,
      gmailDraftId: null,
      gmailDraftUrl: null,
      emailSubject: 'Coffee chat about Offerloop',
      draftToEmail: null,
      lastMessageSnippet: "Happy to chat. Tuesday or Thursday afternoon both work for me. What are you working on?",
      lastMessageFrom: 'Nick Wittig',
      emailSentAt: sentIso,
      draftCreatedAt: null,
      replyReceivedAt: repliedIso,
      lastActivityAt: repliedIso,
      followUpCount: 0,
      nextFollowUpAt: null,
      messageCount: 2,
      resolution: null,
      resolutionDetails: null,
      conversationSummary: null,
      archivedAt: null,
      snoozedUntil: null,
      updatedAt: repliedIso,
      lastSyncError: null,
      lastSyncAt: null,
    };

    const messages: ThreadMessage[] = [
      {
        messageId: 'demo-msg-1',
        sender: `${userDisplayName} <${userEmail}>`,
        isFromRecipient: false,
        isFromUser: true,
        sentAt: sentIso,
        subject: 'Coffee chat about Offerloop',
        body:
          "Hi Nick,\n\nI came across Offerloop and love what you're building with AI-driven networking for students. I'd love to learn how you've thought about positioning the product for the college-recruiting use case.\n\nWould you be open to a 20-minute call next week?\n\nBest,\n" + userDisplayName,
      },
      {
        messageId: 'demo-msg-2',
        sender: 'Nick Wittig <nickwittig@offerloop.ai>',
        isFromRecipient: true,
        isFromUser: false,
        sentAt: repliedIso,
        subject: 'Re: Coffee chat about Offerloop',
        body:
          "Happy to chat. Tuesday or Thursday afternoon both work for me. Tell me a bit about what you're working on so I can come prepped.\n\nNick",
      },
    ];

    const threadResponse: ThreadMessagesResponse = { source: 'local', messages };

    // Pre-fill the recommended-reply box so the tour shows the full picture
    // at once: Nick's reply in the thread, AND a thread-aware draft response
    // above it. The text responds to Nick's specific offer (Tuesday or
    // Thursday, "what are you working on?"), so it reads like the page's
    // own "thread-aware reply" label rather than a placeholder. Inert: the
    // Send button's onClick guards on `inboxDemoActive` before it can fire
    // sendOutboxReply, and Generate is guarded too — the user can read the
    // draft but cannot send or regenerate against the backend.
    const demoDraftReply =
      "Thursday afternoon works great. Would 2pm fit? I'm thinking through how to position a recruiting product for college students and would love to hear how Offerloop dialed in PMF with that audience. See you Thursday.";

    // Cancel any in-flight fetches BEFORE seeding. `enabled: false` on the
    // useQuery prevents new fetches from starting, but a fetch already
    // dispatched during cross-route mount (when `run` was transiently false
    // and `enabled` resolved true for one render) keeps running and would
    // write its result into the cache, clobbering the seed below. cancelQueries
    // marks the in-flight fetch as cancelled so React Query discards the
    // eventual response instead of overwriting our seed. Same race shape as
    // the My Network cancellable-effect fix; this is the React-Query
    // equivalent.
    queryClient.cancelQueries({ queryKey: ['trackerContacts'] });
    queryClient.cancelQueries({ queryKey: ['outboxThreadMessages', 'demo-nick'] });
    queryClient.setQueryData<OutboxThread[]>(['trackerContacts'], [nickThread]);
    queryClient.setQueryData<ThreadMessagesResponse>(['outboxThreadMessages', 'demo-nick'], threadResponse);
    setSelectedContactId('demo-nick');
    setDraftsByContact((prev) => ({ ...prev, 'demo-nick': demoDraftReply }));

    return () => {
      // Drop the seeded entries entirely so the next render starts with an
      // empty cache. React Query's enabled flag flips true on the same
      // render, sees no cache, fires ONE fetch, and the page's existing
      // isLoading branch shows the loading state during that fetch.
      setSelectedContactId(null);
      queryClient.removeQueries({ queryKey: ['trackerContacts'] });
      queryClient.removeQueries({ queryKey: ['outboxThreadMessages', 'demo-nick'] });
      // Drop the seeded draft so the real reply box returns to its normal
      // empty "Click Generate" state on the next inbox session.
      setDraftsByContact((prev) => {
        if (!('demo-nick' in prev)) return prev;
        const next = { ...prev };
        delete next['demo-nick'];
        return next;
      });
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inboxDemoActive]);

  // Send the current draft body via the Gmail send endpoint. On success we
  // invalidate the threads list (stage / lastActivityAt have moved), clear
  // the local draft so the textarea returns to the placeholder, and refetch
  // the thread messages so the just-sent note shows up in the chain.
  const sendReplyMutation = useMutation({
    mutationFn: async ({ contactId, body }: { contactId: string; body: string }) => {
      const res = await apiService.sendOutboxReply(contactId, body);
      if ("error" in res) throw new Error(res.error);
      return res.thread;
    },
    onSuccess: (_thread, { contactId }) => {
      setDraftsByContact((prev) => {
        const next = { ...prev };
        delete next[contactId];
        return next;
      });
      setDraftErrorByContact((prev) => ({ ...prev, [contactId]: null }));
      queryClient.invalidateQueries({ queryKey: ["trackerContacts"] });
      queryClient.invalidateQueries({ queryKey: ["outboxThreadMessages", contactId] });
      toast({ title: "Reply sent" });
    },
    onError: (err: Error, { contactId }) => {
      setDraftErrorByContact((prev) => ({ ...prev, [contactId]: err.message }));
      toast({ title: "Couldn't send reply", description: err.message, variant: "destructive" });
    },
  });

  const handleSendReply = useCallback(() => {
    if (inboxDemoActive) return;
    if (!selectedContactId) return;
    const trimmed = activeDraftBody.trim();
    if (!trimmed) return;
    sendReplyMutation.mutate({ contactId: selectedContactId, body: trimmed });
  }, [selectedContactId, activeDraftBody, sendReplyMutation, inboxDemoActive]);

  const sendInFlight = sendReplyMutation.isPending && sendReplyMutation.variables?.contactId === selectedContactId;
  const canSend = !!selectedContactId && activeDraftBody.trim().length > 0 && !gmailDisconnected && !sendInFlight;

  // Every Generate click passes refresh=1. The cached replyDrafts doc was
  // written by the Gmail webhook path before the thread-aware prompt
  // existed, so returning the cache would surface a stale draft on the
  // contacts where thread context matters most (the ones with replies).
  // Always-refresh keeps that footgun closed.
  const { hasResume } = useResumeStatus();
  const [showResumeNudge, setShowResumeNudge] = useState(false);

  const runGenerate = useCallback(async () => {
    if (inboxDemoActive) return;
    if (!selectedContactId) return;
    setDraftLoadingId(selectedContactId);
    setDraftErrorByContact((prev) => ({ ...prev, [selectedContactId]: null }));
    try {
      const res = await apiService.getReplyCoachDraft(selectedContactId, { refresh: true });
      if ("error" in res) {
        setDraftErrorByContact((prev) => ({ ...prev, [selectedContactId]: res.error || "Failed to generate." }));
        return;
      }
      if (res.status === "generating") {
        setDraftErrorByContact((prev) => ({ ...prev, [selectedContactId]: "Still generating, try again in a moment." }));
        return;
      }
      setDraftsByContact((prev) => ({ ...prev, [selectedContactId]: res.body || "" }));
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Failed to generate.";
      setDraftErrorByContact((prev) => ({ ...prev, [selectedContactId]: msg }));
    } finally {
      setDraftLoadingId((current) => (current === selectedContactId ? null : current));
    }
  }, [selectedContactId, inboxDemoActive]);

  // Generate CTA: nudge for a resume first (once per session) — the draft
  // personalizes much better with one. "Continue without resume" generates
  // the email anyway, just without resume input.
  const handleGenerate = useCallback(() => {
    if (hasResume === false && !sessionStorage.getItem("ol_nudge_resume")) {
      sessionStorage.setItem("ol_nudge_resume", "1");
      setShowResumeNudge(true);
      return;
    }
    void runGenerate();
  }, [hasResume, runGenerate]);

  // ── Render ──────────────────────────────────────────────────────────────
  return (
    <SidebarProvider>
      <SetupNudgeModal
        open={showResumeNudge}
        variant="resume"
        onClose={() => setShowResumeNudge(false)}
        onContinue={() => void runGenerate()}
        body="Replies personalize far better with your resume: real overlaps between you and the contact instead of generic lines. You can also generate without it."
      />
      <div className="flex h-screen w-full">
        <AppSidebar />
        <div className="flex-1 flex flex-col min-w-0">
          <AppHeader />
          <main className="tracker-redesign" data-tour="tour-inbox">
            <div className="filter-bar">
              <ProtoHeader />
              <ProtoToolbar
                view={view}
                onChangeView={setView}
                searchQuery={searchQuery}
                onSearchChange={setSearchQuery}
                sortKey={sortKey}
                activeFilters={activeFilters}
                onSortChange={setSortKey}
                onFilterToggle={toggleFilter}
                onClearFilters={clearFilters}
                bookmarkedOnly={bookmarkedOnly}
                onToggleBookmarkedOnly={() => setBookmarkedOnly((v) => !v)}
                onImportCsv={stubAction("Import CSV")}
                onExport={stubAction("Export")}
              />
              {activeFilters.size > 0 && (
                <div className="active-filters">
                  <span className="active-filters-label">Active filters:</span>
                  {Array.from(activeFilters).map((id) => (
                    <span key={id} className="active-filter-pill">
                      <span className="active-filter-label">{FILTER_LABELS[id] || id}</span>
                      {filterCounts[id] != null && (
                        <span className="active-filter-count">({filterCounts[id]})</span>
                      )}
                      <button
                        type="button"
                        className="active-filter-remove"
                        onClick={() => toggleFilter(id)}
                        aria-label={`Remove ${FILTER_LABELS[id] || id}`}
                      >
                        <svg viewBox="0 0 14 14" fill="none" xmlns="http://www.w3.org/2000/svg">
                          <line x1="3.5" y1="3.5" x2="10.5" y2="10.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                          <line x1="10.5" y1="3.5" x2="3.5" y2="10.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                        </svg>
                      </button>
                    </span>
                  ))}
                  <button type="button" className="active-filter-clear" onClick={clearFilters}>
                    Clear all
                  </button>
                </div>
              )}
            </div>

            {isLoading ? (
              <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "#94a3b8", fontSize: 14 }}>
                Loading contacts...
              </div>
            ) : isError ? (
              <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "#dc2626", fontSize: 14 }}>
                Failed to load contacts.
              </div>
            ) : view === "default" ? (
              <div className="content-area">
                <div className="list-col">
                  <SegmentTabs
                    activeSegment={segment}
                    onSelectSegment={setSegment}
                  />
                  <div className="list-col-inner">
                    {segment === "hiringManagers" && filteredContacts.length === 0 ? (
                      <div style={{ padding: 16, color: "#94a3b8", fontSize: 13 }}>
                        {searchQuery.trim()
                          ? "No hiring manager conversations match your search."
                          : "No hiring manager conversations yet. Find a hiring manager from the Find page and draft an email, and the thread will show up here."}
                      </div>
                    ) : (
                      <ContactListAccordion
                        grouped={stageGroups}
                        openGroups={openGroups}
                        selectedContactId={selectedContactId}
                        onToggleGroup={toggleGroup}
                        onSelectContact={setSelectedContactId}
                      />
                    )}
                  </div>
                </div>

                <div className="detail-col">
                  {selectedContact ? (
                    <>
                      <div className="detail-scroll">
                        <ProtoDetailHeader
                          contact={selectedContact}
                          isBookmarked={bookmarkedIds.has(selectedContact.id)}
                          onToggleBookmark={() => toggleBookmark(selectedContact.id)}
                        />
                        <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
                          <div className="section-heading">
                            <span className="section-label">Pipeline Stage</span>
                            <span className="section-hint">Click a stage to update</span>
                          </div>
                          <ProtoPipelineDots
                            activeStage={selectedContact.stage}
                            openedAt={selectedContact.lastOpenedAt}
                            onStageClick={(stage) => {
                              if (inboxDemoActive) return;
                              stageMutation.mutate({ contactId: selectedContact.id, stage });
                            }}
                          />
                        </div>
                        <ProtoEmailBlock
                          contact={selectedContact}
                          userName={userName}
                          userEmail={userEmail}
                          messages={threadMessages}
                          messagesLoading={threadMessagesQuery.isLoading}
                          messagesError={threadMessagesQuery.isError ? "Couldn't load the conversation." : null}
                          draftBody={activeDraftBody}
                          onChangeDraftBody={handleChangeDraftBody}
                          draftLoading={activeDraftLoading}
                          draftError={activeDraftError}
                          generateSlot={
                            <button
                              type="button"
                              onClick={handleGenerate}
                              disabled={activeDraftLoading || gmailDisconnected}
                              title={gmailDisconnected ? "Connect Gmail to reply" : undefined}
                              style={{
                                padding: "6px 12px",
                                fontSize: 13,
                                fontWeight: 500,
                                borderRadius: 4,
                                border: "1px solid var(--primary, #4a60a8)",
                                background: gmailDisconnected ? "#f5f6f8" : "var(--primary, #4a60a8)",
                                color: gmailDisconnected ? "#94a3b8" : "#fff",
                                cursor: activeDraftLoading || gmailDisconnected ? "not-allowed" : "pointer",
                                opacity: activeDraftLoading ? 0.7 : 1,
                              }}
                            >
                              {activeDraftLoading
                                ? "Generating…"
                                : activeDraftBody
                                  ? "Regenerate"
                                  : gmailDisconnected
                                    ? "Connect Gmail to reply"
                                    : "Generate"}
                            </button>
                          }
                          emptyChainSlot={
                            gmailDisconnected ? (
                              <div style={{ fontSize: 13, color: "#94a3b8", padding: 8 }}>
                                Connect Gmail to load this conversation.
                              </div>
                            ) : undefined
                          }
                        />
                      </div>

                      <div className="detail-footer">
                        <button type="button" className="btn-primary" onClick={stubAction("Save Draft")}>
                          Save Draft
                        </button>
                        <div className="footer-btn-group">
                          <button
                            type="button"
                            className="btn-primary"
                            onClick={handleSendReply}
                            disabled={!canSend}
                            title={
                              gmailDisconnected
                                ? "Connect Gmail to send"
                                : activeDraftBody.trim().length === 0
                                  ? "Generate or write a reply first"
                                  : undefined
                            }
                            style={!canSend ? { opacity: 0.5, cursor: "not-allowed" } : undefined}
                          >
                            <svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
                              <path d="M7.25 8.74L1.25 6.07L14.58 1.4L9.92 14.74L7.25 8.74Z" stroke="white" strokeWidth="1.33" strokeLinecap="round" strokeLinejoin="round" />
                              <path d="M14.58 1.4L7.25 8.74" stroke="white" strokeWidth="1.33" strokeLinecap="round" strokeLinejoin="round" />
                            </svg>
                            {sendInFlight ? "Sending…" : "Send"}
                          </button>
                          <button type="button" className="btn-secondary" onClick={stubAction("Edit Template")}>
                            <svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
                              <path d="M13.8 14.83V15.18H2V14.83H13.8ZM10.05 1.05C10.12 0.98 10.23 0.98 10.3 1.05L12.32 3.07C12.39 3.14 12.39 3.25 12.32 3.32L4.32 11.32H2V8.97L10.05 1.05Z" stroke="currentColor" strokeWidth="0.9" strokeLinejoin="round" />
                            </svg>
                            Edit Template
                          </button>
                          {selectedGmailUrl ? (
                            <a
                              href={selectedGmailUrl}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="btn-primary"
                              style={{ textDecoration: "none" }}
                            >
                              <svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
                                <path d="M6.5 2.5h-3a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-3" stroke="white" strokeWidth="1.33" strokeLinecap="round" strokeLinejoin="round" />
                                <path d="M9.5 2.5h4v4" stroke="white" strokeWidth="1.33" strokeLinecap="round" strokeLinejoin="round" />
                                <path d="M13.5 2.5L7 9" stroke="white" strokeWidth="1.33" strokeLinecap="round" strokeLinejoin="round" />
                              </svg>
                              View in Gmail
                            </a>
                          ) : (
                            <button
                              type="button"
                              className="btn-primary"
                              disabled
                              title="No Gmail thread for this contact yet"
                              style={{ opacity: 0.5, cursor: "not-allowed" }}
                            >
                              <svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
                                <path d="M6.5 2.5h-3a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-3" stroke="white" strokeWidth="1.33" strokeLinecap="round" strokeLinejoin="round" />
                                <path d="M9.5 2.5h4v4" stroke="white" strokeWidth="1.33" strokeLinecap="round" strokeLinejoin="round" />
                                <path d="M13.5 2.5L7 9" stroke="white" strokeWidth="1.33" strokeLinecap="round" strokeLinejoin="round" />
                              </svg>
                              View in Gmail
                            </button>
                          )}
                        </div>
                      </div>
                    </>
                  ) : (
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", color: "#94a3b8", fontSize: 14 }}>
                      Select a contact to view details
                    </div>
                  )}
                </div>
              </div>
            ) : (
              <ProtoSpreadsheet
                contacts={filteredContacts}
                activeSegment={segment}
                onSelectSegment={setSegment}
                sort={spreadsheetSort}
                onSort={handleSpreadsheetSort}
                page={spreadsheetPage}
                onChangePage={setSpreadsheetPage}
                selectedRows={selectedRows}
                onToggleRow={toggleRow}
                onToggleAllVisible={toggleAllVisible}
                bookmarkedIds={bookmarkedIds}
                onToggleBookmark={toggleBookmark}
                onDraft={stubAction("Draft email")}
              />
            )}
          </main>
        </div>
      </div>
    </SidebarProvider>
  );
}
