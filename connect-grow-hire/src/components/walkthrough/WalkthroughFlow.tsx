import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ArrowLeft, Check, ExternalLink, Loader2 } from "lucide-react";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { trackFeatureActionCompleted, trackFirstOutreachFunnel, type FirstOutreachFunnelEvent } from "@/lib/analytics";
import { auth } from "@/lib/firebase";
import {
  REVEAL_COST,
  WALKTHROUGH_VERSION,
  contactIdFor,
  matchReasons,
  normalizeWalkthrough,
  pickMatches,
  type EmailDraft,
  type MatchProfile,
  type WalkthroughMatch,
  type WalkthroughState,
} from "@/services/walkthrough";
import {
  createPreviewWalkthroughServices,
  liveWalkthroughServices,
  type GmailState,
  type SendReview,
  type WalkthroughServices,
} from "@/pages/walkthroughServices";
import { OB, obFieldLabel, obInput, obPrimaryButton, obFocus } from "@/pages/onboardingTheme";
import OfferloopLogo from "@/assets/offerloop_logo2_trimmed.png";
import { MatchGallery, ProfilePortrait, initialsOf } from "./MatchCards";

// First-use walkthrough (onboarding v2). Onboarding hands off here: a few
// matched people from different companies, pick one, see why they fit, then
// (only on an explicit click) find their email and write an editable first
// email. Sending is on the same screen: connect Gmail if needed (the OAuth
// round trip comes back to this draft), then Send, then an explicit confirm.
// The backend sends exactly the saved draft that was confirmed, once.
// Progress is saved on users/{uid}.walkthrough, the draft on the contact
// doc, so leaving at any point resumes in place. Nothing here blocks the
// app: every state has a way out to Find.
//
// Rendered two ways: as the /get-started page (mode "page": exits navigate)
// and inside the first-email pop-up on /find (mode "modal": exits call
// onExit, and a sent email shows a success screen in place).

export const safeReturnTo = (raw: string | null): string => {
  if (!raw) return "";
  return raw.startsWith("/") && !raw.startsWith("//") && !raw.startsWith("/onboarding") && !raw.startsWith("/get-started")
    ? raw
    : "";
};

const PREVIEW_UID = "preview";
const EMPTY_RETRY_MS = 3000;

type Notice = { tone: "info" | "error" | "success"; text: string } | null;
type SendPhase = "idle" | "reviewing" | "confirm" | "sending" | "unconfirmed";

const GMAIL_SENT_URL = "https://mail.google.com/mail/u/0/#sent";

// ?gmail_error= codes from the OAuth callback.
const GMAIL_ERROR_COPY: Record<string, string> = {
  scopes_declined:
    "Gmail didn't get every permission it asked for, so we can't send for you yet. Try again and leave all the boxes checked. Your draft is saved.",
  not_test_user: "This Google account can't be connected yet. Try another account, or save and finish later.",
};
const GMAIL_ERROR_DEFAULT = "Gmail didn't connect. Your draft is saved, so you can try again.";

const BLOCKING_COPY: Record<string, string> = {
  no_recipient: "We don't have an email address for them. Pick someone else.",
  empty_subject: "Add a subject line.",
  empty_body: "Your email is empty.",
  placeholder: "Replace placeholder text like [Name] before sending.",
};
const WARNING_COPY: Record<string, string> = {
  too_short: "It's on the short side. A sentence about why them helps.",
  too_long: "It's a bit long. Shorter emails tend to get more replies.",
  no_clear_ask: "Ending with a clear ask, like a 15-minute call, gets more replies.",
  no_specificity: "A detail about them (their team, path or school) makes it feel personal.",
  weak_subject: "A specific subject line gets opened more often.",
  subject_no_contact_noun: "Mentioning them or their company in the subject helps it get opened.",
};

const notStyle = (tone: NonNullable<Notice>["tone"]): React.CSSProperties => ({
  fontSize: 14,
  lineHeight: 1.5,
  borderRadius: 10,
  padding: "10px 14px",
  margin: "0 0 14px",
  background: tone === "error" ? "#FEF2F2" : tone === "success" ? "#ECFDF5" : OB.primary50,
  color: tone === "error" ? "#991B1B" : tone === "success" ? "#065F46" : OB.ink2,
});
type SaveStatus = "idle" | "saving" | "saved" | "error";

export type WalkthroughExit = "skip" | "later" | "find";

export interface WalkthroughFlowProps {
  /** /dev/get-started-preview: sample people, fake email, no auth, no writes. */
  preview?: boolean;
  /** Test seam; defaults to the live (or preview) services. */
  services?: WalkthroughServices;
  /** Where Google sends the user back after Gmail OAuth (relative path). */
  gmailReturnPath: string;
  /** "page": exits navigate like today. "modal": exits call onExit; a sent email shows the in-place success screen. */
  mode: "page" | "modal";
  onExit?: (why: WalkthroughExit) => void;
}

const secondaryButton: React.CSSProperties = {
  ...obPrimaryButton,
  background: "#fff",
  color: OB.primary,
  border: `1px solid ${OB.primary200}`,
};

const linkButton: React.CSSProperties = {
  background: "none",
  border: "none",
  padding: 0,
  cursor: "pointer",
  color: OB.ink3,
  fontWeight: 600,
  fontSize: 14,
  fontFamily: OB.fontBody,
};

