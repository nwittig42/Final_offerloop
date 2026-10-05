import { type ProtoContact, openedSummary } from "@/pages/trackerAdapter";
import { CompanyLogo } from "@/components/CompanyLogo";

// Helpers ported from network-tracker.html.

function initials(name: string): string {
  const parts = name.trim().split(/\s+/);
  const first = parts[0]?.[0] ?? "";
  const second = parts[1]?.[0] ?? "";
  return (first + second).toUpperCase() || "?";
}

// Prototype line 2265-2271: collapses week-scale ages to weeks.
function timeAgo(days: number): string {
  if (days >= 7) {
    const w = Math.round(days / 7);
    return `${w} week${w === 1 ? "" : "s"} ago`;
  }
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

// Split the proto role string into title and company parts so we can drop
// an inline company logo between them. The adapter's deriveRole formats as
// "Title @Company" when both are present, "Title" or "Company" alone when
// only one is, and "" when neither. We use contact.company to disambiguate
// the no-@ case (title-only vs company-only).
function splitRole(role: string, company: string): { title: string; company: string } {
  if (!role) return { title: "", company: "" };
  const at = role.indexOf("@");
  if (at >= 0) {
    return { title: role.slice(0, at).trim(), company: role.slice(at + 1).trim() };
  }
  // No "@": role is either title-only or company-only.
  if (role === company) return { title: "", company };
  return { title: role, company: "" };
}

interface ProtoContactCardProps {
  contact: ProtoContact;
  isSelected: boolean;
  onSelect: () => void;
}

export function ProtoContactCard({ contact, isSelected, onSelect }: ProtoContactCardProps) {
  const { title, company } = splitRole(contact.role, contact.company);
  const opened = openedSummary(contact);

  return (
    <button
      type="button"
      onClick={onSelect}
      data-contact-id={contact.id}
      className={`contact-card${isSelected ? " selected" : ""}`}
    >
      <div className="contact-card-info">
        <div className="contact-initials">{initials(contact.name)}</div>
        <div className="contact-text">
          <div className="contact-name">{contact.name}</div>
          <div className="contact-role">
            {title}
            {title && company && <span className="contact-role-sep"> · </span>}
            {company && (
              <>
                <CompanyLogo
                  company={company}
                  size={16}
                  rounded={3}
                  bordered={false}
                  hideWhenMonogram
                  className="contact-role-logo"
                />
                <span className="contact-role-company">{company}</span>
              </>
            )}
          </div>
          <div className="contact-time">
            {timeAgo(contact.daysAgo)}
            {contact.source === "agent" && (
              <span className="contact-source-badge" title="Discovered by a Loop">
                Loop
              </span>
            )}
          </div>
        </div>
      </div>
      <div className="status-pill" title={opened || undefined}>
        <div className="status-dot" />
        <span className="status-text">{contact.status}</span>
      </div>
    </button>
  );
}
