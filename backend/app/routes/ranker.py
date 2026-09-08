"""HTTP surface for the iOS ranker.

Thin — algorithm lives in services/person_ranker.py; this module owns
HTTP shape, validation, and credit metering. Rylan's swipe-deck client
hits these three endpoints only.

    GET  /api/ranker/candidates                        — deck (free)
    POST /api/ranker/feedback                          — swipe signal (free)
    POST /api/ranker/candidates/<id>/reveal-email      — metered Hunter lookup

See person_ranker module docstring for algorithm details, sensitive-field
posture, and known gaps.
"""
import logging
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from firebase_admin import firestore

from ..extensions import require_firebase_auth, get_db
from app.services.firm_cache.schema import slugify_company
from app.services.person_ranker import (
    DEFAULT_RANKED_LIMIT,
    MAX_RANKED_LIMIT,
    rank_people_for_user,
)
from app.utils.recommendation_events import log_recommendation_event

logger = logging.getLogger(__name__)

ranker_bp = Blueprint("ranker", __name__, url_prefix="/api/ranker")


# ── Tunables ──────────────────────────────────────────────────────────
# Reveal-email cache TTL. 30 days is comfortably shorter than typical
# job-change cadence, so cached email stays deliverable. After TTL, we
# re-charge and re-lookup rather than serve stale addresses.
REVEAL_CACHE_TTL_DAYS = 30

# Warehouse-cache TTL for the shared work_email on firm_employees docs.
# One student's verified reveal serves every later student free (the cache
# economics the whole provider plan leans on); longer than the per-user
# TTL because it was SMTP-verified at write time, shorter than job-change
# cadence would make risky.
WAREHOUSE_EMAIL_TTL_DAYS = 90

# Charge-keeper threshold for reveal-email. Hunter's own "trustworthy"
# cutoff for Email Finder is 80; we put a 10-point safety margin above
# that. `email_verified == True` (Hunter SMTP-confirmed) is stronger
# than any confidence score and unconditionally keeps the charge.
# Refund on everything else — err toward refund because a charged
# bounce hurts trust more than a free miss.
REVEAL_CHARGE_MIN_CONFIDENCE = 90


# ── Helpers ───────────────────────────────────────────────────────────


def _within_ttl(iso_ts: str | None, *, days: int) -> bool:
    """True if an ISO-8601 timestamp is within `days` of now (UTC)."""
    if not iso_ts:
        return False
    try:
        # Handle "Z" suffix (Python <3.11 fromisoformat doesn't parse it).
        parsed = iso_ts.rstrip("Z")
        ts = datetime.fromisoformat(parsed)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    delta = datetime.now(timezone.utc) - ts
    return 0 <= delta.total_seconds() <= days * 86400


def _try_refund(uid: str, amount: int, reason: str) -> None:
    """Refund credits, log-and-continue on failure. Never raises.

    Firestore blip during refund must NOT bubble a 500 to the client —
    that would lose the money silently AND fail the endpoint. Log
    loudly so Sid can eyeball the credits ledger for orphan deductions.
    """
    try:
        from app.services.auth import refund_credits_atomic
        refund_credits_atomic(uid, amount, reason)
    except Exception as exc:
        logger.warning(
            "reveal: refund failed uid=%s amount=%d reason=%s exc=%s "
            "(ledger will show orphan deduction; manual reconcile)",
            uid, amount, reason, exc,
        )


# ── Routes ────────────────────────────────────────────────────────────


