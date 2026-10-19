import type { OutboxThread } from '../services/api';

export type SuggestionType = 'dream_company' | 'pipeline_gap' | 'follow_up';

export interface Suggestion {
  id: string;
  type: SuggestionType;
  priority: number;
  title: string;
  subtitle: string;
  company?: string;
  createdAt: number;
}

interface GoalsData {
  dreamCompanies: string[];
}

const SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000;

const SUCCESS_STAGES = new Set(['replied', 'meeting_scheduled', 'connected']);

function groupByCompany(threads: OutboxThread[]): Map<string, OutboxThread[]> {
  const map = new Map<string, OutboxThread[]>();
  for (const t of threads) {
    if (!t.company) continue;
    const key = t.company.toLowerCase();
    const arr = map.get(key);
    if (arr) arr.push(t);
    else map.set(key, [t]);
  }
  return map;
}

function makeDreamCompanySuggestions(
  goals: GoalsData,
  byCompany: Map<string, OutboxThread[]>,
): Suggestion[] {
  const now = Date.now();
  const results: Suggestion[] = [];

  for (const company of goals.dreamCompanies) {
    const key = company.toLowerCase();
    const threads = byCompany.get(key) || [];

    // Skip if 3+ threads at this company
    if (threads.length >= 3) continue;

    // Skip if any thread has reached a success stage
    if (threads.some(t => t.pipelineStage && SUCCESS_STAGES.has(t.pipelineStage))) continue;

    results.push({
      id: `dream_company_${key}`,
      type: 'dream_company',
      priority: 9,
      title: `No outreach to ${company} yet`,
      subtitle: 'Start a search to find contacts',
      company,
      createdAt: now,
    });
  }

  return results;
}

function makePipelineGapSuggestions(
  goals: GoalsData,
  byCompany: Map<string, OutboxThread[]>,
): Suggestion[] {
  if (goals.dreamCompanies.length < 3) return [];

  const now = Date.now();
  const hasThreads = goals.dreamCompanies.some(c => {
    const threads = byCompany.get(c.toLowerCase());
    return threads && threads.length > 0;
  });
  if (!hasThreads) return [];

  const results: Suggestion[] = [];
  for (const company of goals.dreamCompanies) {
    const key = company.toLowerCase();
    const threads = byCompany.get(key);
    if (threads && threads.length > 0) continue;

    results.push({
      id: `pipeline_gap_${key}`,
      type: 'pipeline_gap',
      priority: 7,
      title: `No outreach to ${company}, one of your dream companies`,
      subtitle: `Search for ${company} contacts to close the gap`,
      company,
      createdAt: now,
    });
    // Single-company framing: pick ONE missing company only
    break;
  }

  return results;
}

function makeFollowUpSuggestions(threads: OutboxThread[]): Suggestion[] {
  const now = Date.now();
  const stale = threads.filter(t => {
    if (t.pipelineStage !== 'waiting_on_reply' && t.pipelineStage !== 'email_opened') return false;
    if (!t.emailSentAt) return false;
    if (t.followUpCount !== 0) return false;
    const sentAt = new Date(t.emailSentAt).getTime();
    return now - sentAt > SEVEN_DAYS_MS;
  });

  if (stale.length === 0) return [];

  return [{
    id: 'follow_up_stale',
    type: 'follow_up',
    priority: 8,
    title: `${stale.length} contact${stale.length === 1 ? '' : 's'} haven't replied in 7+ days`,
    subtitle: 'Follow up to keep the momentum',
    createdAt: now,
  }];
}

export function generateSuggestions(
  _context: 'find' | 'email',
  goals: GoalsData,
  threads: OutboxThread[],
  dismissed: Set<string>,
): Suggestion[] {
  const byCompany = groupByCompany(threads);

  const all = [
    ...makeDreamCompanySuggestions(goals, byCompany),
    ...makePipelineGapSuggestions(goals, byCompany),
    ...makeFollowUpSuggestions(threads),
  ];

  // Sort: priority desc, then recency desc
  all.sort((a, b) => b.priority - a.priority || b.createdAt - a.createdAt);

  // Filter dismissed, cap at 3
  return all.filter(s => !dismissed.has(s.id)).slice(0, 3);
}
