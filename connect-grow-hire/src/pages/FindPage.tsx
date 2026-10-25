// Search. Opens on a gallery of recommended people (the same ranked deck the
// mobile People feed uses), so there is always someone to reach out to.
// The search bar and LinkedIn-style filter pills on top swap the gallery for
// search results in the same card layout.
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, ArrowLeft, Users, ChevronRight } from "lucide-react";
import { AppSidebar } from "@/components/AppSidebar";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppHeader } from "@/components/AppHeader";
import { MainContentWrapper } from "@/components/MainContentWrapper";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { apiService, isErrorResponse, type EmailTemplate } from "@/services/api";
import { firebaseApi } from "@/services/firebaseApi";
import { toast } from "@/hooks/use-toast";
import { NoSchoolEmptyState } from "@/components/NoSchoolEmptyState";
import { GoalsPromptBanner } from "@/components/find/GoalsPromptBanner";
import { IS_DEV_PREVIEW, DEV_MOCK_USER } from "@/lib/devPreview";
import { EMPTY_PEOPLE_FILTERS, peopleFiltersActive, type PeopleFilters } from "@/types/findFilters";
import { CONTACTS_KEY, THREADS_KEY, normalizeLinkedIn } from "@/components/contacts/contactsModel";
import { TIER_CONFIGS } from "@/lib/constants";
import { PeopleSearchBar } from "@/components/find/people/PeopleSearchBar";
import { useOrgs } from "@/hooks/useOrgs";
import { orgLogo } from "@/lib/orgLogos";
import type { Org } from "@/services/api";
import { APPLE_FONT, PersonCard, type ContactState } from "@/components/find/people/PersonCard";
import { ContactProgressTray, type TrayItem } from "@/components/find/people/ContactProgressTray";
import { PersonInfoDialog } from "@/components/find/people/PersonInfoDialog";
import { SearchLoader } from "@/components/find/people/SearchLoader";
import { GOLD_RING } from "@/components/find/people/PersonAvatar";
import { seedCompanies, type FeedProfile } from "@/components/find/people/feedSeeds";
import { neverEmailed, useBookmarks } from "@/components/find/people/useBookmarks";
import {
  personFromRanker, personFromSavedSearch, personFromSearch, type Person,
} from "@/components/find/people/peopleModel";
import { FirstEmailReminder } from "@/components/walkthrough/FirstEmailReminder";

// First screen loads 40 people; scrolling near the bottom pulls 20 more.
const FIRST_PAGE = 40;
const NEXT_PAGE = 20;
// Narrow cards: as many ~236px columns as fit.
const GRID = "grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(236px,1fr))]";
// Default for the "how many people" slider; the plan's cap is the max.
const DEFAULT_COUNT = 5;
const COUNT_KEY = "offerloop_find_count";
// "Recently found" (OFF-56): how many lead the feed, and how often the rest
// are mixed in among recommended cards.
const FOUND_LEAD = 8;
const FOUND_EVERY = 6;
// How many people lead the feed as "Top connections".
const TOP_COUNT = 4;
// Written by the landing page hero search (HeroSearchCTA.PENDING_QUERY_KEY).
const PENDING_QUERY_KEY = "offerloop_pending_query";

// Puts one `extra` after every `every` items of `list`; leftovers go last.
function disperse<T>(list: T[], extras: T[], every: number): T[] {
  const out: T[] = [];
  let e = 0;
  list.forEach((item, i) => {
    out.push(item);
    if ((i + 1) % every === 0 && e < extras.length) out.push(extras[e++]);
  });
  return out.concat(extras.slice(e));
}

// When only filters are set, build the sentence the prompt parser expects.
// Org pills read as "who are USC TKE alumni"; the backend strips that back
// out and searches the directory (the ids travel in `filters` too).
function promptFromFilters(f: PeopleFilters, orgs: Org[]): string {
  const parts = [f.titles.length ? f.titles.join(" or ") : "People"];
  if (f.companies.length) parts.push(`at ${f.companies.join(" or ")}`);
  if (f.industries.length) parts.push(`in ${f.industries.join(" or ")}`);
  if (f.locations.length) parts.push(`in ${f.locations.join(" or ")}`);
  if (f.schools.length) parts.push(`who went to ${f.schools.join(" or ")}`);
  const orgNames = orgs.filter((o) => f.organizations.includes(o.id)).map((o) => o.shortName);
  if (orgNames.length) parts.push(`who are ${orgNames.join(" or ")} alumni`);
  return parts.join(" ");
}

