# Target-Employer Job Sources Implementation Plan (OFF-48)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Workday, Oracle and Oleeo job sources for the banks and big employers USC students target, keep each job's team, and recover the live jobs the daily cleanup deleted. All of it plugs into the existing direct-ATS crawl (fetch → normalize → quality gate → `sync_board_jobs`).

**Architecture:** A new `backend/pipeline/direct/` package holds a platform registry and one fetcher module per new system. `run_crawl_ats` looks platforms up in that registry. The new platforms crawl on their own 6-hour schedule through `--platform=`, so the existing 2-hour hot crawl doesn't change. Jobs keep the `direct_{platform}_{slug}_{id}` ID scheme, so the board diff, crawl state, OFF-53's cleanup rule and the auto-apply detector all work unchanged.

**Tech Stack:** Python 3.12, requests, Firestore, pytest, GitHub Actions (`.github/workflows/job-pipeline.yml`).

**Spec:** `docs/superpowers/specs/2026-10-20-early-career-jobs-and-hiring-team-design.md` (Part A), amended below. **This plan replaces** `docs/superpowers/plans/2026-10-20-early-career-job-database.md`.

## What it takes (summary for Nick)

- **Prerequisite:** OFF-53 (the cleanup fix) on `main`. Without it, every job these crawlers add is deleted 14 days later.
- **Build:** 12 tasks. About a day of agent work with reviews; most tasks are independent fetchers behind one interface.
- **Your gates (nothing writes to PROD or spends without these):**
  - Push OFF-53.
  - Approve the recovery run (Task 1).
  - Review the target-employer list (Task 7).
  - Approve the first live crawl (Task 8).
  - Approve the push to `main`.
  - Approve the team backfill (Task 11).
  - Confirm the Fantastic.jobs subscription (Task 12).
- **Money:**
  - The job systems are free public endpoints.
  - Firecrawl enrichment is about $0.003 per new early-career job, so 2,000 new jobs is about $6 once.
  - GitHub Actions: 4 extra runs a day of about 5 minutes, roughly 600 minutes a month. The repo is private, so check the plan's monthly allowance.
- **Expected yield:** roughly 1,500 to 3,000 more US early-career jobs at target employers. Probes found 64 at PIMCO, 116 at Barclays and 14 at Capital One, against 9, 5 and 4 in Simplify. Task 8's first run measures the real number.
- **Coverage:**
  - Workday: Citi, Morgan Stanley (tenant `ms` to verify), Wells Fargo, BofA experienced hires, PJT, Moelis, PwC, BlackRock, PIMCO, Vanguard, Barclays, Capital One, NVIDIA, Salesforce and about 450 Simplify tenants.
  - Oracle: JPMorgan, Lazard.
  - Oleeo: BofA campus, Jefferies, Evercore, Moelis campus.
  - Goldman: investigation (Task 9).
  - McKinsey, Bain and other sites that block us: entered by hand (Task 10).

## Spec amendments (from 2026-10-21 probes)

- The spec said "big employers mostly run on Workday". Top banks are split across Workday, Oracle and Oleeo; Goldman, McKinsey and Bain use their own sites. All three systems answered without a key.
- Workday's `searchText` is fuzzy: "intern" returns VP roles. Titles must be filtered in the fetcher.
- At banks, a plain "Analyst" title is the entry-level rank, but `quality_gate._EARLY_CAREER_TITLE` doesn't count it. Bank sources get `analyst_entry`.
- Bank programs stay open longer than `_MAX_AGE_DAYS = 90`. These sources get liveness from the board diff, so the age rule doesn't apply to them.
- The new sources live on a branch off `origin/main`, not the website branch. The scheduled crawl runs from `main`, and the website branch is 88 commits ahead of it.

## Global Constraints

