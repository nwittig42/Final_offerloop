// Contacts: My Network + Inbox + Meeting Prep in one page, laid out like the
// macOS Contacts app. List view = people on the left, the selected contact's
// card and conversation on the right. Spreadsheet view = everyone in a table.
// URL state: ?contact=<id> selects a person, ?view=table shows the table.
import { useEffect, useMemo, useState } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, LayoutList, Search, Table2, Trash2 } from "lucide-react";
import { AppSidebar } from "@/components/AppSidebar";
import { SidebarProvider } from "@/components/ui/sidebar";
import { AppHeader } from "@/components/AppHeader";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { firebaseApi } from "@/services/firebaseApi";
import { toast } from "@/hooks/use-toast";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { CONTACTS_KEY, THREADS_KEY, downloadCsv, formatDate, useContactsData, type ContactRow } from "@/components/contacts/contactsModel";
import { ContactList, type ListFilter, type SortDir } from "@/components/contacts/ContactList";
import { ContactDetail } from "@/components/contacts/ContactDetail";
import { ContactsSpreadsheet } from "@/components/contacts/ContactsSpreadsheet";
import { ShareContactsDialog } from "@/components/contacts/ShareContactsDialog";
import { ScoutTour } from "@/components/walkthrough/ScoutTour";
import { useContactsTour } from "@/components/walkthrough/useContactsTour";

const NEW_ID = "__new__";
const SORT_KEY = "ofl_contacts_sort";
const GROUP_KEY = "ofl_contacts_group";

function readPref(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}
function writePref(key: string, value: string) {
  try { localStorage.setItem(key, value); } catch { /* storage unavailable */ }
}

