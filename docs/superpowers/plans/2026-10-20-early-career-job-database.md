# Early-Career Job Database Implementation Plan (OFF-48)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Workday as a job source for USC-target employers, keep each job's department/team, backfill team on existing early-career jobs, and measure Fantastic.jobs before paying for it.

**Architecture:** Workday becomes a fourth direct-ATS platform in `backend/pipeline/` beside Greenhouse/Lever/Ashby, using the same fetch → normalize → `sync_board_jobs` path in `run_crawl_ats`. Team fields are captured in each fetcher and passed through the normalizer. The backfill and Fantastic.jobs probe are standalone scripts that refuse to write or spend without `--confirm`.

**Tech Stack:** Python 3.12, requests, Firestore (google-cloud-firestore), pytest, GitHub Actions (`.github/workflows/job-pipeline.yml`).

**Spec:** `docs/superpowers/specs/2026-10-20-early-career-jobs-and-hiring-team-design.md` (Part A)

## Global Constraints

- Work lands in `/Users/nicholaswittig/Offerloop/Final_offerloop` on `nickwittig/offerloop-web-redesign`. Commit locally, never push. Another Claude session edits this checkout: stage only the files your task names.
- Local runs hit PROD Firestore (`offerloop-native`). Any script that writes in bulk or calls a paid API must do nothing without `--confirm`, and default to a dry run that prints what it would do.
- Tests: `cd backend && PYTHONPATH=$PWD/.. DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib /Users/nicholaswittig/Offerloop/.venvs/offerloop-backend/bin/python -m pytest <path> -q`. No live HTTP or Firestore in tests; mock both.
- Fantastic.jobs stays off in the scheduled pipeline (default `full` mode already passes `skip_fantastic=True`; do not change that).
- Normalized team fields are named exactly `department` and `team` (strings, `""` when absent).
- Workday searches use only early-career text: `"intern"`, `"internship"`, `"analyst"`, `"new grad"`, `"early career"`, `"summer"`.

## Review Focus

- A Workday tenant that 404s, times out, or returns HTML: the crawl of other tenants continues and the slug is marked failed (`crawl_state.mark_failure`).
- The same Workday posting returned by two search terms: written once (dedupe by `externalPath`).
- Greenhouse jobs with several departments: `department` joins names with `" / "`, never crashes on an empty list.
- Backfill script run twice: second run writes nothing (only docs whose `department`/`team` are missing are touched).
- Fantastic.jobs probe with no `RAPIDAPI_KEY`: exits with a clear message, spends nothing.

---

### Task 1: Keep department/team from Greenhouse, Lever and Ashby

**Files:**
- Modify: `backend/pipeline/fetcher.py` (`_fetch_greenhouse` ~106-152, `_fetch_lever` ~172-224, `_fetch_ashby` ~292-336)
- Modify: `backend/pipeline/normalizer.py` (`_normalize_board_job` ~689-758)
- Test: `backend/tests/test_job_team_fields.py`

**Interfaces:**
- Produces: raw fetcher dicts and normalized docs carry `department: str` and `team: str`.

- [ ] **Step 1: Write failing tests** with mocked HTTP responses:
  - `test_greenhouse_departments_joined`: a job with `departments: [{"name": "Investment Banking"}, {"name": "TMT"}]` → `department == "Investment Banking / TMT"`, `team == ""`.
  - `test_greenhouse_no_departments`: `departments: []` → `department == ""`.
  - `test_lever_categories_team`: `categories: {"team": "Payments Platform", "department": "Engineering"}` → `team == "Payments Platform"`, `department == "Engineering"`.
  - `test_ashby_department_and_team`: `{"department": "Strategy", "team": "Office of the CTO"}` → both kept.
  - `test_normalized_doc_keeps_team_fields`: `_normalize_board_job` on a raw dict with both fields → output has them; raw without them → both `""`.
- [ ] **Step 2: Run, expect FAIL** (`KeyError`/missing keys).
- [ ] **Step 3: Implement.** Add the two keys in each fetcher's job dict and copy them in `_normalize_board_job` with `str(raw.get(...) or "")`.
- [ ] **Step 4: Run, expect PASS**, plus `tests/test_ats_pipeline.py` still passes.
- [ ] **Step 5: Commit** `feat(pipeline): keep department/team on crawled jobs (OFF-48)`.

### Task 2: Workday fetcher and tenant list

**Files:**
- Create: `backend/pipeline/workday.py`
- Create: `backend/pipeline/data/ats_companies/workday.csv` (header `company,tenant,wd_host,site`)
- Modify: `backend/pipeline/slug_loader.py` (load `workday` records; slug = `tenant/site`)
- Test: `backend/tests/test_workday_fetcher.py`

**Interfaces:**
- Produces: `fetch_workday(slug: str) -> list[dict]` where `slug` is `"{tenant}/{site}"`, returning raw dicts shaped like the other fetchers (`job_id = f"direct_workday_{tenant}_{site}_{externalPath-slug}"`, `source = board_platform = "workday"`, `board_slug`, `title`, `company`, `location`, `remote`, `description_raw` ≤ 8000 chars, `apply_url`, `posted_at`, `department`, `team`, salary fields `None`).
- Produces: `slug_loader.load_slugs("workday", tier=...)` and `get_company_name("workday", slug)`.

