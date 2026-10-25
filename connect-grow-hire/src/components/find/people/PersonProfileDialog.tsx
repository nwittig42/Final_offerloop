// A person's profile, opened by clicking their card on Find or in an org
// directory (OFF-57). Styled like the walkthrough's profile view: portrait
// on the left, serif name, "Why them", big buttons. Shows only what we have;
// a section with no data doesn't render. Meeting prep saves them to
// Contacts first (after the confirm and the credit check), then starts the
// same prep MeetingPrepButton runs, so it lands on that contact.
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Bookmark, Coffee, Loader2 } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { ToastAction } from "@/components/ui/toast";
import { apiService } from "@/services/api";
import { COFFEE_CHAT_CREDITS } from "@/lib/constants";
import { useCreditsView } from "@/hooks/useCreditsView";
import { toast } from "@/hooks/use-toast";
import { linkedInHref, PREPS_KEY } from "@/components/contacts/contactsModel";
import { ProfilePortrait } from "@/components/walkthrough/MatchCards";
import { OB } from "@/pages/onboardingTheme";
import { sameUniversity } from "@/lib/universityLogos";
import { relationFor, type Person } from "./peopleModel";
import { contactButtonTitle, fitLabel, isContactBusy, type ContactState } from "./PersonCard";

interface PersonProfileDialogProps {
  person: Person | null;
  contactState: ContactState;
  // The card's exact contact handler, so credits and refunds behave the same.
  onContact: () => void;
  // Where "Open in Contacts" goes once they're drafted (same as the card's).
  contactHref?: string;
  isBookmarked: boolean;
  onToggleBookmark: () => void;
  ensureSaved: (p: Person, opts?: { quiet?: boolean }) => Promise<boolean>;
  // The signed-in student's school, for the "Went to …" chip.
  userSchool?: string | null;
  // Their email when we have it (the person's own, or their Contacts row's).
  email?: string;
  onClose: () => void;
}

const norm = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

// "Why them": the ranker's reasons, then the briefing, skipping anything
// that repeats a line already shown (the same rule matchReasons uses).
export function whyThem(p: Person): string[] {
  const out: string[] = [];
  for (const r of [...p.reasons, p.briefing]) {
    const k = norm(r || "");
    if (k && !out.some((o) => norm(o) === k || norm(o).includes(k))) out.push(r.trim());
  }
  return out;
}

const BTN: React.CSSProperties = {
  height: 52,
  borderRadius: 12,
  fontFamily: OB.fontBody,
  fontSize: 15,
  fontWeight: 600,
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  gap: 8,
  padding: "0 18px",
  whiteSpace: "nowrap",
};
const PRIMARY: React.CSSProperties = { ...BTN, flex: "2 1 220px", background: OB.primary, color: "#fff", boxShadow: OB.shadowBlue };
const SECONDARY: React.CSSProperties = { ...BTN, flex: "1 1 140px", background: "#fff", color: OB.heading, border: `1px solid ${OB.border}` };
const DISABLED: React.CSSProperties = { ...PRIMARY, background: OB.primary50, color: OB.ink4, boxShadow: "none", cursor: "not-allowed" };

function Chip({ children, tone = "plain" }: { children: React.ReactNode; tone?: "plain" | "accent" }) {
  return (
    <span
      className="inline-flex h-7 items-center rounded-full px-3 text-[12.5px] font-semibold"
      style={tone === "accent"
        ? { background: OB.primary100, color: OB.primaryDark }
        : { background: OB.primary50, color: OB.ink2 }}
    >
      {children}
    </span>
  );
}