@ranker_bp.route("/candidates", methods=["GET"])
@require_firebase_auth
def get_ranked_candidates():
    """GET /api/ranker/candidates?limit=50

    Returns a diversified, scored list of people the authenticated user
    should reach out to. No search-query params — the ranker sources
    candidates from the caller's profile (dreamCompanies +
    academics.school) alone. Deterministic given user profile +
    firm_employees state (same call → same result).

    Response 200:
      {
        "candidates":  [{person, score, tier, reasons, briefing}, ...],
        "count":       int,
        "deck_id":     str,          # opaque token — client MUST echo on
                                     # feedback + reveal-email calls
        "generatedAt": ISO8601
      }

    Response 200 with count=0:
      Deck empty. Causes: sparse profile, no firm_employees hits, or
      (defensively) an unexpected error inside the ranker. `reason`
      distinguishes for the client. Clients render an empty state,
      not an error.

    Response 500:
      Database not initialized (backend health issue, not a user issue).
    """
    uid = request.firebase_user["uid"]
    try:
        limit = int(request.args.get("limit", DEFAULT_RANKED_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_RANKED_LIMIT
    limit = max(1, min(limit, MAX_RANKED_LIMIT))

    # ?exclude=id1,id2 — candidate ids the client has already shown, so a
    # refresh EXTENDS the feed with new people instead of replaying the same
    # top-N (Rylan 2026-08-19: "refresh should cache a new 20 to continue
    # what the default feed would show"). Capped defensively.
    exclude_ids = {
        e.strip() for e in (request.args.get("exclude") or "").split(",") if e.strip()
    }
    if len(exclude_ids) > 500:
        exclude_ids = set(list(exclude_ids)[:500])

    # Company deck (2026-09-07): ?company=Hallmark%20Media narrows the deck
    # to one firm (the extension's Company tab). ?role= is the typed filter
    # or chip. ?surface= labels the event rows and sizes the prefetch.
    company = (request.args.get("company") or "").strip()[:120] or None
    role = (request.args.get("role") or "").strip()[:80] or None
    surface = (request.args.get("surface") or "").strip()[:40] or "ios_swipe_deck"
    # ?prefetch=0 skips the FullEnrich prefetch (the extension's glance lists).
    do_prefetch = (request.args.get("prefetch") or "1").strip() != "0"

    db = get_db()
    if not db:
        return jsonify({"error": "Database not initialized"}), 500

    candidates = rank_people_for_user(
        uid=uid, db=db, limit=limit, exclude_ids=exclude_ids,
        company=company, role=role,
    )

    # Dry well? DIG (2026-08-30). "Show more from your targets" rank-orders the
    # warehouse, so a target firm nobody has searched yet (Rylan's well was
    # Stripe-only, warehouse rows: zero) returned no_candidates forever, which
    # read as the button doing nothing. When the rank comes back empty and the
    # user has target firms, run ONE live Coresignal search for the least
    # covered firm, feed the warehouse, and rank again. Bounded: one firm, one
    # search, ~10 collects, only on an empty rank, so a healthy well never
    # pays it and a dry one pays it once.
    if not candidates:
        try:
            from app.services import coresignal_client
            from app.services.firm_cache.writer import cache_pdl_contacts
            from app.services.person_ranker import _load_user_context
            _ctx = _load_user_context(uid, db) or {}
            # A company deck digs that company and nothing else.
            _firms = [slugify_company(company)] if company else (_ctx.get("dream_company_slugs") or [])
            _firms = [f for f in _firms if f]
            _dig_firm = None
            for _f in _firms:
                # The warehouse stamps the firm slug under "company"
                # (schema.py), not "company_slug".
                _n = (db.collection("firm_employees")
                        .where("company", "==", _f).limit(1).get())
                if not list(_n):
                    _dig_firm = _f
                    break
            if _dig_firm is None and _firms:
                _dig_firm = _firms[0]
            if _dig_firm and getattr(coresignal_client, "CORESIGNAL_API_KEY", ""):
                _dig_name = company if company else _dig_firm.replace("-", " ")
                _dug, _rl, _sv, _meta = coresignal_client.search_contacts_from_prompt(
                    {"companies": [{"name": _dig_name}],
                     "title_variations": [role] if role else [],
                     "locations": ["United States"]},
                    12,
                )
                # Keep only people whose employer actually IS the firm. A
                # one-word firm name like Stripe token-matches "Kleen Stripe"
                # and "PERMA-STRIPE OF FLORIDA" in the search index; caching
                # those as Stripe would poison the warehouse for everyone.
                import re as _re
                _firm_l = _dig_name.lower().strip()
                _pat = _re.compile(r"^\s*" + _re.escape(_firm_l) + r"\b", _re.I)
                _dug = [c for c in (_dug or []) if _pat.match((c.get("Company") or ""))]
                if _dug:
                    cache_pdl_contacts(_dug, shape="app", async_write=False)
                    _collected = int(((_meta or {}).get("collected_count")) or 0)
                    if _collected:
                        try:
                            from google.cloud import firestore as _fs
                            db.collection("meta").document("coresignalTestBudget").set(
                                {"spent_estimate": _fs.Increment(20.0 * _collected),
                                 "collect_count": _fs.Increment(_collected)}, merge=True)
                            from app.services.metering import log_provider_spend
                            log_provider_spend("coresignal", "member_collect", _collected, returned=_collected)
                        except Exception:
                            pass
                    candidates = rank_people_for_user(
                        uid=uid, db=db, limit=limit, exclude_ids=exclude_ids,
                        company=company, role=role,
                    )
                    logger.info("ranker dig: %s -> %d collected, %d ranked",
                                _dig_firm, len(_dug), len(candidates))
        except Exception:
            logger.exception("ranker dig failed uid=%s", uid)

    generated_at = datetime.now(timezone.utc).isoformat()

    # Warm-first ordering (Rylan 2026-08-26): within each relevance tier,
    # people whose email is already banked in the warehouse lead the deck, so
    # early swipes draft instantly and the cold tail is reached only after
    # the prefetch below has had time to finish. Readiness is a tiebreaker
    # INSIDE a tier, never across tiers: the best-fit person still outranks
    # a mediocre-but-instant one. One batched read for the whole deck.
    if candidates:
        try:
            from app.services.firm_cache.schema import FIRM_EMPLOYEES_COLLECTION
            refs = [
                db.collection(FIRM_EMPLOYEES_COLLECTION).document(
                    ((c.get("person") or {}).get("id") or "").strip()
                )
                for c in candidates
                if ((c.get("person") or {}).get("id") or "").strip()
            ]
            ready_ids = set()
            for snap in db.get_all(refs):
                if not getattr(snap, "exists", False):
                    continue
                d = snap.to_dict() or {}
                if (d.get("work_email") or "").strip() and _within_ttl(
                    d.get("work_email_verified_at"), days=WAREHOUSE_EMAIL_TTL_DAYS
                ):
                    ready_ids.add(snap.id)
            if ready_ids:
                # Tier order as the RANKER emitted it, then readiness, then
                # the ranker's own order as the final stable tiebreak.
                tier_rank: dict = {}
                for c in candidates:
                    tier_rank.setdefault(c.get("tier"), len(tier_rank))
                original_index = {id(c): i for i, c in enumerate(candidates)}
                candidates.sort(key=lambda c: (
                    tier_rank.get(c.get("tier"), 99),
                    0 if ((c.get("person") or {}).get("id") in ready_ids) else 1,
                    original_index[id(c)],
                ))
        except Exception:
            logger.exception("ranked warm-first ordering failed uid=%s", uid)

    # deck_id is generated per-call. Client MUST echo it on feedback +
    # reveal-email calls so the training row can be joined back to the
    # deck context (features_snapshot, rank).
    deck_id = uuid.uuid4().hex[:16]

    # FullEnrich prefetch (2026-08-25), same trick the prompt deck ships:
    # one bulk waterfall for the top of the deck, fired while the student
    # reads card one, so a right-swipe finds its address already waiting
    # instead of paying a live 30-second lookup. Runs in a daemon thread:
    # deck latency is the product here and the prefetch must never add to
    # it. Their dedupe makes re-enriching a recently-enriched person free,
    # so no cache check is needed before asking.
    if candidates and do_prefetch:
        def _prefetch(cands, uid_, deck_id_):
            try:
                from app.services import fullenrich_client
                from app.extensions import get_db as _get_db
                pdb = _get_db()
                if pdb is None or not fullenrich_client.enabled(pdb):
                    return
                wanting = []
                # The extension opens company pages while browsing; keep its
                # prefetch to the rows on screen.
                _prefetch_n = 6 if surface == "extension" else 12
                for c in cands[:_prefetch_n]:
                    p = c.get("person") or c
                    pid = (p.get("id") or "").strip()
                    if not pid:
                        continue
                    wanting.append((pid,
                                    p.get("firstName") or "",
                                    p.get("lastName") or "",
                                    p.get("company") or "",
                                    (p.get("linkedinUrl") or "").strip()))
                if not wanting:
                    return
                eid = fullenrich_client.start_bulk(wanting, db=pdb)
                if eid:
                    (pdb.collection("users").document(uid_)
                        .collection("rankerPrefetch").document(deck_id_)
                        .set({"fe_job": eid,
                              "pids": [w[0] for w in wanting],
                              "created_at": datetime.now(timezone.utc).isoformat()}))
            except Exception:
                logger.exception("ranker prefetch failed uid=%s", uid_)
        import threading as _threading
        _threading.Thread(target=_prefetch, args=(candidates, uid, deck_id),
                          daemon=True, name="ranker-prefetch").start()

    if not candidates:
        return jsonify({
            "candidates":  [],
            "count":       0,
            "deck_id":     deck_id,
            "reason":      "no_candidates",
            "generatedAt": generated_at,
        })
    return jsonify({
        "candidates":  candidates,
        "count":       len(candidates),
        "deck_id":     deck_id,
        "generatedAt": generated_at,
    })


@ranker_bp.route("/feedback", methods=["POST"])
@require_firebase_auth
def post_ranker_feedback():
    """POST /api/ranker/feedback

    Record a swipe. Two writes, both idempotent per (deck_id, candidate_id):

      1. users/{uid}/peoplePreferences/{candidate_id} — latest-wins state
         powering "don't show again" exclusion in future decks. Read by
         person_ranker._swiped_person_ids.
      2. recommendation_events/person_swipe_{deck_id}_{candidate_id} —
         training-data label. Deterministic doc_id so client retries
         cannot double-count.

    Request body:
      {
        "candidate_id": str,      # required — from person.id in the deck response
        "signal":       "right" | "left" | "skip",
        "deck_id":      str,      # required — echoed from candidates response
        "rank":         int,      # required — 0-based deck position

        // Recommended for training-data feature snapshotting; if omitted,
        // features_snapshot is empty on the event row but the training
        // pipeline can still join back to context via (deck_id, candidate_id).
        "score":        int,
        "tier":         "warm" | "neutral" | "cold",
        "reasons":      list,
        "briefing":     str
      }

    Response 200 {"ok": true} — normal.
    Response 400 — validation failure (candidate_id / signal / deck_id / rank).
    Response 500 — DB not initialized.
    """
    uid = request.firebase_user["uid"]
    data = request.get_json(silent=True) or {}

    candidate_id = (data.get("candidate_id") or "").strip()
    signal       = (data.get("signal") or "").strip()
    deck_id      = (data.get("deck_id") or "").strip()
    rank_val     = data.get("rank")

    if not candidate_id:
        return jsonify({"error": "candidate_id required"}), 400
    if signal not in ("right", "left", "skip", "restore"):
        return jsonify({"error": "signal must be right, left, skip, or restore"}), 400

    # "restore" (2026-09-03): the user put someone back from Network's
    # "People you passed on". Forget the left/skip mark so the ranker can
    # serve them again; nothing else to record. No deck_id or rank needed.
    if signal == "restore":
        db = get_db()
        if not db:
            return jsonify({"error": "Database not initialized"}), 500
        try:
            (db.collection("users").document(uid)
               .collection("peoplePreferences").document(candidate_id).delete())
        except Exception as exc:
            logger.warning("feedback: restore delete failed uid=%s cid=%s: %s", uid, candidate_id, exc)
        try:
            log_recommendation_event(
                event_type="person_swipe_restore", uid=uid, contact_id=candidate_id,
                surface="ios_network_passed", extra={"deck_id": deck_id or "restore"},
            )
        except Exception:
            logger.exception("feedback: restore event log failed uid=%s", uid)
        return jsonify({"ok": True, "restored": candidate_id}), 200

    if not deck_id:
        return jsonify({
            "error": "deck_id required — echo from /candidates response"
        }), 400
    try:
        rank_int = int(rank_val)
        if rank_int < 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "rank required, must be int >= 0"}), 400

    db = get_db()
    if not db:
        return jsonify({"error": "Database not initialized"}), 500

    # Optional attribution echo (features captured at deck-render time).
    # Client trust model: user is source of truth for their own action
    # (signal + rank); everything else is a best-effort echo of what we
    # returned in the deck response. If we later suspect drift, we can
    # add server-side impression snapshots. MVP accepts client echo.
    score    = data.get("score")
    tier     = data.get("tier") or ""
    reasons  = data.get("reasons") or []
    briefing = data.get("briefing") or ""

    now = datetime.now(timezone.utc)

    # Write 1: peoplePreferences — latest-wins overwrite.
    try:
        (db.collection("users")
           .document(uid)
           .collection("peoplePreferences")
           .document(candidate_id)
           .set({
               "candidate_id": candidate_id,
               "signal":       signal,
               "last_deck_id": deck_id,
               "last_rank":    rank_int,
               "updated_at":   now.isoformat(),
               "updated_ts":   firestore.SERVER_TIMESTAMP,
           }))
    except Exception as exc:
        logger.warning(
            "feedback: peoplePreferences write failed uid=%s cid=%s: %s",
            uid, candidate_id, exc,
        )

    # Write 2: recommendation_events — deterministic doc_id makes at-
    # least-once client retries safe (Pub/Sub-style redelivery too, if
    # this ever ends up behind a queue).
    log_recommendation_event(
        event_type=f"person_swipe_{signal}",
        uid=uid,
        contact_id=candidate_id,
        rank=rank_int,
        score=float(score) if isinstance(score, (int, float)) else None,
        model_version="person_ranker_v0",
        surface="ios_swipe_deck",
        features_snapshot={
            "tier":           tier,
            "warmth_signals": reasons,
            "briefing":       briefing,
            "deck_id":        deck_id,
        },
        attribution_source="ranker_deck",
        extra={"signal": signal},
        doc_id=f"person_swipe_{deck_id}_{candidate_id}",
    )

    return jsonify({"ok": True})


