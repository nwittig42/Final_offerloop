/**
 * ReferralDraftModal — Phase 5 inline preview/edit for referral emails.
 *
 * Now supports two modes (eng review, June 2026):
 *
 *  mode='saved'      (default, original behavior)
 *    The job's referral_contact is non-null — the user already has a saved
 *    contact at this company. Modal opens straight in the Draft phase,
 *    pulling rich context (coffee-chat prep, JD/resume overlap, recent
 *    activity) via /referral-draft.
 *
 *  mode='discovery'  (auto-discover alumni at company)
 *    No saved contact. Modal opens in Confirm phase showing
 *    `AlumniDiscoveryPanel`. The student confirms the search, picks an
 *    alum from the Results phase, and the modal transitions to the Draft
 *    phase via /referral-draft/from-discovery (which persists the contact
 *    then runs the same build_referral_draft pipeline).
 *
 * Phase state ('confirm' | 'results' | 'draft') is OWNED HERE so the Draft
 * view stays unchanged across both modes and so the back-button transitions
 * remain in one component.
 */
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Loader2, RefreshCw, Send, Copy, AlertTriangle, Coffee, ChevronLeft,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { apiService, type FeedJob } from "@/services/api";
import { toast } from "@/hooks/use-toast";
import {
  AlumniDiscoveryPanel,
  type AlumniRow,
  type DiscoveryResult,
} from "./AlumniDiscoveryPanel";

export type ReferralDraftModalMode = "saved" | "discovery" | "prefilled";
export type ReferralDraftModalPhase = "confirm" | "results" | "draft";

interface ReferralDraftModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  job: FeedJob | null;
  /** Defaults to 'saved' for backward compat. */
  mode?: ReferralDraftModalMode;
  /** Only used when mode='discovery'. */
  studentSchool?: string | null;
  /** Discovery tier cap — Free 3 / Pro 5 / Elite 8. */
  tierMax?: number;
  /** Called once a discovery row results in a successful negative-cache
   * write or empty results, so the parent JobBoardPage can patch its
   * in-memory negativeCache map without a refetch. */
  onNegativeCacheHit?: (company: string) => void;
  /** Used only when mode='prefilled'. Skips the build_referral_draft call
   * because the caller already has the contact_id + subject + body (from
   * /referral-draft/from-find-recruiter or any other from-X endpoint).
   * Renders the draft phase immediately so the user lands on preview/edit. */
  prefilledContactId?: string;
  prefilledSubject?: string;
  prefilledBody?: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  prefilledContextUsed?: any;
  /** Shown in the title when the prefilled mode is used so the modal
   * chrome reads e.g. "Reach out to Patrick at Figma". Falls back to
   * "your contact" when omitted. */
  prefilledContactName?: string;
}

type DraftState = "loading" | "ready" | "committing" | "error";

const RELATIONSHIP_LABEL: Record<string, string> = {
  strong: "Coffee chat prep on file",
  moderate: "Some shared context",
  weak: "Cold — you haven't met",
};

