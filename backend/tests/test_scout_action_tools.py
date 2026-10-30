"""Unit tests for Scout's job action tools (find_jobs + auto_apply_to_job).

The execute tool's gates must hold without any network: no uid, wrong tier,
and missing job_id all short-circuit before touching the submit service.
"""
import asyncio

import pytest

from app.services.scout.tools import run_helper_tool


def _run(name, args, ctx):
    return asyncio.run(run_helper_tool(name, args, ctx))


@pytest.mark.unit
def test_auto_apply_requires_auth():
    out = _run("auto_apply_to_job", {"job_id": "j1"}, {"uid": None, "tier": "pro"})
    assert out["code"] == "AUTH_REQUIRED"


@pytest.mark.unit
def test_auto_apply_requires_pro_tier():
    out = _run("auto_apply_to_job", {"job_id": "j1"}, {"uid": "u1", "tier": "free"})
    assert out["code"] == "TIER_REQUIRED"


@pytest.mark.unit
def test_auto_apply_requires_job_id():
    out = _run("auto_apply_to_job", {}, {"uid": "u1", "tier": "elite"})
    assert out["code"] == "BAD_REQUEST"


@pytest.mark.unit
def test_find_jobs_requires_query():
    out = _run("find_jobs", {"query": ""}, {"uid": "u1", "tier": "pro"})
    assert out["count"] == 0 and out.get("error")


@pytest.mark.unit
def test_prompt_advertises_job_action_tools():
    from app.services.scout_assistant_service import _build_static_system_prompt
    prompt = _build_static_system_prompt()
    assert "find_jobs" in prompt
    assert "auto_apply_to_job" in prompt
    assert "Applying to jobs from chat" in prompt


# ---------------------------------------------------------------------------
# find_jobs must survive job docs whose fields are not strings (a dict
# location once raised KeyError(slice(...)) and killed the whole search).
# ---------------------------------------------------------------------------

