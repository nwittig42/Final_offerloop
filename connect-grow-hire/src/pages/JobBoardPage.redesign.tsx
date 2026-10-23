// JobBoardPage.redesign.tsx
//
// Prototype-faithful copy of offerloop-job-board.html wired to live data.
// Lives behind /dev/job-board-redesign. Production /job-board is untouched.
//
// Visual reference: offerloop-job-board.html + design frames (prototype-default,
// prototype-loading, prototype-found-people, prototype-save-search,
// prototype-filters, prototype-locked).
//
// Independent-scroll architecture:
//   .jb-editorial          height: 100% + overflow: hidden  (shell, no scroll)
//     .jb-fb               flex-shrink: 0                   (filter bar pinned)
//     .jb-twopane          flex: 1 + overflow: hidden       (container, no scroll)
//       .jb-list           overflow-y: auto                 (left scrolls)
//       .jb-detail         overflow-y: auto                 (right scrolls)
//
// Data flow: getJobFeed -> buildSectionedJobs (Recent-wins dedup) -> render.
// Quick-filter chips are visual-only this pass (counts hardcoded from frames).
// People CTA opens existing FindHumansModal.

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { AppSidebar } from "@/components/AppSidebar";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppHeader } from "@/components/AppHeader";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { useCreditsView } from "@/hooks/useCreditsView";
import { useResumeStatus } from "@/hooks/useResumeStatus";
import { SetupNudgeModal } from "@/components/gates/SetupNudgeModal";
import {
  apiService,
  type FeedJob,
  type JobFeedResponse,
  type JobSearchParams,
  type SavedJob,
} from "@/services/api";
import { JobBoardSkeleton } from "@/components/JobBoardSkeleton";
import { jobFeedQueryKey, JOB_FEED_STALE_MS } from "@/lib/jobFeedQuery";
import { readScoutPrefillEnvelope, SCOUT_PREFILL_EVENT } from "@/lib/scoutBridge";
import { JobBoardViewToggle, type JobBoardView } from "@/components/jobs/JobBoardViewToggle";
import {
  FindHumansModal,
  type FindHumansJob,
} from "@/components/jobs/FindHumansModal";
import { toast } from "@/hooks/use-toast";

import { JobCard } from "@/components/jobs/JobCard";
import { JobDetail, type JobDescriptionState } from "@/components/jobs/JobDetail";
import { SaveSearchModal } from "@/components/jobs/SaveSearchModal";
import { ApplicationProfileModal } from "@/components/jobs/ApplicationProfileModal";
import { AutoApplyReviewModal } from "@/components/jobs/AutoApplyReviewModal";
import { AutoSubmissionTab } from "@/components/jobs/AutoSubmissionTab";
import { NeedsAttentionTab } from "@/components/jobs/NeedsAttentionTab";
import { NeedsVerificationTab } from "@/components/jobs/NeedsVerificationTab";
import {
  submitAutoApply,
  type AutoApplyPrepareResponse,
} from "@/services/api";
import {
  MoreFiltersPanel,
  type MoreFiltersState,
} from "@/components/jobs/MoreFiltersPanel";
// moreFiltersToParams is defined below the imports so it can reference the
// imported MoreFiltersState type.
import {
  IconClose,
  IconFilter,
  IconPlus,
  IconRefresh,
  IconSavedBookmark,
  IconSearch,
} from "@/components/jobs/icons";

import {
  buildSectionedJobs,
  feedJobToProto,
  type ProtoJob,
} from "./jobBoardAdapter";
import "./JobBoardEditorial.redesign.css";

// Quick-filter chips. Each chip carries the JobSearchParams delta it
// contributes when active. The handler in catalogParamsFromChips merges them
// into a single query before calling apiService.searchJobs.
//
// Counts intentionally absent: badge numbers would lie until the catalog
// query has returned, so we leave them off rather than render stale ones.
type QuickFilter = { key: string; label: string; toParams: Partial<JobSearchParams> };

const QUICK_FILTERS: QuickFilter[] = [
  { key: "Remote",      label: "Remote",      toParams: { location: "remote" } },
  { key: "Full-time",   label: "Full-time",   toParams: { type: "FULLTIME" } },
  { key: "Internship",  label: "Internship",  toParams: { type: "INTERNSHIP" } },
  { key: "Entry-level", label: "Entry-level", toParams: { seniority: "entry" } },
];

// Merge active chip params into one JobSearchParams object. Later chips win
// on key conflicts (e.g. Full-time + Internship both set `type` -> the last
// one wins). The chip UI already prevents selecting conflicting chips by
// removing one when the other is added; this is a defensive last step.
function catalogParamsFromChips(
  active: string[],
  defs: QuickFilter[],
): Partial<JobSearchParams> {
  const out: Partial<JobSearchParams> = {};
  for (const def of defs) {
    if (active.includes(def.key)) Object.assign(out, def.toParams);
  }
  return out;
}

// Map the drawer state into backend params.
//
// Experience Level: "Internship" routes to `type` (the backend treats
// internships as an employment type, not a seniority). The rest route to
// `seniority`. Company Size and Visa Sponsorship were not re-added because
// the data audit found 0% coverage; this helper does not need to handle them.
//
// Date Posted maps the human label to the backend's relative-window token
// vocabulary ("24h" / "7d" / "30d"). "Any time" means no filter, so we omit
// the param entirely rather than send an empty string.
function moreFiltersToParams(
  state: MoreFiltersState | null,
): Partial<JobSearchParams> {
  if (!state) return {};
  const out: Partial<JobSearchParams> = {};
  const exp = state.experience[0];
  if (exp === "Internship") out.type = "INTERNSHIP";
  else if (exp === "Entry Level") out.seniority = "entry";
  else if (exp === "Mid Level") out.seniority = "mid";
  else if (exp === "Senior") out.seniority = "senior";
  if (state.datePosted === "Past 24h") out.postedAfter = "24h";
  else if (state.datePosted === "Past week") out.postedAfter = "7d";
  else if (state.datePosted === "Past month") out.postedAfter = "30d";
  return out;
}

// Cards rendered on the first synchronous paint after a feed loads. The rest
// are revealed on the next idle tick (see revealAll). Keeps first paint cheap
// without dropping any job from the final list.
const INITIAL_RENDER = 30;

// Session cache of fetched job descriptions, keyed by job id. Lives at module
// scope so it survives navigating away from and back to the board within the
// SPA session. Loaded and empty results are cached; errors are not, so a
// transient failure can be retried.
const descriptionCache = new Map<string, JobDescriptionState>();

interface JobBoardPageProps {
  // Supplied when hosted inside the Job Board tab so the board can render the
  // List/Gallery toggle. Standalone use (no props) just hides the toggle.
  view?: JobBoardView;
  onViewChange?: (view: JobBoardView) => void;
}

