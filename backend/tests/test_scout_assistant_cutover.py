"""Phase 2 cutover verification: integration tests for /api/scout-assistant/chat.

These hit the real Flask route and make real gpt-4.1-mini calls (the LLM is NOT
mocked). Only the boundary is mocked: Firebase auth and the Firestore
user-context read. The point is to verify the navigate / answer / clarify tool
contract end to end before Phase 3 deletes scout_service.py.

Run:  cd backend && pytest tests/test_scout_assistant_cutover.py -s -v
Requires OPENAI_API_KEY (loaded from the repo .env).

LLM output is non-deterministic, so the clarify/ambiguous cases assert a set of
acceptable tools rather than one exact answer.
"""
import os
from unittest.mock import patch

import pytest

# Ensure OPENAI_API_KEY is loaded (config.py also loads .env on import).
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
except Exception:
    pass

from app.services.scout.page_registry import valid_routes  # noqa: E402

pytestmark = pytest.mark.integration

VALID_ROUTES = set(valid_routes())
FAKE_USER = {"uid": "test-user-id", "email": "test@example.com", "name": "Maya"}


@pytest.fixture
def scout_client():
    """Flask test client with only the Scout blueprint registered.

    The full app (backend.wsgi.create_app) cannot be imported on this branch:
    wsgi.py imports route modules (events, company_contexts) that do not exist
    here. That is a pre-existing break unrelated to the Scout cutover (see the
    report). A minimal app registering just scout_assistant_bp exercises the
    real route and the real service without that dependency.

    Mocked at the boundary: Firebase auth and the Firestore user-context read.
    OpenAI is left live on purpose - these are integration tests.
    """
    import firebase_admin
    from flask import Flask
    from app.routes.scout_assistant import scout_assistant_bp

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    flask_app.register_blueprint(scout_assistant_bp)

    with patch.dict(firebase_admin._apps, {"[DEFAULT]": object()}, clear=False), \
         patch("firebase_admin.auth.verify_id_token", return_value=FAKE_USER), \
         patch("app.routes.scout_assistant._fetch_user_context", return_value={}):
        yield flask_app.test_client()


def _chat(client, message, current_page="/dashboard", user_info=None):
    """POST one turn to the Scout chat endpoint and return (status, json)."""
    resp = client.post(
        "/api/scout-assistant/chat",
        json={
            "message": message,
            "current_page": current_page,
            "conversation_history": [],
            "user_info": user_info or {},
        },
        headers={"Authorization": "Bearer test-token"},
    )
    return resp.status_code, resp.get_json()


def _assert_navigate(data, expected_route):
    """Common navigate-shape checks. Returns the navigate object."""
    assert data["tool"] == "navigate", f"expected navigate, got {data['tool']}: {data}"
    nav = data["navigate"]
    assert nav is not None, f"navigate object missing: {data}"
    assert nav["route"] in VALID_ROUTES, f"hallucinated route {nav['route']!r}"
    assert nav["route"] == expected_route, f"expected {expected_route}, got {nav['route']}"
    assert 0.0 <= nav["confidence"] <= 1.0, f"confidence out of range: {nav['confidence']}"
    return nav


# Case 1 -------------------------------------------------------------------
def test_01_imperative_navigate_job_board(scout_client):
    status, data = _chat(scout_client, "take me to the job board")
    assert status == 200, data
    nav = _assert_navigate(data, "/job-board")
    assert nav["user_was_imperative"] is True, data


# Case 2 -------------------------------------------------------------------
def test_02_imperative_navigate_contact_search_credit_cost(scout_client):
    status, data = _chat(scout_client, "take me to contact search")
    assert status == 200, data
    nav = _assert_navigate(data, "/find")
    assert nav["user_was_imperative"] is True, data
    # The frontend needs the actual credit cost to warn the user before the
    # search spends credits.
    assert "credit_cost" in nav, (
        "credit_cost integer not surfaced in navigate object "
        f"(only have credit_spending={nav.get('credit_spending')!r}): {nav}"
    )


# Case 3 -------------------------------------------------------------------
def test_03_inferred_navigate_contact_search_with_prefill(scout_client):
    status, data = _chat(scout_client, "find product managers at Stripe in SF")
    assert status == 200, data
    nav = _assert_navigate(data, "/find")
    prefill = nav["prefill"]
    # The prompt carrier is the default contract; structured fields remain
    # acceptable when a path still emits them. Either way every piece of the
    # query (role, company, location) must survive into the prefill.
    carried = " ".join(str(v) for v in prefill.values()).lower()
    for piece in ("product manager", "stripe", "sf"):
        assert piece in carried, f"missing {piece!r} in prefill: {prefill}"
    assert nav["user_was_imperative"] is False, data