function CardSkeleton() {
  return (
    <div className="h-[244px] animate-pulse rounded-[22px] bg-white p-[18px]" style={{ boxShadow: "0 1px 2px rgba(0,0,0,0.04), 0 4px 14px rgba(0,0,0,0.05)" }}>
      <div className="flex gap-3">
        <div className="h-[52px] w-[52px] rounded-full bg-[#F5F5F7]" />
        <div className="flex-1 space-y-2 pt-1.5">
          <div className="h-3.5 w-2/3 rounded-full bg-[#F5F5F7]" />
          <div className="h-3 w-1/2 rounded-full bg-[#F5F5F7]" />
        </div>
      </div>
      <div className="mt-5 h-8 w-3/4 rounded-lg bg-[#F5F5F7]" />
      <div className="mt-3 h-6 w-1/2 rounded-full bg-[#F5F5F7]" />
    </div>
  );
}

const FindPage: React.FC = () => {
  const routerLocation = useLocation();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const { user: authUser } = useFirebaseAuth();
  const user = IS_DEV_PREVIEW ? DEV_MOCK_USER : authUser;

  // ── School gate (unchanged from the old Find) ───────────────────────────
  const [userUniversity, setUserUniversity] = useState<string | null>(null);
  const [universityOptOut, setUniversityOptOut] = useState(false);
  const [schoolLoaded, setSchoolLoaded] = useState(false);
  const [forceRefresh, setForceRefresh] = useState(0);
  // What the feed is built from; null until the profile loads.
  const [feedProfile, setFeedProfile] = useState<FeedProfile | null>(null);
  useEffect(() => {
    if (!user?.uid) return;
    if (IS_DEV_PREVIEW) {
      setUserUniversity((user as any)?.university || "");
      setFeedProfile({ dreamCompanies: [], targetFirms: [], targetIndustries: [], careerTrack: "", preferredJobRole: "", extractedRoles: [], directionNarrative: "", personalContext: "" });
      setSchoolLoaded(true);
      return;
    }
    firebaseApi.getUserOnboardingData(user.uid).then((data) => {
      setUserUniversity(data.university || null);
      setUniversityOptOut(data.universityOptOut === true);
      setFeedProfile({
        dreamCompanies: data.dreamCompanies || [], targetFirms: data.targetFirms || [],
        targetIndustries: data.targetIndustries || [], careerTrack: data.careerTrack || "",
        preferredJobRole: data.preferredJobRole || "", extractedRoles: data.extractedRoles || [],
        directionNarrative: data.directionNarrative || "", personalContext: data.personalContext || "",
      });
      setSchoolLoaded(true);
    }).catch(() => {
      setFeedProfile({ dreamCompanies: [], targetFirms: [], targetIndustries: [], careerTrack: "", preferredJobRole: "", extractedRoles: [], directionNarrative: "", personalContext: "" });
      setSchoolLoaded(true);
    });
  }, [user?.uid, forceRefresh]);

  // Saved email template (or one applied from /find/templates) rides along
  // on every draft so Contact writes in the user's voice.
  const [emailTemplate, setEmailTemplate] = useState<EmailTemplate | null>(null);
  useEffect(() => {
    if (!user?.uid || IS_DEV_PREVIEW) return;
    apiService.getEmailTemplate().then((t) => setEmailTemplate({
      purpose: t.purpose, stylePreset: t.stylePreset, customInstructions: t.customInstructions,
      signoffPhrase: t.signoffPhrase, signatureBlock: t.signatureBlock, name: t.name,
      subject: t.subject, savedTemplateId: t.savedTemplateId,
    })).catch(() => {});
  }, [user?.uid]);
  useEffect(() => {
    const applied = (routerLocation.state as { appliedEmailTemplate?: EmailTemplate } | null)?.appliedEmailTemplate;
    if (applied) setEmailTemplate(applied);
  }, [routerLocation.state]);

  // ── Recommended gallery (ranker deck) ───────────────────────────────────
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  // Companies to pull people from: named targets, then firms inferred from
  // the profile, then a broad mix. Never empty, so the feed never is.
  const seeds = useMemo(() => (feedProfile ? seedCompanies(feedProfile) : []), [feedProfile]);

  // Each page = the general recommender (first page only) plus a few seed
  // companies in parallel, mixed round-robin so one firm doesn't take over.
  // pageParam carries who's been shown and which seed comes next.
  type FeedCursor = { seen: string[]; next: number };
  const recommended = useInfiniteQuery({
    queryKey: ["findRecommended-v2", user?.uid, seeds],
    enabled: !!user?.uid && !IS_DEV_PREVIEW && feedProfile !== null,
    initialPageParam: { seen: [], next: 0 } as FeedCursor,
    staleTime: 10 * 60 * 1000,
    queryFn: async ({ pageParam: rawParam }) => {
      // Defensive: a cursor from anywhere unexpected starts the feed over.
      const pageParam: FeedCursor = rawParam && Array.isArray(rawParam.seen) && typeof rawParam.next === "number"
        ? rawParam : { seen: [], next: 0 };
      const first = pageParam.next === 0;
      const want = first ? FIRST_PAGE : NEXT_PAGE;
      const batch = seeds.slice(pageParam.next, pageParam.next + (first ? 4 : 2));
      const perCompany = Math.ceil(want / Math.max(1, batch.length)) + 4;
      const seen = new Set(pageParam.seen);

      const requests = [
        ...(first ? [apiService.getRankerCandidates({ limit: want, exclude: pageParam.seen })] : []),
        ...batch.map((company) => apiService.getRankerCandidates({ limit: perCompany, company, exclude: pageParam.seen })),
      ];
      const settled = await Promise.allSettled(requests);
      if (settled.every((r) => r.status === "rejected")) throw new Error("Couldn't load recommendations");

      const lists = settled.map((r) => {
        if (r.status !== "fulfilled") return [] as Person[];
        const res = r.value;
        return (res.candidates || [])
          .map((c, i) => personFromRanker(c, i, res.deck_id || ""))
          .filter((p): p is Person => p !== null);
      });
      // Round-robin across the lists, skipping anyone already shown.
      const people: Person[] = [];
      for (let i = 0; people.length < want && lists.some((l) => i < l.length); i++) {
        for (const l of lists) {
          const p = l[i];
          if (p && !seen.has(p.id) && people.length < want) { seen.add(p.id); people.push({ ...p, rank: pageParam.seen.length + people.length }); }
        }
      }
      return { people, cursor: { seen: [...seen], next: pageParam.next + batch.length } as FeedCursor };
    },
    // Keep going while there are seed companies left, or the last page was full.
    getNextPageParam: (last) => {
      if (!last?.cursor) return undefined;
      return last.cursor.next < seeds.length || (last.people?.length ?? 0) >= NEXT_PAGE ? last.cursor : undefined;
    },
  });
  const recommendedPeople = useMemo(
    () => (recommended.data?.pages ?? []).flatMap((p) => p?.people ?? []).filter((p) => !dismissed.has(p.id)),
    [recommended.data, dismissed],
  );
  const recommendedLoading = recommended.isLoading && !IS_DEV_PREVIEW;
  // Nothing to show after loading: ask for dream companies right in the feed.
  const feedEmpty = !recommendedLoading && feedProfile !== null && recommendedPeople.length === 0 && !recommended.hasNextPage;

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = recommended;
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el || !hasNextPage) return;
    const io = new IntersectionObserver(
      (entries) => { if (entries[0]?.isIntersecting && !isFetchingNextPage) void fetchNextPage(); },
      { root: scrollRef.current, rootMargin: "800px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage, recommendedPeople.length]);

  // ── Search ──────────────────────────────────────────────────────────────
  // Pre-fill from ?q= or from the landing page's hero search, which stashes
  // the visitor's query before sending them through sign-up.
  const [query, setQuery] = useState(() => {
    const q = searchParams.get("q");
    if (q) return q;
    try {
      const pending = localStorage.getItem(PENDING_QUERY_KEY);
      if (pending) localStorage.removeItem(PENDING_QUERY_KEY);
      return pending ?? "";
    } catch { return ""; }
  });
  const [filters, setFilters] = useState<PeopleFilters>(EMPTY_PEOPLE_FILTERS);
  // Orgs are added in Account Settings (OFF-42); Find just reflects them.
  const { joined: joinedOrgs, orgs: allOrgs } = useOrgs(!!user?.uid && !IS_DEV_PREVIEW);
  // A search that named an org the student hasn't joined ("TKE alumni").
  const [orgHint, setOrgHint] = useState<{ id: string; name: string; shortName: string } | null>(null);
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<Person[] | null>(null);
  const [resultsLabel, setResultsLabel] = useState("");
  const [resultsNote, setResultsNote] = useState<string | null>(null);
  // How many people a search finds: 1..the plan's cap, remembered per browser.
  const maxCount = TIER_CONFIGS[(authUser?.tier as keyof typeof TIER_CONFIGS) || "free"]?.maxContacts ?? 3;
  const [count, setCountState] = useState(() => {
    try { return Number(localStorage.getItem(COUNT_KEY)) || DEFAULT_COUNT; } catch { return DEFAULT_COUNT; }
  });
  const searchCount = Math.min(Math.max(1, count), maxCount);
  const setCount = (n: number) => {
    setCountState(n);
    try { localStorage.setItem(COUNT_KEY, String(n)); } catch { /* private mode */ }
  };

  // The search in flight, so Cancel (button, Esc or Ctrl+C) can stop it.
  const activeSearch = useRef<{ id: string; abort: AbortController } | null>(null);

  const runSearch = useCallback(async (q: string, rawFilters: PeopleFilters) => {
    if (activeSearch.current) return;
    // Drop orgs the student has since left, so a stale pill can't linger.
    const f = { ...rawFilters, organizations: rawFilters.organizations.filter((id) => joinedOrgs.some((o) => o.id === id)) };
    setFilters(f);
    const prompt = q.trim() || (peopleFiltersActive(f) ? promptFromFilters(f, joinedOrgs) : "");
    if (!prompt) { setResults(null); return; }
    const search = { id: `s_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`, abort: new AbortController() };
    activeSearch.current = search;
    setSearching(true);
    setResultsLabel(prompt);
    setResultsNote(null);
    setOrgHint(null);
    try {
      const res = await apiService.runPromptSearch({
        prompt, batchSize: searchCount, mode: "preview", emailTemplate,
        filters: peopleFiltersActive(f) ? f : undefined,
        searchId: search.id, signal: search.abort.signal,
      });
      if (search.abort.signal.aborted || (res as { cancelled?: boolean }).cancelled) return;
      if (isErrorResponse(res)) throw new Error(res.error);
      setResults((res.contacts || []).map(personFromSearch));
      setOrgHint((res as { parsed_query?: { org_hint?: { id: string; name: string; shortName: string } } }).parsed_query?.org_hint ?? null);
      const saved = res.already_saved_contacts?.length ?? 0;
      setResultsNote(res.message || (saved ? `${saved} more ${saved === 1 ? "match is" : "matches are"} already in your Contacts.` : null));
      queryClient.invalidateQueries({ queryKey: CONTACTS_KEY });
    } catch (e: any) {
      if (search.abort.signal.aborted) return;
      const msg = e?.status === 402
        ? "You need more credits for this search. Searches cost 10 credits per person found."
        : e?.message || "Try again in a moment.";
      toast({ title: "Search failed", description: msg, variant: "destructive" });
    } finally {
      if (activeSearch.current === search) {
        activeSearch.current = null;
        setSearching(false);
      }
    }
  }, [emailTemplate, queryClient, joinedOrgs, searchCount]);

  const cancelSearch = useCallback(() => {
    const search = activeSearch.current;
    if (!search) return;
    activeSearch.current = null;
    search.abort.abort();
    setSearching(false);
    setResults(null);
    void apiService.cancelPromptSearch(search.id).catch(() => {});
    toast({ title: "Search cancelled", description: "No credits were charged." });
  }, []);

  // Esc or Ctrl+C cancels a running search. Ctrl only (not Cmd), and not
  // while text is selected, so copying still works.
  useEffect(() => {
    if (!searching) return;
    const onKey = (e: KeyboardEvent) => {
      const ctrlC = e.ctrlKey && !e.metaKey && e.key.toLowerCase() === "c" && !window.getSelection()?.toString();
      if (e.key === "Escape" || ctrlC) { e.preventDefault(); cancelSearch(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [searching, cancelSearch]);

  // ?q= hands a query in from elsewhere (Loop cards, Getting Started). Run
  // it once, then drop it (and any legacy ?tab=) from the URL.
  useEffect(() => {
    if (!user?.uid) return;
    const q = searchParams.get("q");
    if (!q && !searchParams.has("tab")) return;
    const next = new URLSearchParams(searchParams);
    next.delete("q");
    next.delete("tab");
    setSearchParams(next, { replace: true });
    if (q?.trim()) void runSearch(q, EMPTY_PEOPLE_FILTERS);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.uid]);

  const clearSearch = () => {
    setResults(null);
    setQuery("");
    setFilters(EMPTY_PEOPLE_FILTERS);
  };

  // ── Contact + Info ──────────────────────────────────────────────────────
  const [contactStates, setContactStates] = useState<Record<string, ContactState>>({});
  const bookmarks = useBookmarks();

  // People the student's own searches saved and they haven't reached out to
  // yet, newest first, with the search each came from.
  const [foundFilter, setFoundFilter] = useState<string | null>(null);
  const found = useMemo(() => {
    const seen = new Set<string>();
    return bookmarks.contacts
      .filter((c) => c.source === "find_search" && neverEmailed(c))
      .sort((a, b) => String(b.createdAt || "").localeCompare(String(a.createdAt || "")))
      .map((c) => ({ person: personFromSavedSearch(c), query: String(c.foundQuery || "").trim() }))
      // One card per person, even if Contacts holds duplicate rows.
      .filter((f) => !seen.has(f.person.id) && !!seen.add(f.person.id));
  }, [bookmarks.contacts]);
  const recentSearches = useMemo(() => {
    const counts = new Map<string, number>();
    for (const f of found) if (f.query) counts.set(f.query, (counts.get(f.query) ?? 0) + 1);
    return [...counts.entries()].slice(0, 5).map(([query, n]) => ({ query, n }));
  }, [found]);
  // A search chip that no longer has anyone left falls back to All.
  const activeFilter = foundFilter && recentSearches.some((r) => r.query === foundFilter) ? foundFilter : null;
  const foundLead = activeFilter
    ? found.filter((f) => f.query === activeFilter).map((f) => f.person)
    : found.slice(0, FOUND_LEAD).map((f) => f.person);
  const foundMixed = activeFilter ? [] : found.slice(FOUND_LEAD).map((f) => f.person);
  const foundLinkedIns = useMemo(
    () => new Set(found.map((f) => normalizeLinkedIn(f.person.linkedinUrl)).filter(Boolean)),
    [found],
  );
  const [infoPerson, setInfoPerson] = useState<Person | null>(null);
  const setContactState = (id: string, s: ContactState) => setContactStates((m) => ({ ...m, [id]: s }));
  // Gmail links from this session's drafts, until Contacts refetches.
  const [draftUrls, setDraftUrls] = useState<Record<string, string>>({});
  // People contacted this session, newest first, for the progress tray.
  const [trayPeople, setTrayPeople] = useState<Person[]>([]);

  // A card's state: what's in flight this session, else what Contacts says,
  // so someone drafted earlier still reads as drafted after a reload.
  const stateFor = (p: Person): ContactState => {
    const local = contactStates[p.id];
    if (local && local !== "idle") return local;
    const c = bookmarks.contactFor(p);
    return c && !neverEmailed(c) ? "drafted" : "idle";
  };
  const contactHrefFor = (p: Person) => {
    const c = bookmarks.contactFor(p);
    return c?.id ? `/contacts?contact=${encodeURIComponent(c.id)}` : "/contacts";
  };
  const emailHrefFor = (p: Person): string | null => {
    const c = bookmarks.contactFor(p);
    if (c?.gmailThreadId) return `https://mail.google.com/mail/u/0/#all/${c.gmailThreadId}`;
    return c?.gmailDraftUrl || draftUrls[p.id] || null;
  };

  const contact = useCallback(async (person: Person) => {
    if (person.source === "sample") return;
    setTrayPeople((list) => [person, ...list.filter((q) => q.id !== person.id)].slice(0, 4));
    setContactState(person.id, person.source === "recommended" ? "finding" : "working");
    try {
      if (person.source === "recommended") {
        // Reveal (8 credits, refunded on a miss), then draft to that exact
        // person. The outreach route also saves them to Contacts.
        const reveal = await apiService.revealRankerEmail(person.id, person.deckId);
        if (!reveal.email) {
          setContactState(person.id, "no_email");
          toast({ title: "No email found", description: `We couldn't find a verified address for ${person.firstName}. No credits were charged.` });
          return;
        }
        setContactState(person.id, "working");
        await apiService.rankerOutreach(person.id, "draft", person.deckId);
        void apiService.sendRankerFeedback({ candidateId: person.id, signal: "right", deckId: person.deckId, rank: person.rank }).catch(() => {});
      } else {
        if (!person.email) {
          setContactState(person.id, "no_email");
          toast({ title: "No email found", description: `We don't have an address for ${person.firstName}.` });
          return;
        }
        const res = await apiService.generateAndDraftEmails({
          contacts: [{
            FirstName: person.firstName, LastName: person.lastName, name: person.name,
            Email: person.email, Company: person.company, Title: person.title,
          }],
          emailTemplate,
        });
        if ("error" in res) throw new Error(res.message || res.error);
        if (!res.success || !res.draft_count) throw new Error("No draft was created. Check that Gmail is connected.");
        const url = res.drafts?.[0]?.gmailUrl;
        if (url) setDraftUrls((m) => ({ ...m, [person.id]: url }));
      }
      setContactState(person.id, "drafted");
      queryClient.invalidateQueries({ queryKey: CONTACTS_KEY });
      queryClient.invalidateQueries({ queryKey: THREADS_KEY });
      toast({ title: `Drafted an email to ${person.firstName}`, description: "It's in your Gmail drafts and in Contacts." });
    } catch (e: any) {
      setContactState(person.id, "idle");
      setTrayPeople((list) => list.filter((q) => q.id !== person.id));
      const msg = e?.status === 402 ? "You're out of credits for this. Contacting someone costs 8 credits." : e?.message || "Try again.";
      toast({ title: "Couldn't contact them", description: msg, variant: "destructive" });
    }
  }, [emailTemplate, queryClient]);

  const dismiss = (person: Person) => {
    setDismissed((d) => new Set(d).add(person.id));
    void apiService.sendRankerFeedback({ candidateId: person.id, signal: "left", deckId: person.deckId, rank: person.rank }).catch(() => {});
  };

  // Dev only: /find?previewContact=1 puts the first three cards in the
  // finding / writing / drafted states so the UI can be checked without
  // spending credits or creating real drafts.
  const previewContact = import.meta.env.DEV && searchParams.get("previewContact") === "1";
  const previewPeople = results ?? recommendedPeople;
  const previewReady = previewContact && previewPeople.length >= 3;
  useEffect(() => {
    if (!previewReady) return;
    const [a, b, c] = previewPeople;
    setContactStates((m) => ({ ...m, [a.id]: "finding", [b.id]: "working", [c.id]: "drafted" }));
    setDraftUrls((m) => ({ ...m, [c.id]: "https://mail.google.com/mail/u/0/#drafts" }));
    setTrayPeople([a, b, c]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [previewReady]);

  // ── Render ──────────────────────────────────────────────────────────────
  if (schoolLoaded && !userUniversity && !universityOptOut && user?.uid) {
    return (
      <SidebarProvider>
        <div className="flex min-h-screen w-full font-sans" style={{ color: "var(--ink)" }}>
          <AppSidebar />
          <MainContentWrapper>
            <AppHeader title="" />
            <NoSchoolEmptyState uid={user.uid} onSchoolSet={() => setForceRefresh((n) => n + 1)} />
          </MainContentWrapper>
        </div>
      </SidebarProvider>
    );
  }

  const inSearch = results !== null || searching;
  // Someone already in "Recently found" doesn't show twice in the feed.
  const people = inSearch
    ? results ?? []
    : recommendedPeople.filter((p) => !foundLinkedIns.has(normalizeLinkedIn(p.linkedinUrl)));

  const card = (p: Person, featured = false, found = false, foundLabel?: string) => (
    <PersonCard
      key={`${p.source}-${p.id}`}
      person={p}
      featured={featured}
      found={found}
      foundLabel={foundLabel}
      contactState={stateFor(p)}
      contactHref={contactHrefFor(p)}
      emailHref={emailHrefFor(p)}
      onContact={() => contact(p)}
      onInfo={() => setInfoPerson(p)}
      onDismiss={p.source === "recommended" ? () => dismiss(p) : undefined}
      bookmarked={bookmarks.isBookmarked(p)}
      onToggleBookmark={() => void bookmarks.toggle(p)}
      userSchool={userUniversity}
    />
  );
  // Cards from earlier searches mixed into the recommended feed.
  const withFound = (list: Person[]) => (
    foundMixed.length
      ? disperse(list.map((p) => card(p)), foundMixed.map((p) => card(p, false, true, "Your find")), FOUND_EVERY)
      : list.map((p) => card(p))
  );


  return (
    <SidebarProvider>
      <div className="flex min-h-screen w-full bg-[#F5F5F7] font-sans text-ink">
        <AppSidebar />
        <MainContentWrapper>
          <AppHeader title="" />
          <GoalsPromptBanner />
          <div ref={scrollRef} className="flex-1 overflow-y-auto bg-[#F5F5F7]">
            <div className="mx-auto max-w-[1320px] px-6 pb-16 pt-4 sm:px-10">
              {user?.uid && !IS_DEV_PREVIEW && <FirstEmailReminder uid={user.uid} />}
              <div className="mt-4" data-tour="tour-search-form">
                <PeopleSearchBar
                  query={query}
                  onQueryChange={setQuery}
                  filters={filters}
                  onSearch={runSearch}
                  userSchool={userUniversity}
                  orgs={joinedOrgs}
                  showJoinOrg={joinedOrgs.length === 0 && allOrgs.length > 0}
                  searching={searching}
                  count={searchCount}
                  maxCount={maxCount}
                  onCountChange={setCount}
                  onCancel={cancelSearch}
                />
              </div>

              {/* Org alumni directories the student belongs to (OFF-42) */}
              {joinedOrgs.length > 0 && (
                <div className="mt-4 flex flex-wrap gap-3">
                  {joinedOrgs.map((o) => (
                    <Link
                      key={o.id}
                      to={`/directory/${o.id}`}
                      className="group flex min-w-0 flex-1 items-center gap-3 rounded-[18px] bg-white px-4 py-3 shadow-[0_1px_2px_rgba(0,0,0,0.04),0_4px_14px_rgba(0,0,0,0.05)] transition-shadow hover:shadow-[0_2px_4px_rgba(0,0,0,0.04),0_12px_32px_rgba(0,0,0,0.08)] sm:max-w-[520px]"
                      style={{ fontFamily: APPLE_FONT }}
                    >
                      {orgLogo(o.id) ? (
                        <img src={orgLogo(o.id)!} alt="" aria-hidden className="h-10 w-10 shrink-0 object-contain" />
                      ) : (
                        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-[#EEF1FA] text-[#3A4E8C]">
                          <Users className="h-4 w-4" />
                        </span>
                      )}
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-[14.5px] font-semibold text-[#1D1D1F]">{o.name} alumni directory</span>
                        <span className="block truncate text-[12.5px] text-[#6E6E73]">See every alum, free to browse</span>
                      </span>
                      <span className="inline-flex shrink-0 items-center gap-0.5 text-[13px] font-medium text-[#0071E3]">
                        Open <ChevronRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
                      </span>
                    </Link>
                  ))}
                </div>
              )}

              {/* Section header */}
              <div className="mb-4 mt-8 flex flex-wrap items-end justify-between gap-3">
                {inSearch ? (
                  <div>
                    <button type="button" onClick={clearSearch} className="mb-1 inline-flex items-center gap-1 text-[13px] text-[var(--accent,#4A60A8)] hover:underline">
                      <ArrowLeft className="h-3.5 w-3.5" /> Back to recommended
                    </button>
                    <div className="text-[28px] font-semibold tracking-[-0.02em]" style={{ fontFamily: APPLE_FONT, color: "#1D1D1F" }}>
                      {searching ? "Searching…" : `${people.length} ${people.length === 1 ? "person" : "people"} found`}
                    </div>
                    <p className="mt-0.5 text-[13px] text-ink-3">"{resultsLabel}" · 10 credits per person found</p>
                  </div>
                ) : (
                  <div>
                    <div className="text-[28px] font-semibold tracking-[-0.02em]" style={{ fontFamily: APPLE_FONT, color: "#1D1D1F" }}>Recommended for you</div>
                    {/* One quiet line, Apple style: the status in grey, the fix as a blue link. */}
                    <p className="mt-1 text-[14px]" style={{ fontFamily: APPLE_FONT, color: "#6E6E73" }}>
                      {feedEmpty ? (
                        <>
                          {recommended.isError ? "We couldn't load your recommendations." : "We couldn't find anyone just now."}{" "}
                          <button type="button" onClick={() => recommended.refetch()} className="whitespace-nowrap text-[#0066CC] hover:underline">
                            Try again ›
                          </button>
                        </>
                      ) : (
                        "People worth reaching out to, picked from your school and target companies."
                      )}
                    </p>
                  </div>
                )}
                {!inSearch && !feedEmpty && (
                  <button
                    type="button"
                    onClick={() => { setDismissed(new Set()); queryClient.resetQueries({ queryKey: ["findRecommended-v2", user?.uid, seeds] }); }}
                    className="inline-flex h-8 items-center gap-1.5 rounded-full bg-white px-3.5 text-[13px] font-medium text-[#1D1D1F] shadow-[0_1px_2px_rgba(0,0,0,0.06)] hover:bg-[#FAFAFA]"
                  >
                    <RefreshCw className="h-3.5 w-3.5" /> Refresh
                  </button>
                )}
              </div>

              {/* Recently found (OFF-56): leads the feed in the search green */}
              {!inSearch && found.length > 0 && (
                <section
                  className="mb-8 rounded-[26px] p-4 sm:p-5"
                  style={{ fontFamily: APPLE_FONT, background: "linear-gradient(180deg, #EAF5EF 0%, rgba(234,245,239,0) 100%)" }}
                >
                  <div className="mb-3 flex flex-wrap items-start justify-between gap-2 px-1">
                    <div className="flex items-center gap-2.5">
                      <span className="h-2.5 w-2.5 rounded-full bg-[#01754F]" />
                      <div>
                        <div className="text-[17px] font-semibold tracking-[-0.01em] text-[#1D1D1F]">Recently found</div>
                        <div className="text-[13px] text-[#6E6E73]">
                          People from your searches you haven't reached out to yet. They're saved in Contacts.
                        </div>
                      </div>
                    </div>
                    <Link to="/contacts" className="inline-flex items-center gap-0.5 text-[13px] font-medium text-[#01754F] hover:underline">
                      Contacts <ChevronRight className="h-4 w-4" />
                    </Link>
                  </div>
                  {recentSearches.length > 1 && (
                    <div className="mb-3.5 flex gap-2 overflow-x-auto px-1 pb-1">
                      {[{ query: null as string | null, label: "All", n: found.length }, ...recentSearches.map((r) => ({ query: r.query as string | null, label: r.query, n: r.n }))].map((chip) => {
                        const on = activeFilter === chip.query;
                        return (
                          <button
                            key={chip.label}
                            type="button"
                            onClick={() => setFoundFilter(chip.query)}
                            aria-pressed={on}
                            title={chip.label}
                            className={`inline-flex h-8 max-w-[280px] shrink-0 items-center gap-1.5 rounded-full border px-3 text-[13px] font-medium transition-colors ${
                              on ? "border-[#01754F] bg-[#01754F] text-white" : "border-[#CFE5D9] bg-white text-[#1D1D1F] hover:border-[#01754F]"
                            }`}
                          >
                            <span className="truncate">{chip.label}</span>
                            <span className={`tabular-nums ${on ? "text-white/80" : "text-[#86868B]"}`}>{chip.n}</span>
                          </button>
                        );
                      })}
                    </div>
                  )}
                  <div className={GRID}>{foundLead.map((p) => card(p, false, true, "Your find"))}</div>
                  {foundMixed.length > 0 && (
                    <p className="mt-3 px-1 text-[12.5px] text-[#6E6E73]">
                      {foundMixed.length} more from your searches are mixed into your feed below.
                    </p>
                  )}
                </section>
              )}

              {inSearch && orgHint && !searching && (
                <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-[18px] px-4 py-3 text-white" style={{ fontFamily: APPLE_FONT, background: "linear-gradient(165deg, #25335A 0%, #0F172A 100%)" }}>
                  <span className="text-[14px]">{orgHint.shortName} alumni are in a members-only directory. Join with your chapter's password to search them.</span>
                  <Link to="/account-settings#organizations" className="inline-flex h-9 shrink-0 items-center gap-1 rounded-full bg-white px-4 text-[14px] font-semibold text-[#0F172A]">
                    Add {orgHint.shortName} in Settings <ChevronRight className="h-4 w-4" />
                  </Link>
                </div>
              )}
              {inSearch && resultsNote && !searching && !orgHint && (
                <p className="mb-4 text-[13px] text-ink-3">{resultsNote}</p>
              )}

              {/* Gallery */}
              {searching ? (
                <SearchLoader count={searchCount} grid={GRID} onCancel={cancelSearch} />
              ) : !inSearch && feedEmpty ? (
                foundMixed.length > 0 ? <div className={GRID}>{withFound([])}</div> : null
              ) : (recommendedLoading || feedProfile === null || (recommendedPeople.length === 0 && (hasNextPage || isFetchingNextPage))) && !inSearch ? (
                <div className={GRID}>
                  {Array.from({ length: 15 }).map((_, i) => <CardSkeleton key={i} />)}
                </div>
              ) : people.length === 0 && orgHint ? null : people.length === 0 ? (
                <div className="rounded-2xl border border-dashed border-line px-6 py-14 text-center text-[14px] text-ink-3">
                  Nobody new matched that search. Try fewer filters or a broader title.
                </div>
              ) : (
                (() => {
                  // Search results are one flat grid. The feed leads with the
                  // top 4. PLACEHOLDER: these are simply the first four the
                  // recommender returned; the real pick (shared clubs, same
                  // alumni groups, likeliest to reply) comes with the pairing
                  // algorithm.
                  if (inSearch) {
                    return <div className={GRID}>{people.map((p) => card(p, false, true))}</div>;
                  }
                  if (people.length <= TOP_COUNT) {
                    return <div className={GRID}>{people.map((p) => card(p, true))}{foundMixed.map((p) => card(p, false, true, "Your find"))}</div>;
                  }
                  const top = people.slice(0, TOP_COUNT);
                  const rest = people.slice(TOP_COUNT);
                  return (
                    <>
                      <section
                        className="rounded-[26px] p-4 sm:p-5"
                        style={{ fontFamily: APPLE_FONT, background: "linear-gradient(180deg, #FFFBF0 0%, rgba(255,251,240,0) 100%)" }}
                      >
                        <div className="mb-3.5 flex items-center gap-2.5 px-1">
                          <span className="h-2.5 w-2.5 rounded-full" style={{ background: GOLD_RING }} />
                          <div>
                            <div className="text-[17px] font-semibold tracking-[-0.01em] text-[#1D1D1F]">Top connections for you</div>
                            <div className="text-[13px] text-[#6E6E73]">
                              These four have the most in common with you, so they're the most likely to reply.
                            </div>
                          </div>
                        </div>
                        <div className={GRID}>{top.map((p) => card(p, true))}</div>
                      </section>
                      <div className="mb-3 mt-8 px-1 text-[17px] font-semibold tracking-[-0.01em] text-[#1D1D1F]" style={{ fontFamily: APPLE_FONT }}>
                        More people for you
                      </div>
                      <div className={GRID}>{withFound(rest)}</div>
                    </>
                  );
                })()
              )}

              {!inSearch && !feedEmpty && (
                <>
                  <div ref={sentinelRef} aria-hidden className="h-px" />
                  {isFetchingNextPage && (
                    <div className={`${GRID} mt-4`}>
                      {Array.from({ length: 5 }).map((_, i) => <CardSkeleton key={i} />)}
                    </div>
                  )}
                  {!hasNextPage && recommendedPeople.length > 0 && (
                    <p className="mt-10 text-center text-[13px]" style={{ color: "#86868B" }}>
                      That's everyone for now. Check back soon for new people.
                    </p>
                  )}
                </>
              )}
            </div>
          </div>
        </MainContentWrapper>
      </div>

      <PersonInfoDialog
        person={infoPerson}
        contactState={infoPerson ? stateFor(infoPerson) : "idle"}
        onContact={() => infoPerson && contact(infoPerson)}
        onClose={() => setInfoPerson(null)}
      />
      <ContactProgressTray
        items={trayPeople.map((p): TrayItem => ({
          person: p, state: stateFor(p), contactHref: contactHrefFor(p), emailHref: emailHrefFor(p),
        }))}
        onDismiss={(id) => setTrayPeople((list) => list.filter((q) => q.id !== id))}
      />
    </SidebarProvider>
  );
};

export default FindPage;