export function PersonProfileDialog({
  person, contactState, onContact, contactHref = "/contacts", isBookmarked, onToggleBookmark, ensureSaved, userSchool = null, email = "", onClose,
}: PersonProfileDialogProps) {
  const queryClient = useQueryClient();
  const creditsView = useCreditsView();
  const navigate = useNavigate();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [starting, setStarting] = useState(false);

  const p = person;
  const linkedIn = linkedInHref(p?.linkedinUrl);
  const isSample = p?.source === "sample";

  // Order matters (spec, decision 3): confirm -> credits -> save -> prep.
  // Cancelling or lacking credits saves nothing; a failed prep leaves them saved.
  const runPrep = async () => {
    setConfirmOpen(false);
    if (!p) return;
    if (creditsView.balance < COFFEE_CHAT_CREDITS) {
      toast({ title: "Not enough credits", description: `A meeting prep costs ${COFFEE_CHAT_CREDITS} credits.`, variant: "destructive" });
      return;
    }
    setStarting(true);
    try {
      if (!(await ensureSaved(p, { quiet: true }))) return;
      const res = await apiService.createCoffeeChatPrep({ linkedinUrl: p.linkedinUrl });
      if ("error" in res) throw new Error(res.error);
      toast({
        title: "Meeting prep started",
        description: `${p.firstName || p.name} is saved in Contacts; the prep will appear there when it's ready.`,
        action: <ToastAction altText="View saved contacts" onClick={() => navigate("/contacts?filter=saved")}>View</ToastAction>,
      });
      queryClient.invalidateQueries({ queryKey: PREPS_KEY });
    } catch (e) {
      toast({ title: "Meeting prep failed", description: e instanceof Error ? e.message : "Try again.", variant: "destructive" });
    } finally {
      setStarting(false);
    }
  };

  const primary = () => {
    if (!p) return null;
    if (contactState === "drafted") {
      return <Link to={contactHref} style={PRIMARY}>Open in Contacts</Link>;
    }
    if (isContactBusy(contactState) || contactState === "no_email" || isSample) {
      const label = contactState === "finding" ? "Finding email…"
        : contactState === "working" ? "Writing…"
        : contactState === "no_email" ? "No email found"
        : p.source === "recommended" ? "See email & draft" : "Draft email";
      return (
        <button type="button" disabled style={DISABLED} title={contactState === "no_email" ? "No credits were charged" : undefined}>
          {isContactBusy(contactState) && <Loader2 className="h-4 w-4 animate-spin" />}
          {label}
        </button>
      );
    }
    return (
      <button type="button" onClick={onContact} style={PRIMARY} title={contactButtonTitle(p)}>
        {p.source === "recommended" ? "See email & draft" : "Draft email"}
      </button>
    );
  };

  const schools = p ? (p.schools?.length ? p.schools : p.school ? [p.school] : []) : [];
  const placeLine = p ? [...schools, p.location].filter(Boolean).join(" · ") : "";
  const relation = p?.title ? relationFor(p.title) : "Employee";
  const alumOf = p ? schools.find((s) => sameUniversity(s, userSchool)) : undefined;
  const fit = p ? fitLabel(p.matchScore) : null;
  const why = p ? whyThem(p) : [];

  return (
    <>
      <Dialog open={!!p} onOpenChange={(o) => { if (!o) onClose(); }}>
        <DialogContent
          // No title line means no description; tell Radix so it doesn't warn.
          {...(!p?.title && { "aria-describedby": undefined })}
          className="max-h-[92vh] max-w-[820px] overflow-y-auto rounded-[20px] p-0 sm:rounded-[20px]"
          style={{ fontFamily: OB.fontBody, color: OB.heading }}
        >
          {p && (
            <div className="grid gap-0 sm:grid-cols-[280px_minmax(0,1fr)]">
              <div className="p-5 sm:p-6 sm:pr-0">
                <ProfilePortrait match={p} style={{ aspectRatio: "1 / 1.25", width: "100%", borderRadius: 14 }} />
              </div>

              <div className="flex min-w-0 flex-col p-5 pt-0 sm:p-8 sm:pl-7">
                {p.company && (
                  <div className="text-[12px] font-semibold uppercase tracking-[0.1em]" style={{ color: OB.primary }}>{p.company}</div>
                )}
                <DialogTitle className="mt-1 text-[32px] font-semibold leading-[1.1]" style={{ fontFamily: OB.fontDisplay, color: OB.heading }}>
                  {p.name}
                </DialogTitle>
                {p.title && (
                  <DialogDescription className="mt-1.5 text-[15px]" style={{ color: OB.ink2 }}>
                    {[p.title, p.company].filter(Boolean).join(" at ")}
                  </DialogDescription>
                )}
                {placeLine && <div className="mt-3 text-[14px]" style={{ color: OB.ink3 }}>{placeLine}</div>}
                {email && (
                  <a href={`mailto:${email}`} className="mt-1 block w-fit max-w-full truncate text-[14px] hover:underline" style={{ color: OB.primary }}>
                    {email}
                  </a>
                )}
                {p.joinedYear ? <div className="mt-1 text-[14px]" style={{ color: OB.ink3 }}>Joined {p.joinedYear}</div> : null}

                {(p.orgBadge || alumOf || relation !== "Employee" || fit) && (
                  <div className="mt-4 flex flex-wrap gap-2">
                    {p.orgBadge && <Chip tone="accent">{p.orgBadge}</Chip>}
                    {alumOf && <Chip tone="accent">Went to {alumOf}</Chip>}
                    {relation !== "Employee" && <Chip>{relation}</Chip>}
                    {fit && <Chip tone={fit === "Strong fit" ? "accent" : "plain"}>{fit}</Chip>}
                  </div>
                )}

                {why.length > 0 && (
                  <div className="mt-6">
                    <h4 className="mb-2 text-[12px] font-semibold uppercase tracking-[0.1em]" style={{ color: OB.ink3 }}>Why them</h4>
                    <ul className="space-y-2">
                      {why.map((r) => (
                        <li key={r} className="flex gap-2.5 whitespace-pre-wrap text-[14.5px] leading-relaxed" style={{ color: OB.ink2 }}>
                          <span className="mt-[9px] h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: OB.primary }} />
                          {r}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                <div className="mt-7 flex flex-wrap gap-2.5">
                  {primary()}
                  {linkedIn && !isSample && (
                    <button
                      type="button"
                      onClick={() => setConfirmOpen(true)}
                      disabled={starting}
                      title={`${COFFEE_CHAT_CREDITS} credits`}
                      style={{ ...SECONDARY, opacity: starting ? 0.6 : 1 }}
                    >
                      {starting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Coffee className="h-4 w-4" />}
                      Meeting prep
                    </button>
                  )}
                  {!isSample && (
                    <button
                      type="button"
                      onClick={onToggleBookmark}
                      aria-pressed={isBookmarked}
                      style={{ ...SECONDARY, color: isBookmarked ? OB.primary : OB.heading }}
                    >
                      <Bookmark className="h-4 w-4" fill={isBookmarked ? "currentColor" : "none"} />
                      {isBookmarked ? "Saved" : "Save"}
                    </button>
                  )}
                  {linkedIn && (
                    <a href={linkedIn} target="_blank" rel="noopener noreferrer" style={SECONDARY}>
                      LinkedIn ↗
                    </a>
                  )}
                </div>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Prep for a meeting with {p?.name}?</AlertDialogTitle>
            <AlertDialogDescription>
              We'll research their background and company and build a one-page prep sheet with talking
              points and questions. Uses {COFFEE_CHAT_CREDITS} credits.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={runPrep}>Run prep</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
