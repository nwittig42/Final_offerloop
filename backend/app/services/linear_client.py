"""
Linear issue creation + reply sync for in-app feedback (Rylan 2026-09-22).

Feedback already lands in Firestore and pings founders' phones
(routes/mobile.py:submit_feedback); this adds a THIRD, purely additive
best-effort step that files the same message as a Linear issue so it shows
up in the team's actual work queue instead of only a push notification.

Linear's GraphQL API (docs.linear.app/api) needs a teamId (a UUID), not the
short key shown in issue numbers (e.g. "OFF"). We only have the key, so the
team's id is resolved once via a lookup query and cached in-process; it
never changes for a given key. Auth is a personal API key, sent as a raw
Authorization header (no "Bearer" prefix, unlike most APIs, per Linear's docs).

Reply direction (Linear -> user) goes through a webhook on new comments
(routes/mobile.py:linear_webhook_receive). A comment only reaches the user
if it starts with REPLY_PREFIX; internal triage chatter between teammates on
the same issue never syncs, by construction (default is "never sends",
opt-in per comment, not opt-out) - Rylan 2026-09-22, after flagging the risk
of internal comments accidentally reaching a user.
"""
import hashlib
import hmac
import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

API_URL = "https://api.linear.app/graphql"
TEAM_KEY = os.getenv("LINEAR_TEAM_KEY", "OFF")
FEEDBACK_LABEL = "feedback"
REPLY_PREFIX = "TO USER:"

REPLY_INSTRUCTIONS = (
    f'To reply to {{name}}, comment on this issue starting with "{REPLY_PREFIX}" '
    f"(e.g. \"{REPLY_PREFIX} thanks for flagging this, fixed in today's build\"). "
    "That comment is sent to them in the app. Any other comment here is internal "
    "only and is never shown to them."
)

_team_id_cache: Optional[str] = None
_label_id_cache: Optional[str] = None


def _key() -> str:
    return (os.getenv("LINEAR_API_KEY") or "").strip()


def enabled() -> bool:
    return bool(_key())


def _headers() -> dict:
    return {"Authorization": _key(), "Content-Type": "application/json"}


def _query(query: str, variables: dict) -> dict:
    r = requests.post(API_URL, json={"query": query, "variables": variables},
                       headers=_headers(), timeout=10)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(str(body["errors"]))
    return body["data"]


def _team_id() -> Optional[str]:
    global _team_id_cache
    if _team_id_cache:
        return _team_id_cache
    data = _query(
        "query($key: String!) { teams(filter: { key: { eq: $key } }) { nodes { id } } }",
        {"key": TEAM_KEY},
    )
    nodes = (data.get("teams") or {}).get("nodes") or []
    if not nodes:
        logger.error("[linear] no team found for key=%s", TEAM_KEY)
        return None
    _team_id_cache = nodes[0]["id"]
    return _team_id_cache


def _label_id(team_id: str) -> Optional[str]:
    """Finds the team's "feedback" label, creating it once if it doesn't
    exist yet, so every issue this files is filterable in one saved view
    regardless of which workflow state (Triage/Backlog) it lands in."""
    global _label_id_cache
    if _label_id_cache:
        return _label_id_cache
    data = _query(
        "query($teamId: ID!, $name: String!) {"
        " issueLabels(filter: { team: { id: { eq: $teamId } }, name: { eq: $name } }) {"
        " nodes { id } } }",
        {"teamId": team_id, "name": FEEDBACK_LABEL},
    )
    nodes = (data.get("issueLabels") or {}).get("nodes") or []
    if nodes:
        _label_id_cache = nodes[0]["id"]
        return _label_id_cache
    data = _query(
        "mutation($input: IssueLabelCreateInput!) {"
        " issueLabelCreate(input: $input) { issueLabel { id } } }",
        {"input": {"teamId": team_id, "name": FEEDBACK_LABEL}},
    )
    label = (data.get("issueLabelCreate") or {}).get("issueLabel") or {}
    _label_id_cache = label.get("id")
    return _label_id_cache


def _webhook_secret() -> str:
    return (os.getenv("LINEAR_WEBHOOK_SECRET") or "").strip()


def webhook_enabled() -> bool:
    return bool(_webhook_secret())


def verify_webhook_signature(raw_body: bytes, signature: Optional[str]) -> bool:
    """Linear signs webhook deliveries as hex HMAC-SHA256 of the raw request
    body, header `Linear-Signature` (docs.linear.app/api/webhooks). Must run
    on the raw bytes, not a re-serialized/parsed version, since re-encoding
    JSON is not guaranteed to reproduce the exact bytes that were signed."""
    secret = _webhook_secret()
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def extract_reply(comment_body: str) -> Optional[str]:
    """Returns the text to send the user, or None if this comment isn't a
    reply (i.e. wasn't opted in with REPLY_PREFIX) - internal triage
    comments on the same issue must never reach a user by default."""
    body = (comment_body or "").strip()
    if not body.upper().startswith(REPLY_PREFIX.upper()):
        return None
    reply = body[len(REPLY_PREFIX):].strip()
    return reply or None


def create_issue(title: str, description: str) -> Optional[dict]:
    """Files one issue, returns {"identifier": "OFF-123", "id": "<uuid>"} or
    None on any failure. Never raises; callers treat this as fire-and-forget.
    Both fields are returned (not just the display identifier) because the
    reply webhook's payload shape isn't guaranteed to include one over the
    other, so callers should store both and match on whichever arrives."""
    if not enabled():
        return None
    try:
        team_id = _team_id()
        if not team_id:
            return None
        input_: dict = {"teamId": team_id, "title": title, "description": description}
        try:
            label_id = _label_id(team_id)
            if label_id:
                input_["labelIds"] = [label_id]
        except Exception:
            logger.exception("[linear] feedback label lookup/create failed, filing unlabeled")
        data = _query(
            """
            mutation($input: IssueCreateInput!) {
              issueCreate(input: $input) {
                success
                issue { id identifier }
              }
            }
            """,
            {"input": input_},
        )
        issue = (data.get("issueCreate") or {}).get("issue") or {}
        if not issue.get("identifier"):
            return None
        return {"identifier": issue["identifier"], "id": issue.get("id")}
    except Exception:
        logger.exception("[linear] create_issue failed")
        return None
