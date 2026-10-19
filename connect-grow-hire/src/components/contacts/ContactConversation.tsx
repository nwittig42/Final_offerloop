// The Inbox, folded into the contact card: pipeline stage, the email chain,
// and a reply box (Generate / Send). Contacts with no thread yet get a
// "Draft email" button that creates a cold first-touch Gmail draft.
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, ArchiveRestore, ExternalLink, Loader2, MoreHorizontal, Send, Sparkles, Mail } from "lucide-react";
import { apiService, type ThreadMessage } from "@/services/api";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { useResumeStatus } from "@/hooks/useResumeStatus";
import { toast } from "@/hooks/use-toast";
import { SetupNudgeModal } from "@/components/gates/SetupNudgeModal";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { CONTACTS_KEY, THREADS_KEY, type ContactRow } from "./contactsModel";

function gmailUrlFor(contact: ContactRow): string | null {
  const t = contact.thread;
  if (t?.gmailThreadId) return `https://mail.google.com/mail/u/0/#inbox/${t.gmailThreadId}`;
  if (t?.gmailDraftUrl) return t.gmailDraftUrl;
  if (contact.email) return `https://mail.google.com/mail/u/0/#search/to:${encodeURIComponent(contact.email)}`;
  return null;
}

function formatSentAt(sentAt: string | null | undefined): string {
  if (!sentAt) return "";
  const d = new Date(sentAt);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function MessageBubble({ message, contactName }: { message: ThreadMessage; contactName: string }) {
  const fromUser = message.isFromUser;
  const who = fromUser ? "You" : message.isFromRecipient ? contactName : message.sender || "Unknown";
  return (
    <div className={`flex ${fromUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[85%] rounded-2xl px-4 py-3 text-[13.5px] leading-relaxed ${
          fromUser ? "bg-[var(--accent,#4A60A8)] text-white rounded-br-md" : "bg-paper-2 text-ink border border-line rounded-bl-md"
        }`}
      >
        <div className={`mb-1 flex items-baseline gap-2 text-[11.5px] ${fromUser ? "text-white/75" : "text-ink-3"}`}>
          <span className="font-semibold">{who}</span>
          <span>{formatSentAt(message.sentAt)}</span>
        </div>
        <div className="whitespace-pre-wrap break-words">{message.body}</div>
      </div>
    </div>
  );
}

export function ContactConversation({ contact }: { contact: ContactRow }) {
  const { user } = useFirebaseAuth();
  const queryClient = useQueryClient();
  const thread = contact.thread;
  const hasThread = !!thread && (!!thread.gmailThreadId || !!thread.gmailDraftId || thread.messageCount > 0 || !!thread.emailSentAt || !!thread.draftCreatedAt);

  const messagesQuery = useQuery({
    queryKey: ["outboxThreadMessages", contact.id],
    queryFn: async () => {
      const res = await apiService.getOutboxThreadMessages(contact.id);
      if ("error" in res) throw new Error(res.error);
      return res;
    },
    enabled: hasThread,
    staleTime: 5 * 60 * 1000,
  });
  const messages = messagesQuery.data?.messages ?? [];
  const gmailDisconnected = messagesQuery.data?.source === "local" && messagesQuery.data?.reason === "gmail_disconnected";

  // Drafts are kept per contact so edits survive switching between people.
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [generating, setGenerating] = useState(false);
  const [draftError, setDraftError] = useState<string | null>(null);
  const draft = drafts[contact.id] ?? "";
  const setDraft = (v: string) => setDrafts((p) => ({ ...p, [contact.id]: v }));

  useEffect(() => { setDraftError(null); }, [contact.id]);

  // A Drafted contact's unsent Gmail draft comes back as the single local
  // message; put it in the editor so the user can tweak and send it.
  useEffect(() => {
    if (contact.stage !== "drafted" || drafts[contact.id] !== undefined) return;
    if (messagesQuery.data?.source !== "local") return;
    const body = messages[0]?.body?.trim();
    if (body) setDrafts((p) => ({ ...p, [contact.id]: messages[0].body }));
  }, [contact.id, contact.stage, messagesQuery.data, messages, drafts]);

  const refreshThreads = () => {
    queryClient.invalidateQueries({ queryKey: THREADS_KEY });
    queryClient.invalidateQueries({ queryKey: ["outboxThreadMessages", contact.id] });
  };

  const archiveMutation = useMutation({
    mutationFn: async (archive: boolean) => {
      const res = archive ? await apiService.archiveOutboxThread(contact.id) : await apiService.unarchiveOutboxThread(contact.id);
      if ("error" in res) throw new Error(res.error);
    },
    onSuccess: (_d, archive) => {
      refreshThreads();
      toast({ title: archive ? "Conversation archived" : "Conversation restored" });
    },
    onError: (e: Error) => toast({ title: "Couldn't update", description: e.message, variant: "destructive" }),
  });

  const sendMutation = useMutation({
    mutationFn: async (body: string) => {
      const res = await apiService.sendOutboxReply(contact.id, body);
      if ("error" in res) throw new Error(res.error);
    },
    onSuccess: () => {
      setDrafts((p) => { const n = { ...p }; delete n[contact.id]; return n; });
      refreshThreads();
      toast({ title: "Reply sent" });
    },
    onError: (e: Error) => {
      setDraftError(e.message);
      toast({ title: "Couldn't send reply", description: e.message, variant: "destructive" });
    },
  });

  // Cold first-touch draft for a contact with no conversation yet. Same
  // /emails/generate-and-draft path Find uses; the backend fills in resume,
  // template and signoff.
  // Bookmarked from Search's recommendations with no email yet: find it
  // (8 credits, refunded on a miss) and draft, same as Contact on Search.
  // The backend writes the draft into this same contact doc.
  const canRevealAndDraft = !contact.email && !!contact.rankerCandidateId;

  const coldDraftMutation = useMutation({
    mutationFn: async () => {
      if (canRevealAndDraft) {
        const reveal = await apiService.revealRankerEmail(contact.rankerCandidateId, contact.rankerDeckId || undefined);
        if (!reveal.email) throw new Error(`We couldn't find a verified email for ${contact.firstName || contact.name}. No credits were charged.`);
        await apiService.rankerOutreach(contact.rankerCandidateId, "draft", contact.rankerDeckId || undefined);
        queryClient.invalidateQueries({ queryKey: CONTACTS_KEY });
        return;
      }
      const res = await apiService.generateAndDraftEmails({
        contacts: [{
          FirstName: contact.firstName, LastName: contact.lastName, name: contact.name,
          Email: contact.email, Company: contact.company, Title: contact.role,
        }],
      });
      if ("error" in res) throw new Error(res.message || res.error);
      if (!res.success || !res.draft_count) throw new Error("No draft was created. Check that Gmail is connected.");
    },
    onSuccess: () => {
      refreshThreads();
      toast({ title: "Draft created", description: "It's in your Gmail drafts and below." });
    },
    onError: (e: Error) => toast({ title: "Couldn't draft email", description: e.message, variant: "destructive" }),
  });

  const { hasResume } = useResumeStatus();
  const [showResumeNudge, setShowResumeNudge] = useState(false);

  const runGenerate = async () => {
    const id = contact.id;
    setGenerating(true);
    setDraftError(null);
    try {
      const res = await apiService.getReplyCoachDraft(id, { refresh: true });
      if ("error" in res) setDraftError(res.error || "Failed to generate.");
      else if (res.status === "generating") setDraftError("Still generating, try again in a moment.");
      else setDrafts((p) => ({ ...p, [id]: res.body || "" }));
    } catch (e) {
      setDraftError(e instanceof Error ? e.message : "Failed to generate.");
    } finally {
      setGenerating(false);
    }
  };

  const handleGenerate = () => {
    if (hasResume === false && !sessionStorage.getItem("ol_nudge_resume")) {
      sessionStorage.setItem("ol_nudge_resume", "1");
      setShowResumeNudge(true);
      return;
    }
    void runGenerate();
  };

  const gmailUrl = gmailUrlFor(contact);
  const canSend = draft.trim().length > 0 && !gmailDisconnected && !sendMutation.isPending;

  return (
    <section className="mt-8">
      <SetupNudgeModal
        open={showResumeNudge}
        variant="resume"
        onClose={() => setShowResumeNudge(false)}
        onContinue={() => void runGenerate()}
        body="Replies personalize far better with your resume: real overlaps between you and the contact instead of generic lines. You can also generate without it."
      />

      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="text-[13px] font-semibold uppercase tracking-[0.08em] text-ink-3">Conversation</h3>
        <div className="flex items-center gap-2">
          {gmailUrl && hasThread && (
            <a
              href={gmailUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex h-8 items-center gap-1.5 rounded-full border border-line bg-paper-2/60 px-3 text-[13px] text-ink hover:bg-paper-2"
            >
              <ExternalLink className="h-3.5 w-3.5" /> Gmail
            </a>
          )}
          {hasThread && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button type="button" aria-label="More" className="flex h-8 w-8 items-center justify-center rounded-full border border-line bg-paper-2/60 hover:bg-paper-2">
                  <MoreHorizontal className="h-4 w-4" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {contact.archived ? (
                  <DropdownMenuItem onClick={() => archiveMutation.mutate(false)}>
                    <ArchiveRestore className="mr-2 h-4 w-4" /> Unarchive conversation
                  </DropdownMenuItem>
                ) : (
                  <DropdownMenuItem onClick={() => archiveMutation.mutate(true)}>
                    <Archive className="mr-2 h-4 w-4" /> Archive conversation
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>
      </div>

      {!hasThread ? (
        <div className="rounded-xl border border-dashed border-line px-5 py-8 text-center">
          <p className="text-[13.5px] text-ink-2">No conversation with {contact.firstName || contact.name} yet.</p>
          <button
            type="button"
            onClick={() => coldDraftMutation.mutate()}
            disabled={(!contact.email && !canRevealAndDraft) || coldDraftMutation.isPending}
            title={canRevealAndDraft ? "Finds their email (8 credits, refunded if none is found) and drafts an outreach" : contact.email ? undefined : "Add an email address first"}
            className="mt-3 inline-flex h-9 items-center gap-2 rounded-full bg-[var(--accent,#4A60A8)] px-4 text-[13px] font-medium text-white disabled:opacity-50"
          >
            {coldDraftMutation.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <Mail className="h-4 w-4" />}
            {coldDraftMutation.isPending ? "Drafting…" : canRevealAndDraft ? "Find email & draft" : "Draft email"}
          </button>
          {canRevealAndDraft ? (
            <p className="mt-2 text-[12px] text-ink-3">8 credits to find their email. Refunded if none is found.</p>
          ) : !contact.email && <p className="mt-2 text-[12px] text-ink-3">Add an email address to draft one.</p>}
        </div>
      ) : (
        <>
          <div className="flex flex-col gap-3">
            {messagesQuery.isLoading ? (
              <div className="py-6 text-center text-[13px] text-ink-3">Loading conversation…</div>
            ) : messagesQuery.isError ? (
              <div className="py-6 text-center text-[13px] text-red-600">Couldn't load the conversation.</div>
            ) : gmailDisconnected && messages.length === 0 ? (
              <div className="py-6 text-center text-[13px] text-ink-3">Connect Gmail to load this conversation.</div>
            ) : messages.length === 0 ? (
              <div className="py-6 text-center text-[13px] text-ink-3">No messages yet.</div>
            ) : (
              [...messages]
                .sort((a, b) => (Date.parse(a.sentAt || "") || 0) - (Date.parse(b.sentAt || "") || 0))
                .map((m, i) => <MessageBubble key={m.messageId || i} message={m} contactName={contact.firstName || contact.name} />)
            )}
          </div>

          <div className="mt-4 rounded-xl border border-line bg-white">
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={gmailDisconnected ? "Connect Gmail to reply" : "Write a reply, or click Generate for a draft that reads the thread"}
              rows={draft ? 7 : 3}
              className="w-full resize-y rounded-t-xl bg-transparent px-4 py-3 text-[13.5px] leading-relaxed text-ink outline-none placeholder:text-ink-3"
            />
            <div className="flex items-center justify-between gap-2 border-t border-line px-3 py-2">
              <span className="truncate text-[12px] text-red-600">{draftError}</span>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={handleGenerate}
                  disabled={generating || gmailDisconnected}
                  className="inline-flex h-8 items-center gap-1.5 rounded-full border border-line px-3 text-[13px] text-ink hover:bg-paper-2 disabled:opacity-50"
                >
                  {generating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Sparkles className="h-3.5 w-3.5" />}
                  {generating ? "Generating…" : draft ? "Regenerate" : "Generate"}
                </button>
                <button
                  type="button"
                  onClick={() => sendMutation.mutate(draft.trim())}
                  disabled={!canSend || !user}
                  title={gmailDisconnected ? "Connect Gmail to send" : !draft.trim() ? "Write or generate a reply first" : undefined}
                  className="inline-flex h-8 items-center gap-1.5 rounded-full bg-[var(--accent,#4A60A8)] px-4 text-[13px] font-medium text-white disabled:opacity-50"
                >
                  {sendMutation.isPending ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
                  {sendMutation.isPending ? "Sending…" : "Send"}
                </button>
              </div>
            </div>
          </div>
        </>
      )}
    </section>
  );
}
