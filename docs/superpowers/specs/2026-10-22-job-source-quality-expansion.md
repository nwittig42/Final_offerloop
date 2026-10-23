# Job sourcing: quality over apply-eligibility

Date: 2026-10-22
Owner: Sid / Nick
Status: SCOPE FOR REVIEW — nothing built, no decisions locked

## Why

Sid's read: "we use Fantastic.jobs, Lever, Greenhouse, all these, but we focus way too much on only finding
jobs that have auto-apply on them. Focus on getting the best quality jobs instead of just making sure
they are apply-eligible."

I audited the claim rather than assuming it. It is **partly true, and the true part is narrower and much
cheaper to fix than it looks.** The bias is not in what we ingest. It is two lines of display logic.

---

## Finding 1: the auto-apply bias is real, and it is two lines

Traced the whole path: ingest → quality gate → tiering → Firestore → feed query → serialization →
ranking → frontend.

| Stage | Biased toward auto-applyable? | Evidence |
|---|---|---|
| Source selection | No | sources are curated for employer quality, not applyability |
| Ingest / drop | No | `quality_gate.py` never reads `is_eligible` or `ats_platform` |
| Relevance tiering | No | `compute_relevance_tier` is title/function keywords only |
| Firestore storage | No | Workday/Oracle jobs indexed identically to Greenhouse |
| Feed query | No | filters on `relevance_tier in [1,2]` and `expired`, not applyability |
| **Feed serialization** | **YES** | `backend/app/routes/jobs.py:203` |
| **Scout suggestions** | **YES — hard filter** | `backend/app/services/scout/tools.py:1412-1413` |
| Vector search | No | filters `expired==false` only |
| **Frontend job board** | **YES — default-ON filter** | `JobBoardPage.redesign.tsx:509-512` |

**Correction (found after the first pass):** the frontend audit initially came back clean. It is not. The
live routed job board (`JobBoardRedesign` in `App.tsx:70`) has an `autoApplyOnly` filter that
**defaults to `true`** and persists to localStorage:

```tsx
const stored = localStorage.getItem("jobBoard.autoApplyOnly");
return stored === null ? true : stored === "true";   // default ON
...
autoApplyOnly ? list.filter((j) => j.autoApplyEligible) : list
```

This is the single most consequential bias site — larger than either backend one, because it discards
the jobs client-side after we paid to fetch, store and rank them. All ~3,600 Workday/Oracle/Oleeo/Goldman
early-career jobs from OFF-48 would have landed in production invisible by default. The original code
comment acknowledged the trade: *"Defaults to ON because the marketing claim of the feature lands hardest
when the first impression is 'every job has an Auto-apply button.' Power users who need Workday / Indeed
inventory (Goldman, JPM, Microsoft) flip it off."* Those "power users" are the core use case.

The two sites, verified verbatim:

```python
# backend/app/routes/jobs.py:203 — every feed response is reordered
cleaned.sort(key=lambda j: not j.get("auto_apply_eligible"))

# backend/app/services/scout/tools.py:1412-1413 — Scout hides the rest entirely
eligible = [j for j in by_score if j.get("auto_apply_eligible")]
pool = eligible if eligible else by_score
```

Consequence: a job ranked #3 by genuine match quality is displayed below a #80 job that happens to sit on
Greenhouse. In Scout, non-applyable jobs are **invisible** unless there are zero applyable matches — so
every Workday, Oracle, Oleeo and Goldman job we just spent OFF-48 building is silenced in Scout by
construction. We are paying to ingest inventory we then suppress.

**This is the highest-leverage change in this document and it is hours of work, not weeks.**

## Finding 2: Workday is already built and unpushed

Sid asked to "look on Workday." It exists — OFF-48 on `nickwittig/off-48-target-employer-sources`:
106 hot Workday companies (Citi, Morgan Stanley, Wells Fargo, BofA, PJT, Moelis, PwC, BlackRock, PIMCO,
Barclays, Capital One, Blackstone...), plus Oracle (JPMorgan, Lazard), Oleeo (BofA campus), Goldman's
GraphQL (81 US campus programs) and a 5-row manual CSV for firms that block crawlers. A dry run kept
~3,600 early-career jobs in 8 minutes.

**It is not on main and has never run live.** Shipping it is a prerequisite for everything below, and it
is blocked on the OFF-53 cleanup fix landing first.

## Finding 3: we already pay for broad ATS coverage and barely use it

Fantastic.jobs covers **58 ATS platforms** including iCIMS, Avature, BrassRing, Eightfold, Jobvite,
Cornerstone and Dayforce — most of the platforms we would otherwise hand-build. The paid 7-day sweep is
**skipped by default** (`main.py:792`, `skip_fantastic=True`); only the free daily delta endpoints run via
daemon. Before building any new ATS fetcher, we should test `source=` filters on the subscription we own.

