// Person card for the Find gallery, drawn in Apple's visual language: one
// white surface with a soft shadow and no borders, the system font, a strict
// black/grey text scale, grey capsule chips, and capsule buttons (blue for
// the main action). Same content as the app's People card: who they are,
// where they work, why we picked them, Info and Contact.
import { Bookmark, GraduationCap, Loader2, Mail, MapPin, User, Users, X } from "lucide-react";
import { CompanyLogo } from "@/components/CompanyLogo";
import { linkedInHref } from "@/components/contacts/contactsModel";
import { relationFor, type Person } from "./peopleModel";
import { PersonAvatar } from "./PersonAvatar";
import { LinkedInLink } from "./LinkedInLink";
import { orgLogo } from "@/lib/orgLogos";
import { useEffect, useState } from "react";
import { MIN_LOGO_PX, findUniversity, sameUniversity, universityLogoSources } from "@/lib/universityLogos";

export type ContactState = "idle" | "working" | "drafted" | "no_email";

// Apple-like system stack: SF Pro on Apple devices, Inter elsewhere.
export const APPLE_FONT = '-apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", system-ui, sans-serif';

const INK = "#1D1D1F";     // primary text
const INK_2 = "#6E6E73";   // secondary text
const INK_3 = "#86868B";   // tertiary text, icons
const FILL = "#F5F5F7";    // grey fills: chips, secondary buttons

function FitBadge({ score, sample }: { score: number | null; sample: boolean }) {
  if (sample) {
    return <span className="rounded-full px-2 py-[3px] text-[11px] font-semibold" style={{ background: FILL, color: INK_3 }}>Example</span>;
  }
  if (score === null || score < 6.5) return null;
  const strong = score >= 8;
  return (
    <span
      className="rounded-full px-2 py-[3px] text-[11px] font-semibold"
      style={strong ? { background: "#E8F1FD", color: "#0066CC" } : { background: FILL, color: INK_2 }}
    >
      {strong ? "Strong fit" : "Good fit"}
    </span>
  );
}

function Chip({ icon, children }: { icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <span className="inline-flex h-6 max-w-full items-center gap-1 rounded-full px-2.5 text-[12px] font-medium" style={{ background: FILL, color: "#424245" }}>
      <span className="shrink-0" style={{ color: INK_3 }}>{icon}</span>
      <span className="truncate">{children}</span>
    </span>
  );
}

// School pill: the school's logo from the library (src/lib/universityLogos)
// in place of its name. A school we have no logo for, or whose logo fails
// to load, keeps the text pill. When the person went to the student's own
// school, the pill takes the school's color and says "Alum".
function SchoolChip({ school, sameSchool }: { school: string; sameSchool: boolean }) {
  const uni = findUniversity(school);
  const sources = uni ? universityLogoSources(uni) : [];
  const [idx, setIdx] = useState(0);
  useEffect(() => setIdx(0), [school]);
  const logo = sources[idx];
  const color = uni?.color ?? "#0066CC";

  const highlight: React.CSSProperties = sameSchool
    ? { background: `${color}1A`, boxShadow: `inset 0 0 0 1px ${color}59` }
    : { background: FILL };
  const title = sameSchool ? `${school}, same school as you` : school;
  const alum = sameSchool && (
    <span className="text-[11px] font-semibold" style={{ color }}>Alum</span>
  );

  if (!logo) {
    return (
      <span
        className="inline-flex h-6 max-w-full items-center gap-1 rounded-full px-2.5 text-[12px] font-medium"
        style={{ ...highlight, color: sameSchool ? color : "#424245" }}
        title={title}
      >
        <GraduationCap className="h-3 w-3 shrink-0" style={{ color: sameSchool ? color : INK_3 }} />
        <span className="truncate">{school}</span>
      </span>
    );
  }
  return (
    <span className="inline-flex h-6 items-center gap-1.5 rounded-full px-2.5" style={highlight} title={title}>
      <img
        src={logo}
        alt={school}
        className="h-4 w-auto max-w-[40px] object-contain"
        loading="lazy"
        onLoad={(e) => { if (e.currentTarget.naturalWidth < MIN_LOGO_PX) setIdx((i) => i + 1); }}
        onError={() => setIdx((i) => i + 1)}
      />
      {alum}
    </span>
  );
}

