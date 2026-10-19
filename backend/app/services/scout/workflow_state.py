"""Scout workflow-state read tools (Phase 5, Stage 2).

Eight read-only Firestore wrappers that let Scout pull workflow state from
across the product when it needs to ground a chat response or a strategy
discussion in what the user has actually done. Read-only by design: the
workflow pages remain the source of truth and Scout never writes through
these tools.

Each function returns a small JSON-safe dict optimized for the LLM context
(no datetime objects, no raw Firestore snapshots, no long blobs like cover
letter bodies). Aim is under 2 KB returned per call in the typical case.

Tier-independent: a Free user can ask Scout how many emails they sent the
same way an Elite user can. These tools enforce no tier gating.

Best-effort Firestore: when the database is unavailable a read returns the
documented empty-shape envelope. Nothing here raises.

Collection paths (audited against the production code, not invented):
  outbox          users/{uid}/contacts          (filtered to inOutbox == True)
  searches        users/{uid}/searchHistory
  cover letters   users/{uid}/cover_letter_library
  meeting preps   users/{uid}/coffee-chat-preps
  firm searches   users/{uid}/firmSearches
  applications    users/{uid}/autoApplyJobs     (auto-apply queue)
  loops           users/{uid}/loops             (recurring outreach agents)

When the interview prep feature is rebuilt later, add a reader function
following the get_meeting_prep_drafts pattern. The Application Lab and
Interview Prep backends were removed end-to-end in the Phase 5 cleanup;
restoring either is a fresh build, not a revert.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.models.pipeline_stages import AWAITING_REPLY_STAGES


# Stage normalization for the outbox read. Mirrors the canonical pipeline
# stages defined in outbox_service.py; kept local so a Scout read does not
# import from the outbox service (the read should survive churn there). The
# awaiting-reply set comes from the side-effect-free models/pipeline_stages.
_REPLIED_STAGES = frozenset({"replied", "meeting_scheduled", "connected"})
_AWAITING_REPLY_STAGES = AWAITING_REPLY_STAGES
_DRAFTING_STAGES = frozenset({"new", "draft_created", "draft_deleted"})

# A waiting contact past this many days surfaces as "no_reply_Nd" so Scout
# can flag stalls without the LLM doing the math.
_NO_REPLY_THRESHOLD_DAYS = 7

# Empty-shape envelopes per tool, used when the db is unavailable or uid is
# missing. Keeping these as constants makes the contract obvious at the call
# site (callers can rely on the keys always being present).
_EMPTY_OUTBOX = {"total_contacts": 0, "awaiting_reply": 0, "replied": 0, "recent": []}
_EMPTY_LIST = {"count": 0, "recent": []}


# ===========================================================================
# Internal helpers
# ===========================================================================

def _db():
    """The Firestore client, or None when it is unavailable."""
    try:
        from app.extensions import get_db
        return get_db()
    except Exception:
        return None


def _subcollection(db, uid: str, name: str):
    """The users/{uid}/{name} subcollection reference."""
    return db.collection("users").document(uid).collection(name)


def _coerce_datetime(value: Any) -> Optional[datetime]:
    """Best-effort parse of a stored timestamp into a timezone-aware datetime.

    Firestore returns DatetimeWithNanoseconds (a datetime subclass) for
    Timestamp fields, but several writers use isoformat() strings instead
    (meeting prep, cover letters). Normalize both. Naive datetimes are
    assumed UTC so downstream math stays consistent.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # Python's fromisoformat handles the formats datetime.utcnow().isoformat()
        # and datetime.now().isoformat() produce, with or without a trailing Z.
        try:
            cleaned = text[:-1] if text.endswith("Z") else text
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


def _iso(value: Any) -> Optional[str]:
    """ISO-8601 string for a stored timestamp, or None when unparseable."""
    dt = _coerce_datetime(value)
    return dt.isoformat() if dt is not None else None


