// BrowseJobsPage — the new "browse everything" job board, designed to be the
// post-login landing page. DoorDash-style category carousels on the home;
// "See all", a Browse pill, or a search query drops into a Handshake-style
// filterable, infinite-scrolling grid. Clicking any job opens a detail drawer
// that reuses the existing JobDetail component.
//
// Built entirely on existing pieces: the catalog search endpoint
// (apiService.searchJobs), the personalized feed (getJobFeed) for the
// Recommended row, the shared ProtoJob adapter, JobDetail, and FindHumansModal.
import React, { useCallback, useEffect, useMemo, useState } from "react";

import { AppSidebar } from "@/components/AppSidebar";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppHeader } from "@/components/AppHeader";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { useCreditsView } from "@/hooks/useCreditsView";
import {
  apiService,
  submitAutoApply,
  type JobSearchParams,
  type SavedJob,
} from "@/services/api";
import { toast } from "@/hooks/use-toast";
import { readScoutPrefillEnvelope, SCOUT_PREFILL_EVENT } from "@/lib/scoutBridge";

import type { ProtoJob } from "./jobBoardAdapter";
import { JobDetail, type JobDescriptionState } from "@/components/jobs/JobDetail";
import {
  FindHumansModal,
  type FindHumansJob,
} from "@/components/jobs/FindHumansModal";
import {
  FindContactsModal,
  type FindContactsJob,
} from "@/components/jobs/FindContactsModal";
import { ApplicationProfileModal } from "@/components/jobs/ApplicationProfileModal";
import { IconClose } from "@/components/jobs/icons";
import { JobBoardSearchBar, jobPillClass } from "@/components/jobs/JobBoardSearchBar";
import { JobRow } from "@/components/jobs/browse/JobRow";
import { JobGrid } from "@/components/jobs/browse/JobGrid";
import type { BrowseCardActions } from "@/components/jobs/browse/BrowseJobCard";
import { JobBoardViewToggle, type JobBoardView } from "@/components/jobs/JobBoardViewToggle";
import {
  BROWSE_CATEGORIES,
  BROWSE_PILLS,
  type BrowseCategory,
} from "@/components/jobs/browse/categories";

// Module-scoped description cache, mirroring the redesign board: survives
// opening/closing the drawer within the session. Loaded + empty results are
// cached; errors are not, so a transient failure can be retried.
const descriptionCache = new Map<string, JobDescriptionState>();

interface GridContext {
  title: string;
  params: Partial<JobSearchParams>;
  fromSearch: boolean;
}

interface BrowseJobsPageProps {
  // Supplied when hosted inside the Job Board tab so the page can render the
  // List/Gallery toggle. Standalone use (no props) just hides the toggle.
  view?: JobBoardView;
  onViewChange?: (view: JobBoardView) => void;
}

