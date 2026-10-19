// Right side of the Contacts page: the contact card (macOS Contacts style)
// with the conversation underneath. Also renders the blank card used by
// "Add person" — same form, Save creates instead of updates.
import { useEffect, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Linkedin, Mail, Share2, Trash2 } from "lucide-react";
import { CompanyLogo } from "@/components/CompanyLogo";
import { useFirebaseAuth } from "@/contexts/FirebaseAuthContext";
import { firebaseApi } from "@/services/firebaseApi";
import { toast } from "@/hooks/use-toast";
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { CONTACTS_KEY, formatDate, linkedInHref, type ContactRow } from "./contactsModel";
import { ActionButton } from "./ActionButton";
import { MeetingPrepButton } from "./MeetingPrepButton";
import { ContactConversation } from "./ContactConversation";
import { StagePill } from "./StagePill";
import { StageTracker } from "./StageTracker";

interface Draft {
  firstName: string;
  lastName: string;
  role: string;
  company: string;
  email: string;
  linkedinUrl: string;
  school: string;
  location: string;
  notes: string;
}

const EMPTY: Draft = { firstName: "", lastName: "", role: "", company: "", email: "", linkedinUrl: "", school: "", location: "", notes: "" };

function draftFrom(c: ContactRow): Draft {
  return {
    firstName: c.firstName, lastName: c.lastName, role: c.role, company: c.company, email: c.email,
    linkedinUrl: c.linkedinUrl, school: c.school, location: c.location, notes: c.notes,
  };
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid grid-cols-[110px_1fr] items-baseline gap-4 border-b border-line py-2.5 last:border-b-0">
      <span className="text-right text-[13px] text-ink-3">{label}</span>
      <div className="min-w-0 text-[14px] text-ink">{children}</div>
    </div>
  );
}

const INPUT = "w-full rounded-md border border-line bg-white px-2.5 py-1.5 text-[14px] text-ink outline-none focus:border-[var(--accent,#4A60A8)]";

interface ContactDetailProps {
  // null = blank "new person" card
  contact: ContactRow | null;
  onCreated: (id: string) => void;
  onCancelNew: () => void;
  onDeleted: (id: string) => void;
  onShare: (contact: ContactRow) => void;
}