export default function ContactsPage() {
  const { user } = useFirebaseAuth();
  const queryClient = useQueryClient();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const { rows, isLoading, isError } = useContactsData();

  const view: "list" | "table" = searchParams.get("view") === "table" ? "table" : "list";
  const selectedId = searchParams.get("contact");

  const updateParams = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(searchParams);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null) next.delete(k); else next.set(k, v);
    }
    setSearchParams(next, { replace: true });
  };
  const select = (id: string | null) => updateParams({ contact: id });
  // Scout's first-email tour: review, connect Gmail, open the draft.
  const contactsTour = useContactsTour(user?.uid, { searchParams, updateParams });

  // ?q= pre-fills the search (Loop cards link here filtered by company).
  const [query, setQuery] = useState(() => searchParams.get("q") ?? "");
  const [sortDir, setSortDir] = useState<SortDir>(() => (readPref(SORT_KEY) === "oldest" ? "oldest" : "newest"));
  const [groupByCompany, setGroupByCompany] = useState(() => readPref(GROUP_KEY) === "1");
  const [shareTargets, setShareTargets] = useState<ContactRow[] | null>(null);
  const [tableSelection, setTableSelection] = useState<Set<string>>(new Set());
  const [confirmBulkDelete, setConfirmBulkDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // Saved filter: people kept but never emailed (bookmarks from Search, saves).
  const [listFilter, setListFilter] = useState<ListFilter>(() => (searchParams.get("filter") === "saved" ? "saved" : "all"));
  const isSaved = (r: ContactRow) => r.stage === "saved" && r.bookmarkedOnly;
  const savedCount = useMemo(() => rows.filter(isSaved).length, [rows]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    const base = listFilter === "saved" ? rows.filter(isSaved) : rows;
    const filtered = q
      ? base.filter((r) => `${r.name} ${r.company} ${r.role} ${r.school} ${r.email} ${r.location}`.toLowerCase().includes(q))
      : base;
    const mul = sortDir === "oldest" ? -1 : 1;
    return [...filtered].sort(
      (a, b) => ((Date.parse(b.createdAt) || 0) - (Date.parse(a.createdAt) || 0)) * mul || a.name.localeCompare(b.name),
    );
  }, [rows, query, sortDir, listFilter]);

  const selected = useMemo(() => rows.find((r) => r.id === selectedId) ?? null, [rows, selectedId]);

  // Deep-links from Find / Job Board ("see in inbox") carry the contact's
  // email in route state; resolve it to an id once the rows include it.
  useEffect(() => {
    const focusEmail = (location.state as { focusEmail?: string } | null)?.focusEmail?.toLowerCase();
    if (!focusEmail) return;
    queryClient.invalidateQueries({ queryKey: CONTACTS_KEY });
    queryClient.invalidateQueries({ queryKey: THREADS_KEY });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.key]);
  useEffect(() => {
    const focusEmail = (location.state as { focusEmail?: string } | null)?.focusEmail?.toLowerCase();
    if (!focusEmail || selectedId) return;
    const match = rows.find((r) => r.email.toLowerCase() === focusEmail);
    if (match) select(match.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, location.state]);

  // Desktop list view always shows someone: pick the first person when
  // nothing (or a since-deleted id) is selected.
  useEffect(() => {
    if (view !== "list" || isLoading || selectedId === NEW_ID) return;
    if (contactsTour.holdSelection) return;
    if ((location.state as { focusEmail?: string } | null)?.focusEmail && !selectedId) return;
    if (selectedId && rows.some((r) => r.id === selectedId)) return;
    if (selectedId && rows.length > 0) {
      // Unknown id (deleted, or a deep-link to someone not saved): fall back.
      select(visible[0]?.id ?? null);
      return;
    }
    if (!selectedId && visible.length > 0 && window.matchMedia("(min-width: 768px)").matches) select(visible[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, isLoading, rows, selectedId, contactsTour.holdSelection]);

  const handleExport = () => {
    downloadCsv(
      "offerloop-contacts.csv",
      ["Name", "Email", "Company", "Role", "School", "Location", "LinkedIn", "Stage", "Prep", "Added"],
      rows.map((r) => [
        r.name, r.email, r.company, r.role, r.school, r.location, r.linkedinUrl, r.stageLabel,
        r.prep && (r.prep.status === "completed" || r.prep.pdfUrl) ? "Ready" : "", formatDate(r.createdAt),
      ]),
    );
  };

  const handleShareList = () => {
    if (visible.length === 0) {
      toast({ title: "No contacts to share" });
      return;
    }
    setShareTargets(visible);
  };

  const runBulkDelete = async () => {
    if (!user?.uid) return;
    setDeleting(true);
    const ids = [...tableSelection];
    try {
      await Promise.all(ids.map((id) => firebaseApi.deleteContact(user.uid, id).catch(() => {})));
      await queryClient.invalidateQueries({ queryKey: CONTACTS_KEY });
      setTableSelection(new Set());
      toast({ title: `${ids.length} ${ids.length === 1 ? "contact" : "contacts"} deleted` });
    } finally {
      setDeleting(false);
      setConfirmBulkDelete(false);
    }
  };

  // Lives top-left in both views (above the list, or leading the spreadsheet
  // toolbar) so switching is always in the same spot.
  const viewToggle = (
    <div className="flex w-full items-center">
      {/* Apple segmented control: gray track, white thumb slides under the
          active view. */}
      <div className="relative grid h-8 w-full shrink-0 grid-cols-2 rounded-[9px] bg-[rgba(118,118,128,0.12)] p-[2px]" role="tablist" aria-label="View">
        <span
          aria-hidden
          className="absolute bottom-[2px] left-[2px] top-[2px] w-[calc(50%-2px)] rounded-[7px] bg-white shadow-[0_3px_8px_rgba(0,0,0,0.12),0_1px_1px_rgba(0,0,0,0.04)] transition-transform duration-200 ease-out"
          style={{ transform: `translateX(${view === "table" ? 100 : 0}%)` }}
        />
        {([["list", "List", LayoutList], ["table", "Spreadsheet", Table2]] as const).map(([id, label, Icon]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={view === id}
            onClick={() => updateParams({ view: id === "table" ? "table" : null })}
            className={`relative z-10 inline-flex items-center justify-center gap-1.5 rounded-[7px] px-3 text-[13px] font-medium transition-colors duration-200 ${
              view === id ? "cursor-default text-[#1d1d1f]" : "cursor-pointer text-[#6e6e73] hover:text-[#1d1d1f]"
            }`}
          >
            <Icon className="h-3.5 w-3.5 shrink-0" /> {label}
          </button>
        ))}
      </div>
    </div>
  );

  const listColumn = (
    <ContactList
      rows={visible}
      selectedId={selectedId}
      onSelect={select}
      query={query}
      onQueryChange={setQuery}
      sortDir={sortDir}
      onSortDirChange={(d) => { setSortDir(d); writePref(SORT_KEY, d); }}
      filter={listFilter}
      onFilterChange={setListFilter}
      savedCount={savedCount}
      groupByCompany={groupByCompany}
      onToggleGroup={() => setGroupByCompany((g) => { writePref(GROUP_KEY, g ? "0" : "1"); return !g; })}
      onAdd={() => select(NEW_ID)}
      onExport={handleExport}
      onShareList={handleShareList}
    />
  );

  const detailOpen = selectedId === NEW_ID || !!selected;

  return (
    <SidebarProvider>
      <div className="flex h-screen w-full bg-paper font-sans text-ink">
        <AppSidebar />
        <div className="flex min-w-0 flex-1 flex-col">
          <AppHeader title="Contacts" />
          <main className="flex min-h-0 flex-1 border-t border-line" data-tour="tour-contacts">
            {isLoading ? (
              <div className="flex flex-1 items-center justify-center text-[14px] text-ink-3">Loading contacts…</div>
            ) : isError ? (
              <div className="flex flex-1 items-center justify-center text-[14px] text-red-600">Couldn't load your contacts.</div>
            ) : view === "table" ? (
              <div className="flex min-w-0 flex-1 flex-col">
                <div className="flex items-center justify-between gap-3 border-b border-line px-6 py-3">
                  <div className="flex min-w-0 items-center gap-3">
                    <div className="w-[260px] shrink-0">{viewToggle}</div>
                    <label className="flex h-8 w-72 items-center gap-1.5 rounded-[10px] bg-[rgba(118,118,128,0.12)] px-2.5 focus-within:ring-2 focus-within:ring-[#0071e3]/40">
                      <Search className="h-4 w-4 shrink-0 text-[#8e8e93]" strokeWidth={2.25} />
                      <input
                        value={query}
                        onChange={(e) => setQuery(e.target.value)}
                        placeholder="Search"
                        aria-label="Search contacts"
                        className="min-w-0 flex-1 bg-transparent text-[14px] text-[#1d1d1f] outline-none placeholder:text-[#8e8e93]"
                      />
                    </label>
                    <span className="hidden text-[12.5px] text-ink-3 lg:inline">{visible.length} {visible.length === 1 ? "contact" : "contacts"}</span>
                  </div>
                  <div className="flex items-center gap-2">
                    {tableSelection.size > 0 && (
                      <>
                        <button type="button" onClick={() => setShareTargets(rows.filter((r) => tableSelection.has(r.id)))}
                          className="h-9 rounded-full border border-[#D4D4D4] bg-white px-4 text-[13px] font-semibold text-ink-2 hover:border-slate-400 hover:bg-slate-50">
                          Share {tableSelection.size}
                        </button>
                        <button type="button" onClick={() => setConfirmBulkDelete(true)}
                          className="inline-flex h-9 items-center gap-1.5 rounded-full border border-red-200 bg-white px-4 text-[13px] font-semibold text-red-600 hover:bg-red-50">
                          <Trash2 className="h-3.5 w-3.5" /> Delete {tableSelection.size}
                        </button>
                      </>
                    )}
                    <button type="button" onClick={handleExport} className="h-9 rounded-full border border-[#D4D4D4] bg-white px-4 text-[13px] font-semibold text-ink-2 hover:border-slate-400 hover:bg-slate-50">
                      Export CSV
                    </button>
                  </div>
                </div>
                <div className="min-h-0 flex-1">
                  <ContactsSpreadsheet
                    rows={visible}
                    selected={tableSelection}
                    onSelectionChange={setTableSelection}
                    onOpen={(id) => updateParams({ view: null, contact: id })}
                  />
                </div>
              </div>
            ) : (
              <>
                {/* Narrow screens show one pane at a time. */}
                <aside className={`${detailOpen ? "hidden md:flex" : "flex"} w-full shrink-0 flex-col border-r border-line bg-white md:w-[320px]`}>
                  <div className="px-3 pb-1 pt-3">{viewToggle}</div>
                  <div className="flex min-h-0 flex-1 flex-col">{listColumn}</div>
                </aside>
                <section className={`${detailOpen ? "flex" : "hidden md:flex"} min-w-0 flex-1 flex-col overflow-y-auto bg-white`}>
                  {detailOpen && (
                    <button type="button" onClick={() => select(null)}
                      className="flex items-center gap-1 px-4 pt-3 text-[13px] text-[var(--accent,#4A60A8)] md:hidden">
                      <ChevronLeft className="h-4 w-4" /> Contacts
                    </button>
                  )}
                  {selectedId === NEW_ID ? (
                    <ContactDetail
                      key="new"
                      contact={null}
                      onCreated={(id) => select(id)}
                      onCancelNew={() => select(null)}
                      onDeleted={() => select(null)}
                      onShare={(c) => setShareTargets([c])}
                    />
                  ) : selected ? (
                    <ContactDetail
                      contact={selected}
                      onCreated={(id) => select(id)}
                      onCancelNew={() => select(null)}
                      onDeleted={() => select(null)}
                      onShare={(c) => setShareTargets([c])}
                    />
                  ) : (
                    <div className="flex flex-1 items-center justify-center text-[14px] text-ink-3">
                      {rows.length === 0 ? "Your contacts will show up here." : "Select a contact"}
                    </div>
                  )}
                </section>
              </>
            )}
          </main>
        </div>
      </div>

      <ShareContactsDialog contacts={shareTargets} onClose={() => setShareTargets(null)} />
      {contactsTour.view && <ScoutTour {...contactsTour.view} />}

      <AlertDialog open={confirmBulkDelete} onOpenChange={(o) => !deleting && setConfirmBulkDelete(o)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {tableSelection.size} {tableSelection.size === 1 ? "contact" : "contacts"}?</AlertDialogTitle>
            <AlertDialogDescription>This removes them from your network. It can't be undone.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => { e.preventDefault(); runBulkDelete(); }}
              disabled={deleting}
              className="bg-red-600 hover:bg-red-700"
            >
              {deleting ? "Deleting…" : "Delete"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </SidebarProvider>
  );
}
