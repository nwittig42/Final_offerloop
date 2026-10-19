import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Check, Inbox, Users } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { UpgradeModal } from "@/components/gates/UpgradeModal";
import { apiService, type PendingShare } from "@/services/api";
import ShareScout from "@/assets/share-scout.jpeg";

// Friendly primary action — brand blue, used across the share cards.
const PRIMARY_BTN =
  "rounded-lg bg-[#3B82F6] hover:bg-[#2563EB] text-white shadow-sm gap-1.5";
const GHOST_BTN =
  "mt-0 rounded-lg border-slate-200 text-ink-2 hover:bg-slate-50";

const kindNoun = (k: PendingShare["kind"], n: number) => {
  const nouns: Record<PendingShare["kind"], [string, string]> = {
    contacts: ["contact", "contacts"],
    companies: ["company", "companies"],
    hiringManagers: ["hiring manager", "hiring managers"],
  };
  const [singular, plural] = nouns[k] ?? nouns.contacts;
  return n === 1 ? singular : plural;
};

/**
 * On login/refresh, checks for pending contact shares addressed to the user and
 * presents them one at a time. Pro/Elite can accept (imports the records, which
 * render green in My Network); free users are pushed to the Pro free-trial modal
 * and the share stays pending until they upgrade.
 */
export default function PendingShareModal() {
  const { user, isLoading } = useFirebaseAuth();
  const { toast } = useToast();
  const navigate = useNavigate();
  const [queue, setQueue] = useState<PendingShare[]>([]);
  const [busy, setBusy] = useState(false);
  const [showUpgrade, setShowUpgrade] = useState(false);
  const [imported, setImported] = useState<{ count: number } | null>(null);

  const isPro = user?.tier === "pro" || user?.tier === "elite";

  useEffect(() => {
    if (isLoading || !user || user.needsOnboarding) return;
    let cancelled = false;
    apiService
      .getPendingShares()
      .then((res) => {
        if (cancelled || !("shares" in res) || !res.shares.length) return;
        setQueue(res.shares);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [isLoading, user?.uid, user?.needsOnboarding]);

  const current = queue[0];
  const dismissCurrent = () => setQueue((q) => q.slice(1));

  const onAccept = async () => {
    if (!current) return;
    if (!isPro) {
      // Keep the share pending; push the Pro free-trial upsell.
      setShowUpgrade(true);
      return;
    }
    setBusy(true);
    try {
      const res = await apiService.acceptShare(current.id);
      if ("error" in res) {
        if (res.current_tier === "free") {
          setShowUpgrade(true);
          return;
        }
        toast({ title: res.error, variant: "destructive" });
        return;
      }
      setImported({ count: res.imported ?? current.count });
      dismissCurrent();
    } catch (e: any) {
      console.error("[shares] accept failed", e);
      toast({
        title: e?.needsAuth
          ? "Your session expired — please sign in again."
          : "Couldn't accept the share. Please try again.",
        variant: "destructive",
      });
    } finally {
      setBusy(false);
    }
  };

  const onDecline = async () => {
    if (!current) return;
    setBusy(true);
    try {
      await apiService.declineShare(current.id);
      dismissCurrent();
    } catch (e: any) {
      console.error("[shares] decline failed", e);
      toast({ title: "Couldn't decline the share. Please try again.", variant: "destructive" });
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <AlertDialog open={!!current && !showUpgrade}>
        <AlertDialogContent>
          <div className="flex flex-col items-center text-center">
            <img
              src={ShareScout}
              alt=""
              className="h-20 w-20 rounded-2xl object-contain"
              style={{ background: "#F7F5EC" }}
            />
            <AlertDialogHeader className="mt-3 space-y-1">
              <AlertDialogTitle className="text-center text-xl">
                {current?.fromName} shared {current?.count}{" "}
                {current && kindNoun(current.kind, current.count)} with you
              </AlertDialogTitle>
              <AlertDialogDescription className="text-center">
                Accept to add them to your network.{" "}
                {isPro ? "" : "Receiving shared contacts is a Pro feature."}
              </AlertDialogDescription>
            </AlertDialogHeader>
          </div>
          <AlertDialogFooter className="mt-5 gap-2 sm:justify-center">
            <AlertDialogCancel disabled={busy} onClick={onDecline} className={GHOST_BTN}>
              Decline
            </AlertDialogCancel>
            <AlertDialogAction
              disabled={busy}
              onClick={(e) => {
                e.preventDefault();
                onAccept();
              }}
              className={PRIMARY_BTN}
            >
              <Check className="h-4 w-4" />
              {busy ? "Accepting…" : "Accept"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <UpgradeModal
        open={showUpgrade}
        onOpenChange={setShowUpgrade}
        feature="bulk_drafting"
        reason="Receiving shared contacts is a Pro feature. Start a free trial to accept them."
        currentTier={user?.tier || "free"}
      />

      <AlertDialog open={!!imported} onOpenChange={() => setImported(null)}>
        <AlertDialogContent>
          <div className="flex flex-col items-center text-center">
            <img
              src={ShareScout}
              alt=""
              className="h-20 w-20 rounded-2xl object-contain"
              style={{ background: "#F7F5EC" }}
            />
            <AlertDialogHeader className="mt-3 space-y-1">
              <AlertDialogTitle className="text-center text-xl">
                {imported?.count} added to your network
              </AlertDialogTitle>
              <AlertDialogDescription className="text-center">
                They're highlighted in green. Draft emails to them, or open them in your inbox.
              </AlertDialogDescription>
            </AlertDialogHeader>
          </div>
          <AlertDialogFooter className="mt-5 gap-2 sm:justify-center">
            <Button
              variant="outline"
              className={GHOST_BTN}
              onClick={() => {
                setImported(null);
                navigate("/contacts");
              }}
            >
              <Users className="h-4 w-4" />
              View in network
            </Button>
            <Button
              className={PRIMARY_BTN}
              onClick={() => {
                setImported(null);
                navigate("/contacts");
              }}
            >
              <Inbox className="h-4 w-4" />
              View in inbox
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
