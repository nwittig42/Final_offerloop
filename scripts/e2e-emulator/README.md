# Isolated end-to-end testing (Firebase Emulator Suite)

Runs the whole signup → onboarding → walkthrough → Gmail → send → Contacts
journey locally against the Firebase emulators. Production Firebase, real
Gmail and paid APIs are unreachable unless you explicitly opt in.

## What's real and what's faked

| Piece | In this setup |
|---|---|
| Frontend, backend, routes, Firestore rules | Real code (rules loaded into the emulator) |
| Firebase Auth / Firestore / Storage | Local emulators (ports 9099 / 8180 / 9199, UI on 4000) |
| Recommended people | 4 synthetic people seeded by `seed.py` (all marked `e2e_synthetic`) |
| Email lookup | Real reveal route; the address comes from the seeded record. Hunter / FullEnrich / Apify / PDL / Coresignal keys are forced empty |
| Resume parse + email draft | Fake by default (`E2E_FAKE_LLM=1`); real OpenAI only with `E2E_FAKE_LLM=0` |
| Gmail OAuth + send | Fake by default (`E2E_FAKE_GMAIL=1`): a local consent page (allow / partial / cancel) drives the **real** OAuth callback, credential storage and scope checks, and an in-memory Gmail records sends (`GET http://127.0.0.1:5002/e2e/fake-gmail/sent`) |

`e2e_server.py` refuses to start if any emulator host is missing, if Google
Application Default Credentials are available, or if any vendor key is set.
Firebase Admin runs with an anonymous credential (`OFFERLOOP_E2E_EMULATOR=1`
in `backend/app/extensions.py`), so it cannot authenticate to a real project.

## One-time setup

```bash
brew install openjdk@21
npm install -g firebase-tools
```

Copy the variables below into `.env.e2e.local` at the repo root (gitignored).
Only the whitelisted keys are read; see `ALLOWED_FROM_LOCAL_ENV` in
`e2e_server.py`.

```
E2E_TEST_ACCOUNT_EMAIL=avery.e2e@example.test
E2E_TEST_ACCOUNT_PASSWORD=<any emulator-only password>
E2E_RECIPIENT_EMAIL=<an inbox you control>
FIRST_OUTREACH_FREE_SINCE=2020-01-01
# Real integrations, only for an approved real test:
# E2E_FAKE_GMAIL=0
# GOOGLE_CLIENT_ID=<separate dev OAuth client, NOT production's>
# GOOGLE_CLIENT_SECRET=
# E2E_FAKE_LLM=0
# OPENAI_API_KEY=<spend-capped key>
```

## Run

Three terminals from the repo root (or the `e2e-*` entries in `.claude/launch.json`):

```bash
scripts/e2e-emulator/run-emulators.sh     # emulators + UI at http://127.0.0.1:4000
scripts/e2e-emulator/run-backend.sh       # backend at http://127.0.0.1:5002
scripts/e2e-emulator/run-frontend.sh      # app at http://127.0.0.1:8081
.venv/bin/python scripts/e2e-emulator/seed.py               # synthetic people
.venv/bin/python scripts/e2e-emulator/make_test_resume.py   # data/e2e-test-resume.pdf
```

Use `http://127.0.0.1:8081` (not `localhost`): the OAuth return goes there,
and it keeps the test session separate from your normal local sign-in.

## Walkthrough checklist

1. Sign up with email + password (Auth emulator) → lands on `/onboarding`.
2. Get started → upload `data/e2e-test-resume.pdf` → Continue.
3. Confirm screen: USC, Economics, 2027, finance/consulting roles and firms. Edit a field, refresh: the edit stays.
4. Looks good → `/get-started` shows the 4 seeded people.
5. Morgan Mockford (no email) → "nothing was charged"; the free benefit is kept.
6. Taylor Testwell → free reveal ("on us") → draft. Edit it, refresh: kept.
7. Connect Gmail → "Continue with a permission unticked" → back on the draft with the declined-permission message.
8. Connect Gmail → Allow → "Gmail is connected".
9. Send email → confirm → lands on `/contacts?contact=ranker_e2e-taylor-testwell`, stage Contacted.
10. Check `GET /e2e/fake-gmail/sent`: one message, edited body, no "Re:", resume attached.

Inspect data in the Emulator UI (http://127.0.0.1:4000): `users/{uid}`
(`walkthrough`, `credits`), `entitlements/firstOutreach`,
`firstEmailSends/*`, `sendQuota/*`, `contacts/*`.

## Real Gmail / OpenAI test (needs explicit approval)

Set `E2E_FAKE_GMAIL=0` with a **separate** Google OAuth client whose
authorized redirect URI is `http://127.0.0.1:5002/api/google/oauth/callback`
and the test Gmail account added as a test user. Set `E2E_FAKE_LLM=0` with a
spend-capped OpenAI key. Set `E2E_RECIPIENT_EMAIL` to an inbox you control
and reseed, so Taylor Testwell's address is yours. Only Taylor should be
emailed.
