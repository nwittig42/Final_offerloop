import type { OutboxThread } from "@/services/api";
import { statusLine, type BucketType } from "./shared/contactStatus";

export type { BucketType };

// --- helpers ---

function initialsFor(name: string): string {
  return name
    .split(/\s+/)
    .map((w) => w[0])
    .filter(Boolean)
    .slice(0, 2)
    .join("")
    .toUpperCase() || "?";
}

function avatarColor(name: string): string {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = name.charCodeAt(i) + ((h << 5) - h);
  const hue = ((h % 360) + 360) % 360;
  return `hsl(${hue}, 55%, 55%)`;
}

// --- stage-based border color ---

function stageBorderColor(c: OutboxThread, isSelected: boolean): string {
  if (isSelected) return "border-l-[#3B82F6] bg-[#FAFBFF]";
  if (c.hasUnreadReply || c.pipelineStage === "replied") return "border-l-red-400 hover:bg-red-50/40";
  if (c.pipelineStage === "new") return "border-l-gray-300 hover:bg-gray-50";
  if (c.pipelineStage === "draft_deleted") return "border-l-red-300 hover:bg-red-50/30";
  if (c.pipelineStage === "draft_created") return "border-l-amber-400 hover:bg-amber-50/30";
  if (c.pipelineStage === "email_sent" || c.pipelineStage === "waiting_on_reply") return "border-l-[#3B82F6]/50 hover:bg-[#FAFBFF]";
  // Opened: teal, between Contacted blue and Connected green.
  if (c.pipelineStage === "email_opened") return "border-l-teal-400 hover:bg-teal-50/30";
  if (c.pipelineStage === "meeting_scheduled" || c.pipelineStage === "connected") return "border-l-green-400 hover:bg-green-50/30";
  return "border-l-transparent hover:bg-gray-50";
}

// --- action chip ---

interface Chip {
  label: string;
  className: string;
}

function actionChip(c: OutboxThread, bucket: BucketType): Chip | null {
  if (bucket === "done") return null;
  // Replied
  if (c.hasUnreadReply || c.pipelineStage === "replied") {
    return { label: "View Reply", className: "bg-[rgba(59,130,246,0.10)] text-[#2563EB]" };
  }
  // Draft
  if (c.pipelineStage === "draft_deleted") {
    return { label: "Recreate Draft", className: "bg-red-100 text-red-700" };
  }
  if (c.pipelineStage === "draft_created") {
    return { label: "Send Draft", className: "bg-orange-100 text-orange-700" };
  }
  // Overdue follow-up
  if (c.nextFollowUpAt && new Date(c.nextFollowUpAt) <= new Date()) {
    return { label: "Follow Up", className: "bg-orange-100 text-orange-700" };
  }
  // Sent / waiting — open thread
  if (c.pipelineStage === "email_sent" || c.pipelineStage === "waiting_on_reply" || c.pipelineStage === "email_opened") {
    if (c.gmailThreadId) return { label: "Open Thread", className: "bg-gray-100 text-gray-600" };
  }
  // Fallback
  if (c.gmailDraftUrl || c.gmailThreadId) {
    return { label: "Open Gmail", className: "bg-gray-100 text-gray-600" };
  }
  return null;
}

// --- component ---

interface ContactCardProps {
  contact: OutboxThread;
  bucket: BucketType;
  isSelected: boolean;
  onClick: () => void;
}

export function ContactCard({ contact, bucket, isSelected, onClick }: ContactCardProps) {
  const name = contact.name || contact.email || "Unknown";
  const subtitle = [contact.title, contact.company].filter(Boolean).join(" at ");
  const chip = actionChip(contact, bucket);
  const isReplied = contact.hasUnreadReply || contact.pipelineStage === "replied";

  return (
    <button
      onClick={onClick}
      className={`w-full text-left flex items-center gap-3 px-3 py-2.5 rounded-[3px] transition-all border-l-[3px] ${stageBorderColor(contact, isSelected)}`}
    >
      {/* avatar */}
      <div
        className="flex-shrink-0 w-9 h-9 rounded-full flex items-center justify-center text-xs font-semibold text-white"
        style={{ backgroundColor: avatarColor(name) }}
      >
        {initialsFor(name)}
      </div>

      {/* text */}
      <div className="min-w-0 flex-1">
        <p className="text-sm font-semibold truncate text-gray-900">{name}</p>
        {subtitle && <p className="text-xs text-gray-500 truncate">{subtitle}</p>}
        <p className={`text-xs mt-0.5 truncate ${isReplied ? "text-red-500 font-medium" : "text-gray-400"}`}>
          {statusLine(contact)}
        </p>
        {(contact as any).personalizationLabel && (
          <span
            className="inline-block mt-0.5 text-[10px] font-medium px-1.5 py-px rounded"
            style={{
              background: (contact as any).personalizationType === 'university' ? 'rgba(59,130,246,0.08)' :
                         (contact as any).personalizationType === 'hometown' ? 'rgba(34,197,94,0.08)' :
                         (contact as any).personalizationType === 'company' ? 'rgba(124,58,237,0.08)' :
                         'rgba(107,114,128,0.08)',
              color: (contact as any).personalizationType === 'university' ? '#2563EB' :
                     (contact as any).personalizationType === 'hometown' ? '#16A34A' :
                     (contact as any).personalizationType === 'company' ? '#7C3AED' :
                     '#6B7280',
            }}
          >
            {(contact as any).personalizationLabel}
          </span>
        )}
      </div>

      {/* chip */}
      {chip && (
        <span className={`flex-shrink-0 text-[10px] font-semibold px-2 py-0.5 rounded-full whitespace-nowrap ${chip.className}`}>
          {chip.label}
        </span>
      )}
    </button>
  );
}