def _days_since(value: Any, now: Optional[datetime] = None) -> Optional[int]:
    """Whole days between a stored timestamp and now, or None if unparseable."""
    dt = _coerce_datetime(value)
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, (now - dt).days)


def _stream(coll) -> List[Any]:
    """Return all snapshots in a collection. Best-effort: errors degrade to []."""
    try:
        return [d for d in coll.stream() if d is not None]
    except Exception as e:
        print(f"[ScoutWorkflowState] stream failed: {e}")
        return []


def _sort_by_timestamp(items: List[Dict[str, Any]], key: str, *, reverse: bool = True) -> None:
    """In-place sort by a timestamp field, treating missing values as oldest."""
    _floor = datetime.min.replace(tzinfo=timezone.utc)
    items.sort(key=lambda d: _coerce_datetime(d.get(key)) or _floor, reverse=reverse)


def _truncate(text: Any, max_chars: int) -> str:
    """Coerce to a trimmed, length-capped string."""
    if text is None:
        return ""
    s = str(text).strip()
    return s[:max_chars]


# ===========================================================================
# 1. Outbox status
# ===========================================================================

def _normalize_outbox_status(stage: Optional[str], days_since_send: Optional[int]) -> str:
    """Map a raw pipelineStage plus elapsed time to the LLM-friendly status."""
    if stage in _REPLIED_STAGES:
        return "replied"
    if stage == "bounced":
        return "bounced"
    if stage == "closed":
        return "closed"
    if stage == "no_response":
        return "no_response"
    if stage in _AWAITING_REPLY_STAGES:
        if days_since_send is not None and days_since_send >= _NO_REPLY_THRESHOLD_DAYS:
            return f"no_reply_{days_since_send}d"
        return "sent"
    if stage in _DRAFTING_STAGES:
        return "drafting"
    return stage or "unknown"


def get_outbox_status(uid: str, limit: int = 10, db=None) -> Dict[str, Any]:
    """The user's recent contact outreach state, summarized for the LLM.

    Reads users/{uid}/contacts and filters to inOutbox == True (the same flag
    the outbox UI keys off). The recent array is ordered by lastActivityAt
    desc and truncated to `limit`. Counts cover every contact in the outbox,
    not just the truncated tail.
    """
    if not uid:
        return dict(_EMPTY_OUTBOX)
    db = db or _db()
    if db is None:
        return dict(_EMPTY_OUTBOX)

    coll = _subcollection(db, uid, "contacts")
    contacts: List[Dict[str, Any]] = []
    for snap in _stream(coll):
        data = snap.to_dict() or {}
        if not data.get("inOutbox"):
            continue
        data["_id"] = snap.id
        contacts.append(data)

    total = len(contacts)
    awaiting = sum(
        1 for c in contacts if (c.get("pipelineStage") in _AWAITING_REPLY_STAGES)
    )
    replied = sum(
        1 for c in contacts if (c.get("pipelineStage") in _REPLIED_STAGES)
    )

    _sort_by_timestamp(contacts, "lastActivityAt", reverse=True)
    now = datetime.now(timezone.utc)

    recent: List[Dict[str, Any]] = []
    for c in contacts[:max(0, int(limit))]:
        stage = c.get("pipelineStage")
        last_sent = c.get("emailSentAt") or c.get("lastActivityAt")
        days = _days_since(last_sent, now)
        first = _truncate(c.get("firstName") or c.get("FirstName") or "", 60)
        last = _truncate(c.get("lastName") or c.get("LastName") or "", 60)
        name = (f"{first} {last}".strip()
                or _truncate(c.get("contactName") or c.get("name"), 120)
                or "(unnamed)")
        recent.append({
            "id": c["_id"],
            "contact_name": name,
            "contact_company": _truncate(c.get("company"), 120),
            "contact_role": _truncate(c.get("jobTitle") or c.get("Title"), 160),
            "last_sent_at": _iso(last_sent),
            "status": _normalize_outbox_status(stage, days),
            "days_since_last_send": days if days is not None else 0,
        })

    return {
        "total_contacts": total,
        "awaiting_reply": awaiting,
        "replied": replied,
        "recent": recent,
    }


