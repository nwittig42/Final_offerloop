import { type ProtoContact, openedSummary } from "@/pages/trackerAdapter";

// Detail panel header: avatar + name + role + LinkedIn / email pills, plus
// the bookmark icon button. The detail panel renders the raw role
// (with @-separator) unlike the card, which splits on @ (matches proto behavior).
//
// Bookmark state lives on the page as an in-memory Set<string> in PR1.

function initials(name: string): string {
  const parts = name.trim().split(/\s+/);
  return ((parts[0]?.[0] ?? "") + (parts[1]?.[0] ?? "")).toUpperCase() || "?";
}

interface ProtoDetailHeaderProps {
  contact: ProtoContact;
  isBookmarked: boolean;
  onToggleBookmark: () => void;
}

export function ProtoDetailHeader({
  contact,
  isBookmarked,
  onToggleBookmark,
}: ProtoDetailHeaderProps) {
  const linkedinHref = contact.linkedinUrl
    ? (contact.linkedinUrl.startsWith("http") ? contact.linkedinUrl : `https://${contact.linkedinUrl}`)
    : null;
  const opened = openedSummary(contact);

  return (
    <div className="detail-info-row">
      <div className="detail-avatar-info">
        <div className="detail-avatar">{initials(contact.name)}</div>
        <div className="detail-identity">
          <div className="detail-name-block">
            <div className="detail-name">{contact.name}</div>
            <div className="detail-role">{contact.role}</div>
            {opened && (
              <div
                className="detail-opened"
                title="Based on the email's images loading. Some mail apps load them automatically, so treat this as likely, not certain."
              >
                {opened}
              </div>
            )}
          </div>
          <div className="detail-links">
            {linkedinHref && (
              <a className="detail-link-pill" href={linkedinHref} target="_blank" rel="noopener noreferrer">
                <svg viewBox="0 0 12 12" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <path d="M9.5 0H2.5C1.1195 0 0 1.1195 0 2.5V9.5C0 10.8805 1.1195 12 2.5 12H9.5C10.881 12 12 10.8805 12 9.5V2.5C12 1.1195 10.881 0 9.5 0ZM4 9.5H2.5V4H4V9.5ZM3.25 3.366C2.767 3.366 2.375 2.971 2.375 2.484C2.375 1.997 2.767 1.602 3.25 1.602C3.733 1.602 4.125 1.997 4.125 2.484C4.125 2.971 3.7335 3.366 3.25 3.366ZM10 9.5H8.5V6.698C8.5 5.014 6.5 5.1415 6.5 6.698V9.5H5V4H6.5V4.8825C7.198 3.5895 10 3.494 10 6.1205V9.5Z" fill="#1E2D4D" />
                </svg>
                LinkedIn
              </a>
            )}
            {contact.email && (
              <span className="detail-link-pill">
                <svg viewBox="0 0 12 12" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <path d="M2 2H10C10.55 2 11 2.45 11 3V9C11 9.55 10.55 10 10 10H2C1.45 10 1 9.55 1 9V3C1 2.45 1.45 2 2 2Z" stroke="#1E2D4D" />
                  <path d="M11 3L6 6.5L1 3" stroke="#1E2D4D" />
                </svg>
                {contact.email}
              </span>
            )}
          </div>
        </div>
      </div>
      <div className="detail-action-btns">
        <button
          type="button"
          className={`icon-btn icon-btn-bookmark${isBookmarked ? " active" : ""}`}
          aria-label={isBookmarked ? "Remove bookmark" : "Add bookmark"}
          aria-pressed={isBookmarked}
          onClick={onToggleBookmark}
        >
          <svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M12.8333 14.5L8.16667 11.8333L3.5 14.5V3.83333C3.5 3.47971 3.64048 3.14057 3.89052 2.89052C4.14057 2.64048 4.47971 2.5 4.83333 2.5H11.5C11.8536 2.5 12.1928 2.64048 12.4428 2.89052C12.6929 3.14057 12.8333 3.47971 12.8333 3.83333V14.5Z" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>
    </div>
  );
}
