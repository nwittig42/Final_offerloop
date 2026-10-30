# First-email pop-up on Find + person profile pop-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** New users finish the original onboarding, land on Find, and Archit's first-email walkthrough opens as a pop-up over it with warehouse people already loaded. Clicking any person card on Find opens a large profile pop-up with big action buttons, including Meeting prep, which saves the person to Contacts.

**Architecture:** The walkthrough page (`GetStartedPage.tsx`) becomes a reusable `WalkthroughFlow` body with exit callbacks. `/get-started` renders it full-page, and a new `WalkthroughDialog` renders it in a modal on Find. A `useFindWalkthrough` hook owns when the modal auto-opens and holds paid lookups off while it's showing. A new `PersonProfileDialog` replaces `PersonInfoDialog`, and `PersonCard` gains a whole-card `onOpen`. The backend change is limited to sending every school for each person.

**Tech Stack:** React 18 + TypeScript + Vite, Tailwind, shadcn `Dialog`/`AlertDialog`, React Query, Firestore web SDK, vitest + Testing Library; Flask + pytest.

**Spec:** `docs/superpowers/specs/2026-10-25-first-email-popup-and-person-profile-design.md` (Linear OFF-57)

## Global Constraints

- Work on branch `nickwittig/offerloop-web-redesign` in `/Users/nicholaswittig/Offerloop/Final_offerloop`. Commit locally after each task; **never push**. End commit messages with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- **Don't start or restart the local backend (:5001)**; another session runs it. Say when backend changes need a restart.
- Local dev hits **production Firebase**. Test by hand only through `/dev/get-started-preview`, `/dev/onboarding-preview`, or a throwaway account you were told to use.
- No new paid calls. Anything that charges credits reuses today's handlers: Find's `contact(p)`, `apiService.createCoffeeChatPrep`, and the walkthrough's reveal/outreach/send.
- Walkthrough people and FindPage's feed use `allowPaidLookups: false` (`?prefetch=0&dig=0`) while the walkthrough modal is open or about to auto-open.
- The profile shows only real data: no empty sections, no filler text.
- Look: Offerloop walkthrough styling (`pages/onboardingTheme.ts` `OB` tokens, `components/walkthrough/MatchCards.tsx` portrait/monogram). Big buttons are 52px tall.
- Frontend tests: `cd connect-grow-hire && npx vitest run <paths>`. Backend tests: `cd backend && /Library/Frameworks/Python.framework/Versions/3.12/bin/pytest -q <paths>`. Typecheck: `npx tsc --noEmit -p .` must stay at **130** errors, the same baseline as today.

## Review Focus

1. **Clicks inside the card.** Clicking a card's bookmark, dismiss, LinkedIn, Contact/See email or "View contact" must not also open the pop-up, and pressing Enter on a focused button inside the card must not either. Task 6 tests this.
2. **Auto-open happens once.** After a reload or a new tab following a close or skip, the modal must not auto-open, though the banner can still reopen it. Task 2 tests `shouldAutoOpen`; Task 5 tests the hook.
3. **Meeting prep save only on success.** Cancelling the confirm dialog, or not having 15 credits, must not save anyone to Contacts. Running it on an already-saved person must not remove them, since the bookmark toggle would. Task 7 tests this.
4. **Gmail round-trip from the modal.** After Google OAuth the user returns to `/find?walkthrough=1&connected=gmail` (or `&gmail_error=…`). The modal must reopen on the draft step with the result handled, and the query string must be cleaned. Task 4 tests the return path; Task 5 tests the reopen.
5. **People with missing data.** A person with no LinkedIn URL hides Meeting prep and LinkedIn. With no schools there's no schools line, and with no reasons and no briefing there's no "Why them" section. Decks saved before Task 1 lack `schools` and fall back to `school`. Task 1 and Task 7 test this.

---

### Task 1: Send every school from the warehouse to the UI

