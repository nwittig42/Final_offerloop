// Share contacts with another Offerloop account. Same /shares API My Network
// used; the recipient gets a popup to accept.
import { useEffect, useState } from "react";
import { Forward } from "lucide-react";
import ShareScout from "@/assets/share-scout.jpeg";
import { apiService } from "@/services/api";
import { toast } from "@/hooks/use-toast";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import type { ContactRow } from "./contactsModel";

interface Props {
  contacts: ContactRow[] | null; // null = closed
  onClose: () => void;
}

export function ShareContactsDialog({ contacts, onClose }: Props) {
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sharing, setSharing] = useState(false);
  const open = contacts !== null;

  useEffect(() => { if (open) { setEmail(""); setError(null); } }, [open]);

  const count = contacts?.length ?? 0;
  const subject = count === 1 ? contacts![0].name : `${count} contacts`;

  const submit = async () => {
    const to = email.trim().toLowerCase();
    if (!to) { setError("Enter an email."); return; }
    if (!contacts?.length) return;
    setSharing(true);
    setError(null);
    try {
      const items = contacts.map((c) => ({
        firstName: c.firstName, lastName: c.lastName, name: c.name, email: c.email,
        linkedinUrl: c.linkedinUrl, jobTitle: c.role, company: c.company, college: c.school, location: c.location,
      }));
      const res = await apiService.shareRecords({ toEmail: to, kind: "contacts", items });
      if ("error" in res) { setError(res.error); return; }
      toast({ title: `Shared with ${res.toName || to}` });
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.");
    } finally {
      setSharing(false);
    }
  };

  return (
    <AlertDialog open={open} onOpenChange={(o) => { if (!o && !sharing) onClose(); }}>
      <AlertDialogContent>
        <div className="flex flex-col items-center text-center">
          <img src={ShareScout} alt="" className="h-20 w-20 rounded-2xl object-contain" style={{ background: "#F7F5EC" }} />
          <AlertDialogHeader className="mt-3 space-y-1">
            <AlertDialogTitle className="text-center text-xl">Share {subject}</AlertDialogTitle>
            <AlertDialogDescription className="text-center">
              Enter the Offerloop account email to share with. They'll get a popup to accept.
            </AlertDialogDescription>
          </AlertDialogHeader>
        </div>
        <div className="mt-4">
          <input
            type="email"
            autoFocus
            value={email}
            onChange={(e) => { setEmail(e.target.value); setError(null); }}
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } }}
            placeholder="name@example.com"
            className="w-full rounded-lg border border-slate-200 px-3.5 py-2.5 text-sm outline-none focus:border-[#3B82F6] focus:ring-2 focus:ring-[#3B82F6]/20"
          />
          {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
        </div>
        <AlertDialogFooter className="mt-5 gap-2 sm:justify-center">
          <AlertDialogCancel disabled={sharing} className="mt-0">Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={(e) => { e.preventDefault(); submit(); }}
            disabled={sharing}
            className="gap-1.5 bg-[#3B82F6] text-white hover:bg-[#2563EB]"
          >
            <Forward className="h-4 w-4" />
            {sharing ? "Sharing…" : "Share"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
