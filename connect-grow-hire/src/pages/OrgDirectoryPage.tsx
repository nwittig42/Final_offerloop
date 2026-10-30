// The whole alumni directory of an org the student belongs to (OFF-42).
// Every living alum, free to browse: name, class year, job, LinkedIn, and
// their email when we have it. Contact drafts an intro the same way Find
// does (which also saves them to Contacts). For alumni with no email yet,
// Contact first finds it from their LinkedIn, like the Chrome extension.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ChevronDown, Search, X } from "lucide-react";
import { AppSidebar } from "@/components/AppSidebar";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppHeader } from "@/components/AppHeader";
import { MainContentWrapper } from "@/components/MainContentWrapper";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { apiService, type DirectoryMember, type EmailTemplate } from "@/services/api";
import { toast } from "@/hooks/use-toast";
import { CONTACTS_KEY, THREADS_KEY } from "@/components/contacts/contactsModel";
import { APPLE_FONT, PersonCard, type ContactState } from "@/components/find/people/PersonCard";
import { PersonProfileDialog } from "@/components/find/people/PersonProfileDialog";
import { useBookmarks } from "@/components/find/people/useBookmarks";
import { personFromDirectory, type Person } from "@/components/find/people/peopleModel";
import { orgAccent, orgMonogram } from "@/hooks/useOrgs";
import { orgLogo } from "@/lib/orgLogos";

const GRID = "grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(236px,1fr))]";
const PAGE = 48;