**Files:**
- Modify: `backend/app/services/firm_cache/reader.py` (`firm_employee_doc_to_app_contact`, ~179-234)
- Modify: `backend/app/services/person_ranker.py` (`_to_response_dict`, ~977-1010)
- Modify: `connect-grow-hire/src/components/find/people/peopleModel.ts` (`Person`, `personFromRanker`)
- Test: `backend/tests/test_ranker_schools.py` (create), `connect-grow-hire/src/components/find/people/peopleModel.test.ts` (create)

**Interfaces:**
- Produces: the reader's contact dict gains `"Schools": list[str]` (from `schools_display`; `[]` when missing). The ranker response `person` gains `"schools": list[str]`. `Person` gains `schools?: string[]` and `joinedYear?: number | null`. `personFromRanker` fills `schools` from `person.schools` (falling back to `[school]` when empty and `school` is set) and `joinedYear` from `person.joinedYear`.

- [ ] **Step 1: Write the failing backend test** `test_ranker_schools.py`:
  - `test_reader_exposes_all_schools`: `firm_employee_doc_to_app_contact({...minimal doc, "schools_display": ["University of Michigan", "Harvard Business School"]})["Schools"] == ["University of Michigan", "Harvard Business School"]`, and `["College"] == "University of Michigan"` (unchanged).
  - `test_reader_schools_empty_when_missing`: no `schools_display` gives `["Schools"] == []`.
  - `test_response_dict_sends_schools`: `_to_response_dict({"id": "x", "candidate": {"Schools": ["A", "B"], "College": "A"}, "score": 7, "tier": "t", "reasons": [], "briefing": ""})["person"]["schools"] == ["A", "B"]`; a candidate without `Schools` gives `[]`.
- [ ] **Step 2: Run it and confirm it fails.** `pytest -q tests/test_ranker_schools.py` should FAIL with KeyError 'Schools'.
- [ ] **Step 3: Implement.** Add `"Schools": list(schools_display)` in the reader. Add `"schools": list(cand.get("Schools") or [])` to the allowlist in `_to_response_dict`, with a one-line comment in the style of the existing allowlist-extension comments (school names are public profile facts).
- [ ] **Step 4: Write the failing frontend test** `peopleModel.test.ts`:
  - `personFromRanker({ person: { id: "a", firstName: "M", lastName: "O", college: "Michigan", schools: ["Michigan", "HBS"], joinedYear: 2021 }, reasons: [], briefing: "" }, 0, "d")` gives `schools` equal to `["Michigan", "HBS"]` and `joinedYear` equal to `2021`.
  - A legacy payload with `college: "Michigan"` and no `schools` gives `schools` equal to `["Michigan"]`.
  - With neither, `schools` equals `[]`.
- [ ] **Step 5: Implement the `Person` fields and the `personFromRanker` mapping.** Run `npx vitest run src/components/find/people/peopleModel.test.ts` and `pytest -q tests/test_ranker_schools.py tests/test_person_ranker_personalization.py`. Expected: PASS.
- [ ] **Step 6: Commit** with the message `feat(find): send every school a person attended to the UI (OFF-57)`, and note in the summary that the backend needs a restart to pick this up.

### Task 2: Walkthrough state: skip, close, auto-open, and the pending record written by onboarding

**Files:**
- Modify: `connect-grow-hire/src/services/walkthrough.ts`
- Modify: `connect-grow-hire/src/pages/OnboardingFlow.tsx` (`buildFinalData`, ~111-145)
- Test: `connect-grow-hire/src/services/walkthrough.test.ts`

**Interfaces:**
- Produces:
  - `WalkthroughState` gains `closedAt?: string` and `skippedAt?: string`.
  - `normalizeWalkthrough` preserves both, including through the version/step reset branch, alongside `reminderDismissedAt` and `createdAt`.
  - `export const shouldAutoOpen = (s: WalkthroughState | null | undefined): boolean` returns `walkthroughIncomplete(s) && !s.closedAt && !s.skippedAt`.
  - `export function newWalkthrough(now: string): WalkthroughState` returns `{ version: WALKTHROUGH_VERSION, status: "pending", step: "matches", createdAt: now }`.

