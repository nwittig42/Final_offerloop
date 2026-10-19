"""
A0: Email baseline measurement.

Aggregates reply data across all users with Gmail integration to establish
a baseline reply rate before/after email personalization changes.

Stores results in Firestore:
  analytics/email_baseline  — overall baseline snapshot
  analytics/email_outcomes  — dimensional breakdown (school, industry, personalization type)
"""
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

from app.extensions import get_db
from app.models.pipeline_stages import AWAITING_REPLY_STAGES
from app.services.outbox_service import REPLIED_STAGES, _parse_iso
from app.utils.industry_classifier import classify_industry
from app.utils.users import get_user_school

logger = logging.getLogger(__name__)

# Stages where the user sent an email and could receive a reply
ELIGIBLE_STAGES = AWAITING_REPLY_STAGES | frozenset({
    "replied", "meeting_scheduled", "connected", "no_response",
})


# Keep the old name as an alias so existing tests can still import it.
_classify_industry = classify_industry


def _make_segment_key(dimension: str, value: str) -> str:
    """Normalize a dimension value into a Firestore-safe key."""
    return f"{dimension}:{(value or 'unknown').strip().lower()[:60]}"


def compute_email_baseline():
    """
    Iterate all users with Gmail integration, aggregate contact-level
    email metrics, and store the baseline in Firestore.

    Returns the baseline dict that was written.
    """
    db = get_db()
    now = datetime.now(timezone.utc)

    # Per-user accumulators
    users_sampled = 0
    users_with_emails = 0

    # Global accumulators
    total_contacts_emailed = 0
    total_replied = 0
    total_eligible = 0
    all_response_hours = []
    total_meetings = 0
    total_replied_denom = 0  # replied + meeting_scheduled + connected

    # Per-tier breakdown
    tier_stats = {}

    # Dimensional breakdowns for analytics/email_outcomes
    # Key: segment string (e.g., "school:usc", "industry:consulting")
    # Value: { "totalSent": int, "replyCount": int, "responseTimes": [float] }
    segment_stats = defaultdict(lambda: {
        "totalSent": 0, "replyCount": 0, "responseTimes": [],
    })

    for user_doc in db.collection("users").stream():
        uid = user_doc.id
        user_data = user_doc.to_dict() or {}

        # Only include users with Gmail connected (reply tracking requires it)
        gmail_ref = (
            db.collection("users").document(uid)
            .collection("integrations").document("gmail")
        )
        gmail_doc = gmail_ref.get()
        if not gmail_doc.exists:
            continue
        gmail_data = gmail_doc.to_dict() or {}
        if not (gmail_data.get("token") or gmail_data.get("refresh_token")):
            continue

        users_sampled += 1
        tier = user_data.get("subscriptionTier", user_data.get("tier", "free"))
        user_school = get_user_school(user_data)

        # Query ALL contacts for this user
        contacts_ref = db.collection("users").document(uid).collection("contacts")
        contacts = list(contacts_ref.stream())

        user_eligible = 0
        user_replied = 0
        user_meetings = 0
        user_replied_denom = 0

        for doc in contacts:
            data = doc.to_dict() or {}
            stage = data.get("pipelineStage") or ""
            sent_at = _parse_iso(data.get("emailGeneratedAt") or data.get("emailSentAt"))

            # Only count contacts where an email was actually sent
            if not sent_at:
                continue

            total_contacts_emailed += 1

            # Extract dimensional data from contact
            contact_company = data.get("company") or ""
            contact_title = data.get("jobTitle") or ""
            contact_school = data.get("college") or ""
            personalization_type = data.get("personalizationType") or "none"
            industry = _classify_industry(contact_company, contact_title)

            # Determine if replied
            is_replied = stage in REPLIED_STAGES
            response_hours = None

            if stage in ELIGIBLE_STAGES:
                total_eligible += 1
                user_eligible += 1

            if is_replied:
                total_replied += 1
                user_replied += 1
                total_replied_denom += 1
                user_replied_denom += 1
                if stage in ("meeting_scheduled", "connected"):
                    total_meetings += 1
                    user_meetings += 1

                # Response time
                reply_at = _parse_iso(
                    data.get("replyReceivedAt") or data.get("lastActivityAt")
                )
                if reply_at and reply_at >= sent_at:
                    response_hours = (reply_at - sent_at).total_seconds() / 3600.0
                    all_response_hours.append(response_hours)

            # Accumulate dimensional segments
            segments = [
                _make_segment_key("industry", industry),
                _make_segment_key("personalization", personalization_type),
            ]
            if contact_school:
                segments.append(_make_segment_key("contact_school", contact_school))
            if user_school:
                segments.append(_make_segment_key("user_school", user_school))

            for seg in segments:
                segment_stats[seg]["totalSent"] += 1
                if is_replied:
                    segment_stats[seg]["replyCount"] += 1
                if response_hours is not None:
                    segment_stats[seg]["responseTimes"].append(response_hours)

        if user_eligible > 0:
            users_with_emails += 1

        # Accumulate per-tier
        if tier not in tier_stats:
            tier_stats[tier] = {
                "users": 0,
                "eligible": 0,
                "replied": 0,
                "meetings": 0,
            }
        tier_stats[tier]["users"] += 1
        tier_stats[tier]["eligible"] += user_eligible
        tier_stats[tier]["replied"] += user_replied
        tier_stats[tier]["meetings"] += user_meetings

    # Compute aggregate rates
    reply_rate = (total_replied / total_eligible) if total_eligible else 0.0
    avg_response_hours = (
        round(sum(all_response_hours) / len(all_response_hours), 1)
        if all_response_hours
        else None
    )
    meeting_rate = (
        (total_meetings / total_replied_denom) if total_replied_denom else 0.0
    )

    # Per-tier rates
    tier_breakdown = {}
    for tier, stats in tier_stats.items():
        tier_breakdown[tier] = {
            "users": stats["users"],
            "contactsEligible": stats["eligible"],
            "contactsReplied": stats["replied"],
            "replyRate": round(
                stats["replied"] / stats["eligible"], 4
            ) if stats["eligible"] else 0.0,
            "meetings": stats["meetings"],
        }

    baseline = {
        "measuredAt": now.isoformat().replace("+00:00", "Z"),
        "sampleSize": {
            "usersWithGmail": users_sampled,
            "usersWithEmails": users_with_emails,
            "totalContactsEmailed": total_contacts_emailed,
            "totalEligible": total_eligible,
        },
        "metrics": {
            "replyRate": round(reply_rate, 4),
            "avgResponseTimeHours": avg_response_hours,
            "meetingRate": round(meeting_rate, 4),
            "totalReplied": total_replied,
            "responseTimeSampleSize": len(all_response_hours),
        },
        "tierBreakdown": tier_breakdown,
        "bias": (
            "Only users with Gmail connected have reply tracking. "
            "This biases toward more engaged users."
        ),
    }

    # Write baseline to Firestore
    db.collection("analytics").document("email_baseline").set(baseline)

    # Write dimensional breakdowns to analytics/email_outcomes
    outcomes = {}
    for seg_key, stats in segment_stats.items():
        avg_resp = None
        if stats["responseTimes"]:
            avg_resp = round(
                sum(stats["responseTimes"]) / len(stats["responseTimes"]), 1
            )
        outcomes[seg_key] = {
            "totalSent": stats["totalSent"],
            "replyCount": stats["replyCount"],
            "replyRate": round(
                stats["replyCount"] / stats["totalSent"], 4
            ) if stats["totalSent"] else 0.0,
            "avgResponseTimeHours": avg_resp,
            "lastUpdated": now.isoformat().replace("+00:00", "Z"),
        }

    db.collection("analytics").document("email_outcomes").set({
        "measuredAt": now.isoformat().replace("+00:00", "Z"),
        "segments": outcomes,
        "segmentCount": len(outcomes),
    })

    logger.info(
        "Email baseline stored: %d users, %d eligible, %.2f%% reply rate, %d segments",
        users_sampled,
        total_eligible,
        reply_rate * 100,
        len(outcomes),
    )

    return baseline


