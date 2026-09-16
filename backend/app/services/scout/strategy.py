"""Scout strategy memory (Phase 5, Stage 1).

A strategy is the user's single active multi-step recruiting plan: the
throughline that outlives one chat and one page. Scout builds it when a user
shows up with a fuzzy goal, breaks it into ordered steps, and sequences
Offerloop workflows to execute it. There is exactly one active strategy per
user at a time.

Firestore layout: users/{uid}/scoutStrategies/{id}. Each doc is one strategy.
Exactly one doc carries status == "active"; the rest are status == "archived".

Replacing or closing a strategy archives the old one for 90 days, every
tier (Rylan 2026-09-16: memory is not a paywalled feature, this used to be
Free-deletes/Pro-and-Elite-archive; not any more). The active strategy
itself already persisted across sessions for every tier.

Archived docs carry an `expires_at` datetime. This module prunes expired
archives on every write (so the archive a user sees is never stale), and the
field also supports a Firestore TTL policy as a backstop:

ONE-TIME SETUP (Firestore TTL policies are project config, not code):
  Firebase console -> Firestore Database -> TTL -> add a policy on collection
  group `scoutStrategies` with timestamp field `expires_at`. Or via gcloud:
    gcloud firestore fields ttls update expires_at \\
      --collection-group=scoutStrategies --enable-ttl

All Firestore access is best-effort: when the database is unavailable a read
returns None and a write returns an {"ok": False} envelope. Nothing here
raises.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.services.scout.page_registry import get_page, is_valid_route

# --- limits -----------------------------------------------------------------
MAX_STEPS = 10
MAX_GOAL_LEN = 300
MAX_STEP_TITLE_LEN = 200
MAX_STEP_DETAIL_LEN = 600

# Archive retention in days. Same for every tier on purpose (Rylan
# 2026-09-16: not a paywalled feature). Kept as a per-tier dict, not a flat
# constant, so a future tier-specific need doesn't require a schema change.
ARCHIVE_RETENTION_DAYS = {"free": 90, "pro": 90, "elite": 90}

# A strategy with an unfinished step and no update in this many days is stalled.
STALL_DAYS = 7

_COLLECTION = "scoutStrategies"
# em dash, kept out of the source as a literal so house style stays clean here.
_EM_DASH = chr(0x2014)


# ===========================================================================
# Pure helpers (no I/O, fully testable)
# ===========================================================================

def normalize_tier(tier: Optional[str]) -> str:
    """Map any tier value to one of 'free' / 'pro' / 'elite'.

    Firestore stores `subscriptionTier` lowercase, but callers also pass
    display-cased or unknown values; anything unrecognized is treated as free.
    """
    t = (tier or "").strip().lower()
    return t if t in ARCHIVE_RETENTION_DAYS else "free"


def archive_retention_days(tier: Optional[str]) -> int:
    """Days an archived strategy is kept for this tier (0 means no archive)."""
    return ARCHIVE_RETENTION_DAYS[normalize_tier(tier)]


def keeps_archive(tier: Optional[str]) -> bool:
    """True when this tier keeps a strategy archive (every tier now)."""
    return archive_retention_days(tier) > 0


def compute_expiry(tier: Optional[str], archived_at: datetime) -> Optional[datetime]:
    """When an archived strategy should expire, or None when it is not kept."""
    days = archive_retention_days(tier)
    if days <= 0:
        return None
    return archived_at + timedelta(days=days)


def _strip_em_dashes(text: str) -> str:
    """Replace em dashes with a spaced hyphen. House style bans U+2014."""
    if not text or _EM_DASH not in text:
        return text
    cleaned = text.replace(" " + _EM_DASH + " ", " - ").replace(_EM_DASH, " - ")
    while "  " in cleaned:
        cleaned = cleaned.replace("  ", " ")
    return cleaned


def _trim(value: Any, limit: int) -> str:
    """Coerce to a trimmed, em-dash-free, length-capped string."""
    text = _strip_em_dashes(str(value or "").strip())
    return text[:limit].strip()


def clean_goal(goal: Any) -> str:
    """Normalize a strategy goal string, or '' when there is nothing usable."""
    return _trim(goal, MAX_GOAL_LEN)


def clean_steps(raw_steps: Any) -> List[Dict[str, Any]]:
    """Normalize the model-supplied steps into stored step dicts.

    Each stored step is {title, detail, done, route?, rationale?, feature?,
    prefill_payload?, completed_at?, created_artifact_id?}. Steps with no
    title are dropped, an unknown route is dropped (the step is kept), and
    the list is capped at MAX_STEPS. Every step from this function starts
    not done.

    The five extra fields (rationale, feature, prefill_payload, completed_at,
    created_artifact_id) are additive per the D2 schema audit. They support
    the E2 recommendation-memory narrative ("you completed the Loop targeting
    Stripe on Tuesday") without breaking existing strategies that were saved
    before this migration shipped: they all default to empty/None.
    """
    steps: List[Dict[str, Any]] = []
    if not isinstance(raw_steps, list):
        return steps
    for item in raw_steps:
        if not isinstance(item, dict):
            continue
        title = _trim(item.get("title"), MAX_STEP_TITLE_LEN)
        if not title:
            continue
        step: Dict[str, Any] = {
            "title": title,
            "detail": _trim(item.get("detail"), MAX_STEP_DETAIL_LEN),
            "done": False,
            # E2 fields. All optional. completed_at is set in
            # update_strategy_progress when a step transitions to done.
            "rationale": _trim(item.get("rationale"), MAX_STEP_DETAIL_LEN),
            "feature": _trim(item.get("feature"), 40),
            "prefill_payload": (
                item.get("prefill_payload")
                if isinstance(item.get("prefill_payload"), dict)
                else {}
            ),
            "completed_at": None,
            "created_artifact_id": _trim(item.get("created_artifact_id"), 64),
        }
        route = str(item.get("route") or "").strip()
        if route and is_valid_route(route):
            # Store the canonical route string from the registry.
            step["route"] = get_page(route)["route"]
        steps.append(step)
        if len(steps) >= MAX_STEPS:
            break
    return steps


def _as_aware(dt: Any) -> Optional[datetime]:
    """Coerce a stored timestamp to a timezone-aware UTC datetime, or None."""
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def days_since(dt: Any, now: Optional[datetime] = None) -> Optional[int]:
    """Whole days between `dt` and now, or None when `dt` is not a datetime."""
    aware = _as_aware(dt)
    if aware is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0, (now - aware).days)


def is_stalled(strategy: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> bool:
    """True when an active strategy has an open step and has not moved lately."""
    if not strategy:
        return False
    steps = strategy.get("steps") or []
    if not any(not s.get("done") for s in steps if isinstance(s, dict)):
        return False  # nothing left to do is not stalled, it is finished
    elapsed = days_since(strategy.get("updated_at"), now)
    return elapsed is not None and elapsed >= STALL_DAYS


def render_active_strategy_block(
    strategy: Optional[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> str:
    """Render the active strategy as a system-prompt block.

    Returns '' when there is no active strategy. The block rides in the live
    (per-turn) context so it is always current even after Scout edits it.
    """
    if not strategy:
        return ""
    goal = clean_goal(strategy.get("goal"))
    if not goal:
        return ""
    steps = [s for s in (strategy.get("steps") or []) if isinstance(s, dict)]
    done = sum(1 for s in steps if s.get("done"))
    total = len(steps)

    lines = [
        "[ACTIVE STRATEGY - the user's one current multi-step plan]",
        f"Goal: {goal}",
    ]
    progress = f"Progress: {done} of {total} steps done."
    if is_stalled(strategy, now):
        elapsed = days_since(strategy.get("updated_at"), now)
        progress += f" This plan has not moved in {elapsed} days; it has stalled."
    lines.append(progress)

    if steps:
        lines.append("Steps:")
        next_marked = False
        for i, step in enumerate(steps, start=1):
            if step.get("done"):
                marker = "done"
            elif not next_marked:
                marker = "next"
                next_marked = True
            else:
                marker = " "
            row = f"  {i}. [{marker}] {step.get('title', '')}".rstrip()
            detail = str(step.get("detail") or "").strip()
            if detail:
                row += f" ({detail})"
            route = step.get("route")
            if route:
                row += f" [page: {route}]"
            lines.append(row)

    lines.append(
        "Keep this plan moving. Do not start a different strategy without the "
        "user's go-ahead."
    )
    return "\n".join(lines)


# ===========================================================================
# Firestore access (best-effort)
# ===========================================================================

def _db():
    """The Firestore client, or None when it is unavailable."""
    try:
        from app.extensions import get_db
        return get_db()
    except Exception:
        return None


def _strategies_coll(db, uid: str):
    """The users/{uid}/scoutStrategies collection reference."""
    return db.collection("users").document(uid).collection(_COLLECTION)


def _stream_all(coll) -> List[Any]:
    """Every strategy snapshot in the collection. The per-user collection is
    tiny (one active doc plus a tier-bounded archive), so streaming all of it
    and filtering in Python avoids needing any Firestore composite index."""
    try:
        return [d for d in coll.stream() if d is not None]
    except Exception as e:
        print(f"[ScoutStrategy] stream failed: {e}")
        return []


def _prune_expired_archives(coll, now: datetime) -> None:
    """Delete archived strategies whose expiry has passed. Best-effort."""
    for snap in _stream_all(coll):
        data = snap.to_dict() or {}
        if data.get("status") != "archived":
            continue
        expiry = _as_aware(data.get("expires_at"))
        if expiry is not None and expiry <= now:
            try:
                coll.document(snap.id).delete()
            except Exception as e:
                print(f"[ScoutStrategy] prune delete failed: {e}")


def _retire(coll, snap_id: str, data: Dict[str, Any], tier: str,
            outcome: str, now: datetime) -> bool:
    """Move a strategy out of the active slot.

    Pro and Elite keep it as a dated archive entry; Free deletes it outright.
    Returns True when the strategy was kept in the archive.
    """
    if not keeps_archive(tier):
        try:
            coll.document(snap_id).delete()
        except Exception as e:
            print(f"[ScoutStrategy] retire delete failed: {e}")
        return False
    archived = dict(data)
    archived["status"] = "archived"
    archived["outcome"] = outcome
    archived["archived_at"] = now
    archived["expires_at"] = compute_expiry(tier, now)
    try:
        coll.document(snap_id).set(archived)
        return True
    except Exception as e:
        print(f"[ScoutStrategy] retire archive failed: {e}")
        return False


def get_active_strategy(uid: str, db=None) -> Optional[Dict[str, Any]]:
    """The user's active strategy as a dict (with an `id` field), or None.

    The returned dict carries datetime objects and is meant for prompt
    rendering, not JSON serialization.
    """
    if not uid:
        return None
    db = db or _db()
    if db is None:
        return None
    coll = _strategies_coll(db, uid)
    active: List[Dict[str, Any]] = []
    for snap in _stream_all(coll):
        data = snap.to_dict() or {}
        if data.get("status") == "active":
            data["id"] = snap.id
            active.append(data)
    if not active:
        return None
    # One active strategy is the invariant; if somehow there are more, the
    # most recently created one wins.
    active.sort(key=lambda d: _as_aware(d.get("created_at")) or datetime.min.replace(
        tzinfo=timezone.utc), reverse=True)
    return active[0]


def save_strategy(uid: str, tier: Optional[str], goal: Any, steps: Any,
                  db=None) -> Dict[str, Any]:
    """Create the active strategy, replacing any existing one.

    Replacing follows the tier rule: Free drops the old plan, Pro and Elite
    archive it. Returns a JSON-safe result envelope (no datetimes).
    """
    if not uid:
        return {"ok": False, "error": "not_signed_in"}
    tier_norm = normalize_tier(tier)
    clean = clean_goal(goal)
    if not clean:
        return {"ok": False, "error": "empty_goal"}
    cleaned_steps = clean_steps(steps)
    if not cleaned_steps:
        return {"ok": False, "error": "no_steps"}

    db = db or _db()
    if db is None:
        return {"ok": False, "error": "unavailable"}
    coll = _strategies_coll(db, uid)
    now = datetime.now(timezone.utc)
    _prune_expired_archives(coll, now)

    # Retire the existing active strategy, if any.
    replaced = False
    previous_kept = False
    for snap in _stream_all(coll):
        data = snap.to_dict() or {}
        if data.get("status") == "active":
            replaced = True
            previous_kept = _retire(coll, snap.id, data, tier_norm,
                                    outcome="switched", now=now)

    new_id = uuid.uuid4().hex
    doc = {
        "id": new_id,
        "status": "active",
        "goal": clean,
        "steps": cleaned_steps,
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
        "expires_at": None,
        "outcome": None,
        "tier_at_creation": tier_norm,
    }
    try:
        coll.document(new_id).set(doc)
    except Exception as e:
        print(f"[ScoutStrategy] save failed: {e}")
        return {"ok": False, "error": "save_failed"}

    return {
        "ok": True,
        "goal": clean,
        "step_count": len(cleaned_steps),
        "replaced_previous": replaced,
        "previous_kept_in_archive": previous_kept,
        "tier": tier_norm,
    }


def update_strategy_progress(uid: str, tier: Optional[str],
                             completed_steps: Any = None,
                             close: Optional[str] = None,
                             db=None) -> Dict[str, Any]:
    """Mark steps done on the active strategy, and optionally close it out.

    completed_steps is a list of 1-based step numbers. close is "completed" or
    "abandoned" to retire the whole strategy. Returns a JSON-safe envelope.
    """
    if not uid:
        return {"ok": False, "error": "not_signed_in"}
    tier_norm = normalize_tier(tier)
    db = db or _db()
    if db is None:
        return {"ok": False, "error": "unavailable"}
    coll = _strategies_coll(db, uid)

    strategy = get_active_strategy(uid, db=db)
    if not strategy:
        return {"ok": False, "error": "no_active_strategy"}

    steps = [s for s in (strategy.get("steps") or []) if isinstance(s, dict)]
    wanted = set()
    if isinstance(completed_steps, list):
        for n in completed_steps:
            try:
                wanted.add(int(n))
            except (TypeError, ValueError):
                continue
    now = datetime.now(timezone.utc)
    for i, step in enumerate(steps, start=1):
        if i in wanted and not step.get("done"):
            step["done"] = True
            # E2: timestamp the transition so the next briefing can say
            # "you completed step X on Tuesday" instead of "you completed X".
            # Only set on the done-edge; re-marking an already-done step
            # leaves the original timestamp alone.
            step["completed_at"] = now
    all_done = bool(steps) and all(s.get("done") for s in steps)
    sid = strategy["id"]

    close = (close or "").strip().lower() or None
    if close in ("completed", "abandoned"):
        data = dict(strategy)
        data.pop("id", None)
        data["steps"] = steps
        data["updated_at"] = now
        kept = _retire(coll, sid, data, tier_norm, outcome=close, now=now)
        return {
            "ok": True,
            "done_steps": sum(1 for s in steps if s.get("done")),
            "total_steps": len(steps),
            "all_done": all_done,
            "closed": close,
            "kept_in_archive": kept,
        }

    # A normal progress update: rewrite the active doc.
    data = dict(strategy)
    data.pop("id", None)
    data["steps"] = steps
    data["updated_at"] = now
    try:
        coll.document(sid).set(data)
    except Exception as e:
        print(f"[ScoutStrategy] progress update failed: {e}")
        return {"ok": False, "error": "save_failed"}

    return {
        "ok": True,
        "done_steps": sum(1 for s in steps if s.get("done")),
        "total_steps": len(steps),
        "all_done": all_done,
        "closed": None,
    }


def list_archived_strategies(uid: str, db=None) -> List[Dict[str, Any]]:
    """Archived strategies for this user, newest first, expired ones pruned.

    Used by the Pro/Elite strategy archive (the sidebar UI lands in Stage 3).
    """
    if not uid:
        return []
    db = db or _db()
    if db is None:
        return []
    coll = _strategies_coll(db, uid)
    now = datetime.now(timezone.utc)
    _prune_expired_archives(coll, now)
    archived: List[Dict[str, Any]] = []
    for snap in _stream_all(coll):
        data = snap.to_dict() or {}
        if data.get("status") == "archived":
            data["id"] = snap.id
            archived.append(data)
    archived.sort(
        key=lambda d: _as_aware(d.get("archived_at"))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return archived