class _FakeJobSnap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _FakeJobQuery:
    def __init__(self, snaps):
        self._snaps = snaps

    def order_by(self, *a, **k):
        return self

    def where(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def stream(self):
        return list(self._snaps)


class _FakeJobsDb:
    def __init__(self, snaps):
        self._snaps = snaps

    def collection(self, name):
        return _FakeJobQuery(self._snaps)


@pytest.mark.unit
def test_find_jobs_tolerates_dict_fields(monkeypatch):
    from app.services.scout import tools as scout_tools

    snaps = [
        _FakeJobSnap("j1", {
            "title": "Data Science Intern",
            "company": {"name": "Snap"},
            "location": {"city": "Los Angeles", "state": "CA"},
            "ats_platform": "greenhouse",
        }),
        _FakeJobSnap("j2", {
            "title": "Data Analyst",
            "company": "Netflix",
            "location": None,
        }),
    ]
    import app.extensions as extensions
    monkeypatch.setattr(extensions, "get_db", lambda: _FakeJobsDb(snaps))

    out = scout_tools._find_jobs("data science intern", 5)
    assert "error" not in out, out
    assert out["count"] == 2
    by_id = {j["job_id"]: j for j in out["jobs"]}
    assert by_id["j1"]["company"] == "Snap"
    assert by_id["j1"]["location"] == "Los Angeles"
    # Higher token overlap ranks first.
    assert out["jobs"][0]["job_id"] == "j1"


# ---------------------------------------------------------------------------
# draft_outreach_emails gates
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_draft_outreach_requires_auth():
    out = _run("draft_outreach_emails", {"contact_names": ["a b"]}, {"uid": None, "tier": "pro"})
    assert out["code"] == "AUTH_REQUIRED"
    assert out["count"] == 0


@pytest.mark.unit
def test_prompt_advertises_draft_tool():
    from app.services.scout_assistant_service import _build_static_system_prompt
    prompt = _build_static_system_prompt()
    assert "draft_outreach_emails" in prompt
    assert "Drafting emails from chat" in prompt


# ---------------------------------------------------------------------------
# Workflow cta fallback (execute -> result in chat -> navigate chip): the
# harness attaches the chip when the model forgets, and never overrides a
# model-authored one.
# ---------------------------------------------------------------------------

def _svc():
    from app.services.scout_assistant_service import ScoutAssistantService
    return ScoutAssistantService.__new__(ScoutAssistantService)


@pytest.mark.unit
def test_workflow_cta_fallback_for_queued_apply():
    result = {"tool": "answer", "message": "queued", "cta": None}
    helpers = [{"name": "auto_apply_to_job", "result": {"status": "queued"}}]
    out = _svc()._enrich_workflow_ctas(result, helpers)
    assert out["cta"]["route"] == "/applications"


@pytest.mark.unit
def test_workflow_cta_fallback_for_job_matches():
    result = {"tool": "answer", "message": "5 matches", "cta": None}
    helpers = [{"name": "find_jobs", "result": {"count": 5, "jobs": [], "query": "swe intern"}}]
    out = _svc()._enrich_workflow_ctas(result, helpers)
    assert out["cta"]["route"] == "/job-board"
    assert out["cta"]["prefill"] == {"query": "swe intern"}


@pytest.mark.unit
def test_workflow_cta_respects_model_chip():
    chip = {"label": "x", "route": "/find", "prefill": {}}
    result = {"tool": "answer", "message": "5 matches", "cta": chip}
    helpers = [{"name": "find_jobs", "result": {"count": 5, "jobs": [], "query": "swe"}}]
    out = _svc()._enrich_workflow_ctas(result, helpers)
    assert out["cta"] is chip


# ---------------------------------------------------------------------------
# Consent gates: execute tools refuse workflows the user never asked for.
# Regression for the bare "1" count reply that triggered an unrequested
# draft AND an unrequested 30-credit meeting prep.
# ---------------------------------------------------------------------------

def _count_reply_ctx():
    return {
        "uid": "u1", "tier": "elite",
        "user_message": "1",
        "recent_user_text": "find me a software engineer at cluely \n1",
        "last_assistant_text": "How many software engineers at Cluely should I pull?",
    }


@pytest.mark.unit
def test_draft_refused_without_user_ask():
    out = _run("draft_outreach_emails", {}, _count_reply_ctx())
    assert out["code"] == "CONSENT_REQUIRED"


@pytest.mark.unit
def test_meeting_prep_refused_without_user_ask():
    out = _run("run_meeting_prep", {"contact_name": "Yash"}, _count_reply_ctx())
    assert out["code"] == "CONSENT_REQUIRED"
    assert out["started"] is False


@pytest.mark.unit
def test_auto_apply_refused_without_user_ask():
    out = _run("auto_apply_to_job", {"job_id": "j1"}, _count_reply_ctx())
    assert out["code"] == "CONSENT_REQUIRED"


@pytest.mark.unit
def test_draft_allowed_when_user_asked():
    from unittest.mock import patch
    ctx = {"uid": "u1", "tier": "pro",
           "user_message": "draft emails to them",
           "recent_user_text": "find me 3 people at stripe \ndraft emails to them",
           "last_assistant_text": "Found 3 people."}
    with patch("app.services.scout.outreach_actions.draft_emails_to_contacts",
               return_value={"drafted": [], "skipped": [], "count": 0}) as m:
        out = _run("draft_outreach_emails", {}, ctx)
    assert m.called
    assert out.get("code") != "CONSENT_REQUIRED"


@pytest.mark.unit
def test_offer_acceptance_counts_as_consent():
    from unittest.mock import patch
    ctx = {"uid": "u1", "tier": "pro",
           "user_message": "yes please",
           "recent_user_text": "find me 3 people at stripe \nyes please",
           "last_assistant_text": "Found 3 people. Want me to draft emails to them?"}
    with patch("app.services.scout.outreach_actions.draft_emails_to_contacts",
               return_value={"drafted": [], "skipped": [], "count": 0}) as m:
        out = _run("draft_outreach_emails", {}, ctx)
    assert m.called
    assert out.get("code") != "CONSENT_REQUIRED"


@pytest.mark.unit
def test_gate_open_without_chat_context():
    """Direct callers (tests, future API surfaces) without recent_user_text
    are not gated; the gate protects the chat loop."""
    from app.services.scout.tools import _user_authorized, _DRAFT_KEYWORDS
    assert _user_authorized({"uid": "u1"}, _DRAFT_KEYWORDS) is True


# ---------------------------------------------------------------------------
# Broken-promise guard: an `answer` claiming cover-letter work that no tool
# performed is detected (the loop rejects it once and forces the tool call).
# Claim strings below are verbatim from the 2026-07-08 field recording.
# ---------------------------------------------------------------------------

def _promise(text, helpers=(), user="choose one, write a cover letter for it"):
    return _svc()._is_broken_cover_letter_promise(
        "answer", {"text": text}, list(helpers), {"recent_user_text": user})


@pytest.mark.unit
def test_promise_guard_catches_future_claim():
    assert _promise(
        "Sure - I'll pick the Trading Desk Operations Engineer role at Jane "
        "Street in NYC and generate a tailored cover letter for it now.")


@pytest.mark.unit
def test_promise_guard_catches_ready_claim():
    assert _promise(
        "You asked for the Trading Desk Operations Engineer cover letter - "
        "I've generated it and it's ready on the Cover Letter page.",
        user="where's the cover letter")


@pytest.mark.unit
def test_promise_guard_ignores_offers():
    assert not _promise("I can write a cover letter once you pick a role.")


@pytest.mark.unit
def test_promise_guard_cleared_when_tool_ran():
    helpers = [{"name": "generate_cover_letter", "result": {"error": "x",
                "code": "NEEDS_JOB_DESCRIPTION"}}]
    assert not _promise(
        "I couldn't generate the cover letter without the posting - paste "
        "the URL and I'll write it.", helpers=helpers)


@pytest.mark.unit
def test_promise_guard_needs_user_to_have_asked():
    assert not _promise(
        "I'll generate a tailored cover letter for it now.",
        user="what should I do next")


def _nav_promise(reasoning, route="/cover-letter", helpers=(),
                 user="refresh the cover letter"):
    return _svc()._is_broken_cover_letter_promise(
        "navigate", {"route": route, "reasoning": reasoning},
        list(helpers), {"recent_user_text": user})


@pytest.mark.unit
def test_promise_guard_catches_navigate_claiming_refreshed_letter():
    # Verbatim from the 2026-07-08 screenshot (DO-mode navigate).
    assert _nav_promise(
        "Sure thing, setting up a refreshed cover letter for the Jane Street "
        "Trading Desk Operations Engineer role focused on your systems and "
        "data engineering strengths from Offerloop; open the editor to "
        "review and tweak tone or add a project.")


@pytest.mark.unit
def test_promise_guard_catches_navigate_naming_a_specific_letter():
    assert _nav_promise(
        "Done, opening the Jane Street Trading Desk Operations Engineer "
        "cover letter in the editor for you to review and edit.",
        user="okay where is it, the letter")
    assert _nav_promise(
        "Opening the Jane Street Trading Desk Operations Engineer cover "
        "letter editor so you can review and edit the refreshed draft.",
        user="where's my letter")


@pytest.mark.unit
def test_promise_guard_allows_honest_page_open():
    assert not _nav_promise(
        "Opening the cover letter editor so you can generate one for this role.")


@pytest.mark.unit
def test_promise_guard_ignores_navigate_to_other_routes():
    assert not _nav_promise(
        "Opening the Job Board with your refreshed cover letter search.",
        route="/job-board")


# ---------------------------------------------------------------------------
# Network cta normalization: find-only turns chip to My Network (an Inbox
# chip is a false promise); draft turns carry BOTH chips via `ctas`.
# ---------------------------------------------------------------------------

_NETWORK_ROUTE = "/my-network/people"


@pytest.mark.unit
def test_find_only_outbox_chip_is_replaced_with_network():
    result = {"tool": "answer", "message": "found 3", "cta": {
        "label": "Open your Inbox", "route": "/outbox", "prefill": {},
        "credit_spending": False, "credit_cost": None}}
    helpers = [{"name": "find_contacts", "result": {"count": 3, "contacts": []}}]
    out = _svc()._enrich_network_ctas(result, helpers)
    assert out["cta"]["route"] == _NETWORK_ROUTE
    assert "ctas" not in out


@pytest.mark.unit
def test_find_only_non_outbox_chip_is_respected():
    chip = {"label": "Open the Job Board", "route": "/job-board", "prefill": {}}
    result = {"tool": "answer", "message": "found 3", "cta": chip}
    helpers = [{"name": "find_contacts", "result": {"count": 3, "contacts": []}}]
    out = _svc()._enrich_network_ctas(result, helpers)
    assert out["cta"] is chip


@pytest.mark.unit
def test_draft_turn_gets_both_chips():
    inbox = {"label": "Open in your Inbox", "route": "/outbox?contact=c1",
             "prefill": {}, "credit_spending": False, "credit_cost": None}
    result = {"tool": "answer", "message": "drafted", "cta": inbox}
    helpers = [
        {"name": "find_contacts", "result": {"count": 3, "contacts": []}},
        {"name": "draft_outreach_emails", "result": {"count": 3, "drafted": [{}]}},
    ]
    out = _svc()._enrich_network_ctas(result, helpers)
    assert out["ctas"][0] is inbox
    assert out["ctas"][1]["route"] == _NETWORK_ROUTE
    assert out["cta"] is inbox


# ---------------------------------------------------------------------------
# discover_companies: multi-company discovery executes in chat. Same gates
# as find_contacts (auth + harness-enforced count), same credit rules as the
# Companies tab (2 credits per company returned, none on a zero result).
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_discover_companies_requires_auth():
    out = _run("discover_companies", {"query": "telecom startups", "count": 10},
               {"uid": None, "tier": "pro"})
    assert out["code"] == "AUTH_REQUIRED"
    assert out["count"] == 0


@pytest.mark.unit
def test_discover_companies_requires_user_count():
    ctx = {"uid": "u1", "tier": "pro",
           "user_message": "find smaller telecom startups on the west coast"}
    out = _run("discover_companies", {"query": "telecom startups", "count": 10}, ctx)
    assert out["code"] == "COUNT_REQUIRED"
    assert out["count"] == 0


@pytest.mark.unit
def test_discover_companies_dispatches_with_user_count():
    from unittest.mock import patch
    ctx = {"uid": "u1", "tier": "pro",
           "user_message": "find 10 smaller telecom startups on the west coast"}
    with patch("app.services.scout.company_actions.discover_companies_for_chat",
               return_value={"count": 1, "companies": [{"name": "Acme"}],
                             "query": "telecom startups", "credits_charged": 2}) as m:
        out = _run("discover_companies",
                   {"query": "smaller telecom startups on the west coast", "count": 10}, ctx)
    assert m.called
    args = m.call_args[0]
    assert args[0] == "u1"
    assert args[3] == 10
    assert out["count"] == 1
    assert ctx.get("workflow_state_touched") is True


@pytest.mark.unit
def test_prompt_advertises_discover_companies():
    from app.services.scout_assistant_service import _build_static_system_prompt
    prompt = _build_static_system_prompt()
    assert "discover_companies" in prompt
    assert "Finding companies from chat" in prompt


@pytest.mark.unit
def test_workflow_cta_for_hiring_managers_opens_saved_view():
    """The chip after a hiring manager find opens My Network's Managers tab
    (where the saved people are), not the search page."""
    result = {"tool": "answer", "message": "found 1", "cta": None}
    helpers = [{"name": "find_hiring_managers", "result": {"count": 1, "managers": []}}]
    out = _svc()._enrich_workflow_ctas(result, helpers)
    assert out["cta"]["route"] == "/my-network/managers"


@pytest.mark.unit
def test_workflow_cta_fallback_for_discovered_companies():
    result = {"tool": "answer", "message": "10 companies", "cta": None}
    helpers = [{"name": "discover_companies",
                "result": {"count": 10, "companies": [], "query": "telecom startups"}}]
    out = _svc()._enrich_workflow_ctas(result, helpers)
    assert out["cta"]["route"] == "/find?tab=companies"
    assert out["cta"]["prefill"] == {"prompt": "telecom startups"}


# ---------------------------------------------------------------------------
# discover_companies_for_chat guard sequence (no network: search + credits
# + history are all patched at their source modules).
# ---------------------------------------------------------------------------

class _FakeUserDoc:
    def __init__(self, exists=True, data=None):
        self.exists = exists
        self._data = data or {}

    def to_dict(self):
        return dict(self._data)


class _FakeUserRef:
    def __init__(self, doc):
        self._doc = doc

    def get(self):
        return self._doc


class _FakeUsersDb:
    def __init__(self, doc):
        self._doc = doc

    def collection(self, name):
        return self

    def document(self, uid):
        return _FakeUserRef(self._doc)


def _discover(monkeypatch, search_result, credits=100, deduct_ok=True):
    from unittest.mock import MagicMock
    from app.services.scout import company_actions
    import app.services.company_search as company_search
    import app.services.auth as auth
    import app.routes.firm_search as firm_search

    monkeypatch.setattr(company_actions, "_db",
                        lambda: _FakeUsersDb(_FakeUserDoc(data={"credits": credits})))
    monkeypatch.setattr(auth, "check_and_reset_credits", lambda ref, data: credits)
    deduct = MagicMock(return_value=(deduct_ok, credits))
    monkeypatch.setattr(auth, "deduct_credits_atomic", deduct)
    save = MagicMock(return_value="hist-1")
    monkeypatch.setattr(firm_search, "save_search_to_history", save)
    monkeypatch.setattr(company_search, "search_firms",
                        lambda query, limit: search_result)

    out = company_actions.discover_companies_for_chat(
        "u1", "pro", "smaller telecom startups on the west coast", 10)
    return out, deduct, save


@pytest.mark.unit
def test_discover_companies_charges_per_firm_and_saves_history(monkeypatch):
    firms = [
        {"name": "Beta Telecom", "industry": "telecom", "employeeCount": 40,
         "sizeBucket": "small", "location": {"display": "Seattle, WA"}},
        {"name": "Acme Wireless", "industry": "telecom", "employeeCount": 120,
         "sizeBucket": "mid", "location": {"display": "Portland, OR"}},
    ]
    out, deduct, save = _discover(
        monkeypatch, {"success": True, "firms": firms, "parsedFilters": {"industry": "telecom"}})
    assert out["count"] == 2
    assert out["credits_charged"] == 4
    assert out["saved_to_history"] is True
    # Largest firm first, mirroring the Companies tab ordering.
    assert out["companies"][0]["name"] == "Acme Wireless"
    assert out["companies"][0]["location"] == "Portland, OR"
    deduct.assert_called_once_with("u1", 4, "firm_search")
    assert save.called


@pytest.mark.unit
def test_discover_companies_zero_results_charge_nothing(monkeypatch):
    out, deduct, save = _discover(monkeypatch, {"success": True, "firms": []})
    assert out["count"] == 0
    assert "error" not in out
    assert not deduct.called
    assert not save.called


@pytest.mark.unit
def test_discover_companies_insufficient_credits(monkeypatch):
    out, deduct, _ = _discover(
        monkeypatch, {"success": True, "firms": [{"name": "Acme"}]}, credits=1)
    assert out["code"] == "INSUFFICIENT_CREDITS"
    assert not deduct.called


@pytest.mark.unit
def test_discover_companies_parse_failure_is_bad_request(monkeypatch):
    out, deduct, _ = _discover(
        monkeypatch,
        {"success": False, "error": "Failed to understand the search query"})
    assert out["code"] == "BAD_REQUEST"
    assert not deduct.called