- [ ] **Step 1: Write failing tests** (mock `requests.Session.post/get`):
  - `test_lists_with_early_career_terms`: list endpoint `POST https://{tenant}.{wd_host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` called once per term with body `{"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term}`.
  - `test_dedupes_external_path`: same `externalPath` from two terms → one job.
  - `test_detail_maps_fields`: detail `GET .../job/{externalPath}` `jobPostingInfo` (`title`, `jobDescription` HTML, `location`, `startDate`/`postedOn`, `externalUrl`) → stripped description, `apply_url == externalUrl`.
  - `test_tenant_error_returns_empty`: 404 or timeout → `[]`, no raise.
  - `test_slug_loader_reads_workday_csv`: a two-row CSV → two slugs, company names resolve.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** `fetch_workday` with a pooled session like `_build_session`, paging by `offset` up to 100 postings per term, detail fetch only for new `externalPath`s in this call. Seed `workday.csv` with the Workday tenants found in Simplify apply links (`myworkdayjobs.com` hosts in Firestore `jobs` where `source == "simplify"`; read-only query) plus a hand-picked list of USC-target employers; mark the curated ones hot in `hot_slugs.txt` as `workday:{tenant}/{site}`.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(pipeline): Workday fetcher and target-employer tenants (OFF-48)`.

### Task 3: Crawl Workday in run_crawl_ats and report target-employer counts

**Files:**
- Modify: `backend/pipeline/main.py` (`run_crawl_ats` platforms map ~501; `_write_run_log`)
- Modify: `backend/pipeline/writer.py` (`sync_board_jobs` docstring/platform list only if it validates platform names)
- Modify: `.github/workflows/job-pipeline.yml` (add `crawl-ats-workday` to the mode list and run it in `full` mode after the hot crawl)
- Test: `backend/tests/test_ats_pipeline.py` (add cases)

**Interfaces:**
- Consumes: `workday.fetch_workday`, `slug_loader.load_slugs("workday", ...)` (Task 2).
- Produces: `python -m backend.pipeline.main --crawl-ats --tier=hot --platform=workday`; `pipeline_runs` docs gain `early_career_written: int` and `target_employer_written: int`.

- [ ] **Step 1: Write failing tests:** `test_crawl_includes_workday_platform` (mock fetcher → `sync_board_jobs` called with `platform="workday"`), `test_run_log_counts_early_career` (two written docs with `relevance_tier` 1 and 3 → `early_career_written == 1`).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Add `"workday": workday.fetch_workday` to `platforms`; optional `--platform=` filter; count tiers 1-2 and slugs listed in `workday.csv`/`hot_slugs.txt` as target employers.
- [ ] **Step 4: Run, expect PASS**, then a dry local crawl of 2 tenants with Firestore writes mocked out (`--dry-run` flag that skips `sync_board_jobs`) and check the printed jobs look right.
- [ ] **Step 5: Commit** `feat(pipeline): crawl Workday tenants, log early-career counts (OFF-48)`.

### Task 4: Gated backfill of department/team on existing early-career jobs

**Files:**
- Create: `backend/scripts/backfill_job_team_fields.py`
- Test: `backend/tests/test_backfill_job_team_fields.py`

**Interfaces:**
- Consumes: Task 1 fetchers.
- Produces: CLI `python backend/scripts/backfill_job_team_fields.py [--confirm] [--limit N]`.

- [ ] **Step 1: Write failing tests** with a fake Firestore: `test_dry_run_writes_nothing`; `test_only_tier_1_2_missing_fields` (a tier-3 doc and a doc that already has `department` are skipped); `test_groups_by_board` (re-fetches each `(source, board_slug)` once, matches jobs by `job_id`, updates only `department`/`team`); `test_second_run_noop`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Query `relevance_tier in [1, 2]` and `source in ["greenhouse","lever","ashby"]`; update with `merge=True` in batches of 400.
- [ ] **Step 4: Run, expect PASS.** Run the dry run locally and paste the counts (docs to update, boards to fetch) in the task report. Do not run `--confirm`; Nick runs it.
- [ ] **Step 5: Commit** `feat(scripts): gated department/team backfill for early-career jobs (OFF-48)`.

### Task 5: Fantastic.jobs probe (capped, no writes)

**Files:**
- Create: `backend/scripts/fantastic_jobs_probe.py`
- Test: `backend/tests/test_fantastic_jobs_probe.py`

**Interfaces:**
- Consumes: `fetcher._fj_fetch_page`, `fetcher._normalize_fj_job`, `fetcher.FANTASTICJOBS_DEFAULT_PARAMS`.
- Produces: CLI `python backend/scripts/fantastic_jobs_probe.py [--confirm] [--max-requests 10]`, printing: jobs returned, early-career share, share already in Firestore (by `apply_url`), share with `ai_hiring_manager_name`, and requests used.

- [ ] **Step 1: Write failing tests:** `test_no_key_exits_cleanly`; `test_without_confirm_makes_no_requests`; `test_stops_at_max_requests`; `test_report_counts` (fixture pages → exact counts above).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Early-career title filter on the request (`title_filter` with the Global Constraints terms); never calls the writer.
- [ ] **Step 4: Run, expect PASS.** Do not run `--confirm`; Nick confirms the subscription first (cap ~$25).
- [ ] **Step 5: Commit** `feat(scripts): capped Fantastic.jobs coverage probe (OFF-48)`.

---

Later (only after Tasks 3 and 5 report numbers): extra GitHub internship lists, SmartRecruiters and Workable fetchers, growing Greenhouse/Lever/Ashby company lists. Each gets its own plan.
