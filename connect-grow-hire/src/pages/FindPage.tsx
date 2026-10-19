// Search. Opens on a gallery of recommended people (the same ranked deck the
// mobile People feed uses), so there is always someone to reach out to.
// The search bar and LinkedIn-style filter pills on top swap the gallery for
// search results in the same card layout.
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, ArrowLeft } from "lucide-react";
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
import { CONTACTS_KEY, THREADS_KEY } from "@/components/contacts/contactsModel";
import { PeopleSearchBar } from "@/components/find/people/PeopleSearchBar";
import { APPLE_FONT, PersonCard, type ContactState } from "@/components/find/people/PersonCard";
import { PersonInfoDialog } from "@/components/find/people/PersonInfoDialog";
import { GOLD_RING } from "@/components/find/people/PersonAvatar";
import { seedCompanies, type FeedProfile } from "@/components/find/people/feedSeeds";
import { useBookmarks } from "@/components/find/people/useBookmarks";
import {
  personFromRanker, personFromSearch, type Person,
} from "@/components/find/people/peopleModel";

// First screen loads 40 people; scrolling near the bottom pulls 20 more.
const FIRST_PAGE = 40;
const NEXT_PAGE = 20;
// Narrow cards: as many ~236px columns as fit.
const GRID = "grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(236px,1fr))]";
const SEARCH_BATCH = 6;
// How many people lead the feed as "Top connections".
const TOP_COUNT = 4;
// Written by the landing page hero search (HeroSearchCTA.PENDING_QUERY_KEY).
const PENDING_QUERY_KEY = "offerloop_pending_query";

