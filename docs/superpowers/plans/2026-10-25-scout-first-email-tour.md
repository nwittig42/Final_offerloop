# Scout first-email tour Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the OFF-57 walkthrough pop-up on Find with a Scout-yeti spotlight tour across the real Find and Contacts pages: pick a card, write the email in-app, review the draft in Contacts, connect Gmail, push the draft to Gmail, and open it. Also stop recommending users to themselves.

**Architecture:**
- **Tour overlay:** a small custom `ScoutTour` component draws a dim layer with a spotlight cutout around a `data-tour` target, plus the yeti and a speech bubble. It takes props only; the pages decide which step shows.
- **Tour state:** stored in `users/{uid}.walkthrough` (bumped version, new `tourStep`).
- **`useFirstEmailTour` hook:** owns loading, saving, auto-run, resume, skip and close. Find and Contacts each render the steps that belong to them.
- **Backend:** gains a free route that pushes a contact's saved draft into Gmail drafts, and a self-match filter in the ranker.

**Tech Stack:** React 18 + TS + Vite + Tailwind, React Query, Firestore web SDK, vitest + Testing Library; Flask + pytest.

**Spec:** `docs/superpowers/specs/2026-10-25-scout-first-email-tour-design.md` (Linear OFF-60)

## Global Constraints

- Build in a worktree on a branch off `nickwittig/offerloop-web-redesign`. Commit locally and never push. Use the commit trailer `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`. Never use bare `git stash`.
- Never start or restart any backend or dev server. Local dev hits production Firebase, so test with unit tests only.
- Commands:
  - Frontend tests: `cd connect-grow-hire && npx vitest run <paths>`.
  - Backend tests: `cd backend && /Library/Frameworks/Python.framework/Versions/3.12/bin/pytest -q <paths>`.
  - Typecheck: `npx tsc --noEmit -p . 2>&1 | grep -c 'error TS'` must stay **130**.
- No new paid calls. Reuse the existing reveal and outreach handlers. The new gmail-draft route is free.
- The tour is not a big pop-up. It is only a dim layer, a spotlight, the yeti (`connect-grow-hire/src/assets/scouts/scout-yeti-full.png`) and a bubble. Find's cards stay the real `PersonCard`s.
- Gmail is never requested before the user has seen their draft in Contacts.
- Copy is fixed by the spec. Step 3's bubble must not say "edit it here".

## Review Focus

1. **The Google round-trip.** The user comes back to `/contacts?contact=<id>&tour=gmail&connected=gmail`, or with `&gmail_error=scopes_declined`. They must land on the same contact, on the Gmail step, with the result handled and the params removed. Task 6 tests this.
2. **Already-connected users.** A user who already has send/draft scopes skips OAuth and still gets the push. A contact that already has a Gmail draft doesn't get a second one. Task 2 and Task 6 test this.
3. **Clicks during the tour.** Clicks outside the spotlight do nothing. The spotlighted element still works. The bubble buttons work. Task 4 tests this.
4. **No email found.** The reveal finds no email, the bubble says nothing was charged, and the tour returns to the pick step. Task 5 tests this.
5. **Resume and skip.** After Skip or ✕ the tour doesn't auto-run again. The banner's Continue clears both and resumes at the saved step, including after a refresh. Task 3 tests this.

---

### Task 1: Never recommend a user to themselves

**Files:**
- Modify: `backend/app/services/person_ranker.py` (`rank_people_for_user` ~138, user context ~474, candidate loop ~294)
- Modify: `backend/app/routes/ranker.py` (`people_deck_cache.serve` path ~287)
- Test: `backend/tests/test_ranker_self_match.py` (create)

**Interfaces:**
- Produces: `own_candidate_id(user_doc: dict) -> str | None`, which returns the canonical candidate id (same canonicalization as `_candidate_id`) of `profile.linkedinUrl`, falling back to `linkedinUrl`, or `None`. Both live ranking and cached-deck serving drop the candidate with that id.

- [ ] **Step 1: Write the failing tests.**
  - `own_candidate_id({"profile": {"linkedinUrl": "https://www.linkedin.com/in/giovanna-hughes/"}})` equals the id `_candidate_id` would give a candidate with that LinkedIn.
  - It falls back to top-level `linkedinUrl`, and returns `None` when neither is set.
  - Ranking with a candidate pool that includes the user's own slug excludes it.
  - Serving a cached deck that includes it excludes it. Mock Firestore the way `tests/test_person_ranker_personalization.py` does.
- [ ] **Step 2: Run it and confirm it fails.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the new test and `tests/test_person_ranker_personalization.py tests/test_ranker_schools.py`. Expect PASS. The 4 authlib errors in the personalization file are pre-existing and caused by the environment.
- [ ] **Step 5: Commit** `fix(ranker): never recommend a user their own profile (OFF-60)`.