export function ReferralDraftModal({
  open,
  onOpenChange,
  job,
  mode = "saved",
  studentSchool = null,
  tierMax = 3,
  onNegativeCacheHit,
  prefilledContactId,
  prefilledSubject,
  prefilledBody,
  prefilledContextUsed,
  prefilledContactName,
}: ReferralDraftModalProps) {
  const navigate = useNavigate();

  // ---- Phase state (discovery-aware) ----------------------------------
  // 'prefilled' lands directly on the draft phase with everything already
  // computed — the caller (e.g. FindHumansModal after /from-find-recruiter)
  // owns the build_referral_draft round-trip, so we don't repeat it here.
  const initialPhase: ReferralDraftModalPhase =
    mode === "discovery" ? "confirm" : "draft";
  const [phase, setPhase] = useState<ReferralDraftModalPhase>(initialPhase);

  // ---- Discovery-phase state ------------------------------------------
  const [discoveryResult, setDiscoveryResult] =
    useState<DiscoveryResult | null>(null);
  const [discoveryError, setDiscoveryError] = useState<string | null>(null);
  const [isDiscovering, setIsDiscovering] = useState(false);
  const [discoveryLoadingMsg, setDiscoveryLoadingMsg] = useState<string>("");
  const [selectingPdlId, setSelectingPdlId] = useState<string | null>(null);

  // The persisted contact_id once we've handed an alum to build_referral_draft.
  // In saved mode this is set from job.referral_contact.contact_id; in
  // discovery mode it's set from the /from-discovery response.
  const [draftContactId, setDraftContactId] = useState<string | null>(null);

  // ---- Draft-phase state (same as original modal) ---------------------
  const [draftState, setDraftState] = useState<DraftState>("loading");
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [draftError, setDraftError] = useState<string | null>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [contextUsed, setContextUsed] = useState<any>(null);

  // ---- Reset on open/close --------------------------------------------
  useEffect(() => {
    if (open) {
      setPhase(initialPhase);
      setDiscoveryResult(null);
      setDiscoveryError(null);
      setSelectingPdlId(null);
      if (mode === "saved" && job?.referral_contact) {
        setDraftContactId(job.referral_contact.contact_id);
      } else if (mode === "prefilled" && prefilledContactId) {
        // Land straight on the draft phase with the LLM output the caller
        // already produced. No second build_referral_draft round-trip.
        setDraftContactId(prefilledContactId);
        setSubject(prefilledSubject ?? "");
        setBody(prefilledBody ?? "");
        setContextUsed(prefilledContextUsed ?? null);
        setDraftState("ready");
      } else {
        setDraftContactId(null);
      }
    } else {
      setSubject("");
      setBody("");
      setDraftError(null);
      setContextUsed(null);
      setDiscoveryResult(null);
      setSelectingPdlId(null);
      setDraftContactId(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, mode, job?.job_id]);

  // ---- Generate draft (saved mode: on open; discovery mode: after select)
  // In 'prefilled' mode the caller already produced the draft text, so we
  // skip the build_referral_draft call here — generate() would just burn a
  // second OpenAI round-trip for no UX win.
  useEffect(() => {
    if (!open) return;
    if (phase !== "draft") return;
    if (!draftContactId) return;
    if (mode === "prefilled" && prefilledSubject && prefilledBody) return;
    void generate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, phase, draftContactId]);

  const generate = async () => {
    if (!job || !draftContactId) return;
    setDraftState("loading");
    setDraftError(null);
    try {
      const result = await apiService.draftReferralEmail({
        contact_id: draftContactId,
        job: {
          job_id: job.job_id,
          title: job.title,
          company: job.company,
          location: typeof job.location === "string" ? job.location : undefined,
          apply_url: job.apply_url,
          structured: job.structured,
        },
      });
      if (!result.ok || !result.subject || !result.body) {
        setDraftError(result.error || "Couldn't draft an email. Try again.");
        setDraftState("error");
        return;
      }
      setSubject(result.subject);
      setBody(result.body);
      setContextUsed(result.context_used || null);
      setDraftState("ready");
    } catch (e) {
      setDraftError(e instanceof Error ? e.message : "Network error.");
      setDraftState("error");
    }
  };

  // ---- Discovery: confirm → results --------------------------------------
  const runDiscovery = async (opts: {
    allowDropTitle?: boolean;
    allowNoSchoolFallback?: boolean;
  }) => {
    if (!job) return;
    setIsDiscovering(true);
    setDiscoveryError(null);
    setPhase("results");
    setDiscoveryLoadingMsg(
      opts.allowNoSchoolFallback
        ? `Trying recent hires at ${job.company}…`
        : opts.allowDropTitle
          ? "Trying without title filter…"
          : `Searching ${studentSchool ?? "your school"} alumni at ${job.company}…`,
    );
    try {
      const result = await apiService.discoverAlumniForJob({
        job_id: job.job_id,
        company: job.company,
        title: job.title,
        allow_drop_title: !!opts.allowDropTitle,
        allow_no_school_fallback: !!opts.allowNoSchoolFallback,
      });
      // Backend error responses carry `code` and the SPA renders inline.
      if ((result as { code?: string }).code) {
        const code = (result as { code: string }).code;
        if (code === "no_school") {
          // Sticks the panel into the no-school inline prompt by falling
          // through with no result + clearing the parent prop.
          setDiscoveryError(null);
        } else if (code === "pdl_timeout") {
          setDiscoveryError(
            "Search timed out. Try again — the lookup can be slow on first run.",
          );
        } else {
          setDiscoveryError("Couldn't run discovery. Try again.");
        }
        setDiscoveryResult(null);
      } else {
        setDiscoveryResult(result as DiscoveryResult);
        if ((result as DiscoveryResult).contacts.length === 0) {
          onNegativeCacheHit?.(job.company);
        }
      }
    } catch (e) {
      setDiscoveryError(e instanceof Error ? e.message : "Network error.");
      setDiscoveryResult(null);
    } finally {
      setIsDiscovering(false);
    }
  };

  // ---- Discovery: row select → persist + draft -------------------------
  const onSelectAlum = async (pdlId: string) => {
    if (!job) return;
    setSelectingPdlId(pdlId);
    try {
      const result = await apiService.draftReferralFromDiscovery({
        job_id: job.job_id,
        pdl_id: pdlId,
        job: {
          job_id: job.job_id,
          title: job.title,
          company: job.company,
          location: typeof job.location === "string" ? job.location : undefined,
          apply_url: job.apply_url,
          structured: job.structured,
        },
      });
      if (result.code === "discovery_expired") {
        setDiscoveryError(
          "The discovery results expired. Rerun discovery to pick an alum.",
        );
        setPhase("confirm");
        setDiscoveryResult(null);
        setSelectingPdlId(null);
        return;
      }
      if (!result.ok || !result.contact_id) {
        toast({
          title: "Couldn't draft from this alum",
          description: result.error || "Try again or pick another.",
          variant: "destructive",
        });
        setSelectingPdlId(null);
        return;
      }
      // Pull the subject/body the backend already generated and jump to
      // the Draft phase. We DON'T re-run generate() since /from-discovery
      // already called build_referral_draft and returned the text.
      setDraftContactId(result.contact_id);
      if (result.subject && result.body) {
        setSubject(result.subject);
        setBody(result.body);
        setContextUsed(result.context_used || null);
        setDraftState("ready");
      } else {
        // Fall back to draft-phase generate() with the new contact_id.
        setDraftState("loading");
      }
      setPhase("draft");
    } catch (e) {
      toast({
        title: "Couldn't draft from this alum",
        description: e instanceof Error ? e.message : "Network error.",
        variant: "destructive",
      });
    } finally {
      setSelectingPdlId(null);
    }
  };

  // ---- Existing draft-commit path (unchanged) -------------------------
  const commitAndOpen = async () => {
    if (!draftContactId || !subject.trim() || !body.trim()) return;
    setDraftState("committing");
    try {
      const result = await apiService.commitReferralDraft({
        contact_id: draftContactId,
        subject: subject.trim(),
        body: body.trim(),
      });
      if (result.ok && result.gmailUrl) {
        window.open(result.gmailUrl, "_blank", "noopener,noreferrer");
        onOpenChange(false);
        return;
      }
      if (result.error === "gmail_not_connected") {
        toast({
          title: "Gmail isn't connected",
          description:
            "Connect Gmail in Account Settings to create drafts automatically. " +
            "Your draft text has been kept here so you can copy it.",
          variant: "destructive",
        });
        setDraftState("ready");
        return;
      }
      toast({
        title: "Couldn't create the Gmail draft",
        description: result.error || "Try again or copy the text below.",
        variant: "destructive",
      });
      setDraftState("ready");
    } catch (e) {
      toast({
        title: "Couldn't create the Gmail draft",
        description: e instanceof Error ? e.message : "Network error.",
        variant: "destructive",
      });
      setDraftState("ready");
    }
  };

  const copyEmail = async () => {
    const text = `Subject: ${subject}\n\n${body}`;
    try {
      await navigator.clipboard.writeText(text);
      toast({ title: "Copied to clipboard" });
    } catch {
      toast({
        title: "Couldn't copy",
        description: "Select the text manually and copy.",
        variant: "destructive",
      });
    }
  };

  // ---- Render ---------------------------------------------------------

  const contactName =
    mode === "saved" && job?.referral_contact
      ? job.referral_contact.name
      : mode === "prefilled" && prefilledContactName
        ? prefilledContactName
        : "your contact";
  const firstName = contactName.split(/\s+/)[0];
  const company = job?.company || "the company";
  const relationship = contextUsed?.relationship as string | undefined;
  const qualityIssues = (contextUsed?.quality_issues as string[]) || [];
  const overlapCount = (contextUsed?.overlap_count as number) || 0;

  // Title varies by mode + phase so the chrome reflects what the user is doing.
  let title: string;
  let description: string;
  if (mode === "discovery" && phase === "confirm") {
    title = `Find alumni at ${company}`;
    description = "Confirm your search before we spend any credits.";
  } else if (mode === "discovery" && phase === "results") {
    title = `Alumni at ${company}`;
    description = "Pick one to draft an email. Back to pick again any time.";
  } else if (mode === "discovery" && phase === "draft") {
    title = `Reach out to ${firstName} at ${company}`;
    description = "Review and edit before sending. Hit \"Open in Gmail\" to create the draft.";
  } else {
    title = `Reach out to ${firstName} at ${company}`;
    description = "Review and edit before sending. Hit \"Open in Gmail\" to create the draft.";
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>

        {/* Discovery phases (confirm / results) -------------------------- */}
        {mode === "discovery" && phase !== "draft" && job && (
          <AlumniDiscoveryPanel
            phase={phase as "confirm" | "results"}
            job={job}
            studentSchool={studentSchool}
            tierMax={tierMax}
            result={discoveryResult}
            isLoading={isDiscovering}
            loadingMessage={discoveryLoadingMsg}
            error={discoveryError}
            selectingPdlId={selectingPdlId}
            onConfirmSearch={runDiscovery}
            onSelectContact={onSelectAlum}
            onBack={() => {
              setPhase("confirm");
              setDiscoveryError(null);
            }}
            onGoOnboarding={() => {
              navigate("/onboarding/academics");
              onOpenChange(false);
            }}
          />
        )}

        {/* Draft phase (shared across modes) ----------------------------- */}
        {phase === "draft" && (
          <>
            {/* Back button when we got here through discovery */}
            {mode === "discovery" && (
              <Button
                variant="ghost"
                size="sm"
                className="self-start -mt-2"
                onClick={() => {
                  setPhase("results");
                  setDraftContactId(null);
                  setSubject("");
                  setBody("");
                  setContextUsed(null);
                }}
                disabled={draftState === "committing"}
              >
                <ChevronLeft className="w-3.5 h-3.5 mr-1" /> Pick a different alum
              </Button>
            )}

            {draftState === "loading" && (
              <div className="flex items-center gap-3 py-12 justify-center text-slate-500">
                <Loader2 className="w-4 h-4 animate-spin" />
                <span className="text-sm">
                  Pulling your coffee-chat prep, the JD requirements, and recent
                  activity to draft a referral…
                </span>
              </div>
            )}

            {draftState === "error" && (
              <div className="py-8 space-y-3">
                <div className="flex items-start gap-2 text-red-700">
                  <AlertTriangle className="w-4 h-4 mt-0.5" />
                  <div className="text-sm">{draftError || "Something went wrong."}</div>
                </div>
                <Button variant="outline" onClick={generate}>
                  <RefreshCw className="w-4 h-4 mr-2" /> Try again
                </Button>
              </div>
            )}

            {(draftState === "ready" || draftState === "committing") && (
              <div className="space-y-4">
                {/* Context strip — tells user what the LLM had to work with */}
                <div className="text-xs text-slate-500 flex flex-wrap gap-x-3 gap-y-1">
                  {relationship && (
                    <span>
                      <strong>Relationship:</strong>{" "}
                      {RELATIONSHIP_LABEL[relationship] || relationship}
                    </span>
                  )}
                  {contextUsed?.has_coffee_chat_prep && (
                    <span>· Used your coffee-chat prep</span>
                  )}
                  {contextUsed?.has_recent_activity && (
                    <span>· Cited recent activity</span>
                  )}
                  {overlapCount > 0 && (
                    <span>· {overlapCount} JD/resume match{overlapCount > 1 ? "es" : ""}</span>
                  )}
                </div>

                {relationship === "weak" && (
                  <div className="text-xs bg-amber-50 border border-amber-200 rounded px-3 py-2 flex items-start gap-2">
                    <AlertTriangle className="w-3.5 h-3.5 text-amber-600 mt-0.5 flex-shrink-0" />
                    <div className="text-amber-800 flex-1">
                      <strong>This is essentially a cold email.</strong> You haven't
                      connected with{" "}
                      {firstName} before,
                      so the draft asks for a short chat rather than a referral. Running
                      a Coffee Chat Prep first will give the next draft real research to
                      cite — much higher response rate.
                    </div>
                    <Button
                      size="sm"
                      variant="outline"
                      className="flex-shrink-0"
                      onClick={() => navigate("/contacts")}
                    >
                      <Coffee className="w-3.5 h-3.5 mr-1.5" /> Start prep
                    </Button>
                  </div>
                )}
                {relationship === "moderate" && overlapCount === 0 &&
                  !contextUsed?.has_coffee_chat_prep && (
                    <div className="text-xs bg-slate-50 border border-slate-200 rounded px-3 py-2 flex items-start gap-2 text-slate-700">
                      <div className="flex-1">
                        No coffee-chat prep on this contact yet — running one would let
                        the next draft cite specific things they've worked on.
                      </div>
                      <Button
                        size="sm"
                        variant="outline"
                        className="flex-shrink-0"
                        onClick={() => navigate("/contacts")}
                      >
                        <Coffee className="w-3.5 h-3.5 mr-1.5" /> Add prep
                      </Button>
                    </div>
                  )}

                {qualityIssues.length > 0 && (
                  <div className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded px-3 py-2">
                    Heads up — the draft tripped these quality checks: {qualityIssues.join(", ")}.
                    Worth re-reading before you send.
                  </div>
                )}

                <div className="space-y-1">
                  <label className="text-xs font-medium uppercase tracking-wide text-slate-500">
                    Subject
                  </label>
                  <input
                    type="text"
                    value={subject}
                    onChange={(e) => setSubject(e.target.value)}
                    className="w-full border border-slate-300 rounded px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
                    disabled={draftState === "committing"}
                    maxLength={120}
                  />
                </div>

                <div className="space-y-1">
                  <label className="text-xs font-medium uppercase tracking-wide text-slate-500">
                    Body
                  </label>
                  <textarea
                    value={body}
                    onChange={(e) => setBody(e.target.value)}
                    className="w-full border border-slate-300 rounded px-3 py-2 text-sm font-serif leading-relaxed focus:border-slate-500 focus:outline-none resize-y"
                    rows={9}
                    disabled={draftState === "committing"}
                    maxLength={4000}
                  />
                  <div className="text-xs text-slate-400 text-right">
                    {body.split(/\s+/).filter(Boolean).length} words
                  </div>
                </div>

                <div className="flex flex-wrap gap-2 pt-2 border-t border-slate-100">
                  <Button
                    onClick={commitAndOpen}
                    disabled={draftState === "committing" || !subject.trim() || !body.trim()}
                  >
                    {draftState === "committing" ? (
                      <>
                        <Loader2 className="w-4 h-4 mr-2 animate-spin" /> Creating draft…
                      </>
                    ) : (
                      <>
                        <Send className="w-4 h-4 mr-2" /> Open in Gmail
                      </>
                    )}
                  </Button>
                  <Button
                    variant="outline"
                    onClick={generate}
                    disabled={draftState === "committing"}
                    title="Discard edits and ask the model for a fresh draft"
                  >
                    <RefreshCw className="w-4 h-4 mr-2" /> Regenerate
                  </Button>
                  <Button variant="outline" onClick={copyEmail}>
                    <Copy className="w-4 h-4 mr-2" /> Copy
                  </Button>
                </div>
              </div>
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

// Type re-exports kept for the parent JobBoardPage that needs them.
export type { AlumniRow };
