# Find Contacts Implementation Plan (OFF-49)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace "Find manager" on job cards with "Find contacts": for a job in our database, name the team behind it and return its leader, the person likely running interviews, and the recruiter, saved on the job for every later student.

**Architecture:** A new service package `backend/app/services/find_contacts/` runs the pipeline (read posting → size + playbook → tiered people search → score and fill → save). A new blueprint `backend/app/routes/find_contacts.py` exposes it by job id. Coresignal gets two direct query helpers that skip the prompt-search title filter. The legacy `find_hiring_manager()` keeps serving Loops, Scout, the public tool and pasted URLs, with its three bugs fixed. The frontend gets a `FindContactsModal` and a two-button job card.

**Tech Stack:** Flask blueprints, Firestore, OpenAI (`get_openai_client`, JSON mode), Coresignal ES DSL (`_search_ids`, `_collect_profiles_parallel`, `_normalize_to_contact`), FullEnrich (`fullenrich_search.search_contacts_from_prompt`, `fullenrich_client.find_work_email`), React 18 + TypeScript.

**Spec:** `docs/superpowers/specs/2026-10-20-early-career-jobs-and-hiring-team-design.md` (Part B)

**Where this plan differs from the spec (plan's calls):**
- Company size comes from a curated large-employer list, `linkedin_org_employees`, and the posting reader's hint, not a Coresignal company lookup (our Coresignal client has no company endpoint wired).
- Per Nick in chat, the job card goes to two buttons (`Find contacts`, `Auto-apply`); the card's "Email team" action is removed. The modal's other search kinds stay in the code.

## Global Constraints

- Work lands in `/Users/nicholaswittig/Offerloop/Final_offerloop` on `nickwittig/offerloop-web-redesign`. Commit locally, never push. Another Claude session edits this checkout: stage only your task's files.
- Local dev hits PROD Firestore and live vendor keys. Tests mock Coresignal, FullEnrich, OpenAI and Firestore. No live paid call except the gated Task 9 review run.
- Saved result lives at `jobs/{jobId}/contacts/result` (one doc). It is reused until the job doc is expired or deleted.
- Slots, in order: `leader`, `process`, `recruiter`. Labels, exact copy:
  - leader: `"Leads this team · can vouch for you"`
  - process: `"Likely runs interviews"`
  - recruiter: `"Scheduling and logistics"`
  - any person found by name in the posting: `"Named in the posting"` (replaces the slot label)
- Panel heading copy: `"{n} people to reach at {company}"`; team line: `"{team} · from the {source}"` where source is `job title`, `department field` or `description`; when no team: `"We couldn't pin down the team for this role"`.
- Job card buttons, exact labels: `Find contacts`, `Auto-apply`.
- Credits: 5 per person shown (`RECRUITER_CREDIT_COST`), charged once per student per job (record at `users/{uid}/contactUnlocks/{jobId}`), including when the result was already saved.
- Vendor spend cap per first search: at most 6 Coresignal profile collects and 1 FullEnrich search call.
- Startup = under 200 employees.
- Office city for search never comes from "Remote", "Hybrid", "Multiple locations", or region strings like "Europe - Remote".
- Emails are fetched only on "See email", per person, then saved on that person in the result doc. Do not change `email_fast.py` or the FullEnrich bulk/pending code (Sid's branch owns those).

## Review Focus

- Two students click "Find contacts" on the same job at the same moment: one search runs, the other waits for it (or gets "still searching, try again in a few seconds"), and neither is charged twice.
- A job whose posting names a person who isn't in Coresignal: the slot falls back to normal search, no crash, nobody shown as "Named in the posting".
- A Simplify or Workday job with no `department`/`team` and a thin description: the team line says we couldn't pin it down, and the function-level fallback still fills slots.
- A job with a non-US or remote location: search never filters people by that string.
- A student with too few credits: gets the existing "Insufficient credits" response before any vendor spend.

---

### Task 1: Fix the three bugs in the legacy finder

**Files:**
- Modify: `backend/app/services/coresignal_client.py` (`_build_es_query` ~247; honor `parsed["skip_title_guard"]`)
- Modify: `backend/app/services/recruiter_finder.py` (`_run_engine_tier_search` ~156; `determine_job_type` ~469)
- Create: `backend/app/utils/locations.py`
- Test: `backend/tests/test_hiring_manager_regressions.py`

**Interfaces:**
- Produces: `office_city_or_none(location: str | None) -> str | None` in `app/utils/locations.py` (returns the city part of "New York, NY" style strings; `None` for remote/hybrid/multiple/region-only strings).
- Produces: `parsed["skip_title_guard"] = True` makes `_build_es_query` use `title_variations` as given.

- [ ] **Step 1: Write failing tests:**
  - `test_tier_titles_not_gutted`: `_build_es_query({"companies":[{"name":"bybit"}], "title_variations":["staffing manager","director","vp"], "skip_title_guard": True})` contains all three in the title `should` clause.
  - `test_remote_location_dropped`: `office_city_or_none("Europe - Remote") is None`, `office_city_or_none("Remote") is None`, `office_city_or_none("New York, NY") == "New York"`, `office_city_or_none("Multiple Locations") is None`.
  - `test_engine_tier_search_passes_city_only`: `_run_engine_tier_search` with location `"Europe - Remote"` sends `locations == []`.
  - `test_job_type_whole_words`: `determine_job_type("Business Development Assistant", "Join our international team") == "sales"`; `determine_job_type("Summer Analyst Intern", "") == "intern"`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Keyword matching in `determine_job_type` uses `re.search(rf"\b{re.escape(k)}\b", text)`; `_run_engine_tier_search` sets `skip_title_guard` and uses `office_city_or_none`.
- [ ] **Step 4: Run, expect PASS**, and `tests/test_hiring_manager.py` still passes.
- [ ] **Step 5: Commit** `fix(hiring-manager): keep tier titles, drop non-city locations, whole-word job type (OFF-49)`.

### Task 2: Coresignal people queries for Find contacts

**Files:**
- Modify: `backend/app/services/coresignal_client.py`
- Test: `backend/tests/test_coresignal_people_queries.py`

**Interfaces:**
- Produces: `search_people(company: str, titles: list[str], keywords: list[str] | None = None, city: str | None = None, page_size: int = 10) -> list[int]` — ES ids for current employees (`experience.active_experience == 1`, `match_phrase` on `experience.company_name`), any title in `titles` (`match_phrase` on `experience.position_title`), every keyword required somewhere in the same experience's `position_title` (or headline when the field exists), `city` as `match_phrase` on `location_full` when given. Never applies `_distinctive_variations`.
- Produces: `find_person_by_name(full_name: str, company: str) -> dict | None` — one search (`match_phrase` on `full_name` + current company), collects the top hit, returns `_normalize_to_contact(...)` output or `None`.
- Produces: `collect_contacts(ids: list[int], limit: int) -> list[dict]` — collects at most `limit` ids via `_collect_profiles_parallel` and normalizes them.

- [ ] **Step 1: Write failing tests** asserting the exact query JSON sent to a mocked `_search_ids` for: titles only; titles + keywords; titles + city; and that `collect_contacts(ids=[1,2,3,4,5,6,7], limit=6)` collects 6.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(coresignal): direct people queries for Find contacts (OFF-49)`.

### Task 3: Read the posting

**Files:**
- Create: `backend/app/services/find_contacts/__init__.py`
- Create: `backend/app/services/find_contacts/posting_reader.py`
- Test: `backend/tests/find_contacts/test_posting_reader.py`

**Interfaces:**
- Produces: `read_posting(job: dict) -> dict` returning exactly these keys: `company: str`, `team: str`, `team_source: "job title" | "department field" | "description" | ""`, `function: str` (one of `ib`, `sales_trading`, `asset_management`, `consulting`, `software`, `product`, `data`, `marketing`, `strategy`, `finance`, `operations`, `other`), `office_city: str | None`, `level: "intern" | "analyst" | "associate" | "other"`, `is_internship: bool`, `named_people: list[{"name": str, "role": str}]`, `size_hint: "large" | "startup" | "unknown"`.
- `job` is a Firestore `jobs` doc dict (`title`, `company`, `location`, `department`, `team`, `description_raw`, `type`, `linkedin_org_employees`).

- [ ] **Step 1: Write failing tests** (mock the OpenAI client):
  - `test_department_field_wins`: job with `department: "Investment Banking"` → `team == "Investment Banking"`, `team_source == "department field"`, no LLM override of team.
  - `test_team_from_title`: title "OCTO Strategy Intern", LLM returns team "Office of the CTO", source "job title" → kept.
  - `test_office_city_sanitized`: LLM returns `"Remote"` → `office_city is None`.
  - `test_llm_failure_returns_fallback`: client raises → keys present, `team == ""`, `function == "other"`, `office_city` from `office_city_or_none(job["location"])`.
  - `test_unknown_function_coerced`: LLM returns `"banking stuff"` → `function == "other"`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** with one JSON-mode call (`temperature=0`, `max_tokens=500`, same pattern as `agent_brief_parser.py`), title + department + team + first 5000 chars of description. Cache the result on the job doc as `posting_facts` (write only from the service, Task 6).
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(find-contacts): read team, function, office and names from the posting (OFF-49)`.

### Task 4: Company size and playbooks

**Files:**
- Create: `backend/app/services/find_contacts/playbooks.py`
- Create: `backend/app/services/find_contacts/large_employers.txt` (one company per line: USC-target banks, asset managers, Big 4, consulting firms, Fortune 500)
- Test: `backend/tests/find_contacts/test_playbooks.py`

**Interfaces:**
- Produces: `company_size(facts: dict, job: dict) -> "large" | "startup"` — `large` if the company is in `large_employers.txt` (case-insensitive, canonicalized like `pipeline.normalizer.canonicalize_company`), or `linkedin_org_employees >= 200`, or `facts["size_hint"] == "large"`; else `startup`.
- Produces: `slot_specs(facts: dict, size: str) -> dict[str, dict]` keyed `leader`, `process`, `recruiter`; each `{"titles": list[str], "keywords": list[str]}` where `keywords` are the team's distinctive words (stop words and generic role words removed). Startups return `leader` and `process` only.
- Starting title sets (refine later from Task 9 results):

| function × size | leader titles | process titles | recruiter titles |
|---|---|---|---|
| ib / sales_trading / asset_management, large | Managing Director, Head of {team} | Vice President, Director, Associate | Campus Recruiting, Campus Recruiter, Early Careers Recruiter |
| software / product / data, large | Director, Head of {team}, VP {team} | Engineering Manager, Product Manager, Manager | University Recruiter, Early Careers Recruiter |
| strategy / marketing / finance / operations / other, large | Vice President, Head of {team}, Chief {X} Officer when team names one | Director, Senior Manager | Campus Recruiter, Early Talent Recruiter |
| consulting, large | Partner, Principal | Manager, Senior Associate | Campus Recruiter |
| any, startup | Founder, Co-Founder, CEO, Head of {function} | {function} Lead | — |

- [ ] **Step 1: Write failing tests:** `test_goldman_is_large`; `test_small_unknown_is_startup`; `test_ib_large_specs` (exact titles above with team "TMT" → `"Head of TMT"` present, keywords `["tmt"]`); `test_octo_specs` (team "Office of the CTO" → leader titles include `"Chief Technology Officer"`, keywords `["cto"]`); `test_startup_has_no_recruiter_slot`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(find-contacts): company size and function playbooks (OFF-49)`.

### Task 5: Tiered search and scoring

**Files:**
- Create: `backend/app/services/find_contacts/search.py`
- Create: `backend/app/services/find_contacts/scoring.py`
- Test: `backend/tests/find_contacts/test_search.py`, `backend/tests/find_contacts/test_scoring.py`

**Interfaces:**
- Consumes: Task 2 (`search_people`, `find_person_by_name`, `collect_contacts`), Task 4 (`slot_specs`), `fullenrich_search.search_contacts_from_prompt`, warehouse via `engine_search_contacts(parsed, n, allow_vendor=False, source="find_contacts")`.
- Produces: `find_slot_candidates(facts: dict, specs: dict, budget: Budget) -> dict[str, list[dict]]` — per slot, candidates each tagged `_found_by` in `named | warehouse | coresignal_r1 | coresignal_r2 | coresignal_r3 | coresignal_r4 | fullenrich`.
- Produces: `class Budget(collects: int = 6, fullenrich_calls: int = 1)` with `take_collects(n) -> int` and `take_fullenrich() -> bool`.
- Produces: `score(candidate: dict, slot: str, facts: dict, specs: dict) -> float` and `fill_slots(candidates: dict, facts: dict, specs: dict, student_school: str | None) -> list[dict]`, each output `{"slot", "label", "name", "title", "company", "city", "linkedin", "photo", "alumni": bool, "found_by", "person_key"}`.

- [ ] **Step 1: Write failing tests:**
  - `test_named_person_first`: `named_people=[{"name":"Kristen Rietow","role":"VP"}]` and `find_person_by_name` returns her → she fills `process` (VP) with label `"Named in the posting"`, `_found_by == "named"`.
  - `test_rounds_widen_in_order`: Coresignal mocks return nothing for team+city, then hits for team-any-city → `_found_by == "coresignal_r2"`, and round 3/4 never called.
  - `test_stops_when_slots_full_from_warehouse`: warehouse fills all slots → no Coresignal call.
  - `test_budget_caps_collects`: total collected across slots ≤ 6.
  - `test_fullenrich_fallback_once`: Coresignal empty → FullEnrich called exactly once.
  - `test_score_prefers_team_words_and_city`: candidate with team word in title + same city outranks one with neither.
  - `test_one_person_one_slot`: the same person never fills two slots.
  - `test_alumni_flag`: candidate `College` "University of Southern California" + student school "USC" → `alumni is True`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Rounds per slot: r1 `keywords + city`, r2 `keywords`, r3 function titles `+ city` without keywords, r4 function titles only. Score: +3 per team keyword in title, +2 seniority title match for the slot, +2 same city, +1 Coresignal `_coresignal_is_decision_maker` for `leader`; alumni does not change score. `person_key` = LinkedIn URL, else `first|last|company` lowercased.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(find-contacts): tiered people search and slot scoring (OFF-49)`.

### Task 6: Service with saved result, lock and step log

**Files:**
- Create: `backend/app/services/find_contacts/service.py`
- Test: `backend/tests/find_contacts/test_service.py`

**Interfaces:**
- Consumes: Tasks 3-5.
- Produces: `find_contacts_for_job(db, job_id: str, student_school: str | None) -> dict` returning `{"status": "ready" | "searching" | "not_found", "company", "team", "team_source", "people": [...]}`.
- Firestore: `jobs/{jobId}/contacts/result` holds `{status, company, team, team_source, people, steps, created_at}`; `steps` is `{slot: found_by | "empty"}`. Lock: a transaction sets `status: "searching"` with `lock_until = now + 60s`; a second caller during the lock gets `{"status": "searching"}`; an expired lock is retaken.
- Produces: `reveal_email(db, job_id: str, person_key: str) -> dict | None` — returns the saved email if present, else `fullenrich_client.find_work_email(...)`, saves `email`/`email_status` on that person, returns `{"email", "status"}` or `None`.

- [ ] **Step 1: Write failing tests** (fake Firestore): `test_returns_saved_without_search`; `test_first_call_searches_and_saves`; `test_concurrent_call_sees_searching`; `test_stale_lock_retaken`; `test_expired_job_not_found`; `test_reveal_email_saves_and_reuses` (second call makes no vendor call); `test_steps_logged`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** The student's school is never stored in the shared doc; `alumni` is computed per request from `people[].college` (store `college` per person).
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(find-contacts): saved per-job result with lock and email reveal (OFF-49)`.

### Task 7: Routes and credits

**Files:**
- Create: `backend/app/routes/find_contacts.py` (blueprint `find_contacts_bp`, prefix `/api/job-board`)
- Modify: `backend/wsgi.py` (register after `job_board_bp`, line ~283)
- Test: `backend/tests/test_find_contacts_routes.py`

**Interfaces:**
- Produces: `POST /api/job-board/find-contacts` body `{"jobId": str}` → `200 {status, company, team, team_source, people, creditsCharged, credits}`; `202 {"status":"searching"}`; `404` unknown/expired job; `402 {"error":"Insufficient credits"}`.
- Produces: `POST /api/job-board/find-contacts/email` body `{"jobId", "personKey"}` → `200 {"email","status"}` or `404`. No credit charge.
- Both `@require_firebase_auth`; same tier gate as `/find-hiring-manager`.

- [ ] **Step 1: Write failing tests:** `test_charges_5_per_person_once` (3 people → 15 credits; second call by the same uid → 0); `test_insufficient_credits_before_search` (search not called); `test_searching_returns_202`; `test_unknown_job_404`; `test_email_route_no_charge`; `test_requires_auth`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** using `deduct_credits_atomic(uid, n, "find_contacts")` after a successful result; write `users/{uid}/contactUnlocks/{jobId}` with `{charged, at}`. Credit check before search = `5 × 3` minimum.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(find-contacts): job-board routes with once-per-student credits (OFF-49)`.

### Task 8: Frontend — two-button card, modal, per-role drafts

**Files:**
- Modify: `connect-grow-hire/src/components/jobs/browse/BrowseJobCard.tsx` (~200-222: replace "Find manager" and "Email team" with one `Find contacts` action; keep `Auto-apply`)
- Modify: `connect-grow-hire/src/pages/BrowseJobsPage.tsx` (~304: `onFindContacts(j)` opens the new modal)
- Create: `connect-grow-hire/src/components/jobs/FindContactsModal.tsx`
- Modify: `connect-grow-hire/src/services/api.ts` (`findContacts(jobId)`, `revealContactEmail(jobId, personKey)`, types `FindContactsResponse`, `ContactSlotPerson`)
- Modify: `backend/app/services/reply_generation.py` (`batch_generate_emails`: honor `contact["OutreachRole"]`)
- Test: `backend/tests/test_outreach_role_prompt.py`

**Interfaces:**
- Consumes: Task 7 routes; `apiService.generateAndDraftEmails({ contacts })`.
- Produces: contacts sent to drafting carry `OutreachRole: "leader" | "process" | "recruiter"`.

- [ ] **Step 1: Write the failing backend test** `test_role_line_in_prompt`: for each role, the prompt built for that contact contains the matching line, exact copy:
  - leader: `"This person leads the team the role sits in. Ask for 15 minutes of advice about the team, not for a job."`
  - process: `"This person likely interviews for the role. Mention the student has applied and ask one specific question about the work."`
  - recruiter: `"This person coordinates recruiting. Ask a short logistics question about timeline or next steps."`
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** the prompt line, then the modal: heading, team line and labels per Global Constraints; rows in slot order leader → process → recruiter, the leader row tagged `Email first`; a row per person (photo, name, title, label, alumni badge, `See email`, `Draft`); a polling state when the route returns 202 (retry every 3s up to 60s, copy `"Finding the right people for this role…"`); the not-found copy when `people` is empty. Card shows exactly `Find contacts` and, when eligible, `Auto-apply`.
- [ ] **Step 4: Verify.** Backend test passes; `cd connect-grow-hire && npx tsc --noEmit -p tsconfig.app.json` shows no errors in the touched files; open localhost:8080 job board, check the card buttons and the modal on one job (vendor calls happen on click, so use one job only).
- [ ] **Step 5: Commit** `feat(job-board): Find contacts card button and modal with per-role drafts (OFF-49)`.

### Task 9: Gated review run on 50 postings

**Files:**
- Create: `backend/scripts/find_contacts_review.py`

**Interfaces:**
- Consumes: `find_contacts_for_job`.
- Produces: CLI `python backend/scripts/find_contacts_review.py [--confirm] [--n 50]` that picks tier-1/2 jobs across finance, tech, Fortune 500 and startups (balanced by `company_size` × `function`), runs the service, and writes a CSV to the scratchpad with job title, company, team, team source, each slot's person, label and `found_by`, and total vendor collects.

- [ ] **Step 1: Implement** with a dry run that only lists the chosen jobs and the estimated max spend (`n × 6` collects).
- [ ] **Step 2: Run the dry run** and report the job list and spend estimate. Nick approves before `--confirm`.
- [ ] **Step 3: Commit** `feat(scripts): Find contacts review run (OFF-49)`.