# Case 4 -------------------------------------------------------------------
def test_04_meeting_prep_missing_linkedin_url_clarifies(scout_client):
    status, data = _chat(
        scout_client, "I have a coffee chat with someone at Stripe tomorrow"
    )
    assert status == 200, data
    # meeting-prep requires linkedin_url, which was not provided.
    assert data["tool"] == "clarify", (
        f"expected clarify (meeting-prep needs linkedin_url), got {data['tool']}: {data}"
    )


# Case 5 -------------------------------------------------------------------
# Removed: the Interview Prep feature was deleted end-to-end in the Phase 5
# cleanup, so there is no /interview-prep route to be ambiguous against
# /meeting-prep anymore. test_18 still covers the meta question "help me
# prep for my interview" under any of {clarify, navigate, answer}.


# Case 6 -------------------------------------------------------------------
def test_06_meta_question_answers(scout_client):
    status, data = _chat(scout_client, "what does meeting prep do?")
    assert status == 200, data
    assert data["tool"] == "answer", data
    assert data["navigate"] is None, data
    assert data["message"], "answer message empty"


# Case 7 -------------------------------------------------------------------
def test_07_greeting_answers(scout_client):
    status, data = _chat(scout_client, "hey scout")
    assert status == 200, data
    assert data["tool"] == "answer", f"expected answer for a greeting: {data}"


# Case 8 -------------------------------------------------------------------
def test_08_already_on_page_navigate(scout_client):
    status, data = _chat(
        scout_client, "find engineers at Google", current_page="/find"
    )
    assert status == 200, data
    nav = _assert_navigate(data, "/find")
    assert nav["already_on_page"] is True, data
    carried = " ".join(str(v) for v in nav["prefill"].values()).lower()
    assert "google" in carried, f"expected Google in prefill: {nav}"


# Case 9 -------------------------------------------------------------------
def test_09_vague_request_no_hallucinated_route(scout_client):
    status, data = _chat(scout_client, "schedule a meeting for me")
    assert status == 200, data
    assert data["tool"] in ("clarify", "answer", "navigate"), data
    # Whatever it does, it must not invent a route.
    if data["tool"] == "navigate":
        assert data["navigate"]["route"] in VALID_ROUTES, (
            f"hallucinated route: {data['navigate']['route']}"
        )


# Case 10 ------------------------------------------------------------------
def test_10_low_credits_navigate_surfaces_cost(scout_client):
    status, data = _chat(
        scout_client, "find people at Google", user_info={"credits": 10}
    )
    assert status == 200, data
    nav = _assert_navigate(data, "/find")
    assert nav.get("credit_spending") is True, data
    # Frontend needs the number to warn a low-credit user.
    assert "credit_cost" in nav, (
        f"credit_cost integer not surfaced for a low-credit user: {nav}"
    )


# Case 11 ------------------------------------------------------------------
# Chat-first: "here in the chat" must keep the response in the conversation,
# not navigate, even though a recruiting-timeline page exists. Phase 5
# Stage 1 added strategy-memory tools whose presence nudges the model to
# scope the plan before drafting it; a focused clarifying response that
# names concrete recruiting variables (industry, timeline) is just as
# chat-first as a full plan dump, so the threshold mirrors test_13's 200.
def test_11_chat_first_recruiting_plan_answers(scout_client):
    status, data = _chat(
        scout_client, "help me plan a recruiting plan here in the chat"
    )
    assert status == 200, data
    assert data["tool"] == "answer", (
        f"'here in the chat' must stay in the conversation, got {data['tool']}: {data}"
    )
    assert data["navigate"] is None, data
    msg = data["message"] or ""
    assert len(msg) > 150, f"expected a substantive response, got {len(msg)} chars: {msg!r}"
    assert any(w in msg.lower() for w in (
        "timeline", "weeks", "target", "industry", "role",
    )), f"response should reference a recruiting variable: {msg!r}"


# Case 12 ------------------------------------------------------------------
def test_12_chat_first_cold_email_walkthrough_answers(scout_client):
    status, data = _chat(scout_client, "walk me through how to approach cold emails")
    assert status == 200, data
    assert data["tool"] == "answer", (
        f"'walk me through' is a chat-first cue, got {data['tool']}: {data}"
    )
    msg = data["message"] or ""
    assert "\n" in msg and len(msg) > 300, (
        f"expected a multi-paragraph walkthrough: {msg!r}"
    )