const Avatar = ({ match, size }: { match: WalkthroughMatch; size: number }) => {
  const [broken, setBroken] = useState(false);
  const style: React.CSSProperties = {
    width: size,
    height: size,
    borderRadius: "50%",
    flexShrink: 0,
    background: OB.primary100,
    color: OB.primary,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    fontWeight: 700,
    fontSize: size * 0.36,
    objectFit: "cover",
  };
  return match.photoUrl && !broken ? (
    <img src={match.photoUrl} alt="" style={style} onError={() => setBroken(true)} />
  ) : (
    <span aria-hidden style={style}>{initialsOf(match.name)}</span>
  );
};

const STEPS = ["Pick a person", "Your email", "Send it"];

const Progress = ({ index }: { index: number }) => (
  <ol aria-label="Progress" style={{ display: "flex", gap: 8, listStyle: "none", padding: 0, margin: "0 0 24px", flexWrap: "wrap" }}>
    {STEPS.map((label, i) => (
      <li
        key={label}
        aria-current={i === index ? "step" : undefined}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          fontSize: 13,
          fontWeight: i === index ? 700 : 500,
          color: i <= index ? OB.primary : OB.ink4,
        }}
      >
        <span
          style={{
            width: 20,
            height: 20,
            borderRadius: "50%",
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 11,
            background: i < index ? OB.primary : i === index ? OB.primary100 : "#fff",
            color: i < index ? "#fff" : "inherit",
            border: `1px solid ${i <= index ? OB.primary : OB.border}`,
          }}
        >
          {i < index ? <Check size={11} strokeWidth={3} /> : i + 1}
        </span>
        {label}
        {i < STEPS.length - 1 && <span aria-hidden style={{ color: OB.ink4, marginLeft: 2 }}>·</span>}
      </li>
    ))}
  </ol>
);

