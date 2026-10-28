"""
Provider-call metering.

Wraps any HTTP-calling function with `@meter_call("provider", "endpoint")` and
writes one row per call to the `provider_calls` Firestore collection (on a
background thread so we don't add latency to the user's request).

Used by every external data provider client so we have one place to look for:
  - Where credits are going (PDL vs Coresignal vs Hunter vs Apify)
  - Which users / features burn the most
  - Actual $ cost trend vs the provider's billing dashboard

DESIGN PRINCIPLES
  1. Metering must NEVER break a search. Every write is try/except'd; if
     Firestore is down, we log and keep going.
  2. Metering must NEVER add user-visible latency. Writes happen on a
     daemon thread spawned in the decorator's `finally` block.
  3. Credit + cost math is centralized in this module so each provider
     client stays clean — they just decorate their function.

PROVIDER_RATES are the *effective* $/credit assumptions used for the
`est_cost_usd` column. They're approximations sized to the Pro/Standard
plans we expect to be on; refine them as contracts settle.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, Dict, Optional, Tuple

from flask import g, has_app_context, has_request_context

from app.extensions import get_db

logger = logging.getLogger("metering")


# ---------------------------------------------------------------------------
# Rate sheet
#
# Effective $ per native credit. The native credit unit depends on provider:
#   - PDL:          1 credit = 1 result returned (Search) or 1 enriched record
#   - Coresignal:   Search = 1 credit / call;  Collect = 1 credit / profile.
#                   At Pro ($800/mo for 10K Collect + 20K Search), Collect is
#                   the meaningful unit; Search is essentially included.
#                   We attribute proportionally so the dashboard sums match
#                   the actual plan cost.
#   - Hunter:       1 credit = 1 request (Domain Search / Email Finder).
#   - Apify:        1 credit ≈ 1 actor-enrichment (proxy for compute units).
# ---------------------------------------------------------------------------

PROVIDER_RATES: Dict[str, Dict[str, float]] = {
    # title_enrich draws on the same fixed 50k pool as the person endpoints
    # (see pdl_title_cache). It is the largest PDL consumer by volume — 17k
    # credits as of 2026-10-22 — and was unmetered until then, so none of that
    # spend appeared in provider_calls or reached the spend alerter.
    "pdl":         {"person_search": 0.20, "person_enrich": 0.20,
                    "title_enrich": 0.20},
    # Coresignal bills a Search credit per ES-DSL search call and a Collect
    # credit per profile fetched. `search` rows are logged inside
    # coresignal_client._search_ids — the one chokepoint every search goes
    # through, and the only place that can see the HTTP 200 that bills.
    # `member_search` rows therefore carry Collect credits ONLY (see
    # _credits_for) and price at the Collect rate (see _cost_for); there is
    # deliberately no `member_search` entry here to bill against.
    "coresignal":  {"search": 0.04, "member_collect": 0.08},
    "hunter":      {"domain_search": 0.004, "email_finder": 0.003, "email_verify": 0.002},
    "apify":       {"linkedin_posts": 0.005, "sales_nav": 0.005},
    "neverbounce": {"single_check": 0.005},
    # FullEnrich: 1 credit = 1 found work email; Pro is $55/1,000 credits.
    # Search rung billed per result (capability matrix 2026-08-24).
    "fullenrich":  {"enrich": 0.055, "search": 0.0145},
}


def log_provider_spend(provider: str, endpoint: str, credits: float,
                       returned: int = 0) -> None:
    """One provider_calls row for spend tracked OUTSIDE meter_call (the
    vendor budget docs in fullenrich_client / people_deck). Without this,
    the spend alerter is blind to exactly the vendors the app now runs on.
    Background write, never raises, never blocks the caller."""
    try:
        rate = PROVIDER_RATES.get(provider, {}).get(endpoint, 0.0)
        payload = {
            "provider": provider,
            "endpoint": endpoint,
            "user_id": _get_g("user_id", "unknown"),
            "search_id": _get_g("search_id"),
            "returned_records": int(returned),
            "credits_charged": float(credits),
            "est_cost_usd": round(float(credits) * rate, 6),
            "cache_hit": False,
            "latency_ms": 0,
            "status": "ok",
            "error_msg": None,
        }
        threading.Thread(target=_safe_write, args=(payload,),
                         daemon=True, name=f"meter-{provider}").start()
    except Exception as e:  # noqa: BLE001
        logger.warning("log_provider_spend failed: %s", e)


# ---------------------------------------------------------------------------
# LLM rate sheet (token-based, USD per 1M tokens)
#
# OpenAI + Anthropic list prices. Keys are matched by longest startswith() so a
# dated model id ("gpt-4o-mini-2024-07-18", "claude-sonnet-4-6-20250...") maps
# to its base rate. Unknown models cost $0 (honest floor — no fake numbers; the
# model is logged so we can add it). `cached_input` defaults to `input` when a
# model has no separate cached rate.
# ---------------------------------------------------------------------------

LLM_RATES: Dict[str, Dict[str, float]] = {
    # OpenAI
    "gpt-4o-mini":             {"input": 0.15,  "cached_input": 0.075, "output": 0.60},
    "gpt-4o":                  {"input": 2.50,  "cached_input": 1.25,  "output": 10.00},
    "gpt-4-turbo":             {"input": 10.00, "cached_input": 10.00, "output": 30.00},
    "gpt-4.1-mini":            {"input": 0.40,  "cached_input": 0.10,  "output": 1.60},
    "gpt-4.1":                 {"input": 2.00,  "cached_input": 0.50,  "output": 8.00},
    "gpt-4":                   {"input": 30.00, "cached_input": 30.00, "output": 60.00},
    "text-embedding-3-small":  {"input": 0.02,  "cached_input": 0.02,  "output": 0.0},
    "text-embedding-3-large":  {"input": 0.13,  "cached_input": 0.13,  "output": 0.0},
    # Audio transcription (voice overhaul P2). Audio input bills ~$6/$3 per 1M
    # audio tokens; the small text-prompt share bills lower, so metering all
    # input at the audio rate over-counts by fractions of a cent — fine.
    # These MUST sit above the "gpt-4o"/"gpt-4o-mini" prefixes in match length
    # (longest-prefix wins) or audio calls would bill at chat rates.
    "gpt-4o-mini-transcribe":  {"input": 3.00,  "cached_input": 3.00,  "output": 5.00},
    "gpt-4o-transcribe":       {"input": 6.00,  "cached_input": 6.00,  "output": 10.00},
    # Anthropic (Claude). Cache reads bill at ~0.1x input.
    "claude-sonnet-4":         {"input": 3.00,  "cached_input": 0.30,  "output": 15.00},
    "claude-3-5-sonnet":       {"input": 3.00,  "cached_input": 0.30,  "output": 15.00},
    "claude-3-5-haiku":        {"input": 0.80,  "cached_input": 0.08,  "output": 4.00},
    "claude-opus-4":           {"input": 15.00, "cached_input": 1.50,  "output": 75.00},
}


def _llm_rate(model: str) -> Optional[Dict[str, float]]:
    """Longest-prefix match a model id to its rate sheet entry."""
    if not model:
        return None
    best_key = ""
    for key in LLM_RATES:
        if model.startswith(key) and len(key) > len(best_key):
            best_key = key
    return LLM_RATES.get(best_key) if best_key else None


def llm_cost_usd(model: str, input_tokens: int, cached_input_tokens: int, output_tokens: int) -> float:
    """USD cost of one LLM call. Cached input tokens bill at the cached rate."""
    rate = _llm_rate(model or "")
    if not rate:
        return 0.0
    uncached = max(0, int(input_tokens) - int(cached_input_tokens))
    return round(
        uncached / 1_000_000 * rate["input"]
        + int(cached_input_tokens) / 1_000_000 * rate.get("cached_input", rate["input"])
        + int(output_tokens) / 1_000_000 * rate["output"],
        8,
    )


def _tokens_from_usage(usage: Any) -> Tuple[int, int, int]:
    """
    Extract (input, cached_input, output) tokens from an OpenAI or Anthropic
    usage object (or a plain dict). Returns zeros if the shape is unrecognized.
    """
    if usage is None:
        return 0, 0, 0

    def _get(obj, name, default=0):
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    # OpenAI: prompt_tokens / completion_tokens / prompt_tokens_details.cached_tokens
    inp = _get(usage, "prompt_tokens", None)
    out = _get(usage, "completion_tokens", None)
    if inp is not None or out is not None:
        cached = 0
        details = _get(usage, "prompt_tokens_details", None)
        if details is not None:
            cached = _get(details, "cached_tokens", 0) or 0
        return int(inp or 0), int(cached or 0), int(out or 0)

    # Anthropic: input_tokens / output_tokens / cache_read_input_tokens
    inp = _get(usage, "input_tokens", None)
    out = _get(usage, "output_tokens", None)
    if inp is not None or out is not None:
        cached = _get(usage, "cache_read_input_tokens", 0) or 0
        return int(inp or 0), int(cached or 0), int(out or 0)

    return 0, 0, 0


def log_llm_usage(
    provider: str,
    model: str,
    usage: Any,
    *,
    latency_ms: int = 0,
    status: str = "ok",
    error_msg: Optional[str] = None,
) -> None:
    """
    Record one LLM call into the same `provider_calls` collection the data
    providers use, so spend dashboards + alerts see OpenAI/Claude too.

    Best-effort: never raises, never adds user-visible latency (Firestore write
    happens on a daemon thread). `usage` is the raw `.usage` off an OpenAI or
    Anthropic response; pass None to log a failed/streamed call as a 0-cost row.
    """
    try:
        inp, cached, out = _tokens_from_usage(usage)
        total = inp + out
        # Nothing useful to record (e.g. a streamed response with no usage).
        if total == 0 and status == "ok":
            return
        cost = llm_cost_usd(model, inp, cached, out)
        payload = {
            "provider": provider,
            "endpoint": model or "unknown",
            "user_id": _get_g("user_id", "unknown"),
            "search_id": _get_g("search_id"),
            "returned_records": 0,
            "credits_charged": total,          # for LLMs, "credits" == total tokens
            "input_tokens": inp,
            "cached_input_tokens": cached,
            "output_tokens": out,
            "est_cost_usd": cost,
            "cache_hit": cached > 0,
            "latency_ms": int(latency_ms),
            "status": status,
            "error_msg": error_msg,
        }
        threading.Thread(
            target=_safe_write,
            args=(payload,),
            daemon=True,
            name=f"meter-{provider}",
        ).start()
    except Exception as meter_err:  # noqa: BLE001 - metering must never break a call
        logger.warning("LLM metering failed: %s", meter_err)


def log_transcription_usage(
    model: str,
    usage: Any,
    *,
    latency_ms: int = 0,
    status: str = "ok",
    error_msg: Optional[str] = None,
) -> None:
    """Meter one audio-transcription call. Audio bypasses the chat.completions
    auto-meter patch, so the transcribe route calls this explicitly on both
    success and failure paths. Transcription responses carry
    input_tokens/output_tokens, which the Anthropic-shaped extractor in
    _tokens_from_usage already reads; this wrapper exists so the route has one
    obvious call and audio-specific fields can be added later."""
    log_llm_usage("openai", model, usage, latency_ms=latency_ms, status=status, error_msg=error_msg)


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------


def meter_call(provider: str, endpoint: str):
    """
    Decorator: wrap a provider-HTTP-calling function. After it returns (or
    raises), log a `provider_calls` row capturing credits, est_cost_usd,
    latency, status, plus user_id/search_id from Flask `g` if available.

    The wrapped function's return value is computed-once and not modified.
    """

    def decorator(fn: Callable):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            t0 = time.time()
            status = "ok"
            err: Optional[str] = None
            result: Any = None
            try:
                result = fn(*args, **kwargs)
                return result
            except Exception as e:  # noqa: BLE001 - re-raised below
                status = "error"
                err = str(e)[:300]
                raise
            finally:
                # Compute metering payload while still inside the request
                # context (so flask.g access works) but defer the Firestore
                # write to a daemon thread so the user sees no latency.
                try:
                    if status == "ok":
                        credits = _credits_for(provider, endpoint, result, kwargs)
                        returned = _count_returned(provider, endpoint, result)
                        cost = _cost_for(provider, endpoint, credits)
                    else:
                        credits, returned, cost = 0, 0, 0.0
                    latency_ms = int((time.time() - t0) * 1000)

                    payload = {
                        "provider": provider,
                        "endpoint": endpoint,
                        "user_id": _get_g("user_id", "unknown"),
                        "search_id": _get_g("search_id"),
                        "returned_records": returned,
                        "credits_charged": credits,
                        "est_cost_usd": cost,
                        "cache_hit": False,  # Phase 2 flips this to True for cached lookups
                        "latency_ms": latency_ms,
                        "status": status,
                        "error_msg": err,
                    }
                    threading.Thread(
                        target=_safe_write,
                        args=(payload,),
                        daemon=True,
                        name=f"meter-{provider}",
                    ).start()
                except Exception as meter_err:  # noqa: BLE001
                    # Metering itself must never break a search.
                    logger.warning("Metering instrumentation failed: %s", meter_err)

        return wrapped

    return decorator


def _cost_for(provider: str, endpoint: str, credits: int) -> float:
    """
    USD cost for `credits` units of `endpoint`. Most endpoints have a flat
    per-credit rate. Coresignal member_search is special: its credits are all
    Collect credits ($0.08 each), because the Search credit is logged
    separately as a `coresignal/search` row from inside _search_ids.
    """
    if credits <= 0:
        return 0.0
    if provider == "coresignal" and endpoint == "member_search":
        return round(credits * PROVIDER_RATES["coresignal"]["member_collect"], 6)
    rate = PROVIDER_RATES.get(provider, {}).get(endpoint, 0.0)
    return round(credits * rate, 6)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_g(name: str, default=None):
    """Read a value from flask.g if a request/app context exists, else default."""
    if has_request_context() or has_app_context():
        return getattr(g, name, default)
    return default


def new_search_id() -> str:
    """Generate a stable short search_id. Call once per route entry and set on g."""
    return uuid.uuid4().hex[:12]


def attach_request_context(user_id: Optional[str] = None, search_id: Optional[str] = None) -> str:
    """
    Stash user_id + search_id on flask.g so the meter_call decorator can pick
    them up. Returns the search_id (newly minted if not provided).

    Routes call this once at entry:
        from app.services.metering import attach_request_context
        attach_request_context(user_id=request.firebase_user["uid"])
    """
    if not (has_request_context() or has_app_context()):
        return search_id or new_search_id()
    if user_id is not None:
        g.user_id = user_id
    sid = search_id or getattr(g, "search_id", None) or new_search_id()
    g.search_id = sid
    return sid


# ---------------------------------------------------------------------------
# Credit + return-count calculators (centralized so provider clients stay clean)
# ---------------------------------------------------------------------------


def _credits_for(provider: str, endpoint: str, result: Any, kwargs: Dict[str, Any]) -> int:
    """
    Compute how many native provider credits this call consumed.

    Heuristics:
      - PDL Search: bills per result returned (1 credit per record).
      - PDL Enrich: 1 credit on hit (response present), 0 on miss.
      - Coresignal member_search (our public surface): N Collect credits,
        where N = meta.collected_count. Search credits are NOT counted here;
        _search_ids logs them itself as `coresignal/search` rows.
      - Coresignal member_collect: 1 credit per call (its internal
        search-by-URL bills separately, same as above).
      - Hunter / Apify: 1 credit per call (refined in later phases).
    """
    try:
        if provider == "pdl" and endpoint == "person_search":
            # execute_pdl_search returns (raw_contacts, status_code)
            if isinstance(result, tuple) and result and isinstance(result[0], list):
                return len(result[0])
            return 0
        if provider == "pdl" and endpoint == "person_enrich":
            return 1 if result else 0
        if provider == "pdl" and endpoint == "title_enrich":
            # 1 per API call even on a no-match, to match the pool counter:
            # pdl_title_cache calls _increment_usage() unconditionally after
            # the request returns. Keep these two in step or the ledger and
            # the circuit breaker will disagree about how much is left.
            return 1
        if provider == "coresignal" and endpoint == "member_search":
            # Collect credits only. search_contacts_from_prompt fires one
            # search of its own plus (when alumni_school is set) a second one
            # inside alumni_first_ids, so a "1 + collects" count here was both
            # double-counting against the `search` row and still undercounting
            # the alumni search. _search_ids owns every Search credit now.
            # search_contacts_from_prompt returns
            # (contacts, retry_level, saved, meta)
            if isinstance(result, tuple) and len(result) >= 4:
                meta = result[3] or {}
                return int(meta.get("collected_count", 0) or 0)
            return 0
        if provider == "coresignal" and endpoint == "member_collect":
            return 1 if result else 0
        # Default: 1 credit per call. Good enough until refined.
        return 1
    except Exception:
        return 1


def _count_returned(provider: str, endpoint: str, result: Any) -> int:
    """How many records the call returned (for the dashboard's
    avg-records-per-call column)."""
    try:
        if isinstance(result, tuple) and result and isinstance(result[0], list):
            return len(result[0])
        if isinstance(result, list):
            return len(result)
        if isinstance(result, dict):
            return 1
        return 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Firestore write (background thread)
# ---------------------------------------------------------------------------


def _safe_write(payload: Dict[str, Any]) -> None:
    """Background-thread Firestore write. Adds timestamp and never raises."""
    try:
        payload["timestamp"] = datetime.now(timezone.utc)
        db = get_db()
        if db is None:
            logger.debug("get_db() returned None; skipping metering write")
            return
        db.collection("provider_calls").add(payload)
    except Exception as e:  # noqa: BLE001
        logger.warning("provider_calls write failed: %s", e)


# ---------------------------------------------------------------------------
# Admin dashboard helpers (called from admin routes)
# ---------------------------------------------------------------------------


def spend_summary(days: int = 7) -> Dict[str, Any]:
    """
    Aggregate provider_calls for the last `days` days, grouped by provider
    and endpoint. Returns a dict suitable for JSON serialization.
    """
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    db = get_db()
    if db is None:
        return {"error": "firestore_unavailable", "days": days, "by_provider": {}}

    by_provider: Dict[str, Dict[str, Any]] = {}
    total_calls = 0
    total_cost = 0.0
    try:
        docs = db.collection("provider_calls").where("timestamp", ">=", cutoff).stream()
        for doc in docs:
            d = doc.to_dict() or {}
            total_calls += 1
            cost = float(d.get("est_cost_usd") or 0.0)
            total_cost += cost
            prov = d.get("provider", "unknown")
            ep = d.get("endpoint", "unknown")
            p = by_provider.setdefault(prov, {"calls": 0, "credits": 0, "cost_usd": 0.0, "by_endpoint": {}})
            p["calls"] += 1
            p["credits"] += int(d.get("credits_charged") or 0)
            p["cost_usd"] += cost
            e = p["by_endpoint"].setdefault(ep, {"calls": 0, "credits": 0, "cost_usd": 0.0})
            e["calls"] += 1
            e["credits"] += int(d.get("credits_charged") or 0)
            e["cost_usd"] += cost
    except Exception as exc:
        return {"error": f"query_failed: {exc}", "days": days, "by_provider": {}}

    # Round dollars for display
    for p in by_provider.values():
        p["cost_usd"] = round(p["cost_usd"], 4)
        for e in p["by_endpoint"].values():
            e["cost_usd"] = round(e["cost_usd"], 4)

    return {
        "days": days,
        "total_calls": total_calls,
        "total_cost_usd": round(total_cost, 4),
        "by_provider": by_provider,
    }


def spend_by_user(days: int = 7, limit: int = 25) -> Dict[str, Any]:
    """Top users by est_cost_usd over the window."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    db = get_db()
    if db is None:
        return {"error": "firestore_unavailable", "days": days, "users": []}

    by_user: Dict[str, Dict[str, Any]] = {}
    try:
        docs = db.collection("provider_calls").where("timestamp", ">=", cutoff).stream()
        for doc in docs:
            d = doc.to_dict() or {}
            uid = d.get("user_id") or "unknown"
            cost = float(d.get("est_cost_usd") or 0.0)
            u = by_user.setdefault(uid, {"user_id": uid, "calls": 0, "credits": 0, "cost_usd": 0.0})
            u["calls"] += 1
            u["credits"] += int(d.get("credits_charged") or 0)
            u["cost_usd"] += cost
    except Exception as exc:
        return {"error": f"query_failed: {exc}", "days": days, "users": []}

    users = sorted(by_user.values(), key=lambda x: x["cost_usd"], reverse=True)[:limit]
    for u in users:
        u["cost_usd"] = round(u["cost_usd"], 4)
    return {"days": days, "users": users}