# ---------------------------------------------------------------------------
# Scanner entry point (Tracker daemon contract)
# ---------------------------------------------------------------------------
#
# Contract: docs/designs/tracker-daemon-contract.md
#
# Dispatch cadence: every 6 hours invocation, but internal gate — returns
# early unless today is Sunday AND current UTC hour is in [3, 9) AND the
# last successful run was more than 6 days ago. Low-traffic Sunday window.
#
# Health doc: `system/aggregation_scanner` with fields lastSuccessAt,
# lastDurationMs, contactsScanned, segmentsWritten, docSizeBytes, errorCount.
# The watchdog alerts if lastSuccessAt is older than 8 days.

# Gate constants — exposed so tests can assert against the contract.
AGGREGATION_SUNDAY_WEEKDAY = 6     # datetime.weekday(): Sun=6
AGGREGATION_HOUR_START = 3         # UTC hour window [3, 9)
AGGREGATION_HOUR_END = 9
AGGREGATION_STALENESS_SECONDS = 6 * 24 * 3600  # 6 days


def _should_run_aggregation_scanner(db, now: "datetime | None" = None) -> bool:
    """
    Return True if the aggregation scanner should execute this tick.

    Rule (per daemon contract): run iff ALL three are true —
      1. today is Sunday UTC,
      2. current UTC hour is in [3, 9),
      3. last successful run was more than 6 days ago.

    The Sunday 3-9am UTC window is low-traffic in both US and EU zones;
    the 6-day freshness check prevents the scanner from running twice in
    the same 6-hour dispatch window if a tick slides a few hours late.
    """
    now = now or datetime.now(timezone.utc)
    if now.weekday() != AGGREGATION_SUNDAY_WEEKDAY:
        return False
    if not (AGGREGATION_HOUR_START <= now.hour < AGGREGATION_HOUR_END):
        return False

    try:
        doc = db.collection("system").document("aggregation_scanner").get()
        if not doc.exists:
            # First-ever Sunday in the window — run.
            return True
        data = doc.to_dict() or {}
        last_success = data.get("lastSuccessAt")
        if not last_success:
            return True
        last_dt = datetime.fromisoformat(
            str(last_success).replace("Z", "+00:00")
        )
        age = (now - last_dt).total_seconds()
        return age > AGGREGATION_STALENESS_SECONDS
    except Exception as exc:
        logger.warning("aggregation_scanner: gate check failed, skipping: %s", exc)
        return False


