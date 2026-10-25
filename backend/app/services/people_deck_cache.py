"""Saved People deck: rank once in the background, serve instantly (OFF-41).

The deck used to rank from scratch on every open, so a student landing on
Search waited on a full rank (and a dry profile waited on a vendor dig).
The job feed solved the same problem with jobFeedCache; this is the people
version:

  * build_deck() ranks up to CACHE_SIZE people and saves them at
    users/{uid}/rankerDeck/current. It runs in the background, kicked off
    right after the resume is parsed (onboarding) and whenever the deck
    goes stale.
  * serve() answers the general deck from that list in one read. People
    the student acted on (swiped, drafted) drop out; people shown but not
    acted on sink a little each time, so each new visit leads with people
    they haven't seen. "Load more" (?exclude=) walks further down the list.
  * The list rebuilds itself, while the old one keeps serving, when it is
    a day old, when the profile that built it changed, when a saved job
    changes the targets, or when the student's ranker arm changes.

Only the general deck is saved. Company / role / focus decks stay live:
they are narrow, cheap, and may need a dig.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

CACHE_SIZE = 200                 # people saved per student
FRESH_SECONDS = 24 * 3600        # rebuild a day-old list
SEEN_PENALTY = 8                 # rank points lost per showing without action
MAX_DECK_BYTES = 800_000         # headroom under Firestore's 1 MiB doc cap
DECK_SUBCOLLECTION = "rankerDeck"
DECK_DOC_ID = "current"

_build_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="people-deck")
_building: set[str] = set()
_building_lock = threading.Lock()


def _ref(db, uid: str):
    return db.collection("users").document(uid).collection(DECK_SUBCOLLECTION).document(DECK_DOC_ID)


def profile_fingerprint(profile: dict) -> str:
    """Hash of the profile fields the ranker reads. A change means the saved
    list was built for a different student and should be rebuilt."""
    profile = profile or {}
    goals = profile.get("goals") or {}
    academics = profile.get("academics") or {}
    keys = {
        "dream": goals.get("dreamCompanies") or profile.get("dreamCompanies"),
        "track": goals.get("careerTrack") or profile.get("careerTrack"),
        "roles": [profile.get(k) or goals.get(k) for k in
                  ("targetRoles", "preferredJobRole", "preferredJobRolesOrTitles")],
        "school": [academics.get("university"), academics.get("school"),
                   profile.get("university"), profile.get("school")],
        "grad": academics.get("graduationYear") or profile.get("graduationYear"),
        "major": academics.get("major") or profile.get("major"),
        "resume": str(profile.get("resumeUpdatedAt") or profile.get("resumeParseVersion") or ""),
    }
    blob = json.dumps(keys, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


# ── Build ─────────────────────────────────────────────────────────────


def build_deck(uid: str, db) -> int:
    """Rank and save the student's deck. Returns how many people were saved."""
    from app.services.person_ranker import model_version_for, rank_people_for_user

    try:
        snap = db.collection("users").document(uid).get()
        profile = (snap.to_dict() or {}) if getattr(snap, "exists", False) else {}
    except Exception:
        profile = {}

    features: dict = {}
    people = rank_people_for_user(
        uid=uid, db=db, limit=CACHE_SIZE, features_out=features, max_limit=CACHE_SIZE,
    )
    items = []
    for p in people:
        pid = ((p.get("person") or {}).get("id") or "").strip()
        if not pid:
            continue
        f = features.get(pid) or {}
        items.append({
            "c":  p,
            "f":  f,
            "rs": f.get("rank_score", p.get("score") or 0),
        })
    # Firestore caps a document at 1 MiB. Cards carry briefings and reasons,
    # so 200 can run over; drop from the bottom until the list fits.
    while items and len(json.dumps(items, default=str)) > MAX_DECK_BYTES:
        items = items[: max(1, int(len(items) * 0.85))] if len(items) > 1 else []
    # Keep acted-on marks for anyone still in the new list: a swipe that
    # landed while this build ranked must not bring that person back. Marks
    # for people no longer listed are dropped, so the field stays small.
    new_ids = {((it["c"].get("person") or {}).get("id")) for it in items}
    try:
        prev = _ref(db, uid).get()
        prev_removed = (prev.to_dict() or {}).get("removed") or [] if getattr(prev, "exists", False) else []
    except Exception:
        prev_removed = []
    _ref(db, uid).set({
        "items":         items,
        "count":         len(items),
        "built_at":      datetime.now(timezone.utc).isoformat(),
        "fingerprint":   profile_fingerprint(profile),
        "model_version": model_version_for(uid),
        "stale":         False,
        "shown":         {},
        "removed":       [r for r in prev_removed if r in new_ids],
    })
    logger.info("people_deck: built uid=%s people=%d", uid, len(items))
    return len(items)


