# Early-career job database + "who runs this internship" finder

Date: 2026-10-20
Owner: Nick
Status: approved by Nick 2026-10-20 (person-finder flow approved in chat; open calls listed at the end)

## Why

Offerloop is an early-career tool. USC students target internships and entry-level roles at big companies: finance (banks, asset managers), tech, and the Fortune 500. Two things are failing them today.

1. **The job database is thin where they look.** We hold 74,552 jobs, but only ~7,800 are early-career (relevance tier 1 or 2) and ~4,200 are internships. We crawl Greenhouse, Lever and Ashby for ~10,000 companies, which skews to startups and tech. The big employers our students want mostly run on Workday, Oracle, iCIMS, SuccessFactors and their own career sites, which we barely cover (only through the Simplify internship list, 2,718 jobs, a third of them Workday links).
2. **"Find Hiring Managers" doesn't find the person in charge.** It searches the company for generic titles (recruiter, director, VP), and since the Coresignal switch it returns nothing at all: a title filter built for prompt search strips each tier to one literal title ("hiring manager"), the job's location ("Europe - Remote") is required as an exact match on where people live, and the job category is guessed by substring ("international" counts as "intern").

### The real-world model (from Nick)

Nick's OCTO Strategy internship at Pearson: the job title named the team (OCTO = Office of the CTO). A VP inside that office, Kristen Rietow, ran the interview process. Nick reached out to the CTO, her boss, who recommended him, and that got him the job.

So the person a student needs is not one "hiring manager". It is **the chain for the team the role sits in**: the team leader who can vouch for them, and the person who actually runs the interviews. The campus recruiter has a part (logistics, scheduling) but usually is not the one deciding. The strongest clue to the team is in the posting itself: the title, the department, the description.

## Goals

- More early-career and internship postings from the employers USC students target, especially big-company finance, tech and Fortune 500.
- For any early-career posting in our database, name the team behind it and the people who run and decide it, with an honest label for how sure we are.
- Work with our data and vendors only: our own job dataset, Coresignal for people, FullEnrich for emails (and FullEnrich people search as a cheaper fallback).

## Non-goals

- LinkedIn as a job source or as the primary person source. Our jobs do not come from LinkedIn.
- Mid-career and senior roles. Tier-3 jobs stay as they are.
- Rebuilding the whole job board UI. Only the hiring-team result changes.

## Anti-goals (failure even if it "works")

- Showing a confident "hiring manager" who is really a guess. Every person carries a label saying how we know.
- Defaulting big-company internships to "the campus recruiter" as the answer.
- Paying for discovery or emails before the student asks (no vendor spend on page load).
- Breaking the existing crawl or feed while adding sources.

## Constraints

- People discovery: Coresignal (warehouse first, then paid search). FullEnrich `/people/search` may be used as a cheaper fallback rung (0.25 credits/row, no photos).
- Emails: FullEnrich (and Hunter as the fast rung, once Sid's fix that actually calls Hunter is merged).
- Runs on the existing pipeline (`backend/pipeline/`) and the existing `/api/job-board/find-hiring-manager` route.
- Local dev hits PROD Firestore; anything that writes in bulk must be gated and run deliberately.
- Sid's branch `fix/search-email-pipeline` (email pipeline fixes: Hunter rung, FullEnrich pending sweep, enrichment order) is not pushed yet. Part B's email step assumes it lands; do not duplicate those fixes.

---

## Part A: Grow the early-career job database

Order matters: A1 and A2 first, they decide the rest.

### A1. Workday crawler (new source)

Workday career sites expose a public jobs endpoint that needs no key:

- List: `POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` with a JSON body (search text, facets, limit, offset).
- Detail: `GET .../wday/cxs/{tenant}/{site}/job/{externalPath}` returns the full description, time type, requisition id and posting date.

Build a `workday` fetcher alongside Greenhouse/Lever/Ashby in `pipeline/fetcher.py`, writing the same normalized shape with `source: "workday"`.

**Which employers:** a curated target list, not a crawl of all of Workday. Seed it from:
- the Workday tenants already in Simplify's apply links (we have ~125 in the sample alone), and
- a hand-picked list of USC-target employers: bulge-bracket and middle-market banks, asset managers, Big 4, consulting, Fortune 500 tech and consumer.

Store each tenant as `{company, tenant, wd_host, site}` in `pipeline/data/ats_companies/workday.csv`.

**Early-career focus:** search each tenant with early-career text ("intern", "internship", "analyst", "new grad", "early career", "summer") rather than pulling every job, then run the existing quality gate and relevance tiering.

**Keep the team fields** Workday gives us (see A3).

### A2. Fantastic.jobs test (existing code, never run)

The fetcher and normalizer already exist (`fetch_fantasticjobs`, RapidAPI `active-jobs-db`) and map `ai_hiring_manager_name` / `ai_hiring_manager_email`. Not one Fantastic.jobs job is in Firestore today.

Run one small, capped test (filtered to early-career titles and target employers), and measure:
- how many new early-career jobs it adds that we don't already have,
- how often `ai_hiring_manager_name` is filled,
- credit cost per useful job.

Decide from the numbers whether to schedule it. This is a decision point, not a commitment.

### A3. Save the team on every job (all sources)

Every ATS we crawl already sends the department or team in the data we download, and we throw it away. Add to the normalized job doc:

- `department` and `team` (strings, may be empty)
- Source mapping: Greenhouse `departments[].name`; Lever `categories.team` and `categories.department`; Ashby `department` and `team`; Workday the job's organization/category fields where present; Simplify none (falls to Part B's description reading).

