"""Coresignal name+school lookup for USC TKE 'lost' people who still have no
verified email after the FullEnrich pass (org_directory_enrich_fullenrich.py).
Explicitly SKIPS anyone who already has a workEmail.

Two-credit-type split, mirrors coresignal_client's own design:
  Search  (1 credit/call, ~20K/mo on the Pro plan, NOT tracked in the
           Firestore spend doc at all) -> run for every target, cheap.
  Collect (the metered one, ~20 spent_estimate units/call) -> only spent on
           an unambiguous match, so money only goes toward matches we trust.

Ambiguity rule: multiple name hits at USC is expected for common names.
  - 1 hit -> collect, use it.
  - >1 hit AND the person has a directory headline -> collect up to 3
    candidates, keep the first whose current company_name appears in the
    directory headline text (case-insensitive substring).
  - >1 hit, no headline to disambiguate against -> SKIP. Guessing which
    "Michael Smith" is ours risks writing someone else's data onto this
    person's record, which is worse than leaving it blank.

IMPORTANT, learned the hard way: Coresignal's real Collect credits can run
out mid-run (402 "Billing: Insufficient credits"). The retry/backoff/
fail-fast logic below exists specifically so a credit exhaustion gets
reported as an error, not silently mislabeled as "ambiguous_skip" (an
earlier version of this script did exactly that for ~125 records before the
bug was caught — a flat 0% match rate for 124 consecutive records after a
clean cutoff point was the tell). A re-run after a failure picks up exactly
where it left off: coresignalAttemptedAt is only stamped on a real result,
never on an API failure.

Writes onto people/{personKey}: linkedinUrl, photoUrl, coresignalCompany,
coresignalTitle, coresignalMatchCount, coresignalResult, coresignalAttemptedAt.
Never touches workEmail (that stays FullEnrich's field).

Usage:
  python org_directory_enrich_coresignal.py --cred <fs.json> --coresignal-key-file <k.txt> --dry-run
  python org_directory_enrich_coresignal.py --cred <fs.json> --coresignal-key-file <k.txt> --live

--coresignal-key-file is a plain text file holding only the
CORESIGNAL_API_KEY value (Render env var on the backend service). Never
commit it.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ORG_ID = "usc_tke"
BASE = "https://api.coresignal.com/cdapi/v2/employee_multi_source"
SEARCH_ENDPOINT = f"{BASE}/search/es_dsl"
COLLECT_ENDPOINT = f"{BASE}/collect"

USC_SCHOOLS = [
    "University of Southern California",
    "USC Marshall School of Business",
    "USC Viterbi School of Engineering",
]


def _headers(key: str) -> dict:
    return {"apikey": key, "Content-Type": "application/json"}


class ApiFailure(Exception):
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body
        super().__init__(f"status {status_code}: {body[:200]}")


def _request_with_retry(method: str, url: str, key: str, **kwargs):
    """One retry after a backoff on a non-200. Raises ApiFailure if both fail,
    so the caller can tell a real error apart from a genuine empty result."""
    for attempt in (1, 2):
        r = requests.request(method, url, headers=_headers(key), timeout=25, **kwargs)
        if r.status_code == 200:
            return r
        if attempt == 1:
            time.sleep(5 if r.status_code == 429 else 2)
    raise ApiFailure(r.status_code, r.text)


def _search(key: str, full_name: str) -> list[int]:
    query = {
        "query": {
            "bool": {
                "must": [
                    {"match": {"full_name": {"query": full_name, "operator": "and"}}},
                    {
                        "nested": {
                            "path": "education",
                            "query": {
                                "bool": {
                                    "should": [
                                        {"match_phrase": {"education.institution_name": s}}
                                        for s in USC_SCHOOLS
                                    ],
                                    "minimum_should_match": 1,
                                }
                            },
                        }
                    },
                ]
            }
        }
    }
    r = _request_with_retry("POST", SEARCH_ENDPOINT, key, json=query)
    ids = r.json()
    return ids if isinstance(ids, list) else []


def _collect(key: str, profile_id: int) -> dict | None:
    r = _request_with_retry("GET", f"{COLLECT_ENDPOINT}/{profile_id}", key)
    return r.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cred", required=True)
    ap.add_argument("--coresignal-key-file", required=True)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--live", action="store_true")
    args = ap.parse_args()

    key = Path(args.coresignal_key_file).read_text().strip()

    import firebase_admin
    from firebase_admin import credentials, firestore
    from google.cloud.firestore_v1.base_query import FieldFilter

    cred = credentials.Certificate(args.cred)
    firebase_admin.initialize_app(cred)
    db = firestore.client()

    links = list(
        db.collection("person_org_links")
        .where(filter=FieldFilter("orgId", "==", ORG_ID))
        .where(filter=FieldFilter("membershipStatus", "==", "lost"))
        .stream()
    )

    targets = []
    for link in links:
        pk = link.to_dict().get("personKey")
        if not pk:
            continue
        pdoc = db.collection("people").document(pk).get()
        if not pdoc.exists:
            continue
        pdata = pdoc.to_dict() or {}
        if pdata.get("workEmail"):
            continue  # already found via FullEnrich, don't touch
        if pdata.get("coresignalAttemptedAt"):
            continue  # idempotent: don't re-search on a re-run
        full_name = f"{pdata.get('firstName') or ''} {pdata.get('lastName') or ''}".strip()
        targets.append((pk, full_name, pdata.get("directoryHeadline") or ""))

    print(f"{len(targets)} targets (lost, no email yet, not yet coresignal-attempted)")

    if args.dry_run:
        for t in targets[:5]:
            print(" ", t)
        return

    now = lambda: datetime.now(timezone.utc).isoformat()
    stats = {"no_match": 0, "matched": 0, "ambiguous_skip": 0, "api_error": 0}
    consecutive_api_failures = 0

    for i, (pk, name, headline) in enumerate(targets):
        try:
            ids = _search(key, name)
        except ApiFailure as e:
            consecutive_api_failures += 1
            stats["api_error"] += 1
            print(f"  [{i}] search API_FAILURE for {name!r}: {e}")
            # Do NOT stamp coresignalAttemptedAt: leave untouched so a re-run retries it.
            if consecutive_api_failures >= 5:
                print(f"\nABORTING: {consecutive_api_failures} consecutive API failures "
                      f"(likely rate-limited, out of credits, or a bad key). Stopping "
                      f"instead of grinding through and mislabeling results. "
                      f"Processed {i}/{len(targets)}.")
                print("final (partial):", stats)
                return
            time.sleep(10)
            continue
        consecutive_api_failures = 0

        result_doc = {"coresignalAttemptedAt": now(), "coresignalMatchCount": len(ids)}
        chosen = None
        collect_failed = False
        try:
            if len(ids) == 1:
                chosen = _collect(key, ids[0])
            elif len(ids) > 1 and headline:
                for cid in ids[:3]:
                    prof = _collect(key, cid)
                    active = [e for e in (prof.get("experience") or []) if e.get("active_experience")]
                    for e in active:
                        co = (e.get("company_name") or "").strip()
                        if co and co.lower() in headline.lower():
                            chosen = prof
                            break
                    if chosen:
                        break
        except ApiFailure as e:
            collect_failed = True
            print(f"  [{i}] collect API_FAILURE for {name!r}: {e}")

        if collect_failed:
            # Leave coresignalAttemptedAt UNSET so this one is retried, not
            # permanently mislabeled as ambiguous/no-match.
            stats["api_error"] += 1
            consecutive_api_failures += 1
            if consecutive_api_failures >= 5:
                print(f"\nABORTING: {consecutive_api_failures} consecutive API failures. "
                      f"Processed {i}/{len(targets)}.")
                print("final (partial):", stats)
                return
            time.sleep(10)
            continue
        consecutive_api_failures = 0

        if chosen:
            active = [e for e in (chosen.get("experience") or []) if e.get("active_experience")]
            co = active[0].get("company_name") if active else None
            title = active[0].get("position_title") if active else None
            result_doc.update({
                "coresignalResult": "matched",
                "linkedinUrl": chosen.get("linkedin_url") or chosen.get("professional_network_url"),
                "photoUrl": chosen.get("picture_url"),
                "coresignalCompany": co,
                "coresignalTitle": title,
            })
            stats["matched"] += 1
        elif ids:
            result_doc["coresignalResult"] = "ambiguous_skip"
            stats["ambiguous_skip"] += 1
        else:
            result_doc["coresignalResult"] = "no_match"
            stats["no_match"] += 1

        db.collection("people").document(pk).set(result_doc, merge=True)
        time.sleep(0.5)  # pacing; the original run hit real credit exhaustion around call 150

        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{len(targets)} done — {stats}")

    print("\nfinal:", stats)


if __name__ == "__main__":
    main()
