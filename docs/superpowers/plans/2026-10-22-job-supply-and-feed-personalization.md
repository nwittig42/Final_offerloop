# Plan: unblock the pipeline, grow early-career supply, personalize the feed

Date: 2026-10-22
Owner: Nick
Status: SCOPE FOR REVIEW — Phase A partly diagnosed, B researched, C newly scoped

Three phases, in order. Each one is a prerequisite for the next being worth doing:
A unblocks the pipeline, B fills it with the right jobs, C decides which of those jobs each
student sees. Doing C first would be personalizing a feed that's 89% noise; doing B first
would be adding jobs to a pipeline that deletes them nightly.

Research sources: `docs/superpowers/specs/2026-10-22-job-source-quality-expansion.md` (source
study, verified dead ends), the production census (`backend.scripts.count_early_career_jobs`),
and `docs/RECOMMENDER_STATUS.md` (stale since May, see Phase C).

---

## Phase A — Unblock the pipeline (days, free, no new surface area)

All four items are diagnosed; none is speculative.

### A1. OFF-53 to `main` — URGENT
The nightly 03:00 UTC cleanup deletes live Greenhouse/Lever/Ashby jobs. The fix is written and
reviewed, sitting unpushed. **Every night it isn't on main, more live jobs get deleted.**
Confirmed not bypassed by a Firestore TTL policy: `gcloud firestore fields ttls list
--collection-group=jobs` returns 0 items (5 other collections do have ACTIVE TTLs, so the
credentials and the query are good — this is a real negative).

### A2. Legacy empty-snapshot guard
`main.py:607` guards with `if platform in direct.TARGET_PLATFORMS and not kept and prior_kept`,
which excludes greenhouse/lever/ashby — the platforms holding ~95% of jobs. Those fetchers
return `[]` on any exception (`fetcher.py:124-126`), so a transient 403 syncs an empty snapshot
with `partial=False`, `writer.py:315` computes `removed_ids = prior_kept`, and the whole board
expires. Self-healing within one crawl (the same sync writes `kept_job_ids = []`, so the next
run rewrites everything), so exposure is a ~2h feed gap — but the persistent case is silent: a
board that keeps failing never raises, never calls `mark_failure`, never goes dormant, and logs
as a clean "0 jobs" board while its live jobs expire. Fix: drop the platform condition, or gate
on `not raw` rather than `not kept` so a genuinely empty board still expires normally.

### A3. `crawled_hosts` / Simplify de-dup
Commit 1c2a838f deleted `direct.crawled_hosts()` but left the caller at `fetcher.py:957`. A
broad `try/except` swallows it, so production logs a warning and **Simplify de-dup is off
entirely**. Two tests fail. Ruling 18 replaces host-level with per-posting de-dup (match on
normalized apply URL via a `select()` projection read of target-source jobs, no de-dup if the
read fails). Must land before the first live target crawl or the board shows every Citi/JPM job
twice.

### A4. OFF-54 — substring location matching
`normalizer._is_non_us_non_remote` matches country names as substrings: "india" inside
Indianapolis/Indiana, "uk" inside Milwaukee/Tukwila. **Drops US jobs from every source.** This
has to be fixed before Phase B, or every yield measurement from a new source will be wrong.

**Exit criteria:** OFF-53 on main, all tests green except the 2 known cold-mode failures,
first live target crawl completed and its per-source counts recorded.

---

## Phase B — More entry-level jobs and internships

### The governing insight from the census

| source | jobs stored | early-career | hit rate |
|---|---|---|---|
| Greenhouse | 45,091 | 3,062 | 6.8% |
| Lever | 11,186 | 1,110 | 9.9% |
| Ashby | 10,941 | 649 | 5.9% |
| **Simplify** | **2,739** | **2,667** | **97.4%** |

70,794 jobs, 2,223 tier-1. Simplify is 4% of storage and 62% of tier-1 supply. Broad ATS
crawling has a 6-10% hit rate; curated early-career boards are near 100%.

**So the strategy is hit rate, not volume.** Adding another 10,000 Greenhouse companies buys
~700 relevant jobs and a lot of storage. Adding 175 dedicated early-career boards buys most of
their postings. Every item below is ordered by hit rate per unit effort.

Also established: the shortage is *employers*, not tiering. Expanding the tier-1 regex with
every finance function across all 70,794 docs moved only 103 jobs — the finance inventory isn't
mis-graded, it's absent.

### B1. Ship OFF-48 (built, never run)
106 Workday companies, Oracle (JPMorgan, Lazard), Oleeo (BofA campus), Goldman GraphQL (81 US
campus programs), 5 manual rows. Dry run: ~3,577 early-career jobs in 8 minutes. Census confirms
`workday/oracle/oleeo/goldman/manual` all at **0 docs** — this has produced nothing yet.
Roughly +50% early-career supply, weighted to finance.

Watch on first live run: with the tier-1 regex fix, a large share of the dry run's 3,107 tier-2
jobs become tier-1, and tier-1 gates Firecrawl + PDL enrichment
(`writer._apply_enrichment_gate`). Firecrawl is ~$10 at that volume; **get the PDL per-call cost
before the run, not after.**

