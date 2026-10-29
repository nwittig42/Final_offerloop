"""
Provider-spend alerting — the cost safeguard.

Totals actual $ spend across every metered provider for two windows:
  - today        (since 00:00 UTC)
  - month-to-date (since the 1st, 00:00 UTC)  ← matches our calendar-month billing

Spend comes from two Firestore collections that already log real, computed cost:
  - `provider_calls`  — PDL / Hunter / Coresignal / Apify + (new) OpenAI / Claude
  - `scout_metrics`   — Scout's own per-turn OpenAI cost

When today's or the month's spend crosses a configured budget threshold
(50% / 80% / 100%), email the founders. Each threshold fires at most once per
period (deduped via the `system/spend_alert_state` doc) so a cron hitting this
every few hours never spams.

Email is the primary channel — it reuses the existing Resend pipeline
(notification_adapter) and goes to SPEND_ALERT_EMAILS (default support@offerloop.ai,
which fans out to the whole team). Telegram is kept only as an optional silent
backup, fired solely when TELEGRAM_CHAT_ID is configured.

This module NEVER raises — a monitoring failure must not take anything down.

ENV
  SPEND_DAILY_ALERT_USD     daily budget (alert at 50/80/100%). 0/unset = off.
  SPEND_MONTHLY_ALERT_USD   month-to-date budget. 0/unset = off.
  SPEND_ALERT_EMAILS        comma-separated recipients (default support@offerloop.ai).
  TELEGRAM_BOT_TOKEN        optional backup; reused from the reddit scanner.
  TELEGRAM_CHAT_ID          optional backup; if set, also pings Telegram.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("spend_alerts")

# Threshold fractions of the budget at which we alert, ascending.
THRESHOLDS: List[float] = [0.5, 0.8, 1.0]
_STATE_DOC = ("system", "spend_alert_state")


# ---------------------------------------------------------------------------
# Cost aggregation
# ---------------------------------------------------------------------------


def _db():
    try:
        from app.extensions import get_db
        return get_db()
    except Exception:  # noqa: BLE001
        return None


def _sum_cost(db, collection: str, ts_field: str, since: datetime) -> Dict[str, Any]:
    """Sum est_cost_usd/cost_usd over `collection` since `since`, grouped by provider."""
    total = 0.0
    by_provider: Dict[str, float] = {}
    try:
        docs = db.collection(collection).where(ts_field, ">=", since).stream()
        for doc in docs:
            d = doc.to_dict() or {}
            cost = float(d.get("est_cost_usd") or d.get("cost_usd") or 0.0)
            total += cost
            prov = d.get("provider") or ("scout" if collection == "scout_metrics" else "unknown")
            by_provider[prov] = by_provider.get(prov, 0.0) + cost
    except Exception as e:  # noqa: BLE001
        logger.warning("spend sum failed for %s: %s", collection, e)
    return {"total": total, "by_provider": by_provider}


def _window_spend(db, since: datetime) -> Dict[str, Any]:
    """Combined spend across provider_calls + scout_metrics since `since`."""
    pc = _sum_cost(db, "provider_calls", "timestamp", since)
    sc = _sum_cost(db, "scout_metrics", "created_at", since)
    by_provider = dict(pc["by_provider"])
    for prov, cost in sc["by_provider"].items():
        by_provider[prov] = by_provider.get(prov, 0.0) + cost
    return {
        "total": round(pc["total"] + sc["total"], 4),
        "by_provider": {k: round(v, 4) for k, v in sorted(by_provider.items(), key=lambda x: -x[1])},
    }


def compute_spend() -> Dict[str, Any]:
    """Today + month-to-date spend. Returns zeros if Firestore is unavailable."""
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    db = _db()
    if db is None:
        return {"error": "firestore_unavailable", "today": {"total": 0.0}, "mtd": {"total": 0.0}}
    return {
        "as_of": now.isoformat(),
        "today": _window_spend(db, day_start),
        "mtd": _window_spend(db, month_start),
    }


# ---------------------------------------------------------------------------
# Threshold + dedup logic
# ---------------------------------------------------------------------------


def _crossed(spend: float, budget: float) -> Optional[float]:
    """Highest threshold fraction crossed, or None. budget<=0 disables."""
    if budget <= 0 or spend <= 0:
        return None
    frac = spend / budget
    hit = [t for t in THRESHOLDS if frac >= t]
    return max(hit) if hit else None


def _read_state(db) -> Dict[str, Any]:
    try:
        snap = db.collection(_STATE_DOC[0]).document(_STATE_DOC[1]).get()
        return snap.to_dict() or {} if snap.exists else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("spend_alert_state read failed: %s", e)
        return {}


def _write_state(db, state: Dict[str, Any]) -> None:
    try:
        db.collection(_STATE_DOC[0]).document(_STATE_DOC[1]).set(state, merge=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("spend_alert_state write failed: %s", e)


# ---------------------------------------------------------------------------
# Alert dispatch — email primary (Resend), Telegram optional backup
# ---------------------------------------------------------------------------


def _recipients() -> List[str]:
    raw = os.getenv("SPEND_ALERT_EMAILS", "support@offerloop.ai")
    return [e.strip() for e in raw.split(",") if e.strip()]


def send_telegram(message: str) -> bool:
    """Optional backup channel. Only fires if a Telegram chat is configured."""
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    try:
        import requests
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=15,
        )
        return resp.ok
    except Exception as e:  # noqa: BLE001
        logger.warning("telegram send failed: %s", e)
        return False


def _dispatch_alert(subject: str, html_body: str, text_body: str) -> bool:
    """Email every recipient via the existing Resend pipeline; also ping
    Telegram if (and only if) a backup chat is configured. Best-effort."""
    sent_any = False
    try:
        from app.services.notification_adapter import send, Channel
        for rcpt in _recipients():
            try:
                res = send(Channel.EMAIL, rcpt, subject, html_body, text_body)
                sent_any = bool(getattr(res, "success", False)) or sent_any
            except Exception as e:  # noqa: BLE001
                logger.warning("spend alert email to %s failed: %s", rcpt, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("spend alert email dispatch failed: %s", e)

    if os.getenv("TELEGRAM_CHAT_ID"):
        sent_any = send_telegram(text_body) or sent_any

    if not sent_any:
        logger.warning("spend alert had no working channel — alert:\n%s", text_body)
    return sent_any


def _format_alert(window: str, period_key: str, frac: float, spend: float,
                  budget: float, by_provider: Dict[str, float]):
    """Return (subject, html_body, text_body) for one alert."""
    pct = int(round(frac * 100))
    siren = "🔴" if frac >= 1.0 else ("🟠" if frac >= 0.8 else "🟡")
    subject = f"{siren} Offerloop spend alert — {window} at {pct}% of budget (${spend:,.2f}/${budget:,.2f})"

    rows = [(prov, cost) for prov, cost in list(by_provider.items())[:8] if cost > 0]
    text_lines = [
        subject,
        f"${spend:,.2f} / ${budget:,.2f}  ({period_key})",
        "",
        "By provider:",
    ] + [f"  - {prov}: ${cost:,.2f}" for prov, cost in rows]
    text_body = "\n".join(text_lines)

    html_rows = "".join(f"<li>{prov}: ${cost:,.2f}</li>" for prov, cost in rows)
    html_body = (
        f"<h2>{siren} Offerloop spend alert</h2>"
        f"<p><b>{window}</b> spend has reached <b>{pct}%</b> of budget "
        f"(<b>${spend:,.2f}</b> / ${budget:,.2f}) for {period_key}.</p>"
        f"<p>By provider:</p><ul>{html_rows}</ul>"
        f"<p style='color:#888;font-size:12px'>Automated by the spend monitor. "
        f"Live numbers: GET /api/admin/spend-check?dry=1</p>"
    )
    return subject, html_body, text_body


# ---------------------------------------------------------------------------
# Entry point (called by the admin endpoint / cron)
# ---------------------------------------------------------------------------


def check_and_alert(force: bool = False) -> Dict[str, Any]:
    """
    Compute spend, fire Telegram for any newly-crossed threshold, dedup via
    Firestore state. `force=True` re-sends the current highest threshold even
    if already alerted this period (for manual testing). Returns a summary.
    """
    daily_budget = float(os.getenv("SPEND_DAILY_ALERT_USD") or 0)
    monthly_budget = float(os.getenv("SPEND_MONTHLY_ALERT_USD") or 0)
    spend = compute_spend()
    if spend.get("error"):
        return {"ok": False, **spend}

    db = _db()
    state = _read_state(db) if db is not None else {}
    now = datetime.now(timezone.utc)
    fired: List[Dict[str, Any]] = []

    checks = [
        ("daily", now.strftime("%Y-%m-%d"), spend["today"]["total"], daily_budget,
         spend["today"]["by_provider"], "Today"),
        ("monthly", now.strftime("%Y-%m"), spend["mtd"]["total"], monthly_budget,
         spend["mtd"]["by_provider"], "Month-to-date"),
    ]

    for kind, period_key, total, budget, by_prov, label in checks:
        frac = _crossed(total, budget)
        if frac is None:
            continue
        # Dedup: only alert if this is a higher threshold than last alerted
        # for THIS period (reset automatically when period_key changes).
        st = state.get(kind) or {}
        already = st.get("level", 0.0) if st.get("period") == period_key else 0.0
        if frac > already or force:
            subject, html_body, text_body = _format_alert(label, period_key, frac, total, budget, by_prov)
            sent = _dispatch_alert(subject, html_body, text_body)
            fired.append({"kind": kind, "level": frac, "spend": total, "budget": budget, "sent": sent})
            state[kind] = {"period": period_key, "level": frac, "alerted_at": now.isoformat()}

    # Vendor tank gauges (2026-08-25). The dollar windows above read
    # provider_calls; these read the ENFORCEMENT docs the clients actually
    # stop on, so "80% of the tank is gone" reaches an inbox even when a
    # metering row was missed. The Coresignal tank was removed 2026-10-29
    # when the vendor was canceled; the gauge list is now FullEnrich-only
    # until PDL retires.
    if db is not None:
        # PDL's title-enrichment pool. Budget is the CIRCUIT BREAKER (45k of
        # the 50k pool), not the pool itself, because the breaker is where
        # enrichment actually stops — matching "the docs the clients stop on".
        # Until 2026-10-22 this pool had no gauge and enrich_job_title_with_pdl
        # had no @meter_call, so 17k credits of burn were invisible to both the
        # dollar windows and these gauges. It is also the one tank that fails
        # silently: when it trips, job titles just stop being standardised.
        try:
            from app.services.pdl_title_cache import TOTAL_BUDGET_CIRCUIT_BREAKER
            _pdl_budget = float(TOTAL_BUDGET_CIRCUIT_BREAKER)
        except Exception:  # noqa: BLE001
            _pdl_budget = 45_000.0

        # (kind, doc_path, budget, period_key, label, spend_field)
        gauges = [
            ("fullenrich_tank", ("meta", "fullenrichBudget"),
             float(os.getenv("FULLENRICH_BUDGET") or 450),
             now.strftime("%Y-%m"), "FullEnrich monthly credits", "spent_estimate"),
            ("pdl_title_tank", ("pdl_usage", "title_enrich"),
             _pdl_budget, "pool", "PDL title-enrichment pool", "credits_used"),
        ]
        for kind, doc_path, budget, period_key, label, spend_field in gauges:
            try:
                snap = db.collection(doc_path[0]).document(doc_path[1]).get()
                doc = (snap.to_dict() or {}) if snap.exists else {}
                tank_spent = float(doc.get(spend_field, 0.0) or 0.0)
                # The FullEnrich doc is month-stamped; an older month means
                # the counter has effectively reset to zero.
                if kind == "fullenrich_tank" and doc.get("month") not in (None, period_key):
                    tank_spent = 0.0
            except Exception:  # noqa: BLE001
                logger.warning("tank gauge read failed for %s", kind)
                continue
            frac = _crossed(tank_spent, budget)
            if frac is None:
                continue
            st = state.get(kind) or {}
            already = st.get("level", 0.0) if st.get("period") == period_key else 0.0
            if frac > already or force:
                pct = int(round(frac * 100))
                siren = "🔴" if frac >= 1.0 else ("🟠" if frac >= 0.8 else "🟡")
                subject = (f"{siren} Offerloop tank alert — {label} at {pct}% "
                           f"({tank_spent:,.0f} of {budget:,.0f} credits)")
                text_body = (f"{subject}\n\nAt 100% this vendor stops serving and the app "
                             f"degrades to cached data. Decide on the next tier before the wall, "
                             f"not after students hit it.")
                html_body = (f"<h2>{subject}</h2><p>At 100% this vendor stops serving and the "
                             f"app degrades to cached data. Decide on the next tier before the "
                             f"wall, not after students hit it.</p>")
                sent = _dispatch_alert(subject, html_body, text_body)
                fired.append({"kind": kind, "level": frac, "spend": tank_spent,
                              "budget": budget, "sent": sent})
                state[kind] = {"period": period_key, "level": frac, "alerted_at": now.isoformat()}

    if fired and db is not None:
        _write_state(db, state)

    return {
        "ok": True,
        "as_of": spend.get("as_of"),
        "today": spend["today"]["total"],
        "mtd": spend["mtd"]["total"],
        "daily_budget": daily_budget,
        "monthly_budget": monthly_budget,
        "alerts_fired": fired,
    }
