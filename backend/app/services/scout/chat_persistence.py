"""Scout chat persistence (Phase 5, Stage 3).

Persists Scout conversations to Firestore so they survive page reloads, sign
outs, and a tier-appropriate retention window. Before this stage the chat
thread lived only in component state.

Firestore layout:
  users/{uid}/scoutChats/{chat_id}                 - parent doc per conversation
  users/{uid}/scoutChats/{chat_id}/messages/{mid}  - one doc per turn

Parent doc fields:
  chat_id, title, created_at, last_active_at, message_count,
  active_strategy_id (nullable), tier_when_created, expires_at.

Message doc fields:
  message_id, role ("user" | "assistant"), content, tool_calls (or None),
  tool_results (or None), created_at, metrics (or None), expires_at.

Retention (TTL):
  Free  -> 7 days after creation.
  Pro   -> 60 days.
  Elite -> 90 days (Rylan 2026-09-16: first real use of the tier being kept
           separate from Pro, since Scout's actual memory ceiling was found
           to be this retention window, not model capability).

Why expires_at on the message doc too: Firestore TTL policies do not cascade
from a parent to a subcollection. Writing expires_at on every message lets a
collection-group TTL prune the message docs at the same horizon as the parent.

ONE-TIME SETUP (Firestore TTL policies are project config, not code):
  Firebase console -> Firestore Database -> TTL -> add two policies:
    1. collection group `scoutChats`, timestamp field `expires_at`
    2. collection group `messages`, timestamp field `expires_at`
  Or via gcloud:
    gcloud firestore fields ttls update expires_at \\
      --collection-group=scoutChats --enable-ttl
    gcloud firestore fields ttls update expires_at \\
      --collection-group=messages --enable-ttl
  The `messages` group covers other features too; the TTL only fires on docs
  that carry expires_at, so this is safe.

Best-effort Firestore: when the database is unavailable a read returns an
empty shape and a write returns an {"ok": False} envelope. Nothing here
raises. List functions return [] when empty (never an error).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# Retention per tier, in days.
CHAT_TTL_DAYS = {"free": 7, "pro": 60, "elite": 90}

# Default cap when reading messages back. The LLM-context windowing in the
# service layer further trims this; the cap here just protects the read.
DEFAULT_MESSAGE_LIMIT = 200

# Default cap when listing chats for the sidebar.
DEFAULT_CHAT_LIST_LIMIT = 20

# Title generation: keep it short so the sidebar renders cleanly.
MAX_TITLE_LEN = 60
# Messages too trivial to summarize. The list is conservative; everything else
# attempts an LLM summary, with a truncation fallback when the call fails.
_TRIVIAL_MESSAGES = frozenset({
    "hi", "hey", "hello", "yo", "sup", "test", "testing", "ping", "?", "ok",
})

_COLLECTION = "scoutChats"
_SUBCOLLECTION = "messages"
# em dash, kept out of source as a literal so house style stays clean here.
_EM_DASH = chr(0x2014)


# ===========================================================================
# Pure helpers (no I/O, fully testable)
# ===========================================================================

def normalize_tier(tier: Optional[str]) -> str:
    """Map any tier value to one of 'free' / 'pro' / 'elite'.

    Mirrors the strategy module: anything unrecognized is treated as free, so a
    misconfigured user does not accidentally get the longest retention.
    """
    t = (tier or "").strip().lower()
    return t if t in CHAT_TTL_DAYS else "free"


def chat_ttl_days(tier: Optional[str]) -> int:
    """How many days a chat is kept for this tier."""
    return CHAT_TTL_DAYS[normalize_tier(tier)]


def compute_chat_expiry(tier: Optional[str], created_at: datetime) -> datetime:
    """When a chat (and its messages) should expire for this tier."""
    return created_at + timedelta(days=chat_ttl_days(tier))


def _strip_em_dashes(text: str) -> str:
    """Replace em dashes with a spaced hyphen. House style bans U+2014."""
    if not text or _EM_DASH not in text:
        return text
    cleaned = text.replace(" " + _EM_DASH + " ", " - ").replace(_EM_DASH, " - ")
    while "  " in cleaned:
        cleaned = cleaned.replace("  ", " ")
    return cleaned


def _as_aware(dt: Any) -> Optional[datetime]:
    """Coerce a stored timestamp to a timezone-aware UTC datetime, or None."""
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _truncate_for_title(text: str) -> str:
    """Fallback title when the LLM call is skipped or fails.

    Trims and caps to MAX_TITLE_LEN total (the ellipsis counts toward the
    cap), strips em dashes, and degrades to "New chat" when nothing usable
    remains.
    """
    cleaned = _strip_em_dashes(str(text or "").strip())
    if not cleaned:
        return "New chat"
    if len(cleaned) <= MAX_TITLE_LEN:
        return cleaned
    return cleaned[: MAX_TITLE_LEN - 3].rstrip() + "..."


def _is_trivial_first_message(message: str) -> bool:
    """True when the first message is too small to summarize."""
    t = (message or "").strip().lower()
    if not t:
        return True
    if t in _TRIVIAL_MESSAGES:
        return True
    return len(t) < 4


def generate_title_for_first_message(
    message: str,
    *,
    llm_client: Any = None,
    model: str = "gpt-4.1-mini",
) -> str:
    """Summarize the first user message into a sidebar title (under 60 chars).

    The LLM client is injected so callers control sync vs async and so tests
    can stub it. When it is None, the call fails, or the message is trivial,
    the function falls back to a truncated form of the message itself.
    """
    if _is_trivial_first_message(message):
        return _truncate_for_title(message) if message.strip() else "New chat"

    if llm_client is None:
        return _truncate_for_title(message)

    prompt = (
        "Summarize the following user message into a short chat title (under "
        "60 characters, no quotes, no trailing period, no em dashes). The "
        "title should name the goal or topic, not paraphrase the message. "
        f"Message: {message.strip()[:400]}"
    )
    try:
        response = llm_client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You write short, concrete chat titles."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=30,
        )
        raw = (response.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"[ScoutChatPersistence] title LLM call failed: {e}")
        return _truncate_for_title(message)

    cleaned = _strip_em_dashes(raw.strip().strip('"').strip("'").rstrip("."))
    if not cleaned:
        return _truncate_for_title(message)
    if len(cleaned) > MAX_TITLE_LEN:
        cleaned = cleaned[: MAX_TITLE_LEN - 3].rstrip() + "..."
    return cleaned


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


def _chats_coll(db, uid: str):
    """The users/{uid}/scoutChats collection reference."""
    return db.collection("users").document(uid).collection(_COLLECTION)


def _messages_coll(db, uid: str, chat_id: str):
    """The users/{uid}/scoutChats/{chat_id}/messages subcollection ref."""
    return _chats_coll(db, uid).document(chat_id).collection(_SUBCOLLECTION)


def _stream_all(coll) -> List[Any]:
    """Every snapshot in the collection. Best-effort: errors degrade to []."""
    try:
        return [d for d in coll.stream() if d is not None]
    except Exception as e:
        print(f"[ScoutChatPersistence] stream failed: {e}")
        return []


def _serialize_parent(snap_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """JSON-safe view of a chat parent doc.

    Datetimes are returned as ISO-8601 strings so the dict is safe to ship
    over the wire without an extra serialization step in the route layer.
    """
    return {
        "chat_id": snap_id,
        "title": data.get("title") or "New chat",
        "created_at": _iso(data.get("created_at")),
        "last_active_at": _iso(data.get("last_active_at")),
        "message_count": int(data.get("message_count") or 0),
        "active_strategy_id": data.get("active_strategy_id"),
        "tier_when_created": data.get("tier_when_created") or "free",
        "expires_at": _iso(data.get("expires_at")),
    }


def _serialize_message(snap_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """JSON-safe view of a message doc."""
    return {
        "message_id": snap_id,
        "role": data.get("role") or "user",
        "content": data.get("content") or "",
        "tool_calls": data.get("tool_calls"),
        "tool_results": data.get("tool_results"),
        "created_at": _iso(data.get("created_at")),
        "metrics": data.get("metrics"),
    }


def _iso(value: Any) -> Optional[str]:
    """ISO-8601 for a stored timestamp, or None when unparseable / missing."""
    dt = _as_aware(value)
    return dt.isoformat() if dt is not None else None


# ===========================================================================
# 1. create_chat
# ===========================================================================

def create_chat(
    uid: str,
    tier: Optional[str],
    active_strategy_id: Optional[str] = None,
    db: Any = None,
) -> Dict[str, Any]:
    """Create a new chat parent doc.

    Returns the JSON-safe serialized parent, or an error envelope when the
    write cannot land. Errors do not raise; the caller may proceed without a
    persisted chat (the conversation just will not survive a reload).
    """
    if not uid:
        return {"ok": False, "error": "not_signed_in"}
    db = db or _db()
    if db is None:
        return {"ok": False, "error": "unavailable"}

    tier_norm = normalize_tier(tier)
    now = datetime.now(timezone.utc)
    chat_id = uuid.uuid4().hex
    doc = {
        "chat_id": chat_id,
        "title": "New chat",
        "created_at": now,
        "last_active_at": now,
        "message_count": 0,
        "active_strategy_id": active_strategy_id or None,
        "tier_when_created": tier_norm,
        "expires_at": compute_chat_expiry(tier_norm, now),
    }
    try:
        _chats_coll(db, uid).document(chat_id).set(doc)
    except Exception as e:
        print(f"[ScoutChatPersistence] create_chat failed: {e}")
        return {"ok": False, "error": "save_failed"}

    return {"ok": True, **_serialize_parent(chat_id, doc)}


# ===========================================================================
# 2. append_message
# ===========================================================================

def append_message(
    uid: str,
    chat_id: str,
    role: str,
    content: str,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    tool_results: Optional[List[Dict[str, Any]]] = None,
    metrics: Optional[Dict[str, Any]] = None,
    db: Any = None,
) -> Dict[str, Any]:
    """Append a message to a chat's subcollection.

    Bumps message_count and last_active_at on the parent. The message's
    expires_at mirrors the parent's so a collection-group TTL can prune the
    subcollection at the same horizon as the parent.
    """
    if not uid:
        return {"ok": False, "error": "not_signed_in"}
    if not chat_id:
        return {"ok": False, "error": "missing_chat_id"}
    if role not in ("user", "assistant"):
        return {"ok": False, "error": "invalid_role"}
    db = db or _db()
    if db is None:
        return {"ok": False, "error": "unavailable"}

    parent_ref = _chats_coll(db, uid).document(chat_id)
    try:
        parent_snap = parent_ref.get()
    except Exception as e:
        print(f"[ScoutChatPersistence] append_message parent read failed: {e}")
        return {"ok": False, "error": "read_failed"}

    parent_data = parent_snap.to_dict() if parent_snap is not None else None
    if not parent_data:
        return {"ok": False, "error": "chat_not_found"}

    now = datetime.now(timezone.utc)
    message_id = uuid.uuid4().hex
    expires_at = _as_aware(parent_data.get("expires_at")) or compute_chat_expiry(
        parent_data.get("tier_when_created"), now
    )

    message_doc = {
        "message_id": message_id,
        "role": role,
        "content": _strip_em_dashes(str(content or "")),
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "created_at": now,
        "metrics": metrics,
        "expires_at": expires_at,
    }
    try:
        _messages_coll(db, uid, chat_id).document(message_id).set(message_doc)
    except Exception as e:
        print(f"[ScoutChatPersistence] append_message write failed: {e}")
        return {"ok": False, "error": "save_failed"}

    new_count = int(parent_data.get("message_count") or 0) + 1
    parent_data["message_count"] = new_count
    parent_data["last_active_at"] = now
    try:
        parent_ref.set(parent_data)
    except Exception as e:
        # The message landed; failing to bump the parent is non-fatal. The
        # message_count will catch up on the next successful append.
        print(f"[ScoutChatPersistence] append_message parent bump failed: {e}")

    return {
        "ok": True,
        "message_id": message_id,
        "message_count": new_count,
        "created_at": _iso(now),
        "expires_at": _iso(expires_at),
    }


# ===========================================================================
# 3. get_chat
# ===========================================================================

def get_chat(
    uid: str,
    chat_id: str,
    message_limit: int = DEFAULT_MESSAGE_LIMIT,
    db: Any = None,
) -> Dict[str, Any]:
    """Return the parent doc plus the ordered message list (oldest first).

    `message_limit` caps the number of messages returned to keep Scout's
    context from blowing up on very long chats. The full transcript stays
    in Firestore; only the read is windowed. When the limit is hit, the
    OLDEST messages are dropped so the recent context survives.
    """
    if not uid:
        return {"ok": False, "error": "not_signed_in", "messages": []}
    if not chat_id:
        return {"ok": False, "error": "missing_chat_id", "messages": []}
    db = db or _db()
    if db is None:
        return {"ok": False, "error": "unavailable", "messages": []}

    try:
        parent_snap = _chats_coll(db, uid).document(chat_id).get()
    except Exception as e:
        print(f"[ScoutChatPersistence] get_chat parent read failed: {e}")
        return {"ok": False, "error": "read_failed", "messages": []}

    parent_data = parent_snap.to_dict() if parent_snap is not None else None
    if not parent_data:
        return {"ok": False, "error": "chat_not_found", "messages": []}

    raw_messages: List[Dict[str, Any]] = []
    for snap in _stream_all(_messages_coll(db, uid, chat_id)):
        data = snap.to_dict() or {}
        raw_messages.append({"_snap_id": snap.id, **data})
    # Sort oldest -> newest by created_at; missing timestamps sink to the bottom
    # of "old" so they at least surface in the trimmed window.
    _floor = datetime.min.replace(tzinfo=timezone.utc)
    raw_messages.sort(key=lambda d: _as_aware(d.get("created_at")) or _floor)

    cap = max(0, int(message_limit or 0))
    if cap and len(raw_messages) > cap:
        raw_messages = raw_messages[-cap:]

    messages = [_serialize_message(m["_snap_id"], m) for m in raw_messages]
    return {
        "ok": True,
        "chat": _serialize_parent(chat_id, parent_data),
        "messages": messages,
    }


# ===========================================================================
# 4. list_chats
# ===========================================================================

def list_chats(
    uid: str,
    tier: Optional[str],
    limit: int = DEFAULT_CHAT_LIST_LIMIT,
    db: Any = None,
) -> List[Dict[str, Any]]:
    """Recent chats for the sidebar, newest first. Parent docs only, no messages.

    Free tier: returns at most one chat (the most recently active). The
    sidebar UI then shows the current chat only and surfaces the upgrade
    affordance. Pro and Elite get up to `limit` chats.
    """
    if not uid:
        return []
    db = db or _db()
    if db is None:
        return []

    snaps = _stream_all(_chats_coll(db, uid))
    if not snaps:
        return []

    rows: List[Dict[str, Any]] = []
    for snap in snaps:
        data = snap.to_dict() or {}
        rows.append({"_snap_id": snap.id, **data})

    _floor = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(
        key=lambda d: _as_aware(d.get("last_active_at")) or _floor,
        reverse=True,
    )

    tier_norm = normalize_tier(tier)
    cap = 1 if tier_norm == "free" else max(0, int(limit or 0))
    if cap and len(rows) > cap:
        rows = rows[:cap]

    return [_serialize_parent(r["_snap_id"], r) for r in rows]


# ===========================================================================
# 5. update_chat_title
# ===========================================================================

def update_chat_title(
    uid: str,
    chat_id: str,
    title: str,
    db: Any = None,
) -> Dict[str, Any]:
    """Set the chat's title. Used by both the auto-title write and any manual rename."""
    if not uid:
        return {"ok": False, "error": "not_signed_in"}
    if not chat_id:
        return {"ok": False, "error": "missing_chat_id"}
    db = db or _db()
    if db is None:
        return {"ok": False, "error": "unavailable"}

    cleaned = _strip_em_dashes(str(title or "").strip())
    if not cleaned:
        return {"ok": False, "error": "empty_title"}
    if len(cleaned) > MAX_TITLE_LEN:
        cleaned = cleaned[: MAX_TITLE_LEN - 3].rstrip() + "..."

    parent_ref = _chats_coll(db, uid).document(chat_id)
    try:
        parent_snap = parent_ref.get()
    except Exception as e:
        print(f"[ScoutChatPersistence] update_chat_title read failed: {e}")
        return {"ok": False, "error": "read_failed"}
    parent_data = parent_snap.to_dict() if parent_snap is not None else None
    if not parent_data:
        return {"ok": False, "error": "chat_not_found"}

    parent_data["title"] = cleaned
    try:
        parent_ref.set(parent_data)
    except Exception as e:
        print(f"[ScoutChatPersistence] update_chat_title write failed: {e}")
        return {"ok": False, "error": "save_failed"}

    return {"ok": True, "chat_id": chat_id, "title": cleaned}