### Task 2: `POST /api/contacts/<id>/gmail-draft`, which pushes the saved draft to Gmail

**Files:**
- Modify: `backend/app/routes/contacts.py` (next to `send-first-email` ~967)
- Modify: `connect-grow-hire/src/services/api.ts` (add `pushContactGmailDraft`)
- Test: `backend/tests/test_contact_gmail_draft.py` (create), `connect-grow-hire/src/services/__tests__/api.gmailDraft.test.ts` (create)

**Interfaces:**
- Produces the route `POST /api/contacts/<contact_id>/gmail-draft`, which needs `@require_firebase_auth`. Its responses:
  - `200 {"gmailDraftId", "gmailDraftUrl", "created": bool}`. `created` is false when the contact already had `gmailDraftId`; nothing new is made in that case.
  - `404 {"error": "contact_not_found"}`.
  - `400 {"error": "no_saved_draft"}` when `emailBody` or `draftToEmail` is missing.
  - `409 {"error": "gmail_not_connected" | "gmail_scope_missing"}`.
  - On success it writes `gmailDraftId`, `gmailDraftUrl`, `pipelineStage: "draft_created"` and `draftStillExists: True`. It reuses `gmail_client.create_gmail_draft_for_user(...)` with the same arguments, including the resume, that ranker outreach passes (`routes/ranker.py` ~1283-1345). It costs no credits.
- Produces in the frontend: `apiService.pushContactGmailDraft(contactId: string): Promise<{ ok: true; gmailDraftUrl: string; created: boolean } | { ok: false; reason: "gmail_not_connected" | "gmail_scope_missing" | "no_saved_draft" | "error" }>`.

- [ ] **Step 1: Write the failing backend tests.** Use the fake Firestore in `tests/fake_firestore.py` and mock `create_gmail_draft_for_user` and the Gmail creds loader.
  - It creates a draft and writes the four fields.
  - An existing `gmailDraftId` returns `created: False` without calling the creator.
  - It returns 409 when there are no creds.
  - It returns 400 when there's no saved draft.
  - It returns 404 for an unknown contact.
- [ ] **Step 2: Write the failing frontend test** for `pushContactGmailDraft`, mapping 200, 409 and 400 to the union above. Copy the pattern from `api.walkthrough.test.ts`.
- [ ] **Step 3: Run both and confirm they fail.**
- [ ] **Step 4: Implement both.**
- [ ] **Step 5: Run both and confirm they pass.** Run the tsc count.
- [ ] **Step 6: Commit** `feat(contacts): push a contact's saved draft into Gmail drafts (OFF-60)`. Note that the backend needs a restart.

### Task 3: Tour state and the `useFirstEmailTour` hook

**Files:**
- Modify: `connect-grow-hire/src/services/walkthrough.ts`
- Create: `connect-grow-hire/src/components/walkthrough/useFirstEmailTour.ts`
- Test: `connect-grow-hire/src/services/walkthrough.test.ts`, `connect-grow-hire/src/components/walkthrough/useFirstEmailTour.test.tsx` (create)

**Interfaces:**
- Produces:
  - `export type TourStep = "pick" | "draft" | "review" | "gmail" | "open"`.
  - `WalkthroughState` gains `tourStep?: TourStep`, `tourContactId?: string`, `tourPersonFirstName?: string`.
  - Bump `WALKTHROUGH_VERSION` to 2. Older states restart at `tourStep: "pick"`, keeping `closedAt`, `skippedAt`, `reminderDismissedAt` and `createdAt`.
  - `newWalkthrough(now)` includes `tourStep: "pick"`.
  - `export function resumeWalkthrough(s: WalkthroughState): WalkthroughState` clears `closedAt` and `skippedAt`.
- Produces the hook:
  ```ts
  export function useFirstEmailTour(uid: string | undefined, deps?: { load?: typeof loadWalkthrough; save?: typeof saveWalkthrough }): {
    loaded: boolean;
    state: WalkthroughState | null;
    active: boolean;                       // shouldAutoOpen(state) after load
    step: TourStep | null;                 // state.tourStep when active
    goTo: (step: TourStep, patch?: Partial<WalkthroughState>) => Promise<void>; // sets status "in_progress"
    complete: () => Promise<void>;         // status "completed"
    skip: () => Promise<void>;             // skippedAt
    close: () => Promise<void>;            // closedAt
    resume: () => Promise<void>;           // resumeWalkthrough
  }
  ```
  - Every write re-reads the latest state, merges the change, then saves, the same way `useFindWalkthrough.exit` does.
  - The state is cached in the React Query cache under `["walkthrough", uid]`, so Find and Contacts share it and a navigation doesn't reload it.