// When only filters are set, build the sentence the prompt parser expects.
function promptFromFilters(f: PeopleFilters): string {
  const parts = [f.titles.length ? f.titles.join(" or ") : "People"];
  if (f.companies.length) parts.push(`at ${f.companies.join(" or ")}`);
  if (f.industries.length) parts.push(`in ${f.industries.join(" or ")}`);
  if (f.locations.length) parts.push(`in ${f.locations.join(" or ")}`);
  if (f.schools.length) parts.push(`who went to ${f.schools.join(" or ")}`);
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
  const [searching, setSearching] = useState(false);
  const [results, setResults] = useState<Person[] | null>(null);
  const [resultsLabel, setResultsLabel] = useState("");
  const [resultsNote, setResultsNote] = useState<string | null>(null);

  const runSearch = useCallback(async (q: string, f: PeopleFilters) => {
    setFilters(f);
    const prompt = q.trim() || (peopleFiltersActive(f) ? promptFromFilters(f) : "");
    if (!prompt) { setResults(null); return; }
    setSearching(true);
    setResultsLabel(prompt);
    setResultsNote(null);
    try {
      const res = await apiService.runPromptSearch({
        prompt, batchSize: SEARCH_BATCH, mode: "preview", emailTemplate,
        filters: peopleFiltersActive(f) ? f : undefined,
      });
      if (isErrorResponse(res)) throw new Error(res.error);
      setResults((res.contacts || []).map(personFromSearch));
      const saved = res.already_saved_contacts?.length ?? 0;
      setResultsNote(res.message || (saved ? `${saved} more ${saved === 1 ? "match is" : "matches are"} already in your Contacts.` : null));
      queryClient.invalidateQueries({ queryKey: CONTACTS_KEY });
    } catch (e: any) {
      const msg = e?.status === 402
        ? "You need more credits for this search. Searches cost 10 credits per person found."
        : e?.message || "Try again in a moment.";
      toast({ title: "Search failed", description: msg, variant: "destructive" });
    } finally {
      setSearching(false);
    }
  }, [emailTemplate, queryClient]);

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
  const [infoPerson, setInfoPerson] = useState<Person | null>(null);
  const setContactState = (id: string, s: ContactState) => setContactStates((m) => ({ ...m, [id]: s }));

  const contact = useCallback(async (person: Person) => {
    if (person.source === "sample") return;
    setContactState(person.id, "working");
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
      }
      setContactState(person.id, "drafted");
      queryClient.invalidateQueries({ queryKey: CONTACTS_KEY });
      queryClient.invalidateQueries({ queryKey: THREADS_KEY });
      toast({ title: `Drafted an email to ${person.firstName}`, description: "It's in your Gmail drafts and in Contacts." });
    } catch (e: any) {
      setContactState(person.id, "idle");
      const msg = e?.status === 402 ? "You're out of credits for this. Contacting someone costs 8 credits." : e?.message || "Try again.";
      toast({ title: "Couldn't contact them", description: msg, variant: "destructive" });
    }
  }, [emailTemplate, queryClient]);

  const dismiss = (person: Person) => {
    setDismissed((d) => new Set(d).add(person.id));
    void apiService.sendRankerFeedback({ candidateId: person.id, signal: "left", deckId: person.deckId, rank: person.rank }).catch(() => {});
  };

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
  const people = inSearch ? results ?? [] : recommendedPeople;

  return (
    <SidebarProvider>
      <div className="flex min-h-screen w-full bg-[#F5F5F7] font-sans text-ink">
        <AppSidebar />
        <MainContentWrapper>
          <AppHeader title="" />
          <GoalsPromptBanner />
          <div ref={scrollRef} className="flex-1 overflow-y-auto bg-[#F5F5F7]">
            <div className="mx-auto max-w-[1320px] px-6 pb-16 pt-4 sm:px-10">
              <div className="mt-4" data-tour="tour-search-form">
                <PeopleSearchBar
                  query={query}
                  onQueryChange={setQuery}
                  filters={filters}
                  onSearch={runSearch}
                  userSchool={userUniversity}
                  searching={searching}
                />
              </div>

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

              {inSearch && resultsNote && !searching && (
                <p className="mb-4 text-[13px] text-ink-3">{resultsNote}</p>
              )}

              {/* Gallery */}
              {!inSearch && feedEmpty ? null : ((recommendedLoading || feedProfile === null || (recommendedPeople.length === 0 && (hasNextPage || isFetchingNextPage))) && !inSearch) || searching ? (
                <div className={GRID}>
                  {Array.from({ length: searching ? SEARCH_BATCH : 15 }).map((_, i) => <CardSkeleton key={i} />)}
                </div>
              ) : people.length === 0 ? (
                <div className="rounded-2xl border border-dashed border-line px-6 py-14 text-center text-[14px] text-ink-3">
                  Nobody new matched that search. Try fewer filters or a broader title.
                </div>
              ) : (
                (() => {
                  const card = (p: Person, featured = false) => (
                    <PersonCard
                      key={`${p.source}-${p.id}`}
                      person={p}
                      featured={featured}
                      contactState={contactStates[p.id] ?? "idle"}
                      onContact={() => contact(p)}
                      onInfo={() => setInfoPerson(p)}
                      onDismiss={p.source === "recommended" ? () => dismiss(p) : undefined}
                      bookmarked={bookmarks.isBookmarked(p)}
                      onToggleBookmark={() => void bookmarks.toggle(p)}
                    />
                  );
                  // Search results are one flat grid. The feed leads with the
                  // top 4. PLACEHOLDER: these are simply the first four the
                  // recommender returned; the real pick (shared clubs, same
                  // alumni groups, likeliest to reply) comes with the pairing
                  // algorithm.
                  if (inSearch || people.length <= TOP_COUNT) {
                    return <div className={GRID}>{people.map((p) => card(p, !inSearch))}</div>;
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
                      <div className={GRID}>{rest.map((p) => card(p))}</div>
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
        contactState={infoPerson ? contactStates[infoPerson.id] ?? "idle" : "idle"}
        onContact={() => infoPerson && contact(infoPerson)}
        onClose={() => setInfoPerson(null)}
      />
    </SidebarProvider>
  );
};

export default FindPage;
