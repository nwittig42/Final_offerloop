# First-email pop-up on Find + person profile pop-up

Date: 2026-10-25 · Owner: Nick · Linear: OFF-55 follow-up

## Why

Nick kept the original onboarding (five intro slides → resume/LinkedIn → optional organizations step, landing on Find) after rejecting onboarding v2's intro and confirm screens. Archit's first-email walkthrough (`/get-started`: pick a person → editable email → connect Gmail → send) is still good, but it should happen **on the home page, not on its own page**. A new user lands on Find and the walkthrough opens as a pop-up over it, already filled with people to talk to. Nothing should make them wait.

Separately, person cards on Find are small, and the only way to see more is a narrow "Info" dialog. Nick wants clicking a person to open a large profile pop-up in the style of Archit's walkthrough profile view (big portrait, serif name, "why them", large buttons).

## Goals

1. **Walkthrough as a pop-up on Find.** Right after onboarding finishes, a new user lands on `/find` and the walkthrough opens in a large modal over it. It starts on the people step with 3–4 people already loaded.
2. **People come straight from our warehouse.** The walkthrough's people use the existing no-vendor fast path (`GET /api/ranker/candidates?prefetch=0&dig=0`, already used by `walkthroughServices.ts`). No Coresignal dig, no FullEnrich prefetch, no paid lookups to show the list.
3. **Every person card on Find opens a profile pop-up.** Clicking anywhere on a card opens it. Clicking a button *on* the card (bookmark, dismiss, LinkedIn, Contact / See email, etc.) only runs that button's action and does not open the pop-up.
4. **The profile pop-up shows everything we have** for the person, and has big action buttons, including **Meeting prep**, which also saves the person to Contacts.

## Non-goals

- Storing or showing work history, degrees/years, skills or an "about" section. The warehouse (`firm_employees`) doesn't store them (see "Data we have" below). Not in this project.
- Paid LinkedIn lookups (Apify) when a pop-up opens.
- Bringing back onboarding v2's intro screen, confirm screen, or anything else in the onboarding steps. Onboarding stays as it is on `nickwittig/offerloop-web-redesign` (commit c63ac2ff).
- Showing the walkthrough to existing users. It's only for accounts that finish onboarding after this ships.
- Turning on the free first email. It stays controlled by `FIRST_OUTREACH_FREE_SINCE` (currently unset, so off). The walkthrough already shows the real cost.

## Anti-goals (would be a failure even if it "works")

- The pop-up shows a spinner for several seconds or calls a paid vendor before anyone has clicked anything.
- Sections that are empty or contain filler ("Experience" with nothing under it, "Professional at X").
- Card buttons that open the pop-up as well as doing their own job.
- The walkthrough reappearing every time Find loads after the user has closed or finished it.

## Data we have per person (decides what the profile shows)

From the warehouse audit (`backend/app/services/firm_cache/schema.py:544-582`; the deck response is built in `person_ranker.py:977-1010`):

- **Available to the card today:** name, title, company, city/state, first school, LinkedIn URL, photo, ranker `reasons` and `briefing`, match score, org badge and grad year (org directory cards).
- **Stored but not sent to the UI:** every school after the first (`schools_display`).
- **Not stored at all:** past jobs, degrees/years, skills, about.

