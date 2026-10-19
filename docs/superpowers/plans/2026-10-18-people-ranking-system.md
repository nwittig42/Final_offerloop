# People Ranking System — Plan (OFF-41)

**Goal:** Every surface that recommends people (iOS/extension deck, web Find, find-similar, MCP, Loops) pulls candidates from the warehouse and online through one pipeline, ranks them with everything we know about the student, and learns from what actually happens (saves, sends, replies, meetings).

**Status today, in one line:** a hand-weighted warmth score over sparse warehouse records, fed by only two profile fields (dream companies, school), with outcome data logged but never used — and partly not logged at all because of bugs.

## Decisions (Nick, 2026-10-18)
- **Goal:** a fostered connection (meeting booked / connected), and eventually a job landed. Replies and sends are leading indicators, not the target.
- **No onboarding changes.** Target firms, roles and the rest are collected in the profile later; the ranker reads them from there plus Loops, saved jobs and the student's own pipeline.
- **No cross-user contact cap** for now (§3e deferred).
- **Warehouse depth and data providers are owned by the backend/data-provider work in flight.** This plan does not change the writer, vendors or Coresignal budget (§4, §5 are dependencies, not tasks here).

## Status
- **Shipped in OFF-41 (branch `nickwittig/off-41-personalized-people-ranking`, off `main`):** Phase 0 deck fixes, Phase 1 RankingProfile, and the v1 scoring layer on the People deck behind a 50/50 uid-hash split (`PERSON_RANKER_V1_PERCENT`). See "What OFF-41 shipped" at the end.
- Line numbers below were taken on `staging/mobile-field`; `main` holds the newer ranker (company deck, chips) and is what OFF-41 builds on.

---

## 1. What we know about the user (inventory)

### Declared
| Signal | Where | Used in ranking today |
|---|---|---|
| School | `academics.university` (web), `academics.school`/`school` (iOS) | **Partly broken.** `person_ranker.py:259` reads only `academics.school`/`school`, so web users get no alumni candidate fetch. Warmth +20 still fires. |
| Major | `academics.major` | Warmth +10, but warehouse docs have no major, so it never fires |
| Grad year | `academics.graduationYear` | Chip order only |
| Career track | top-level `careerTrack` (Settings only — removed from onboarding) | Warmth +15 |
| Dream companies | top-level `dreamCompanies` (Settings only — removed from onboarding) | Candidate source + warmth +10 (exact string match, not slug) |
| Industries, preferred roles, locations, job types | Settings | Not used |
| Hometown | **never collected** | +8 rule is dead |
| Personal note, direction narrative | Settings / extractor | Not used |

### Resume + user's own LinkedIn
`resumeParsed` (education, experience[], projects, skills{}, extracurriculars, honors, coursework), `linkedinEnrichmentData` (raw Apify: headline, posts), `linkedinResumeParsed.career_interests`, `resumeEmbedding`, `preferenceVector`.
Used: past employers (+15), skills/clubs for lead-type reasons. Not used: projects, coursework, honors, headline, career_interests, either embedding.

### Behavior and intent (collected, not used for people)
- **Loops** `users/{uid}/loops/*.briefParsed{companies,industries,roles,locations}` — the strongest stated intent we have.
- **Searches** `metrics_events` `search_performed{companies,titles,locations}` (the `searchHistory` subcollection has no writer).
- **Firm searches** `users/{uid}/firmSearches`, **saved jobs** `savedJobs.company`, **job feedback** `jobPreferences`.
- **Coffee chat preps**, **Scout strategies**.
- **Deck swipes** `peoplePreferences/{candidate_id}.signal` — left/skip only used as exclusion; right swipes ignored.
- **Saved contacts** — only used as exclusion, never as "more like this".
- **Weekly queue dismissals + blocklist** `settings/queue_preferences.blocklist` — not read by the ranker.

### Outcomes (joinable to warehouse people)
- Contact fields: `emailSentAt`, `replyReceivedAt`, `bouncedAt`, `meetingScheduledAt`, `resolution` (meeting_booked / soft_no / hard_no / ghosted), `pipelineStage`.
- `recommendation_events`: `recommendation_shown` (web Find only, with rank + features), `email_sent`, `email_replied` (attributed by uid+email). Deck contacts join directly via `ranker_{candidate_id}`.
- `global_suppression/{email}` (bounces).
- Opens: arriving with OFF-12 (`emailOpenedAt`, `emailOpenCount`).

### Logging bugs that lose labels
1. Web-redesign branch: `ranker.py` passes `doc_id=` to an old `log_recommendation_event` → `/feedback`, `/reveal-email`, `/outreach` 500 after writing. Fixed on staging; resolves when the branch merges staging.
2. `email_bounced`, `email_actually_sent` not in `models/events.py:EventType` → silently dropped.
3. No server-side deck impression event (only the client echo on swipe).
4. `meeting_scheduled` is a valid rec-event type but nothing writes it.

