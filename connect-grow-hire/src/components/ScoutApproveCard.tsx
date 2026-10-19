/**
 * ScoutApproveCard - the inline approve UI for a Scout navigate proposal.
 *
 * Rendered in the chat only when Scout proposes a navigation it is NOT sure
 * the user directed (DO-mode navigations execute immediately). Shows a
 * friendly destination name and each prefill field as an editable chip.
 * Approving calls onApprove with the (possibly edited) prefill; the panel
 * then writes the bridge and navigates.
 *
 * Styling uses the app's real tokens (--brand-blue, --line, --ink) with hex
 * fallbacks. An earlier version used a --c-* token set that exists nowhere
 * in this codebase, which rendered the Approve button invisible.
 */
import { useState } from 'react';
import { Check, ArrowRight } from 'lucide-react';
import type { ScoutNavigate } from '@/hooks/useScoutChat';

interface ScoutApproveCardProps {
  navigate: ScoutNavigate;
  /** True once the user has approved; renders a collapsed confirmation. */
  resolved?: boolean;
  onApprove: (prefill: Record<string, string>) => void;
}

// Human-readable labels for prefill field keys.
const FIELD_LABELS: Record<string, string> = {
  job_title: 'Role',
  company: 'Company',
  location: 'Location',
  industry: 'Industry',
  size: 'Size',
  linkedin_url: 'LinkedIn',
  job_url: 'Job link',
  query: 'Search',
  prompt: 'Search',
};

// Friendly destination names; falls back to a prettified path.
const PAGE_LABELS: Record<string, string> = {
  '/find': 'Find People',
  '/contacts': 'Contacts',
  '/job-board': 'Job Board',
  '/applications': 'Applications',
  '/agent': 'Loops',
  '/agent/setup': 'a new Loop',
  '/upload-list': 'Upload List',
  '/integrations': 'Integrations',
};

function pageLabel(route: string): string {
  if (PAGE_LABELS[route]) return PAGE_LABELS[route];
  const base = (route || '').split('?')[0];
  if (PAGE_LABELS[base]) return PAGE_LABELS[base];
  return base.replace(/^\//, '').replace(/[-/]/g, ' ') || 'that page';
}

export function ScoutApproveCard({ navigate, resolved, onApprove }: ScoutApproveCardProps) {
  const [prefill, setPrefill] = useState<Record<string, string>>(
    () => ({ ...(navigate.prefill || {}) }),
  );
  const [editingKey, setEditingKey] = useState<string | null>(null);

  const keys = Object.keys(prefill);
  const destination = pageLabel(navigate.route);

  if (resolved) {
    return (
      <div className="mt-2 flex items-center gap-1.5 rounded-lg border border-[var(--line,#E5E5E0)] bg-[var(--paper,#fff)] px-3 py-2 text-xs text-[var(--ink-3,#8A8F9A)]">
        <Check className="h-3.5 w-3.5 text-[var(--brand-blue,#3B82F6)]" />
        <span>Done, opened {destination}</span>
      </div>
    );
  }

  return (
    <div className="mt-2 rounded-xl border border-[var(--line,#E5E5E0)] bg-[var(--paper,#fff)] p-3 shadow-sm">
      {/* The reasoning already appears in the message bubble above; the card
          only carries the destination, the editable inputs, and the button. */}
      <p className="text-xs text-[var(--ink-3,#8A8F9A)]">
        Run this in <span className="font-semibold text-[var(--ink,#111318)]">{destination}</span>?
      </p>

      {keys.length > 0 && (
        <div className="mt-2.5 flex flex-wrap gap-1.5">
          {keys.map((k) => (
            <div
              key={k}
              className="inline-flex items-center gap-1 rounded-full border border-[var(--line,#E5E5E0)] bg-[var(--paper-2,#FAFBFF)] px-2 py-1 text-xs"
            >
              <span className="text-[var(--ink-3,#8A8F9A)]">{FIELD_LABELS[k] || k}</span>
              {editingKey === k ? (
                <input
                  autoFocus
                  value={prefill[k]}
                  onChange={(e) => setPrefill((p) => ({ ...p, [k]: e.target.value }))}
                  onBlur={() => setEditingKey(null)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === 'Escape') setEditingKey(null);
                  }}
                  className="w-40 rounded bg-white px-1 text-[var(--ink,#111318)] outline-none ring-1 ring-[var(--brand-blue,#3B82F6)]"
                />
              ) : (
                <button
                  type="button"
                  onClick={() => setEditingKey(k)}
                  title="Click to edit"
                  className="font-medium text-[var(--ink,#111318)] underline decoration-dotted decoration-[var(--ink-3,#8A8F9A)] underline-offset-2"
                >
                  {prefill[k] || 'add'}
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      <button
        type="button"
        onClick={() => onApprove(prefill)}
        className="mt-3 inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-semibold text-white transition active:scale-[0.98]"
        style={{ background: 'var(--brand-blue, #3B82F6)' }}
      >
        <Check className="h-4 w-4" />
        Run it
        <ArrowRight className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

export default ScoutApproveCard;