- [ ] **Step 1: Write failing tests** in `walkthrough.test.ts`:
  - `shouldAutoOpen(newWalkthrough(t))` is true.
  - It's false once `closedAt` is set, false once `skippedAt` is set, false for `status: "completed"`, and false for `null`.
  - `normalizeWalkthrough({ version: 0, status: "pending", step: "bogus", closedAt: "x", skippedAt: "y" })` keeps `closedAt: "x"` and `skippedAt: "y"`.
  - `newWalkthrough("2026-10-25T00:00:00Z")` deep-equals the shape above.
- [ ] **Step 2: Run** `npx vitest run src/services/walkthrough.test.ts`. Expected: FAIL (`shouldAutoOpen`/`newWalkthrough` not exported).
- [ ] **Step 3: Implement the type fields, the normalize change, `shouldAutoOpen` and `newWalkthrough`.**
- [ ] **Step 4: Have onboarding write the pending walkthrough.** In `OnboardingFlow.tsx` `buildFinalData`, add `walkthrough: newWalkthrough(nowIso)`, using the same timestamp as `onboarding.completedAt`. Preview mode already returns before `completeOnboarding`, so it writes nothing. Confirm `firestore.rules` allows the client to write `walkthrough` on `users/{uid}`; Archit's v2 did this client-side through `saveWalkthrough`, so it should.
- [ ] **Step 5: Run the tests again.** `npx vitest run src/services/walkthrough.test.ts src/components/walkthrough`. Expected: PASS.
- [ ] **Step 6: Commit** with the message `feat(walkthrough): onboarding queues the first-email walkthrough; skip/close state (OFF-57)`.

### Task 3: `useBookmarks.ensureSaved`, which saves a person but never removes one

**Files:**
- Modify: `connect-grow-hire/src/components/find/people/useBookmarks.ts`
- Test: `connect-grow-hire/src/components/find/people/useBookmarks.test.tsx` (create)

**Interfaces:**
- Produces: `useBookmarks()` also returns `ensureSaved(p: Person, opts?: { quiet?: boolean }): Promise<boolean>`.
  - It resolves `true` when the person is already in Contacts (by `contactFor`) or was saved now, and `false` when the save failed.
  - It never deletes.
  - `quiet: true` skips the "Saved X" toast. The failure toast always shows.
  - `toggle` keeps its behavior and reuses the same save code: move the add branch into a shared `save(p, quiet)` inside the hook.

- [ ] **Step 1: Write failing tests**, mocking `firebase/firestore` `setDoc`, `firebaseApi.createContact`, `firebaseApi.getContacts` (whatever `contactsQuery` uses), the auth context, and the toast:
  - `ensureSaved resolves true without writing when the person is already a contact`: preload the contacts query with a doc whose `linkedinUrl` matches; expect `setDoc` and `createContact` not called.
  - `ensureSaved saves a recommended person to ranker_<id> and resolves true`: expect `setDoc` called with a doc path ending in `contacts/ranker_<id>` and `status: "Saved"`.
  - `ensureSaved never removes`: calling it twice on the same person leaves it saved; no delete is called.
  - `ensureSaved resolves false when the write fails`: `setDoc` rejects.
- [ ] **Step 2: Run** `npx vitest run src/components/find/people/useBookmarks.test.tsx`. Expected: FAIL.
- [ ] **Step 3: Implement `ensureSaved` and the shared `save`.**
- [ ] **Step 4: Run it again.** Expected: PASS.
- [ ] **Step 5: Commit** with the message `feat(find): ensureSaved for saving a person without toggling (OFF-57)`.

### Task 4: Turn the walkthrough page into a reusable `WalkthroughFlow`