---

## 2. What we know about candidates (warehouse + online)

**Warehouse** = Firestore `firm_employees/{linkedin_slug | synth:hash}`. Fields: name, title (lowercased), `title_level` (10 substring buckets), company slug, `schools[]`, city/state, photo, `work_email` (+source, verified_at, 90-day TTL), `last_seen_at`.
Missing: experience history, majors, skills, hometown, `joined_year` (always None on the App-shape write path, `schema.py:558`). So half the warmth features can't fire for warehouse people, and scores cluster.

**Online:** Coresignal (search 1 credit + collect ~20 budget units, budget 7400), Hunter (email finder ≥90 conf; people fallback by domain), FullEnrich (email waterfall, $0.055/hit, 450/mo), NeverBounce, Apify (LinkedIn detail, not sourcing), Perplexity (hiring-manager verification). Coresignal results get written back; **Hunter emails never are.**

**Retrieval problems**
- Web Find reader returns the first N docs in **doc-ID order**, then trims to 3/8/15 *before* scoring (`reader.py:350`, `runs.py:483`). Ranking only reorders what was picked at random.
- Title filter is mostly inert (anything mapping to "other" is dropped from the filter); location never filtered on warehouse reads.
- Four surfaces, four ranking behaviors: deck = warmth + MMR, Find = warmth on a pre-trimmed list, MCP = unsorted, find-similar = bucket fill.
- Bugs: ranker dig sends `goldman_sachs` to Coresignal (`ranker.py` `.replace("-", " ")` on an underscore slug); demand metrics use hyphen keys vs underscore shelf keys; synth-ID duplicates never merge.

---

## 3. Target architecture

```
                 ┌───────────────── RankingProfile (per user, cached) ─────────────────┐
                 │ declared · resume · intent (loops/searches/jobs) · affinities ·    │
                 │ negatives · outcome history                                        │
                 └───────────────┬─────────────────────────────────────────────────────┘
                                 │
  request (deck | find | similar | mcp | loop)
        │
        ▼
  1. CANDIDATE GENERATION  — several retrievers, union, dedupe by person key
     R1 target firm × school      (warehouse)
     R2 target firm, any school   (warehouse)
     R3 school × target industry  (warehouse)
     R4 user's past employers / clubs-adjacent firms (warehouse)
     R5 lookalikes of people the user saved / replied to (same firm × role family × level)
     R6 online fill (Coresignal) when the pool for a goal is below threshold — budget-gated
        → pool of 200–400, never trimmed before scoring
        │
        ▼
  2. FILTERS  (hard)  already-contacted · swiped left/skip · blocklist · global suppression ·
                      over-contacted people (cross-user cap) · not currently at firm
        │
        ▼
  3. FEATURES  → one vector per (user, candidate)
        │
        ▼
  4. SCORE  = Relevance × w1 + Warmth × w2 + Reachability × w3 + Affinity × w4 − Fatigue
        │
        ▼
  5. RE-RANK  MMR diversity (company, role family) · 10–15% exploration slots · surface rules
        │
        ▼
  6. SERVE + LOG impression (model_version, variant, rank, features)  → outcomes join back
        │
        ▼
  7. LEARN  nightly: recompute affinities; weekly: refit weights on outcomes
```

One module, `services/people_ranking/`, with a single entry point `rank_people(uid, request) -> list[Ranked]`. Every surface calls it with its own request shape (`company=`, `role=`, `focus=`, query parse, seed person for find-similar). `person_ranker.py` becomes a thin adapter.

### 3a. RankingProfile
Built per deck request by `ranking_profile.load_ranking_profile` (loops, saved jobs, swipes, contacts; each read bounded). No cache yet: add one at `users/{uid}/derived/rankingProfile` if deck latency or read cost shows up.

```
goals:      target_firms[]  (dreamCompanies ∪ loops.companies ∪ firms searched ≥2× in 30d ∪ saved-job companies)
            target_roles[]  (careerTrack, preferredJobRole, loops.roles, search titles)
            target_industries[], target_locations[]
            stage: "explore" (≥2 yrs to grad) | "recruit" (≤1 yr) | "post-grad"
identity:   school_slugs[] (ALL fields: academics.university/school/college, resume education),
            major, past_employers[], clubs[], hometown (when collected)
affinity:   company{slug: w}, role_family{f: w}, level{l: w}   from right-swipes, saves, sends,
            replies; weight by event (reply 5, meeting 8, send 3, save 2, right 1), 30-day half-life
negatives:  companies/role families with ≥3 left swipes and 0 positives, queue blocklist
history:    sends, replies, reply rate — to calibrate how warm the user's emails land
```

