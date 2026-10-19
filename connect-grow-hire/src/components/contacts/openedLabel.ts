// Wording for email-open tracking on the Contacts page. Counts come only from
// emailOpenCount; nothing is invented when the backend sent no data.
import type { OutboxThread } from "@/services/api";

export function timeAgo(iso: string | null | undefined, now: number = Date.now()): string {
  const t = iso ? Date.parse(iso) : NaN;
  if (Number.isNaN(t)) return "";
  const mins = Math.max(0, Math.floor((now - t) / 60_000));
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

// "Opened 2h ago" / "Opened 3 times, last 2h ago"; "" when no open data.
export function openedSummary(t: OutboxThread | undefined, now: number = Date.now()): string {
  if (!t) return "";
  const last = timeAgo(t.emailLastOpenedAt || t.emailOpenedAt, now);
  if (!last) return "";
  const count = t.emailOpenCount ?? 0;
  return count > 1 ? `Opened ${count} times, last ${last}` : `Opened ${last}`;
}
