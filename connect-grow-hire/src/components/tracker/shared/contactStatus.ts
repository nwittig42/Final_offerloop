/**
 * Pure functions that derive UI strings from an OutboxThread.
 * Extracted so the status-line logic is importable + unit-testable
 * independent of the ContactCard React component.
 */
import type { OutboxThread } from "@/services/api";
import { formatTimeAgo, daysBetween } from "@/lib/formatters";

export type BucketType = "needsAttention" | "waiting" | "done";

/**
 * Human-readable status line shown under the contact name in the card.
 * Ordering matters — earlier branches take precedence. The ordering is:
 *   1. Unread reply / replied (user action required)
 *   2. Draft state (deleted, stuck, unsent, ready)
 *   3. Overdue follow-up
 *   4. Sent / waiting
 *   5. Terminal stages
 *   6. `new` / archived
 *   7. Fallback: stage name with underscores stripped
 */
export function statusLine(c: OutboxThread): string {
  // 1. Replied / unread reply — highest priority
  if (c.hasUnreadReply || c.pipelineStage === "replied") {
    const ago = formatTimeAgo(c.replyReceivedAt || c.lastActivityAt);
    return ago ? `Replied ${ago} — action needed` : "Replied — action needed";
  }

  // 2. Draft states
  if (c.pipelineStage === "draft_deleted") {
    return "Draft deleted — recreate it";
  }
  if (c.pipelineStage === "draft_created") {
    if (c.needsManualSync) {
      return "Draft may have been sent — tap Refresh to check";
    }
    const d = daysBetween(c.draftCreatedAt);
    if (d >= 1) return `Draft unsent for ${d} day${d !== 1 ? "s" : ""} — send it!`;
    return "Draft ready to send";
  }

  // 3. Overdue follow-up (applies across stages, so checked before sent/waiting)
  if (c.nextFollowUpAt && new Date(c.nextFollowUpAt) <= new Date()) {
    return "Follow-up overdue";
  }

  // 4. Sent / waiting
  if (c.pipelineStage === "waiting_on_reply" || c.pipelineStage === "email_sent") {
    if (c.emailOpenedAt) {
      return `Opened ${formatTimeAgo(c.emailLastOpenedAt || c.emailOpenedAt)} — waiting for reply`;
    }
    if (c.emailSentAt) return `Sent ${formatTimeAgo(c.emailSentAt)} — waiting for reply`;
    return "Sent — waiting for reply";
  }

  // 5. Terminal stages
  if (c.pipelineStage === "meeting_scheduled") return "Meeting scheduled";
  if (c.pipelineStage === "connected") return "Connected";
  if (c.pipelineStage === "no_response" || c.resolution === "ghosted") {
    return "No response after follow-ups";
  }
  if (c.pipelineStage === "bounced") return "Email bounced";
  if (c.pipelineStage === "closed") return "Closed";

  // 6. new / archived
  if (c.pipelineStage === "new") return "Ready to draft an email";
  if (c.archivedAt) return "Archived";

  // 7. Fallback for any future stages
  const stage: string = c.pipelineStage || "";
  return stage.replace(/_/g, " ");
}
