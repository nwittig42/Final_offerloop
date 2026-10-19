# Email Open Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every email Offerloop creates for a user carries a 1x1 tracking pixel, and an open moves the contact to a new `email_opened` pipeline stage, shown as an "Opened" step on the website and the mobile app.

**Architecture:** Builds on PR #218 (`nickwittig/off-12-track-when-emails-get-opened`). That PR already embeds the pixel in 11 of 12 Gmail send/draft sites, serves `/api/t/o/<token>.gif`, and stamps `emailOpenedAt` / `emailLastOpenedAt` / `emailOpenCount` on `users/{uid}/contacts/{id}`. This plan adds:
- a guard test so no email path ships without the pixel
- a filter for the sender's own first render
- **a new `pipelineStage` value, `email_opened`**, sitting between `waiting_on_reply` and `replied`
- the mobile backend port
- the Opened step in every UI

`email_sent` / `waiting_on_reply` drive reply sync, follow-up nudges, Loop state, metrics and the mobile Inbox. So `email_opened` joins one shared "sent, awaiting reply" set (`AWAITING_REPLY_STAGES`), and every check of the old pair reads that set. That way opened contacts keep getting reply sync and follow-ups.

**Tech Stack:** Flask + Firestore (`offerloop-native`, shared by both backends), Gmail API (`gmail.compose` + `gmail.readonly`, no new scopes), React/TS + vitest (website), Expo/React Native (app, no test runner).

