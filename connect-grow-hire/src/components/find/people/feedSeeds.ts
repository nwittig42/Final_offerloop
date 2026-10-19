// Which companies to fill the Find feed from, using whatever we know about
// the user. The recommender (/api/ranker/candidates) only finds people when
// it knows where to look; given `company`, it ranks people there (alumni of
// the user's school first) and goes and finds some if none are on file. So
// the feed is never empty: named targets first, then firms inferred from
// their industries / track / roles / goals text, then a broad default mix.

export interface FeedProfile {
  dreamCompanies: string[];
  targetFirms: string[];
  targetIndustries: string[];
  careerTrack: string;
  preferredJobRole: string;
  extractedRoles: string[];
  directionNarrative: string;
  personalContext: string;
}

// Keyword → the firms students in that lane most often network into.
const LANES: { match: RegExp; firms: string[] }[] = [
  { match: /invest(ment)? bank|\bib\b|banking|private equity|\bpe\b|hedge fund|asset management|sales (and|&) trading|capital markets|finance/i,
    firms: ["Goldman Sachs", "JPMorgan Chase", "Morgan Stanley", "Evercore", "Blackstone", "Bank of America"] },
  { match: /consult|strategy|management consulting/i,
    firms: ["McKinsey & Company", "Boston Consulting Group", "Bain & Company", "Deloitte", "Accenture"] },
  { match: /account|audit|\btax\b|\bcpa\b|assurance/i,
    firms: ["Deloitte", "PwC", "EY", "KPMG"] },
  { match: /software|engineer|computer science|\bcs\b|tech|product manag|data scien|machine learning|\bai\b/i,
    firms: ["Google", "Meta", "Apple", "Amazon", "Microsoft"] },
  { match: /market|brand|advertis|media|entertainment|consumer/i,
    firms: ["Procter & Gamble", "Nike", "The Walt Disney Company", "Netflix", "L'Oréal"] },
  { match: /real estate/i,
    firms: ["CBRE", "JLL", "Blackstone"] },
];

// Nothing to go on at all: a broad mix across the lanes students recruit for.
const DEFAULT_MIX = ["Goldman Sachs", "McKinsey & Company", "Google", "Deloitte", "JPMorgan Chase", "Amazon", "Morgan Stanley", "Boston Consulting Group"];

export function seedCompanies(p: FeedProfile, max = 12): string[] {
  const out: string[] = [];
  const add = (name: string) => {
    const n = name?.trim();
    if (n && !out.some((x) => x.toLowerCase() === n.toLowerCase())) out.push(n);
  };

  // 1. Companies they named.
  [...p.dreamCompanies, ...p.targetFirms].forEach(add);

  // 2. Firms inferred from what they told us about direction.
  const text = [
    ...p.targetIndustries, p.careerTrack, p.preferredJobRole, ...p.extractedRoles,
    p.directionNarrative, p.personalContext,
  ].filter(Boolean).join(" | ");
  if (text) for (const lane of LANES) if (lane.match.test(text)) lane.firms.forEach(add);

  // 3. Always end with the broad mix so there's more to scroll into.
  DEFAULT_MIX.forEach(add);

  return out.slice(0, max);
}