**Files:**
- Create: `connect-grow-hire/src/components/walkthrough/WalkthroughFlow.tsx` (the body, moved out of `GetStartedPage.tsx`)
- Modify: `connect-grow-hire/src/pages/GetStartedPage.tsx` (becomes a thin page wrapper)
- Test: `connect-grow-hire/src/pages/__tests__/GetStartedPage.test.tsx` (must keep passing unchanged), `connect-grow-hire/src/components/walkthrough/WalkthroughFlow.test.tsx` (create)

**Interfaces:**
- Produces:
  ```ts
  export type WalkthroughExit = "skip" | "later" | "find";
  export interface WalkthroughFlowProps {
    preview?: boolean;
    services?: WalkthroughServices;
    /** Where Google sends the user back after Gmail OAuth (relative path). */
    gmailReturnPath: string;
    /** "page": exits navigate like today. "modal": exits call onExit; a sent email shows the in-place success screen. */
    mode: "page" | "modal";
    onExit?: (why: WalkthroughExit) => void;
  }
  export function WalkthroughFlow(props: WalkthroughFlowProps): JSX.Element
  ```
  - `GetStartedPage` renders `<WalkthroughFlow mode="page" gmailReturnPath={"/get-started" + returnTo query} … />` and keeps today's behavior exactly: exits `navigate(exitTo)`, and `finishSent` navigates to `/contacts?contact=…`.
  - In `mode="modal"`:
    - "Skip for now and go to Find" calls `onExit("skip")`.
    - "Save and finish later" calls `onExit("later")`, after flushing the draft save as today.
    - "Search on Find" calls `onExit("find")`.
    - A loaded `status: "completed"` calls `onExit("find")` instead of navigating.
    - `finishSent` shows the success screen: heading "Sent to {firstName}", and two big buttons, "Open in Contacts" (`navigate("/contacts?contact=" + id)`) and "Done" (`onExit("find")`). The preview's existing `sentTo` screen can be reused.
    - The OAuth `?connected=`/`?gmail_error=` handling still reads and cleans `location.search`. In the modal it runs on `/find`, so cleaning must keep other params except `connected`, `gmail_error` and `walkthrough`.
  - `connectGmail` uses `gmailReturnPath` instead of the hard-coded `/get-started…` back path.

- [ ] **Step 1: Write failing tests** in `WalkthroughFlow.test.tsx`, using the same preview services and helpers as `GetStartedPage.test.tsx`:
  - `modal mode: Skip calls onExit("skip") and does not navigate`.
  - `modal mode: a sent email shows "Sent to <first name>" with Open in Contacts and Done; Done calls onExit("find")`.
  - `modal mode: connectGmail is called with gmailReturnPath "/find?walkthrough=1"`: a spy `connectGmail` in the injected services receives exactly that.
  - `modal mode: returning with ?walkthrough=1&connected=gmail on /find handles the connect and leaves /find with no walkthrough/connected params`.
  - `modal mode: a completed walkthrough calls onExit("find")`.
- [ ] **Step 2: Run** `npx vitest run src/components/walkthrough/WalkthroughFlow.test.tsx`. Expected: FAIL (module missing).
- [ ] **Step 3: Move the body into `WalkthroughFlow.tsx`** with the props above. Branch on `mode` only at the exit points listed above. Keep all copy, steps, error states and analytics as they are. Keep `/dev/get-started-preview` working: `GetStartedPage preview` passes `mode="page"`.
- [ ] **Step 4: Run** `npx vitest run src/components/walkthrough src/pages/__tests__/GetStartedPage.test.tsx`. Expected: all PASS, including the 32 existing GetStartedPage tests unchanged.
- [ ] **Step 5: Commit** with the message `refactor(walkthrough): reusable WalkthroughFlow with page and modal modes (OFF-57)`.

### Task 5: The walkthrough pop-up on Find