export const BrowseJobsPage: React.FC<BrowseJobsPageProps> = ({ view = "gallery", onViewChange }) => {
  const { user } = useFirebaseAuth();
  const creditsView = useCreditsView();

  const [searchTerm, setSearchTerm] = useState("");

  // Scout prefill bridge: Scout's "find jobs at X" handoff writes a query
  // envelope addressed to /job-board; dropping it silently made Scout look
  // like it did nothing. Setting searchTerm here runs the search via the
  // existing debounce.
  useEffect(() => {
    const applyFromBridge = () => {
      const env = readScoutPrefillEnvelope("/job-board");
      const q = env && (env.prefill.query || env.prefill.prompt || "").trim();
      if (q) setSearchTerm(q);
    };
    applyFromBridge();
    window.addEventListener(SCOUT_PREFILL_EVENT, applyFromBridge);
    return () => window.removeEventListener(SCOUT_PREFILL_EVENT, applyFromBridge);
  }, []);
  const [grid, setGrid] = useState<GridContext | null>(null);
  const [savedIds, setSavedIds] = useState<Set<string>>(new Set());

  const [detailJob, setDetailJob] = useState<ProtoJob | null>(null);
  const [, setDescVersion] = useState(0);
  const bumpDesc = () => setDescVersion((v) => v + 1);
  const [findHumansJob, setFindHumansJob] = useState<FindHumansJob | null>(null);
  const [findHumansCount, setFindHumansCount] = useState(3);
  const [findContactsJob, setFindContactsJob] = useState<FindContactsJob | null>(null);

  // Auto-apply (mirrors the personalized board's flow): one in-flight job at a
  // time drives the card's "Applying…" state; PROFILE_REQUIRED opens the
  // application-profile modal, then resumes.
  const [autoApplyingId, setAutoApplyingId] = useState<string | null>(null);
  const [showProfileModal, setShowProfileModal] = useState(false);
  const [pendingAutoApplyJob, setPendingAutoApplyJob] = useState<ProtoJob | null>(null);

  // ---- Saved jobs ---------------------------------------------------------
  useEffect(() => {
    if (!user) return;
    apiService
      .listSavedJobs()
      .then((r) => setSavedIds(new Set(r.saved.map((s) => s.job_id))))
      .catch(() => {
        /* non-fatal; user can still browse */
      });
  }, [user]);

  const toggleSave = useCallback(
    async (job: ProtoJob) => {
      const already = savedIds.has(job.id);
      // Optimistic flip.
      setSavedIds((prev) => {
        const next = new Set(prev);
        if (already) next.delete(job.id);
        else next.add(job.id);
        return next;
      });
      try {
        if (already) {
          await apiService.unsaveJob(job.id);
        } else {
          const snap: SavedJob = {
            job_id: job.id,
            title: job.title,
            company: job.company,
            location: job.location,
            apply_url: job.applyUrl,
            match_score: job.match ?? undefined,
            logo_url: job.logoUrl ?? undefined,
          };
          await apiService.saveJob(snap);
        }
      } catch {
        // Roll back on failure.
        setSavedIds((prev) => {
          const next = new Set(prev);
          if (already) next.add(job.id);
          else next.delete(job.id);
          return next;
        });
        toast({ title: "Couldn't update saved jobs", variant: "destructive" });
      }
    },
    [savedIds]
  );

  // ---- Grid navigation ----------------------------------------------------
  const openGridForCategory = useCallback((category: BrowseCategory) => {
    // The Recommended row deep-links to the existing personalized board.
    if (category.special === "recommended") {
      window.location.assign("/job-board");
      return;
    }
    setGrid({ title: category.label, params: category.params, fromSearch: false });
  }, []);

  const backToRows = useCallback(() => {
    setGrid(null);
    setSearchTerm("");
  }, []);

  // Debounced search → grid mode. Clearing the box returns to the rows home
  // (only when the active grid came from search, so it doesn't yank the user
  // out of a category grid).
  useEffect(() => {
    const t = searchTerm.trim();
    const handle = window.setTimeout(() => {
      if (t) {
        setGrid({ title: `Results for "${t}"`, params: { q: t }, fromSearch: true });
      } else {
        setGrid((g) => (g && g.fromSearch ? null : g));
      }
    }, 350);
    return () => window.clearTimeout(handle);
  }, [searchTerm]);

  // ---- Detail drawer description (lazy) -----------------------------------
  useEffect(() => {
    const id = detailJob?.id;
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
      .catch(() => descriptionCache.set(id, { status: "error" }))
      .finally(() => {
        if (!cancelled) bumpDesc();
      });
    return () => {
      cancelled = true;
    };
  }, [detailJob]);

  const displayedDesc: JobDescriptionState =
    (detailJob ? descriptionCache.get(detailJob.id) : undefined) ?? { status: "loading" };

  const retryDescription = () => {
    if (!detailJob) return;
    descriptionCache.delete(detailJob.id);
    bumpDesc();
  };

  const handleApply = (j: ProtoJob) => {
    if (j.applyUrl) window.open(j.applyUrl, "_blank", "noopener,noreferrer");
  };

  // Employee search only; the hiring team goes through FindContactsModal.
  const openFindEmployees = useCallback(
    (j: ProtoJob, count = 3) => {
      const cached = descriptionCache.get(j.id);
      setFindHumansCount(count);
      setFindHumansJob({
        id: j.id,
        title: j.title,
        company: j.company,
        location: j.location,
        description: cached && cached.status === "loaded" ? cached.text : undefined,
        url: j.applyUrl,
      });
    },
    []
  );

  const handleAutoApply = useCallback(
    async (j: ProtoJob) => {
      if (!j.autoApplyEligible) return;
      if (!user || user.tier === "free") {
        toast({
          title: "Auto-apply is a Pro feature",
          description: "Upgrade to have Offerloop fill the application for you.",
        });
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
        return;
      }

      const code = (res.data as { code?: string })?.code;
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
        toast({ title: "Auto-apply unavailable", description: "This job's source ATS isn't supported yet." });
        return;
      }
      if (code === "INSUFFICIENT_CREDITS") {
        toast({ title: "Not enough credits", description: "Top up to keep auto-applying.", variant: "destructive" });
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
        description: (res.data as { error?: string })?.error || "Try again in a moment.",
        variant: "destructive",
      });
    },
    [user, autoApplyingId]
  );

  // Handler bundle threaded down to every card (carousel + grid).
  const cardActions = useMemo<BrowseCardActions>(
    () => ({
      onOpen: setDetailJob,
      onToggleSave: toggleSave,
      onFindContacts: (j) => setFindContactsJob({ id: j.id, title: j.title, company: j.company }),
      onAutoApply: handleAutoApply,
    }),
    [toggleSave, handleAutoApply]
  );

  const pillActiveKey = useMemo(() => {
    if (!grid || grid.fromSearch) return null;
    const match = BROWSE_PILLS.find((c) => c.label === grid.title);
    return match?.key ?? null;
  }, [grid]);

  return (
    <SidebarProvider>
      {/* Shared shimmer keyframe used by the row/grid skeletons. */}
      <style>{`@keyframes jbSkelShimmer {0%{background-position:-360px 0}100%{background-position:360px 0}}`}</style>
      <div className="flex h-screen w-full overflow-hidden">
        <AppSidebar />
        <div className="flex-1 flex flex-col overflow-hidden min-w-0">
          <AppHeader title="" />

          {/* Search + browse pills (pinned) */}
          <div
            style={{
              flexShrink: 0,
              padding: "20px 28px 12px",
              borderBottom: "1px solid var(--line, #E5E5E5)",
              background: "var(--paper, #fff)",
            }}
          >
            {/* Same search treatment as Find: logo + pill search bar, then the
                filter pills underneath. The search itself is live (debounced
                above). */}
            <div className="space-y-3">
              <JobBoardSearchBar value={searchTerm} onChange={setSearchTerm} />

              <div className="flex items-center gap-2 overflow-x-auto pb-1">
                <span className={jobPillClass(true)} aria-hidden>
                  Jobs
                </span>
                <span className="mx-1 h-7 w-px shrink-0 bg-line" />
                {BROWSE_PILLS.map((c) => {
                  const active = pillActiveKey === c.key;
                  return (
                    <button
                      key={c.key}
                      type="button"
                      aria-pressed={active}
                      onClick={() => (active ? backToRows() : openGridForCategory(c))}
                      className={jobPillClass(active)}
                    >
                      {c.label}
                    </button>
                  );
                })}
                {onViewChange && (
                  <div className="ml-auto shrink-0 pl-2">
                    <JobBoardViewToggle view={view} onChange={onViewChange} />
                  </div>
                )}
              </div>
            </div>
          </div>

          {/* Body: rows home or grid */}
          <div style={{ flex: 1, overflowY: "auto", minHeight: 0, padding: "24px 28px" }}>
            {grid ? (
              <JobGrid
                title={grid.title}
                baseParams={grid.params}
                savedIds={savedIds}
                actions={cardActions}
                autoApplyingId={autoApplyingId}
                onBack={backToRows}
              />
            ) : (
              BROWSE_CATEGORIES.map((c) => (
                <JobRow
                  key={c.key}
                  category={c}
                  savedIds={savedIds}
                  actions={cardActions}
                  autoApplyingId={autoApplyingId}
                  onSeeAll={openGridForCategory}
                />
              ))
            )}
          </div>
        </div>
      </div>

      {/* Detail drawer */}
      {detailJob && (
        <>
          <div
            onClick={() => setDetailJob(null)}
            style={{
              position: "fixed",
              inset: 0,
              background: "rgba(15,23,42,0.35)",
              zIndex: 60,
            }}
          />
          <div
            style={{
              position: "fixed",
              top: 0,
              right: 0,
              bottom: 0,
              width: "min(560px, 100%)",
              background: "var(--paper, #fff)",
              zIndex: 61,
              boxShadow: "-8px 0 30px rgba(15,23,42,0.15)",
              overflowY: "auto",
            }}
          >
            <div style={{ display: "flex", justifyContent: "flex-end", padding: "12px 12px 0" }}>
              <button
                type="button"
                onClick={() => setDetailJob(null)}
                aria-label="Close"
                style={{ border: "none", background: "none", cursor: "pointer", color: "var(--ink-2, #475569)" }}
              >
                <IconClose />
              </button>
            </div>
            <JobDetail
              job={detailJob}
              description={displayedDesc}
              onRetryDescription={retryDescription}
              isSaved={savedIds.has(detailJob.id)}
              onApply={() => handleApply(detailJob)}
              onSave={() => toggleSave(detailJob)}
              onShare={() => {
                if (detailJob.applyUrl) {
                  navigator.clipboard?.writeText(detailJob.applyUrl);
                  toast({ title: "Job link copied" });
                }
              }}
              onFindPeople={() =>
                setFindContactsJob({ id: detailJob.id, title: detailJob.title, company: detailJob.company })
              }
              onFindEmployees={(count) => openFindEmployees(detailJob, count)}
              onAutoApply={
                detailJob.autoApplyEligible ? () => handleAutoApply(detailJob) : undefined
              }
              autoApplyLoading={autoApplyingId === detailJob.id}
              userPlan={user?.tier ?? "free"}
              currentCredits={creditsView.balance}
            />
          </div>
        </>
      )}

      <FindHumansModal
        open={!!findHumansJob}
        onOpenChange={(o) => !o && setFindHumansJob(null)}
        job={findHumansJob}
        kind="employee"
        count={findHumansCount}
      />

      <FindContactsModal
        open={!!findContactsJob}
        onOpenChange={(o) => !o && setFindContactsJob(null)}
        job={findContactsJob}
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
    </SidebarProvider>
  );
};

export default BrowseJobsPage;