**Spec:** Linear OFF-12 (https://linear.app/offerloop/issue/OFF-12) plus its 2026-10-05 progress comment. Nick decided on 2026-10-18 that Opened is a real database stage.

## Global Constraints

- New stage value is exactly `email_opened`. UI label is exactly `Opened`.
- Stage order: `new` → `draft_created` → `email_sent` / `waiting_on_reply` → **`email_opened`** → `replied` → `meeting_scheduled` / `connected`.
- An open only promotes a contact out of `email_sent` or `waiting_on_reply`. It never moves a contact out of `replied`, a done stage, or a draft stage.
- A follow-up the user sends from `email_opened` resets the stage to `waiting_on_reply`, because the new email hasn't been opened yet. The contact-level `emailOpenedAt` keeps the first open ever.
- "Opened" is set only by the pixel. It is never a clickable/selectable target in any stage UI.
- No new OAuth scopes. `backend/app/config.py` `GMAIL_SCOPES` stays as is.
- Tracking never blocks or breaks a send. Every public function in `services/email_tracking.py` swallows its own errors.
- Kill switch stays: `EMAIL_OPEN_TRACKING=1` = everyone, `EMAIL_OPEN_TRACKING_UIDS=a,b` = trial users, unset = off. With tracking off, no contact ever reaches `email_opened`.
- Pixel URL is always `https://offerloop.ai/api/t/o/<token>.gif` (`EMAIL_TRACKING_BASE_URL`), from both backends.
- Emails Offerloop sends **to the user themself** (e.g. `agent_service.py:655`) are never tracked.
- No invented numbers in UI (mobile CLAUDE.md rule): open counts render only from `emailOpenCount`.

## Review Focus

1. **The sender reads their own sent email right after sending a draft from Gmail.** Gmail renders the sent message immediately, which loads the pixel. Expected: not an open, stage stays `waiting_on_reply`. Pinned by `test_hit_within_grace_of_send_time_does_not_count` (Task 2).
2. **An opened contact goes quiet for 7 days.** Expected: still gets a follow-up nudge and reply sync. Pinned by `test_email_opened_is_awaiting_reply_everywhere` (Task 3).
3. **The pixel loads before the Gmail webhook confirms the draft was sent.** Expected: the webhook lands the contact on `email_opened`, not `waiting_on_reply`. Pinned by `test_webhook_send_confirm_respects_earlier_open` (Task 3).
4. **The contact replied, then an old email gets reopened.** Expected: stays `replied`. Pinned by `test_open_never_demotes_replied` (Task 3) and the `replied → connected` UI test (Task 6).
5. **A new email path is added later without the pixel.** Expected: CI fails. Pinned by `test_every_gmail_send_site_is_tracked` (Task 1).

---

### Task 1: Close coverage gaps and add the guard test

Branch: `nickwittig/off-12-track-when-emails-get-opened`, rebased on `origin/main` first.

**Files:**
- Modify: `backend/app/services/gmail_client.py` (`create_drafts_batch`, ~line 1727)
- Modify: every draft-creating caller of `email_tracking.register` that doesn't pass `message_id`
- Test: `backend/tests/test_email_tracking.py`

**Interfaces:**
- Produces: tracking docs for drafts always carry `gmailMessageId` (Task 2 relies on it).

- [ ] **Step 1: Rebase.** `git fetch && git rebase origin/main`. Run `pytest backend/tests/test_email_tracking.py -q`. Expected: 18 passed.
- [ ] **Step 2: Write the failing guard test** `test_every_gmail_send_site_is_tracked`. It walks `backend/app/**/*.py`, finds each line matching `drafts\(\)\.create\(|messages\(\)\.send\(`, and asserts the enclosing function body references `email_tracking` or `tracking_kind`. Include allowlist dict `UNTRACKED_OK = {}` with a reason string per entry. The failure message lists `file:line` for each offender.
- [ ] **Step 3: Run it.** Expected: FAIL naming `gmail_client.py` `create_drafts_batch`.
- [ ] **Step 4:** `create_drafts_batch` has no callers, so delete it. If a caller appears after the rebase, add the pixel as `create_drafts_parallel` does.
- [ ] **Step 5:** Every `register(..., draft_id=...)` call also passes `message_id=draft["message"]["id"]`. Add `test_register_for_draft_stores_message_id`, asserting the stored doc's `gmailMessageId == "m1"`.
- [ ] **Step 6:** Run `pytest backend/tests/test_email_tracking.py -q`. Expected: all pass. Commit `test(tracking): fail CI when an email path skips the pixel (OFF-12)`.

### Task 2: Ignore the sender's own first render

**Files:**
- Modify: `backend/app/services/email_tracking.py` (`_is_sent`, `record_open`)
- Test: `backend/tests/test_email_tracking.py`

**Interfaces:**
- Consumes: `gmailMessageId` on tracking docs (Task 1).
- Produces: `_sent_at(uid, data, contact) -> datetime | None` replaces `_is_sent`. `None` = not sent yet.

- [ ] **Step 1: Write failing tests.**
  - `test_hit_within_grace_of_send_time_does_not_count`: `sentAt = now-20s`. `record_open` returns False, contact has no `emailOpenedAt`, token doc `selfViewHits == 1`.
  - `test_hit_after_grace_counts`: `sentAt = now-5min`. Returns True.
  - `test_draft_send_time_read_from_gmail_message`: fake `messages().get` returns `labelIds=["SENT"]`, `internalDate=now-10s`. Hit is ignored. Same at `now-10min`: counts, and the token doc's `sentAt` becomes that time.
  - `test_message_still_labeled_draft_is_not_sent`: `labelIds=["DRAFT"]`. Returns False, `preSendHits == 1`.
- [ ] **Step 2:** Run. Expected: the 4 new tests FAIL.
- [ ] **Step 3: Implement.** `SELF_VIEW_GRACE = timedelta(seconds=60)`. `_sent_at` checks in this order:
  - (a) stored `sentAt`
  - (b) `messages().get(id=gmailMessageId, format="minimal")`: `SENT` in labels and `DRAFT` not → `internalDate`, persisted to the token doc
  - (c) the existing draft-exists check, where a 404 means sent with unknown time, so return `now - SELF_VIEW_GRACE - 1s`
  - (d) the existing contact `emailSentAt` fallback

  In `record_open`, a hit where `now - sent_at < SELF_VIEW_GRACE` increments `selfViewHits` and returns False.
- [ ] **Step 4:** Run the whole file. Expected: all pass. Adjust `test_hit_after_draft_was_sent_counts`'s fixture only if it now falls inside the grace window. Commit `feat(tracking): don't count the sender's own render right after send (OFF-12)`.

### Task 3: `email_opened` pipeline stage (backend)

**Files:**
- Create: `backend/app/models/pipeline_stages.py`
- Modify: `services/email_tracking.py` (`record_open`)
- Modify, using the set instead of the old pair:
  - `services/outbox_service.py`: `ALLOWED_PIPELINE_STAGES` :25, `POST_SEND_STAGES` :53, the stats checks :475 and :484, the draft-gone transitions :762 and :784, the follow-up send transition :1047-1053
  - `services/background_sync.py:11`
  - `services/nudge_service.py:30`
  - `services/email_baseline.py:26`
  - `services/migration.py:10`
  - `services/scout/workflow_state.py:43`
  - `services/loop_service.py:999`
  - `routes/gmail_webhook.py:494`
- Test: `backend/tests/test_email_opened_stage.py`

**Interfaces:**
- Produces, in `app/models/pipeline_stages.py`:
  - `EMAIL_OPENED = "email_opened"`
  - `AWAITING_REPLY_STAGES = frozenset({"email_sent", "waiting_on_reply", "email_opened"})`
  - `stage_after_send_confirmed(contact: dict) -> str`: returns `"email_opened"` if `contact.get("emailOpenedAt")`, else `"waiting_on_reply"`

- [ ] **Step 1: Write failing tests.**
  - `test_open_promotes_waiting_on_reply`: contact `waiting_on_reply` + counted open → `pipelineStage == "email_opened"`. Same for `email_sent`.
  - `test_open_never_demotes_replied`: parametrize over `replied`, `meeting_scheduled`, `connected`, `no_response`, `bounced`, `closed`, `draft_created`. After the open the stage is unchanged, but `emailOpenCount` still increments.
  - `test_uncounted_hit_does_not_change_stage`: a pre-send or self-view hit leaves `waiting_on_reply`.
  - `test_webhook_send_confirm_respects_earlier_open`: `stage_after_send_confirmed({"emailOpenedAt": "…"}) == "email_opened"`, and `({}) == "waiting_on_reply"`. The webhook sent-detection path writes that value (drive it the way the existing webhook tests do).
  - `test_followup_send_from_opened_resets_to_waiting`: `outbox_service` send on an `email_opened` contact → `waiting_on_reply`.
  - `test_email_opened_is_awaiting_reply_everywhere`: `"email_opened"` is in `background_sync.STALE_PIPELINE_STAGES`, `nudge_service.NUDGE_ELIGIBLE_STAGES`, `outbox_service.ALLOWED_PIPELINE_STAGES`, `outbox_service.POST_SEND_STAGES`, `migration.PIPELINE_STAGES`, and `workflow_state._AWAITING_REPLY_STAGES`. Also, an `email_opened` contact counts as reply-rate eligible in the outbox stats.
- [ ] **Step 2:** Run `pytest backend/tests/test_email_opened_stage.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
  - `record_open`: in the same `contact_ref.update`, add `pipelineStage: "email_opened"` when the current stage is `email_sent` or `waiting_on_reply`.
  - Webhook :494 and outbox :762/:784 write `stage_after_send_confirmed(data)` instead of the literal.
  - Follow-up send: `email_opened` → `waiting_on_reply`.
  - Every listed set/tuple/`in (…)` check uses or includes `AWAITING_REPLY_STAGES`.
  - Leave unchanged: the webhook :488 skip list (an opened contact is already sent) and unarchive :604.
- [ ] **Step 4:** Run the new file plus `pytest backend -q`. Expected: no new failures vs. `origin/main` baseline. Re-grep `git grep -nE "['\"](email_sent|waiting_on_reply)['\"]" backend/app`. Every remaining hit should be a literal *write* of a fresh-send stage, not a check. Commit `feat(pipeline): email_opened stage, set by the tracking pixel (OFF-12)`.

### Task 4: Ship PR #218 to web prod (trial users only)

- [ ] **Step 1:** `pytest backend -q` (no new failures) and `npm run build` + `npm test` in `connect-grow-hire` (pass). Do this after Task 6a so the web UI understands the new stage before any contact reaches it.
- [ ] **Step 2:** Push, get approval, merge PR #218. Merging is Nick's call, so ask first.
- [ ] **Step 3:** On the web prod Render service set `EMAIL_OPEN_TRACKING_UIDS=<Nick's uid>` (API key in keychain `render-api-key`). Then `curl -sI https://offerloop.ai/api/t/o/x.gif` should return `200` and `image/gif`.

### Task 5: Port to the mobile backend (`staging/mobile-field`)

Both mobile Render services (offerloop-staging, offerloop-mobile-prod) deploy from this branch. Without the port, app-sent emails have no pixel, and the mobile backend's reply sync/nudges ignore `email_opened` contacts that web prod promoted (same Firestore). **So this must deploy before tracking is on for anyone who uses the app.**

- [ ] **Step 1:** Branch `nickwittig/off-12-mobile-field` from `origin/staging/mobile-field`. Cherry-pick `4c6fea95` and the Task 1–3 commits. The branches have diverged by ~240 commits, so resolve per file, keeping mobile-field's logic and adding only the tracking/stage lines. Line numbers differ: webhook send-confirm is at :425 here.
- [ ] **Step 2:** Re-run the Task 3 re-grep on this branch and handle the extra hits: `routes/mobile.py:1309` and `services/people_deck.py:1609` are fresh-send writes, so leave them.
- [ ] **Step 3:** Run `pytest backend/tests/test_email_tracking.py backend/tests/test_email_opened_stage.py -q`. Expected: all pass, including the guard test against this branch's send sites. Fix any site it names.
- [ ] **Step 4:** PR into `staging/mobile-field`. After merge, set `EMAIL_OPEN_TRACKING_UIDS=<Nick's uid>` and `EMAIL_TRACKING_BASE_URL=https://offerloop.ai` on both mobile services.

### Task 6: Opened step on the website

Two stage models exist: the live tracker (`pages/trackerAdapter.ts`, on `main`) and the redesign Contacts page (`components/contacts/contactsModel.ts` + `StageTracker.tsx`, branch `nickwittig/offerloop-web-redesign`). 6a goes on the OFF-12 branch. 6b goes on the redesign branch after rebasing it on `main`.

**Interfaces:**
- Consumes: `pipelineStage === "email_opened"`, `emailOpenedAt`, `emailLastOpenedAt`, `emailOpenCount` on `OutboxThread`.
- Produces: `PipelineStage` union in `services/api.ts` gains `"email_opened"`. Proto/journey stages become `["saved","drafted","contacted","opened","connected","interviewing","offer"]`.

- [ ] **6a Step 1: Failing tests** in `connect-grow-hire/src/pages/trackerAdapter.test.ts` (vitest):
  - `email_opened → stage "opened", status "Opened"`
  - `waiting_on_reply with emailOpenedAt set → "contacted"` (the stage field is now the source of truth)
  - `replied → "connected"`
  - `email_opened + hasUnreadReply → status "Reply"`
  - `protoStageToBackend("opened") === "email_opened"`
  - In a new `src/lib/suggestionEngine.test.ts`: an `email_opened` thread 8 days after `emailSentAt` with `followUpCount: 0` produces a `follow_up` card.
- [ ] **6a Step 2:** Run `npm test`. Expected: FAIL.
- [ ] **6a Step 3: Implement.**
  - Add `"email_opened"` to `PipelineStage`.
  - `trackerAdapter`: `stageOnlyMap` maps `email_opened` to `"opened"`, and `deriveStatus` returns `"Opened"` from the stage. Remove the PR's `emailOpenedAt`-derived status.
  - Treat `email_opened` like the sent pair in `ContactCard.tsx:33,63`, `shared/contactStatus.ts:48` (keeps the `Opened 2h ago` / `Opened 3 times, last 2h ago` wording), and `suggestionEngine.ts:104`.
  - `ConversationPanel.tsx` stage select: add `{ value: "email_opened", label: "Opened", disabled: true }` so the current value renders but can't be picked.
  - Disable the Opened dot in `TrackerBuckets.tsx` / `ProtoDetailHeader.tsx`, with tooltip `Opened <time ago>`. Give the bucket a color between Contacted and Connected.
- [ ] **6a Step 4:** Run `npm test` and `npm run build`. Expected: pass. Commit `feat(tracker): Opened step in the contact journey (OFF-12)`.
- [ ] **6b:** Same tests against `contactsModel.ts` `threadStage`. Add `opened` to `STAGES` / `STAGE_LABELS` / `stageStyle` / `stageToBackend`. In `StageTracker.tsx` the Opened chip is `disabled`, and the ring is now 7 steps. Commit on the redesign branch.

### Task 7: Opened in the mobile app

Repo `offerloop-mobile`, branch `nickwittig/off-12-opened-status`. There's no test runner, so don't add one. Verify with `tsc` here and live in Task 8.

- [ ] **Step 1: Inbox status.** Add `'opened'` to `OutreachStatus` (`src/types.ts:27`). `statusFor` (`src/api/endpoints/inbox.ts`) returns it for `pipelineStage === "email_opened"`, after the two reply checks. Required outcomes:
  - `email_opened → "opened"`
  - `email_opened + hasUnreadReply → "replied"`
  - `replied → "replied"`
- [ ] **Step 2: Every reader of `'sent'`.**
  - `(tabs)/inbox.tsx:75` `isWaiting` includes `'opened'` (still "Waiting on them"), so the Sent filter keeps it.
  - `(tabs)/feed.tsx:288` includes `'opened'`.
  - `company/[name].tsx:590` shows `Opened`.
  - `src/api/endpoints/contacts.ts:63` maps `email_opened` to `'waiting'`.
  - `mapThread` carries `openedAt` and `openCount`, both optional on `OutreachItem`. The detail screen `outreach/[id].tsx` shows `Opened 2h ago` / `Opened 3 times, last 2h ago`. The Inbox badge says `Opened`.
- [ ] **Step 3:** Run `npx tsc --noEmit`. Expected: clean, with every `Record<OutreachStatus, …>` / switch updated. Commit `feat(inbox): Opened status when a contact opens your email (OFF-12)`.

### Task 8: Live verification, then turn it on for everyone

- [ ] **Step 1:** From Nick's account, send to Gmail, iCloud (Apple Mail) and Outlook addresses: once as a direct send (Loop/queue), once as a draft sent from Gmail, and once from the mobile app.
- [ ] **Step 2: Check each one.**
  - The sender's own render lands in `selfViewHits` and the stage stays `waiting_on_reply`.
  - The recipient's open sets `emailOpenedAt` and `pipelineStage == "email_opened"`.
  - The website tracker, the redesign ring (if deployed) and the mobile Inbox all show Opened.
  - A reply moves it to `replied`.
  - Confirm the Task 2 assumption that a sent draft keeps its `gmailMessageId` with the `SENT` label. If it doesn't, switch `_sent_at` (b) to `find_sent_thread_for_recipient` and re-run Task 2 tests.
- [ ] **Step 3:** Set `EMAIL_OPEN_TRACKING=1` on web prod and both mobile services. Move OFF-12 to In Review with a summary comment.

---

**Not in this plan (decide separately):**
- **Notifying the user** ("Sarah opened your email"): wait until Task 8 shows how often Apple Mail preloads create false opens.
- **Chrome extension:** no stage UI exists there. Emails it creates already go through tracked backend paths.
- **Privacy policy:** one line saying outreach emails include open tracking (PR #87 or a follow-up).
- **Known limits:**
  - Apple Mail Privacy Protection preloads images, which creates false opens, and those will move the stage too.
  - Clients that block images never report an open.
  - If the sender rereads the email in Sent later, that still counts.
  - Reopening an older email in the thread can promote a later, unopened follow-up to `email_opened`.
