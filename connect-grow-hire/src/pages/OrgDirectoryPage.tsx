// The whole alumni directory of an org the student belongs to (OFF-42).
// Every living alum, free to browse: name, class year, job, LinkedIn, and
// their email when we have it. Contact drafts an intro the same way Find
// does (which also saves them to Contacts). For alumni with no email yet,
// Contact is where finding their info will hook in.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Search } from "lucide-react";
import { AppSidebar } from "@/components/AppSidebar";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppHeader } from "@/components/AppHeader";
import { MainContentWrapper } from "@/components/MainContentWrapper";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { apiService, type DirectoryMember, type EmailTemplate } from "@/services/api";
import { toast } from "@/hooks/use-toast";
import { CONTACTS_KEY, THREADS_KEY } from "@/components/contacts/contactsModel";
import { APPLE_FONT, PersonCard, type ContactState } from "@/components/find/people/PersonCard";
import { PersonInfoDialog } from "@/components/find/people/PersonInfoDialog";
import { personFromDirectory, type Person } from "@/components/find/people/peopleModel";
import { orgAccent, orgMonogram } from "@/hooks/useOrgs";
import { orgLogo } from "@/lib/orgLogos";

const GRID = "grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(236px,1fr))]";
const PAGE = 48;

type Sort = "complete" | "recent" | "oldest" | "name";

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
  const [infoPerson, setInfoPerson] = useState<Person | null>(null);

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
    const email = person.email;
    if (!email) {
      // TODO(OFF-42): kick off the find-their-info pipeline here once it's
      // merged. Until then there's nothing to draft to.
      toast({ title: `We don't have ${person.firstName}'s contact info yet`, description: "Looking it up from here is coming soon." });
      return;
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
  }, [emailTemplate, queryClient]);

  const org = data?.org;
  const notMember = (error as Error | null)?.message === "not_a_member";

  return (
    <SidebarProvider>
      <div className="flex min-h-screen w-full bg-[#F5F5F7] font-sans text-ink">
        <AppSidebar />
        <MainContentWrapper>
          <AppHeader title="" />
          <div className="flex-1 overflow-y-auto" style={{ background: "#ECEFF6" }}>
            <div className="mx-auto max-w-[1320px] px-4 pb-16 pt-4 sm:px-10" style={{ fontFamily: APPLE_FONT }}>
              <Link to="/find" className="mt-4 inline-flex items-center gap-1 text-[13px] text-[#3A4E8C] hover:underline">
                <ArrowLeft className="h-3.5 w-3.5" /> Back to Find
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
                  {/* The org's own space: a header band in its color */}
                  <div
                    className="mt-3 flex items-center gap-4 rounded-[26px] px-5 py-6 text-white sm:px-8"
                    style={{ background: `linear-gradient(160deg, ${orgAccent(org)} 0%, #1A1A1A 140%)` }}
                  >
                    {org && (orgLogo(org.id) ? (
                      <span className="hidden h-[72px] w-[72px] shrink-0 items-center justify-center rounded-[18px] bg-white p-2 shadow-[0_4px_14px_rgba(0,0,0,0.25)] sm:flex" aria-hidden>
                        <img src={orgLogo(org.id)!} alt="" className="h-full w-full object-contain" />
                      </span>
                    ) : (
                      <span className="hidden h-14 w-14 shrink-0 items-center justify-center rounded-[16px] bg-white/15 text-[17px] font-bold tracking-wide ring-1 ring-white/25 sm:flex" aria-hidden>
                        {orgMonogram(org)}
                      </span>
                    ))}
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

                  <div className="mt-5 flex flex-wrap items-center gap-3">
                    <label className="flex h-10 min-w-0 flex-1 items-center gap-2 rounded-full bg-white px-4 shadow-[0_1px_2px_rgba(0,0,0,0.04)] sm:max-w-[420px]">
                      <Search className="h-4 w-4 shrink-0 text-[#86868B]" />
                      <input
                        value={text}
                        onChange={(e) => setText(e.target.value)}
                        placeholder="Search by name, company, role or year"
                        aria-label="Search the directory"
                        className="min-w-0 flex-1 bg-transparent text-[14px] text-[#1D1D1F] outline-none placeholder:text-[#86868B]"
                      />
                    </label>
                    <select
                      value={sort}
                      onChange={(e) => setSort(e.target.value as Sort)}
                      aria-label="Sort"
                      className="h-10 rounded-full bg-white px-4 text-[14px] text-[#1D1D1F] shadow-[0_1px_2px_rgba(0,0,0,0.04)] outline-none"
                    >
                      <option value="complete">Most complete first</option>
                      <option value="recent">Newest first</option>
                      <option value="oldest">Oldest first</option>
                      <option value="name">Last name A–Z</option>
                    </select>
                    {data && text && (
                      <span className="text-[13px] text-[#6E6E73]">{people.length.toLocaleString()} match{people.length === 1 ? "" : "es"}</span>
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
                        onInfo={() => setInfoPerson(person)}
                      />
                    ))}
                  </div>
                  {data && people.length === 0 && (
                    <p className="mt-8 text-center text-[14px] text-[#6E6E73]">No alumni match "{text}".</p>
                  )}
                  {shown < people.length && <div ref={sentinel} className="h-10" />}
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
    </SidebarProvider>
  );
}
