# SDD ledger — plan: /Users/nicholaswittig/Offerloop/Final_offerloop/docs/superpowers/plans/2026-10-21-target-employer-job-sources.md

Worktree: /Users/nicholaswittig/Offerloop/Final_offerloop-off-48, branch nickwittig/off-48-target-employer-sources, base ee828821 (OFF-53 branch tip = origin/main + cleanup fix).
Spec: Final_offerloop/docs/superpowers/specs/2026-10-20-early-career-jobs-and-hiring-team-design.md (Part A), amended in plan.

## Pre-flight scan
| Pair / task | Produces vs consumes | Finding |
|---|---|---|
| T2 ↔ T3 | T2 normalizer passes department/team; T3 fetchers set them | consistent |
| T2 ↔ T4/T5/T6 | get_fetcher lazily imports direct.{workday,oracle,oleeo}.fetch; fetchers use get_record/make_job_id/is_candidate_title/FetchError | consistent; get_fetcher for unbuilt platform must fail only when called (lazy) |
| T2 ↔ T7 | crawled_hosts() reads CSVs T7 creates | T2 must tolerate missing CSVs → empty set (Ruling 2) |
| T2 ↔ T7 (fetcher.py) | T7 makes fetch_simplify call direct.crawled_hosts; direct.get_fetcher references fetcher._fetch_* | import cycle risk fetcher↔direct (Ruling 3) |
| T2 ↔ writer (OFF-53) | _DIRECT_ATS_PREFIXES derived from ALL_PLATFORMS; _ats_board rsplit("_",1) relies on make_job_id rules | consistent |
| T2 ↔ T9/T10 | BOARD_DIFF_SOURCES pre-lists goldman, manual; TARGET_PLATFORMS gains them later | consistent |
| T8 ↔ T9/T10 | workflow --platform list appended by T9/T10 | consistent |
| T3/T11/T12 | "carried over" from old plan file | briefs must include old plan task text (Ruling 1) |
| T1 | self-consistent (fake db tests, dry run only) | ok |
| T2 | tests vs code: analyst tier test needs "investment banking" in _TARGET_FUNCTION_TITLE (it is) | ok |
| T4 | location "Singapore  Singapore" + ", Singapore" → dropped by _is_non_us_non_remote | ok |
| T6 | posted_at None → normalizer stamps now | ok (noted in plan) |
| T10 | slug "all" matches [a-z0-9-]+; manual has no CSV in ats_companies → slug_loader warns only | ok |

