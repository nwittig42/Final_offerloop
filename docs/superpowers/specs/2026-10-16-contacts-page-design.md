# Contacts page (OFF-32)

Merges Inbox (`/outbox`), My Network (`/my-network`) and Meeting Prep (`/coffee-chat-prep`) into one
`/contacts` page laid out like macOS Contacts. Backend unchanged.

## Data
- Contacts: `firebaseApi.getContacts(uid)` (Firestore `users/{uid}/contacts`).
- Conversations: `apiService.getOutboxThreads()`. `OutboxThread.id` is the contact doc id, so threads
  join to contacts by id. Contacts with no thread are stage "Saved".
- Preps: `apiService.getAllCoffeeChatPreps()`, joined to contacts by normalized LinkedIn URL
  (lowercase, no protocol, no `www.`, no trailing slash). No contact id is stored on a prep.

## List view
- Left column: search (name, company, role, school, email), Newest/Oldest, Group by company.
  Rows show company logo, name, "role · company", stage label and an unread-reply dot.
  Bottom toolbar: Add person, Export CSV, Share list (shares the current filtered list).
- Right column: company logo avatar, name, "role · company"; action buttons Email, LinkedIn,
  Meeting prep, Share; detail fields (email, LinkedIn, school, location, notes, added, source)
  with an Edit mode; then the conversation: stage dropdown, thread messages, draft reply with
  Generate / Send, View in Gmail, and a menu with Archive / Unarchive. No thread yet shows a
  Draft email button (cold draft via `/emails/generate-and-draft`).

## Spreadsheet view
List | Spreadsheet toggle (`?view=table`). Full-width table: name, company, role, school, email,
stage, prep, added. Checkbox selection with bulk delete. Clicking a row opens it in List view.

## Meeting prep
Button "Meeting prep · 30 credits" → confirm → `createCoffeeChatPrep({linkedinUrl})` → poll
`getCoffeeChatPrepStatus` → "Prep ready ✓", which opens the PDF. Without a LinkedIn URL the
button is disabled. "Past preps" dialog lists all preps (open / delete), including people who
are not contacts.

## Navigation
Sidebar: one "Contacts" item replaces My Network, Inbox, Meeting Prep. `/outbox`, `/tracker`,
`/my-network/*`, `/contact-directory`, `/coffee-chat-prep`, `/coffee-chat-library` redirect to
`/contacts`, preserving `?contact=<id>`. `focusEmail` route state is resolved to a contact by email.
The three tour steps collapse into one Contacts step. Old pages are deleted.

## Out of scope
Hiring managers (separate `recruiters` collection). Scout backend route maps.