export function ContactDetail({ contact, onCreated, onCancelNew, onDeleted, onShare }: ContactDetailProps) {
  const { user } = useFirebaseAuth();
  const queryClient = useQueryClient();
  const isNew = contact === null;
  const [editing, setEditing] = useState(isNew);
  const [draft, setDraft] = useState<Draft>(contact ? draftFrom(contact) : EMPTY);
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  // Reset when switching contacts (keyed on id, not the object, so a 30s
  // background refetch doesn't wipe an edit in progress).
  useEffect(() => {
    setEditing(contact === null);
    setDraft(contact ? draftFrom(contact) : EMPTY);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [contact?.id ?? "new"]);

  const set = (k: keyof Draft) => (e: { target: { value: string } }) => setDraft((d) => ({ ...d, [k]: e.target.value }));

  const save = async () => {
    if (!user?.uid) return;
    if (!draft.firstName.trim() && !draft.lastName.trim()) {
      toast({ title: "Add a name first", variant: "destructive" });
      return;
    }
    const fields = {
      firstName: draft.firstName.trim(),
      lastName: draft.lastName.trim(),
      jobTitle: draft.role.trim(),
      company: draft.company.trim(),
      email: draft.email.trim(),
      linkedinUrl: draft.linkedinUrl.trim(),
      college: draft.school.trim(),
      location: draft.location.trim(),
      notes: draft.notes,
    };
    setSaving(true);
    try {
      if (isNew) {
        const now = new Date().toISOString();
        const id = await firebaseApi.createContact(user.uid, {
          ...fields, firstContactDate: now, lastContactDate: now, status: "manual",
        });
        await queryClient.invalidateQueries({ queryKey: CONTACTS_KEY });
        toast({ title: "Contact added", description: `${fields.firstName} ${fields.lastName}`.trim() });
        onCreated(id);
      } else {
        await firebaseApi.updateContact(user.uid, contact.id, fields);
        await queryClient.invalidateQueries({ queryKey: CONTACTS_KEY });
        setEditing(false);
      }
    } catch (e) {
      toast({ title: "Save failed", description: e instanceof Error ? e.message : "Try again.", variant: "destructive" });
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (!user?.uid || !contact) return;
    try {
      await firebaseApi.deleteContact(user.uid, contact.id);
      await queryClient.invalidateQueries({ queryKey: CONTACTS_KEY });
      toast({ title: "Contact deleted" });
      onDeleted(contact.id);
    } catch (e) {
      toast({ title: "Delete failed", description: e instanceof Error ? e.message : "Try again.", variant: "destructive" });
    }
  };

  const name = isNew ? `${draft.firstName} ${draft.lastName}`.trim() || "New contact" : contact.name;
  const company = editing ? draft.company : contact?.company || "";
  const linkedIn = linkedInHref(contact?.linkedinUrl);

  return (
    <div className="mx-auto max-w-[720px] px-8 pb-16 pt-8">
      {/* Header: company logo as the avatar, name, role · company */}
      <div className="flex items-center gap-5">
        <CompanyLogo company={company || name} size={72} rounded={18} className="shadow-sm" />
        <div className="min-w-0">
          <h2 className="truncate text-[26px] font-semibold leading-tight text-ink">{name}</h2>
          {!isNew && (contact.role || contact.company) && (
            <p className="mt-1 truncate text-[14px] text-ink-2">
              {[contact.role, contact.company].filter(Boolean).join(" · ")}
            </p>
          )}
          {!isNew && <div className="mt-2"><StagePill contact={contact} size="md" /></div>}
        </div>
      </div>

      {!isNew && !editing && (
        <div className="mt-6 flex flex-wrap items-center gap-2" data-tour="tour-contact-actions">
          <ActionButton
            icon={<Mail className="h-4 w-4" />}
            label="Email"
            tone="primary"
            href={contact.email ? `https://mail.google.com/mail/?view=cm&to=${encodeURIComponent(contact.email)}` : null}
            disabled={!contact.email}
            title={contact.email || "No email address"}
          />
          <ActionButton
            icon={<Linkedin className="h-4 w-4" />}
            label="LinkedIn"
            href={linkedIn}
            disabled={!linkedIn}
            title={linkedIn ? undefined : "No LinkedIn URL"}
          />
          <MeetingPrepButton contact={contact} />
          <ActionButton icon={<Share2 className="h-4 w-4" />} label="Share" onClick={() => onShare(contact)} />
        </div>
      )}

      {!isNew && !editing && <StageTracker contact={contact} />}

      {/* Fields */}
      <div className="mt-7 border-t border-line">
        {editing ? (
          <>
            <Field label="first name"><input autoFocus={isNew} className={INPUT} value={draft.firstName} onChange={set("firstName")} /></Field>
            <Field label="last name"><input className={INPUT} value={draft.lastName} onChange={set("lastName")} /></Field>
            <Field label="role"><input className={INPUT} value={draft.role} onChange={set("role")} placeholder="e.g. Analyst" /></Field>
            <Field label="company"><input className={INPUT} value={draft.company} onChange={set("company")} /></Field>
            <Field label="email"><input className={INPUT} type="email" value={draft.email} onChange={set("email")} /></Field>
            <Field label="LinkedIn"><input className={INPUT} value={draft.linkedinUrl} onChange={set("linkedinUrl")} placeholder="linkedin.com/in/…" /></Field>
            <Field label="school"><input className={INPUT} value={draft.school} onChange={set("school")} /></Field>
            <Field label="location"><input className={INPUT} value={draft.location} onChange={set("location")} /></Field>
            <Field label="note"><textarea className={`${INPUT} min-h-[72px] resize-y`} value={draft.notes} onChange={set("notes")} /></Field>
          </>
        ) : (
          <>
            <Field label="email">
              {contact!.email ? <span className="break-all">{contact!.email}</span> : <span className="text-ink-3">—</span>}
            </Field>
            <Field label="LinkedIn">
              {linkedIn ? (
                <a href={linkedIn} target="_blank" rel="noopener noreferrer" className="break-all text-[var(--accent,#4A60A8)] hover:underline">
                  {contact!.linkedinUrl.replace(/^https?:\/\/(www\.)?/, "")}
                </a>
              ) : <span className="text-ink-3">—</span>}
            </Field>
            {contact!.school && <Field label="school">{contact!.school}</Field>}
            {contact!.location && <Field label="location">{contact!.location}</Field>}
            <Field label="note">
              {contact!.notes ? <span className="whitespace-pre-wrap">{contact!.notes}</span> : <span className="text-ink-3">—</span>}
            </Field>
            {contact!.createdAt && <Field label="added">{formatDate(contact!.createdAt)}</Field>}
            {contact!.source === "agent" && <Field label="source">Found by a Loop</Field>}
          </>
        )}
      </div>

      {/* Edit / Save bar */}
      <div className="mt-4 flex items-center justify-between">
        {editing && !isNew ? (
          <button type="button" onClick={() => setConfirmDelete(true)} className="inline-flex items-center gap-1.5 text-[13px] text-red-600 hover:underline">
            <Trash2 className="h-3.5 w-3.5" /> Delete contact
          </button>
        ) : <span />}
        <div className="flex gap-2">
          {editing ? (
            <>
              <button
                type="button"
                onClick={() => (isNew ? onCancelNew() : (setEditing(false), setDraft(draftFrom(contact!))))}
                className="h-8 rounded-md border border-line bg-white px-4 text-[13px] text-ink hover:bg-paper-2"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={save}
                disabled={saving}
                className="h-8 rounded-md bg-[var(--accent,#4A60A8)] px-4 text-[13px] font-medium text-white disabled:opacity-60"
              >
                {saving ? "Saving…" : "Done"}
              </button>
            </>
          ) : (
            <button type="button" onClick={() => setEditing(true)} className="h-8 rounded-md border border-line bg-white px-4 text-[13px] text-ink hover:bg-paper-2">
              Edit
            </button>
          )}
        </div>
      </div>

      {!isNew && !editing && <ContactConversation contact={contact} />}

      <AlertDialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {name}?</AlertDialogTitle>
            <AlertDialogDescription>This removes the contact from your network. It can't be undone.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={remove} className="bg-red-600 hover:bg-red-700">Delete</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
