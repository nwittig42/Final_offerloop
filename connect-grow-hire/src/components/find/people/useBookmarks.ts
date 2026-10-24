// Bookmarks on the Find page are just Contacts membership. Bookmarking a
// person writes them into Contacts at the "Saved" stage (free, no email
// written); un-bookmarking removes them, but only while they've never been
// emailed. Recommended people use the deterministic doc id the backend's
// ranker outreach also writes to, so a later Contact fills the same row.
import { createElement, useCallback, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { ToastAction } from "@/components/ui/toast";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { doc, setDoc } from "firebase/firestore";
import { db } from "@/lib/firebase";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { firebaseApi, type Contact } from "@/services/firebaseApi";
import { toast } from "@/hooks/use-toast";
import { CONTACTS_KEY, normalizeLinkedIn, rankerContactId } from "@/components/contacts/contactsModel";
import type { Person } from "./peopleModel";

export type ContactDoc = Contact & Record<string, any>;

export function neverEmailed(c: ContactDoc): boolean {
  return !c.emailBody && !c.gmailDraftId && !c.gmailThreadId && !c.emailSent && !c.inOutbox;
}

export function useBookmarks() {
  const { user } = useFirebaseAuth();
  const uid = user?.uid;
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const key = [...CONTACTS_KEY, uid];

  // Same query (and cache) the Contacts page reads.
  const contactsQuery = useQuery({
    queryKey: key,
    queryFn: () => firebaseApi.getContacts(uid!),
    enabled: !!uid,
  });

  // Indexes for "is this person already in Contacts?"
  const index = useMemo(() => {
    const byId = new Map<string, ContactDoc>();
    const byLinkedIn = new Map<string, ContactDoc>();
    const byEmail = new Map<string, ContactDoc>();
    for (const c of (contactsQuery.data ?? []) as ContactDoc[]) {
      if (c.id) byId.set(c.id, c);
      const li = normalizeLinkedIn(c.linkedinUrl);
      if (li) byLinkedIn.set(li, c);
      if (c.email) byEmail.set(c.email.toLowerCase(), c);
    }
    return { byId, byLinkedIn, byEmail };
  }, [contactsQuery.data]);

  const findContact = useCallback((p: Person): ContactDoc | undefined => {
    if (p.source === "recommended") {
      const hit = index.byId.get(rankerContactId(p.id));
      if (hit) return hit;
    }
    const li = normalizeLinkedIn(p.linkedinUrl);
    if (li && index.byLinkedIn.has(li)) return index.byLinkedIn.get(li);
    if (p.email && index.byEmail.has(p.email.toLowerCase())) return index.byEmail.get(p.email.toLowerCase());
    return undefined;
  }, [index]);

  // Filled only for people saved and not yet emailed. Once Contact drafts
  // to someone the card shows "Drafted" instead.
  const isBookmarked = useCallback((p: Person) => {
    const c = findContact(p);
    return !!c && neverEmailed(c);
  }, [findContact]);

  const setCache = (fn: (list: ContactDoc[]) => ContactDoc[]) =>
    queryClient.setQueryData<ContactDoc[]>(key, (old) => fn(old ?? []));

  const toggle = useCallback(async (p: Person) => {
    if (!uid || p.source === "sample") return;
    const existing = findContact(p);

    // Un-bookmark: only people never emailed; the rest live in Contacts now.
    if (existing?.id) {
      if (!neverEmailed(existing)) {
        toast({ title: `${p.firstName} is already in your Contacts`, description: "You've emailed them, so manage them from Contacts." });
        return;
      }
      setCache((list) => list.filter((c) => c.id !== existing.id));
      try {
        await firebaseApi.deleteContact(uid, existing.id);
      } catch (e) {
        queryClient.invalidateQueries({ queryKey: CONTACTS_KEY });
        toast({ title: "Couldn't remove bookmark", description: e instanceof Error ? e.message : "Try again.", variant: "destructive" });
      }
      return;
    }

    // Bookmark: write a Saved contact. No email, no credits.
    const now = new Date().toISOString();
    const docData: ContactDoc = {
      firstName: p.firstName,
      lastName: p.lastName,
      email: p.email || "",
      linkedinUrl: p.linkedinUrl || "",
      company: p.company || "",
      jobTitle: p.title || "",
      college: p.school || "",
      location: p.location || "",
      photoUrl: p.photoUrl || "",
      firstContactDate: now,
      lastContactDate: now,
      createdAt: now,
      status: "Saved",
      source: "find_bookmark",
      ...(p.source === "recommended" ? { rankerCandidateId: p.id, rankerDeckId: p.deckId || "" } : {}),
    };
    const id = p.source === "recommended" ? rankerContactId(p.id) : `bookmark_${Date.now()}`;
    setCache((list) => [{ ...docData, id }, ...list]);
    try {
      if (p.source === "recommended") {
        await setDoc(doc(db, "users", uid, "contacts", id), docData, { merge: true });
      } else {
        const realId = await firebaseApi.createContact(uid, docData);
        setCache((list) => list.map((c) => (c.id === id ? { ...c, id: realId } : c)));
      }
      toast({
        title: `Saved ${p.firstName || p.name}`,
        description: "Bookmarks live in Contacts under Saved.",
        action: createElement(ToastAction, { altText: "View saved contacts", onClick: () => navigate("/contacts?filter=saved") }, "View"),
      });
    } catch (e) {
      setCache((list) => list.filter((c) => c.id !== id));
      toast({ title: "Couldn't save", description: e instanceof Error ? e.message : "Try again.", variant: "destructive" });
    }
  }, [uid, findContact, queryClient, navigate]);

  const contacts = useMemo(() => (contactsQuery.data ?? []) as ContactDoc[], [contactsQuery.data]);
  return { isBookmarked, toggle, contactFor: findContact, contacts };
}