Backfill happens naturally on the next crawl of each company, since every job is re-fetched. No separate migration.

### A4. More internship lists and ATS sources (after A1/A2 numbers)

In priority order, only if A1/A2 leave gaps:
- Other public GitHub internship and new-grad lists alongside Simplify's.
- SmartRecruiters and Workable public endpoints.
- Growing the Greenhouse/Lever/Ashby company lists (adds mostly startups and tech).

### Part A success

- Early-career (tier 1+2) jobs and internships at USC-target employers clearly up from today's ~7,800 / ~4,200, with the target-employer share reported per run in `pipeline_runs`.
- Every new or re-crawled job from Greenhouse, Lever, Ashby and Workday carries `department`/`team` when the source has one.

---

## Part B: Find the people who run a posting

### What the student sees

Each job shows two buttons: **[Auto apply] [Find contacts]**. "Find contacts" replaces "Find Hiring Managers"; the name says that clicking it hands you people.

After the click, a panel:

> **3 people to reach at Pearson**
> Office of the CTO · from the job title
> 1. **Kristen Rietow** · VP, Strategy · *Likely runs interviews*
> 2. **[CTO]** · Chief Technology Officer · *Leads this team, can vouch for you*
> 3. **[Recruiter]** · Early Careers Recruiter · *Scheduling and logistics*

Rules:
- The team line says where it came from (job title, department field, description). If we can't pin a team, say so instead of guessing.
- Up to three slots: **team leader** ("Leads this team, can vouch for you"), **likely runs the process** ("Likely runs interviews", always "likely"), **recruiter** ("Scheduling and logistics", shown last). A person found in the posting is labeled "Named in the posting".
- "Hiring team" wording is used only when the posting names someone.
- Alumni from the student's school get a badge wherever they appear. Startups may show one or two people (often a founder).
- Suggested outreach order: team leader first (a short note asking for advice, not a job), then the person running the process after they apply, the recruiter only for logistics. Each person gets a draft written for their role.
- The separate Recruiter and Employee searches in the modal stay as they are.

### Saved per job

The search runs the first time any student clicks "Find contacts" on a job. The result is saved on that job and served instantly to every later student, until the job expires or is removed. A lock prevents two simultaneous first clicks from both running it. Emails are looked up only when a student clicks "See email" on a person, and saved per person.

### Pipeline (first click on a job)

**Step 0: Saved result.** Return it if present.

**Step 1: Read the posting.** One LLM pass over title + department/team field + description returns:
- `company` and `team` (e.g. "Office of the CTO", "Investment Banking – TMT") and where the team came from
- `function` (IB, sales & trading, asset management, consulting, software, product, data, marketing, strategy, operations, ...)
- `office_city` (a real city; "Remote"/"Europe - Remote" never becomes a person filter)
- `level` (intern, analyst/new grad, associate) and whether it's an internship program
- any named person or "reports to" line

Cached on the job.

**Step 2: Size up the company.** Coresignal company lookup for headcount and name variants ("Goldman Sachs" / "Goldman Sachs & Co."). Under ~200 people = startup. Fantastic.jobs `linkedin_org_employees` is used when present.

**Step 3: Pick the playbook** for function × size (table below). It gives, per slot, the titles and seniority to search, built from the team's own words. Example, Goldman IB summer analyst in New York: leader = Managing Director / head of that group; runs the process = VP or Associate in the group; recruiter = Campus Recruiting, Americas.

