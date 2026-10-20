// "Info" on a person card: everything we know about them in one place.
import { GraduationCap, Linkedin, Mail, MapPin, Briefcase } from "lucide-react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { CompanyLogo } from "@/components/CompanyLogo";
import { linkedInHref } from "@/components/contacts/contactsModel";
import { relationFor, type Person } from "./peopleModel";
import { PersonAvatar } from "./PersonAvatar";
import type { ContactState } from "./PersonCard";

interface Props {
  person: Person | null;
  contactState: ContactState;
  onContact: () => void;
  onClose: () => void;
}

function Row({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-3 py-2 text-[14px] text-ink">
      <span className="mt-0.5 text-ink-3">{icon}</span>
      <div className="min-w-0 break-words">{children}</div>
    </div>
  );
}

export function PersonInfoDialog({ person, contactState, onContact, onClose }: Props) {
  const linkedIn = linkedInHref(person?.linkedinUrl);
  return (
    <Dialog open={!!person} onOpenChange={(o) => { if (!o) onClose(); }}>
      <DialogContent className="max-w-md">
        {person && (
          <>
            <DialogHeader>
              <div className="flex items-center gap-4">
                <PersonAvatar name={person.name} photoUrl={person.photoUrl} size={64} />
                <div className="min-w-0 text-left">
                  <DialogTitle className="text-[20px]">{person.name}</DialogTitle>
                  <DialogDescription className="mt-0.5">
                    {[person.title, person.company].filter(Boolean).join(" at ")}
                  </DialogDescription>
                </div>
              </div>
            </DialogHeader>

            <div className="divide-y divide-line">
              {person.company && (
                <Row icon={<Briefcase className="h-4 w-4" />}>
                  <div className="flex items-center gap-2">
                    <CompanyLogo company={person.company} size={22} rounded={5} />
                    <span>{person.company}</span>
                    {person.title && <span className="text-ink-3">· {relationFor(person.title)}</span>}
                  </div>
                </Row>
              )}
              {person.location && <Row icon={<MapPin className="h-4 w-4" />}>{person.location}</Row>}
              {person.school && <Row icon={<GraduationCap className="h-4 w-4" />}>{person.school}</Row>}
              {person.email && <Row icon={<Mail className="h-4 w-4" />}>{person.email}</Row>}
              {linkedIn && (
                <Row icon={<Linkedin className="h-4 w-4" />}>
                  <a href={linkedIn} target="_blank" rel="noopener noreferrer" className="text-[var(--accent,#4A60A8)] hover:underline">
                    {person.linkedinUrl.replace(/^https?:\/\/(www\.)?/, "")}
                  </a>
                </Row>
              )}
            </div>

            {person.reasons.length > 0 && (
              <div className="mt-2">
                <h4 className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-ink-3">Why we picked them</h4>
                <ul className="space-y-1">
                  {person.reasons.map((r) => (
                    <li key={r} className="flex gap-2 text-[13.5px] text-ink-2">
                      <span className="mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full bg-[var(--accent,#4A60A8)]" />
                      {r}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {person.briefing && (
              <div className="mt-2">
                <h4 className="mb-1.5 text-[11px] font-semibold uppercase tracking-[0.08em] text-ink-3">Background</h4>
                <p className="whitespace-pre-wrap text-[13.5px] leading-relaxed text-ink-2">{person.briefing}</p>
              </div>
            )}

            <div className="mt-3 flex justify-end">
              {contactState === "drafted" ? (
                <span className="text-[13px] font-medium text-emerald-700">Drafted. It's in Contacts.</span>
              ) : (
                <button
                  type="button"
                  onClick={onContact}
                  disabled={person.source === "sample" || contactState !== "idle"}
                  className="h-9 rounded-lg bg-[var(--accent,#4A60A8)] px-5 text-[13px] font-medium text-white disabled:opacity-50"
                >
                  {contactState === "finding" ? "Finding email…" : contactState === "working" ? "Drafting…" : contactState === "no_email" ? "No email found" : "Contact"}
                </button>
              )}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