interface PersonCardProps {
  person: Person;
  contactState: ContactState;
  onContact: () => void;
  onInfo: () => void;
  onDismiss?: () => void;
  // Top connection: gold ring + "Top match" capsule (placeholder until the
  // pairing algorithm picks these for real).
  featured?: boolean;
  bookmarked?: boolean;
  onToggleBookmark?: () => void;
  // The signed-in student's school: highlights the pill on fellow alumni.
  userSchool?: string | null;
}

export function PersonCard({ person, contactState, onContact, onInfo, onDismiss, featured = false, bookmarked = false, onToggleBookmark, userSchool = null }: PersonCardProps) {
  const linkedIn = linkedInHref(person.linkedinUrl);
  const isSample = person.source === "sample";
  const reason = person.reasons[0] || (featured ? "Most in common with you" : "");

  return (
    <article
      className="group relative flex flex-col rounded-[22px] bg-white p-[18px] transition-all duration-200 ease-out hover:-translate-y-0.5 hover:shadow-[0_2px_4px_rgba(0,0,0,0.04),0_12px_32px_rgba(0,0,0,0.08)]"
      style={{ fontFamily: APPLE_FONT, boxShadow: "0 1px 2px rgba(0,0,0,0.04), 0 4px 14px rgba(0,0,0,0.05)" }}
    >
      {onDismiss && !isSample && (
        <button
          type="button"
          onClick={onDismiss}
          aria-label={`Hide ${person.name}`}
          title="Not interested"
          className="absolute -left-2 -top-2 flex h-6 w-6 items-center justify-center rounded-full bg-white opacity-0 shadow-[0_1px_4px_rgba(0,0,0,0.15)] transition-opacity focus:opacity-100 group-hover:opacity-100"
          style={{ color: INK_2 }}
        >
          <X className="h-3 w-3" strokeWidth={2.5} />
        </button>
      )}

      {/* Header */}
      <div className="flex items-start gap-3">
        <PersonAvatar name={person.name} photoUrl={person.photoUrl} size={featured ? 56 : 52} ring={featured} />
        <div className="min-w-0 flex-1 pt-0.5">
          <div className="truncate text-[16px] font-semibold leading-tight tracking-[-0.01em]" style={{ color: INK }}>
            {person.name}
          </div>
          <div className="mt-0.5 line-clamp-2 text-[13px] leading-[1.3]" style={{ color: INK_2 }}>
            {person.title || " "}
          </div>
        </div>
        {/* Class year from an org directory (OFF-42): only when it's real. */}
        {person.gradYear ? (
          <span
            className="-mt-1 shrink-0 select-none leading-none"
            title={`Class of ${person.gradYear}`}
            aria-label={`Class of ${person.gradYear}`}
            style={{ fontFamily: '"Instrument Serif", Georgia, serif', fontStyle: "italic", fontSize: 30, color: "#8A6116" }}
          >
            ’{String(person.gradYear % 100).padStart(2, "0")}
          </span>
        ) : null}
        {onToggleBookmark && !isSample && (
          <button
            type="button"
            onClick={onToggleBookmark}
            aria-pressed={bookmarked}
            aria-label={bookmarked ? `Remove ${person.name} from Saved` : `Save ${person.name} to Contacts`}
            title={bookmarked ? "Saved to Contacts" : "Save for later"}
            className="-mr-1 -mt-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-full transition-colors hover:bg-[#F5F5F7] active:bg-[#E8E8ED]"
            style={{ color: bookmarked ? "#0071E3" : INK_3 }}
          >
            <Bookmark className="h-[18px] w-[18px]" fill={bookmarked ? "currentColor" : "none"} strokeWidth={2} />
          </button>
        )}
      </div>

      {/* Company */}
      {person.company && (
        <div className="mt-4 flex items-center gap-2.5">
          <CompanyLogo company={person.company} size={32} rounded={8} bordered={false} />
          <div className="min-w-0 flex-1">
            <div className="truncate text-[13.5px] font-semibold" style={{ color: INK }}>{person.company}</div>
            {person.location && (
              <div className="flex items-center gap-1 truncate text-[12px]" style={{ color: INK_3 }}>
                <MapPin className="h-3 w-3 shrink-0" /> {person.location}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Email, for org directory alumni we already have one for (OFF-42) */}
      {person.email && (person.source === "directory" || person.orgBadge) && (
        <a
          href={`mailto:${person.email}`}
          className="mt-2.5 flex min-w-0 items-center gap-1.5 text-[12.5px] hover:underline"
          style={{ color: INK_2 }}
          title={person.email}
        >
          <Mail className="h-3.5 w-3.5 shrink-0" style={{ color: INK_3 }} />
          <span className="truncate">{person.email}</span>
        </a>
      )}

      {/* Chips */}
      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        {featured ? (
          <span className="rounded-full px-2 py-[3px] text-[11px] font-semibold" style={{ background: "#FBF3DF", color: "#8A6116" }}>
            Top match
          </span>
        ) : (
          <FitBadge score={person.matchScore} sample={isSample} />
        )}
        {person.orgBadge && (
          <span className="inline-flex h-6 max-w-full items-center gap-1 rounded-full px-2.5 text-[12px] font-semibold" style={{ background: "#EEF1FA", color: "#3A4E8C" }}>
            {orgLogo(person.orgId)
              ? <img src={orgLogo(person.orgId)!} alt="" aria-hidden className="h-4 w-4 shrink-0 object-contain" />
              : <Users className="h-3 w-3 shrink-0" />}
            <span className="truncate">{person.orgBadge}</span>
          </span>
        )}
        {person.school && !person.orgBadge && <SchoolChip school={person.school} sameSchool={sameUniversity(person.school, userSchool)} />}
        {person.title && <Chip icon={<User className="h-3 w-3" />}>{relationFor(person.title)}</Chip>}
      </div>

      {reason && (
        <p className="mt-2.5 line-clamp-2 text-[12px] leading-[1.35]" style={{ color: INK_3 }}>{reason}</p>
      )}

      {/* Actions */}
      <div className="mt-auto flex gap-2 pt-4">
        {linkedIn && <LinkedInLink href={linkedIn} name={person.name} />}
        <button
          type="button"
          onClick={onInfo}
          className="h-8 flex-1 rounded-full text-[13px] font-medium transition-colors hover:bg-[#E8E8ED]"
          style={{ background: FILL, color: INK }}
        >
          Info
        </button>
        {contactState === "drafted" ? (
          <span className="inline-flex h-8 flex-1 items-center justify-center rounded-full text-[13px] font-medium" style={{ background: "#E9F7EE", color: "#1D7A3E" }}>
            Drafted
          </span>
        ) : contactState === "no_email" ? (
          <span className="inline-flex h-8 flex-1 items-center justify-center rounded-full text-[12px]" style={{ background: FILL, color: INK_3 }} title="No credits were charged">
            No email found
          </span>
        ) : (
          <button
            type="button"
            onClick={onContact}
            disabled={isSample || contactState === "working"}
            title={isSample ? "Example card" : person.source === "recommended" ? "Finds their email and drafts an outreach (8 credits, refunded if no email is found)" : "Drafts an outreach email"}
            className="inline-flex h-8 flex-1 items-center justify-center gap-1.5 rounded-full bg-[#0071E3] text-[13px] font-medium text-white transition-colors hover:bg-[#0077ED] disabled:cursor-not-allowed disabled:bg-[#F5F5F7] disabled:text-[#AEAEB2]"
          >
            {contactState === "working" && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            {contactState === "working" ? "Drafting" : "Contact"}
          </button>
        )}
      </div>
    </article>
  );
}
