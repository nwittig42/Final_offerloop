// Meeting prep for one contact, run in place. Same backend flow the old
// Meeting Prep page used: POST /coffee-chat-prep with the LinkedIn URL, then
// poll /coffee-chat-prep/<id> until a pdfUrl appears. Completed preps are
// matched back to contacts by LinkedIn URL (see contactsModel).
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Coffee, Check, Loader2 } from "lucide-react";
import { apiService } from "@/services/api";
import { COFFEE_CHAT_CREDITS } from "@/lib/constants";
import { useCreditsView } from "@/hooks/useCreditsView";
import { toast } from "@/hooks/use-toast";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { PREPS_KEY, type ContactRow } from "./contactsModel";
import { ActionButton } from "./ActionButton";

const POLL_MS = 3000;
const MAX_POLL_MS = 10000;
const MAX_POLLS = 200;

// Fetches a fresh signed URL (stored ones expire) and opens the PDF.
export async function openPrepPdf(prepId: string) {
  // Open the tab synchronously so the popup blocker treats it as user-initiated.
  const win = window.open("", "_blank");
  try {
    const { pdfUrl } = await apiService.downloadCoffeeChatPDF(prepId);
    if (!pdfUrl) throw new Error("The PDF isn't ready yet. Try again in a moment.");
    if (win) win.location.href = pdfUrl;
    else window.location.href = pdfUrl;
  } catch (e) {
    win?.close();
    toast({ title: "Couldn't open prep", description: e instanceof Error ? e.message : undefined, variant: "destructive" });
  }
}

export function MeetingPrepButton({ contact }: { contact: ContactRow }) {
  const queryClient = useQueryClient();
  const creditsView = useCreditsView();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState("");
  const stoppedRef = useRef(false);

  // Stop polling if the contact changes or the page unmounts. The prep keeps
  // generating server-side and shows as ready on the next load.
  useEffect(() => {
    stoppedRef.current = false;
    setRunning(false);
    setProgress("");
    return () => { stoppedRef.current = true; };
  }, [contact.id]);

  const prep = contact.prep;
  const prepReady = prep && (prep.status === "completed" || !!prep.pdfUrl);
  // A prep stuck in a working status for over 15 minutes is treated as dead
  // so the user can run it again.
  const prepInFlight = !!prep && !prepReady && prep.status !== "failed"
    && Date.now() - (Date.parse(prep.createdAt || "") || 0) < 15 * 60 * 1000;

  const poll = async (prepId: string) => {
    let interval = POLL_MS;
    for (let i = 0; i < MAX_POLLS && !stoppedRef.current; i++) {
      try {
        const res = await apiService.getCoffeeChatPrepStatus(prepId);
        if ("pdfUrl" in res && res.pdfUrl) return true;
        if ("status" in res && res.status === "failed") throw new Error(res.error || "Generation failed");
        if ("error" in res && !("status" in res)) throw new Error(res.error);
        const r = res as { stageLabel?: string };
        if (r.stageLabel) setProgress(r.stageLabel);
        interval = POLL_MS;
      } catch (e: any) {
        if (!(e?.status === 429 || String(e?.message).includes("429"))) throw e;
        interval = Math.min(interval * 2, MAX_POLL_MS);
      }
      await new Promise((r) => setTimeout(r, interval));
    }
    return false;
  };

  const start = async () => {
    setConfirmOpen(false);
    if (creditsView.balance < COFFEE_CHAT_CREDITS) {
      toast({ title: "Not enough credits", description: `A meeting prep costs ${COFFEE_CHAT_CREDITS} credits.`, variant: "destructive" });
      return;
    }
    setRunning(true);
    setProgress("Starting…");
    try {
      const res = await apiService.createCoffeeChatPrep({ linkedinUrl: contact.linkedinUrl });
      if ("error" in res) throw new Error(res.error);
      const done = await poll(res.prepId);
      if (done) toast({ title: "Meeting prep ready", description: contact.name });
    } catch (e) {
      toast({ title: "Meeting prep failed", description: e instanceof Error ? e.message : "Try again.", variant: "destructive" });
    } finally {
      if (!stoppedRef.current) setRunning(false);
      queryClient.invalidateQueries({ queryKey: PREPS_KEY });
    }
  };

  if (prepReady) {
    return (
      <ActionButton
        icon={<Check className="h-4 w-4" />}
        label="Prep ready"
        tone="success"
        onClick={() => openPrepPdf(prep!.id)}
      />
    );
  }

  if (running || prepInFlight) {
    return (
      <ActionButton
        icon={<Loader2 className="h-4 w-4 animate-spin" />}
        label={running ? progress || "Preparing…" : "Preparing…"}
        disabled
      />
    );
  }

  return (
    <>
      <ActionButton
        icon={<Coffee className="h-4 w-4" />}
        label="Meeting prep"
        disabled={!contact.linkedinUrl}
        title={contact.linkedinUrl ? `${COFFEE_CHAT_CREDITS} credits` : "Add a LinkedIn URL to prep"}
        onClick={() => setConfirmOpen(true)}
      />
      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Prep for a meeting with {contact.name}?</AlertDialogTitle>
            <AlertDialogDescription>
              We'll research their background and company and build a one-page prep sheet with talking
              points and questions. Uses {COFFEE_CHAT_CREDITS} credits.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={start}>Run prep</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