# ===========================================================================
# 2. Recent contact searches (natural-language prompt history)
# ===========================================================================

def get_recent_searches(uid: str, limit: int = 5, db=None) -> Dict[str, Any]:
    """The user's recent natural-language contact searches.

    Reads users/{uid}/searchHistory. Each entry carries the original prompt
    and the result count, ordered by createdAt desc. This is the same
    collection the Find page persists prompt-search runs to.
    """
    if not uid:
        return dict(_EMPTY_LIST)
    db = db or _db()
    if db is None:
        return dict(_EMPTY_LIST)

    coll = _subcollection(db, uid, "searchHistory")
    items: List[Dict[str, Any]] = []
    for snap in _stream(coll):
        data = snap.to_dict() or {}
        items.append({"_id": snap.id, **data})
    count = len(items)

    _sort_by_timestamp(items, "createdAt", reverse=True)

    recent: List[Dict[str, Any]] = []
    for it in items[:max(0, int(limit))]:
        prompt = (
            it.get("prompt")
            or it.get("query")
            or it.get("naturalLanguageQuery")
            or ""
        )
        result_count = (
            it.get("resultCount")
            if isinstance(it.get("resultCount"), int)
            else it.get("count")
            if isinstance(it.get("count"), int)
            else it.get("resultsCount")
            if isinstance(it.get("resultsCount"), int)
            else 0
        )
        recent.append({
            "id": it["_id"],
            "query": _truncate(prompt, 240),
            "result_count": int(result_count or 0),
            "searched_at": _iso(it.get("createdAt")),
        })
    return {"count": count, "recent": recent}


# ===========================================================================
# 3. Recent cover letters
# ===========================================================================

def get_recent_cover_letters(uid: str, limit: int = 5, db=None) -> Dict[str, Any]:
    """The user's recent cover letters, metadata only.

    Reads users/{uid}/cover_letter_library. The cover_letter_text body is
    deliberately NOT included; the LLM can ask for it via a separate
    explicit fetch if it ever needs the prose.
    """
    if not uid:
        return dict(_EMPTY_LIST)
    db = db or _db()
    if db is None:
        return dict(_EMPTY_LIST)

    coll = _subcollection(db, uid, "cover_letter_library")
    items: List[Dict[str, Any]] = []
    for snap in _stream(coll):
        data = snap.to_dict() or {}
        items.append({"_id": snap.id, **data})
    count = len(items)

    _sort_by_timestamp(items, "created_at", reverse=True)

    recent: List[Dict[str, Any]] = []
    for it in items[:max(0, int(limit))]:
        body = it.get("cover_letter_text") or ""
        recent.append({
            "id": it["_id"],
            "company": _truncate(it.get("company"), 160),
            "role": _truncate(it.get("job_title"), 160),
            "created_at": _iso(it.get("created_at")),
            "length_chars": len(body) if isinstance(body, str) else 0,
        })
    return {"count": count, "recent": recent}


# ===========================================================================
# 4. Meeting prep drafts (coffee chat, informational interview)
# ===========================================================================