Caveat: their dedicated Internships API was deprecated 2026-08-03, so early-career classification is now
our problem, not theirs.

## Finding 4: the real structural gap is one board per tenant

Our `workday.csv` holds exactly **one URL per company**. Workday tenants run many separate career sites,
and the early-career ones are usually distinct from the experienced-hire site. Each tenant publishes its
full site list in its own `robots.txt` as `Sitemap:` directives, with explicit `Allow:` rules — this is
sanctioned discovery, not scraping around a block.

Verified live, 2026-10-22:

```
GET https://aig.wd1.myworkdayjobs.com/robots.txt
→ Sitemap: .../aig/siteMap.xml
→ Sitemap: .../early_careers/siteMap.xml      ← we do not crawl this
```

We crawl `aig/aig` (99 analyst hits, mixed seniority). The board we miss, `aig/early_careers`, holds 39
postings that are **100% early-career**: "2027 Early Careers: Analyst, Underwriting – Structured Credit",
"2027 Early Careers: Analyst, Forensic Accounting", "2027 Claims Summer Internship".

Blackstone's robots reveals 8 boards; we crawl 1. Measured across ~1,350 tenants, robots-based discovery
found 4,148 boards vs 1,104 known — 3.8× — including 175 early-career boards at 111 tenants we currently
miss (Blackstone campus, PwC US Entry Level, Truist LDP, Visa Early Careers, Mastercard Campus, Invesco,
Guggenheim Undergraduate, Barings Early Talent, Wellington Campus, BMO/CIBC Campus, Salesforce
Futureforce, Chevron University, and all 12 Federal Reserve Banks on one tenant).

The same `robots.txt` trick enumerates Avature portals, so it is one reusable component.

---

## What "quality" should mean instead of apply-eligibility

Today `relevance_tier` is title-keyword matching, and tier 3 is hidden from the feed. That is a crude
proxy. If we are ranking on quality we should say what quality is. Proposed explicit signals, in rough
priority:

1. **Target employer** — is this a firm our students actually recruit into? We already have
   `large_employers.txt` and the hot/cold tiering; make employer tier a first-class ranking input.
2. **Genuine early-career program** — a named cycle ("2027 Summer Analyst") beats a bare "Analyst",
   which beats "Associate". Already partly captured by `analyst_entry`.
3. **Freshness** and an open application window (we extract deadlines via Perplexity already).
4. **US / location match** to the student's preferences.
5. **Resume/profile match score** — already computed.
6. **Apply-eligibility — as a tiebreaker only, never a filter.**

Open question for Nick: is auto-apply a *product promise* ("Offerloop applies for you") or a
*convenience*? If it is the promise, suppressing non-applyable jobs is defensible and this whole document
is just about adding sources. If it is a convenience, Findings 1's two lines are a bug. **This is the one
decision that changes the shape of the work.**

---

## Scoped work, by leverage

### Tier 0 — unbias the surfaces — DONE 2026-10-22
Nick's call: auto-apply is a convenience, so rank on quality and let eligibility break ties only.

- [x] Removed the `jobs.py` eligibility re-sort; the caller's `match_score` ranking now stands.
- [x] Replaced Scout's hard filter with `job_rank_key` — token match, then `relevance_tier`
      (early-career first), then eligibility as a final tiebreaker. Nothing is dropped.
- [x] Flipped the job board's `autoApplyOnly` default to OFF, with the storage key versioned to `.v2`
      so existing users actually get the new default (the old stored value was the persisted default,
      not a deliberate choice).
- [x] Kept the auto-apply button and the toggle — this changes ordering and default visibility, not
      capability.
- [x] Instrumented both backend surfaces to log how many top-ranked results are ineligible, so the
      suppression can be quantified rather than estimated.
- [x] Regression tests: `backend/tests/test_job_ranking_not_apply_biased.py` (6 tests) pin
      quality-over-eligibility and that ineligible jobs are never filtered out.

**Nothing else in this document pays off until this is done.** New inventory lands in surfaces that
currently hide it.

### Tier 1 — ship what is already built (days, free)
- Land OFF-53 (cleanup fix) on main; it is deleting live jobs nightly.
- Review the two unreviewed commits, fix the legacy empty-snapshot guard gap, reconnect Simplify de-dup.
- Run the recovery script (~108K job IDs, 99% still live on spot check) — Nick's approval.
- First live target crawl: +~3,600 early-career jobs.

### Tier 2 — Workday/Avature board enumeration (days, free, highest new-inventory yield)
- Build robots.txt board discovery; expand `workday.csv` from one row per tenant to one row per board.
- Gate on validation: non-zero jobs, not captcha, early-career-bearing.
- Reuse for Avature tenants (Deloitte, HSBC, Two Sigma, Forvis Mazars verified reachable).
- Expected: 100–175 additional dedicated early-career boards at target employers.