- Branch `nickwittig/off-48-target-employer-sources` off `origin/main`, created **after OFF-53 is merged to `main`**. Worktree `/Users/nicholaswittig/Offerloop/Final_offerloop-off-48`. Commit locally; push only when Nick says.
- Tests: `cd <worktree> && PYTHONPATH=$PWD DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib /Users/nicholaswittig/Offerloop/.venvs/offerloop-backend/bin/python -m pytest backend/tests/<file> -q`. No live HTTP or Firestore in tests. Save real responses as fixtures under `backend/tests/fixtures/direct/`.
- `backend/tests/test_ats_pipeline.py` already fails 2 cold-mode tests on `main`. Don't count those as regressions.
- Local runs hit PROD Firestore. Any script or mode that writes in bulk does nothing without `--confirm` (scripts) or runs with `--dry-run` (crawler) until Nick approves.
- New platform names, exactly: `workday`, `oracle`, `oleeo`. Slugs match `^[a-z0-9-]+$` and are our own company keys (`citi`, `jpmorgan`). External IDs match `^[A-Za-z0-9-]+$`, with no `_` and no `/`.
- Early-career search terms, exactly: `("intern", "internship", "analyst", "new grad", "early career", "summer")`.
- Event postings to drop, case-insensitive: `coffee chat|info(rmation)? session|webinar|insight day|open day|networking event|virtual event`.
- New fetchers **raise `FetchError`** on any failure. They never return a partial or empty list for a board they couldn't read fully.
- User-Agent: reuse `fetcher.USER_AGENT`. At most 4 concurrent boards per new platform, with `_polite_sleep()` before each request.

## Review Focus

1. A detail call fails for one job on a board. Expected: `FetchError`, the crawl marks the board failed, and no job on it gets expired. (Tasks 4 to 6)
2. A job reaches us from both Simplify and a direct crawl. Expected: it appears once, from the direct crawl. (Task 7)
3. A non-US job whose location has no country word ("Warsaw", "Singapore  Singapore"). Expected: dropped, because fetchers append the country. (Tasks 4 to 6)
4. A bank program posted more than 90 days ago that's still open. Expected: kept. (Task 2)
5. An Oleeo "coffee chat" or "info session" posting. Expected: dropped. (Task 6)

---

### Task 1: Recover live jobs the cleanup deleted

**Files:**
- Create: `backend/scripts/recover_deleted_ats_jobs.py`
- Test: `backend/tests/test_recover_deleted_ats_jobs.py`

**Interfaces:**
- Produces: CLI `python -m backend.scripts.recover_deleted_ats_jobs [--confirm]`.
- How it works: for each `ats_crawl_state` doc of platform `greenhouse|lever|ashby`, find `kept_job_ids` with no `jobs/{id}` doc (batched `get_all`, 300 refs per batch). On `--confirm`, update that state doc to `kept_job_ids = kept - missing` and `board_hash = None`. The next scheduled crawl then sees those jobs as new and rewrites them. The script never writes `jobs` itself.

- [ ] **Step 1: Write failing tests** with a fake Firestore:
  - `test_dry_run_reports_and_writes_nothing`: 2 boards, 3 missing IDs. The return value is `{"boards": 2, "missing": 3, "updated": 0}` and there are no `update` calls.
  - `test_confirm_drops_missing_and_clears_hash`: the state doc is updated with only the present IDs and `board_hash=None`.
  - `test_board_with_nothing_missing_untouched`.
  - `test_second_run_noop`: after confirm, a rerun reports `missing == 0`.
- [ ] **Step 2: Run, expect FAIL** (module missing).
- [ ] **Step 3: Implement** `recover(db, confirm: bool) -> dict` plus a `main()` that bootstraps Firebase the way `pipeline.main._bootstrap_app` does.
- [ ] **Step 4: Run, expect PASS.** Then run the dry run locally and paste the per-platform counts in the report. Don't run `--confirm`; Nick runs it after OFF-53 is on `main`.
- [ ] **Step 5: Commit** `feat(scripts): recover live ATS jobs deleted by the TTL cleanup (OFF-48)`.

### Task 2: Direct-source registry and crawl plumbing