# ===========================================================================
# 6. set_active_strategy
# ===========================================================================

def set_active_strategy(
    uid: str,
    chat_id: str,
    strategy_id: Optional[str],
    db: Any = None,
) -> Dict[str, Any]:
    """Stamp the active strategy id on a chat.

    Pass `strategy_id=None` to clear it (the strategy was closed or the user
    has no active plan). Used when a chat creates or switches a strategy so
    the sidebar can flag which chats had a plan in flight.
    """
    if not uid:
        return {"ok": False, "error": "not_signed_in"}
    if not chat_id:
        return {"ok": False, "error": "missing_chat_id"}
    db = db or _db()
    if db is None:
        return {"ok": False, "error": "unavailable"}

    parent_ref = _chats_coll(db, uid).document(chat_id)
    try:
        parent_snap = parent_ref.get()
    except Exception as e:
        print(f"[ScoutChatPersistence] set_active_strategy read failed: {e}")
        return {"ok": False, "error": "read_failed"}
    parent_data = parent_snap.to_dict() if parent_snap is not None else None
    if not parent_data:
        return {"ok": False, "error": "chat_not_found"}

    parent_data["active_strategy_id"] = strategy_id or None
    try:
        parent_ref.set(parent_data)
    except Exception as e:
        print(f"[ScoutChatPersistence] set_active_strategy write failed: {e}")
        return {"ok": False, "error": "save_failed"}

    return {"ok": True, "chat_id": chat_id, "active_strategy_id": strategy_id or None}
