# Org directory ingestion (USC TKE pilot)

One-off scripts that built the first "org affiliation" data layer, separate
from the `firm_employees` search warehouse. Pilot case: the USC TKE alumni
directory (2,127 members). Written 2026-10-16/17, handed off for Nick to
test and extend.

## What this is for

The product goal (not yet built): users self-select orgs they belong to in
their profile, and contact cards show a badge ("USC TKE Alumni '17") when a
contact matches an ingested directory the viewer also belongs to. These
scripts only build the data layer. None of the UI, the "Alumni mode" toggle,
or the backend route that would surface a badge exist yet.

## Pipeline, in order

1. **`org_directory_parse_tke.py`** — parses the TKE CSV's specific export
   format (not real tabular CSV, see the file's docstring) into normalized
   records. **TKE-specific.** The next org's directory will need its own
   parser; nothing else downstream is.
2. **`org_directory_ingest_tke.py`** — writes `orgs` / `people` /
   `person_org_links` to Firestore. All 2,127 records kept as-is, nothing
   filtered (deceased, lost, undergrad all included — filtering happens at
   query time, not ingest time, per Rylan 2026-10-17).
3. **`org_directory_enrich_fullenrich.py`** — for people flagged
   `membershipStatus: lost` (TKE's own low-confidence flag), tries to find a
   current verified email via FullEnrich.
4. **`org_directory_enrich_coresignal.py`** — for `lost` people FullEnrich
   didn't resolve, searches Coresignal by name + school to recover a current
   LinkedIn profile, employer, and photo. Has a real lesson baked in: an
   earlier version of this script silently mislabeled ~125 people when
   Coresignal's Collect credits ran out mid-run — read the docstring before
   touching the error handling.
5. **`org_directory_mirror_to_warehouse.py`** — mirrors only the
   enrichment results with a clean, verified company name into the shared
   `firm_employees` warehouse, so other surfaces (website, extension, MCP)
   don't re-pay a provider for the same person. Deliberately leaves out
   anything with just a raw, unsplit directory headline as "company."

## State as of 2026-10-17 (USC TKE)

| Step | Result |
| --- | --- |
| Ingested | 2,127 records, all kept |
| FullEnrich (targeted the 311 "lost") | 62 verified emails found |
| Coresignal (targeted remaining "lost", no email) | 61 matched (47 with a clean company), 128 no match, 33 ambiguous (skipped rather than guessed), 27 blocked on exhausted Coresignal Collect credits |
| Warehoused into `firm_employees` | 47 records |

Still open: the 27 blocked people (re-run `org_directory_enrich_coresignal.py`
once Coresignal Collect credits are topped up — it's idempotent, it'll only
touch the untouched ones), and the 76 enriched-but-no-clean-company records
that can't warehouse yet.

## Credentials

Every script takes its credentials as a file path, never a pasted secret:
- `--cred` — a Firebase service account JSON (Firebase console → Project
  Settings → Service Accounts → Generate new private key)
- `--fullenrich-key-file` / `--coresignal-key-file` — a plain text file
  holding just that API key, copied from the Render env var on the backend
  service

None of these are committed. Don't start one of these scripts from inside a
checkout that also holds a key file unless it's gitignored.

## What's reusable for the next org vs. not

Reusable as-is: the `orgs`/`people`/`person_org_links` schema, both
enrichment scripts, the warehouse mirror script (now takes `--school` as a
flag rather than hardcoding USC).

Needs a new file per source: only the parser
(`org_directory_parse_tke.py` → copy the pattern, not the TKE-specific
regexes, for the next directory's actual export shape). If a generic
ingestion format gets built later (per the original product brief), this
parser becomes the first of several adapters feeding one shared loader
instead of a one-off script.

## Making an org live in the app (OFF-42)

After ingesting, two steps make an org joinable:

1. `org_directory_seed_org.py` sets the short name, school, the words
   students type for it ("tke"), and `joinable: true`.
2. `org_directory_set_password.py --org-id <id>` sets the join password at
   a hidden prompt. Only a hash is stored, in `org_secrets/{id}`. Students
   join in Account Settings or onboarding by typing it; the right password
   makes them an active member right away. An org with no password can't be
   joined, and wrong guesses lock out for 15 minutes after 8 tries.
