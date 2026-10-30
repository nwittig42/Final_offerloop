import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation } from "react-router-dom";
import {
  loadWalkthrough,
  saveWalkthrough,
  shouldAutoOpen,
  walkthroughIncomplete,
  type WalkthroughState,
} from "@/services/walkthrough";
import type { WalkthroughExit } from "./WalkthroughFlow";

// The first-email pop-up on Find. It opens by itself once for a new user
// (until they close or skip it), and again on the Gmail OAuth return
// (/find?walkthrough=1) if it isn't finished. While it's open, or before we know whether it will
// open, Find's feed holds off on paid lookups.

export function useFindWalkthrough(
  uid: string | undefined,
  deps: { load?: typeof loadWalkthrough; save?: typeof saveWalkthrough } = {},
) {
  const load = deps.load ?? loadWalkthrough;
  const save = deps.save ?? saveWalkthrough;
  const loadRef = useRef(load);
  loadRef.current = load;
  const saveRef = useRef(save);
  saveRef.current = save;

  const location = useLocation();
  // The Gmail return marker, as it was when Find opened (the flow cleans it up).
  const gmailReturn = useRef(new URLSearchParams(location.search).get("walkthrough") === "1");

  const [state, setState] = useState<WalkthroughState | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [open, setOpen] = useState(false);
  const stateRef = useRef(state);
  stateRef.current = state;

  useEffect(() => {
    if (!uid) return;
    let cancelled = false;
    setLoaded(false);
    loadRef
      .current(uid)
      .catch(() => null)
      .then((s) => {
        if (cancelled) return;
        setState(s);
        // A Gmail return reopens it only while it's unfinished.
        setOpen(!!s && walkthroughIncomplete(s) && (shouldAutoOpen(s) || gmailReturn.current));
        setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [uid]);

  const openNow = useCallback(() => setOpen(true), []);

  const exit = useCallback(
    (why: WalkthroughExit) => {
      setOpen(false);
      if (!uid) return;
      const now = new Date().toISOString();
      // The flow saved its own progress while it was open; build on that, not
      // on what Find loaded, so closing never rolls the walkthrough back.
      void loadRef
        .current(uid)
        .catch(() => null)
        .then((latest) => {
          const base = latest ?? stateRef.current;
          if (!base) return;
          const next: WalkthroughState =
            why === "skip" ? { ...base, skippedAt: base.skippedAt ?? now } : { ...base, closedAt: base.closedAt ?? now };
          setState(next);
          saveRef.current(uid, next).catch(() => {});
        });
    },
    [uid],
  );

  return { state, loaded, open, openNow, exit, holdPaidLookups: !loaded || open };
}
