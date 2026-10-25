import { useMemo } from "react";
import { useLocation } from "react-router-dom";
import { WalkthroughFlow, safeReturnTo } from "@/components/walkthrough/WalkthroughFlow";
import type { WalkthroughServices } from "./walkthroughServices";

// /get-started: the first-email walkthrough as a full page. The flow itself
// lives in WalkthroughFlow (also shown as a pop-up on /find); exits here
// navigate to ?returnTo= or Find, and Gmail OAuth comes back to this page.

interface GetStartedPageProps {
  /** /dev/get-started-preview: sample people, fake email, no auth, no writes. */
  preview?: boolean;
  /** Test seam; defaults to the live (or preview) services. */
  services?: WalkthroughServices;
}

export default function GetStartedPage({ preview = false, services }: GetStartedPageProps = {}) {
  const location = useLocation();
  const returnTo = useMemo(() => safeReturnTo(new URLSearchParams(location.search).get("returnTo")), [location.search]);
  const gmailReturnPath = `/get-started${returnTo ? `?returnTo=${encodeURIComponent(returnTo)}` : ""}`;
  return <WalkthroughFlow mode="page" preview={preview} services={services} gmailReturnPath={gmailReturnPath} />;
}
