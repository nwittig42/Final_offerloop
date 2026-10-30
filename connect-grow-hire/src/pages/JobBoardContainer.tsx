// JobBoardContainer — the /job-board route. Owns the List vs Gallery view
// choice and renders the matching board. Both boards keep their own app shell;
// only one mounts at a time. The view is read from the URL (?view=) first so
// it's shareable and back-button friendly, then falls back to the user's last
// choice in localStorage (default: gallery — the browse-everything cards).
import React, { Suspense } from "react";
import { useSearchParams } from "react-router-dom";

import { JobBoardSkeleton } from "@/components/JobBoardSkeleton";
import type { JobBoardView } from "@/components/jobs/JobBoardViewToggle";

const JobBoardRedesign = React.lazy(() => import("./JobBoardPage.redesign"));
const BrowseJobsPage = React.lazy(() => import("./BrowseJobsPage"));

const STORAGE_KEY = "jobBoardView";

function readStoredView(): JobBoardView {
  try {
    return localStorage.getItem(STORAGE_KEY) === "list" ? "list" : "gallery";
  } catch {
    return "gallery";
  }
}

export default function JobBoardContainer() {
  const [params, setParams] = useSearchParams();
  const urlView = params.get("view");
  const view: JobBoardView =
    urlView === "gallery" || urlView === "list" ? urlView : readStoredView();

  const onViewChange = (next: JobBoardView) => {
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* storage unavailable; URL still drives the view */
    }
    const p = new URLSearchParams(params);
    p.set("view", next);
    setParams(p, { replace: true });
  };

  return (
    <Suspense fallback={<JobBoardSkeleton />}>
      {view === "gallery" ? (
        <BrowseJobsPage view={view} onViewChange={onViewChange} />
      ) : (
        <JobBoardRedesign view={view} onViewChange={onViewChange} />
      )}
    </Suspense>
  );
}