// "#A6192E" -> "rgba(166, 25, 46, a)"
function withAlpha(hex: string, a: number): string {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

// Darker version of a hex color (f = 0.55 keeps 55% of each channel).
function shade(hex: string, f: number): string {
  const n = parseInt(hex.slice(1), 16);
  const c = (v: number) => Math.round(v * f).toString(16).padStart(2, "0");
  return `#${c((n >> 16) & 255)}${c((n >> 8) & 255)}${c(n & 255)}`;
}

type Sort = "complete" | "recent" | "oldest" | "name";

// Same cadence as the extension: FullEnrich's waterfall can run minutes.
const EMAIL_POLL_EVERY_MS = 5000;
const EMAIL_POLL_MAX_TRIES = 84;

// Their email from a LinkedIn URL, or null when none exists, plus the
// contact it was saved on (import-linkedin adds them to Contacts). A
// background lookup that comes back empty refunds its credits.
async function findEmailFromLinkedIn(linkedinUrl: string): Promise<{ email: string | null; contactId: string }> {
  const res = await apiService.importLinkedInContact(linkedinUrl);
  const contactId = res.contact_id;
  if (res.contact?.email) return { email: res.contact.email, contactId };
  if (!res.email_pending || !contactId) return { email: null, contactId };
  for (let i = 0; i < EMAIL_POLL_MAX_TRIES; i++) {
    await new Promise((r) => setTimeout(r, EMAIL_POLL_EVERY_MS));
    const s = await apiService.getContactEmailStatus(contactId).catch(() => null);
    if (s?.status === "found") return { email: s.email, contactId };
    if (s?.status === "miss") return { email: null, contactId };
  }
  return { email: null, contactId };
}

// How much of a profile we can show: photo, then a job, then nothing.
const completeness = (m: DirectoryMember) => (m.photoUrl ? 2 : 0) + (m.company || m.title ? 1 : 0);

export default function OrgDirectoryPage() {
  const { orgId = "" } = useParams();
  const { user } = useFirebaseAuth();
  const queryClient = useQueryClient();

  const { data, isLoading, error } = useQuery({
    queryKey: ["orgMembers", orgId],
    queryFn: () => apiService.getOrgMembers(orgId),
    enabled: !!user?.uid && !!orgId,
    retry: false,
  });

  const [emailTemplate, setEmailTemplate] = useState<EmailTemplate | null>(null);
  useEffect(() => {
    if (!user?.uid) return;
    apiService.getEmailTemplate().then((t) => setEmailTemplate({
      purpose: t.purpose, stylePreset: t.stylePreset, customInstructions: t.customInstructions,
      signoffPhrase: t.signoffPhrase, signatureBlock: t.signatureBlock, name: t.name,
      subject: t.subject, savedTemplateId: t.savedTemplateId,
    })).catch(() => {});
  }, [user?.uid]);

  const [text, setText] = useState("");
  // Fullest profiles first, so the directory opens on real cards rather
  // than this year's grads, whom the directory has no job for yet.
  const [sort, setSort] = useState<Sort>("complete");
  const [shown, setShown] = useState(PAGE);
  const [contactStates, setContactStates] = useState<Record<string, ContactState>>({});
  const [profilePerson, setProfilePerson] = useState<Person | null>(null);
  // Save / Meeting prep in the profile write to Contacts like Find's do.
  const bookmarks = useBookmarks();

  const people = useMemo(() => {
    if (!data) return [];
    const q = text.trim().toLowerCase();
    const rows = data.members.filter((m) =>
      !q || [m.firstName, m.lastName, m.company, m.title, m.headline, m.year ? String(m.year) : ""]
        .join(" ").toLowerCase().includes(q),
    );
    const sorted = [...rows].sort((a, b) =>
      sort === "name"
        ? `${a.lastName} ${a.firstName}`.localeCompare(`${b.lastName} ${b.firstName}`)
        : sort === "complete" ? completeness(b) - completeness(a) || (b.year ?? 0) - (a.year ?? 0)
        : sort === "recent" ? (b.year ?? 0) - (a.year ?? 0) : (a.year ?? 9999) - (b.year ?? 9999),
    );
    return sorted.map((m, i) => personFromDirectory(m, i, orgId));
  }, [data, text, sort, orgId]);

  useEffect(() => setShown(PAGE), [text, sort]);

  // Load the next page as the sentinel scrolls into view.
  const sentinel = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = sentinel.current;
    if (!el) return;
    const io = new IntersectionObserver((entries) => {
      if (entries[0]?.isIntersecting) setShown((n) => n + PAGE);
    }, { rootMargin: "600px" });
    io.observe(el);
    return () => io.disconnect();
  }, [people.length, shown]);

  const stateFor = (p: Person): ContactState => contactStates[p.id] ?? "idle";

  const contact = useCallback(async (person: Person) => {
    const setState = (s: ContactState) => setContactStates((m) => ({ ...m, [person.id]: s }));
    let email: string | null = person.email;
    if (!email) {
      if (!person.linkedinUrl) {
        toast({ title: `No way to reach ${person.firstName} yet`, description: "The directory has no email or LinkedIn for them." });
        return;
      }
      // No email in the directory: find it from their LinkedIn the way the
      // Chrome extension does (OFF-51), then draft below as usual.
      setState("finding");
      let contactId = "";
      try {
        ({ email, contactId } = await findEmailFromLinkedIn(person.linkedinUrl));
      } catch (e: any) {
        setState("idle");
        toast({ title: `Couldn't look ${person.firstName} up`, description: e?.message || "Try again.", variant: "destructive" });
        return;
      }
      if (!email) {
        setState("no_email");
        return;
      }
      queryClient.invalidateQueries({ queryKey: CONTACTS_KEY });
      // Show it on their card now, and save it there for every member.
      const found = email;
      queryClient.setQueryData<{ org: any; members: DirectoryMember[] }>(["orgMembers", orgId], (d) => d && {
        ...d,
        members: d.members.map((m) => (m.personKey === person.id ? { ...m, email: found, hasEmail: true } : m)),
      });
      if (contactId) apiService.saveOrgFoundEmail(orgId, person.id, contactId).catch(() => {});
    }
    setState("working");
    try {
      const res = await apiService.generateAndDraftEmails({
        contacts: [{
          FirstName: person.firstName, LastName: person.lastName, name: person.name,
          Email: email, Company: person.company, Title: person.title,
        }],
        emailTemplate,
      });
      if ("error" in res) throw new Error(res.message || res.error);
      if (!res.success || !res.draft_count) throw new Error("No draft was created. Check that Gmail is connected.");
      setState("drafted");
      queryClient.invalidateQueries({ queryKey: CONTACTS_KEY });
      queryClient.invalidateQueries({ queryKey: THREADS_KEY });
      toast({ title: `Drafted an email to ${person.firstName}`, description: "It's in your Gmail drafts and in Contacts." });
    } catch (e: any) {
      setState("idle");
      toast({ title: "Couldn't contact them", description: e?.message || "Try again.", variant: "destructive" });
    }
  }, [emailTemplate, queryClient, orgId]);

  const org = data?.org;
  // Orgs with a logo (TKE) get the branded page: their color as the
  // background and the crest large behind the cards. Others keep the band.
  const logo = org ? orgLogo(org.id) : null;
  const branded = !!logo;
  const accent = orgAccent(org);
  const pageBackground: React.CSSProperties = branded
    ? {
        backgroundColor: accent,
        // Top layer tints the crest underneath it into a watermark.
        backgroundImage: `linear-gradient(160deg, ${withAlpha(accent, 0.80)} 0%, ${withAlpha(shade(accent, 0.55), 0.90)} 100%), url(${logo})`,
        backgroundRepeat: "no-repeat, no-repeat",
        backgroundPosition: "center, right -4% center",
        backgroundSize: "cover, min(82vh, 820px) auto",
        backgroundAttachment: "fixed, fixed",
      }
    : { background: "#ECEFF6" };
  const notMember = (error as Error | null)?.message === "not_a_member";

  return (
    <SidebarProvider>
      <div className="flex min-h-screen w-full bg-[#F5F5F7] font-sans text-ink">
        <AppSidebar />
        <MainContentWrapper>
          <AppHeader title="" />
          <div className="flex-1 overflow-y-auto" style={pageBackground}>
            <div className="mx-auto max-w-[1320px] px-4 pb-16 pt-6 sm:px-10" style={{ fontFamily: APPLE_FONT }}>
              <Link
                to="/find"
                className={`inline-flex h-11 items-center gap-2 rounded-full px-5 text-[15px] font-semibold transition-colors ${
                  branded
                    ? "bg-white/15 text-white ring-1 ring-white/30 hover:bg-white/25"
                    : "bg-white text-[#1D1D1F] shadow-[0_1px_2px_rgba(0,0,0,0.06)] hover:bg-[#FAFAFA]"
                }`}
              >
                <ArrowLeft className="h-5 w-5" strokeWidth={2.2} /> Back to Find
              </Link>

              {error ? (
                <div className="mt-10 rounded-[22px] bg-white p-8 text-center">
                  <div className="text-[19px] font-semibold text-[#1D1D1F]">
                    {notMember ? "This directory is for members" : "Couldn't load the directory"}
                  </div>
                  <p className="mt-2 text-[14px] text-[#6E6E73]">
                    {notMember
                      ? <>Add the organization in <Link to="/account-settings#organizations" className="text-[#0071E3] hover:underline">Account Settings</Link> to open its alumni directory.</>
                      : "Try again in a moment."}
                  </p>
                </div>
              ) : (
                <>
                  {branded ? (
                    /* An org with its own logo gets its own space: its color as
                       the page, its crest large behind everything. */
                    <div className="mt-8 flex items-center gap-5 text-white">
                      <span className="hidden h-20 w-20 shrink-0 items-center justify-center rounded-[20px] bg-white p-2.5 shadow-[0_8px_24px_rgba(0,0,0,0.3)] sm:flex" aria-hidden>
                        <img src={logo!} alt="" className="h-full w-full object-contain" />
                      </span>
                      <div className="min-w-0">
                        <h1 className="text-[34px] font-semibold leading-tight tracking-[-0.02em] sm:text-[40px]" style={{ fontFamily: APPLE_FONT, color: "#FFFFFF" }}>
                          {org!.name} alumni
                        </h1>
                        <p className="mt-1 text-[15px] text-white/80">
                          {data ? `${data.members.length.toLocaleString()} alumni` : "Loading…"}
                          {org?.school ? ` · ${org.school}` : ""}. Free for members. Contact drafts an intro in your voice.
                        </p>
                      </div>
                    </div>
                  ) : (
                    /* The org's own space: a header band in its color */
                    <div
                      className="mt-6 flex items-center gap-4 rounded-[26px] px-5 py-6 text-white sm:px-8"
                      style={{ background: `linear-gradient(160deg, ${orgAccent(org)} 0%, #1A1A1A 140%)` }}
                    >
                      {org && (
                        <span className="hidden h-14 w-14 shrink-0 items-center justify-center rounded-[16px] bg-white/15 text-[17px] font-bold tracking-wide ring-1 ring-white/25 sm:flex" aria-hidden>
                          {orgMonogram(org)}
                        </span>
                      )}
                      <div className="min-w-0">
                        <h1 className="text-[28px] font-semibold leading-tight tracking-[-0.02em]" style={{ fontFamily: APPLE_FONT, color: "#FFFFFF" }}>
                          {org ? `${org.name} alumni` : "Alumni directory"}
                        </h1>
                        <p className="mt-1 text-[14px] text-white/75">
                          {data ? `${data.members.length.toLocaleString()} alumni` : "Loading…"}
                          {org?.school ? ` · ${org.school}` : ""}. Free for members. Contact drafts an intro in your voice.
                        </p>
                      </div>
                    </div>
                  )}

                  {/* Finding someone is the main job here, so the search leads. */}
                  <div className="mt-7 flex flex-wrap items-center gap-3">
                    <label className="flex h-14 min-w-0 flex-1 items-center gap-3 rounded-full bg-white px-6 shadow-[0_6px_24px_rgba(0,0,0,0.18)] ring-2 ring-transparent transition-shadow focus-within:ring-white/70 sm:max-w-[760px]">
                      <Search className="h-5 w-5 shrink-0 text-[#6E6E73]" strokeWidth={2.2} />
                      <input
                        value={text}
                        onChange={(e) => setText(e.target.value)}
                        placeholder={org ? `Search ${org.shortName} alumni by name, company, role or year` : "Search by name, company, role or year"}
                        aria-label="Search the directory"
                        className="min-w-0 flex-1 bg-transparent text-[17px] text-[#1D1D1F] outline-none placeholder:text-[#86868B]"
                      />
                      {text && (
                        <button type="button" onClick={() => setText("")} aria-label="Clear search" className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[#86868B] hover:bg-[#F5F5F7]">
                          <X className="h-4 w-4" />
                        </button>
                      )}
                    </label>
                    {/* Native arrow hidden (it hugged the edge); ours has room. */}
                    <div className="relative">
                      <select
                        value={sort}
                        onChange={(e) => setSort(e.target.value as Sort)}
                        aria-label="Sort"
                        className="h-14 cursor-pointer appearance-none rounded-full bg-white pl-5 pr-12 text-[15px] font-medium text-[#1D1D1F] shadow-[0_6px_24px_rgba(0,0,0,0.12)] outline-none"
                      >
                        <option value="complete">Most complete first</option>
                        <option value="recent">Newest first</option>
                        <option value="oldest">Oldest first</option>
                        <option value="name">Last name A–Z</option>
                      </select>
                      <ChevronDown className="pointer-events-none absolute right-5 top-1/2 h-5 w-5 -translate-y-1/2 text-[#1D1D1F]" strokeWidth={2.2} />
                    </div>
                    {data && text && (
                      <span className={`text-[14px] ${branded ? "text-white/85" : "text-[#6E6E73]"}`}>
                        {people.length.toLocaleString()} match{people.length === 1 ? "" : "es"}
                      </span>
                    )}
                  </div>

                  <div className={`mt-6 ${GRID}`}>
                    {isLoading && Array.from({ length: 8 }).map((_, i) => (
                      <div key={i} className="h-[244px] animate-pulse rounded-[22px] bg-white" />
                    ))}
                    {people.slice(0, shown).map((person) => (
                      <PersonCard
                        key={person.id}
                        person={person}
                        contactState={stateFor(person)}
                        onContact={() => contact(person)}
                        onOpen={() => setProfilePerson(person)}
                      />
                    ))}
                  </div>
                  {data && people.length === 0 && (
                    <p className={`mt-8 text-center text-[15px] ${branded ? "text-white/85" : "text-[#6E6E73]"}`}>No alumni match "{text}".</p>
                  )}
                  {shown < people.length && <div ref={sentinel} className="h-10" />}
                </>
              )}
            </div>
          </div>
        </MainContentWrapper>
      </div>
      <PersonProfileDialog
        person={profilePerson}
        contactState={profilePerson ? stateFor(profilePerson) : "idle"}
        onContact={() => profilePerson && contact(profilePerson)}
        isBookmarked={!!profilePerson && bookmarks.isBookmarked(profilePerson)}
        onToggleBookmark={() => { if (profilePerson) void bookmarks.toggle(profilePerson); }}
        ensureSaved={bookmarks.ensureSaved}
        email={profilePerson?.email}
        onClose={() => setProfilePerson(null)}
      />
    </SidebarProvider>
  );
}