**Files:**
- Create: `backend/pipeline/direct/__init__.py`
- Modify: `backend/pipeline/main.py` (`run_crawl_ats` platforms map ~line 501; CLI `--crawl-ats` parsing ~line 664)
- Modify: `backend/pipeline/writer.py` (`_DIRECT_ATS_PREFIXES` from OFF-53 → registry)
- Modify: `backend/pipeline/normalizer.py` (`_normalize_board_job`: pass through `department`, `team`, `analyst_entry`; add `oracle`, `oleeo` to `normalize_job`'s source tuple)
- Modify: `backend/pipeline/quality_gate.py` (`_is_too_old` exemption, `compute_relevance_tier` analyst rule)
- Modify: `backend/pipeline/slug_loader.py` (add `get_record`)
- Test: `backend/tests/test_direct_registry.py`

**Interfaces:**
- Produces in `backend.pipeline.direct`:
  - `class FetchError(Exception)`
  - `LEGACY_PLATFORMS = ("greenhouse", "lever", "ashby")`
  - `TARGET_PLATFORMS = ("workday", "oracle", "oleeo")`
  - `ALL_PLATFORMS = LEGACY_PLATFORMS + TARGET_PLATFORMS`
  - `def get_fetcher(platform: str) -> Callable[[str], list[dict]]`: legacy names map to `fetcher._fetch_*`. Target names import `direct.workday.fetch` / `direct.oracle.fetch` / `direct.oleeo.fetch` lazily.
  - `def make_job_id(platform: str, slug: str, external_id: str) -> str`: returns `f"direct_{platform}_{slug}_{external_id}"`. Raises `ValueError` when slug or ID break the Global Constraints patterns.
  - Re-exports `is_candidate_title` and `BOARD_DIFF_SOURCES` from `quality_gate`. `direct` imports `quality_gate`, never the reverse, to avoid a circular import.
  - `def crawled_hosts() -> frozenset[str]`: hostnames of every `url` in the `workday`, `oracle` and `oleeo` CSVs.
- Produces in `quality_gate`:
  - `BOARD_DIFF_SOURCES = frozenset({"workday", "oracle", "oleeo", "goldman", "manual"})`: sources exempt from the 90-day age drop, because the board diff expires them.
  - `def is_candidate_title(title: str, analyst_entry: bool) -> bool`: True when the title matches `_EARLY_CAREER_TITLE`, or `analyst_entry` is set and the title has `\banalyst\b`; and it matches neither `_SENIOR_TITLE` nor `_EVENT_TITLE` (the event regex from Global Constraints).
- Produces in `slug_loader`: `def get_record(ats: str, slug: str) -> dict | None`, returning the CSV row (`name`, `slug`, `url`, `analyst_entry`).
- Produces in `run_crawl_ats(tier="hot", shard=None, platforms: tuple[str, ...] = LEGACY_PLATFORMS, dry_run: bool = False) -> dict`. With `dry_run`, it fetches, normalizes and gates as usual, skips `sync_board_jobs`, and logs 5 sample kept titles per board. CLI flags: `--platform=workday,oracle` and `--dry-run`.
- New CSV format for target platforms (`backend/pipeline/data/ats_companies/{workday,oracle,oleeo}.csv`): header `name,slug,url,analyst_entry`, where `analyst_entry` is `1` or `0`.

- [ ] **Step 1: Write failing tests:**
  - `test_no_circular_import`: `import backend.pipeline.quality_gate` and then `import backend.pipeline.direct`, in a fresh interpreter (`subprocess`), exits 0.
  - `test_job_id_rejects_underscore_and_slash`: `make_job_id("workday","citi","26978_591")` and `make_job_id("workday","ci/ti","1")` raise `ValueError`. `make_job_id("oracle","jpmorgan","210772971") == "direct_oracle_jpmorgan_210772971"`.
  - `test_cleanup_parses_new_platform_ids`: `writer._ats_board("direct_oleeo_jefferies_1991") == ("oleeo", "jefferies")`.
  - `test_candidate_titles`:
    - `is_candidate_title("2027 Summer Analyst Program", False)` → True.
    - `is_candidate_title("Analyst - Healthcare", True)` → True.
    - `is_candidate_title("Analyst - Healthcare", False)` → False.
    - `is_candidate_title("Senior Analyst - FIG", True)` → False.
    - `is_candidate_title("2028 Summer Analyst Coffee Chats", False)` → False.
  - `test_board_diff_sources_skip_age_drop`: a `workday` doc with `posted_at` 200 days ago passes `quality_gate.evaluate`. The same doc as `greenhouse` fails with `too_old`.
  - `test_analyst_entry_tier`: `{"title": "Investment Banking Analyst", "analyst_entry": True}` gets `compute_relevance_tier == 1`. Without the flag it's 3.
  - `test_normalizer_passes_team_fields`: `_normalize_board_job` keeps `department`, `team` and `analyst_entry` (defaults `""`, `""`, `False`). `normalize_job({"source": "oracle", ...})` uses the board normalizer.
  - `test_crawl_defaults_to_legacy_platforms`: with `get_fetcher` patched, `run_crawl_ats()` calls only greenhouse, lever and ashby.
  - `test_crawl_platform_filter_and_dry_run`: `run_crawl_ats(platforms=("workday",), dry_run=True)` calls the workday fetcher and never `writer.sync_board_jobs`.
  - `test_fetch_error_marks_failure_without_sync`: the fetcher raises `FetchError`, `crawl_state.mark_failure` is called, and `sync_board_jobs` isn't.
  - `test_target_platforms_never_auto_apply`: `ats_detector.is_eligible` is False for a job with ID `direct_workday_citi_1` and apply URL `https://citi.wd5.myworkdayjobs.com/...`, and likewise for an `oraclecloud.com` URL and a `tal.net` URL.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Writer: `_DIRECT_ATS_PREFIXES = tuple(f"direct_{p}_" for p in direct.ALL_PLATFORMS)`. In `compute_relevance_tier`, an `analyst_entry` doc whose title passes `is_candidate_title(title, True)` counts as early-career. Leave `test_ats_pipeline.py`'s existing behavior alone.
- [ ] **Step 4: Run, expect PASS**, plus `test_cleanup_keeps_live_ats_jobs.py` and `test_ats_pipeline.py` (apart from the 2 known failures).
- [ ] **Step 5: Commit** `feat(pipeline): direct-source registry, platform filter, dry run (OFF-48)`.

### Task 3: Keep department/team from Greenhouse, Lever and Ashby

Carried over unchanged from Task 1 of `2026-10-20-early-career-job-database.md`. Follow its tests and values exactly:
- Greenhouse: `departments[].name` joined with `" / "`.
- Lever: `categories.team` / `categories.department`.
- Ashby: `department` / `team`.
The normalizer pass-through is already done in Task 2.

- [ ] Steps 1-5 as in that plan. Commit `feat(pipeline): keep department/team on crawled jobs (OFF-48)`.

### Task 4: Workday fetcher

**Files:**
- Create: `backend/pipeline/direct/workday.py`
- Test: `backend/tests/test_direct_workday.py`, fixtures `backend/tests/fixtures/direct/workday_list.json` and `workday_detail.json` (saved from `citi.wd5` site `2`)

**Interfaces:**
- Consumes: `get_record`, `make_job_id`, `is_candidate_title`, `FetchError` (Task 2).
- Produces: `fetch(slug: str) -> list[dict]` in the raw shape of `fetcher._fetch_greenhouse`, plus `department`, `team` and `analyst_entry`. `source = board_platform = "workday"`.
- Endpoints, from the record `url` (`https://{tenant}.{wdN}.myworkdayjobs.com[/{locale}]/{site}`, where locale looks like `en-US` and is optional):
  - List: `POST https://{tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` with body `{"appliedFacets": {}, "limit": 20, "offset": n, "searchText": term}`. Page until fewer than 20 come back, or 100 per term.
  - Detail: `GET .../wday/cxs/{tenant}/{site}{externalPath}`, reading `jobPostingInfo`.
- Field map:
  - `title` from `title`
  - `location` = `f"{location}, {country.descriptor}"`
  - `posted_at` from `startDate`
  - `description_raw` = stripped `jobDescription`, at most 8000 characters
  - `apply_url` from `externalUrl`
  - external ID = `jobReqId` with characters outside `[A-Za-z0-9-]` replaced by `-`
  - `department` = `team` = `""`

- [ ] **Step 1: Write failing tests** (patch the module's session):
  - `test_searches_each_term`: 6 list POSTs, each with the exact body above, on the URL built from the record.
  - `test_locale_segment_ignored`: record URL `https://pwc.wd3.myworkdayjobs.com/en-US/Global_Campus_Careers` → site `Global_Campus_Careers`.
  - `test_filters_titles_before_detail`: a list page with "Vice President - Treasury" and "2027 Summer Analyst" fetches a detail only for the second.
  - `test_dedupes_external_path`: the same `externalPath` from two terms → one detail call, one job.
  - `test_detail_maps_fields`: from fixtures, the exact `job_id`, `location` ending `", Singapore"`, `posted_at == "2026-07-23"`, and `apply_url == externalUrl`.
  - `test_any_failure_raises`: a list 500 raises `FetchError`; one detail timeout raises `FetchError`; HTML instead of JSON raises `FetchError`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** with a module session built like `fetcher._build_session(4)`, but with `allowed_methods` including `POST`.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(pipeline): Workday fetcher (OFF-48)`.

### Task 5: Oracle fetcher

**Files:**
- Create: `backend/pipeline/direct/oracle.py`
- Test: `backend/tests/test_direct_oracle.py`, fixtures `oracle_list.json` and `oracle_detail.json` (from `jpmc.fa.oraclecloud.com` site `CX_1001`)

**Interfaces:**
- Produces: `fetch(slug: str) -> list[dict]`, same shape, with `source = "oracle"`.
- Endpoints, from the record `url` (`https://{host}/hcmUI/CandidateExperience/{lang}/sites/{site}`):
  - List: `GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true&expand=requisitionList&finder=findReqs;siteNumber={site},keyword={term},limit=25,offset={n}`. Rows are in `items[0].requisitionList`; page until fewer than 25 come back, or 100 per term.
  - Detail: `GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder=ById;Id="{Id}",siteNumber={site}`.
- Keep only list rows with `PrimaryLocationCountry == "US"` and a candidate title, before any detail call.
- Field map:
  - `title` from `Title`
  - `location` = `f"{PrimaryLocation}"` (it already ends with ", United States")
  - `posted_at` from `PostedDate`
  - `description_raw` = stripped `ExternalDescriptionStr + ExternalResponsibilitiesStr + ExternalQualificationsStr`, at most 8000 characters
  - `apply_url` = `f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{Id}"`
  - external ID = `Id`
  - `department` = first non-empty of `Department`, `Organization`, `JobFamily`, `JobFunction`
  - `team` = `""`

- [ ] **Step 1: Write failing tests:**
  - `test_non_us_rows_skipped_without_detail`
  - `test_detail_maps_fields`: exact values from the fixtures
  - `test_department_fallback_order`
  - `test_paging_stops_short_page`
  - `test_any_failure_raises`
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** (GET only, so `fetcher._build_session(4)` works as is).
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(pipeline): Oracle Recruiting fetcher (OFF-48)`.

### Task 6: Oleeo (tal.net) fetcher

**Files:**
- Create: `backend/pipeline/direct/oleeo.py`
- Test: `backend/tests/test_direct_oleeo.py`, fixtures `oleeo_board_jefferies.html`, `oleeo_board_evercore.html` and `oleeo_vacancy_jefferies.html`

**Interfaces:**
- Produces: `fetch(slug: str) -> list[dict]`, same shape, with `source = "oleeo"`.
- Board: the record `url` is the job-board listing page, without the session-like `xf-…` segment. For example: `https://evercore.tal.net/vx/mobile-0/appcentre-ext/brand-4/candidate/jobboard/vacancy/1/adv/`.
- Parsing:
  - Vacancy links are anchors whose `href` contains `/opp/{digits}-`; the anchor text, whitespace-collapsed, is the title.
  - Follow "next page" links on the same host, up to 5 pages.
  - Drop event titles and non-candidate titles before any vacancy fetch.
- Vacancy page:
  - `description_raw` = the visible text of the main content, stripped, at most 8000 characters.
  - `location` = the value after the label `Location`, else after `Region`.
  - `department` = the value after the first label present among `Division`, `Business Area`, `Business unit`, `Group`, `Department`.
  - Labels are matched case-insensitively on the stripped text.
  - `posted_at` = `None`.
  - `apply_url` = the vacancy URL.
  - external ID = the `opp` number.

- [ ] **Step 1: Write failing tests:**
  - `test_board_links_and_titles` (Jefferies fixture: exact count and first title)
  - `test_events_dropped` (Evercore fixture: no title contains "Coffee Chat")
  - `test_vacancy_fields` (exact `location` and `department` from the fixture)
  - `test_pagination_cap`
  - `test_any_failure_raises`
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** with the standard library `html.parser` or regex. No new dependency. Since `posted_at` is `None`, the normalizer stamps it with the current time and the board fingerprint changes every run. That's acceptable: `sync_board_jobs` still writes only new IDs.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(pipeline): Oleeo job-board fetcher (OFF-48)`.

### Task 7: Target-employer list and Simplify de-duplication

**Files:**
- Create: `backend/scripts/build_target_employers.py`
- Create: `backend/pipeline/data/ats_companies/workday.csv`, `oracle.csv`, `oleeo.csv`
- Modify: `backend/pipeline/fetcher.py` (`fetch_simplify`: skip listings whose `url` host is in `direct.crawled_hosts()`)
- Test: `backend/tests/test_target_employers.py`

**Interfaces:**
- The script builds the CSVs from three inputs:
  1. The Workday tenants in Simplify's live `listings.json` (URL in `fetcher.SIMPLIFY_LISTINGS_URL`).
  2. The names in `backend/app/services/find_contacts/large_employers.txt`, used to pick target tenants.
  3. This hand list (verified 2026-10-21):
     - Workday:
       - `citi` `https://citi.wd5.myworkdayjobs.com/2`
       - `wells-fargo` `https://wf.wd1.myworkdayjobs.com/WellsFargoJobs`
       - `bofa` `https://ghr.wd1.myworkdayjobs.com/lateral-us`
       - `pjt` `https://pjtpartners.wd1.myworkdayjobs.com/Careers`
       - `moelis` `https://moelis.wd1.myworkdayjobs.com/Experienced-Hires`
       - `pwc` `https://pwc.wd3.myworkdayjobs.com/Global_Campus_Careers`
       - `morgan-stanley` `https://ms.wd5.myworkdayjobs.com/External`: include only if a list call shows Morgan Stanley in a job's `externalUrl` or description.
     - Oracle:
       - `jpmorgan` `https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001`
       - `lazard` `https://icbpjb.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/LazardProfessionalCareers`
     - Oleeo:
       - `jefferies` `https://jefferies.tal.net/vx/lang-en-GB/mobile-0/appcentre-ext/brand-4/candidate/jobboard/vacancy/2/adv/`
       - `evercore` `https://evercore.tal.net/vx/mobile-0/appcentre-ext/brand-4/candidate/jobboard/vacancy/1/adv/`
       - `bofa-campus` `https://bankcampuscareers.tal.net/vx/lang-en-GB/mobile-0/brand-4/candidate/jobboard/vacancy/2/adv/`
       - `moelis-campus`: the job-board URL found from `https://moelis-careers.tal.net/vx/lang-en-GB/mobile-0/appcentre-1/brand-4/candidate`.
- `analyst_entry = 1` for banks, asset managers and the Big 4. The script marks names matching `bank|capital|securities|partners|asset|investment|sachs|morgan|lazard|evercore|jefferies|moelis|pjt|citi|wells|pwc|deloitte|kpmg|ey\b`; Nick reviews.
- Hot tier: every target employer gets a `{platform}:{slug}` line in `hot_slugs.txt`. Other Simplify Workday tenants go in `workday.csv` as cold-tier companies, crawled only on manual dispatch.
- The script makes one live list call per candidate and drops any board that errors or returns 0 jobs.

- [ ] **Step 1: Write failing tests:**
  - `test_crawled_hosts_from_csvs` (fixture CSVs → exact host set)
  - `test_simplify_skips_crawled_hosts` (two listings, one on `citi.wd5.myworkdayjobs.com` → one kept)
  - `test_script_slugify` (`"J.P. Morgan Chase & Co."` → `"jp-morgan-chase-co"`)
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**, then run the script locally to write the CSVs. It only reads public endpoints and writes local files.
- [ ] **Step 4: Run, expect PASS.** Paste into the report the row counts per platform and the list of hot-tier employers with their `analyst_entry` values. **Nick reviews the list before Task 8.**
- [ ] **Step 5: Commit** `feat(pipeline): target-employer boards for Workday/Oracle/Oleeo; Simplify de-dup (OFF-48)`.

### Task 8: Schedule the target crawl and do the first run

**Files:**
- Modify: `.github/workflows/job-pipeline.yml`:
  - Add cron `'20 */6 * * *'`, which maps to mode `crawl-ats-targets`.
  - Add `crawl-ats-targets` to the `workflow_dispatch` options.
  - Add the case `crawl-ats-targets) python -m backend.pipeline.main --crawl-ats --tier=hot --platform=workday,oracle,oleeo ;;`.
- Modify: `backend/pipeline/main.py` (`run_crawl_ats` totals gain `early_career = tier1 + tier2`, overall and per platform)
- Test: `backend/tests/test_ats_pipeline.py` (add `test_totals_report_early_career`)

- [ ] **Step 1: Write the failing test** (two kept docs, tiers 1 and 3 → `early_career == 1`).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** the totals field and the workflow changes.
- [ ] **Step 4: Run, expect PASS.** Then run `python -m backend.pipeline.main --crawl-ats --tier=hot --platform=workday,oracle,oleeo --dry-run` locally and paste into the report:
  - boards crawled and failed per platform
  - kept jobs per tier
  - 5 sample titles for each of Citi, JPMorgan and Jefferies
  - wall-clock time

  **Nick approves before the first run without `--dry-run`** (which writes to PROD).
- [ ] **Step 5: Commit** `feat(pipeline): 6-hourly target-employer crawl with early-career counts (OFF-48)`.

### Task 9: Goldman Sachs investigation (timeboxed, 1 hour)

`api-higher.gs.com/gateway/api/v1/graphql`, operation `GetRoles` with `"experiences": ["EARLY_CAREER"]`, answers without a key. On 2026-10-21 it returned 4 roles, none of them campus programs.

- [ ] **Step 1:** Find where Goldman's summer analyst and new analyst programs are posted. Check the `experiences` values in `GetRoleFilters`, the students pages on goldmansachs.com, and whether programs open seasonally.
- [ ] **Step 2:** If a stable JSON source lists **at least 10 US early-career roles**, write `backend/pipeline/direct/goldman.py` (`fetch(slug)`, `source = "goldman"`, added to `TARGET_PLATFORMS`) the same way as Task 5, with fixture tests. Otherwise add Goldman's program pages to Task 10's manual list.
- [ ] **Step 3:** If a fetcher was added, append `goldman` to the `--platform=` list in the workflow's `crawl-ats-targets` case.
- [ ] **Step 4: Commit** the fetcher or the manual rows, and put the finding in the report.

### Task 10: Hand-maintained jobs for sites we can't crawl

**Files:**
- Create: `backend/pipeline/data/manual_jobs.csv` (header `company,title,location,apply_url,department,analyst_entry`)
- Create: `backend/pipeline/direct/manual.py`
- Modify: `backend/pipeline/direct/__init__.py` (add `manual` to `TARGET_PLATFORMS`)
- Test: `backend/tests/test_direct_manual.py`

**Interfaces:**
- `fetch(slug: str) -> list[dict]` only accepts `slug == "all"`. It returns one raw job per CSV row:
  - external ID = the first 12 hex characters of `sha1(apply_url)`
  - `source = "manual"`
  - `posted_at = None`
  - `description_raw` = `f"{title} at {company}. Apply on the company's careers site."` (enough to pass the 50-character minimum)
- Liveness: deleting a row expires the job on the next crawl through the normal board diff. Add `manual:all` to `hot_slugs.txt`.
- Seed rows: McKinsey and Bain US campus pages (Business Analyst / Associate Consultant), filled in by Nick or the implementer from the firms' public pages.

- [ ] **Step 1: Write failing tests:**
  - `test_rows_become_jobs` (exact `job_id`)
  - `test_stable_ids` (same URL → same ID)
  - `test_removed_row_absent`
- [ ] **Steps 2-4:** Run (expect FAIL), implement, run (expect PASS).
- [ ] **Step 5:** Append `manual` to the `--platform=` list in the workflow's `crawl-ats-targets` case, then commit `feat(pipeline): hand-maintained jobs for uncrawlable employers (OFF-48)`.

### Task 11: Gated department/team backfill

Carried over from Task 4 of `2026-10-20-early-career-job-database.md`, unchanged. It covers tier 1 and 2 `greenhouse`, `lever` and `ashby` docs that are missing `department`/`team`, re-fetches each board once, uses `--confirm`, and Nick runs it.

- [ ] Steps 1-5 as in that plan.

### Task 12: Fantastic.jobs probe (capped, no writes)

Carried over from Task 5 of `2026-10-20-early-career-job-database.md`, unchanged. Capped at about $25 and run only after Nick confirms the subscription.

- [ ] Steps 1-5 as in that plan.

---

## Rollout order

1. OFF-53 on `main` (Nick pushes, or approves the push).
2. Task 1 dry run, then Nick confirms. Lost jobs return on the next 2-hour crawl.
3. Tasks 2 to 7. Nick reviews the Task 7 list.
4. Task 8 dry run, Nick approves, first live run. Check `pipeline_runs` and the job board.
5. Push to `main` (Nick approves). The 6-hour schedule takes over.
6. Tasks 9 and 10, then 11 and 12 when Nick says go.
