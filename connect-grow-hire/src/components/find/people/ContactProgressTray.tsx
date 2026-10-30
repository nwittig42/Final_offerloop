// Floating tray on the Find page for people you've hit Contact on this
// session. Stays pinned bottom-right so you can keep scrolling while we find
// their email and write the draft, then offers View contact / See email.
import { Check, Loader2, Mail, X } from "lucide-react";
import { Link } from "react-router-dom";
import { APPLE_FONT, isContactBusy, type ContactState } from "./PersonCard";
import { PersonAvatar } from "./PersonAvatar";
import type { Person } from "./peopleModel";

export interface TrayItem {
  person: Person;
  state: ContactState;
  contactHref: string;
  emailHref: string | null;
}

function statusLine(item: TrayItem): string {
  const first = item.person.firstName || item.person.name;
  switch (item.state) {
    case "finding": return `Finding ${first}'s email`;
    case "working": return `Writing your email to ${first}`;
    case "drafted": return "Drafted. It's in your Gmail drafts";
    case "no_email": return "No email found. No credits charged";
    default: return "";
  }
}

export function ContactProgressTray({ items, onDismiss }: { items: TrayItem[]; onDismiss: (id: string) => void }) {
  if (items.length === 0) return null;
  return (
    <div
      className="fixed bottom-5 right-5 z-40 flex w-[340px] max-w-[calc(100vw-32px)] flex-col gap-2"
      style={{ fontFamily: APPLE_FONT }}
      aria-live="polite"
    >
      {items.map((item) => {
        const busy = isContactBusy(item.state);
        const drafted = item.state === "drafted";
        return (
          <div
            key={item.person.id}
            className="rounded-[18px] bg-white p-3 shadow-[0_2px_6px_rgba(0,0,0,0.06),0_12px_32px_rgba(0,0,0,0.12)]"
          >
            <div className="flex items-center gap-2.5">
              <PersonAvatar name={item.person.name} photoUrl={item.person.photoUrl} size={36} />
              <div className="min-w-0 flex-1">
                <div className="truncate text-[13.5px] font-semibold text-[#1D1D1F]">{item.person.name}</div>
                <div
                  className="flex items-center gap-1 truncate text-[12px]"
                  style={{ color: drafted ? "#1D7A3E" : busy ? "#0058B0" : "#86868B" }}
                >
                  {busy && <Loader2 className="h-3 w-3 shrink-0 animate-spin" />}
                  {drafted && <Check className="h-3 w-3 shrink-0" strokeWidth={3} />}
                  <span className="truncate">{statusLine(item)}</span>
                </div>
              </div>
              {!busy && (
                <button
                  type="button"
                  onClick={() => onDismiss(item.person.id)}
                  aria-label="Dismiss"
                  className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-[#86868B] hover:bg-[#F5F5F7]"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
            {busy && (
              <div className="mt-2.5 h-1 overflow-hidden rounded-full bg-[#D6E6FB]">
                <div
                  className="h-full rounded-full bg-[#0071E3] transition-[width] duration-700 ease-out"
                  style={{ width: item.state === "finding" ? "40%" : "80%" }}
                />
              </div>
            )}
            {drafted && (
              <div className="mt-2.5 flex gap-2">
                <Link
                  to={item.contactHref}
                  className="inline-flex h-8 flex-1 items-center justify-center rounded-full bg-[#F5F5F7] text-[13px] font-medium text-[#1D1D1F] hover:bg-[#E8E8ED]"
                >
                  View contact
                </Link>
                {item.emailHref ? (
                  <a
                    href={item.emailHref}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex h-8 flex-1 items-center justify-center gap-1.5 rounded-full bg-[#0071E3] text-[13px] font-medium text-white hover:bg-[#0077ED]"
                  >
                    <Mail className="h-3.5 w-3.5" /> See email
                  </a>
                ) : (
                  <Link
                    to={item.contactHref}
                    className="inline-flex h-8 flex-1 items-center justify-center gap-1.5 rounded-full bg-[#0071E3] text-[13px] font-medium text-white hover:bg-[#0077ED]"
                  >
                    <Mail className="h-3.5 w-3.5" /> See email
                  </Link>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