- [ ] **Step 1: Write the failing tests:**
  - The v1 to v2 reset keeps `closedAt` and `skippedAt` and sets `pick`.
  - `newWalkthrough` has `pick`.
  - `resumeWalkthrough` clears both fields.
  - Hook: auto-active for fresh pending.
  - Hook: inactive after skip or close.
  - Hook: `resume` makes it active again at the saved step.
  - Hook: `goTo("review", { tourContactId })` saves the step and contact.
  - Hook: `complete` gives inactive.
  - Hook: no walkthrough doc gives inactive.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them and confirm they pass.**
- [ ] **Step 5: Commit** `feat(walkthrough): first-email tour state and hook (OFF-60)`.

### Task 4: `ScoutTour` overlay, with the spotlight, the yeti and the bubble

**Files:**
- Create: `connect-grow-hire/src/components/walkthrough/ScoutTour.tsx`
- Test: `connect-grow-hire/src/components/walkthrough/ScoutTour.test.tsx` (create)

**Interfaces:**
- Produces:
  ```ts
  export interface ScoutTourProps {
    target: string | null;      // CSS selector, e.g. '[data-tour="tour-first-cards"]'; null = centered bubble, no cutout
    title: string;
    body: React.ReactNode;
    action?: { label: string; onClick: () => void; disabled?: boolean };
    secondary?: { label: string; onClick: () => void };  // e.g. "Not now"
    onSkip: () => void;          // "Skip tour"
    onClose: () => void;         // ✕
  }
  export function ScoutTour(props: ScoutTourProps): JSX.Element | null
  ```
- **Behavior:**
  - Rendered in a portal.
  - Measures the target with `getBoundingClientRect`, using 8px padding and a 16px radius. It re-measures on scroll, resize and a `MutationObserver`, and waits up to 5s for the target to appear.
  - Draws a dim layer `rgba(15,23,42,0.45)` with a cutout around the target. Clicks inside the cutout reach the page; clicks anywhere else are swallowed.
  - The yeti image (~140px) and the bubble sit beside the cutout, on the side with more room. Below 640px they sit at the bottom of the screen.
  - The bubble uses the Offerloop `OB` tokens (`pages/onboardingTheme.ts`) and has a 44px primary action button.
  - It has `role="dialog"` and `aria-label={title}`, and Escape calls `onClose`.

- [ ] **Step 1: Write the failing tests.** Mock `getBoundingClientRect` on a target in the document.
  - It renders the title, body and the yeti `img`.
  - Action, secondary, Skip tour and ✕ call their handlers.
  - Escape calls `onClose`.
  - A click on the target element reaches its `onClick`.
  - A click on the dim layer outside the cutout does not reach an element underneath.
  - `target: null` renders a centered bubble with no cutout.
  - A missing target shows nothing until it appears.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them and confirm they pass.**
- [ ] **Step 5: Commit** `feat(walkthrough): ScoutTour spotlight overlay with the Scout yeti (OFF-60)`.

### Task 5: The tour on Find (pick and draft steps)

**Files:**
- Modify: `connect-grow-hire/src/pages/FindPage.tsx`, which replaces `useFindWalkthrough` + `WalkthroughDialog` with `useFirstEmailTour` + `ScoutTour`. This includes the feed hold, the `contact(p)` path (~458-515) and the toast.
- Modify: `connect-grow-hire/src/components/find/people/PersonProfileDialog.tsx`, which gets `data-tour="tour-profile-primary"` on the primary button.
- Modify: `connect-grow-hire/src/components/walkthrough/FirstEmailReminder.tsx`, where Continue calls `resume`.
- Test: `connect-grow-hire/src/components/walkthrough/findTourSteps.test.tsx` (create). Pull the step logic into a pure helper so it can be tested without the whole FindPage.

**Interfaces:**
- Consumes: `useFirstEmailTour` (Task 3) and `ScoutTour` (Task 4).
- Produces:
  ```ts
  export function findTourView(step: TourStep | null, ctx: { profileOpen: boolean; noEmailFor?: string }): null | { target: string; title: string; body: string }
  ```
  It lives in `components/walkthrough/findTourSteps.ts`. When the tour is on `pick` or `draft`, FindPage shows the view this function returns:
  - `pick` (profile closed): target `[data-tour="tour-first-cards"]`. Title "Your first email". Body "Here are a few people worth emailing first. Click one to see why they fit." If `noEmailFor` is set, the body is instead "No email for {name}. Nothing was charged. Pick someone else."
  - `pick` with the profile open: target `[data-tour="tour-profile-primary"]`. Title "Write it for me". Body "Hit this and I'll find their email and write a first draft for you."
  - `draft`: target `null`. Title "Writing your email…". Body "Finding their email and drafting it. One moment."