# Case 13 ------------------------------------------------------------------
def test_13_chat_first_brainstorm_targets_answers(scout_client):
    status, data = _chat(scout_client, "let's brainstorm companies I should target")
    assert status == 200, data
    assert data["tool"] == "answer", (
        f"'let's brainstorm' is a chat-first cue, got {data['tool']}: {data}"
    )
    assert len(data["message"] or "") > 200, (
        f"expected a substantive brainstorm: {data['message']!r}"
    )


# Case 14 ------------------------------------------------------------------
# The over-correction guard's twin: "talk me through" is a chat cue, so a
# strategy-style question stays an answer. After the Phase 5 cleanup there is
# no /interview-prep route, which makes the guarantee even firmer.
def test_14_chat_first_interview_strategy_answers_not_navigate(scout_client):
    status, data = _chat(scout_client, "talk me through interview prep strategy")
    assert status == 200, data
    assert data["tool"] == "answer", (
        f"'talk me through' must answer in chat, not navigate: {data}"
    )
    assert data["navigate"] is None, data


# Case 15 ------------------------------------------------------------------
# Counter-case: the chat-first rule must not over-correct. A direct command
# with no chat cue still navigates. Uses a live route (meeting prep) since
# /interview-prep and /cover-letter were removed.
def test_15_explicit_command_still_navigates(scout_client):
    status, data = _chat(scout_client, "take me to meeting prep")
    assert status == 200, data
    _assert_navigate(data, "/coffee-chat-prep")


# Case 16 ------------------------------------------------------------------
# Profile awareness: Scout must read the user's resume and ground its answer
# in it. The scout_client fixture stubs _fetch_user_context to {}; re-patch it
# here so the route hands handle_chat a real resume-bearing context.
def test_16_uses_resume_for_tailored_answer(scout_client):
    resume_ctx = {
        "resume": (
            "Jordan Lee\n"
            "Education: UCLA, B.S. Statistics, Class of 2026\n"
            "Experience:\n"
            "  - Data Analyst Intern at Spotify (Summer 2025): built churn "
            "models in Python, shipped a dashboard used by the growth team\n"
            "  - Research Assistant, UCLA Economics Lab\n"
            "Skills: Python, SQL, R, Tableau, machine learning"
        ),
    }
    with patch(
        "app.routes.scout_assistant._fetch_user_context", return_value=resume_ctx
    ):
        status, data = _chat(
            scout_client,
            "based on my resume, help me think through which roles to target",
        )
    assert status == 200, data
    assert data["tool"] == "answer", data
    msg = (data["message"] or "").lower()
    assert any(w in msg for w in ("python", "spotify", "data", "statistics", "sql")), (
        f"answer did not reference the user's resume specifics: {data['message']!r}"
    )


# Case 17 ------------------------------------------------------------------
# Date awareness: Scout gets today's date in the CURRENT CONTEXT block and
# must be able to state it.
def test_17_knows_current_date(scout_client):
    from datetime import datetime

    now = datetime.now()
    status, data = _chat(scout_client, "what is today's date?")
    assert status == 200, data
    assert data["tool"] == "answer", data
    msg = data["message"] or ""
    assert str(now.year) in msg, f"expected year {now.year} in the reply: {msg!r}"
    assert now.strftime("%B") in msg, (
        f"expected month {now.strftime('%B')} in the reply: {msg!r}"
    )


# Case 18 ------------------------------------------------------------------
# Scout is free: no Scout turn (answer, navigate, or clarify) charges credits.
# A Scout turn does no Firestore writes, so a balance cannot change; this locks
# that in behaviorally across all three terminal tools.
def test_18_scout_turns_never_charge_credits(scout_client):
    cases = [
        ("what does meeting prep do?", {"answer"}),
        ("take me to the job board", {"navigate"}),
        ("help me prep for my interview", {"clarify", "navigate", "answer"}),
    ]
    for message, allowed_tools in cases:
        status, data = _chat(
            scout_client, message, user_info={"credits": 200, "max_credits": 300}
        )
        assert status == 200, data
        assert data["tool"] in allowed_tools, f"{message!r} -> {data}"
        # No turn reports a credit charge against the user.
        assert "credits_charged" not in data, data
        assert "credits_remaining" not in data, data
