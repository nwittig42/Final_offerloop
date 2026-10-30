// connect-grow-hire/src/types/findFilters.ts
// Shared shapes for the Find page filter rail. The dimensions deliberately
// mirror the backend parsers exactly (people: prompt_parser.py, companies:
// company_search.parse_firm_search_prompt) — nothing invented client-side.

export interface PeopleFilters {
  titles: string[];
  companies: string[];
  locations: string[];
  schools: string[];
  industries: string[];
  // Org alumni directory ids (OFF-42), e.g. "usc_tke". Server-checked against
  // the user's memberships; not part of the prompt parser.
  organizations: string[];
}

export interface CompanyFilters {
  industry: string | null;
  location: string | null;
  size: "small" | "mid" | "large" | "none";
  keywords: string[];
}

export const EMPTY_PEOPLE_FILTERS: PeopleFilters = {
  titles: [], companies: [], locations: [], schools: [], industries: [], organizations: [],
};

export const EMPTY_COMPANY_FILTERS: CompanyFilters = {
  industry: null, location: null, size: "none", keywords: [],
};

export function peopleFiltersActive(f: PeopleFilters): boolean {
  return f.titles.length > 0 || f.companies.length > 0 || f.locations.length > 0
    || f.schools.length > 0 || f.industries.length > 0 || f.organizations.length > 0;
}

export function companyFiltersActive(f: CompanyFilters): boolean {
  return !!f.industry || !!f.location || f.size !== "none" || f.keywords.length > 0;
}