@ranker_bp.route("/candidates/<candidate_id>/reveal-email", methods=["POST"])
@require_firebase_auth
def reveal_candidate_email(candidate_id):
    """POST /api/ranker/candidates/<candidate_id>/reveal-email

    Look up a verified email for a ranker candidate. Metered (see
    CREDIT_COSTS["reveal_email"]) on cache-miss + charge-worthy Hunter
    result. Cache-first: subsequent calls within TTL are free. Refund
    on low-confidence / no-hit / error.

    MUST NOT be called on card render — it charges credits. Client
    wires this into the swipe-right handler only. The 30-day cache
    protects against accidental double-charges from client retries.

    Charge rule (both branches → keep the deduction):
      * Hunter returned email_verified == True (SMTP-confirmed), OR
      * email_source == "hunter_finder" AND confidence >= REVEAL_CHARGE_MIN_CONFIDENCE

    Refund rule (any of):
      * verified == False AND confidence < REVEAL_CHARGE_MIN_CONFIDENCE
      * email_source ∈ {"pattern", "domain_generated", "pdl_fallback"}
        (synthesized / best-guess, not deliverability-verified)
      * no email found
      * Hunter API error / timeout

    Request body (optional):
      {"deck_id": str}      # for cross-endpoint attribution on the
                            # recommendation_events row

    Response 200 (email found + charge-worthy):
      {email, verified: true, source, confidence, cached: bool, charged: true}

    Response 200 (email found but below threshold OR no email):
      {email: str|null, verified: bool, source, confidence,
       reason: "no_email_found" | "below_confidence_threshold",
       cached: false, charged: false}

    Response 402: {"error": "insufficient_credits", "required": int, "have": int}
    Response 404: {"error": "candidate_not_found"}
    Response 500: {"error": "..."}
    """
    uid = request.firebase_user["uid"]
    data = request.get_json(silent=True) or {}
    deck_id = (data.get("deck_id") or "").strip()
    surface = (str(data.get("surface") or "").strip()[:40]) or "ios_swipe_deck"

    candidate_id = (candidate_id or "").strip()
    if not candidate_id:
        return jsonify({"error": "candidate_id required in path"}), 400

    db = get_db()
    if not db:
        return jsonify({"error": "Database not initialized"}), 500

    # ── 1. Cache-first — no credit deduction, no Hunter call.
    cache_ref = (db.collection("users").document(uid)
                   .collection("revealedEmails").document(candidate_id))
    try:
        cached_snap = cache_ref.get()
    except Exception:
        cached_snap = None
    if cached_snap is not None and getattr(cached_snap, "exists", False):
        cached = cached_snap.to_dict() or {}
        if _within_ttl(cached.get("revealed_at"), days=REVEAL_CACHE_TTL_DAYS):
            return jsonify({
                "email":      cached.get("email"),
                "verified":   bool(cached.get("verified")),
                "source":     cached.get("source"),
                "confidence": cached.get("confidence", 0),
                "cached":     True,
                "charged":    False,
            })

    # ── 2. firm_employees lookup — need first/last/company for Hunter.
    from app.services.firm_cache.schema import FIRM_EMPLOYEES_COLLECTION
    try:
        fs_snap = db.collection(FIRM_EMPLOYEES_COLLECTION).document(candidate_id).get()
    except Exception:
        fs_snap = None
    if fs_snap is None or not getattr(fs_snap, "exists", False):
        # No credit spent yet — safe to fail.
        return jsonify({"error": "candidate_not_found"}), 404
    fs = fs_snap.to_dict() or {}
    first = (fs.get("first_name") or "").strip()
    last  = (fs.get("last_name")  or "").strip()
    company_display = (fs.get("company_display") or "").strip()

    # ── 3. Deduct atomically BEFORE any paid work.
    from app.services.auth import deduct_credits_atomic
    from app.config import CREDIT_COSTS
    cost = int(CREDIT_COSTS.get("reveal_email", 8))
    ok, remaining = deduct_credits_atomic(uid, cost, "reveal_email")
    if not ok:
        return jsonify({
            "error":    "insufficient_credits",
            "required": cost,
            "have":     remaining,
        }), 402

    email: str | None = None
    verified = False
    source = ""
    confidence = 0

    # ── 3b. Warehouse cache: a previous student's verified reveal for this
    # SAME person, stored on the shared firm_employees doc. The user still
    # pays (the product is the unlock), the vendors get nothing, and the
    # response is instant. This is the "popular targets are paid for once"
    # economics, made real.
    wh_email = (fs.get("work_email") or "").strip()
    if wh_email and _within_ttl(fs.get("work_email_verified_at"), days=WAREHOUSE_EMAIL_TTL_DAYS):
        email, verified, source = wh_email, True, "warehouse"

    # ── 3b2. Hunter fast rung (2026-09-07): one database, 2 to 3 seconds,
    # accepted at confidence >= REVEAL_CHARGE_MIN_CONFIDENCE. A hit skips the
    # prefetch wait and the live waterfall; a miss costs a few seconds and
    # falls through. Same rung the website Find and the extension run
    # (app.services.email_fast). Stays per user: Hunter scores
    # deliverability, it does not SMTP-verify, so it never goes on the
    # shared shelf.
    if not email:
        try:
            from app.services import email_fast as _ef
            _h = _ef.hunter_email(first, last, company_display)
            if _h and _h.get("email") and int(_h.get("confidence") or 0) >= REVEAL_CHARGE_MIN_CONFIDENCE:
                email = _h["email"]
                verified = False
                source = "hunter_finder"
                confidence = int(_h.get("confidence") or 0)
        except Exception:
            logger.exception("reveal: hunter fast rung failed uid=%s cid=%s", uid, candidate_id)

    # ── 3c. Prefetch lookup: the deck-serve fired one bulk waterfall for
    # the top cards; by swipe time it is usually FINISHED, so this is a
    # read, not a wait. fe_checked=True means the job finished and a
    # missing pid is an authoritative miss (skip the live waterfall).
    fe_checked = False
    fe_covered = False
    if not email and deck_id:
        try:
            from app.services import fullenrich_client
            pre_snap = (db.collection("users").document(uid)
                          .collection("rankerPrefetch").document(deck_id).get())
            pre = (pre_snap.to_dict() or {}) if getattr(pre_snap, "exists", False) else {}
            if pre.get("fe_job") and candidate_id in (pre.get("pids") or []):
                # Fast swipers reach here before the waterfall finishes; wait
                # for the job we already paid for rather than abandoning it
                # (the prompt deck learned this the hard way, 2026-08-26).
                fe_covered = True
                ready, results = fullenrich_client.fetch_bulk(pre["fe_job"], wait_seconds=45, db=db)
                fe_checked = bool(ready)
                hit_pre = results.get(str(candidate_id))
                if hit_pre:
                    if fullenrich_client.sellable_gate(hit_pre["email"], hit_pre["status"]):
                        email = hit_pre["email"]
                        verified = True
                        source = f"fullenrich_prefetch_{hit_pre['status'].lower()}"
        except Exception:
            logger.exception("reveal: prefetch lookup failed uid=%s cid=%s", uid, candidate_id)

    # ── 4. (Removed 2026-09-01.) The Hunter rung that sat here paid twice
    # for the same lookup: Hunter is one of FullEnrich's own upstream
    # providers and verification is included in their 1-credit cost (per
    # the reseller agreement thread). Email sourcing is FullEnrich-only
    # now; the waterfall below is the single rung after the warehouse.

    # Refund unless we have deliverability evidence: SMTP-verified OR
    # Hunter Email Finder with confidence >= threshold. Synthesized
    # sources (pattern / domain_generated / pdl_fallback) never charge.
    charge_worthy = bool(
        email and (
            verified
            or (source == "hunter_finder" and confidence >= REVEAL_CHARGE_MIN_CONFIDENCE)
        )
    )

    # ── 4b. FullEnrich waterfall (2026-08-25). Hunter is one database; the
    # bench measured single sources at 40-60% on US targets while the
    # waterfall ran 88-95%, and THIS deck was the only surface still
    # stopping at one source. Same contract as the prompt deck's reveal:
    # DELIVERABLE sells as-is, HIGH_PROBABILITY must pass the SMTP gate,
    # anything else stays a miss. 1 FullEnrich credit only when found.
    # Skipped when a FINISHED prefetch already ruled this person a miss —
    # the live lookup would spend 30 seconds rediscovering that — and when
    # the person is covered by a still-running job (asking twice 429s and
    # can double-bill when both finish).
    if not charge_worthy and not fe_checked and not fe_covered:
        fe_hit = None
        try:
            from app.services import fullenrich_client
            fe_hit = fullenrich_client.find_work_email(
                first_name=first,
                last_name=last,
                company=company_display,
                linkedin_url=(fs.get("linkedin_url") or "").strip(),
                db=db,
            )
        except Exception:
            logger.exception("reveal: fullenrich failed uid=%s cid=%s", uid, candidate_id)
        if fe_hit and fe_hit.get("email"):
            from app.services import fullenrich_client as _fec
            if _fec.sellable_gate(fe_hit["email"], fe_hit.get("status") or ""):
                email = fe_hit["email"]
                verified = True
                source = f"fullenrich_{(fe_hit.get('status') or 'found').lower()}"
                confidence = 0
                charge_worthy = True

    if not charge_worthy:
        _try_refund(uid, cost, "reveal_email_refund")
        return jsonify({
            "email":      email,
            "verified":   verified,
            "source":     source,
            "confidence": confidence,
            "reason":     "no_email_found" if not email else "below_confidence_threshold",
            "cached":     False,
            "charged":    False,
        })

    # ── 5. Charge-worthy: write cache + event. Failures here do NOT
    # cost the user the charge — they got the email in the response and
    # kept the value (see reveal-email safety trace in the handoff doc).

    # Warehouse write-back: a freshly found, verified address goes onto the
    # shared firm_employees doc, so the NEXT student's reveal of this person
    # is instant and vendor-free. Only verified addresses are shared;
    # confidence-threshold Hunter finds stay per-user.
    if verified and source != "warehouse":
        try:
            db.collection(FIRM_EMPLOYEES_COLLECTION).document(candidate_id).set({
                "work_email":             email,
                "work_email_source":      source,
                "work_email_verified_at": datetime.now(timezone.utc).isoformat(),
            }, merge=True)
        except Exception as exc:
            logger.warning("reveal: warehouse write-back failed cid=%s: %s", candidate_id, exc)

    try:
        cache_ref.set({
            "candidate_id": candidate_id,
            "email":        email,
            "verified":     verified,
            "source":       source,
            "confidence":   confidence,
            "revealed_at":  datetime.now(timezone.utc).isoformat(),
            "revealed_ts":  firestore.SERVER_TIMESTAMP,
        })
    except Exception as exc:
        logger.warning(
            "reveal: cache write failed uid=%s cid=%s: %s "
            "(user still gets email + charge; next call will re-Hunter)",
            uid, candidate_id, exc,
        )

    # Reveal event doc_id is deliberately NOT deck-scoped.
    # Reveal is a commercial event (credit-spend), not a training
    # signal — training labels come from person_swipe_* events which
    # ARE deck-scoped. features_snapshot.deck_id preserves the join
    # back to the deck context for the latest reveal. See handoff doc.
    log_recommendation_event(
        event_type="email_revealed",
        uid=uid,
        contact_id=candidate_id,
        contact_email=email or "",
        model_version="person_ranker_v0",
        surface=surface,
        features_snapshot={
            "deck_id":    deck_id,
            "confidence": confidence,
            "source":     source,
        },
        attribution_source="ranker_deck",
        extra={"cost_credits": cost},
        doc_id=f"email_revealed_{uid}_{candidate_id}",
    )

    return jsonify({
        "email":      email,
        "verified":   verified,
        "source":     source,
        "confidence": confidence,
        "cached":     False,
        "charged":    True,
    })


