import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiService } from "@/services/api";
import { canSendWith } from "@/pages/walkthroughServices";
import { CONTACTS_KEY, THREADS_KEY } from "@/components/contacts/contactsModel";
import { loadWalkthrough, saveWalkthrough } from "@/services/walkthrough";
import { useFirstEmailTour } from "./useFirstEmailTour";
import { ALL_SET_VIEW, TOUR_GMAIL_BUTTON, contactsTourView } from "./contactsTourSteps";
import type { ScoutTourProps } from "./ScoutTour";

// The tour's Contacts half: review the draft, connect Gmail (a round-trip
// through Google that reloads this page), push the draft into Gmail drafts,
// then open it from the conversation's Gmail button. Everything that leaves
// the browser comes in as deps so tests make no calls.

type PushResult = Awaited<ReturnType<typeof apiService.pushContactGmailDraft>>;

export interface ContactsTourDeps {
  load: typeof loadWalkthrough;
  save: typeof saveWalkthrough;
  gmailStatus: () => Promise<{ connected: boolean; scopes?: string[] }>;
  pushDraft: (contactId: string) => Promise<PushResult>;
  startOAuth: (returnTo: string) => Promise<string>;
  /** Leave for Google. */
  redirect: (url: string) => void;
  /** Wait before the one retry when another push holds the draft. */
  retryDelayMs: number;
  /** How long "You're all set" stays up. */
  allSetMs: number;
}

const LIVE: ContactsTourDeps = {
  load: loadWalkthrough,
  save: saveWalkthrough,
  gmailStatus: () => apiService.gmailStatus(),
  pushDraft: (id) => apiService.pushContactGmailDraft(id),
  startOAuth: (returnTo) => apiService.startGmailOAuth(returnTo),
  redirect: (url) => {
    window.location.href = url;
  },
  retryDelayMs: 1500,
  allSetMs: 2000,
};