## Rulings
- Ruling 1: Tasks 3/11/12 briefs = new-plan stub + the old plan's Task 1/4/5 text as a second brief file — the plan says "carried over unchanged" — cost if wrong: none.
- Ruling 2: direct.crawled_hosts() returns empty frozenset for missing CSVs — T2 runs before T7 creates them — cost if wrong: none.
- Ruling 3: direct/__init__ must import backend.pipeline.fetcher lazily (inside get_fetcher) so fetcher.py can import direct at module level in T7 — avoids an import cycle — cost if wrong: an import-order bug caught by T2's subprocess import test.
- Ruling 4: Branch base is the OFF-53 branch tip (ee828821), not origin/main, because OFF-53 is not yet pushed — the plan requires OFF-53 underneath — cost if wrong: rebase onto main once OFF-53 merges (trivial, same commits).
Task 1: dispatched (base ee828821, implementer sonnet)
Task 1: implementer DONE_WITH_CONCERNS 6c92f4c0; dry run: 6,498 boards, 108,359 missing ids (gh 69,794 / lever 15,356 / ashby 23,209). Controller spot-check: 15 GH boards, 220/222 missing ids still live on Greenhouse (99%). Review dispatched (sonnet).
Task 1: minor (deferred): stale-snapshot overwrite — --confirm writes kept_job_ids from a copy read up to 20 min earlier; a crawl in between gets reverted (harmless, wasteful). Fix: re-read each state doc before update, or run --confirm with crawler paused.
Task 1: minor (deferred): retry path untested (fail-once / always-fail tests); retries non-transient errors.
Task 1: minor (deferred): import order; "boards" counts only boards with missing ids; 8-thread path untested.
Task 1: complete (commits ee828821..6c92f4c0, review clean)
Task 2: dispatched (base 6c92f4c0, implementer opus)
Task 2: implementer DONE a1922dc6 (13 new tests; 8 pre-existing failures unchanged on parent). Self-flagged: load_slugs hot=whole CSV for target platforms (plan Task 7 says hot = hot_slugs.txt only).
Task 2: review (opus) Needs fixes — Important: load_slugs TARGET_ATS branch makes hot = whole CSV, cold = []; contradicts plan (hot via hot_slugs.txt, cold = CSV - hot).
Task 2: minor (deferred): run_crawl_ats CSV analyst_entry stamping untested; bare --platform= error message "unknown ()".
Ruling 5: Simplify de-dup (Task 7) must use hot-tier target hosts only, not every CSV row — cold-tier tenants aren't crawled on schedule, so dropping their Simplify listings would lose jobs — cost if wrong: a few duplicate jobs for cold tenants. Carry into Task 7 dispatch (Task 7 may change crawled_hosts() accordingly).
Task 2: fix round 1/5 (1 addressed, 0 open; commits a1922dc6..293870c0)
Task 2: complete (commits 6c92f4c0..293870c0, review clean)
Task 3: dispatched (base 293870c0, implementer sonnet)
Task 3: minor (deferred): normalizer/fetchers pass non-str department/team through without str() coercion (oldplan said str(raw.get(...) or "")); no test for departments: None.
Task 3: complete (commits 293870c0..c2bcf490, review clean)
Task 4: dispatched (base c2bcf490, implementer sonnet)
Task 4: implementer DONE 528b4724
Task 4: minor (deferred): missing country falls back to bare city (could let a non-US job through) — raising would drop whole board, so leave, maybe warn; 100-per-term cap truncates silently (plan-mandated cap; add warning log); html.unescape before _strip_html can eat "&lt;" text; job_id collision of sanitized jobReqIds silently dropped (add warning); test gaps: dedupe with distinct paths, cap, list timeout, missing country; double space in location.
Task 4: complete (commits c2bcf490..528b4724, review clean)
Task 5: dispatched (base 528b4724, implementer sonnet)
Task 5: implementer DONE 5e10ec08 (department from list-row fallback; fixture resolves to 'Seasonal Employee')
Task 5: minor (deferred): department can be an employment category ("Seasonal Employee" from JobFamily) — Find Contacts posting_reader (redesign branch) would take it as the team; follow-up: add category-like words to posting_reader._NOT_A_TEAM. Also: cap truncation only warns; non-dict row raises AttributeError not FetchError; posted_at could fall back to detail ExternalPostedStartDate.
Task 5: complete (commits 528b4724..5e10ec08, review clean)
Task 6: dispatched (base 5e10ec08, implementer sonnet)
Task 6: implementer DONE_WITH_CONCERNS 11e7322b — Evercore + Jefferies events board now serve an Altcha/Cloudflare captcha (also to real Chrome); not bypassed; fetcher raises FetchError on it.
Ruling 6: Evercore leaves the Oleeo crawl list (Task 7) and goes to the manual list (Task 10) — the site now challenges every client and we don't circumvent bot protection — cost if wrong: Evercore jobs entered by hand until its board opens up.
Task 6: review Needs fixes — Important: non-US (EMEA/APAC, ~75% of Jefferies roles) not dropped; Region label only used as Location fallback. Minor (deferred): remote always False when location empty.
Task 6: fix round 1/5 (1 addressed, 0 open; commits 11e7322b..d51392e6)
Task 6: minor (deferred): unlisted Region values ("Asia-Pacific", "Middle East", "Latin America") bypass the title fallback; non-US vacancies still cost one fetch each; live EMEA/APAC filtering only covered by synthetic tests.
Task 6: complete (commits 5e10ec08..d51392e6, review clean)
Task 7: dispatched (base d51392e6, implementer opus) with Rulings 5 and 6
Task 7: implementer DONE_WITH_CONCERNS 1a91fb45 — workday 108 hot/338 cold, oracle 2/0, oleeo 1/0 (Jefferies + moelis-campus now captcha'd → dropped); analyst_entry regex (plan) misses Barclays/BlackRock/PIMCO etc., false-positive Disney (ey).
Task 7: review Needs fixes — Important (plan-mandated): analyst_entry regex false 0s (Barclays, BlackRock, Blackstone, PIMCO, Vanguard, Invesco, ...) and false 1s (Disney, Stanley Black & Decker via ey\b; generic "partners"). Important: hot Workday rows on private/internal sites (Fidelity targeted, CVS Private_Postings..., FTI ...Private, Franklin invitation-only, Medtronic redeployment, Mizuho_Confidential, Disney disneycareerdc, Autodesk uni, Raymond James EarlyCareers) + host-level de-dup hides their public Simplify listings.
Ruling 7: Override the plan's analyst_entry regex: use \bey\b, drop the bare `partners` term, and add an explicit finance-name set (banks, asset managers, exchanges, insurers with analyst programs) in the script; table-test the classifier — the plan's regex was a heuristic that review showed misclassifies key banks — cost if wrong: some plain "Analyst" titles land in tier 3 (hidden) or non-finance analysts show as early-career; Nick reviews the list.
Ruling 8: For each hot row on a private/internal/narrow site, switch to the tenant's main public external site if one validates (non-zero jobs, not captcha); otherwise move the row to cold (out of hot_slugs.txt, so it's neither crawled on schedule nor de-duplicated) — keeps Simplify coverage — cost if wrong: a few employers fall back to Simplify-only coverage.
Task 7: minor (deferred): host-level de-dup also hides Simplify listings on a hot tenant's other sites; no tests for match_large_employer/normalize_name (analyst_entry gets tests under Ruling 7); Intel patched without full re-run; hot_slugs.txt comment says hot = every 2h (workday/oracle/oleeo only run via --platform on the 6h cron).
Task 7: fix round 1/5 (2 addressed, 0 open; commits 1a91fb45..0ca097f8). Counts: workday 106 hot/340 cold, oracle 2/0, oleeo 1/0 (bofa-campus only).
Task 7: minor (deferred): substring finance terms (citi/bank/investment/capital) can over-flag (e.g. Citizens); some hot rows on narrow campus sites (Blackstone, MUFG, BP, Mastercard) — Nick to review; Raymond James public site RaymondJamesCareers (307 jobs) unconfirmed → cold pending Nick.
Task 7: complete (commits d51392e6..0ca097f8, review clean)
Task 8: dispatched (base 0ca097f8, implementer sonnet)
Task 8: implementer DONE_WITH_CONCERNS 243cac84. Dry run: 79 min wall; workday 105 ok/1 failed (citi: 403 on a Warsaw detail), oracle 2/0, oleeo 1/0 but 0 jobs (bofa-campus); kept 3611 (t1 467, t2 3144); samples mostly on-target, noise = generic "Associate" titles and "Sr." titles. Review dispatched.
Ruling 9: Speed — prefilter non-US from Workday list locationsText; 4 concurrent detail calls per board; workday board concurrency 8; target crawl daily ('20 6 * * *') instead of 6-hourly; goal ≤20 min — the 79-min run can't fit the 50-min workflow timeout and 6h × 80 min would burn GitHub minutes — cost if wrong: target jobs refresh daily instead of 4×/day.
Ruling 10: A single posting's detail 403/404/410 skips that posting (warning); >10% and >3 skipped raises FetchError — strict all-or-nothing made Citi fail every run on one blocked Warsaw posting — cost if wrong: a briefly unreadable posting drops off until next crawl.
Ruling 11: is_candidate_title rejects Sr/Sr./Snr/Sen. — "Sr. ... Analyst" passed — cost if wrong: none.
Ruling 12: Investigate bofa-campus 0; fix if small, else move out of hot — cost if wrong: BofA campus via Simplify/manual only.
Task 8b: added (brief task-8b-brief.md); runs after Task 8 review.
Task 8: minor (deferred): generic "Associate" titles (warehouse/client/customer support associate) count as early-career via the legacy _EARLY_CAREER_TITLE regex — Nick to decide whether to tighten for target sources.
Task 8: review Needs fixes — Important: new cron schedules a PROD-writing job that the dry run shows exceeds timeout-minutes: 50. Minor (deferred→8b): no concurrency group.
Ruling 13: Task 8's timeout finding is resolved inside Task 8b (daily cron + ≤20-min target + concurrency group), not by a separate fix round — the same files and the same root cause; nothing is pushed, so no PROD run can happen before 8b and Nick's go — cost if wrong: none while unpushed.
Task 8: complete (commits 0ca097f8..243cac84, 1 finding carried into 8b per Ruling 13)
Task 8b: dispatched (base 243cac84, implementer opus)
Task 8b: implementer DONE 7a087725 — dry run 481s, 0 failed boards, kept 3577 (t1 470, t2 3107); bofa-campus fixed (vacancy/1). Found pre-existing bug: normalizer._is_non_us_non_remote substring-matches (india→Indianapolis/Indiana, uk→Milwaukee/Tukwila), dropping US jobs for all sources — separate ticket.
Task 8b: minor (deferred, final review should consider fixing): a small board (≤3 candidates) whose details ALL 403 returns [] and syncs as fully read → expires its jobs; fix = also raise when skipped == candidates > 0.
Task 8b: minor (deferred): oleeo.csv line endings changed CRLF→LF; workflow comments say ~20 min (measured 8); prefilter reuses substring country match (OFF-54 logged).
Task 8b: complete (commits 243cac84..7a087725, review clean)
Task 9: dispatched (base 7a087725, implementer sonnet)
Task 9: implementer DONE 3c6ae62b — Goldman fetcher built (CAMPUS experience: 81 US roles of 213); registered in TARGET_PLATFORMS, slug_loader, POOL_SIZE, normalizer, csv, hot_slugs, workflow.
Task 9: minor (deferred): repeated page with row count == total undetected (count distinct roleIds); pipe-delimited title used for classification — division names containing Principal/Lead/Director could drop roles (classify on last segment); posted_at = lastPostedDate may change daily (board hash never matches → extra state writes); tests missing for repeated page / MAX_PAGES / missing sourceId.
Task 9: complete (commits 7a087725..3c6ae62b, review clean)
Ruling 14: Task 10 seed rows only for program pages whose URL the implementer can verify exists (plain GET 200 or a search result showing the exact URL); no fabricated URLs; McKinsey/Bain/Evercore/Jefferies/Moelis-campus candidates that can't be verified are listed in manual_candidates.md for Nick — cost if wrong: fewer seed rows until Nick fills them.
Task 10: dispatched (base 3c6ae62b, implementer sonnet)
Task 10: implementer DONE_WITH_CONCERNS 34518865 — 5 seed rows (McKinsey, Bain, Evercore, Jefferies, Moelis), program landing pages verified via search results; all default New York, NY.
Task 10: review Approved-with-Important (data): all seed rows guessed "New York, NY"; Evercore/Jefferies/Moelis rows point at generic bot-checked tal.net board URLs; manual_candidates.md contradicts itself.
Ruling 15: seed locations become "United States" (no guessed cities); tal.net rows replaced by the firm's own public careers/program page if verifiable per Ruling 14, else removed and listed for Nick; reword the .md — students shouldn't land on a captcha or a wrong city — cost if wrong: fewer hand rows until Nick fills them.
Task 10: minor (deferred): analyst_entry only exact "1"; URL edit = delete+add (doc note); silent duplicate-URL skip; header-only CSV expiry untested.
Task 10: fix round 1/5 (2 addressed, 0 open; commits 34518865..fee03503). Evercore/Moelis URLs 403 to scripts (verified via search results only); Bain jobid URL may expire per cycle.
Task 10: complete (commits 3c6ae62b..fee03503, review clean)
Task 11: dispatched (base fee03503, implementer sonnet)
Task 11: implementer DONE 7095eeeb — dry run: 5237 docs missing team fields over 1313 boards; 4481 matched current fetch; 750 no longer posted; 5 boards skipped (empty fetch).
Task 11: minor (deferred, fix before Nick's --confirm): batch.set(merge=True) can create stub docs if a job doc is deleted during the 10-12 min fetch window → use update() with NotFound fallback. Others: no test for unmatched/merge-preserves-fields; --limit caps fetching not reads; no real RED run recorded; CLI is `python -m backend.scripts.backfill_job_team_fields`.
Task 11: complete (commits fee03503..7095eeeb, review clean)
Task 12: dispatched (base 7095eeeb, implementer sonnet)
Task 12: implementer DONE_WITH_CONCERNS aa86a34c+608f42c5 — title_filter syntax (' OR ' vs repo's '|') unverified; 429 retry undercounts max-requests.
Task 12: review Needs fixes — Important: --max-requests cap can be exceeded (2x) via _fj_fetch_page's 429 retry; title_filter " OR " syntax unproven.
Ruling 16: count real HTTP calls (and stop the run on a 429) so the spend cap is exact; switch to advanced_title_filter with the repo-proven `'term' | 'term'` syntax — a wrong filter or uncapped retry spends paid quota — cost if wrong: none (more conservative).
Task 12: minor (deferred): empty first page indistinguishable from failure; Firestore read after spend not guarded; in_firestore None prints 0; weak title-filter test; no >30-URL batching test.
Task 12: fix round 1/5 (2 addressed, 0 open; commits 608f42c5..8e0e850c)
Task 12: minor (deferred): probe doesn't record x-ratelimit headers.
Task 12: complete (commits 7095eeeb..8e0e850c, review clean)
Final review: dispatched (opus) over origin/main..HEAD
Final review (opus): With fixes. 175 passed / 2 known failures. Important: (1) sticky dormancy — hash-match path never clears dormant/consecutive_failures → cleanup deletes live jobs; (2) boards removed from slug lists keep non-dormant state → jobs never expire (regression vs pre-OFF-53); (3) 100-result caps give partial snapshots → expire live jobs; (4) host-level Simplify de-dup drops listings direct crawl never produces (Goldman non-campus, beyond-cap, unmatched terms, other sites); (5) all-403 small board / 403 challenge page in Oleeo treated as gone.
Ruling 17: cap hit → fetcher marks the snapshot partial; sync writes new jobs but expires nothing for that board (and doesn't replace kept_job_ids with the partial set — union with prior kept) — strict FetchError would block whole big boards daily — cost if wrong: jobs that really closed on capped boards linger until the board stops hitting the cap or the 14-day TTL... (they won't TTL: so also log cap hits for monitoring).
Ruling 18: Simplify de-dup becomes per-posting: skip a Simplify listing only if its normalized apply URL matches a job the direct target crawl holds (Firestore projection read of apply_url for target sources, select() only; fall back to no de-dup if the read fails) — host-level dropped real jobs — cost if wrong: an extra Firestore read of a few thousand small docs per full run.
Ruling 19: all-skipped (skipped == candidates > 0) raises FetchError; Oleeo checks challenge markers before treating 403 as gone; target-platform guard in _crawl_one: empty snapshot with non-empty prior kept_job_ids → mark_failure, not sync — empty-by-block must never expire — cost if wrong: a board that truly empties out keeps its jobs until it has ≥1 posting again (they still expire individually when the board has others).
Final fix wave: dispatched (opus) — Important 1-5 + Task 11 stub docs + cleanup select() projection + target run-log mode name + dry-run skips run log + crawled_hosts/manual warning + Oleeo region allowlist + Goldman distinct roleId check.