def get_meeting_prep_drafts(uid: str, limit: int = 5, db=None) -> Dict[str, Any]:
    """The user's recent meeting prep drafts.

    Reads users/{uid}/coffee-chat-preps (the persisted collection name is
    coffee-chat-preps for migration reasons; the feature is now called
    Meeting Prep in the UI).
    """
    if not uid:
        return dict(_EMPTY_LIST)
    db = db or _db()
    if db is None:
        return dict(_EMPTY_LIST)

    coll = _subcollection(db, uid, "coffee-chat-preps")
    items: List[Dict[str, Any]] = []
    for snap in _stream(coll):
        data = snap.to_dict() or {}
        items.append({"_id": snap.id, **data})
    count = len(items)

    _sort_by_timestamp(items, "createdAt", reverse=True)

    recent: List[Dict[str, Any]] = []
    for it in items[:max(0, int(limit))]:
        # The background processor fills in contactName / contactCompany /
        # targetName once research completes; fall back to whatever is set.
        name = (
            _truncate(it.get("contactName"), 120)
            or _truncate(it.get("targetName"), 120)
            or _truncate(it.get("name"), 120)
            or _truncate(it.get("meetingTitle"), 160)
            or _truncate(it.get("linkedinUrl"), 240)
            or "(unnamed meeting)"
        )
        # The product only ships coffee chat / informational meetings under
        # this collection today; the field stays explicit so a future split
        # has somewhere to land.
        meeting_type = _truncate(it.get("meetingType") or "coffee_chat", 40)
        recent.append({
            "id": it["_id"],
            "contact_name": name,
            "meeting_type": meeting_type,
            "scheduled_for": _iso(it.get("scheduledFor")),
            "created_at": _iso(it.get("createdAt")),
        })
    return {"count": count, "recent": recent}


# ===========================================================================
# 5. Recent firm searches (structured-search history)
# ===========================================================================

def get_recent_firm_searches(uid: str, limit: int = 5, db=None) -> Dict[str, Any]:
    """The user's recent firm searches.

    Reads users/{uid}/firmSearches. Distinct from searchHistory: that one
    is natural-language contact searches, this one is the structured firm
    search the firm-search feature persists, carrying parsedFilters and a
    resultsCount.
    """
    if not uid:
        return dict(_EMPTY_LIST)
    db = db or _db()
    if db is None:
        return dict(_EMPTY_LIST)

    coll = _subcollection(db, uid, "firmSearches")
    items: List[Dict[str, Any]] = []
    for snap in _stream(coll):
        data = snap.to_dict() or {}
        items.append({"_id": snap.id, **data})
    count = len(items)

    _sort_by_timestamp(items, "createdAt", reverse=True)

    recent: List[Dict[str, Any]] = []
    for it in items[:max(0, int(limit))]:
        result_count = it.get("resultsCount")
        if not isinstance(result_count, int):
            results = it.get("results")
            result_count = len(results) if isinstance(results, list) else 0
        recent.append({
            "id": it["_id"],
            "query": _truncate(it.get("query"), 240),
            "result_count": int(result_count or 0),
            "searched_at": _iso(it.get("createdAt")),
        })
    return {"count": count, "recent": recent}


# ===========================================================================
# 6. Auto-apply applications (the Applications page queues)
# ===========================================================================

# Raw autoApplyJobs statuses bucketed the way the Applications page groups
# them: needs_answers = paused on a screening question, finish_in_browser =
# form filled but blocked by a CAPTCHA the user must clear themselves.
_APP_IN_FLIGHT_STATUSES = frozenset({"queued", "running"})
_APP_FAILED_STATUSES = frozenset({"failed", "submit_failed"})

_EMPTY_APPLICATIONS = {
    "total": 0,
    "submitted": 0,
    "in_flight": 0,
    "needs_answers": 0,
    "finish_in_browser": 0,
    "failed": 0,
    "recent": [],
}