const CONTACTS_STEPS = ["review", "gmail", "open"];
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export function useContactsTour(
  uid: string | undefined,
  url: { searchParams: URLSearchParams; updateParams: (patch: Record<string, string | null>) => void },
  deps: Partial<ContactsTourDeps> = {},
) {
  const depsRef = useRef<ContactsTourDeps>({ ...LIVE, ...deps });
  depsRef.current = { ...LIVE, ...deps };
  const tour = useFirstEmailTour(uid, { load: depsRef.current.load, save: depsRef.current.save });
  const { goTo, complete, skip, close } = tour;
  const client = useQueryClient();
  const { searchParams, updateParams } = url;
  const updateRef = useRef(updateParams);
  updateRef.current = updateParams;

  const contactId = tour.state?.tourContactId;
  const firstName = tour.state?.tourPersonFirstName || "";
  const onContacts = tour.active && !!contactId && CONTACTS_STEPS.includes(tour.step ?? "");
  const selected = searchParams.get("contact");
  const shown = onContacts && selected === contactId;

  const [pushing, setPushing] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [gmailError, setGmailError] = useState<string | null>(null);
  const [pushError, setPushError] = useState<string | null>(null);
  const [allSet, setAllSet] = useState(false);
  // Gmail can already take drafts (the gmail step opened without an OAuth return).
  const [connected, setConnected] = useState(false);

  // Mid-tour with nobody selected (e.g. /contacts typed in): show the tour's contact.
  useEffect(() => {
    if (onContacts && !selected && contactId) updateRef.current({ contact: contactId });
  }, [onContacts, selected, contactId]);

  const canSend = useCallback(async () => {
    try {
      const s = await depsRef.current.gmailStatus();
      return !!s.connected && canSendWith(s.scopes);
    } catch {
      return false;
    }
  }, []);

  const push = useCallback(async () => {
    if (!contactId) return;
    const d = depsRef.current;
    setPushing(true);
    setGmailError(null);
    setPushError(null);
    try {
      let res = await d.pushDraft(contactId).catch((): PushResult => ({ ok: false, reason: "error" }));
      if (!res.ok && res.reason === "draft_in_progress") {
        await sleep(d.retryDelayMs);
        res = await d.pushDraft(contactId).catch((): PushResult => ({ ok: false, reason: "error" }));
      }
      if (res.ok) {
        // Refresh first, so the Gmail button already links to the draft when it's spotlighted.
        await Promise.all([
          client.invalidateQueries({ queryKey: CONTACTS_KEY }),
          client.invalidateQueries({ queryKey: THREADS_KEY }),
        ]);
        await goTo("open");
        return;
      }
      if (res.reason === "gmail_not_connected" || res.reason === "gmail_scope_missing") setGmailError(res.reason);
      else setPushError(res.reason);
      await goTo("gmail");
    } finally {
      setPushing(false);
    }
  }, [contactId, client, goTo]);

  // Back from Google: Contacts reloads on the gmail step with connected=gmail or gmail_error.
  const handledReturn = useRef(false);
  const returned =
    searchParams.get("tour") === "gmail" && (searchParams.get("connected") === "gmail" || searchParams.has("gmail_error"));
  useEffect(() => {
    if (!tour.loaded || !returned || handledReturn.current) return;
    handledReturn.current = true;
    const error = searchParams.get("gmail_error");
    updateRef.current({ tour: null, connected: null, gmail_error: null });
    if (!tour.active || tour.step !== "gmail" || !contactId) return;
    if (error) {
      setGmailError(error);
      return;
    }
    void (async () => {
      setPushing(true);
      if (await canSend()) {
        await push();
      } else {
        setPushing(false);
        setGmailError("gmail_scope_missing");
      }
    })();
  }, [tour.loaded, returned, tour.active, tour.step, contactId, searchParams, canSend, push]);

  // The gmail step without an OAuth return (e.g. a reload after a failed
  // push): if Gmail can already take drafts, offer the push, not Connect.
  const checkedGmail = useRef(false);
  const onGmailStep = shown && tour.step === "gmail";
  useEffect(() => {
    if (!onGmailStep || returned || checkedGmail.current) return;
    checkedGmail.current = true;
    let cancelled = false;
    void canSend().then((ok) => {
      if (!cancelled && ok) setConnected(true);
    });
    return () => {
      cancelled = true;
      checkedGmail.current = false;
    };
  }, [onGmailStep, returned, canSend]);

  // "Looks good": already able to draft in Gmail goes straight to the push.
  const reviewed = useCallback(async () => {
    setPushing(true);
    if (await canSend()) {
      await push();
      return;
    }
    setPushing(false);
    await goTo("gmail");
  }, [canSend, push, goTo]);

  const connect = useCallback(async () => {
    if (!contactId) return;
    setConnecting(true);
    try {
      const authUrl = await depsRef.current.startOAuth(`/contacts?contact=${encodeURIComponent(contactId)}&tour=gmail`);
      depsRef.current.redirect(authUrl);
    } catch {
      setConnecting(false);
    }
  }, [contactId]);

  // The spotlighted Gmail button keeps its own link (new tab); this only listens.
  const opening = shown && tour.step === "open";
  useEffect(() => {
    if (!opening) return;
    const onClick = (e: MouseEvent) => {
      if (!(e.target instanceof Element) || !e.target.closest(TOUR_GMAIL_BUTTON)) return;
      setAllSet(true);
      void complete();
    };
    document.addEventListener("click", onClick, true);
    return () => document.removeEventListener("click", onClick, true);
  }, [opening, complete]);

  useEffect(() => {
    if (!allSet) return;
    const t = setTimeout(() => setAllSet(false), depsRef.current.allSetMs);
    return () => clearTimeout(t);
  }, [allSet]);

  let view: ScoutTourProps | null = null;
  if (allSet) {
    const done = () => setAllSet(false);
    view = { ...ALL_SET_VIEW, onSkip: done, onClose: done };
  } else if (shown) {
    const v = contactsTourView(tour.step, { firstName, gmailError, pushing, pushError, connected });
    if (v) {
      const onAction = tour.step === "review" ? reviewed : (pushError || connected) && !gmailError ? push : connect;
      view = {
        target: v.target,
        title: v.title,
        body: v.body,
        action: v.action
          ? { label: v.action.label, disabled: v.action.disabled || connecting, onClick: () => void onAction() }
          : undefined,
        secondary: v.secondary ? { label: v.secondary.label, onClick: () => void close() } : undefined,
        onSkip: () => void skip(),
        onClose: () => void close(),
      };
    }
  }

  return {
    loaded: tour.loaded,
    view,
    /**
     * Don't auto-pick or fall back yet: the tour may want its own contact, or
     * its contact may not be in a still-refreshing list.
     */
    holdSelection: (!!uid && !tour.loaded) || (onContacts && (!selected || selected === contactId)),
  };
}
