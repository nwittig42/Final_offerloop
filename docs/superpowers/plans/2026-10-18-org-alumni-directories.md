# Org Alumni Directories Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A student who belongs to an org (pilot: USC TKE, 2,127 people already in Firestore) joins it in onboarding or Account Settings and instantly gets that org's alumni inside Find. They can use an org pill under the search bar or type "TKE alumni", and every org contact shows a badge like "USC TKE Alumni '17".

**Architecture:**
- Rylan's three collections stay as they are (`orgs`, `people`, `person_org_links`; see `backend/scripts/org_directory_README.md`).
- A new backend service joins them into contacts, cached in-process. The join covers the pilot's 2,127 records.
- Membership lives on `users/{uid}.orgMemberships` and is written only by the backend. Firestore rules block client writes.
- `POST /api/prompt-search` checks for an org target before the provider ladder:
  - If the target is an org the user belongs to, contacts come from the directory instead of Coresignal or the warehouse.
  - Everything downstream (dedup, email pass, credits, drafts) is unchanged.

**Tech Stack:** Flask + firebase-admin (backend), React 18 + TS + react-query (frontend), pytest.

**Spec:** the conversation of 2026-10-18 with Nick. Linear: OFF-42.

## Global Constraints

- Who it's for: current students and young job seekers reaching out to their org's alumni. Undergrads and deceased members never appear as contacts.
- Access is instant on join for v1: `status: "active"` at join time. The admin approval side comes later, so the status field must already exist.
- Credits are the same as normal search: `CREDIT_COSTS['find_contact']` (10) per contact returned, charged by the existing prompt-search path.
- Only members of an org get its directory. A non-member typing "TKE alumni" gets a normal provider search, never directory rows.
- Clients never read `orgs`, `people` or `person_org_links` directly. They are covered by the default-deny rule and accessed only through the backend.
- Badge copy: `"{shortName} Alumni '{YY}"`, where YY is the chapter year, falling back to class year, e.g. `USC TKE Alumni '17`. If neither year exists, the badge is `"{shortName} Alumni"`.
- Don't touch the provider ladder for non-org searches.

## Review Focus

1. **"TKE alumni" with nothing else.** The prompt parser would call that too vague and return 400. In org mode the parser result must never block the search.
2. **"TKE alumni at Goldman" when no alum matches.** The search must return an empty list with a message naming the org. It must not fall through to providers (that would bill people outside the directory).
3. **Repeat searches.** Members already in the user's Contacts must be skipped before the batch is cut, so the second search shows new people.
4. **A user whose orgMemberships entry is not "active"** (the future pending state) is treated as a non-member.
5. **Lost members with no recovered email or LinkedIn.** They are unreachable, so they're excluded rather than shown and billed.

---

### Task 1: Org directory service

**Files:**
- Create: `backend/app/services/org_directory.py`
- Test: `backend/tests/test_org_directory.py`

**Interfaces (Produces):**
- `MEMBERSHIPS_FIELD = "orgMemberships"`
- `list_orgs(db) -> list[dict]`: orgs docs with `joinable == True`. Each is `{id, name, shortName, school, type, memberCount, aliases}`.
- `active_org_ids(user_data: dict | None) -> list[str]`: ids whose membership `status == "active"`.
- `match_orgs_in_prompt(prompt: str, orgs: list[dict]) -> tuple[list[str], str]`:
  - Matches whole-word, case-insensitive aliases, plus `shortName` and `name`.
  - Returns the matched ids and the prompt with the alias removed, along with the filler words alumni/alum/alums/alumnus/brothers/members and any dangling leading or trailing connectors.
- `resolve_org_targets(prompt, filter_org_ids, user_data, orgs) -> tuple[list[str], str]`:
  - Targets are (filter ids ∪ prompt matches) ∩ the user's active ids.
  - The cleaned prompt is returned only when a prompt match was used; otherwise the original prompt comes back.
- `load_org_members(db, org_id) -> list[dict]`:
  - Joins `person_org_links` (where `orgId ==`) with `people` via `db.get_all`.
  - Cached for 15 minutes per org.
- `member_contact(member: dict, org: dict) -> dict | None`:
  - Returns an App-shape contact, or None when the member is deceased, an undergrad, or has no email and no LinkedIn.
  - Email uses `workEmail` first, then the directory `email`.
  - Company and Title come from `coresignalCompany`/`coresignalTitle`. Without those, Company is "" and `WorkSummary` is set to the raw headline.
  - Adds `OrgId`, `OrgBadge`, `source: "org_directory"`.
- `filter_by_query(contacts, parsed) -> list[dict]`:
  - Company names and title variations are case-insensitive substring matches against Company, Title and `OrgHeadline`. Within a dimension the values are OR'd; across dimensions they're AND'd.
  - Locations, schools and industries are ignored, because the directory has no such data.
- `rank(contacts) -> list[dict]`: verified email first, then any email, then most recent year first.
- `search_org_directory(db, org_ids, parsed, orgs, cap=200) -> list[dict]`: ranked contacts across the target orgs, deduped by email/LinkedIn.