def _write_aggregation_scanner_health(
    db,
    *,
    contacts_scanned: int,
    segments_written: int,
    doc_size_bytes: int,
    error_count: int,
    duration_ms: int,
) -> None:
    """Write the health doc consumed by the wsgi.py watchdog."""
    try:
        db.collection("system").document("aggregation_scanner").set({
            "lastSuccessAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "lastDurationMs": int(duration_ms),
            "contactsScanned": int(contacts_scanned),
            "segmentsWritten": int(segments_written),
            "docSizeBytes": int(doc_size_bytes),
            "errorCount": int(error_count),
        })
    except Exception as exc:
        logger.warning("aggregation_scanner: health doc write failed: %s", exc)


def aggregate_email_outcomes() -> None:
    """
    Scanner entry point invoked by the tracker daemon loop every 6 hours.

    Gates on the Sunday 3-9am UTC window and 6-day freshness, then delegates
    to `compute_email_baseline()` which does the actual aggregation across
    all users. Wraps the result in the daemon-contract health doc format.
    """
    db = get_db()
    if db is None:
        logger.warning("aggregation_scanner: no db client, skipping run")
        return

    if not _should_run_aggregation_scanner(db):
        logger.info("aggregation_scanner: gate closed (not Sun 3-9am UTC or ran recently)")
        return

    started = time.time()
    error_count = 0
    contacts_scanned = 0
    segments_written = 0
    doc_size_bytes = 0

    logger.info("aggregation_scanner: starting aggregation")

    try:
        baseline = compute_email_baseline() or {}
        contacts_scanned = int(
            (baseline.get("sampleSize") or {}).get("totalContactsEmailed", 0)
        )
        # Read back the segments doc to count and size it. Reading is cheaper
        # than threading segmentCount/docSizeBytes through compute_email_baseline.
        try:
            outcomes_doc = db.collection("analytics").document("email_outcomes").get()
            if outcomes_doc.exists:
                outcomes_data = outcomes_doc.to_dict() or {}
                segments_written = int(outcomes_data.get("segmentCount", 0))
                # Rough size estimate via JSON serialization — Firestore doc
                # limit is 1MB; the contract warns at 800KB / fails at 950KB.
                try:
                    doc_size_bytes = len(json.dumps(outcomes_data, default=str))
                except Exception:
                    doc_size_bytes = 0
        except Exception as read_exc:
            logger.warning(
                "aggregation_scanner: could not read email_outcomes for metrics: %s",
                read_exc,
            )
    except Exception:
        error_count += 1
        logger.exception("aggregation_scanner: compute_email_baseline failed")

    duration_ms = int((time.time() - started) * 1000)
    _write_aggregation_scanner_health(
        db,
        contacts_scanned=contacts_scanned,
        segments_written=segments_written,
        doc_size_bytes=doc_size_bytes,
        error_count=error_count,
        duration_ms=duration_ms,
    )
    logger.info(
        "aggregation_scanner: done contacts=%d segments=%d doc_bytes=%d errors=%d duration_ms=%d",
        contacts_scanned, segments_written, doc_size_bytes, error_count, duration_ms,
    )