- **FindPage behavior:**
  - A wrapper with `data-tour="tour-first-cards"` goes around the first 4 recommended cards (or fewer if there are fewer).
  - Paid lookups stay held (`allowPaidLookups: false`, through the existing ref) while the tour is active on `pick`.
  - While the tour is active, `contact(p)`:
    - calls `goTo("draft")`;
    - reveals with `useFirstOutreach` when `apiService` first-outreach status says eligible (reuse the walkthrough services' `loadFirstOutreach`);
    - calls outreach with `createGmailDraft: false`.
  - On success it calls `goTo("review", { tourContactId, tourPersonFirstName })` and `navigate("/contacts?contact=" + id)`.
  - With no email, it goes back to `pick` with `noEmailFor`.
  - It's unchanged outside the tour.
  - The toast says "It's in your Gmail drafts and in Contacts." only when a Gmail draft was created. Otherwise it says "Saved in Contacts."
  - Skip tour and ✕ call `skip` and `close`. FindPage no longer renders `WalkthroughDialog`.

- [ ] **Step 1: Write the failing tests** for `findTourView`, covering each case and its exact copy.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement the helper and the FindPage, profile and reminder wiring.**
- [ ] **Step 4: Run** `npx vitest run src/components src/pages/__tests__` and the tsc count.
- [ ] **Step 5: Commit** `feat(find): Scout tour on Find replaces the walkthrough pop-up (OFF-60)`.

### Task 6: The tour on Contacts (review, Gmail and open steps)

**Files:**
- Modify: `connect-grow-hire/src/pages/ContactsPage.tsx` (renders `ScoutTour` for those steps)
- Modify: `connect-grow-hire/src/components/contacts/ContactConversation.tsx`, which gets `data-tour="tour-draft"` on the message list area (not the reply editor) and `data-tour="tour-gmail-button"` on the Gmail button
- Create: `connect-grow-hire/src/components/walkthrough/contactsTourSteps.ts`
- Test: `connect-grow-hire/src/components/walkthrough/contactsTourSteps.test.ts` (create), plus a hook-level test of the OAuth-return handling

**Interfaces:**
- Consumes: `useFirstEmailTour`, `ScoutTour`, `apiService.pushContactGmailDraft` (Task 2), `apiService.startGmailOAuth`, `apiService.gmailStatus`, and `canSendWith` (`pages/walkthroughServices.ts`).
- Produces: `contactsTourView(step, ctx: { firstName: string; gmailError?: string | null; pushing?: boolean; pushError?: string | null })`, returning `{ target, title, body, action, secondary? }` descriptors:
  - `review`: target `[data-tour="tour-draft"]`. Title "Your email to {first}". Body "Read it over. You can tweak it in Gmail before you send." Action "Looks good". If Gmail is already connected with `canSendWith`, the next step is `open` after a push; otherwise it is `gmail`.
  - `gmail`: target `null`. Title "Connect Gmail". Body "Connect Gmail so this goes out from your own inbox. Google will ask you to tick a few boxes. Tick all of them." Action "Connect Gmail". Secondary "Not now", which calls `close`.
    - With `gmailError` or missing scopes, the body is "Google didn't give us permission to create drafts. Connect again and tick every box." and the action is "Try again".
    - While `pushing`, the body is "Putting it in your Gmail drafts…" and the action is disabled.
  - `open`: target `[data-tour="tour-gmail-button"]`. Title "It's in your Gmail drafts". Body "Open it, give it a last look and hit send."
- **ContactsPage behavior:**
  - Only active when the tour is active and `?contact=` equals `tourContactId`. If the URL has no `contact` param during the tour, set it from `tourContactId`.
  - Connect calls `startGmailOAuth("/contacts?contact=" + id + "&tour=gmail")` and goes to the returned URL.
  - On return with `connected=gmail` or `gmail_error`, it checks `gmailStatus` with `canSendWith`. When allowed, it pushes; otherwise it shows the error copy. In both cases it removes `tour`, `connected` and `gmail_error` from the URL and keeps `contact`.
  - A successful push calls `goTo("open")` and invalidates the contacts and threads queries, so the Gmail button gets `gmailDraftUrl`.
  - Clicking the spotlighted Gmail button opens Gmail in a new tab (existing link behavior), calls `complete()`, and shows a 2-second centered "You're all set" bubble.
  - Push errors `gmail_not_connected` and `gmail_scope_missing` go back to the `gmail` step with the error copy.

- [ ] **Step 1: Write the failing tests:**
  - `contactsTourView` for each case and its exact copy.
  - The OAuth-return handler (extract it as a hook or function): `connected=gmail` with good scopes pushes, then `open`, then cleans the URL; `gmail_error=scopes_declined` shows the error copy and doesn't push.
  - Already connected at review skips to the push.
  - A push that returns `created: false` still moves to `open`.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full** `npx vitest run`, the tsc count and `npx vite build`.
- [ ] **Step 5: Commit** `feat(contacts): Scout tour reviews the draft, connects Gmail and opens it (OFF-60)`.
