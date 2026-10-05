"""firm_cache.writer — JIT cache write path.

Called from pdl_client.search_contacts_from_prompt right after PDL returns
fresh results. Normalizes each contact through schema.firm_employee_doc_*
and batch-upserts into firm_employees/{linkedin_id}.

Design invariants (in priority order):
  1. NEVER break a search. Every failure path is caught + logged; the
     search's return value is not affected by cache-write outcome.
  2. NEVER add user-visible latency. All Firestore writes go through
     one batched .commit() at the end, not per-doc awaits.
  3. Off by default. ENABLE_FIRM_CACHE_WRITE env flag gates every call so
     Phase 2 can ship dark and be enabled independently of Phase 3.

Firestore batch limit is 500 ops per commit. Contact-search results max at
~15 per request (Elite tier), so one batch per call is always enough.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Iterable, Optional

from google.cloud.firestore import SERVER_TIMESTAMP

from app.extensions import get_db
from app.services.firm_cache.schema import (
    FIRM_EMPLOYEES_COLLECTION,
    firm_employee_doc_from_app_contact,
    firm_employee_doc_from_pdl_person,
)

logger = logging.getLogger(__name__)

_FLAG_ENV = "ENABLE_FIRM_CACHE_WRITE"


def _flag_enabled() -> bool:
    """Cheap env-var check. Reading os.environ per-call is fine — the
    flag flip is expected at deploy boundary, not hot-path."""
    raw = os.environ.get(_FLAG_ENV, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def cache_pdl_contacts(
    contacts: Iterable[dict],
    *,
    shape: str = "app",
    async_write: bool = True,
) -> int:
    """Upsert PDL-returned contacts into firm_employees.

    Args:
        contacts: iterable of contact dicts. For shape="app", these are the
            flattened contact dicts returned by pdl_client.search_contacts_*
            (keys: FirstName, LastName, LinkedIn, Company, Title, City, State,
            College, EducationTop, pdlId). For shape="raw", these are raw
            PDL person dicts (with education[], experience[], location_country).
        shape: "app" (default) or "raw". Determines which normalizer to use.
        async_write: if True (default), fire the Firestore batch on a daemon
            thread so the caller sees no latency. Set False for tests.

    Returns:
        Count of docs *queued* for upsert (may be less than input length —
        contacts missing a current company, non-US, or with nothing at all
        to key on (no LinkedIn, no email, no name) are silently skipped by
        the normalizer. A contact with no LinkedIn but a usable email or
        name still gets a doc, keyed by a synthesized id instead of a
        LinkedIn slug — 2026-10-05, see schema.py's
        _synthesize_person_key).
        Returns 0 if the feature flag is off or any pre-flight check fails.
    """
    if not _flag_enabled():
        return 0

    if not contacts:
        return 0

    normalizer = (
        firm_employee_doc_from_app_contact if shape == "app"
        else firm_employee_doc_from_pdl_person
    )

    docs: list[dict] = []
    for c in contacts:
        try:
            doc = normalizer(c)
        except Exception as exc:  # noqa: BLE001 — never let a bad contact break the batch
            logger.debug("firm_cache normalizer raised on contact: %s", exc)
            continue
        if doc is not None:
            docs.append(doc)

    if not docs:
        return 0

    if async_write:
        # Fire-and-forget on a daemon thread. If the process dies mid-write
        # we lose this batch — acceptable, next search re-writes the same
        # people. Never delay the user's response for cache writes.
        threading.Thread(
            target=_safe_batch_upsert,
            args=(docs,),
            daemon=True,
            name="firm-cache-write",
        ).start()
    else:
        _safe_batch_upsert(docs)

    return len(docs)


def _safe_batch_upsert(docs: list[dict]) -> None:
    """Actually perform the Firestore batch write. Never raises.

    Uses set(merge=True) so subsequent writes update fields rather than
    overwriting the whole doc — preserves any fields future phases add
    (e.g., last_seen_by_user_count, quality_signals) without stomping."""
    try:
        db = get_db()
        if db is None:
            logger.debug("firm_cache: get_db() returned None, skipping batch")
            return
        batch = db.batch()
        col = db.collection(FIRM_EMPLOYEES_COLLECTION)
        for doc in docs:
            payload = dict(doc)  # shallow copy — don't mutate caller's data
            payload["last_seen_at"] = SERVER_TIMESTAMP
            ref = col.document(doc["linkedin_id"])
            batch.set(ref, payload, merge=True)
        batch.commit()
        logger.info("firm_cache: upserted %d docs", len(docs))
    except Exception as exc:  # noqa: BLE001
        # Metering-style discipline: cache writes must NEVER surface to
        # user requests. Log and swallow.
        logger.warning("firm_cache batch write failed (non-fatal): %s", exc)


# ─────────────────────────────────────────────────────────────────────────
# Test / eval helpers (safe to call from ipython or an admin route)
# ─────────────────────────────────────────────────────────────────────────


def preview_cache_docs(
    contacts: Iterable[dict],
    *,
    shape: str = "app",
) -> list[Optional[dict]]:
    """Run the normalizer on `contacts` without writing anything. Useful
    to sanity-check a real PDL response shape before flipping the flag."""
    normalizer = (
        firm_employee_doc_from_app_contact if shape == "app"
        else firm_employee_doc_from_pdl_person
    )
    return [normalizer(c) for c in contacts]