**Files:**
- Create: `connect-grow-hire/src/components/walkthrough/useFindWalkthrough.ts`
- Create: `connect-grow-hire/src/components/walkthrough/WalkthroughDialog.tsx`
- Modify: `connect-grow-hire/src/components/walkthrough/FirstEmailReminder.tsx` (accept `state` + `onContinue` from the hook; drop its own load)
- Modify: `connect-grow-hire/src/pages/FindPage.tsx` (~162-200 feed query, ~525 reminder, render the dialog)
- Test: `connect-grow-hire/src/components/walkthrough/useFindWalkthrough.test.tsx` (create), `FirstEmailReminder.test.tsx` (update)

**Interfaces:**
- Consumes: `shouldAutoOpen`, `walkthroughIncomplete`, `loadWalkthrough`, `saveWalkthrough` (Task 2); `WalkthroughFlow` / `WalkthroughExit` (Task 4).
- Produces:
  ```ts
  export function useFindWalkthrough(uid: string | undefined, deps?: { load?: typeof loadWalkthrough; save?: typeof saveWalkthrough }): {
    state: WalkthroughState | null;   // null until loaded (or no walkthrough)
    loaded: boolean;
    open: boolean;
    openNow: () => void;               // banner "Continue"
    exit: (why: WalkthroughExit) => void; // skip -> skippedAt; later/find -> closedAt (only if not already set); closes
    holdPaidLookups: boolean;          // !loaded || open
  }
  ```
  - On load, `open = shouldAutoOpen(state) || searchParams.get("walkthrough") === "1"` (the Gmail return).
  - `WalkthroughDialog({ open, onExit })` is a shadcn `Dialog`, large (`max-w-[1040px]`, `max-h-[92vh]`, scrollable). It renders `<WalkthroughFlow mode="modal" gmailReturnPath="/find?walkthrough=1" onExit={onExit} />`. Closing it with ✕ or the overlay calls `onExit("later")`.
  - `FirstEmailReminder` props become `{ uid, state, onContinue, save? }`. "Continue" becomes a button calling `onContinue`. It still hides when `reminderDismissedAt` is set or the walkthrough is complete.

- [ ] **Step 1: Write failing tests** for `useFindWalkthrough` (renderHook inside a MemoryRouter):
  - `opens automatically for a fresh pending walkthrough`.
  - `does not auto-open after closedAt or skippedAt` (load returns those).
  - `opens on /find?walkthrough=1 even after closedAt`.
  - `exit("skip") saves skippedAt and closes`; `exit("later") saves closedAt and closes`; `exit("later") keeps an existing closedAt`.
  - `holdPaidLookups is true until loaded and while open, false after closing`.
  - `no walkthrough on the user doc: loaded, closed, holdPaidLookups false` (load resolves `null`; existing users).
  - In `FirstEmailReminder.test.tsx`, update the Continue test: clicking it calls `onContinue`.
- [ ] **Step 2: Run** `npx vitest run src/components/walkthrough`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement the hook, the dialog and the reminder changes.**
- [ ] **Step 4: Wire up FindPage.**
  - Call `useFindWalkthrough(user?.uid)` (skip it when `IS_DEV_PREVIEW`).
  - Pass `allowPaidLookups: !holdPaidLookups` to both `getRankerCandidates` calls in the feed query, and add `holdPaidLookups` to that query's key. Gate the query with `enabled: … && loaded` so the first fetch knows the hold.
  - Render `<FirstEmailReminder uid state onContinue={openNow} />` and `<WalkthroughDialog open={open} onExit={exit} />`.
- [ ] **Step 5: Run** `npx vitest run src/components/walkthrough src/pages/__tests__` and `npx tsc --noEmit -p .` (130). Expected: PASS / 130.
- [ ] **Step 6: Commit** with the message `feat(find): first-email walkthrough opens as a pop-up on Find for new users (OFF-57)`.

### Task 6: Make the whole person card clickable

**Files:**
- Modify: `connect-grow-hire/src/components/find/people/PersonCard.tsx`
- Test: `connect-grow-hire/src/components/find/people/PersonCard.test.tsx` (create)

