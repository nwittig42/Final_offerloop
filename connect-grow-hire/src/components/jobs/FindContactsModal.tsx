/**
 * FindContactsModal — "Find contacts" on a job board card.
 *
 * Shows up to three people for a saved job, one per outreach slot:
 * leader (email first) → process (likely interviewer) → recruiter.
 * The search runs once per job on the backend; while another request is
 * running it the route returns 202 and this modal polls every 3s (up to 180s).
 * Emails are looked up only on "See email" or "Draft", per person.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { AlertTriangle, Linkedin, Loader2, Mail } from "lucide-react";
import DraftDeliveryActions from "@/components/DraftDeliveryActions";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { toast } from "@/hooks/use-toast";
import {
  apiService,
  type ContactSlot,
  type ContactSlotPerson,
  type FindContactsResponse,
} from "@/services/api";

export interface FindContactsJob {
  id: string; // Firestore jobs doc id
  title?: string;
  company?: string;
}

interface FindContactsModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  job: FindContactsJob | null;
}

type ReadyResult = Extract<FindContactsResponse, { status: "ready" }>;

type ModalState =
  | { kind: "idle" }
  | { kind: "searching" }
  | { kind: "ready"; result: ReadyResult }
  | { kind: "insufficient"; message: string }
  | { kind: "not_found" }
  | { kind: "timeout" }
  | { kind: "error"; message: string };

// Per-person email lookup: undefined = not looked up yet.
type EmailInfo = { status: "loading" } | { status: "found"; email: string } | { status: "none" };

interface DraftInfo {
  to: string;
  gmailUrl?: string;
  subject?: string;
  body?: string;
}

const POLL_MS = 3000;
const POLL_LIMIT_MS = 180_000;
const SLOT_ORDER: Record<ContactSlot, number> = { leader: 0, process: 1, recruiter: 2 };
const AVATAR_COLORS = ["#5965D8", "#0D9488", "#D97706"];

function initialsOf(name: string): string {
  return name.split(/\s+/).filter(Boolean).map((n) => n[0]).slice(0, 2).join("").toUpperCase() || "?";
}

function teamLine(team: string, source: string): string {
  if (!team) return "We couldn't pin down the team for this role";
  return source ? `${team} · from the ${source}` : team;
}

function linkedinHref(url: string): string {
  return url.startsWith("http") ? url : `https://${url}`;
}

function Avatar({ person, index }: { person: ContactSlotPerson; index: number }) {
  const [broken, setBroken] = useState(false);
  if (person.photo && !broken) {
    return (
      <img
        src={person.photo}
        alt={person.name}
        loading="lazy"
        onError={() => setBroken(true)}
        className="h-10 w-10 flex-shrink-0 rounded-full object-cover"
      />
    );
  }
  return (
    <div
      className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-full text-[13px] font-semibold text-white"
      style={{ background: AVATAR_COLORS[index % AVATAR_COLORS.length] }}
    >
      {initialsOf(person.name)}
    </div>
  );
}

function PersonRow({
  person,
  index,
  email,
  draft,
  drafting,
  onSeeEmail,
  onDraft,
}: {
  person: ContactSlotPerson;
  index: number;
  email?: EmailInfo;
  draft?: DraftInfo;
  drafting: boolean;
  onSeeEmail: () => void;
  onDraft: () => void;
}) {
  const lookingUp = email?.status === "loading";
  return (
    <div className="rounded-2xl border border-[#ECEEF3] bg-white p-3.5">
      <div className="flex items-start gap-3">
        <Avatar person={person} index={index} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="truncate text-[14px] font-bold text-[#0F172A]">{person.name}</span>
            {person.slot === "leader" && (
              <span className="rounded-full bg-[#EEF1F9] px-2 py-0.5 text-[10.5px] font-semibold text-[#4A60A8]">
                Email first
              </span>
            )}
            {person.alumni && (
              <span className="rounded-full bg-[#ECFDF5] px-2 py-0.5 text-[10.5px] font-semibold text-[#047857]">
                Alumni
              </span>
            )}
          </div>
          {person.title && <div className="truncate text-[12.5px] text-[#64748B]">{person.title}</div>}
          {person.label && <div className="mt-0.5 text-[12px] font-medium text-[#475569]">{person.label}</div>}
          {email?.status === "found" && (
            <div className="mt-1 truncate text-[12.5px] text-[#0F172A]">{email.email}</div>
          )}
          {email?.status === "none" && (
            <div className="mt-1 text-[12.5px] italic text-[#B45309]">No email found</div>
          )}
        </div>
        {person.linkedin && (
          <a
            href={linkedinHref(person.linkedin)}
            target="_blank"
            rel="noopener noreferrer"
            title="View LinkedIn profile"
            aria-label="View LinkedIn profile"
            className="inline-flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-[9px] border border-[#E2E8F0] bg-white text-[#0A66C2] hover:bg-[#F1F5F9]"
          >
            <Linkedin className="h-4 w-4" />
          </a>
        )}
      </div>
      <div className="mt-2.5 flex flex-wrap items-center justify-end gap-2">
        {!email && (
          <Button size="sm" variant="outline" disabled={lookingUp} onClick={onSeeEmail}>
            See email
          </Button>
        )}
        {lookingUp && !drafting && (
          <span className="inline-flex items-center gap-1.5 text-xs text-gray-500">
            <Loader2 className="h-3.5 w-3.5 animate-spin" /> Looking up email…
          </span>
        )}
        {draft ? (
          <DraftDeliveryActions
            draft={{
              to: draft.to,
              subject: draft.subject,
              body: draft.body,
              gmailUrl: draft.gmailUrl,
              firstName: person.first_name,
              company: person.company,
            }}
          />
        ) : (
          email?.status !== "none" && (
            <Button size="sm" disabled={drafting || lookingUp} onClick={onDraft}>
              {drafting ? <Loader2 className="h-3.5 w-3.5 mr-1.5 animate-spin" /> : <Mail className="h-3.5 w-3.5 mr-1.5" />}
              Draft
            </Button>
          )
        )}
      </div>
    </div>
  );
}

export function FindContactsModal({ open, onOpenChange, job }: FindContactsModalProps) {
  const [state, setState] = useState<ModalState>({ kind: "idle" });
  const [emails, setEmails] = useState<Record<string, EmailInfo>>({});
  const [drafts, setDrafts] = useState<Record<string, DraftInfo>>({});
  const [draftingKey, setDraftingKey] = useState<string | null>(null);

  // Bumped on close / re-run so stale polls and responses are ignored.
  const runTokenRef = useRef(0);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  const runSearch = useCallback(async () => {
    if (!job?.id) return;
    stopPolling();
    const token = ++runTokenRef.current;
    const startedAt = Date.now();
    setState({ kind: "searching" });

    const attempt = async () => {
      let res: FindContactsResponse;
      try {
        res = await apiService.findContacts(job.id);
      } catch (e) {
        if (token !== runTokenRef.current) return;
        setState({ kind: "error", message: e instanceof Error ? e.message : "Something went wrong." });
        return;
      }
      if (token !== runTokenRef.current) return;
      switch (res.status) {
        case "ready":
          setState({ kind: "ready", result: res });
          return;
        case "searching":
          if (Date.now() - startedAt + POLL_MS > POLL_LIMIT_MS) {
            setState({ kind: "timeout" });
            return;
          }
          pollTimerRef.current = setTimeout(() => void attempt(), POLL_MS);
          return;
        case "insufficient_credits": {
          const detail =
            res.creditsRequired != null && res.creditsAvailable != null
              ? ` You need ${res.creditsRequired} credits and have ${res.creditsAvailable}.`
              : "";
          setState({ kind: "insufficient", message: `${res.error}.${detail}` });
          return;
        }
        case "not_found":
          setState({ kind: "not_found" });
          return;
        default:
          setState({ kind: "error", message: res.error });
      }
    };
    await attempt();
  }, [job?.id, stopPolling]);

  // Start the search when the modal opens; stop polling and reset on close.
  useEffect(() => {
    if (open) {
      if (state.kind === "idle") void runSearch();
      return;
    }
    stopPolling();
    runTokenRef.current += 1;
    const t = setTimeout(() => {
      setState({ kind: "idle" });
      setEmails({});
      setDrafts({});
      setDraftingKey(null);
    }, 200);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => stopPolling, [stopPolling]);

  // Look up one person's email (cached per person for the life of the modal).
  // Returns the email, null when none exists, or undefined when the lookup
  // failed (already toasted; the row stays retryable).
  const lookupEmail = useCallback(
    async (person: ContactSlotPerson): Promise<string | null | undefined> => {
      const known = emails[person.person_key];
      if (known?.status === "found") return known.email;
      if (known?.status === "none") return null;
      if (known?.status === "loading" || !job?.id) return undefined;
      const token = runTokenRef.current;
      setEmails((m) => ({ ...m, [person.person_key]: { status: "loading" } }));
      let email: string | null = null;
      let failure: string | null = null;
      try {
        const res = await apiService.revealContactEmail(job.id, person.person_key);
        if (res.status === "found") email = res.email;
        else if (res.status === "locked") failure = "Find contacts for this job first.";
        else if (res.status === "error") failure = res.error;
      } catch (e) {
        failure = e instanceof Error ? e.message : "Something went wrong.";
      }
      if (token !== runTokenRef.current) return undefined;
      if (failure) {
        // Leave the row retryable: clear the loading state rather than marking "none".
        setEmails((m) => {
          const next = { ...m };
          delete next[person.person_key];
          return next;
        });
        toast({ title: "Couldn't look up the email", description: failure, variant: "destructive" });
        return undefined;
      }
      setEmails((m) => ({
        ...m,
        [person.person_key]: email ? { status: "found", email } : { status: "none" },
      }));
      return email;
    },
    [emails, job?.id],
  );

  const handleDraft = useCallback(
    async (person: ContactSlotPerson) => {
      if (draftingKey) return;
      setDraftingKey(person.person_key);
      try {
        const email = await lookupEmail(person);
        if (email === undefined) return;
        if (!email) {
          toast({ title: "No email found", description: `We couldn't find an email for ${person.name}, so there's nothing to draft.` });
          return;
        }
        const res = await apiService.generateAndDraftEmails({
          contacts: [
            {
              Name: person.name,
              FirstName: person.first_name,
              LastName: person.last_name,
              Email: email,
              Title: person.title,
              Company: person.company,
              College: person.college,
              LinkedIn: person.linkedin,
              OutreachRole: person.slot,
            },
          ],
        });
        if ("error" in res) {
          toast({
            title: "Couldn't create the draft",
            description: res.message || res.error || "Reconnect your Gmail account and try again.",
            variant: "destructive",
          });
          return;
        }
        const d = res.drafts?.[0];
        if (!d || !(d.draftId || d.body)) {
          toast({ title: "Couldn't create the draft", description: "Try again in a moment.", variant: "destructive" });
          return;
        }
        setDrafts((m) => ({
          ...m,
          [person.person_key]: {
            to: email,
            gmailUrl: d.draftId ? d.gmailUrl || "https://mail.google.com/mail/u/0/#drafts" : undefined,
            subject: d.subject,
            body: d.body,
          },
        }));
        toast({
          title: "Draft ready",
          description: d.draftId ? `Draft to ${person.name} created in your Gmail.` : `Draft to ${person.name} is ready to download or copy.`,
        });
      } catch (e) {
        toast({
          title: "Couldn't create the draft",
          description: e instanceof Error ? e.message : "Try again in a moment.",
          variant: "destructive",
        });
      } finally {
        setDraftingKey(null);
      }
    },
    [draftingKey, lookupEmail],
  );

  const result = state.kind === "ready" ? state.result : null;
  const people = result
    ? [...result.people].sort((a, b) => (SLOT_ORDER[a.slot] ?? 9) - (SLOT_ORDER[b.slot] ?? 9))
    : [];
  const company = result?.company || job?.company || "";
  const heading =
    result && people.length > 0
      ? people.length === 1
        ? `1 person to reach at ${company}`
        : `${people.length} people to reach at ${company}`
      : "Find contacts";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg p-0 gap-0 overflow-hidden">
        <DialogHeader className="pl-5 pr-12 pt-5 pb-3 border-b border-gray-100">
          <div className="min-w-0">
            <DialogTitle className="text-base font-semibold text-gray-900">{heading}</DialogTitle>
            {result ? (
              <p className="mt-0.5 text-xs text-gray-500">{teamLine(result.team, result.team_source)}</p>
            ) : (
              job?.title &&
              job?.company && (
                <p className="mt-0.5 truncate text-xs text-gray-500">
                  {job.title} · {job.company}
                </p>
              )
            )}
            {result && result.creditsCharged > 0 && (
              <p className="mt-1 text-xs text-gray-500">Charged: {result.creditsCharged} credits</p>
            )}
          </div>
        </DialogHeader>

        <div className="px-5 py-4 max-h-[60vh] overflow-y-auto">
          {state.kind === "searching" && (
            <div className="flex flex-col items-center gap-3 py-8 text-center">
              <Loader2 className="h-5 w-5 animate-spin text-[#4A60A8]" />
              <p className="text-sm text-gray-600">Finding the right people for this role…</p>
            </div>
          )}

          {result && people.length > 0 && (
            <div className="space-y-2.5">
              {people.map((p, i) => (
                <PersonRow
                  key={p.person_key || `${p.slot}-${i}`}
                  person={p}
                  index={i}
                  email={emails[p.person_key]}
                  draft={drafts[p.person_key]}
                  drafting={draftingKey === p.person_key}
                  onSeeEmail={() => void lookupEmail(p)}
                  onDraft={() => void handleDraft(p)}
                />
              ))}
            </div>
          )}

          {result && people.length === 0 && (
            <Notice>We couldn't find people to reach for this role yet.</Notice>
          )}
          {state.kind === "insufficient" && <Notice>{state.message}</Notice>}
          {state.kind === "not_found" && <Notice>This job is no longer available.</Notice>}
          {state.kind === "timeout" && <Notice>Still searching. Try again in a few seconds.</Notice>}
          {state.kind === "error" && <Notice>{state.message || "Something went wrong."}</Notice>}
        </div>

        <div className="border-t border-gray-100 px-5 py-3 flex items-center justify-end gap-2">
          {(state.kind === "timeout" || state.kind === "error") && (
            <Button size="sm" variant="outline" onClick={() => void runSearch()}>
              Retry
            </Button>
          )}
          <Button size="sm" onClick={() => onOpenChange(false)}>
            {state.kind === "ready" ? "Done" : "Close"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function Notice({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-start gap-2 rounded border border-amber-200 bg-amber-50 px-3 py-2.5 text-xs text-amber-800">
      <AlertTriangle className="h-4 w-4 flex-shrink-0 mt-0.5" />
      <p className="min-w-0 flex-1">{children}</p>
    </div>
  );
}

export default FindContactsModal;