def get_applications_status(uid: str, limit: int = 8, db=None) -> Dict[str, Any]:
    """The user's auto-apply applications, summarized for the LLM.

    Reads users/{uid}/autoApplyJobs (the same collection the Applications
    page's three queues read). Counts cover every application; the recent
    array is ordered by created_at desc and truncated to `limit`.
    """
    if not uid:
        return dict(_EMPTY_APPLICATIONS)
    db = db or _db()
    if db is None:
        return dict(_EMPTY_APPLICATIONS)

    coll = _subcollection(db, uid, "autoApplyJobs")
    items: List[Dict[str, Any]] = []
    for snap in _stream(coll):
        data = snap.to_dict() or {}
        items.append({"_id": snap.id, **data})

    total = len(items)
    statuses = [str(it.get("status") or "") for it in items]
    summary = {
        "total": total,
        "submitted": sum(1 for s in statuses if s == "submitted"),
        "in_flight": sum(1 for s in statuses if s in _APP_IN_FLIGHT_STATUSES),
        "needs_answers": sum(1 for s in statuses if s == "needs_attention"),
        "finish_in_browser": sum(1 for s in statuses if s == "needs_verification"),
        "failed": sum(1 for s in statuses if s in _APP_FAILED_STATUSES),
    }

    _sort_by_timestamp(items, "created_at", reverse=True)

    recent: List[Dict[str, Any]] = []
    for it in items[:max(0, int(limit))]:
        recent.append({
            "id": it["_id"],
            "job_title": _truncate(it.get("job_title") or it.get("job_id"), 160),
            "company": _truncate(it.get("company"), 120),
            "status": _truncate(it.get("status") or "unknown", 40),
            "created_at": _iso(it.get("created_at")),
        })
    summary["recent"] = recent
    return summary


# ===========================================================================
# 7. Loops (recurring outreach agents)
# ===========================================================================

_EMPTY_LOOPS = {
    "count": 0,
    "running": 0,
    "paused": 0,
    "pending_drafts": 0,
    "unread_replies": 0,
    "loops": [],
}


def get_loops_status(uid: str, limit: int = 6, db=None) -> Dict[str, Any]:
    """The user's Loops (recurring outreach agents), summarized for the LLM.

    Reads users/{uid}/loops directly (same collection the Loops fleet page
    lists). Aggregates cover every Loop; the loops array is ordered by
    lastRunAt desc and truncated to `limit`.
    """
    if not uid:
        return dict(_EMPTY_LOOPS)
    db = db or _db()
    if db is None:
        return dict(_EMPTY_LOOPS)

    coll = _subcollection(db, uid, "loops")
    items: List[Dict[str, Any]] = []
    for snap in _stream(coll):
        data = snap.to_dict() or {}
        items.append({"_id": snap.id, **data})

    count = len(items)
    running = sum(1 for it in items if it.get("status") == "running")
    paused = sum(1 for it in items if it.get("status") == "paused")
    pending_drafts = sum(int(it.get("pendingDrafts") or 0) for it in items)
    unread_replies = sum(int(it.get("unreadReplies") or 0) for it in items)

    _sort_by_timestamp(items, "lastRunAt", reverse=True)

    loops: List[Dict[str, Any]] = []
    for it in items[:max(0, int(limit))]:
        name = (
            _truncate(it.get("name"), 120)
            or _truncate(it.get("briefText"), 80)
            or "(unnamed loop)"
        )
        loops.append({
            "id": it["_id"],
            "name": name,
            "status": _truncate(it.get("status") or "unknown", 40),
            "cadence": _truncate(it.get("cadence"), 40),
            "last_run_at": _iso(it.get("lastRunAt")),
            "next_run_at": _iso(it.get("nextRunAt")),
            "pending_drafts": int(it.get("pendingDrafts") or 0),
            "unread_replies": int(it.get("unreadReplies") or 0),
            "emails_drafted": int(it.get("totalEmailsDrafted") or 0),
            "replies_received": int(it.get("totalRepliesReceived") or 0),
            "week_credits_spent": int(it.get("weekCreditsSpent") or 0),
            "credit_budget_per_week": int(it.get("creditBudgetPerWeek") or 0),
            "pause_reason": _truncate(it.get("pauseReason"), 160) or None,
        })

    return {
        "count": count,
        "running": running,
        "paused": paused,
        "pending_drafts": pending_drafts,
        "unread_replies": unread_replies,
        "loops": loops,
    }