**Interfaces:**
- Produces: `PersonCard` takes an optional `onOpen?: () => void` and drops the `onInfo` prop and its "Info" button.
  - When `onOpen` is set and the card isn't a sample, the `<article>` gets `role="button"`, `tabIndex={0}`, `aria-label={"Open " + person.name}`, `cursor-pointer`, `onClick={onOpen}`, and `onKeyDown` (Enter/Space call `onOpen` only when `e.target === e.currentTarget`).
  - Every interactive child (buttons, links, the LinkedIn link, bookmark, dismiss) stops click propagation. Do this with one `onClick` wrapper on each action container rather than per button, where possible.

- [ ] **Step 1: Write failing tests:**
  - `clicking the card body calls onOpen`.
  - `clicking Contact calls onContact and not onOpen`; the same for bookmark, dismiss and the LinkedIn link, and for the drafted "See email" link (render inside a MemoryRouter).
  - `Enter on the focused card calls onOpen; Enter on a focused inner button does not`.
  - `sample cards do not open`.
  - `there is no Info button`.
- [ ] **Step 2: Run** `npx vitest run src/components/find/people/PersonCard.test.tsx`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run it again.** Expected: PASS.
- [ ] **Step 5: Commit** with the message `feat(find): open a person by clicking their card; card buttons keep their own actions (OFF-57)`.

### Task 7: `PersonProfileDialog` with big buttons and Meeting prep, replacing `PersonInfoDialog`

**Files:**
- Create: `connect-grow-hire/src/components/find/people/PersonProfileDialog.tsx`
- Delete: `connect-grow-hire/src/components/find/people/PersonInfoDialog.tsx`
- Modify: `connect-grow-hire/src/pages/FindPage.tsx` (~377, ~500, ~750), `connect-grow-hire/src/pages/OrgDirectoryPage.tsx` (~316, ~335)
- Test: `connect-grow-hire/src/components/find/people/PersonProfileDialog.test.tsx` (create)

