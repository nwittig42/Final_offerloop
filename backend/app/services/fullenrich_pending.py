"""Recover FullEnrich results we stopped waiting for.

FullEnrich's waterfall walks 25+ providers and takes 15 to 90 seconds,
slowest on a miss. Search can't block that long, so it gives up at
FE_SEARCH_WAIT_SECONDS and moves on — and until now that was the end of it:
the job kept running server-side, finished, and nobody ever read the answer.

Measured 2026-10-18 on one real search (job 6db0894a, three Apple contacts):
we abandoned at 75s, the job finished moments later with TWO DELIVERABLE
addresses. The student saw none of them, the warehouse cached none of them,
and the next student to search those people would have paid to find them
again.

So instead of discarding the id, park it here. A later request sweeps the
parked jobs, reads whatever finished, and feeds the addresses to the shared
warehouse. Nobody waits — the beneficiary is the next search, not this one.

Durable on purpose: a daemon thread would lose the ticket on the next deploy
or reload, which is exactly when a 90-second job is most likely in flight.

Collection:
    fullenrich_pending/{enrichment_id}
        enrichment_id  str
        people         list  [{pid, first_name, last_name, company, linkedin_url}]
        created_at     str   ISO8601 UTC
        attempts       int   sweeps that found it unfinished
        resolved_at    str   ISO8601 UTC, set once terminal
        outcome        str   "recovered" | "empty" | "expired"
        recovered      int   addresses written to the warehouse
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

COLLECTION = "fullenrich_pending"

#: Stop chasing a job after this long. FullEnrich's own worst case is ~90s;
#: anything still unfinished after an hour is never finishing.
MAX_AGE_SECONDS = int(os.environ.get("FE_PENDING_MAX_AGE_SECONDS", "3600"))

#: Jobs to resolve per sweep. Each is one HTTP GET with no wait, so this is
#: cheap — the cap exists so a backlog can't lengthen a user's search.
SWEEP_LIMIT = int(os.environ.get("FE_PENDING_SWEEP_LIMIT", "5"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime) -> str:
    return ts.isoformat()


def _parse(stamp: Any) -> Optional[datetime]:
    if isinstance(stamp, datetime):
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    if isinstance(stamp, str) and stamp:
        try:
            ts = datetime.fromisoformat(stamp.rstrip("Z"))
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def record_pending(
    enrichment_id: str,
    people: Sequence[Tuple[str, str, str, str, str]],
    db=None,
) -> bool:
    """Park an unfinished job. `people` is start_bulk's own input shape,
    [(pid, first, last, company, linkedin_url)], so the sweeper can rebuild
    contacts without the request that started it. Never raises."""
    if not enrichment_id or not people:
        return False
    try:
        if db is None:
            from app.extensions import get_db
            db = get_db()
        if db is None:
            return False
        db.collection(COLLECTION).document(enrichment_id).set({
            "enrichment_id": enrichment_id,
            "people": [
                {
                    "pid": str(p[0]),
                    "first_name": p[1] or "",
                    "last_name": p[2] or "",
                    "company": p[3] or "",
                    "linkedin_url": p[4] or "",
                }
                for p in people
            ],
            "created_at": _iso(_now()),
            "attempts": 0,
            "resolved_at": None,
            "outcome": None,
            "recovered": 0,
        })
        logger.info("fe_pending: parked %s (%d people)", enrichment_id, len(people))
        return True
    except Exception:
        logger.exception("fe_pending: could not park %s", enrichment_id)
        return False


def _as_contact(person: Dict[str, Any], email: str) -> Dict[str, Any]:
    """App-shape contact carrying just enough for the warehouse normalizer.
    EmailVerified=True is honest here: only sellable_gate-approved addresses
    reach this function."""
    return {
        "FirstName": person.get("first_name") or "",
        "LastName": person.get("last_name") or "",
        "Company": person.get("company") or "",
        "LinkedIn": person.get("linkedin_url") or "",
        "Email": email,
        "WorkEmail": email,
        "EmailSource": "fullenrich_backfill",
        "EmailVerified": True,
    }


def _finalize(doc_ref, outcome: str, recovered: int = 0) -> None:
    try:
        doc_ref.set({
            "resolved_at": _iso(_now()),
            "outcome": outcome,
            "recovered": recovered,
        }, merge=True)
    except Exception:
        logger.exception("fe_pending: could not finalize %s", outcome)


def sweep(db=None, limit: int = SWEEP_LIMIT) -> int:
    """Resolve parked jobs that have since finished; return the number of
    addresses written to the warehouse. One no-wait GET per job. Never
    raises — a sweep failure must never break the search that triggered it."""
    recovered_total = 0
    try:
        if db is None:
            from app.extensions import get_db
            db = get_db()
        if db is None:
            return 0

        from app.services import fullenrich_client

        docs = list(
            db.collection(COLLECTION)
            .where("resolved_at", "==", None)
            .limit(max(1, int(limit)))
            .stream()
        )
        if not docs:
            return 0

        cutoff = _now() - timedelta(seconds=MAX_AGE_SECONDS)
        for snap in docs:
            data = snap.to_dict() or {}
            eid = data.get("enrichment_id") or snap.id
            created = _parse(data.get("created_at"))

            if created is not None and created < cutoff:
                _finalize(snap.reference, "expired")
                logger.info("fe_pending: %s expired unfinished", eid)
                continue

            ready, hits = fullenrich_client.fetch_bulk(eid, wait_seconds=0, db=db)
            if not ready:
                try:
                    snap.reference.set(
                        {"attempts": int(data.get("attempts") or 0) + 1}, merge=True)
                except Exception:
                    pass
                continue

            by_pid = {str(p.get("pid")): p for p in (data.get("people") or [])}
            contacts: List[Dict[str, Any]] = []
            for pid, hit in (hits or {}).items():
                person = by_pid.get(str(pid))
                email = ((hit or {}).get("email") or "").strip().lower()
                if person and email:
                    contacts.append(_as_contact(person, email))

            if contacts:
                try:
                    from app.services.firm_cache.writer import cache_pdl_contacts
                    cache_pdl_contacts(contacts, shape="app", async_write=False)
                except Exception:
                    logger.exception("fe_pending: warehouse write failed for %s", eid)

            recovered_total += len(contacts)
            _finalize(snap.reference, "recovered" if contacts else "empty", len(contacts))
            logger.info("fe_pending: %s resolved — %d address(es) recovered",
                        eid, len(contacts))

        return recovered_total
    except Exception:
        logger.exception("fe_pending: sweep failed")
        return recovered_total
