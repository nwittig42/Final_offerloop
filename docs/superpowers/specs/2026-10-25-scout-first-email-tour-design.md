# Scout-guided first-email tour on the real pages

Date: 2026-10-25 · Owner: Nick · Linear: OFF-60 (follows OFF-57)

## Why

OFF-57 put Archit's first-email walkthrough in a big pop-up over Find. Nick tried it and wants the walkthrough **built into the page**: a guided tour over the real Find and Contacts pages, using the real Find person cards (the ones on the home page, not the walkthrough's tall portrait cards), with the **Scout yeti** coaching from the side. He also found that the user's own LinkedIn profile was recommended back to them: he uploaded Giovanna Hughes's LinkedIn and she was suggested as someone to email.

The order matters to Nick. The email gets written **before** we ask for Gmail. The user sees their draft first, then connects Gmail (Google's permission screen with the checkboxes), then opens the draft in Gmail. Nick and Claude agreed on this order because it shows the user what they're getting before asking for permissions.

## The tour, step by step

The tour runs once for new accounts: those with `users/{uid}.walkthrough` pending from onboarding (OFF-57 Task 2), using the same `shouldAutoOpen` rules (it stops after `closedAt`/`skippedAt`). Each step dims the page except the spotlighted real element, and shows the Scout yeti beside it with a speech bubble.

1. **Pick someone (Find).** The spotlight covers the first 3–4 recommended person cards in the real feed. Bubble: "Here are a few people worth emailing first. Click one to see why they fit." Clicking a card opens the existing profile pop-up (OFF-57).
2. **Write the email (profile pop-up).** The spotlight is on the pop-up's primary button ("See email & draft"). Bubble: "Hit this and I'll find their email and write a first draft for you." Clicking runs the existing `contact(p)` (reveal, then outreach). During the tour, the reveal uses the one-time free first outreach when the backend says the user is eligible (`useFirstOutreach`). Otherwise it costs the normal 8 credits, refunded when no email is found. If no email is found, the bubble says so ("No email for {first}. Nothing was charged. Pick someone else.") and the tour returns to step 1.
3. **See the draft (Contacts).** When the draft is written, the user is taken to `/contacts?contact=<id>`, with that contact selected and the draft showing in the conversation. The spotlight is on the conversation/draft. Bubble: "Here's your email to {first}. Read it over. You can edit it here." Button: "Looks good".
4. **Connect Gmail (Contacts).** Bubble: "Connect Gmail so this goes out from your own inbox. Google will ask you to tick a few boxes. Tick all of them." Button: "Connect Gmail". This starts the existing Gmail OAuth (`apiService.startGmailOAuth`) with `return_to=/contacts?contact=<id>&tour=gmail`.
   - If the user already has Gmail connected with send/draft scopes, skip this step.
   - **On return** (`connected=gmail`), check `gmailStatus` scopes (the same `canSendWith` check the walkthrough uses). If a permission is missing (`gmail_error=scopes_declined`, or scopes lack compose/send), the bubble explains it ("Google didn't give us permission to create drafts. Connect again and tick every box.") and offers "Try again".
   - **On success,** the saved draft is copied into the user's Gmail drafts (new backend route below), so the contact's Gmail button opens it.
5. **Open it in Gmail (Contacts).** The spotlight is on the conversation's **Gmail** button, which now opens the real Gmail draft. Bubble: "It's in your Gmail drafts. Open it, give it a last look and hit send." Clicking it opens Gmail in a new tab and completes the tour (`status: "completed"`). A short "You're all set" from Scout closes the overlay.

**Every step** has "Skip tour", which sets `skippedAt`, and a close ✕, which sets `closedAt`. Both end the auto-run. The existing "Finish your first email" banner on Find resumes the tour at its saved step. Progress (step, chosen person/contact id) lives on `users/{uid}.walkthrough`, so a refresh, or the Google round-trip, resumes in place.

## Backend changes

1. **Push a saved draft to Gmail:** new `POST /api/contacts/<contact_id>/gmail-draft` in `backend/app/routes/contacts.py`, next to `send-first-email`.
   - Reads the contact's saved `emailSubject`, `emailBody` and `draftToEmail`.
   - Calls `gmail_client.create_gmail_draft_for_user(...)` the same way ranker outreach does, including the resume attachment.
   - Writes `gmailDraftId`, `gmailDraftUrl` and `pipelineStage: "draft_created"`.
   - It's idempotent: if `gmailDraftId` already exists and the draft still exists, it returns that one instead of making a second.
   - Errors: `gmail_not_connected` or missing scope returns 409 with a reason; a missing saved draft returns 404 or 400.
   - Free, with no credits.
2. **Never recommend the user to themselves:**
   - In `services/person_ranker.py`, exclude the candidate whose id equals the canonical LinkedIn slug of the user's own `profile.linkedinUrl` or `linkedinUrl` (canonicalized the same way `_candidate_id` does).
   - Also add the user's (first, last, current company) to the excluded name keys.
   - Apply the same filter to decks served from `people_deck_cache.serve` (cached decks predate the fix).

## Frontend changes

- **A tour overlay component** (builder names it; e.g. `components/walkthrough/ScoutTour.tsx`):
  - A spotlight cutout around a target found by `data-tour` selector, following it on scroll and resize.
  - The Scout yeti image (`assets/scouts/scout-yeti-full.png`) beside the bubble, placed on whichever side has room, and moved to the bottom on narrow screens.
  - The bubble has a title, text, an optional action button, "Skip tour" and ✕.
  - Clicks pass through to the spotlighted element and are blocked elsewhere.
  - **Claude's call:** build this small custom overlay instead of react-joyride. Steps here advance on app events (card opened, draft written, OAuth return), not "Next" buttons, and the bubble needs the yeti. The joyride tour in `TourContext.tsx` stays as it is for the sidebar "Take the tour".
- **Tour state:** extend `WalkthroughState` (`services/walkthrough.ts`) with tour steps (e.g. `tourStep: "pick" | "draft" | "review" | "gmail" | "open" `) and the chosen `contactId` and person name. Keep `closedAt`/`skippedAt`/`reminderDismissedAt`/`status`. Moving through steps sets `status: "in_progress"`, and opening Gmail sets `completed`.
- **Find:**
  - Replace the OFF-57 `WalkthroughDialog` on Find with the tour.
  - Add `data-tour` anchors to the first recommended cards and the profile pop-up's primary button.
  - Keep holding paid lookups (`allowPaidLookups: false`) for the feed while the tour is on the pick step, as OFF-57 does for the modal.
  - After a successful draft during the tour, navigate to `/contacts?contact=<id>`.
  - Fix the toast's "It's in your Gmail drafts" wording when no Gmail draft was made ("Saved in Contacts").
- **Contacts:**
  - Anchors on the conversation draft area and the Gmail button.
  - The tour's Gmail steps run here, including handling the OAuth return params and cleaning them from the URL.
  - After the push, the contact/thread queries refresh so the Gmail button's link becomes the `gmailDraftUrl`.
- **Banner:** "Continue" on the Find banner resumes the tour.

## Decisions after review (Claude's calls; Nick delegated these)

1. **No in-app editing in the tour.** Step 3's bubble says "Read it over. You can tweak it in Gmail before you send." It does not say "edit it here". On Contacts the spotlight covers only the conversation's message area, never the reply editor, and the overlay blocks clicks outside the spotlight. So the in-app Send and Generate buttons can't be used mid-tour.
2. **The tour always drafts in-app first, for everyone.** During the tour, Find's `contact(p)` calls outreach with `createGmailDraft: false`. At the Gmail step:
   - A user without Gmail send/draft scopes goes through OAuth first.
   - A user who already has them skips straight to the push.
   - The push route returns the existing Gmail draft if the contact already has one (no update, no second draft).
3. **No migration.** OFF-57 was never pushed or deployed, so no real user holds its walkthrough progress. Bump `WALKTHROUGH_VERSION`, and older/partial states restart at the pick step, as `normalizeWalkthrough` already does.
4. **Resume and skip:**
   - The banner shows whenever the walkthrough is incomplete and the banner itself wasn't dismissed. That includes after Skip or ✕.
   - The banner's "Continue" clears `closedAt` and `skippedAt` and resumes at the saved step. From then on the tour auto-runs again, including across refreshes and the Google round-trip, until it's completed, skipped or closed again.
   - The Gmail step has a "Not now" button that closes the tour (`closedAt`). The draft stays in Contacts, and the banner can resume it.
5. **Self-match uses the LinkedIn slug only.** Read `users/{uid}.profile.linkedinUrl`, falling back to `users/{uid}.linkedinUrl`, canonicalized like `_candidate_id`, and exclude that id. No name matching: it adds little (students rarely have a current company) and risks hiding namesakes. Nick's Giovanna case is a slug match.

## Non-goals

- Sending from inside Offerloop during the tour. The user sends from Gmail. The in-app send (Archit's `send-first-email`) stays for other uses.
- Changing the sidebar joyride tour.
- Removing `/get-started` or `WalkthroughFlow`. They stay as they are (page mode), just out of the new-user path. `WalkthroughDialog` is no longer used on Find.
- Showing the tour to existing users.

## Anti-goals

- A big pop-up covering the page. Only a dim layer, a spotlight, the yeti and a bubble.
- Cards that look different from Find's normal cards.
- Asking for Gmail before the user has seen their draft.
- A second Gmail draft for the same email, or a paid call before the user clicks anything.
- The tour reappearing after skip/close.

## Constraints

- Branch `nickwittig/offerloop-web-redesign`, built in a worktree. Commit locally; never push. Don't start or restart the local backend (another session runs it). Local dev hits production Firebase: unit tests only, plus manual checks by Nick with a `+test` account.
- Reuse the existing billing handlers. The new gmail-draft route is free.

## Left to the builder

Component and file names, bubble copy polish within the intent above, exact spotlight padding and animation, mobile placement details, and the test split.