**Interfaces:**
- Consumes: `Person.schools`/`joinedYear` (Task 1), `ensureSaved` (Task 3), `PersonCard onOpen` (Task 6), `relationFor` (peopleModel), and the portrait/monogram from `components/walkthrough/MatchCards.tsx` (export `ProfilePortrait` if it isn't exported).
- Produces:
  ```ts
  interface PersonProfileDialogProps {
    person: Person | null;
    contactState: ContactState;
    onContact: () => void;            // the card's exact contact handler
    contactHref?: string;             // when drafted: "Open in Contacts" link target (same as the card's)
    isBookmarked: boolean;
    onToggleBookmark: () => void;
    ensureSaved: (p: Person, opts?: { quiet?: boolean }) => Promise<boolean>;
    onClose: () => void;
  }
  export function PersonProfileDialog(props: PersonProfileDialogProps): JSX.Element
  ```
- **Layout:** a large centered `Dialog` (`max-w-[820px]`) with the portrait (photo or monogram) on the left and details on the right. Below 640px it stacks. Each item renders only with data:
  - Company eyebrow, serif name, "Title at Company".
  - All schools (`schools`, falling back to `school`) · location, and "Joined {joinedYear}".
  - Chips: `orgBadge`, `relationFor` (same school), and the fit label from score (same thresholds as `FitBadge`: ≥8 "Strong fit", ≥6.5 "Good fit").
  - "Why them": the reasons list, then the briefing, de-duplicated like `matchReasons`.
- **Buttons** (52px):
  - Primary, mirroring the card's states: idle "See email & draft" (recommended) or "Draft email" (search) → `onContact`; `finding`/`working` disabled "Finding email…"/"Writing…"; `drafted` "Open in Contacts" link to `contactHref`; `no_email` disabled "No email found"; sample disabled.
  - "Meeting prep", only with a LinkedIn URL.
  - "Saved"/"Save" → `onToggleBookmark`.
  - "LinkedIn ↗", only with a URL.
- **Meeting prep flow:** a confirm `AlertDialog` with the same copy as `MeetingPrepButton` ("Prep for a meeting with {name}?" … "Uses 15 credits." / Cancel / Run prep). On Run prep:
  1. If `creditsView.balance < COFFEE_CHAT_CREDITS`, show the same "Not enough credits" toast and stop.
  2. `await ensureSaved(person, { quiet: true })`; if `false`, stop.
  3. `apiService.createCoffeeChatPrep({ linkedinUrl })`.
  4. Toast "Meeting prep started" / "{firstName} is saved in Contacts; the prep will appear there when it's ready." with a View action to `/contacts?filter=saved`.
  5. `queryClient.invalidateQueries({ queryKey: PREPS_KEY })`.
  On an API error, show the same error toast as `MeetingPrepButton`; the person stays saved.

- [ ] **Step 1: Write failing tests** (mock `apiService.createCoffeeChatPrep`, `useCreditsView`, and the toast):
  - `shows name, title at company, all schools and location`.
  - `hides empty sections`: a person with no reasons, briefing, schools or LinkedIn shows no "Why them", no schools line, and no Meeting prep or LinkedIn buttons.
  - `falls back to school when schools is empty`.
  - `primary button calls onContact when idle; shows Open in Contacts when drafted; disabled No email found when no_email`.
  - `Meeting prep: cancel saves nothing and starts nothing`.
  - `Meeting prep: not enough credits saves nothing and starts nothing`.
  - `Meeting prep: confirm saves first, then starts the prep with the LinkedIn URL`: assert `ensureSaved` was called before `createCoffeeChatPrep`, using call order (`mock.invocationCallOrder`).
  - `Meeting prep: if saving fails, the prep doesn't start`.
- [ ] **Step 2: Run** `npx vitest run src/components/find/people/PersonProfileDialog.test.tsx`. Expected: FAIL.
- [ ] **Step 3: Implement the dialog.**
- [ ] **Step 4: Wire it into both pages.**
  - FindPage: replace `infoPerson`/`setInfoPerson` and `PersonInfoDialog` with `profilePerson`/`setProfilePerson`, pass `onOpen={() => setProfilePerson(p)}` to each `PersonCard`, and pass `bookmarks.isBookmarked`/`bookmarks.toggle`/`bookmarks.ensureSaved` and the same `contactHref` the card uses.
  - OrgDirectoryPage: do the same with its own handlers. Its `onContact` is the directory's existing contact action.
  - Delete `PersonInfoDialog.tsx`.
- [ ] **Step 5: Run the full suite and checks.** `npx vitest run` (all pass), `npx tsc --noEmit -p .` (130), `npx vite build` (succeeds).
- [ ] **Step 6: Commit** with the message `feat(find): big profile pop-up for every person, with Meeting prep that saves to Contacts (OFF-57)`.

### Task 8: Check it by hand and close out

- [ ] **Step 1: Profile pop-up.** In `localhost:8080/find` (Nick's session, already signed in): click a card body to open the pop-up, then click Contact/bookmark/LinkedIn on a card to confirm only that action runs. Open someone with no LinkedIn to confirm there's no Meeting prep or LinkedIn button. Do **not** run Meeting prep or Contact on a real person unless Nick asks; both spend credits on the production account.
- [ ] **Step 2: Walkthrough pop-up.** `localhost:8080/dev/onboarding-preview` finishes into the walkthrough preview. The real Find-modal path needs a new account; if Nick doesn't want to make one, run the e2e emulator harness in `scripts/e2e-emulator/README.md` (frontend + emulators only; it starts its own backend in emulator mode, which is separate from the :5001 backend) and walk through sign-up → onboarding → Find → modal → close → reload (no auto-open) → banner Continue → modal.
- [ ] **Step 3: Update Linear.** Move OFF-57 to In Review with a comment listing the commits, test counts, and "backend restart needed for schools".