Each target carries **provenance + strength** (declared 1.0, loop 1.0, repeated search 0.6, saved job 0.5). This fixes cold start: a user with no dream companies but two Loops and a few searches still gets a targeted deck.

### 3b. Features (all relational, so they transfer across users)
| Group | Feature | Source |
|---|---|---|
| Relevance | firm is a target (strength-weighted) | profile.goals × candidate.company |
| | role family matches target role | title → role family |
| | level fit for the user's stage (explore → analysts/associates; recruit → recruiters, recent hires, managers) | title_level × stage |
| | location match | city/state × target_locations |
| Warmth | same school, same major, past employer overlap, club/greek overlap, hometown | identity × enriched candidate |
| | recently joined (≤2 yrs) — near-peer | joined_year |
| Reachability | verified email on shelf / emailability probability | work_email + source |
| | historical reply rate for this firm × level across all Offerloop sends | outcome aggregate |
| | still at firm (freshness) | last_seen_at age |
| Affinity | user's learned affinity for this firm / role family / level | profile.affinity |
| Fatigue | times this person was emailed by Offerloop users in 30/90 days | cross-user contact count |
| | user already has N contacts at this firm (diminishing returns) | user contacts |

### 3c. Scoring
- **v1 (ship first):** transparent weighted sum of normalized features, weights seeded from today's warmth weights, plus relevance and reachability, which today are missing or only used as tiebreaks. Keep `reasons` and `briefing` from the existing lead-type code; they're the user-facing "why".
- **v2 (when labels exist):** logistic regression on P(reply | sent), plus P(send | shown) as a second head; final score = P(send) × P(reply). Trained on pooled data across users, which works at our size because the features are relational. Gate: start fitting once there are **≥500 sends with a known outcome** (reply or 14 days of silence); before that, use v1 and only adjust weights by hand from the dashboard.
- **Never** let the model downrank people the user explicitly targeted below people they didn't. Relevance stays a floor, so the model only reorders within it.

### 3d. Exploration (not in OFF-41; add before Phase 4 so the learner sees more than v1 already likes)
Reserve ~10–15% of deck slots (positions 4, 9, 14…) for high-uncertainty candidates: new firms in the user's industry, or people with features the model has little data on. Tag them `variant="explore"` so they're separable in evaluation. Without this, the learner only ever sees what v1 already likes.

### 3e. Cross-user fatigue cap — deferred
Not doing this for now (Nick, 2026-10-18). Revisit if reply rates at the most-targeted firms drop as more students from one school use the deck.

---