### B2. Workday + Avature board enumeration (free, highest new-inventory yield)
Our `workday.csv` holds one URL per tenant. Workday tenants run many career sites and the
early-career ones are separate. Each tenant lists them in its own `robots.txt` as `Sitemap:`
directives with explicit `Allow:` rules — sanctioned discovery, not evasion.

Verified live 2026-10-22: we crawl `aig/aig` (99 analyst hits, mixed seniority) and miss
`aig/early_careers` — 39 postings, 100% early-career ("2027 Early Careers: Analyst, Underwriting
– Structured Credit"). Blackstone exposes 8 boards; we crawl 1. Across ~1,350 tenants this finds
4,148 boards vs 1,104 known (3.8x), including 175 early-career boards at 111 tenants we miss:
PwC US Entry Level, Truist LDP, Visa Early Careers, Mastercard Campus, Invesco, Guggenheim
Undergraduate, Barings Early Talent, Wellington Campus, BMO/CIBC Campus, Salesforce Futureforce,
Chevron University, and all 12 Federal Reserve Banks on one tenant.

Work: robots.txt board discovery, expand the CSV to one row per board, validate each (non-zero
jobs, not captcha, early-career-bearing). Same mechanism enumerates Avature portals, so it's one
reusable component.

### B3. Exploit the Fantastic.jobs subscription we already pay for
It covers **58 ATS platforms** — iCIMS, Avature, BrassRing, Eightfold, Jobvite, Cornerstone,
Dayforce — i.e. most of what we'd otherwise hand-build. The paid 7-day sweep is **skipped by
default** (`main.py:792`), and the census shows `fantasticjobs` at **0 docs**. Probe `source=`
filters within the existing ~$25 cap and measure new-early-career-jobs per dollar *before*
building any fetcher by hand. The capped probe script from OFF-48 Task 12 already exists.
Caveat: their dedicated Internships API was deprecated 2026-08-03, so early-career
classification is ours now.

### B4. New platforms, only where B3 leaves gaps
Ranked by coverage ÷ effort, all with a verified endpoint: **Radancy TalentBrew** (Citi exposes
3,005 jobs here vs Workday's hard 2,000 ceiling; also Barclays, BlackRock, RBC, BNY — small
effort) → **iCIMS** (~25% of the Fortune 500; sitemap slugs carry the title so classification
needs no detail fetch; measured zero bot defense) → **Phenom** (CVS alone has 3,973 intern reqs)
→ **SuccessFactors** (EY 1,107 US; some tenants serve the whole inventory as one RSS feed) →
**USAJOBS** (small, free, explicitly licensed for commercial use; all 12 Federal Reserve Banks).

### Held and dead — do not re-investigate
**eFinancialCareers** is the highest-value source found anywhere (4,213 "summer analyst", full
descriptions, no auth, genuine bulge-bracket campus inventory) but its robots.txt disallows the
path. **Held pending a legal read** per Nick, 2026-10-22. Do not build it.

Verified dead: Handshake (168K postings, 4% intern, 2.2% target employers — Dollar Tree,
GameStop, summer camps); 12twenty and Symplicity (exactly where MBB recruiting happens, both
hard-closed with commercial-use bans); UKG/ADP/Paylocity/Cornerstone/BrassRing/Dayforce (good
access, wrong employers — the "UKG = 70% of F500" stat is Kronos timekeeping, not recruiting);
Eightfold (~90% of tenants 403 server-side; Morgan Stanley and Capital One both blocked);
Teamtailor/Recruitee/Personio/Breezy/Rippling/JazzHR (clean APIs, zero large US employers);
Adzuna/Muse/Remotive/Careerjet (redirect-only apply URLs by design); Indeed/LinkedIn/Glassdoor
publisher APIs (closed to new entrants); Bright Data (ToS bars competing products); NLx and
CareerOneStop (requests paused; ToS forbids charging users). GitHub lists won't serve this
audience — Simplify is 3.5% finance/consulting with zero Goldman and zero MBB, and the only
useful finance repo is `northwesternfintech/2027QuantInternships` (quant only, stale since July).
BofA needs nothing: its campus pipeline is `bankcampuscareers.tal.net`, already crawled.

### Two engineering rules to bake in
1. **Assert on parsed row counts, never HTTP status.** SuccessFactors, Jobvite, Avature, Phenom,
   SmartRecruiters and Recruitee all return 200 for fake paths and nonexistent companies.
2. **Classify early-career on the title, client-side.** Server-side keyword search matches full
   descriptions on SuccessFactors, UKG and BrassRing, so `q=graduate` matches "graduate degree
   required" (L3Harris: 1,434 false hits). Workday's `searchText` is already known to be fuzzy.

**Exit criteria:** tier-1 live supply up from 2,223; per-source hit rate recorded for every new
source; no source added below ~30% hit rate without a reason.

---

## Phase C — Personalize the job feed

### What already exists (audited 2026-10-22, more than the status doc implies)

The feed is **per-user**, not shared — ranked once from the profile and cached on
`users/{uid}.jobFeedCache` for 30 min (1,000 job IDs + scores + reasons, with a `jobFeedOffset`
rotation on refresh). Pipeline:

1. **Deterministic pre-filter** (`job_ranking.py:290-354`): field alignment 40pts, job-type fit
   15-30, skills match 4-30, grad-year fit 4-10, dream company +15, location +10/remote +5.
   Only ≥15 reach GPT.
2. **GPT-4o-mini rerank** of the top 20 (`:494-656`) → `match_score` 0-100 + `match_reason`,
   with a banned-phrase guard that falls back to a data-derived reason. Jobs 21-50 get a scaled
   deterministic score capped at 49.
3. **Signal boosts** (`job_ranker_signals.py:43-104`): dream company +25, saved contact at
   company +20, target company +15, saved-company affinity +10, plus editorial badges.
4. **Intent gates** (`intent_gates.py:174-687`): hard drops on level, location, interest.
5. **Embedding prefilter** (`embedding_ranker.py`, `vector_store.py`): resume vector vs job
   vector, cosine, `text-embedding-3-small`, cached on the user doc by content hash. **Built but
   flag-gated off** (`embedding_ranker` flag + `RANKER_MODE`).
6. Dedup by title+company, cap 3 per company, hard-filter dismissed jobs.

**Verdict from the audit: about 4.5/10.** Genuinely resume-aware and preference-aware. Not
behavior-aware.

### The one real gap: no behavioral feedback loop

`events_service.py` has solid infrastructure (18-field schema, idempotency, TTL, allowlist) and
Track C shipped a recommendation event logger — **but it covers contacts/alumni only**
(`recommendation_events`, keyed on `contact_email`). For jobs there is **no impression, click,
save-through, dismiss-with-context, or application event logged at all.**

Consequences: no implicit feedback, nothing to train or tune on, no "similar to what you saved",
no cohort signal, and no repetition suppression — a job you scrolled past yesterday can top
today's feed, because nothing recorded that you saw it.

`docs/RECOMMENDER_STATUS.md` is stale: it says resume Phase 1 on "May 15-17, 2026" and nothing
has happened since. Its plan file `.claude/plans/jaunty-spinning-owl.md` **no longer exists**, so
Phases 1-5 would need re-deriving. Treat the status doc as history, not a live plan.

### C1. Cheap wins first (days, no new infrastructure)
- **Make `careerInterests` rank, not just gate.** Today it's drop-only
  (`intent_gates._gate_by_interest`) — a student who said "data science" gets Finance Analyst
  roles if skills align, and interest contributes **zero** ranking weight. Add it as a scoring
  term. Highest value per line of code in this whole phase.
- **Roll out the embedding ranker.** It's built, cached and flag-gated off. Turn it on for a
  cohort, compare against the deterministic path, keep the winner.
- **Surface narrative fields when embeddings are off.** `directionNarrative` and
  `personalContext` are captured and feed *only* the embedding path, so they're ignored entirely
  for most users today.

### C2. Job event logger (the missing Track C for jobs)
Log impression, click, save, dismiss, apply and auto-apply-submit per `(uid, job_id)`, reusing
the `events_service` + `recommendation_events` pattern so the two surfaces stay consistent.
Carry `request_id`, `session_id`, `rank` and `score` so an outcome can be tied back to the
impression that produced it — that join is exactly what `23ccad0` added for contacts.

This is the unlock for everything below it. Until it exists, any "learned ranker" has no
training data and we'd be tuning weights by vibes.

### C3. Close the loop (after ~2 weeks of C2 data)
- **Repetition suppression / seen-cooldown** — the most visible quality win, and impossible
  without C2.
- **Implicit negatives** from repeated skip, distinct from explicit dismissal.
- **Learned company and function affinity** from click-through, replacing hand-set boosts.
- **Cohort signal** ("students at your school with your track applied to…"), the first thing
  here that no competitor can copy from our profile fields alone.

### C4. Learned model — only if C3's data justifies it
Re-derive the old Phases 1-5 (feature store, embedding cache, heuristic baseline, learned model)
from whatever C2/C3 show. Do not start here. Do not restore the 12-week plan on faith; the
status doc's own "verify before starting Phase 1" checklist assumed data that was never
collected.

### Measure before building
What fraction of active users have a parsed resume (`resumeParsed.skills`)? Skills are worth up
to 30 points and the whole embedding path depends on resume text. If the number is low, cold
start is the real personalization problem and C1/C2 priorities change. Cheap to answer, so
answer it first.

**Exit criteria:** job events flowing; a measured lift against the current ranker on a held-out
cohort; no job repeating in a feed within N days.

---

## Decisions needed from Nick

1. **PDL per-call cost** — gates B1's first live run (Firecrawl is ~$10; PDL unknown).
2. **Phase B stopping point.** B1+B2 are free and large. B3 reuses an existing subscription. B4
   is real build effort — worth it only if B3's numbers are poor.
3. **Does personalization wait for Phase B?** C1 is independently worth doing. C2+ should
   arguably wait, since event data collected on a 2,223-job tier-1 pool teaches us less than
   data collected after supply roughly doubles.
4. **eFinancialCareers legal read** — still the single highest-value source found, still held.