Tests (all pure, with a fake db exposing `collection().where().stream()` and `get_all`):
- `test_match_strips_alias_and_filler`: "TKE alumni at Goldman Sachs" gives `["usc_tke"]` and `"at Goldman Sachs"`. "tke alumni" gives `""`.
- `test_alias_needs_word_boundary`: "tkexyz analysts" gives no match.
- `test_resolve_requires_active_membership`: a pending membership yields no targets. A non-member filter id yields no targets.
- `test_member_contact_excludes_deceased_undergrad_unreachable`
- `test_member_contact_prefers_work_email_and_badge`: badge `USC TKE Alumni '17`, Email equals workEmail.
- `test_filter_by_query_company_and_title`
- `test_rank_email_first_then_recent`

### Task 2: Org routes + membership writes

**Files:**
- Create: `backend/app/routes/orgs.py` (`orgs_bp`, prefix `/api/orgs`)
- Modify: `backend/wsgi.py` (import + register)
- Modify: `firestore.rules` (block client writes to `orgMemberships`)
- Test: `backend/tests/test_orgs_routes.py`

Routes (all `@require_firebase_auth`):
- `GET /api/orgs` returns `{orgs: [...list_orgs], memberships: active_org_ids(user)}`.
- `POST /api/orgs/<org_id>/join`:
  - Returns 404 unless the org exists and is joinable.
  - Otherwise sets `orgMemberships.<org_id> = {status: "active", joinedAt, source: "self_select"}` with merge, and returns `{ok, memberships}`.
- `POST /api/orgs/<org_id>/leave` deletes that key with `firestore.DELETE_FIELD`.

Tests: join on an unknown org returns 404; join writes a merge set with status active; leave deletes the field; GET returns memberships.

### Task 3: Prompt search org mode

**Files:**
- Modify: `backend/app/routes/runs.py`, `prompt_search()`
- Test: `backend/tests/test_org_directory.py` (`resolve` cases cover the decision); manual curl check

Steps:
- Before `parse_search_prompt_structured`, call `resolve_org_targets(prompt, data.get("filters", {}).get("organizations"), user_data, list_orgs(db))`. Wrap it in try/except so any failure means no org mode.
- In org mode:
  - Parse the cleaned prompt only when it's 3+ characters. Ignore parser errors and low confidence, falling back to an empty parse.
  - `contacts` are the directory hits, minus `_contact_already_exists` matches against the exclusion sets, trimmed to `max_contacts`.
  - Skip the firm cache, the provider ladder, the Hunter bridge and the warehouse feed.
  - If nothing matches, return 200 with `contacts: []` and the message `No {shortName} alumni matched that. Try fewer filters.`
- Set `adjacency_metadata = {"provider": "org_directory", "org_ids": [...]}` and add `org_ids` to `parsed_query_payload`.

### Task 4: Seed the USC TKE org record

**Files:**
- Create: `backend/scripts/org_directory_seed_org.py`

The script runs `--cred <json> --org-id usc_tke --short-name "USC TKE" --school "University of Southern California" --alias tke --alias "tau kappa epsilon" --alias teke [--dry-run|--live]` and sets `shortName`, `school`, `aliases` and `joinable: true` with merge. **It's run by Nick, not by the implementer** because it writes to production.

### Task 5: Frontend data layer + search filter + badge

**Files:**
- Modify: `connect-grow-hire/src/types/findFilters.ts`: add `organizations: string[]` (org ids) to `PeopleFilters`, `EMPTY_PEOPLE_FILTERS` and `peopleFiltersActive`.
- Modify: `connect-grow-hire/src/services/api.ts`:
  - Add the `Org` type plus `getOrgs()`, `joinOrg(id)` and `leaveOrg(id)`.
  - Add `OrgBadge?`/`OrgId?` to `Contact`.
- Create: `connect-grow-hire/src/hooks/useOrgs.ts`: react-query `["orgs"]`, with join/leave mutations that invalidate it.
- Modify: `components/find/people/PeopleSearchBar.tsx`:
  - Add one toggle pill per joined org (label `{shortName} alumni`) next to the Alumni pill.
  - Add the prop `orgs: Org[]` (the joined ones).
  - Don't add a GROUPS entry, because orgs aren't free text.
- Modify: `pages/FindPage.tsx`:
  - Pass the joined orgs to the search bar.
  - `promptFromFilters` adds `"{shortName} alumni"` when the organizations filter is set.
- Modify: `components/find/people/peopleModel.ts`: add `orgBadge: string` to Person (`""` from the ranker).
- Modify: `components/find/people/PersonCard.tsx`: render the badge chip first in the chip row when it's set.

Verify: `npx tsc --noEmit -p tsconfig.app.json` is clean for the touched files, and `npm run build` succeeds.

### Task 6: Org picker in Account Settings and onboarding

**Files:**
- Create: `connect-grow-hire/src/components/OrgPicker.tsx`: a list of joinable orgs (name and school) with Join/Joined toggles, using `useOrgs`. It also accepts a controlled mode (`value`/`onChange`) for onboarding.
- Modify: `pages/AccountSettings.tsx`: add an "Organizations" block after the University field. Joining or leaving saves immediately, so it isn't part of the Save button.
- Modify: `pages/OnboardingFlow.tsx`:
  - Add a new `"orgs"` step after source/builder and before inbox. It shares rail index 1, the way `builder` does.
  - The step is shown only when `getOrgs()` returns at least one org.
  - The picks are joined via `joinOrg` after `completeOnboarding` succeeds, because the user doc is written then.

Verify: tsc and build clean, then a manual run-through.