@ranker_bp.route("/candidates/<candidate_id>/outreach", methods=["POST"])
@require_firebase_auth
def candidate_outreach(candidate_id):
    """POST /api/ranker/candidates/<candidate_id>/outreach

    Write to the person the user swiped on. Every other drafting path is
    find-then-draft; here the person is already chosen by hand, so this
    route must never go looking for "someone like them".

    Request body: {"action": "draft"|"send", "deck_id": optional str}

    Pricing: NO additional charge. The reveal-email swipe already paid
    for "the verification and the written email" (see the reveal_person
    note in config.CREDIT_COSTS), so this route requires a fresh reveal
    and generates the email for free. Double-charging the same swipe
    would break that promise.

    Flow:
      1. Idempotency: an existing ranker contact doc for this candidate
         short-circuits to its recorded outcome (client retries on the
         60s timeout must not write the same person twice).
      2. Require a fresh revealedEmails cache entry (the reveal charge).
         No entry, or expired, is a 404: the client reveals first.
      3. Generate with the same batch_generate_emails call prompt-search
         uses, so the voice matches the swipe drafts exactly.
      4. Gmail connected: create the draft there (or send, for
         action=send, behind the same quality gate + daily cap as
         prompt-search send mode). Not connected: the contact doc alone
         is the in-app draft, sent stays false.
      5. Save the contact doc in the runs.py shape so Inbox/Network
         render it like any other outreach.

    Response 200: {"drafted": bool, "sent": bool}
    Response 404: {"error": "candidate_not_found"} unknown candidate
                  {"error": "no_verified_email"} no fresh reveal
    Response 500: {"error": "generation_failed"} LLM produced nothing
    """
    uid = request.firebase_user["uid"]
    user_email = request.firebase_user.get("email") or ""
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "draft").strip().lower()
    if action not in ("draft", "send"):
        return jsonify({"error": "action must be draft or send"}), 400
    deck_id = (data.get("deck_id") or "").strip()

    candidate_id = (candidate_id or "").strip()
    if not candidate_id:
        return jsonify({"error": "candidate_id required in path"}), 400

    db = get_db()
    if not db:
        return jsonify({"error": "Database not initialized"}), 500

    # 1. Idempotency. Deterministic doc id, so a client retry after a
    # timeout replays the recorded outcome instead of drafting twice.
    contact_ref = (db.collection("users").document(uid)
                     .collection("contacts").document(f"ranker_{candidate_id}"))
    try:
        existing_snap = contact_ref.get()
    except Exception:
        existing_snap = None
    if existing_snap is not None and getattr(existing_snap, "exists", False):
        existing = existing_snap.to_dict() or {}
        if existing.get("emailBody"):
            return jsonify({
                "drafted": True,
                "sent":    bool(existing.get("emailSent")),
            })

    # 2. The reveal is the ticket. Same cache + TTL as reveal-email.
    reveal_ref = (db.collection("users").document(uid)
                    .collection("revealedEmails").document(candidate_id))
    try:
        reveal_snap = reveal_ref.get()
    except Exception:
        reveal_snap = None
    reveal = (reveal_snap.to_dict() or {}) if (
        reveal_snap is not None and getattr(reveal_snap, "exists", False)) else {}
    to_email = (reveal.get("email") or "").strip()
    if not to_email or not _within_ttl(reveal.get("revealed_at"), days=REVEAL_CACHE_TTL_DAYS):
        return jsonify({"error": "no_verified_email"}), 404

    # Candidate identity from the firm cache.
    from app.services.firm_cache.schema import FIRM_EMPLOYEES_COLLECTION
    from app.services.firm_cache.reader import firm_employee_doc_to_app_contact
    try:
        fs_snap = db.collection(FIRM_EMPLOYEES_COLLECTION).document(candidate_id).get()
    except Exception:
        fs_snap = None
    if fs_snap is None or not getattr(fs_snap, "exists", False):
        return jsonify({"error": "candidate_not_found"}), 404
    contact = firm_employee_doc_to_app_contact(fs_snap.to_dict() or {})
    contact["Email"] = to_email
    contact["EmailSource"] = reveal.get("source") or ""
    contact["EmailVerified"] = bool(reveal.get("verified"))
    contact["EmailConfidenceScore"] = int(reveal.get("confidence") or 0)

    # 3. Generate in the prompt-search voice.
    from app.utils.users import get_outreach_email
    from app.routes.runs import _resolve_email_template
    from app.services.reply_generation import (
        batch_generate_emails, PURPOSES_INCLUDE_RESUME, email_body_mentions_resume,
    )

    user_data = {}
    try:
        user_snap = db.collection("users").document(uid).get()
        if getattr(user_snap, "exists", False):
            user_data = user_snap.to_dict() or {}
    except Exception:
        pass
    user_tier = user_data.get("subscriptionTier", user_data.get("tier", "free")) or "free"
    auth_display_name = (request.firebase_user or {}).get("name") or ""

    user_profile = None
    try:
        prof_snap = (db.collection("users").document(uid)
                       .collection("professionalInfo").document("info").get())
        if getattr(prof_snap, "exists", False):
            pi = prof_snap.to_dict() or {}
            user_profile = {
                "name": f"{pi.get('firstName', '')} {pi.get('lastName', '')}".strip() or user_email,
                "email": user_email,
                "university": pi.get("university", ""),
                "major": pi.get("fieldOfStudy", ""),
                "year": pi.get("graduationYear", ""),
                "graduationYear": pi.get("graduationYear", ""),
                "degree": pi.get("currentDegree", ""),
            }
    except Exception:
        pass
    if not user_profile:
        user_profile = {"name": "", "email": user_email}
    for key in ("resumeParsed", "academics", "goals", "careerTrack",
                "dreamCompanies", "hometown", "location", "pastCompanies"):
        if key in user_data and key not in user_profile:
            user_profile[key] = user_data[key]
    outreach_identity = get_outreach_email(user_data)
    if outreach_identity:
        user_profile["email"] = outreach_identity

    career_interests = user_data.get("careerInterests", [])
    (template_instructions, email_template_purpose,
     template_subject_line, signoff_config) = _resolve_email_template(None, uid, db, user_data=user_data)

    # Cached resume text only. A cold PDF parse here would blow the
    # client's 60s budget; no cache means the draft just leans on the
    # profile instead.
    resume_url = user_data.get("resumeUrl") or user_data.get("resumeURL")
    resume_text = None
    cached_resume_text = user_data.get("resumeText")
    cached_resume_src = user_data.get("resumeTextSourceUrl")
    if (cached_resume_text and len(cached_resume_text.strip()) > 50
            and not (cached_resume_src and cached_resume_src != resume_url)):
        resume_text = cached_resume_text

    try:
        email_results = batch_generate_emails(
            contacts=[contact],
            resume_text=resume_text,
            user_profile=user_profile,
            career_interests=career_interests,
            pre_parsed_user_info=user_data.get("resumeParsed"),
            template_instructions=template_instructions,
            email_template_purpose=email_template_purpose,
            resume_filename=user_data.get("resumeFileName"),
            subject_line=template_subject_line,
            signoff_config=signoff_config,
            auth_display_name=auth_display_name,
            uid=uid,
        )
    except Exception as exc:
        logger.warning("outreach: generation raised uid=%s cid=%s: %s", uid, candidate_id, exc)
        email_results = {}
    result = (email_results or {}).get(0) or (email_results or {}).get("0") or {}
    subject = (result.get("subject") or "").strip()
    body = (result.get("plain_body") or result.get("body") or "").strip()
    if not subject or not body:
        return jsonify({"error": "generation_failed"}), 500

    attach_resume = (email_template_purpose in PURPOSES_INCLUDE_RESUME) or email_body_mentions_resume(body)
    item = {"contact": contact, "email_subject": subject,
            "email_body": body, "attach_resume": attach_resume}
    user_info = {"name": user_profile.get("name", ""),
                 "email": user_profile.get("email", ""), "phone": "", "linkedin": ""}

    # 4. Gmail. Send only behind the same guardrails as prompt-search
    # send mode: quality gate, then the shared daily send cap.
    sent = False
    gmail_draft_id = ""
    gmail_draft_url = ""
    gmail_message_id = ""
    gmail_thread_id = ""
    try:
        from app.services.gmail_client import _load_user_gmail_creds
        creds = _load_user_gmail_creds(uid)
    except Exception:
        creds = None

    if creds and action == "send":
        try:
            from app.utils.email_quality import check_email_quality
            qr = check_email_quality(subject, body, contact,
                                     (user_profile.get("university") or ""))
            quality_ok = bool(qr.get("passed", False))
        except Exception:
            quality_ok = False

        cap_ok = False
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        sends_today = 0
        if quality_ok:
            try:
                if user_data.get("dailySendDate") == today_str:
                    sends_today = int(user_data.get("dailySendCount", 0) or 0)
                cap_ok = sends_today < 20  # ELITE_DAILY_SEND_CAP in runs.py
            except Exception:
                cap_ok = False

        if quality_ok and cap_ok:
            try:
                from app.services.gmail_client import send_emails_parallel
                send_results = send_emails_parallel(
                    [item], user_info=user_info, user_id=uid,
                    tier=user_tier, user_email=user_email, resume_url=resume_url,
                )
                sr = send_results[0] if send_results else None
                message_id = sr.get("message_id", "") if isinstance(sr, dict) else ""
                if message_id and not str(message_id).startswith("mock_"):
                    sent = True
                    gmail_message_id = message_id
                    gmail_thread_id = sr.get("thread_id") or ""
                    db.collection("users").document(uid).set(
                        {"dailySendCount": sends_today + 1, "dailySendDate": today_str},
                        merge=True,
                    )
            except Exception as exc:
                logger.warning("outreach: send failed uid=%s cid=%s: %s", uid, candidate_id, exc)

    if creds and not sent:
        try:
            from app.services.gmail_client import create_drafts_parallel
            draft_results = create_drafts_parallel(
                [item], user_info=user_info, user_id=uid,
                tier=user_tier, user_email=user_email, resume_url=resume_url,
            )
            dr = draft_results[0] if draft_results else None
            draft_id = dr.get("draft_id", "") if isinstance(dr, dict) else (dr or "")
            if draft_id and not str(draft_id).startswith("mock_"):
                gmail_draft_id = draft_id
                if isinstance(dr, dict):
                    gmail_draft_url = dr.get("draft_url") or ""
        except Exception as exc:
            logger.warning("outreach: Gmail draft failed uid=%s cid=%s: %s", uid, candidate_id, exc)

    # 5. Persist in the runs.py contact-doc shape so Inbox and Network
    # render this like any other outreach.
    now_iso = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).strftime("%m/%d/%Y")
    contact_doc = {
        "firstName": contact.get("FirstName") or "",
        "lastName": contact.get("LastName") or "",
        "email": to_email,
        "linkedinUrl": contact.get("LinkedIn") or "",
        "company": contact.get("Company") or "",
        "jobTitle": contact.get("Title") or "",
        "college": contact.get("College") or "",
        "location": "",
        "city": contact.get("City") or "",
        "state": contact.get("State") or "",
        "firstContactDate": today,
        "status": "Not Contacted",
        "lastContactDate": today,
        "userId": uid,
        "createdAt": now_iso,
        "pdlId": contact.get("pdlId") or "",
        "candidateId": candidate_id,
        "attributionSource": "ranker_deck",
        "emailSource": contact.get("EmailSource") or None,
        # Face for the Inbox and Network rows; the Avatar falls back to
        # initials when this is empty or the LinkedIn hotlink expires.
        "photoUrl": (contact.get("PhotoUrl") or "").strip(),
        "emailVerified": bool(contact.get("EmailVerified")),
        "emailConfidenceScore": int(contact.get("EmailConfidenceScore") or 0),
        "emailSubject": subject,
        "emailBody": body,
        "inOutbox": True,
        "draftToEmail": to_email,
        "draftCreatedAt": now_iso,
        "emailGeneratedAt": now_iso,
        "draftStillExists": not sent,
        "lastActivityAt": now_iso,
        "hasUnreadReply": False,
        "gmailMessageId": gmail_message_id or None,
        "pipelineStage": "waiting_on_reply" if sent else (
            "draft_created" if gmail_draft_id else "generated"),
    }
    if gmail_draft_id:
        contact_doc["gmailDraftId"] = gmail_draft_id
    if gmail_draft_url:
        contact_doc["gmailDraftUrl"] = gmail_draft_url
    if sent:
        contact_doc["emailSent"] = True
        contact_doc["emailSentAt"] = now_iso
        if gmail_thread_id:
            contact_doc["gmailThreadId"] = gmail_thread_id
    try:
        contact_ref.set(contact_doc)
    except Exception as exc:
        logger.warning(
            "outreach: contact doc write failed uid=%s cid=%s: %s "
            "(draft may exist in Gmail without an app record)",
            uid, candidate_id, exc,
        )
        return jsonify({"error": "storage_failed"}), 500

    log_recommendation_event(
        event_type="outreach_sent" if sent else "outreach_drafted",
        uid=uid,
        contact_id=candidate_id,
        contact_email=to_email,
        model_version="person_ranker_v0",
        surface="ios_swipe_deck",
        features_snapshot={"deck_id": deck_id, "action": action},
        attribution_source="ranker_deck",
        extra={"cost_credits": 0},
        doc_id=f"outreach_{uid}_{candidate_id}",
    )

    return jsonify({"drafted": True, "sent": sent})