### Tier 3 — exploit the subscription we already pay for (days)
- Probe Fantastic.jobs `source=icims,avature,brassring,eightfold,jobvite,...` within the existing ~$25 cap.
- Measure new-early-career-jobs-per-dollar before building any fetcher by hand.
- The capped probe script from OFF-48 Task 12 already exists for this.

### Tier 4 — new platforms, only where Tier 3 leaves gaps (weeks)
Ranked by coverage ÷ effort. All verified to have a real endpoint:
- **Radancy TalentBrew** — Citi exposes 3,005 jobs here vs Workday's hard 2,000 ceiling; Barclays,
  BlackRock, RBC, BNY. Small effort.
- **iCIMS** — ~25% of the Fortune 500; sitemap slugs carry the title so classification needs no detail
  fetch; measured zero bot defense. Medium.
- **Phenom** — CVS alone has 3,973 intern reqs; US Bank, State Street, RBC, BMO. Medium.
- **SAP SuccessFactors** — EY 1,107 US roles; some tenants serve the whole inventory as one RSS feed. Medium.
- **Oracle Taleo** — real JSON API but needs a session cookie and a scraped portal id; ~50% of tenants
  have no REST API. Medium/large.
- **USAJOBS** — small but free and explicitly licensed for commercial use; all 12 Federal Reserve Banks.

### Flagged — needs a human/legal call before any build
- **eFinancialCareers** — by far the best bulge-bracket campus inventory found (4,213 "summer analyst",
  2,817 internship, full descriptions and expiry dates, no auth). **But robots.txt disallows the `/v1`
  path we would use.** Highest value per unit effort in the entire study and the one I will not build
  without Nick saying yes.
- **SmartRecruiters** — `Disallow: /` with LinkedInBot explicitly allowlisted. Modest yield (~600). Probably skip.

### Verified dead ends — do not spend time here
Handshake (168K public postings but only ~4% intern and 2.2% target employers — Dollar Tree, GameStop,
summer camps); 12twenty and Symplicity (exactly where elite IB/consulting recruiting happens, hard-closed,
commercial-use bans); UKG/ADP/Paylocity/Cornerstone/BrassRing/Dayforce (good access, wrong employers — the
"UKG = 70% of F500" stat is Kronos timekeeping, not recruiting); Eightfold (~90% of tenants return a
server-side 403, Morgan Stanley and Capital One both blocked); Teamtailor/Recruitee/Personio/Breezy/
Rippling/JazzHR (clean APIs, zero large US employers); Adzuna/Muse/Remotive/Careerjet (redirect-only apply
URLs by design — the redirect is their billing); Indeed/LinkedIn/Glassdoor/ZipRecruiter publisher APIs
(closed to new entrants); Bright Data (ToS bars competing products — our exact use case); NLx/CareerOneStop
/state job banks (data requests paused; ToS forbids charging users). NACElink's domain now serves gambling
spam — do not integrate.

Also worth knowing: GitHub lists will not serve this audience. Simplify's feed is 3.5% finance/consulting
(top employers TikTok, ByteDance, Tesla); zero Goldman, zero MBB. The one useful finance repo is
`northwesternfintech/2027QuantInternships` (quant only, stale since 2026-07). No IB or consulting list exists.

Bank of America needs no new work — its campus pipeline is on `bankcampuscareers.tal.net` (Oleeo, already
crawled); its Workday tenant is lateral-only.

---

## Two engineering rules to bake in

1. **Assert on parsed row counts, never HTTP status.** SuccessFactors, Jobvite, Avature, Phenom,
   SmartRecruiters and Recruitee all return 200 for fake paths and nonexistent companies.
2. **Classify early-career on the title, client-side.** Server-side keyword search matches full
   descriptions on SuccessFactors, UKG and BrassRing, so `q=graduate` matches "graduate degree required"
   (L3Harris: 1,434 false hits). Workday's `searchText` is already known to be fuzzy.

## Checked and fine — no action

Our Simplify URL still points at `Summer2025-Internships`. A renamed-repo redirect was a live-bug
suspicion; I tested it. It returns 200, 17,224 records, 4,850 active, newest `date_posted` 2026-10-22.
Healthy, no change needed.

## Known bug in scope-adjacent code

OFF-54: `normalizer._is_non_us_non_remote` matches country names as substrings — "india" inside
Indianapolis/Indiana, "uk" inside Milwaukee/Tukwila — so it drops US jobs from **every** source. Any
source-expansion work inherits this. It should be fixed before we measure yield from new sources, or the
measurements will be wrong.

## Decisions needed from Nick

1. **Is auto-apply the product promise or a convenience?** Governs whether Tier 0 happens at all.
2. eFinancialCareers: proceed despite the robots disallow, or drop it?
3. Order: Tier 0 first (free, fixes suppression of inventory we already have), or push the OFF-48
   backlog to main first?
4. Budget posture for Tier 3 beyond the existing ~$25 probe cap.