**Step 4: Search, cheapest first, stop when the slots are full:**
1. Named person: exact name + company lookup.
2. Our warehouse of saved people (free).
3. Coresignal: current employees at the company with the slot's titles, team words required. Start narrow (team + office city), widen one step at a time: team any city → function + city → function only. Only the top few profiles per slot are collected (paid). This search does not go through the prompt-search title filter (`_distinctive_variations`) and never requires the job location as an exact person location.
4. FullEnrich people search (cheap fallback, 0.25 credits/row).

**Step 5: Score and fill.** Points for team words in the title or headline, seniority matching the slot, same office as the job, currently at the company. Best candidate fills each slot, labeled by how we found them. Alumni badge from the student's school.

**Step 6: Save to the job** (see above).

**Step 7: Emails on "See email" only,** through the existing email ladder (Hunter fast rung + FullEnrich). Sid's pending fixes to that ladder drop in when he pushes; this project does not rewrite it.

**Step 8: Log which step filled each slot** (named / warehouse / Coresignal round N / FullEnrich / empty), per job, so playbooks can be tuned from real results.

### Playbooks (function × size)

The playbook decides which titles to search for in each slot. Starting set (the builder refines with real results):

| Situation | Team leader slot | Runs-the-process slot | Recruiter slot |
|---|---|---|---|
| Bank / finance, large (IB, S&T, AM) | Group or desk head (MD, Head of [group]) in that office | VP / Director in that group | Campus / early-careers recruiter for the region |
| Big tech, large | Director / Head of the named team or org | Engineering / product manager on that team | University recruiter |
| Fortune 500 corporate (strategy, marketing, finance, ops) | VP / Head of the named office or function | Director / Senior Manager in it | Campus / early-talent recruiter |
| Consulting, large | Partner / Principal in the practice and office | Manager / Senior Associate | Campus recruiter |
| Startup, any function | Founder / CEO, or head of the function | Function lead, if separate | Usually none |

When the posting names no team, the leader slot falls back to the head of that function in the office nearest the job.

### Part B success

- On a hand-checked set of ~50 real early-career postings from our database (mix of finance, tech, Fortune 500, startups), the team is identified correctly on most, and the people shown are plausible for that team on most. Nick reviews the set.
- Zero-result rate on Find Hiring Managers drops sharply from today's (currently near-total on the Coresignal path).
- No vendor spend until the student clicks.

---

## Decided

- Two projects, in order: Part A (job database) then Part B (finder). Part B's first build can start on Greenhouse/Lever/Ashby jobs while Workday lands.
- Sources: our job dataset, Coresignal, FullEnrich. No LinkedIn job source.
- Target audience: early-career at big companies (finance, tech, Fortune 500), plus startups.
- Result shape: the team, then team leader → likely runs the process → recruiter, each labeled, leader emailed first.

## Calls I made (Nick to check while reading)

- **Existing jobs:** a one-time, gated update adds `department`/`team` to the ~7,800 early-career (tier 1+2) jobs only. It writes to PROD Firestore, so it runs only when Nick says go. The crawler's skip-if-unchanged design means re-crawls won't backfill on their own (see `pipeline/writer.py`). Jobs without the field rely on Step 1 reading the description.
- **Where the new finder runs:** the "Find contacts" button on jobs in our `jobs` collection; the modal starts sending the job id. Loops, Scout, the public tool and pasted URLs keep the current `find_hiring_manager()` flow, with today's three bugs fixed (title filter, exact location, substring job category).
- **Frontend is in scope:** button rename, the panel above, per-role drafts.
- **Credits:** unchanged for now, 5 credits per person shown, charged to each student who views them, even when the result was saved by someone else's earlier click. Nick to confirm.
- **Spend cap per first click:** about $1 of vendor spend; stop early when the warehouse fills the slots.
- **Fantastic.jobs:** the 2-hour pipeline already skips it (`full` mode passes `skip_fantastic=True`), so nothing changes there. The test is a capped probe (~$25) run only after Nick confirms the subscription. Part B does not depend on it.
- **Sid's branch:** not pushed as of 2026-10-20. Build on main's email code as is.

## Left to the builder

- Exact Workday tenant list format and how the target-employer list is curated (start from Simplify's Workday links plus a hand list).
- LLM prompt and model for Step 1, and caching per job id.
- Title lists per playbook, and the ranking weights.
- Whether the route is rewritten in place or a new route is added.
- Fixing the three current bugs (title filter, location filter, substring job category) as part of Step 3 rather than separately.