export function WalkthroughFlow({ preview = false, services: injected, gmailReturnPath, mode, onExit }: WalkthroughFlowProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const { user } = useFirebaseAuth();
  const services = useMemo(
    () => injected ?? (preview ? createPreviewWalkthroughServices() : liveWalkthroughServices),
    [injected, preview],
  );
  const uid = preview ? PREVIEW_UID : user?.uid;
  const returnTo = useMemo(() => safeReturnTo(new URLSearchParams(location.search).get("returnTo")), [location.search]);
  // The preview has no account, so its exits go to the public home page.
  const exitTo = preview ? "/" : returnTo || "/find";
  const firstName = preview ? "Jordan" : (user?.name || "").trim().split(/\s+/)[0];
  // Every way out of the walkthrough. The pop-up's owner decides what an
  // exit means (and records it); the page just navigates.
  const onExitRef = useRef(onExit);
  onExitRef.current = onExit;
  const leave = useCallback(
    (why: WalkthroughExit, opts?: { replace?: boolean }) => {
      if (mode === "modal") onExitRef.current?.(why);
      else navigate(exitTo, opts);
    },
    [mode, navigate, exitTo],
  );

  const [state, setState] = useState<WalkthroughState | null>(null);
  const [profile, setProfile] = useState<MatchProfile | null>(null);
  const [loadingMatches, setLoadingMatches] = useState(false);
  const [matchesFailed, setMatchesFailed] = useState(false);
  const [preparing, setPreparing] = useState<"email" | "writing" | null>(null);
  const [draft, setDraft] = useState<EmailDraft | null>(null);
  const [draftFailed, setDraftFailed] = useState(false);
  // The server has no email model configured (a setup problem, not a bad draft).
  const [draftNotConfigured, setDraftNotConfigured] = useState(false);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>("idle");
  const [notice, setNotice] = useState<Notice>(null);
  // The one-time free first outreach (backend decides); null until known.
  const [freeEligible, setFreeEligibleState] = useState<boolean | null>(null);
  // prepare() can run before the status arrives (resuming after a refresh),
  // so it awaits the same request instead of reading possibly-stale state.
  const freeRef = useRef<Promise<boolean> | null>(null);
  const setFreeEligible = useCallback((v: boolean) => {
    freeRef.current = Promise.resolve(v);
    setFreeEligibleState(v);
  }, []);
  const [gmail, setGmail] = useState<GmailState | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [sendPhase, setSendPhase] = useState<SendPhase>("idle");
  const [review, setReview] = useState<SendReview | null>(null);
  const [sendNotice, setSendNotice] = useState<Notice>(null);
  const [sentTo, setSentTo] = useState<string | null>(null);
  const [sentContactId, setSentContactId] = useState<string | null>(null);

  const track = useCallback(
    (action: string, success = true) => {
      if (!preview) trackFeatureActionCompleted("walkthrough", action, success);
    },
    [preview],
  );
  // Signup → first email funnel, with timestamps. No email text or profile data.
  const funnel = useCallback(
    (event: FirstOutreachFunnelEvent, props: Record<string, string | number | boolean | null> = {}) => {
      if (!preview) trackFirstOutreachFunnel(event, auth.currentUser?.metadata?.creationTime, props);
    },
    [preview],
  );

  // Coming back from Google's consent screen: remember the outcome, then
  // clean the URL (keeping returnTo and any other params, minus the pop-up's
  // own ?walkthrough= marker). It's acted on once the draft is shown.
  const oauthReturn = useRef<{ connected: boolean; error: string | null } | null>(null);
  useEffect(() => {
    const sp = new URLSearchParams(location.search);
    if (!sp.has("connected") && !sp.has("gmail_error")) return;
    oauthReturn.current = { connected: sp.get("connected") === "gmail", error: sp.get("gmail_error") };
    sp.delete("connected");
    sp.delete("gmail_error");
    sp.delete("walkthrough");
    const rest = sp.toString();
    navigate({ pathname: location.pathname, search: rest ? `?${rest}` : "" }, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // State changes are saved but never wait on the save: a failed write just
  // means a refresh resumes a step earlier.
  const stateRef = useRef<WalkthroughState | null>(null);
  const update = useCallback(
    (patch: Partial<WalkthroughState>) => {
      const base = stateRef.current ?? normalizeWalkthrough(null);
      const next: WalkthroughState = { ...base, ...patch, version: WALKTHROUGH_VERSION };
      stateRef.current = next;
      setState(next);
      if (uid) services.saveState(uid, next).catch((e) => console.warn("[walkthrough] progress save failed", e));
      return next;
    },
    [uid, services],
  );

  // ── Matches ──────────────────────────────────────────────────────────────
  const fetchMatches = useCallback(
    async (exclude: string[] = []) => {
      setLoadingMatches(true);
      setMatchesFailed(false);
      setNotice(null);
      try {
        let res = await services.loadPeople(exclude);
        // Right after onboarding the saved list may still be rebuilding for
        // the confirmed profile; give it one more moment before giving up.
        if (!res.people.length && res.reason !== "sparse_profile") {
          await new Promise((r) => setTimeout(r, EMPTY_RETRY_MS));
          res = await services.loadPeople(exclude);
        }
        const matches = pickMatches(res.people, exclude);
        const cur = stateRef.current;
        update({
          status: cur?.status === "pending" || !cur ? "in_progress" : cur.status,
          step: "matches",
          matches,
          selectedId: undefined,
        });
        track("matches_shown", matches.length > 0);
      } catch {
        setMatchesFailed(true);
        track("matches_shown", false);
      } finally {
        setLoadingMatches(false);
      }
    },
    [services, update, track],
  );

  // ── Preparing the email (only ever on an explicit click, or resuming one) ──
  const prepare = useCallback(
    async (match: WalkthroughMatch) => {
      if (!uid) return;
      const contactId = contactIdFor(match.id);
      update({ step: "draft", selectedId: match.id, contactId });
      setNotice(null);
      setDraft(null);
      setDraftFailed(false);
      setDraftNotConfigured(false);

      // An earlier attempt may already have written it (a refresh mid-way).
      const existing = await services.loadDraft(uid, contactId).catch(() => null);
      if (existing) {
        setDraft(existing);
        update({ status: "drafted" });
        return;
      }

      setPreparing("email");
      const useFree = (await (freeRef.current ?? Promise.resolve(false))) === true;
      const reveal = await services.revealEmail(match.id, match.deckId, useFree);
      if (!reveal.ok) {
        setPreparing(null);
        if (reveal.reason === "free_unavailable") {
          // Never charged silently: show the price and let them choose.
          setFreeEligible(false);
          update({ step: "person", contactId: undefined });
          setNotice({
            tone: "info",
            text: `Your free first email isn't available on this account. Finding ${match.firstName}'s email uses ${REVEAL_COST} credits, and you're not charged if we can't find one.`,
          });
        } else if (reveal.reason === "no_email") {
          const noEmailIds = Array.from(new Set([...(stateRef.current?.noEmailIds || []), match.id]));
          update({ step: "matches", selectedId: undefined, contactId: undefined, noEmailIds });
          setNotice({
            tone: "info",
            text: `We couldn't find a verified email for ${match.firstName}, so nothing was charged. Pick someone else.`,
          });
        } else {
          update({ step: "person", contactId: undefined });
          setNotice({
            tone: "error",
            text:
              reveal.reason === "insufficient_credits"
                ? `You need ${REVEAL_COST} credits to find ${match.firstName}'s email. You can pick someone else, or get more credits from Pricing.`
                : "Something went wrong finding their email. Nothing was charged. Try again in a moment.",
          });
        }
        track("email_found", false);
        return;
      }
      track("email_found");
      if (reveal.free) setFreeEligible(false);

      setPreparing("writing");
      const written = await services.writeDraft(match.id, match.deckId);
      const saved = written.ok ? await services.loadDraft(uid, contactId).catch(() => null) : null;
      setPreparing(null);
      if (!saved) {
        setDraftFailed(true);
        setDraftNotConfigured(!written.ok && written.reason === "not_configured");
        track("draft_written", false);
        return;
      }
      setDraft(saved);
      update({ status: "drafted" });
      track("draft_written");
      funnel("walkthrough_draft_generated", { free_outreach: reveal.free, rank: match.rank });
    },
    [uid, services, update, track, funnel, setFreeEligible],
  );

  // ── Load / resume ────────────────────────────────────────────────────────
  const started = useRef(false);
  useEffect(() => {
    if (!uid || started.current) return;
    started.current = true;
    services.loadProfile(uid).then(setProfile).catch(() => setProfile(null));
    const free = services.loadFirstOutreach().catch(() => false);
    freeRef.current = free;
    void free.then((v) => setFreeEligibleState(v));
    services
      .loadState(uid)
      .catch(() => null)
      .then(async (saved) => {
        const s = normalizeWalkthrough(saved);
        stateRef.current = s;
        setState(s);
        if (s.status === "completed") {
          leave("find", { replace: true });
          return;
        }
        const selected = s.matches?.find((m) => m.id === s.selectedId);
        if (s.step === "draft" && selected) {
          const existing = await services.loadDraft(uid, s.contactId || contactIdFor(selected.id)).catch(() => null);
          if (existing) {
            setDraft(existing);
            return;
          }
          // The draft was being prepared when they left. Resuming is safe:
          // a found email is cached (no second charge) and the write is
          // idempotent per person.
          void prepare(selected);
          return;
        }
        if (!s.matches?.length) void fetchMatches();
      });
  }, [uid, services, leave, fetchMatches, prepare]);

  // ── Draft edits: saved shortly after typing stops ────────────────────────
  const editTimer = useRef<number | undefined>();
  useEffect(() => () => window.clearTimeout(editTimer.current), []);
  const editDraft = (patch: Partial<Pick<EmailDraft, "subject" | "body">>) => {
    if (!draft || !uid || !state?.contactId) return;
    const next = { ...draft, ...patch };
    setDraft(next);
    setSaveStatus("saving");
    // A change after review needs a fresh review before it can be sent.
    if (sendPhase === "confirm" || sendPhase === "unconfirmed") {
      setSendPhase("idle");
      setReview(null);
    }
    const contactId = state.contactId;
    window.clearTimeout(editTimer.current);
    editTimer.current = window.setTimeout(() => {
      services
        .saveDraft(uid, contactId, { subject: next.subject, body: next.body })
        .then(() => setSaveStatus("saved"))
        .catch(() => setSaveStatus("error"));
    }, 700);
  };

  /** Save the current text now (before sending or leaving for Google). */
  const flushSave = async () => {
    if (!draft || !uid || !state?.contactId) throw new Error("no_draft");
    window.clearTimeout(editTimer.current);
    if (saveStatus === "saving" || saveStatus === "error") {
      await services.saveDraft(uid, state.contactId, { subject: draft.subject, body: draft.body });
      setSaveStatus("saved");
    }
  };

  // ── Gmail and sending ────────────────────────────────────────────────────
  const onGmailConnected = useCallback(
    (g: GmailState) => {
      if (g.canSend) {
        setSendNotice({ tone: "success", text: "Gmail is connected. Your email is ready to send." });
        funnel("walkthrough_gmail_connected", {});
      } else {
        setSendNotice({
          tone: "error",
          text: "Gmail connected without permission to send. Reconnect and leave every box checked. Your draft is saved.",
        });
      }
    },
    [funnel],
  );

  // Know whether Gmail can send as soon as the draft is on screen, and
  // finish an OAuth return (re-checking the permissions actually granted).
  const step0 = state?.step;
  useEffect(() => {
    if (step0 !== "draft" || !draft || gmail) return;
    let cancelled = false;
    services
      .gmailStatus()
      .catch(() => ({ connected: false, canSend: false }))
      .then((g) => {
        if (cancelled) return;
        setGmail(g);
        const ret = oauthReturn.current;
        oauthReturn.current = null;
        if (ret?.error) setSendNotice({ tone: "error", text: GMAIL_ERROR_COPY[ret.error] ?? GMAIL_ERROR_DEFAULT });
        else if (ret?.connected) onGmailConnected(g);
      });
    return () => {
      cancelled = true;
    };
  }, [step0, draft, gmail, services, onGmailConnected]);

  const connectGmail = async () => {
    setSendNotice(null);
    setConnecting(true);
    try {
      await flushSave();
    } catch {
      setConnecting(false);
      setSendNotice({ tone: "error", text: "Couldn't save your latest edits. Check your connection and try again." });
      return;
    }
    try {
      const how = await services.connectGmail(gmailReturnPath);
      if (how === "connected") {
        const g = await services.gmailStatus();
        setGmail(g);
        onGmailConnected(g);
        setConnecting(false);
      }
      // "redirecting": the page is leaving for Google.
    } catch (e) {
      setConnecting(false);
      const notConfigured = e instanceof Error && e.message === "gmail_oauth_not_configured";
      setSendNotice({
        tone: "error",
        text: notConfigured
          ? "Gmail connection isn't set up on this server yet. Your draft is saved."
          : GMAIL_ERROR_DEFAULT,
      });
    }
  };

  const finishSent = (alreadySent: boolean) => {
    const contactId = state?.contactId;
    update({ status: "completed" });
    track("email_sent");
    if (!alreadySent) funnel("first_email_sent", { rank: selectedMatch()?.rank ?? null });
    if (preview || !contactId || mode === "modal") {
      setSentTo(selectedMatch()?.firstName || "them");
      setSentContactId(contactId || null);
      return;
    }
    navigate(`/contacts?contact=${encodeURIComponent(contactId)}`, { replace: true });
  };
  const selectedMatch = () => stateRef.current?.matches?.find((m) => m.id === stateRef.current?.selectedId) || null;

  // Step 1 of sending: save, then ask the server what exactly would go out.
  const startSend = async () => {
    const contactId = state?.contactId;
    if (!contactId || !draft) return;
    setSendNotice(null);
    setSendPhase("reviewing");
    try {
      await flushSave();
    } catch {
      setSendPhase("idle");
      setSendNotice({ tone: "error", text: "Couldn't save your latest edits. Check your connection and try again." });
      return;
    }
    let r: SendReview;
    try {
      r = await services.reviewSend(contactId);
    } catch {
      setSendPhase("idle");
      setSendNotice({ tone: "error", text: "Couldn't get your email ready to send. Try again." });
      return;
    }
    if (r.alreadySent) {
      finishSent(true);
      return;
    }
    if (r.blocking.length) {
      setSendPhase("idle");
      setSendNotice({ tone: "error", text: r.blocking.map((b) => BLOCKING_COPY[b] ?? "Something in this email needs fixing first.").join(" ") });
      return;
    }
    setReview(r);
    setSendPhase("confirm");
  };

  // Step 2: the explicit confirmation. Sends the reviewed version only.
  const confirmSend = async (confirmNotSent = false) => {
    const contactId = state?.contactId;
    if (!contactId || !review) return;
    setSendPhase("sending");
    const res = await services.sendEmail(contactId, review.hash, { confirmNotSent });
    if (res.ok) {
      finishSent(res.alreadySent);
      return;
    }
    setSendPhase(res.reason === "send_unconfirmed" ? "unconfirmed" : "idle");
    if (res.reason !== "send_unconfirmed") setReview(null);
    switch (res.reason) {
      case "gmail_not_connected":
      case "gmail_token_expired":
      case "gmail_scope_missing":
        setGmail({ connected: res.reason !== "gmail_not_connected", canSend: false });
        setSendNotice({ tone: "error", text: "Gmail needs to be reconnected before this can send. Your draft is saved." });
        break;
      case "draft_changed": {
        const fresh = uid ? await services.loadDraft(uid, contactId).catch(() => null) : null;
        if (fresh) setDraft(fresh);
        setSendNotice({ tone: "error", text: "Your email changed since you reviewed it. Check it and send again." });
        break;
      }
      case "send_in_progress":
        setSendNotice({ tone: "info", text: "This email is already being sent. Give it a moment, then try again." });
        break;
      case "send_unconfirmed":
        break;
      case "blocked":
        setSendNotice({ tone: "error", text: "Something in this email needs fixing before it can go out." });
        break;
      case "daily_cap_reached":
        setSendNotice({ tone: "error", text: "You've reached today's sending limit. Your draft is saved; send it tomorrow." });
        break;
      default:
        setSendNotice({
          tone: "error",
          text: "Sending didn't go through. Your draft is saved, and trying again won't send it twice.",
        });
    }
  };

  const saveAndExit = () => {
    track("save_and_exit");
    // Flush a pending edit before leaving.
    if (saveStatus === "saving" && draft && uid && state?.contactId) {
      window.clearTimeout(editTimer.current);
      void services.saveDraft(uid, state.contactId, { subject: draft.subject, body: draft.body }).catch(() => {});
    }
    leave("later");
  };

  // ── Render ───────────────────────────────────────────────────────────────
  const matches = state?.matches || [];
  const noEmail = new Set(state?.noEmailIds || []);
  const selected = matches.find((m) => m.id === state?.selectedId) || null;
  const usable = matches.filter((m) => !noEmail.has(m.id));
  const step = state?.step ?? "matches";
  const progressIndex = sentTo
    ? 3
    : step === "draft"
      ? sendPhase === "confirm" || sendPhase === "sending" || sendPhase === "unconfirmed" ? 2 : 1
      : 0;

  // The matches step is a wide card gallery; everything else is a reading column.
  const body = (content: React.ReactNode, opts: { wide?: boolean }) => (
    <div style={{ width: "100%", maxWidth: opts.wide ? 1080 : 640 }}>
      <Progress index={progressIndex} />
      {notice && (
        <p role={notice.tone === "error" ? "alert" : "status"} style={{ ...notStyle(notice.tone), margin: "0 0 18px", maxWidth: 640 }}>
          {notice.text}
        </p>
      )}
      {content}
    </div>
  );
  const shell = (content: React.ReactNode, opts: { wide?: boolean } = {}) =>
    mode === "modal" ? (
      // Inside the pop-up: no page background or logo; the dialog's X sits top right.
      <div style={{ color: OB.heading, fontFamily: OB.fontBody, padding: "18px clamp(16px, 4vw, 32px) 32px" }}>
        <div style={{ display: "flex", justifyContent: "flex-end", paddingRight: 36, marginBottom: 8 }}>
          <button type="button" onClick={saveAndExit} style={linkButton}>
            Save and finish later
          </button>
        </div>
        <div className="flex justify-center">{body(content, opts)}</div>
      </div>
    ) : (
      <div className="min-h-screen flex flex-col" style={{ background: OB.pageBg, color: OB.heading, fontFamily: OB.fontBody }}>
        <header
          style={{ padding: "20px clamp(16px, 5vw, 40px)", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}
        >
          <img src={OfferloopLogo} alt="Offerloop" style={{ height: 34 }} />
          <button type="button" onClick={saveAndExit} style={linkButton}>
            Save and finish later
          </button>
        </header>
        <main className="flex-1 flex justify-center" style={{ padding: "8px 16px 48px" }}>
          {body(content, opts)}
        </main>
      </div>
    );

  const h1 = (text: React.ReactNode) => (
    <h1
      style={{
        fontFamily: OB.fontDisplay,
        fontWeight: 600,
        fontSize: "clamp(24px, 4vw, 32px)",
        letterSpacing: "-0.02em",
        margin: "0 0 10px",
        textWrap: "balance",
      }}
    >
      {text}
    </h1>
  );
  const sub = (text: React.ReactNode) => (
    <p style={{ fontSize: 15.5, color: OB.ink2, lineHeight: 1.6, margin: "0 0 22px" }}>{text}</p>
  );
  const spinner = (text: string) => (
    <div role="status" style={{ display: "flex", alignItems: "center", gap: 10, color: OB.ink2, fontSize: 15, padding: "24px 0" }}>
      <Loader2 className="animate-spin" size={18} style={{ color: OB.primary }} />
      {text}
    </div>
  );
  const card: React.CSSProperties = {
    background: "#fff",
    border: `1px solid ${OB.border}`,
    borderRadius: 14,
    padding: 18,
  };
  const costNote = freeEligible
    ? "Your first email is on us. No credits needed."
    : `Finding their email uses ${REVEAL_COST} credits. If we can't find one, you're not charged.`;

  // The send area under the draft: connect Gmail, send, confirm.
  const sendSection = (m: WalkthroughMatch) => {
    if (!gmail) return spinner("Checking your Gmail…");
    if (!gmail.canSend) {
      return (
        <div style={{ ...card, background: OB.primary50, border: "none" }}>
          <div style={{ fontWeight: 700, marginBottom: 6 }}>Send it from your Gmail</div>
          <p style={{ fontSize: 14, color: OB.ink2, lineHeight: 1.6, margin: "0 0 6px" }}>
            Your email goes out from your own Gmail address, so {m.firstName}'s reply comes straight to you.
          </p>
          <p style={{ fontSize: 13.5, color: OB.ink3, lineHeight: 1.6, margin: "0 0 14px" }}>
            Google will ask to let Offerloop write and send email, and read your inbox. We use that to send only the
            emails you confirm, and to spot replies so they show up in Contacts. Nothing is sent without your OK.
          </p>
          <button type="button" onClick={() => void connectGmail()} disabled={connecting} style={{ ...obPrimaryButton, opacity: connecting ? 0.6 : 1 }}>
            {connecting ? "Opening Google…" : gmail.connected ? "Reconnect Gmail" : "Connect Gmail"}
          </button>
        </div>
      );
    }
    if (sendPhase === "unconfirmed") {
      return (
        <div role="alert" style={{ ...card }}>
          <div style={{ fontWeight: 700, marginBottom: 6 }}>Check your Sent folder first</div>
          <p style={{ fontSize: 14, color: OB.ink2, lineHeight: 1.6, margin: "0 0 12px" }}>
            An earlier try may have sent this already, and we couldn't check. Look in Gmail's Sent folder so{" "}
            {m.firstName} doesn't get it twice.
          </p>
          <a href={GMAIL_SENT_URL} target="_blank" rel="noopener noreferrer" style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 14, fontWeight: 600, color: OB.primary, marginBottom: 12 }}>
            Open Gmail Sent <ExternalLink size={14} />
          </a>
          <button type="button" onClick={() => void confirmSend(true)} style={secondaryButton}>
            It's not there. Send it now
          </button>
        </div>
      );
    }
    if ((sendPhase === "confirm" || sendPhase === "sending") && review) {
      const tips = review.warnings.map((w) => WARNING_COPY[w]).filter(Boolean).slice(0, 2);
      const sending = sendPhase === "sending";
      return (
        <div role="dialog" aria-label="Confirm sending" style={{ ...card, borderColor: OB.primary200 }}>
          <div style={{ fontWeight: 700, marginBottom: 10 }}>Send this email now?</div>
          <dl style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "4px 12px", fontSize: 14, margin: "0 0 12px" }}>
            <dt style={{ color: OB.ink3 }}>To</dt>
            <dd style={{ margin: 0, fontWeight: 600, overflowWrap: "anywhere" }}>{review.recipient}</dd>
            <dt style={{ color: OB.ink3 }}>Subject</dt>
            <dd style={{ margin: 0, overflowWrap: "anywhere" }}>{review.subject}</dd>
          </dl>
          <p style={{ fontSize: 13.5, color: OB.ink3, margin: "0 0 12px" }}>
            The email above is exactly what will be sent, from your Gmail.
          </p>
          {tips.length > 0 && (
            <ul aria-label="Optional suggestions" style={{ margin: "0 0 12px", paddingLeft: 18, fontSize: 13, color: OB.ink2, lineHeight: 1.5 }}>
              {tips.map((t) => (
                <li key={t}>{t}</li>
              ))}
            </ul>
          )}
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <button type="button" onClick={() => void confirmSend()} disabled={sending} style={{ ...obPrimaryButton, opacity: sending ? 0.6 : 1 }}>
              {sending ? "Sending…" : "Yes, send it"}
            </button>
            {!sending && (
              <button
                type="button"
                onClick={() => {
                  setSendPhase("idle");
                  setReview(null);
                }}
                style={secondaryButton}
              >
                Keep editing
              </button>
            )}
          </div>
        </div>
      );
    }
    const busy = sendPhase === "reviewing";
    return (
      <button type="button" onClick={() => void startSend()} disabled={busy} style={{ ...obPrimaryButton, opacity: busy ? 0.6 : 1 }}>
        {busy ? "Getting it ready…" : `Send email to ${m.firstName}`}
      </button>
    );
  };

  if (sentTo && mode === "modal") {
    return shell(
      <>
        {h1(<>Sent to {sentTo}</>)}
        {sub("Their reply will show up in Contacts.")}
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {sentContactId && !preview && (
            <button
              type="button"
              onClick={() => navigate(`/contacts?contact=${encodeURIComponent(sentContactId)}`)}
              style={{ ...secondaryButton, height: 52 }}
            >
              Open in Contacts
            </button>
          )}
          <button type="button" onClick={() => leave("find")} style={{ ...obPrimaryButton, height: 52 }}>
            Done
          </button>
        </div>
      </>,
    );
  }

  if (sentTo) {
    // Page preview only; live sends go straight to the conversation.
    return shell(
      <>
        {h1(<>Sent to {sentTo}</>)}
        {sub("In the app you'd land on the conversation in Contacts now, where their reply will show up.")}
      </>,
    );
  }

  if (!state) return shell(spinner("Loading…"));

  // Draft step: preparing, failed, or the editable email.
  if (step === "draft" && selected) {
    if (preparing) {
      return shell(
        <>
          {h1(<>Writing to {selected.firstName}</>)}
          {spinner(preparing === "email" ? `Finding ${selected.firstName}'s email…` : "Writing your email…")}
        </>,
      );
    }
    if (draftFailed || !draft) {
      return shell(
        draftFailed ? (
          <>
            {h1("We couldn't write that email")}
            {sub(
              draftNotConfigured
                ? "Email writing isn't set up on this server yet, so nothing was written or charged. Your progress is saved."
                : `Something went wrong writing to ${selected.firstName}. Trying again won't cost anything extra.`,
            )}
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <button type="button" onClick={() => void prepare(selected)} style={obPrimaryButton}>
                Try again
              </button>
              <button type="button" onClick={() => update({ step: "matches" })} style={secondaryButton}>
                Pick someone else
              </button>
            </div>
          </>
        ) : (
          spinner("Loading your draft…")
        ),
      );
    }
    return shell(
      <>
        {h1(<>Your email to {selected.firstName}</>)}
        {sub("Make it yours. Edits save as you type, and nothing is sent until you say so.")}
        <div style={{ ...card, display: "flex", gap: 12, alignItems: "center", marginBottom: 18 }}>
          <Avatar match={selected} size={40} />
          <div style={{ minWidth: 0 }}>
            <div style={{ fontWeight: 700 }}>{selected.name}</div>
            <div style={{ fontSize: 13.5, color: OB.ink3 }}>
              {[selected.title, selected.company].filter(Boolean).join(" · ")}
              {draft.to ? ` · ${draft.to}` : ""}
            </div>
          </div>
        </div>
        <label htmlFor="wt-subject" style={obFieldLabel}>Subject</label>
        <input
          id="wt-subject"
          style={{ ...obInput, marginBottom: 14 }}
          {...obFocus}
          value={draft.subject}
          onChange={(e) => editDraft({ subject: e.target.value })}
        />
        <label htmlFor="wt-body" style={obFieldLabel}>Email</label>
        <textarea
          id="wt-body"
          value={draft.body}
          onChange={(e) => editDraft({ body: e.target.value })}
          rows={12}
          style={{ ...obInput, height: "auto", padding: "12px 15px", lineHeight: 1.6, resize: "vertical" }}
        />
        <p role="status" style={{ fontSize: 12.5, color: saveStatus === "error" ? "#B91C1C" : OB.ink3, margin: "6px 0 20px", minHeight: 18 }}>
          {saveStatus === "saving" && "Saving…"}
          {saveStatus === "saved" && "Saved"}
          {saveStatus === "error" && "Couldn't save your last change. Keep typing to try again."}
        </p>
        {sendNotice && (
          <p role={sendNotice.tone === "error" ? "alert" : "status"} style={notStyle(sendNotice.tone)}>
            {sendNotice.text}
          </p>
        )}
        {sendSection(selected)}
        <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 10 }}>
          <button type="button" onClick={saveAndExit} style={linkButton}>
            Save and finish later
          </button>
          {sendPhase !== "sending" && (
            <button type="button" onClick={() => update({ step: "matches" })} style={linkButton}>
              Write to someone else instead
            </button>
          )}
        </div>
      </>,
    );
  }

  // Person step: the full profile and why they fit. Nothing is spent until
  // "Prepare my email".
  if (step === "person" && selected) {
    const reasons = matchReasons(selected, profile);
    const role = [selected.title, selected.company].filter(Boolean).join(" at ");
    return shell(
      <>
        <style>{`
          .wt-profile { display: grid; gap: 22px; grid-template-columns: 1fr; }
          .wt-profile-portrait { width: 132px; aspect-ratio: 1 / 1.4142; }
          @media (min-width: 640px) {
            .wt-profile { grid-template-columns: 190px minmax(0, 1fr); align-items: start; }
            .wt-profile-portrait { width: 100%; }
          }
        `}</style>
        <button
          type="button"
          onClick={() => {
            setNotice(null);
            update({ step: "matches" });
          }}
          style={{ ...linkButton, display: "flex", alignItems: "center", gap: 6, marginBottom: 16 }}
        >
          <ArrowLeft size={16} /> Back to your matches
        </button>
        <article style={{ ...card, padding: "clamp(18px, 4vw, 26px)", borderRadius: 18 }}>
          <div className="wt-profile">
            <div className="wt-profile-portrait" style={{ display: "flex" }}>
              <ProfilePortrait match={selected} style={{ flex: 1, borderRadius: 12 }} />
            </div>
            <div style={{ minWidth: 0 }}>
              {selected.company && (
                <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: "0.1em", textTransform: "uppercase", color: OB.ink3, marginBottom: 6 }}>
                  {selected.company}
                </div>
              )}
              <h1 style={{ fontFamily: OB.fontDisplay, fontWeight: 600, fontSize: "clamp(26px, 4vw, 32px)", letterSpacing: "-0.02em", lineHeight: 1.15, margin: "0 0 6px" }}>
                {selected.name}
              </h1>
              <div style={{ fontSize: 15, color: OB.ink2 }}>{role}</div>
              {(selected.school || selected.location) && (
                <div style={{ fontSize: 13.5, color: OB.ink3, marginTop: 4 }}>
                  {[selected.school, selected.location].filter(Boolean).join(" · ")}
                </div>
              )}
              <div aria-hidden style={{ height: 1, background: OB.border, margin: "16px 0 14px" }} />
              <h2 style={{ fontFamily: OB.fontBody, fontSize: 11, fontWeight: 700, letterSpacing: "0.1em", textTransform: "uppercase", color: OB.primary, margin: "0 0 8px" }}>
                Why {selected.firstName} is a good first email
              </h2>
              <ul style={{ listStyle: "disc", margin: "0 0 14px", paddingLeft: 18, color: OB.ink, fontSize: 14.5, lineHeight: 1.6 }}>
                {reasons.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
              {selected.briefing && (
                <p style={{ fontSize: 14, color: OB.ink2, lineHeight: 1.65, margin: "0 0 14px" }}>{selected.briefing}</p>
              )}
              {selected.linkedinUrl && (
                <a
                  href={selected.linkedinUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 14, fontWeight: 600, color: OB.primary }}
                >
                  View LinkedIn profile <ExternalLink size={14} />
                </a>
              )}
            </div>
          </div>
        </article>
        <p style={{ fontSize: 13.5, color: OB.ink3, margin: "16px 0 10px" }}>{costNote}</p>
        <button type="button" onClick={() => void prepare(selected)} style={obPrimaryButton}>
          Prepare my email to {selected.firstName}
        </button>
      </>,
    );
  }

  // Matches step.
  const header = (
    <div style={{ maxWidth: 640 }}>
      {h1(<>{firstName ? `${firstName}, here` : "Here"} are a few people worth emailing first</>)}
      {sub("Picked from your resume and the roles and companies you confirmed. Choose one to see why they fit.")}
    </div>
  );
  if (loadingMatches) return shell(<>{header}{spinner("Finding people who fit your background…")}</>, { wide: true });
  if (matchesFailed || !matches.length) {
    return shell(
      <>
        {h1(matchesFailed ? "We couldn't load your matches" : "We don't have matches for you yet")}
        {sub(
          matchesFailed
            ? "Something went wrong on our side. Try again, or search for people yourself on Find."
            : "Adding target companies helps us find people for you. You can also search for anyone on Find.",
        )}
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <button type="button" onClick={() => void fetchMatches()} style={obPrimaryButton}>
            Try again
          </button>
          <button type="button" onClick={() => leave("find")} style={secondaryButton}>
            Search on Find
          </button>
          {!matchesFailed && !preview && (
            <button
              type="button"
              onClick={() => {
                // In the pop-up, record the close before leaving Find.
                if (mode === "modal") onExitRef.current?.("later");
                navigate("/account-settings");
              }}
              style={{ ...linkButton, marginTop: 4 }}
            >
              Add target companies in Settings
            </button>
          )}
        </div>
      </>,
    );
  }
  const usableActions = (
    <>
      {usable.length < 2 && (
        <button type="button" onClick={() => void fetchMatches(matches.map((m) => m.id))} style={secondaryButton}>
          Show me other people
        </button>
      )}
      <button type="button" onClick={() => leave("skip")} style={{ ...linkButton, marginTop: 14 }}>
        Skip for now and go to Find
      </button>
    </>
  );
  return shell(
    <>
      {header}
      <MatchGallery
        matches={matches}
        reasonFor={(m) => {
          // With no real reason, matchReasons falls back to "Title at
          // Company", which the card already shows; their briefing says more.
          const top = matchReasons(m, profile)[0];
          const role = [m.title, m.company].filter(Boolean).join(" at ");
          return top === role && m.briefing ? m.briefing : top;
        }}
        selectedId={state.selectedId}
        unavailableIds={noEmail}
        onSelect={(m) => {
          setNotice(null);
          update({ step: "person", selectedId: m.id });
          funnel("walkthrough_match_selected", { rank: m.rank, position: matches.indexOf(m) });
        }}
      />
      <div style={{ maxWidth: 640 }}>
        {usableActions}
      </div>
    </>,
    { wide: true },
  );
}
