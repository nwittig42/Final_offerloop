import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { ArrowLeft, Check, ExternalLink, Loader2 } from "lucide-react";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { trackFeatureActionCompleted } from "@/lib/analytics";
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
  type WalkthroughServices,
} from "./walkthroughServices";
import { OB, obFieldLabel, obInput, obPrimaryButton, obFocus } from "./onboardingTheme";
import OfferloopLogo from "@/assets/offerloop_logo2_trimmed.png";

// First-use walkthrough (onboarding v2, Milestone 2). Onboarding hands off
// here: a few matched people from different companies, pick one, see why
// they fit, then (only on an explicit click) find their email and write an
// editable first email. The draft stays in the app; Gmail and sending are
// Milestone 3. Progress is saved on users/{uid}.walkthrough, the draft on the
// contact doc, so "Save and finish later" resumes in place. Nothing here
// blocks the app: every state has a way out to Find.

const safeReturnTo = (raw: string | null): string => {
  if (!raw) return "";
  return raw.startsWith("/") && !raw.startsWith("//") && !raw.startsWith("/onboarding") && !raw.startsWith("/get-started")
    ? raw
    : "";
};

const PREVIEW_UID = "preview";
const EMPTY_RETRY_MS = 3000;

type Notice = { tone: "info" | "error"; text: string } | null;
type SaveStatus = "idle" | "saving" | "saved" | "error";

interface GetStartedPageProps {
  /** /dev/get-started-preview: sample people, fake email, no auth, no writes. */
  preview?: boolean;
  /** Test seam; defaults to the live (or preview) services. */
  services?: WalkthroughServices;
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

const initials = (name: string) =>
  name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase())
    .join("");

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
    <span aria-hidden style={style}>{initials(match.name)}</span>
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