## 4. Warehouse upgrades (dependency — owned by the backend/data-provider work)
The ranker already reads these fields when present (majors, experience, joined year). As the warehouse fills in, the warmth features that are dead today start firing with no ranker change. Listed so that work knows what the ranker wants:
1. **Store what Coresignal already returns:** experience history (past companies + years), all education entries with majors, `joined_year` on the App-shape path, location, headline. Extend the writer allowlist in `firm_cache/schema.py`, keeping the sensitive-field exclusions.
2. **`hometownSignal`** on firm_employees (documented gap in `person_ranker.py` header).
3. **Proper `title_level` + `role_family`** at write time (reuse `student_job_ranker._title_role_families`); stop discarding "other" in filters.
4. **Freshness:** read `last_seen_at`; treat >180 days as "verify before serving" and let the nightly groom re-collect stale rows for high-demand firms. Stop hard-coding `IsCurrentlyAtTarget=True`.
5. **Identity merge:** when a synth doc later gains a LinkedIn slug (backfill), merge it into the slug doc and leave a tombstone pointer.
6. **Write Hunter-verified emails back** to the shelf (≥90 confidence, with source `hunter`) so the next student doesn't pay again.
7. **Demand-driven warming:** fix the hyphen/underscore key mismatch, then have the nightly warm use RankingProfile target firms × schools (where students' decks are thin) as the demand signal, not just raw search counts.

---

## 5. Online sourcing policy (dependency — owned by the data-provider work; no budget assumed)
The pool for each (target firm × school) goal is checked against a threshold (e.g. 25 rankable people, ≥5 with email). Below it:
1. Coresignal search with **alumni first**, exact firm slug → collect only the top-K by a cheap pre-score (title level/role fit from search hits) rather than everyone.
2. Write back, then re-run retrieval.
3. Budget guard per user per day and a global daily cap; log `cost_per_useful_candidate` (vendor spend ÷ candidates later saved/sent).
Fix the dig slug bug first (`goldman_sachs` → `Goldman Sachs` via `company_display`).

---

## 6. Evaluation and experiments
**Outcome ladder (what "good" means):** shown → saved / right-swiped → sent → replied → **meeting booked → connected** → job landed. The north star is **connections fostered per 100 people shown** (meeting_scheduled or connection_made events), read per arm. Replies are the early read, since meetings take weeks.

**What's logged now (OFF-41):**
- `deck_served` (one row per deck: every person, rank, score, tier, the features that scored them, model_version, variant).
- `person_swipe_*`, `email_revealed`, `outreach_*` (were crashing or dropped before).
- `email_sent`, `email_replied` (already, via the Gmail webhook).
- `meeting_scheduled`, `connection_made` (new, from pipeline stage changes and resolutions). Deck contacts carry the bare candidate id, so every row joins back to `deck_served` by (uid, candidate id).

**A/B:** `ranker_variant(uid)` = sha1 bucket < `PERSON_RANKER_V1_PERCENT` (default 50). Both arms get the bug fixes; only v1 gets the profile points and the wider target sourcing. Read the arms on send-through first, then replies, then connections.

**Offline:** replay harness over `deck_served` rows (re-rank a past deck, score against what the student did) and a golden set of ~25 personas run in CI. Not built yet; next after a few weeks of logs.

**Job landed:** nothing records it today (`offer_received` is a valid event type, but nothing writes it). Needs a way for the student to say "I got the offer / I got a referral here", most simply a resolution on the contact (`offer` / `referral`) or a status on the saved job. That's a product decision about where the button lives, so it's left as its own ticket.

## 7. Collection changes
No onboarding changes (Nick, 2026-10-18). The profile already asks for target firms and roles later; OFF-41 reads them from there. The one small capture added: a swipe now stores the person's firm and title on `peoplePreferences`, so repeated passes on a firm become a negative signal.

## 8. Phased delivery
### Phase 0 — Deck plumbing ✅ OFF-41
- Recommendation logger accepts `doc_id` and the deck event types (swipe/reveal/outreach were returning 500 after their writes landed on `main`)
- Deck alumni pool reads every school field (`academics.university` from web, `academics.school` from the app)
- Dig sends "goldman sachs", not the underscore slug, to the search
- Each target firm gets a fair share of the candidate pool (the first firm used to fill it)
- Email-ready-first reorder stays inside each run of one tier (it used to pull later members of a tier above other tiers)
- Server-side `deck_served` impressions; `meeting_scheduled` / `connection_made` outcome events

### Phase 1 — RankingProfile ✅ OFF-41
`services/ranking_profile.py`: targets (declared + Loops + saved jobs, with strength), target roles (Settings + Loops), recruiting stage (grad year), company and role-family affinity from the student's pipeline weighted toward meetings/connections with a 60-day half-life, and negative firms from repeated passes.

### Phase 2 — v1 scoring on the People deck ✅ OFF-41 (A/B)
`person_ranker._personal_points`: warmth + target firm (12 × strength, minus warmth's dream +10) + target role (10) + stage fit (6) + firm affinity (≤12) + role-family affinity (≤8) − repeatedly-passed firm (15). Features are logged with each deck.

### Phase 3 — One ranker for every surface (next)
Web Find still trims to 3/8/15 before scoring; MCP doesn't sort; find-similar doesn't score. Move them onto `rank_people_for_user` (or a shared scoring core) once the deck A/B reads positive. Web Find's overfetch touches the warehouse reader, so coordinate with the backend work.

### Phase 4 — Learn the weights (when ≥500 sends have outcomes)
Training table = `deck_served` features × outcome (send, reply, meeting, connection). Fit P(connection | shown) with a logistic model; store weights in `meta/rankerWeights/{version}`; shadow-score before switching.

### Phase 5 — Job landed
Capture it (see §6), then add it as the top rung of the outcome ladder and the affinity weights.

## 9. Open items
- Where the "I got the offer / referral" capture lives (contact resolution vs saved job status).
- When to move v1 from 50% to 100%: suggested after two weeks if v1's send-through and reply rates are no worse.


## What OFF-41 shipped (files)
- `backend/app/utils/recommendation_events.py` — `doc_id`, deck + outcome event types
- `backend/app/services/ranking_profile.py` — new
- `backend/app/services/person_ranker.py` — variant split, profile-driven sourcing and scoring, school fix, fair per-firm pool, `features_out`
- `backend/app/routes/ranker.py` — `deck_served` logging, model_version/variant on every deck event, swipe stores firm/title, dig name fix, tier-run reorder
- `backend/app/services/outbox_service.py` — `meeting_scheduled` / `connection_made` events
- Tests: `test_recommendation_events.py`, `test_ranking_profile.py`, `test_person_ranker_personalization.py`