export const JobBoardPage: React.FC<JobBoardPageProps> = ({ view = "list", onViewChange }) => {
  const { user, isLoading: authLoading } = useFirebaseAuth();
  const creditsView = useCreditsView();

  // ---- Server data --------------------------------------------------------
  // The feed is fetched through React Query so navigating away from and back
  // to /job-board serves the cached response instantly (staleTime 5min,
  // matching the app default) instead of re-hitting the backend and re-showing
  // the skeleton every time. The page previously used raw useState/useEffect
  // and refetched the whole feed on every mount.
  //
  // The refresh button keeps its old semantics via refreshRef: when the user
  // presses it we ask the backend to rotate the ranked slice (refresh=true)
  // and surface the "Back to your top picks" wrap toast. A normal mount/
  // background revalidation sends refresh=false.
  const refreshRef = useRef(false);
  const [isManualRefreshing, setIsManualRefreshing] = useState(false);
  const feedQuery = useQuery<JobFeedResponse>({
    queryKey: jobFeedQueryKey(user?.uid),
    enabled: !!user,
    staleTime: JOB_FEED_STALE_MS,
    queryFn: async () => {
      const isRefresh = refreshRef.current;
      refreshRef.current = false;
      const data = await apiService.getJobFeed({ refresh: isRefresh });
      // Refresh-rotation toast. The backend advances an offset into the
      // user's cached ranked list on every refresh and signals feed_wrapped
      // when that offset wraps back to the top. Show the user a brief notice
      // so a wrap does not look like "nothing changed".
      if (isRefresh && data.feed_wrapped) {
        toast({
          title: "Back to your top picks",
          description:
            "You have seen the freshest matches. Starting over from the top of your ranking.",
        });
      }
      return data;
    },
  });
  const feed = feedQuery.data ?? null;
  // Show the skeleton only while there is no data yet (initial load) or during
  // an explicit refresh-button press — NOT during a silent background
  // revalidation, so a return visit with cached data paints immediately.
  const feedLoading = (feedQuery.isPending && !!user) || isManualRefreshing;
  // Progressive render gate. False on each fresh load so the first paint is
  // capped to INITIAL_RENDER cards; flipped true on the next idle tick to
  // render the remainder.
  const [revealAll, setRevealAll] = useState(false);
  const [savedIds, setSavedIds] = useState<Set<string>>(new Set());

  // ---- Local UI state -----------------------------------------------------
  const [search, setSearch] = useState("");

  // Scout prefill bridge: consume a /job-board query handoff into the search
  // box (see BrowseJobsPage for the gallery-view twin).
  useEffect(() => {
    const applyFromBridge = () => {
      const env = readScoutPrefillEnvelope("/job-board");
      const q = env && (env.prefill.query || env.prefill.prompt || "").trim();
      if (q) setSearch(q);
    };
    applyFromBridge();
    window.addEventListener(SCOUT_PREFILL_EVENT, applyFromBridge);
    return () => window.removeEventListener(SCOUT_PREFILL_EVENT, applyFromBridge);
  }, []);
  // No chips active on first render. Earlier the chips defaulted to "all on"
  // because they were visual-only and the user had no way to "turn them on"
  // anyway; now that each chip narrows the query, we start neutral so the
  // initial paint shows the personalized feed rather than a 4-way filtered
  // catalog query.
  const [activeFilters, setActiveFilters] = useState<string[]>([]);
  // Chips coming from the More Filters drawer (experience, date, size, visa).
  // These are removable just like quick filters; visual-only.
  const [moreFilterChips, setMoreFilterChips] = useState<string[]>([]);
  const [moreFiltersState, setMoreFiltersState] = useState<MoreFiltersState | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [dismissedIds, setDismissedIds] = useState<Set<string>>(new Set());
  const [showSaveSearch, setShowSaveSearch] = useState(false);
  const [showMoreFilters, setShowMoreFilters] = useState(false);
  const [showAddDropdown, setShowAddDropdown] = useState(false);
  const [findHumansJob, setFindHumansJob] = useState<FindHumansJob | null>(null);
  // Which search the Find the Humans modal runs, plus the requested count for
  // the employee / hiring-manager flows.
  const [findHumansKind, setFindHumansKind] = useState<"employee" | "hiring-manager">("hiring-manager");
  const [findHumansCount, setFindHumansCount] = useState(3);

  // Saved snapshots (full job data, not just IDs). Drive the Saved
  // top-level tab. Storing the full snapshot means a bookmarked job stays
  // viewable even if the original posting expires upstream and rotates out
  // of the live feed.
  //
  // The Applied tab was deliberately removed: clicking the Apply URL only
  // proves the user opened the link, not that they actually submitted an
  // application. Auto-tracking it created false memory ("oh, I already
  // applied"). If we ever want apply tracking, it should be an explicit
  // action — not a side-effect of opening a tab.
  const [savedJobs, setSavedJobs] = useState<SavedJob[]>([]);

  // Top-level tab. "discover" = the existing Recent + Recommended view.
  // "saved" swaps the entire job list for the bookmarked bin, mirroring
  // the Outbox tab pattern (always-visible at the top of the page).
  // "applications" hosts the three auto-apply queues as sub-tabs:
  //   all: every auto-apply job (in-flight + done + failed)
  //   needs-answers: jobs paused waiting on the user to answer a custom
  //     screening question we don't have an answer for
  //   finish-browser: jobs waiting on an in-browser verification step
  const [activeJobTab, setActiveJobTab] = useState<
    | "discover"
    | "saved"
    | "applications"
  >("discover");
  const [appsSubTab, setAppsSubTab] = useState<
    | "all"
    | "needs-answers"
    | "finish-browser"
  >("all");

  // Collapsible section state. Default: Recent collapsed, Recommended open
  // so the better-matching list is visible without scrolling. User's choice
  // is persisted to localStorage so it survives reload and tab switches.
  const [collapsedSections, setCollapsedSections] = useState<{
    recent: boolean;
    recommended: boolean;
    saved: boolean;
    applied: boolean;
  }>(() => {
    try {
      const raw = localStorage.getItem("jb_collapsed_sections");
      if (raw) {
        const parsed = JSON.parse(raw);
        return {
          recent: typeof parsed.recent === "boolean" ? parsed.recent : true,
          recommended: typeof parsed.recommended === "boolean" ? parsed.recommended : false,
          saved: typeof parsed.saved === "boolean" ? parsed.saved : true,
          applied: typeof parsed.applied === "boolean" ? parsed.applied : true,
        };
      }
    } catch {
      /* localStorage may be unavailable; fall through to defaults */
    }
    return { recent: true, recommended: false, saved: true, applied: true };
  });

  const toggleSection = useCallback((key: "recent" | "recommended" | "saved" | "applied") => {
    setCollapsedSections((prev) => {
      const next = { ...prev, [key]: !prev[key] };
      try {
        localStorage.setItem("jb_collapsed_sections", JSON.stringify(next));
      } catch {
        /* ignore storage failures */
      }
      return next;
    });
  }, []);

  const addDropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!showAddDropdown) return;
    const onClick = (e: MouseEvent) => {
      if (
        addDropdownRef.current &&
        !addDropdownRef.current.contains(e.target as Node)
      ) {
        setShowAddDropdown(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [showAddDropdown]);

  // ---- Data loaders -------------------------------------------------------
  // refetch is referentially stable in React Query v5, so the callback below
  // keeps a stable identity.
  const refetchFeed = feedQuery.refetch;
  const loadFeed = useCallback(
    async (refresh = false) => {
      if (refresh) {
        refreshRef.current = true;
        setRevealAll(false);
        setIsManualRefreshing(true);
      }
      try {
        await refetchFeed();
      } finally {
        if (refresh) setIsManualRefreshing(false);
      }
    },
    [refetchFeed],
  );

  // Surface a toast on fetch failure (the error itself lives in feedQuery.error).
  useEffect(() => {
    if (feedQuery.isError) {
      console.error("getJobFeed failed", feedQuery.error);
      toast({
        title: "Couldn't load jobs",
        description: "Try again in a moment.",
        variant: "destructive",
      });
    }
  }, [feedQuery.isError, feedQuery.error]);

  const loadSaved = useCallback(async () => {
    try {
      const r = await apiService.listSavedJobs();
      setSavedJobs(r.saved);
      setSavedIds(new Set(r.saved.map((s) => s.job_id)));
    } catch (err) {
      console.warn("saved jobs fetch failed", err);
    }
  }, []);

  // Feed itself is fetched by feedQuery (enabled on user). Only the saved-jobs
  // list still needs an imperative load on mount.
  useEffect(() => {
    if (!user) return;
    loadSaved();
  }, [user, loadSaved]);

  // ---- Adapter ------------------------------------------------------------
  const sections = useMemo(() => buildSectionedJobs(feed), [feed]);

  // ---- Catalog mode (search + filter chips hit /api/jobs/search) ---------
  // The discover view shows the personalized, ranked, capped feed. When the
  // user types a query or activates any chip we switch to "catalog mode":
  // the same UI renders results returned by the catalog query over the full
  // jobs store, with no per-company cap. Empty query AND no active chips
  // means the personalized feed renders.
  const isCatalogMode =
    search.trim().length > 0 ||
    activeFilters.length > 0 ||
    moreFilterChips.length > 0;

  const [catalogResults, setCatalogResults] = useState<ProtoJob[]>([]);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogScanned, setCatalogScanned] = useState(0);
  const [catalogNextCursor, setCatalogNextCursor] = useState<string | null>(null);

  useEffect(() => {
    if (!isCatalogMode) {
      // Drop any prior catalog state when leaving the mode so a re-entry
      // does not render stale results for a moment before the new query
      // returns.
      setCatalogResults([]);
      setCatalogError(null);
      setCatalogScanned(0);
      setCatalogNextCursor(null);
      return;
    }
    let cancelled = false;
    const handle = window.setTimeout(async () => {
      setCatalogLoading(true);
      setCatalogError(null);
      try {
        const params: JobSearchParams = {
          ...catalogParamsFromChips(activeFilters, QUICK_FILTERS),
          ...moreFiltersToParams(moreFiltersState),
          q: search.trim() || undefined,
          limit: 50,
        };
        const r = await apiService.searchJobs(params);
        if (cancelled) return;
        setCatalogResults(r.results.map((j: FeedJob) => feedJobToProto(j, "recommended")));
        setCatalogScanned(r.scanned);
        setCatalogNextCursor(r.next_cursor);
      } catch (err) {
        if (cancelled) return;
        console.error("searchJobs failed", err);
        setCatalogError("Search is temporarily unavailable.");
        setCatalogResults([]);
      } finally {
        if (!cancelled) setCatalogLoading(false);
      }
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [isCatalogMode, search, activeFilters, moreFilterChips, moreFiltersState]);

  // When in catalog mode the personalized sections collapse to empty so the
  // existing render code naturally hides them; results flow through what was
  // previously the Recommended section, retitled below.
  // searchedRecent/searchedRecommended are defined further down to compose
  // the auto-apply / chip / search filters before the perf-cap memo consumes
  // them.

  const allProtoJobs = useMemo(
    () => (isCatalogMode ? catalogResults : [...sections.recent, ...sections.recommended]),
    [isCatalogMode, catalogResults, sections]
  );

  // Poll the auto-apply queues at page level: needs-attention + needs-
  // verification counts drive the tab badges, and the full job-id list
  // drives the Discover filter (any job already in the auto-apply pipeline
  // gets hidden so the user can't double-fire and clutter the queue).
  const [needsAttentionCount, setNeedsAttentionCount] = useState<number>(0);
  const [needsVerificationCount, setNeedsVerificationCount] = useState<number>(0);
  const [autoAppliedJobIds, setAutoAppliedJobIds] = useState<Set<string>>(new Set());
  useEffect(() => {
    // The auto-apply queue endpoints are Pro/Elite-gated (403 for free tier).
    // Free users have nothing in these queues, so don't poll at all — it
    // just floods the console and the backend with 403s every 30s.
    if (!user || (user.tier !== "pro" && user.tier !== "elite")) return;
    let cancelled = false;
    const refresh = async () => {
      // Don't poll while the tab is backgrounded — these three endpoints add
      // up to constant load over a session for state the user can't see. We
      // catch up immediately on visibilitychange below.
      if (document.hidden) return;
      try {
        const api = await import("@/services/api");
        const [na, nv, all] = await Promise.all([
          api.listNeedsAttention(),
          api.listNeedsVerification(),
          api.listAutoApplyJobs(),
        ]);
        if (cancelled) return;
        setNeedsAttentionCount((na.items || []).length);
        setNeedsVerificationCount((nv.items || []).length);
        const ids = new Set<string>();
        for (const item of all.items || []) {
          const jid = (item as any).job_id;
          if (jid) ids.add(String(jid));
        }
        setAutoAppliedJobIds(ids);
      } catch {
        /* swallow; next poll will retry */
      }
    };
    refresh();
    // 30s (was 8s). The auto-apply queues are not real-time surfaces; an 8s
    // cadence was constant background load while the user read the feed.
    const id = window.setInterval(refresh, 30000);
    const onVisible = () => {
      if (!document.hidden) refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      window.clearInterval(id);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [user]);

  // "Only show auto-apply jobs" filter. Defaults to OFF: auto-apply is a
  // convenience, not the product, so the board shows the best-matched jobs
  // and lets students narrow to applyable ones if they want to. Defaulting
  // it ON hid our highest-intent inventory — the Workday / Oracle / Oleeo /
  // Goldman campus boards (Citi, JPM, BlackRock, Blackstone, PwC, Goldman
  // summer-analyst programs) are not auto-applyable, so a student's first
  // impression excluded exactly the roles they came here for.
  //
  // The storage key is versioned: the old key's value was almost never a
  // deliberate choice (the effect below persisted the ON default on first
  // mount), so reading it would keep every existing user filtered forever.
  // Bumping to .v2 gives everyone the new default once; explicit toggles
  // after that persist as normal.
  const [autoApplyOnly, setAutoApplyOnly] = useState<boolean>(() => {
    try {
      const stored = localStorage.getItem("jobBoard.autoApplyOnly.v2");
      return stored === null ? false : stored === "true";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem("jobBoard.autoApplyOnly.v2", String(autoApplyOnly));
    } catch {
      /* localStorage unavailable; toggle still works for the session */
    }
  }, [autoApplyOnly]);

  const applyAutoApplyFilter = useCallback(
    (list: ProtoJob[]) =>
      autoApplyOnly ? list.filter((j) => j.autoApplyEligible) : list,
    [autoApplyOnly]
  );

  // Hide jobs the user has already auto-applied to from Discover. Status
  // lives in the dedicated tabs (Auto-submission / Needs attention).
  const applyAppliedFilter = useCallback(
    (list: ProtoJob[]) => list.filter((j) => !autoAppliedJobIds.has(j.id)),
    [autoAppliedJobIds]
  );

  // Quick-filter chips — only apply when the chip is active. AND semantics
  // across chips (Remote + Full-time = both must match), OR within each
  // chip's logic (e.g. Remote matches either explicit-remote location text
  // OR a remote work_arrangement signal in the job's metadata).
  const applyChipFilter = useCallback(
    (list: ProtoJob[]) => {
      if (activeFilters.length === 0) return list;
      const wantRemote = activeFilters.includes("Remote");
      const wantFulltime = activeFilters.includes("Full-time");
      const want100k = activeFilters.includes("$100k+");
      return list.filter((j) => {
        if (wantRemote) {
          const loc = (j.location || "").toLowerCase();
          if (!loc.includes("remote") && !loc.includes("anywhere")) return false;
        }
        if (wantFulltime) {
          const t = (j.jobType || "").toLowerCase();
          if (!t.includes("full") && t !== "fulltime") return false;
        }
        if (want100k) {
          if (!j.salaryAnnual || j.salaryAnnual < 100_000) return false;
        }
        return true;
      });
    },
    [activeFilters]
  );

  // Catalog mode (an active search) collapses sections.recent to empty and
  // routes the catalog results through Recommended. Filters still apply
  // afterwards so chip + auto-apply-only state interacts with catalog
  // queries the same way it interacts with the personalized feed.
  const searchedRecent = useMemo(() => {
    const base = isCatalogMode ? [] : sections.recent;
    return applyAppliedFilter(applyChipFilter(applyAutoApplyFilter(applySearch(base, search))));
  }, [isCatalogMode, sections.recent, search, applyAutoApplyFilter, applyChipFilter, applyAppliedFilter]);
  const searchedRecommended = useMemo(() => {
    const base = isCatalogMode ? catalogResults : sections.recommended;
    return applyAppliedFilter(applyChipFilter(applyAutoApplyFilter(applySearch(base, search))));
  }, [isCatalogMode, catalogResults, sections.recommended, search, applyAutoApplyFilter, applyChipFilter, applyAppliedFilter]);

  // Once the feed is loaded, reveal the full list on the next idle tick. The
  // first synchronous paint renders at most INITIAL_RENDER cards (see the
  // capped slices below); this unblocks that paint and then fills in the rest.
  useEffect(() => {
    if (feedLoading || revealAll) return;
    const w = window as Window & {
      requestIdleCallback?: (cb: () => void) => number;
      cancelIdleCallback?: (id: number) => void;
    };
    let timeoutId: ReturnType<typeof setTimeout> | undefined;
    let idleId: number | undefined;
    if (w.requestIdleCallback) {
      idleId = w.requestIdleCallback(() => setRevealAll(true));
    } else {
      timeoutId = setTimeout(() => setRevealAll(true), 0);
    }
    return () => {
      if (idleId !== undefined && w.cancelIdleCallback) w.cancelIdleCallback(idleId);
      if (timeoutId !== undefined) clearTimeout(timeoutId);
    };
  }, [feedLoading, revealAll]);

  // Cap the rendered cards on first paint by depleting a shared budget across
  // the expanded, searched sections in render order (Recent then Recommended).
  // Collapsed sections render nothing regardless, so they consume no budget.
  // Section counts still use the full searched lengths, so the badges stay
  // honest while only the DOM is trimmed until revealAll flips true.
  const { recentToRender, recommendedToRender } = useMemo(() => {
    if (revealAll) {
      return { recentToRender: searchedRecent, recommendedToRender: searchedRecommended };
    }
    let budget = INITIAL_RENDER;
    const recentSlice = collapsedSections.recent ? [] : searchedRecent.slice(0, budget);
    budget -= recentSlice.length;
    const recommendedSlice = collapsedSections.recommended
      ? []
      : searchedRecommended.slice(0, budget);
    return { recentToRender: recentSlice, recommendedToRender: recommendedSlice };
  }, [
    revealAll,
    collapsedSections.recent,
    collapsedSections.recommended,
    searchedRecent,
    searchedRecommended,
  ]);

  // Default-select first Recent on first load; reselect when current selection
  // drops out of the visible list (e.g. dismissed or filtered away).
  useEffect(() => {
    const flat = [...searchedRecent, ...searchedRecommended].filter(
      (j) => !dismissedIds.has(j.id)
    );
    if (flat.length === 0) {
      setSelectedId(null);
      return;
    }
    if (!selectedId || !flat.some((j) => j.id === selectedId)) {
      setSelectedId(flat[0].id);
    }
  }, [searchedRecent, searchedRecommended, dismissedIds, selectedId]);

  const selectedJob = useMemo(
    () => allProtoJobs.find((j) => j.id === selectedId) ?? null,
    [allProtoJobs, selectedId]
  );

  // Lazy-load the description for the open job. The feed omits descriptions to
  // stay lean, so we fetch the single job's prose when its detail is shown.
  const [descRetryTick, setDescRetryTick] = useState(0);
  const [, setDescVersion] = useState(0);
  const bumpDesc = () => setDescVersion((v) => v + 1);

  useEffect(() => {
    const id = selectedId;
    if (!id) return;
    const cached = descriptionCache.get(id);
    if (cached && cached.status !== "error") return;
    let cancelled = false;
    descriptionCache.set(id, { status: "loading" });
    bumpDesc();
    apiService
      .getJobDescription(id)
      .then((res) => {
        const text = (res.description ?? "").trim();
        descriptionCache.set(id, text ? { status: "loaded", text } : { status: "empty" });
      })
      .catch(() => {
        descriptionCache.set(id, { status: "error" });
      })
      .finally(() => {
        if (!cancelled) bumpDesc();
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, descRetryTick]);

  const displayedDesc: JobDescriptionState =
    (selectedId ? descriptionCache.get(selectedId) : undefined) ?? { status: "loading" };

  const retryDescription = () => {
    if (!selectedId) return;
    descriptionCache.delete(selectedId);
    setDescRetryTick((t) => t + 1);
  };

  // ---- Chip operations (visual only) --------------------------------------
  const removeFilter = (k: string) => {
    setActiveFilters((prev) => prev.filter((x) => x !== k));
    setMoreFilterChips((prev) => prev.filter((x) => x !== k));
  };
  const addFilter = (k: string) =>
    setActiveFilters((prev) => (prev.includes(k) ? prev : [...prev, k]));
  const clearFilters = () => {
    setActiveFilters([]);
    setMoreFilterChips([]);
  };

  const available = QUICK_FILTERS.filter((f) => !activeFilters.includes(f.key));

  const handleApplyMoreFilters = (state: MoreFiltersState) => {
    setMoreFiltersState(state);
    // Render the picker selections as removable chips in the chip bar.
    // "Any time" is the no-filter default and should not appear as a chip.
    const chips: string[] = [...state.experience];
    if (state.datePosted && state.datePosted !== "Any time") {
      chips.push(state.datePosted);
    }
    setMoreFilterChips(chips);
  };

  // ---- Action handlers ----------------------------------------------------
  // Apply just opens the URL — no auto-tracking. Opening a tab doesn't
  // mean the user submitted an application, so tracking it would create
  // false memory.
  const handleApply = (j: ProtoJob) => {
    if (j.applyUrl) window.open(j.applyUrl, "_blank", "noopener,noreferrer");
  };

  // Auto-apply flow (v2 fire-and-forget):
  //   1. Pro/Elite gate (frontend-side; backend rechecks).
  //   2. POST /submit directly (no prepare/modal step). The job lands in
  //      autoApplyJobs status="queued" and a background worker takes over.
  //   3. The job shows up in the Applications tab (All applications sub-tab)
  //      as an in-flight card. The user can keep clicking Auto-apply on
  //      other jobs while the background workers run.
  //   4. If the worker hits a question with no saved answer, it bails with
  //      status="needs_attention" and the card moves to the Needs your
  //      answers sub-tab. The user resolves via NeedsAttentionDrawer; the
  //      worker resumes.
  //
  // The legacy prepare/modal flow is still wired (showReviewModal +
  // AutoApplyReviewModal) but no longer triggered by the default Auto-apply
  // button — it remains as an advanced preview-only escape hatch.
  const [showProfileModal, setShowProfileModal] = useState(false);
  const [pendingAutoApplyJob, setPendingAutoApplyJob] =
    useState<ProtoJob | null>(null);
  const [showReviewModal, setShowReviewModal] = useState(false);
  const [reviewPrepared, setReviewPrepared] =
    useState<AutoApplyPrepareResponse | null>(null);
  // Job id currently in-flight for auto-apply. Drives the JobDetail button's
  // disabled + spinner state so the user can't double-tap during the network
  // round-trip and stares at a clear "Applying…" affordance.
  const [autoApplyingId, setAutoApplyingId] = useState<string | null>(null);
  // Auto-apply needs a resume on file; nudge routes to /resume.
  const { hasResume } = useResumeStatus();
  const [showResumeNudge, setShowResumeNudge] = useState(false);

  const handleAutoApply = useCallback(async (j: ProtoJob) => {
    if (!j.autoApplyEligible) return;
    if (!user || user.tier === "free") {
      toast({
        title: "Auto-apply is a Pro feature",
        description: "Upgrade to have Offerloop fill the application for you.",
      });
      return;
    }
    // Auto-apply fills the application FROM the resume: hard requirement.
    // No nudge when a resume is already on file (hasResume true or unknown).
    if (hasResume === false) {
      setShowResumeNudge(true);
      return;
    }
    if (autoApplyingId === j.id) return;
    setAutoApplyingId(j.id);

    let res;
    try {
      res = await submitAutoApply(j.id, { dry_run: false, edited_answers: {} });
    } finally {
      setAutoApplyingId(null);
    }

    if (res.ok && res.data.auto_apply_id) {
      toast({
        title: "Applying in the background",
        description: "We'll let you know if a question needs your input.",
      });
      // Optimistic update: hide the job from Discover immediately rather
      // than waiting for the 8s autoAppliedJobIds poll. Avoids the dead
      // window where the user can re-click Auto-apply on the same card.
      // The next poll will confirm + persist the state.
      setAutoAppliedJobIds((prev) => {
        const next = new Set(prev);
        next.add(j.id);
        return next;
      });
      // Stay on Discover so the user can keep clicking Auto-apply on more
      // jobs without the tab yanking them away. The toast + the Needs
      // Attention badge handle notification.
      return;
    }

    const code = (res.data as any)?.code;
    if (code === "PROFILE_REQUIRED" || code === "WORK_AUTH_REQUIRED") {
      setPendingAutoApplyJob(j);
      setShowProfileModal(true);
      if (code === "WORK_AUTH_REQUIRED") {
        toast({
          title: "Work authorization required",
          description: "Set your work-authorization answer to continue.",
        });
      }
      return;
    }
    if (code === "INELIGIBLE") {
      toast({
        title: "Auto-apply unavailable",
        description: "This job's source ATS isn't supported yet.",
      });
      return;
    }
    if (code === "INSUFFICIENT_CREDITS") {
      toast({
        title: "Not enough credits",
        description: "Top up to keep auto-applying.",
        variant: "destructive",
      });
      return;
    }
    if (code === "BROWSERBASE_NOT_CONFIGURED") {
      toast({
        title: "Auto-apply isn't live yet",
        description: "Browserbase isn't configured in this environment.",
        variant: "destructive",
      });
      return;
    }
    toast({
      title: "Couldn't start auto-apply",
      description: (res.data as any)?.error || "Try again in a moment.",
      variant: "destructive",
    });
  }, [user, autoApplyingId, hasResume]);

  const toFindHumansJob = (j: ProtoJob): FindHumansJob => {
    // Reuse the lazily fetched description when it is already loaded; never
    // refetch here and never fall back to filler.
    const cached = descriptionCache.get(j.id);
    return {
      id: j.id,
      title: j.title,
      company: j.company,
      location: j.location,
      description: cached && cached.status === "loaded" ? cached.text : undefined,
      url: j.applyUrl,
    };
  };

  const openFindHumans = (
    j: ProtoJob,
    kind: "employee" | "hiring-manager" = "hiring-manager",
    count = 3,
  ) => {
    setFindHumansKind(kind);
    setFindHumansCount(count);
    setFindHumansJob(toFindHumansJob(j));
  };

  // Minimal converter for rendering Saved / Applied snapshots in JobCard.
  // Live feed enrichment (tags, match signals, salary, description) isn't
  // preserved in the snapshot — those fields default to empty / null.
  const snapshotToProto = (s: SavedJob): ProtoJob => ({
    id: s.job_id,
    section: "recommended",
    title: s.title ?? "",
    company: s.company ?? "",
    logoUrl: s.logo_url ?? null,
    logoMonogram: (s.company ?? "?").charAt(0).toUpperCase(),
    posted: "",
    postedISO: null,
    location: s.location ?? "",
    jobType: "",
    category: "",
    match: s.match_score ?? null,
    matchSignals: [],
    whyLine: "",
    ranked: true,
    salary: null,
    salaryAnnual: null,
    tags: [],
    applyUrl: s.apply_url ?? "",
    atsPlatform: null,
    autoApplyEligible: false,
    isNew: false,
    isStale: false,
    detailPosted: "",
    detailMatch: s.match_score ?? null,
    detailLocation: s.location ?? "",
    structured: undefined,
  });

  const savedProtoJobs = useMemo(
    () => applySearch(savedJobs.map(snapshotToProto), search),
    [savedJobs, search]
  );

  const handleSave = async (j: ProtoJob) => {
    const already = savedIds.has(j.id);
    try {
      if (already) {
        await apiService.unsaveJob(j.id);
        setSavedIds((prev) => {
          const next = new Set(prev);
          next.delete(j.id);
          return next;
        });
        setSavedJobs((prev) => prev.filter((p) => p.job_id !== j.id));
      } else {
        const snap: SavedJob = {
          job_id: j.id,
          title: j.title,
          company: j.company,
          location: j.location,
          apply_url: j.applyUrl,
          match_score: j.match ?? undefined,
          logo_url: j.logoUrl ?? undefined,
        };
        await apiService.saveJob(snap);
        setSavedIds((prev) => new Set(prev).add(j.id));
        setSavedJobs((prev) => [snap, ...prev.filter((p) => p.job_id !== j.id)]);
      }
    } catch (err) {
      console.error("save toggle failed", err);
      toast({
        title: "Couldn't update saved jobs",
        variant: "destructive",
      });
    }
  };

  const handleDismiss = (id: string) => {
    setDismissedIds((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
  };
  const handleUndoDismiss = (id: string) => {
    setDismissedIds((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  };

  // ---- Render -------------------------------------------------------------
  if (authLoading) return <JobBoardSkeleton />;

  return (
    <SidebarProvider>
      <SetupNudgeModal
        open={showResumeNudge}
        variant="resume"
        onClose={() => setShowResumeNudge(false)}
        body="Auto-apply fills the application using your resume, so it needs one on file. Upload it once and every application after that is one click."
      />
      <div className="flex h-screen w-full overflow-hidden">
        <AppSidebar />
        <div className="flex-1 flex flex-col overflow-hidden min-w-0">
          <AppHeader title="Job Board" />
          <div className="flex-1 min-h-0 overflow-hidden">
            <div className="jb-editorial">
              {/* ---- FilterBar (pinned) ---- */}
              <div className="jb-fb">
                <div
                  className="jb-fb-row"
                  style={{ display: "flex", alignItems: "center", justifyContent: "flex-start", gap: 16, flexWrap: "wrap" }}
                >
                  <h1 className="jb-fb-title" style={{ margin: 0 }}>Browse all jobs</h1>
                  {onViewChange && <JobBoardViewToggle view={view} onChange={onViewChange} />}
                </div>

                {/* Top-level tabs — Outbox-style segmented control */}
                <div
                  style={{
                    display: "flex",
                    gap: 4,
                    marginTop: 12,
                    marginBottom: 4,
                    borderBottom: "1px solid var(--line, #E5E5E5)",
                  }}
                >
                  {([
                    { id: "discover", label: "Discover", count: sections.recent.length + sections.recommended.length, dot: false },
                    { id: "saved", label: "Saved", count: savedJobs.length, dot: false },
                    // One Applications tab; the three queues live inside it as
                    // sub-tabs. Notification dot when work is waiting on the user.
                    {
                      id: "applications",
                      label: "Applications",
                      count: needsAttentionCount + needsVerificationCount,
                      dot: needsAttentionCount + needsVerificationCount > 0,
                    },
                  ] as const).map((t) => {
                    const isActive = activeJobTab === t.id;
                    return (
                      <button
                        key={t.id}
                        type="button"
                        onClick={() => setActiveJobTab(t.id)}
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 6,
                          padding: "10px 16px",
                          fontSize: 13,
                          fontWeight: isActive ? 600 : 500,
                          color: isActive ? "var(--brand-blue, #3B82F6)" : "var(--ink-3, #94A3B8)",
                          background: "transparent",
                          border: "none",
                          borderBottom: isActive
                            ? "2px solid var(--brand-blue, #3B82F6)"
                            : "2px solid transparent",
                          marginBottom: -1,
                          cursor: "pointer",
                          fontFamily: "inherit",
                          transition: "color .15s, border-color .15s",
                        }}
                        onMouseEnter={(e) => {
                          if (!isActive) (e.currentTarget as HTMLButtonElement).style.color = "var(--ink-2, #475569)";
                        }}
                        onMouseLeave={(e) => {
                          if (!isActive) (e.currentTarget as HTMLButtonElement).style.color = "var(--ink-3, #94A3B8)";
                        }}
                      >
                        {t.label}
                        {t.dot && (
                          <span
                            style={{
                              display: "inline-block",
                              width: 7,
                              height: 7,
                              borderRadius: "50%",
                              background: "#EF4444",
                              marginLeft: -2,
                            }}
                            aria-label="needs your attention"
                          />
                        )}
                        {t.count > 0 && (
                          <span
                            style={{
                              fontFamily: "'JetBrains Mono', monospace",
                              fontSize: 10,
                              padding: "1px 6px",
                              borderRadius: 4,
                              background: isActive ? "var(--primary-50, #EEF1F9)" : "var(--paper-2, #FAFBFF)",
                              color: isActive ? "var(--accent, #4A60A8)" : "var(--ink-3, #94A3B8)",
                            }}
                          >
                            {t.count}
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>

                <div className="jb-fb-actions" data-tour="tour-job-board-filters">
                  <div className="jb-search">
                    <span className="jb-search-icon"><IconSearch /></span>
                    <input
                      type="text"
                      placeholder="Search for jobs by Name, Company or Title..."
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                    />
                  </div>
                  <button
                    className="jb-fb-btn"
                    type="button"
                    onClick={() => setShowSaveSearch(true)}
                  >
                    <IconSavedBookmark />
                    Save Search
                  </button>
                  <button
                    className="jb-fb-btn"
                    type="button"
                    onClick={() => setShowMoreFilters(true)}
                  >
                    <IconFilter />
                    More Filters
                  </button>
                  <button
                    className="jb-fb-btn"
                    type="button"
                    onClick={() => setAutoApplyOnly((v) => !v)}
                    title={
                      autoApplyOnly
                        ? "Currently showing only jobs with one-click Auto-apply. Click to also show jobs that require manual application (Workday, custom careers pages)."
                        : "Currently showing all jobs. Click to show only jobs with one-click Auto-apply."
                    }
                    style={{
                      background: autoApplyOnly ? "#3B82F6" : undefined,
                      color: autoApplyOnly ? "#fff" : undefined,
                      borderColor: autoApplyOnly ? "#3B82F6" : undefined,
                    }}
                  >
                    <span style={{ marginRight: 4 }}>
                      {autoApplyOnly ? "✓" : ""}
                    </span>
                    Auto-apply only
                  </button>
                  <button
                    className="jb-fb-btn jb-fb-btn-icon"
                    type="button"
                    title="Refresh and reshuffle"
                    onClick={() => loadFeed(true)}
                    disabled={feedLoading}
                  >
                    <IconRefresh />
                  </button>
                </div>

                <div className="jb-chips">
                  <span className="jb-chips-label">Active filters:</span>
                  {(activeFilters.length + moreFilterChips.length) > 0 ? (
                    <>
                      {activeFilters.map((k) => {
                        const def = QUICK_FILTERS.find((q) => q.key === k);
                        const label = def ? def.label : k;
                        return (
                          <span key={`q-${k}`} className="jb-chip">
                            {label}
                            <button
                              type="button"
                              className="jb-chip-close"
                              onClick={() => removeFilter(k)}
                              aria-label={`Remove ${k}`}
                            >
                              <IconClose />
                            </button>
                          </span>
                        );
                      })}
                      {moreFilterChips.map((k) => (
                        <span key={`m-${k}`} className="jb-chip">
                          {k}
                          <button
                            type="button"
                            className="jb-chip-close"
                            onClick={() => removeFilter(k)}
                            aria-label={`Remove ${k}`}
                          >
                            <IconClose />
                          </button>
                        </span>
                      ))}
                      {available.length > 0 && (
                        <div className="jb-chip-add-wrap" ref={addDropdownRef}>
                          <button
                            type="button"
                            className="jb-chip-add"
                            onClick={() => setShowAddDropdown((v) => !v)}
                          >
                            <IconPlus />
                            Add Filter
                          </button>
                          {showAddDropdown && (
                            <div className="jb-dropdown">
                              {available.map((f) => (
                                <button
                                  key={f.key}
                                  type="button"
                                  onClick={() => {
                                    addFilter(f.key);
                                    setShowAddDropdown(false);
                                  }}
                                >
                                  {f.label}
                                </button>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                      <button
                        type="button"
                        className="jb-chip-clear"
                        onClick={clearFilters}
                      >
                        Clear all
                      </button>
                    </>
                  ) : (
                    <div className="jb-chip-add-wrap" ref={addDropdownRef}>
                      <button
                        type="button"
                        className="jb-chip-add"
                        onClick={() => setShowAddDropdown((v) => !v)}
                      >
                        <IconPlus />
                        Add Filter
                      </button>
                      {showAddDropdown && (
                        <div className="jb-dropdown">
                          {QUICK_FILTERS.map((f) => (
                            <button
                              key={f.key}
                              type="button"
                              onClick={() => {
                                addFilter(f.key);
                                setShowAddDropdown(false);
                              }}
                            >
                              {f.label}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </div>

              {/* ---- Body ---- */}
              {/* discover / saved: two-pane editorial layout.
                  applications: full-width queue view with its own sub-tabs. */}
              {activeJobTab === "applications" ? (
                <div style={{ flex: 1, overflowY: "auto", minHeight: 0 }}>
                  {/* Sub-tabs: All / Needs your answers / Finish in browser */}
                  <div style={{ display: "flex", gap: 8, padding: "14px 16px 2px", flexWrap: "wrap" }}>
                    {([
                      { id: "all", label: "All applications", count: 0, alert: false },
                      { id: "needs-answers", label: "Needs your answers", count: needsAttentionCount, alert: true },
                      { id: "finish-browser", label: "Finish in browser", count: needsVerificationCount, alert: false },
                    ] as const).map((t) => {
                      const isActive = appsSubTab === t.id;
                      return (
                        <button
                          key={t.id}
                          type="button"
                          onClick={() => setAppsSubTab(t.id)}
                          aria-pressed={isActive}
                          style={{
                            display: "inline-flex",
                            alignItems: "center",
                            gap: 6,
                            padding: "6px 14px",
                            borderRadius: 999,
                            fontSize: 13,
                            fontWeight: isActive ? 600 : 500,
                            fontFamily: "inherit",
                            cursor: "pointer",
                            border: `1px solid ${isActive ? "var(--brand-blue, #3B82F6)" : "var(--line, #E5E5E5)"}`,
                            background: isActive ? "var(--brand-blue, #3B82F6)" : "var(--paper, #fff)",
                            color: isActive ? "#fff" : "var(--ink-2, #475569)",
                            transition: "background .15s, color .15s, border-color .15s",
                          }}
                        >
                          {t.alert && (
                            <span
                              aria-hidden="true"
                              style={{
                                display: "inline-flex",
                                alignItems: "center",
                                justifyContent: "center",
                                width: 14,
                                height: 14,
                                borderRadius: "50%",
                                fontSize: 10,
                                fontWeight: 700,
                                lineHeight: 1,
                                background: t.count > 0 ? "#EF4444" : (isActive ? "rgba(255,255,255,0.35)" : "#CBD5E1"),
                                color: "#fff",
                              }}
                            >
                              !
                            </span>
                          )}
                          {t.label}
                          {t.count > 0 && (
                            <span
                              style={{
                                fontFamily: "'JetBrains Mono', monospace",
                                fontSize: 10,
                                padding: "1px 6px",
                                borderRadius: 4,
                                background: isActive ? "rgba(255,255,255,0.22)" : "var(--paper-2, #FAFBFF)",
                                color: isActive ? "#fff" : "var(--ink-3, #94A3B8)",
                              }}
                            >
                              {t.count}
                            </span>
                          )}
                        </button>
                      );
                    })}
                  </div>
                  {appsSubTab === "all" ? (
                    <AutoSubmissionTab />
                  ) : appsSubTab === "needs-answers" ? (
                    <NeedsAttentionTab />
                  ) : (
                    <NeedsVerificationTab />
                  )}
                </div>
              ) : (
              <div className="jb-twopane">
                <div className="jb-list" data-tour="tour-job-board-list">
                  {!feedLoading &&
                    feed?.summary?.freshness_label &&
                    feed.summary.freshness_label !== "Unknown" && (
                      <div
                        style={{
                          padding: "12px 12px 0",
                          fontSize: 11,
                          color: "var(--ink-3, #94A3B8)",
                          display: "flex",
                          alignItems: "center",
                          gap: 6,
                        }}
                      >
                        <span>Updated {feed.summary.freshness_label}</span>
                        {feed.cached && (
                          <span style={{ color: "var(--ink-3, #94A3B8)", opacity: 0.7 }}>
                            · cached
                          </span>
                        )}
                      </div>
                    )}

                  {((feedLoading && !isCatalogMode) ||
                    (isCatalogMode && catalogLoading)) && (
                    <>
                      <div className="jb-loading-note">
                        <span className="spin" aria-hidden />
                        Finding the best roles for you — give it a few seconds.
                      </div>
                      <ListSkeleton />
                    </>
                  )}

                  {isCatalogMode && catalogError && (
                    <div className="jb-empty" style={{ margin: "16px 8px" }}>
                      <div className="h">{catalogError}</div>
                      <div className="b">
                        Try again, or remove some filters. The catalog index
                        may still be warming up after a recent deploy.
                      </div>
                    </div>
                  )}

                  {!feedLoading &&
                    !catalogLoading &&
                    !catalogError &&
                    searchedRecent.length === 0 &&
                    searchedRecommended.length === 0 && (
                      <div className="jb-empty" style={{ margin: "16px 8px" }}>
                        <div className="h">No roles match your search.</div>
                        <div className="b">
                          {isCatalogMode
                            ? `Scanned ${catalogScanned} jobs. Try a different keyword or remove a filter.`
                            : "Try clearing the search box or check back after the next pipeline run."}
                        </div>
                      </div>
                    )}

                  {activeJobTab === "discover" && !feedLoading && searchedRecent.length > 0 && (
                    <>
                      <button
                        type="button"
                        className="jb-section-heading jb-section-toggle"
                        onClick={() => toggleSection("recent")}
                        aria-expanded={!collapsedSections.recent}
                      >
                        <span
                          className={`jb-section-chevron ${collapsedSections.recent ? "collapsed" : "expanded"}`}
                          aria-hidden="true"
                        >
                          ▾
                        </span>
                        Recent job postings
                        <span className="jb-section-count">{searchedRecent.length}</span>
                      </button>
                      {!collapsedSections.recent && recentToRender.map((j) => (
                        <JobCard
                          key={j.id}
                          job={j}
                          selected={selectedId === j.id}
                          dismissed={dismissedIds.has(j.id)}
                          onClick={() => !dismissedIds.has(j.id) && setSelectedId(j.id)}
                          onDismiss={() => handleDismiss(j.id)}
                          onUndo={() => handleUndoDismiss(j.id)}
                        />
                      ))}
                    </>
                  )}

                  {activeJobTab === "discover" && !feedLoading && searchedRecommended.length > 0 && (
                    <>
                      <button
                        type="button"
                        className="jb-section-heading jb-section-toggle"
                        onClick={() => toggleSection("recommended")}
                        aria-expanded={!collapsedSections.recommended}
                      >
                        <span
                          className={`jb-section-chevron ${collapsedSections.recommended ? "collapsed" : "expanded"}`}
                          aria-hidden="true"
                        >
                          ▾
                        </span>
                        {isCatalogMode ? "Search results" : "Recommended for you"}
                        <span className="jb-section-count">{searchedRecommended.length}</span>
                        {isCatalogMode && catalogNextCursor && (
                          <span
                            className="jb-section-more"
                            style={{ marginLeft: 8, color: "var(--ink-3, #94A3B8)", fontSize: 12 }}
                          >
                            more available
                          </span>
                        )}
                      </button>
                      {!collapsedSections.recommended && recommendedToRender.map((j) => (
                        <JobCard
                          key={j.id}
                          job={j}
                          selected={selectedId === j.id}
                          dismissed={dismissedIds.has(j.id)}
                          onClick={() => !dismissedIds.has(j.id) && setSelectedId(j.id)}
                          onDismiss={() => handleDismiss(j.id)}
                          onUndo={() => handleUndoDismiss(j.id)}
                        />
                      ))}
                    </>
                  )}

                  {/* Saved tab: list of bookmarked jobs */}
                  {activeJobTab === "saved" && (
                    savedProtoJobs.length > 0 ? (
                      savedProtoJobs.map((j) => (
                        <JobCard
                          key={`saved-${j.id}`}
                          job={j}
                          selected={selectedId === j.id}
                          dismissed={false}
                          onClick={() => setSelectedId(j.id)}
                          // On the Saved tab the X icon unsaves the job
                          // rather than dismissing it locally — the card
                          // exists BECAUSE it's saved, so removing it is
                          // an unsave action.
                          onDismiss={() => handleSave(j)}
                          onUndo={() => {}}
                        />
                      ))
                    ) : (
                      <div style={{ padding: "40px 16px", textAlign: "center", color: "var(--ink-3, #94A3B8)", fontSize: 13 }}>
                        <div style={{ fontSize: 14, fontWeight: 500, color: "var(--ink-2, #475569)", marginBottom: 6 }}>
                          No saved jobs yet
                        </div>
                        Click the bookmark icon on any job to save it here.
                      </div>
                    )
                  )}
                </div>

                <div className="jb-detail">
                  {selectedJob ? (
                    <JobDetail
                      job={selectedJob}
                      description={displayedDesc}
                      onRetryDescription={retryDescription}
                      isSaved={savedIds.has(selectedJob.id)}
                      onApply={() => handleApply(selectedJob)}
                      onAutoApply={
                        selectedJob.autoApplyEligible
                          ? () => handleAutoApply(selectedJob)
                          : undefined
                      }
                      autoApplyLoading={autoApplyingId === selectedJob.id}
                      onSave={() => handleSave(selectedJob)}
                      onShare={() => {
                        if (selectedJob.applyUrl) {
                          navigator.clipboard?.writeText(selectedJob.applyUrl);
                          toast({ title: "Job link copied" });
                        }
                      }}
                      onFindPeople={() => openFindHumans(selectedJob, "hiring-manager")}
                      onFindEmployees={(count) => openFindHumans(selectedJob, "employee", count)}
                      userPlan={user?.tier ?? "free"}
                      currentCredits={creditsView.balance}
                    />
                  ) : feedLoading || catalogLoading ? (
                    <div className="jb-loading-note" style={{ paddingTop: 48 }}>
                      <span className="spin" aria-hidden />
                      Loading jobs — give it a few seconds…
                    </div>
                  ) : (
                    <div className="jb-loading">Select a role to see details.</div>
                  )}
                </div>
              </div>
              )}
            </div>
          </div>
        </div>
      </div>

      <SaveSearchModal
        open={showSaveSearch}
        onClose={() => setShowSaveSearch(false)}
        currentFilters={activeFilters}
      />

      <ApplicationProfileModal
        open={showProfileModal}
        onOpenChange={(open) => {
          setShowProfileModal(open);
          if (!open) setPendingAutoApplyJob(null);
        }}
        onSaved={() => {
          setShowProfileModal(false);
          if (pendingAutoApplyJob) {
            const j = pendingAutoApplyJob;
            setPendingAutoApplyJob(null);
            handleAutoApply(j);
          }
        }}
      />

      <AutoApplyReviewModal
        open={showReviewModal}
        onOpenChange={(open) => {
          setShowReviewModal(open);
          if (!open) setReviewPrepared(null);
        }}
        prepared={reviewPrepared}
        onEditProfile={() => {
          setShowReviewModal(false);
          setShowProfileModal(true);
        }}
      />
      <MoreFiltersPanel
        open={showMoreFilters}
        onClose={() => setShowMoreFilters(false)}
        onApply={handleApplyMoreFilters}
        initial={moreFiltersState ?? undefined}
      />
      <FindHumansModal
        open={!!findHumansJob}
        onOpenChange={(o) => !o && setFindHumansJob(null)}
        job={findHumansJob}
        kind={findHumansKind}
        count={findHumansCount}
      />
    </SidebarProvider>
  );
};

export default JobBoardPage;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// In-pane loading state for the 479px list column. The exported
// JobBoardSkeleton is a 3-col grid built for the old full-width page and
// overflows this narrow pane, so we render lightweight per-card shimmer rows
// shaped like a JobCard instead. Keyframe is injected inline so this stays
// self-contained, mirroring how JobBoardSkeleton ships its own animation.
const ListSkeleton: React.FC<{ rows?: number }> = ({ rows = 6 }) => (
  <div style={{ padding: "24px 4px 8px" }} aria-busy="true" aria-label="Loading roles">
    <style>{`
      @keyframes jbSkelShimmer {
        0% { background-position: -360px 0; }
        100% { background-position: 360px 0; }
      }
    `}</style>
    {[...Array(rows)].map((_, i) => (
      <ListSkeletonRow key={i} index={i} />
    ))}
  </div>
);

const SkelBlock: React.FC<{
  w?: number | string;
  h?: number;
  r?: number;
  delay?: number;
  style?: React.CSSProperties;
}> = ({ w = "100%", h = 12, r = 4, delay = 0, style }) => (
  <div
    style={{
      width: w,
      height: h,
      borderRadius: r,
      background:
        "linear-gradient(90deg, hsl(217 20% 93%) 25%, hsl(217 20% 97%) 50%, hsl(217 20% 93%) 75%)",
      backgroundSize: "720px 100%",
      animation: "jbSkelShimmer 1.6s ease-in-out infinite",
      animationDelay: `${delay}ms`,
      ...style,
    }}
  />
);

const ListSkeletonRow: React.FC<{ index: number }> = ({ index }) => {
  const d = index * 120;
  return (
    <div
      style={{
        display: "flex",
        gap: 12,
        padding: "16px 12px",
        borderBottom: "1px solid var(--line, #E5E5E5)",
      }}
    >
      <SkelBlock w={41} h={41} r={8} delay={d} />
      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 8 }}>
        <SkelBlock w="70%" h={14} delay={d + 40} />
        <SkelBlock w="45%" h={12} delay={d + 80} />
        <div style={{ display: "flex", gap: 8, marginTop: 2 }}>
          <SkelBlock w={64} h={18} r={9} delay={d + 120} />
          <SkelBlock w={48} h={18} r={9} delay={d + 160} />
        </div>
        <SkelBlock w="30%" h={10} delay={d + 200} />
      </div>
    </div>
  );
};

function applySearch(list: ProtoJob[], search: string): ProtoJob[] {
  if (!search.trim()) return list;
  const q = search.trim().toLowerCase();
  return list.filter(
    (j) =>
      j.title.toLowerCase().includes(q) ||
      j.company.toLowerCase().includes(q) ||
      j.location.toLowerCase().includes(q)
  );
}