export default function GetStartedPage({ preview = false, services: injected }: GetStartedPageProps = {}) {
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

  const [state, setState] = useState<WalkthroughState | null>(null);
  const [profile, setProfile] = useState<MatchProfile | null>(null);
  const [loadingMatches, setLoadingMatches] = useState(false);
  const [matchesFailed, setMatchesFailed] = useState(false);
  const [preparing, setPreparing] = useState<"email" | "writing" | null>(null);
  const [draft, setDraft] = useState<EmailDraft | null>(null);
  const [draftFailed, setDraftFailed] = useState(false);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>("idle");
  const [notice, setNotice] = useState<Notice>(null);

  const track = useCallback(
    (action: string, success = true) => {
      if (!preview) trackFeatureActionCompleted("walkthrough", action, success);
    },
    [preview],
  );

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

      // An earlier attempt may already have written it (a refresh mid-way).
      const existing = await services.loadDraft(uid, contactId).catch(() => null);
      if (existing) {
        setDraft(existing);
        update({ status: "drafted" });
        return;
      }

      setPreparing("email");
      const reveal = await services.revealEmail(match.id, match.deckId);
      if (!reveal.ok) {
        setPreparing(null);
        if (reveal.reason === "no_email") {
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

      setPreparing("writing");
      const written = await services.writeDraft(match.id, match.deckId);
      const saved = written.ok ? await services.loadDraft(uid, contactId).catch(() => null) : null;
      setPreparing(null);
      if (!saved) {
        setDraftFailed(true);
        track("draft_written", false);
        return;
      }
      setDraft(saved);
      update({ status: "drafted" });
      track("draft_written");
    },
    [uid, services, update, track],
  );

  // ── Load / resume ────────────────────────────────────────────────────────
  const started = useRef(false);
  useEffect(() => {
    if (!uid || started.current) return;
    started.current = true;
    services.loadProfile(uid).then(setProfile).catch(() => setProfile(null));
    services
      .loadState(uid)
      .catch(() => null)
      .then(async (saved) => {
        const s = normalizeWalkthrough(saved);
        stateRef.current = s;
        setState(s);
        if (s.status === "completed") {
          navigate(exitTo, { replace: true });
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
  }, [uid, services, navigate, exitTo, fetchMatches, prepare]);

  // ── Draft edits: saved shortly after typing stops ────────────────────────
  const editTimer = useRef<number | undefined>();
  useEffect(() => () => window.clearTimeout(editTimer.current), []);
  const editDraft = (patch: Partial<Pick<EmailDraft, "subject" | "body">>) => {
    if (!draft || !uid || !state?.contactId) return;
    const next = { ...draft, ...patch };
    setDraft(next);
    setSaveStatus("saving");
    const contactId = state.contactId;
    window.clearTimeout(editTimer.current);
    editTimer.current = window.setTimeout(() => {
      services
        .saveDraft(uid, contactId, { subject: next.subject, body: next.body })
        .then(() => setSaveStatus("saved"))
        .catch(() => setSaveStatus("error"));
    }, 700);
  };

  const saveAndExit = () => {
    track("save_and_exit");
    // Flush a pending edit before leaving.
    if (saveStatus === "saving" && draft && uid && state?.contactId) {
      window.clearTimeout(editTimer.current);
      void services.saveDraft(uid, state.contactId, { subject: draft.subject, body: draft.body }).catch(() => {});
    }
    navigate(exitTo);
  };

  // ── Render ───────────────────────────────────────────────────────────────
  const matches = state?.matches || [];
  const noEmail = new Set(state?.noEmailIds || []);
  const selected = matches.find((m) => m.id === state?.selectedId) || null;
  const usable = matches.filter((m) => !noEmail.has(m.id));
  const step = state?.step ?? "matches";
  const progressIndex = step === "draft" ? 1 : 0;

  const shell = (content: React.ReactNode) => (
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
        <div style={{ width: "100%", maxWidth: 640 }}>
          <Progress index={progressIndex} />
          {notice && (
            <p
              role={notice.tone === "error" ? "alert" : "status"}
              style={{
                fontSize: 14,
                lineHeight: 1.5,
                borderRadius: 10,
                padding: "10px 14px",
                margin: "0 0 18px",
                background: notice.tone === "error" ? "#FEF2F2" : OB.primary50,
                color: notice.tone === "error" ? "#991B1B" : OB.ink2,
              }}
            >
              {notice.text}
            </p>
          )}
          {content}
        </div>
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
  const costNote = services.firstOutreachFree
    ? "Your first email is on us. No credits needed."
    : `Finding their email uses ${REVEAL_COST} credits. If we can't find one, you're not charged.`;

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
            {sub(`Something went wrong writing to ${selected.firstName}. Trying again won't cost anything extra.`)}
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <button type="button" onClick={() => void prepare(selected)} style={obPrimaryButton}>
                Try again
              </button>
              <button type="button" onClick={() => update({ step: "matches", selectedId: undefined })} style={secondaryButton}>
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
        <div style={{ ...card, background: OB.primary50, border: "none", marginBottom: 18 }}>
          <div style={{ fontWeight: 700, marginBottom: 4 }}>Next: send it from your Gmail</div>
          <div style={{ fontSize: 14, color: OB.ink2, lineHeight: 1.55 }}>
            Your draft is saved here and in Contacts. Next we'll connect your Gmail so it goes out from your own address,
            only when you choose to send it.
          </div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <button type="button" onClick={saveAndExit} style={obPrimaryButton}>
            Save and finish later
          </button>
          <button type="button" onClick={() => update({ step: "matches", selectedId: undefined })} style={secondaryButton}>
            Write to someone else instead
          </button>
        </div>
      </>,
    );
  }

  // Person step: profile and why they fit. Nothing is spent until "Prepare my email".
  if (step === "person" && selected) {
    const reasons = matchReasons(selected, profile);
    return shell(
      <>
        <button
          type="button"
          onClick={() => {
            setNotice(null);
            update({ step: "matches", selectedId: undefined });
          }}
          style={{ ...linkButton, display: "flex", alignItems: "center", gap: 6, marginBottom: 16 }}
        >
          <ArrowLeft size={16} /> Back to your matches
        </button>
        <div style={{ ...card, padding: 22 }}>
          <div style={{ display: "flex", gap: 14, alignItems: "center", marginBottom: 16 }}>
            <Avatar match={selected} size={56} />
            <div style={{ minWidth: 0 }}>
              <h1 style={{ fontFamily: OB.fontDisplay, fontWeight: 600, fontSize: 24, margin: 0 }}>{selected.name}</h1>
              <div style={{ fontSize: 14.5, color: OB.ink2 }}>{[selected.title, selected.company].filter(Boolean).join(" at ")}</div>
              <div style={{ fontSize: 13.5, color: OB.ink3 }}>{[selected.school, selected.location].filter(Boolean).join(" · ")}</div>
            </div>
          </div>
          <h2 style={{ fontSize: 14, fontWeight: 700, margin: "0 0 8px" }}>Why {selected.firstName} is a good first email</h2>
          <ul style={{ margin: "0 0 14px", paddingLeft: 18, color: OB.ink2, fontSize: 14.5, lineHeight: 1.6 }}>
            {reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
          {selected.briefing && <p style={{ fontSize: 14, color: OB.ink2, lineHeight: 1.6, margin: "0 0 14px" }}>{selected.briefing}</p>}
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
        <p style={{ fontSize: 13.5, color: OB.ink3, margin: "16px 0 10px" }}>{costNote}</p>
        <button type="button" onClick={() => void prepare(selected)} style={obPrimaryButton}>
          Prepare my email to {selected.firstName}
        </button>
      </>,
    );
  }

  // Matches step.
  const header = (
    <>
      {h1(<>{firstName ? `${firstName}, here` : "Here"} are a few people worth emailing first</>)}
      {sub("Picked from your resume and the roles and companies you confirmed. Choose one to see why they fit.")}
    </>
  );
  if (loadingMatches) return shell(<>{header}{spinner("Finding people who fit your background…")}</>);
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
          <button type="button" onClick={() => navigate(exitTo)} style={secondaryButton}>
            Search on Find
          </button>
          {!matchesFailed && !preview && (
            <button type="button" onClick={() => navigate("/account-settings")} style={{ ...linkButton, marginTop: 4 }}>
              Add target companies in Settings
            </button>
          )}
        </div>
      </>,
    );
  }
  return shell(
    <>
      {header}
      <ul aria-label="Your matches" style={{ listStyle: "none", padding: 0, margin: "0 0 18px", display: "flex", flexDirection: "column", gap: 10 }}>
        {matches.map((m) => {
          const missing = noEmail.has(m.id);
          const top = matchReasons(m, profile)[0];
          return (
            <li key={m.id}>
              <button
                type="button"
                disabled={missing}
                onClick={() => {
                  setNotice(null);
                  update({ step: "person", selectedId: m.id });
                }}
                aria-label={`${m.name}, ${[m.title, m.company].filter(Boolean).join(" at ")}`}
                style={{
                  ...card,
                  width: "100%",
                  textAlign: "left",
                  display: "flex",
                  gap: 14,
                  alignItems: "center",
                  cursor: missing ? "default" : "pointer",
                  opacity: missing ? 0.55 : 1,
                  fontFamily: OB.fontBody,
                }}
              >
                <Avatar match={m} size={48} />
                <span style={{ minWidth: 0, flex: 1 }}>
                  <span style={{ display: "block", fontWeight: 700, color: OB.heading }}>{m.name}</span>
                  <span style={{ display: "block", fontSize: 13.5, color: OB.ink2 }}>
                    {[m.title, m.company].filter(Boolean).join(" at ")}
                  </span>
                  <span style={{ display: "block", fontSize: 13, color: missing ? OB.ink3 : OB.primary, marginTop: 4 }}>
                    {missing ? "No email found" : top}
                  </span>
                </span>
              </button>
            </li>
          );
        })}
      </ul>
      {usable.length < 2 && (
        <button type="button" onClick={() => void fetchMatches(matches.map((m) => m.id))} style={secondaryButton}>
          Show me other people
        </button>
      )}
      <button type="button" onClick={() => navigate(exitTo)} style={{ ...linkButton, marginTop: 14 }}>
        Skip for now and go to Find
      </button>
    </>,
  );
}