def schedule_build(uid: str, reason: str = "") -> bool:
    """Rebuild in the background. One build per student at a time; extra
    requests while one runs are dropped (that build already sees the latest
    data). Returns True if a build was queued."""
    if not uid:
        return False
    with _building_lock:
        if uid in _building:
            return False
        _building.add(uid)

    def _run():
        try:
            from app.extensions import get_db
            db = get_db()
            if db is not None:
                build_deck(uid, db)
        except Exception:
            logger.exception("people_deck: build failed uid=%s reason=%s", uid, reason)
        finally:
            with _building_lock:
                _building.discard(uid)

    try:
        _build_pool.submit(_run)
    except Exception:
        with _building_lock:
            _building.discard(uid)
        logger.exception("people_deck: could not queue build uid=%s", uid)
        return False
    logger.info("people_deck: build queued uid=%s reason=%s", uid, reason)
    return True


def mark_stale(uid: str, db, reason: str = "") -> None:
    """The student's targets changed: keep serving the old list, rebuild now."""
    try:
        _ref(db, uid).set({"stale": True}, merge=True)
    except Exception:
        logger.debug("people_deck: mark_stale failed uid=%s", uid)
    schedule_build(uid, reason or "stale")


# ── Acted-on people ───────────────────────────────────────────────────


def remove(uid: str, db, candidate_id: str) -> None:
    """The student swiped or drafted this person: never serve them again
    from the saved list."""
    if not candidate_id:
        return
    try:
        from google.cloud import firestore
        _ref(db, uid).set({"removed": firestore.ArrayUnion([candidate_id])}, merge=True)
    except Exception:
        logger.debug("people_deck: remove failed uid=%s cid=%s", uid, candidate_id)


def restore(uid: str, db, candidate_id: str) -> None:
    if not candidate_id:
        return
    try:
        from google.cloud import firestore
        _ref(db, uid).set({"removed": firestore.ArrayRemove([candidate_id])}, merge=True)
    except Exception:
        logger.debug("people_deck: restore failed uid=%s cid=%s", uid, candidate_id)


# ── Serve ─────────────────────────────────────────────────────────────


def needs_rebuild(doc: dict, profile: dict, model_version: str, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if doc.get("stale"):
        return True
    if doc.get("model_version") != model_version:
        return True
    if doc.get("fingerprint") != profile_fingerprint(profile):
        return True
    try:
        built = datetime.fromisoformat(str(doc.get("built_at")))
        if built.tzinfo is None:
            built = built.replace(tzinfo=timezone.utc)
        return (now - built).total_seconds() > FRESH_SECONDS
    except (TypeError, ValueError):
        return True


def pick(doc: dict, limit: int, exclude_ids: Optional[set] = None) -> list[dict]:
    """The next people to show, from the saved list: drop acted-on and
    already-on-screen people, then lead with the ones seen least."""
    removed = set(doc.get("removed") or [])
    exclude_ids = exclude_ids or set()
    shown = doc.get("shown") or {}
    pool = []
    for idx, it in enumerate(doc.get("items") or []):
        pid = ((it.get("c") or {}).get("person") or {}).get("id")
        if not pid or pid in removed or pid in exclude_ids:
            continue
        adj = float(it.get("rs") or 0) - SEEN_PENALTY * int(shown.get(pid) or 0)
        pool.append((-adj, idx, it))
    pool.sort(key=lambda t: (t[0], t[1]))
    return [it for _, _, it in pool[:limit]]


def serve(uid: str, db, limit: int, exclude_ids: Optional[set] = None):
    """(candidates, features) from the saved list, or None when there is no
    usable list (the caller ranks live). Queues a rebuild when the list is
    missing or stale; a stale list is still served."""
    from app.services.person_ranker import model_version_for

    try:
        ref = _ref(db, uid)
        user_ref = db.collection("users").document(uid)
        # One round trip for both docs. get_all may return them in any
        # order, so tell them apart by doc id.
        deck_snap = user_snap = None
        for snap in db.get_all([ref, user_ref]):
            if snap.id == DECK_DOC_ID:
                deck_snap = snap
            else:
                user_snap = snap
    except Exception:
        logger.debug("people_deck: read failed uid=%s", uid)
        return None

    doc = (deck_snap.to_dict() or {}) if deck_snap is not None and getattr(deck_snap, "exists", False) else {}
    profile = (user_snap.to_dict() or {}) if user_snap is not None and getattr(user_snap, "exists", False) else {}

    if not doc.get("items"):
        schedule_build(uid, "missing")
        return None
    if needs_rebuild(doc, profile, model_version_for(uid)):
        schedule_build(uid, "refresh")

    # Never recommend a user to themselves, even from a list built before
    # they added their own LinkedIn.
    from app.services.person_ranker import own_candidate_id
    own_id = own_candidate_id(profile)
    pick_exclude = (set(exclude_ids or ()) | {own_id}) if own_id else exclude_ids

    picked = pick(doc, limit, pick_exclude)
    if not picked:
        return None

    if not exclude_ids:
        # A fresh visit (not "load more"): count the showing so these people
        # sink a little and the next visit leads with someone new.
        try:
            from google.cloud import firestore
            ids = [((it.get("c") or {}).get("person") or {}).get("id") for it in picked]
            ref.set({"shown": {i: firestore.Increment(1) for i in ids if i}}, merge=True)
        except Exception:
            logger.debug("people_deck: shown count failed uid=%s", uid)

    candidates = [it["c"] for it in picked]
    features = {(it["c"].get("person") or {}).get("id"): dict(it.get("f") or {}, served_from="saved_deck")
                for it in picked}
    return candidates, features