**Decision (Claude's call; Nick delegated it):** show only what we have. Add **all schools** to the deck response and to `Person` (a small, additive backend change: include `schools_display` as `schools` in `_to_response_dict`, and map it in `personFromRanker`). Everything else uses data that already reaches the frontend.

## The profile pop-up

**Shape (Claude's call; Nick saw mockups A and B in the browser companion and delegated the choice):** A, a large centered modal styled like Archit's walkthrough profile view (`components/walkthrough/MatchCards.tsx`): Offerloop palette, serif name, portrait. It replaces `PersonInfoDialog` everywhere `PersonInfoDialog` is used (`FindPage.tsx`, `OrgDirectoryPage.tsx`).

Content, top to bottom. Each section is shown only when it has real data:

- Portrait: the photo, or a monogram placeholder like the walkthrough's.
- Company eyebrow, name, "Title at Company".
- Schools (all of them) · location.
- Chips: org badge, "Went to <your school>" when it matches (the existing `relationFor`), and match score if present.
- **Why them:** ranker reasons, then the briefing, using the same de-duplication the walkthrough uses (`matchReasons`).

Big buttons (52px tall, full-width row; the primary one is wider):

1. **Primary:** the card's existing contact action for this person's state: "See email & draft" / "Contact" / "Open in Contacts" / disabled "No email found". It calls exactly the same handler as the card's button, so credits and refunds behave the same.
2. **Meeting prep:** saves the person to Contacts (the existing bookmark/save path in `useBookmarks.ts`, so they show up in the Contacts tab), then starts a meeting prep for their LinkedIn URL using the same flow and confirmation as `components/contacts/MeetingPrepButton.tsx` (15 credits, confirm dialog). Preps are matched to contacts by LinkedIn URL, so the prep then appears on that contact in Contacts. Hidden when the person has no LinkedIn URL (synthetic-ID warehouse people).
3. **Save** (bookmark toggle; same as the card).
4. **LinkedIn ↗** (only if there's a URL).

Opening the pop-up costs nothing and makes no network calls beyond what the card already has.

**Card click behavior** (`PersonCard.tsx`): the whole `<article>` gets an `onOpen` handler (keyboard-accessible: role/tabindex, Enter/Space). Every interactive child (bookmark, dismiss, LinkedIn link, Info, Contact/See email, links) stops propagation. The small "Info" button can be removed, since the card itself now does that. **Builder's call.** Applies to every `PersonCard` that receives `onOpen`: Find (recommended + search results), the org directory, and `FindPeopleWidget` if it renders `PersonCard`. Sample/example cards don't open.

## The walkthrough pop-up on Find

- **Trigger:** when the old onboarding completes (`OnboardingFlow.tsx` → `completeOnboarding`), also write `walkthrough: { status: "pending", createdAt }` on `users/{uid}`, the same shape onboarding v2 wrote (`services/walkthrough.ts` → `normalizeWalkthrough`). Preview mode (`/dev/onboarding-preview`) writes nothing.
- **Where:** `FindPage` opens a large modal when the user's walkthrough is incomplete (`walkthroughIncomplete`), not dismissed, and the user hasn't closed it in this session. Onboarding still routes to Find.
- **What's in it:** the existing walkthrough (`pages/GetStartedPage.tsx`) moved into a reusable body component rendered in the modal. The `/get-started` route keeps working by rendering the same body full-page, and `/dev/get-started-preview` keeps working. Steps, copy, error states, Gmail connect and send are unchanged.
- **Closing:** a close (✕) / "Finish later" closes the modal for this session and leaves the walkthrough incomplete. The existing `FirstEmailReminder` banner on Find then offers to reopen it, and reopens the **modal** rather than navigating to `/get-started`. "Skip for now" in the walkthrough marks it dismissed so it doesn't auto-open again. Finishing (email sent) marks it completed, as today.
- **Gmail OAuth round-trip:** the walkthrough currently returns from Google to `/get-started`. When started from the modal it must return to `/find?walkthrough=1`, and Find reopens the modal on the draft step. The backend already accepts a sanitized relative `return_to` (`gmail_oauth.py:19-46`).
- **Speed:** the people list is requested as soon as Find mounts for a pending walkthrough, through the existing fast path. If the saved per-user deck exists it's one Firestore read. If the warehouse returns nothing (`shelf_empty`), use the walkthrough's existing empty state ("no recommendations" with a way to Find). Never fall back to a paid dig.

## Constraints

- Branch `nickwittig/offerloop-web-redesign` in the main checkout; commit locally, never push.
- Local dev runs against **production** Firebase. Test with `/dev/*` previews or the e2e emulator harness (`scripts/e2e-emulator`) before using a real account.
- Don't start or restart the local backend; another session runs it. Say so when backend changes need a restart.
- Reuse existing handlers for anything that charges credits (contact/reveal, outreach, meeting prep). No new billing paths.
- Keep the Offerloop look already used by the walkthrough (`onboardingTheme.ts` tokens, `MatchCards.tsx` styles).

## Decisions after review (Claude's calls; Nick delegated these)

1. **No paid lookups behind the modal either.** While the walkthrough modal is open, or auto-opening, FindPage's own recommendation requests send `dig=0&prefetch=0` (the same `allowPaidLookups: false` path as the walkthrough). Normal behavior resumes once the modal is closed or finished. Today `FindPage.tsx:177-178` triggers the Coresignal dig on the web by default (`ranker.py:271`).
2. **Skip and close are recorded on the user doc** (`users/{uid}.walkthrough`), not in browser storage:
   - **"Skip for now"** sets `skippedAt`. The modal never auto-opens again. The reminder banner still shows (unless its own ✕ was used) and reopens the modal on click.
   - **✕ / "Finish later"** sets `closedAt`. The modal auto-opens only while `closedAt` and `skippedAt` are both unset. After the first close, only the banner reopens it. This works across reloads, tabs and sign-ins.
   - **The banner's ✕** (`reminderDismissedAt`) hides the banner only, as today. If skipped, closed and banner-dismissed are all set, the walkthrough is reachable only at `/get-started`.
   - `normalizeWalkthrough` keeps the new fields. They are UX state only, like the rest of the walkthrough doc.
3. **Meeting prep saves only after the user confirms "Run prep" and has enough credits.** Order: click → the existing confirm dialog → credit check → *ensure saved* (an idempotent "save if not already saved", never the bookmark toggle, which would un-save a saved person) → start the prep. If the user cancels or lacks credits, nothing is saved. If the prep itself fails after starting, the contact stays saved; they chose this person, and the prep can be retried from Contacts.
4. **After the email is sent from the modal, the user stays on Find.** The modal shows a success state ("Sent to <name>") with "Open in Contacts" (`/contacts?contact=…`) and "Done" (closes the modal). The shared walkthrough body takes exit callbacks (`onFinished`, `onFinishLater`, `onSkip`). The `/get-started` page wires them to today's navigation, and the modal wires them to close/success behavior.
5. **All schools:** the warehouse reader (`firm_cache/reader.py` `firm_employee_doc_to_app_contact`) also exposes the full `schools_display` list, `_to_response_dict` sends it as `schools`, and `personFromRanker` maps it. Saved decks built before this have no list and fall back to the single school. The profile also shows `joinedYear` when present.

## Left to the builder

- Component names and file layout (e.g. `PersonProfileDialog`, `WalkthroughDialog`).
- Whether to delete `PersonInfoDialog` or turn it into the new component.
- Modal sizing and the mobile layout (stack the portrait above the details below ~640px).
- Exact test split; extend the existing walkthrough/MatchCards tests and add tests for card click vs. button click and the Meeting prep save-then-prep order.
