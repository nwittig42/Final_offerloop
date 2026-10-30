"""
Tier-based caps for MCP tool result counts.

`user_ctx` is None for anonymous callers, otherwise a dict with `uid`, `tier`,
and `scope` keys set by flask_mount after JWT verification.

The numbers below mirror Offerloop's website tier caps (config.py +
constants.ts). Anonymous and Free signed-in share the same cap — signing in
as Free identifies the user (so we can attribute usage and let them upgrade
in-place) but does not lift the result count.
"""
from __future__ import annotations

from typing import Optional


def _tier(user_ctx: Optional[dict]) -> str:
    if user_ctx is None:
        return "anonymous"
    return (user_ctx.get("tier") or "free").lower()


def find_contacts_cap(user_ctx: Optional[dict]) -> int:
    tier = _tier(user_ctx)
    if tier == "elite":
        return 15
    if tier == "pro":
        return 10
    return 5  # anonymous + free signed-in


def cap_message(user_ctx: Optional[dict], cap: int) -> str:
    """User-facing note attached to truncated responses."""
    tier = _tier(user_ctx)
    if tier == "anonymous":
        return (
            f"Free tier returns up to {cap} contacts. "
            "Connect your Offerloop account to lift the cap based on your plan."
        )
    if tier == "free":
        return (
            f"Free plan returns up to {cap} contacts. "
            "Upgrade to Pro for 10 per search or Elite for 15."
        )
    if tier == "pro":
        return f"Pro plan returns up to {cap} contacts. Upgrade to Elite for 15."
    return f"Elite plan returns up to {cap} contacts."


# ── Per-day / per-hour CALL caps (separate from per-search contact caps) ────


# tool -> tier -> (day_cap, hour_cap). None = unlimited.
# Numbers chosen so a real recruiting workflow doesn't trip the limiter:
# elite = effectively unlimited, pro = comfortable daily session, free =
# enough to evaluate the product without enabling abuse.
_CALL_LIMITS: dict[str, dict[str, tuple[Optional[int], Optional[int]]]] = {
    "find_contacts": {
        "anonymous": (3, 10),
        "free": (10, 20),
        "pro": (50, 30),
        "elite": (None, 100),
    },
    "draft_outreach": {
        "anonymous": (2, 5),
        "free": (10, 10),
        "pro": (30, 20),
        "elite": (None, 60),
    },
    "get_company_intel": {
        "anonymous": (None, 30),
        "free": (None, 50),
        "pro": (None, 100),
        "elite": (None, 200),
    },
}


def call_limits_for(tool: str, user_ctx: Optional[dict]) -> tuple[Optional[int], Optional[int]]:
    """Return (day_cap, hour_cap) for this tool + tier. Unknown tier → free."""
    tier = _tier(user_ctx)
    by_tier = _CALL_LIMITS.get(tool) or {}
    return by_tier.get(tier) or by_tier.get("free") or (None, None)


def rate_limit_identity(user_ctx: Optional[dict], ip_hash: str) -> str:
    """Per-uid for authed callers, per-IP for the (now-unreachable from MCP
    clients) anonymous path. Bucketing by uid means a user behind a corporate
    NAT isn't sharing a quota with strangers, and limits scale with tier."""
    if user_ctx and user_ctx.get("uid"):
        return f"uid:{user_ctx['uid']}"
    return f"ip:{ip_hash}"
