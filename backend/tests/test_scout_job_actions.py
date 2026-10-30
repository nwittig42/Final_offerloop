"""Unit tests for Scout's job-workflow tools: hiring managers, cover
letters, resume tailoring. Gates hold without uid/context; results map to
compact chat envelopes; credits deduct only when results exist."""
import asyncio
from unittest.mock import MagicMock, patch

import pytest

from app.services.scout import job_actions
from app.services.scout.tools import run_helper_tool


def _run(name, args, ctx):
    return asyncio.run(run_helper_tool(name, args, ctx))


@pytest.mark.unit
@pytest.mark.parametrize("tool", [
    "find_hiring_managers", "generate_cover_letter", "tailor_resume_to_job",
])
def test_job_tools_require_auth(tool):
    out = _run(tool, {"company": "Stripe"}, {"uid": None})
    assert out["code"] == "AUTH_REQUIRED"


@pytest.mark.unit
def test_hiring_managers_requires_company():
    with patch.object(job_actions, "_db", return_value=object()):
        out = job_actions.find_hiring_managers_for_chat("u1", "")
    assert out["code"] == "BAD_REQUEST"


def _fake_user_db(user_data):
    db = MagicMock()
    snap = MagicMock()
    snap.exists = True
    snap.to_dict.return_value = dict(user_data)
    db.collection.return_value.document.return_value.get.return_value = snap
    return db


@pytest.mark.unit
def test_hiring_managers_maps_and_charges():
    user = {"resumeParsed": {"name": "Nick"}, "resumeText": "r", "credits": 100}
    managers = [
        {"FirstName": "Ann", "LastName": "Lee", "Title": "Recruiter",
         "Company": "Stripe", "Email": "ann@stripe.com", "LinkedIn": "https://li/ann"},
        {"FirstName": "Bob", "LastName": "Ray", "Title": "EM",
         "Company": "Stripe", "Email": "Not available", "LinkedIn": ""},
    ]
    with patch.object(job_actions, "_db", return_value=_fake_user_db(user)), \
         patch("app.services.auth.check_and_reset_credits", return_value=100), \
         patch("app.services.auth.deduct_credits_atomic", return_value=(True, 90)) as deduct, \
         patch("app.services.recruiter_finder.find_hiring_manager",
               return_value={"hiringManagers": managers}), \
         patch.object(job_actions, "_save_managers_to_tracker", return_value=2):
        out = job_actions.find_hiring_managers_for_chat(
            "u1", "Stripe", job_title="PM", count=3)
    assert out["count"] == 2
    assert out["managers"][0]["name"] == "Ann Lee"
    assert out["managers"][1]["email"] == ""  # "Not available" scrubbed
    assert out["credits_charged"] == 10
    deduct.assert_called_once_with("u1", 10, "find_hiring_manager")


@pytest.mark.unit
def test_hiring_managers_zero_results_charge_nothing():
    user = {"resumeParsed": {}, "credits": 100}
    with patch.object(job_actions, "_db", return_value=_fake_user_db(user)), \
         patch("app.services.auth.check_and_reset_credits", return_value=100), \
         patch("app.services.auth.deduct_credits_atomic") as deduct, \
         patch("app.services.recruiter_finder.find_hiring_manager",
               return_value={"hiringManagers": []}):
        out = job_actions.find_hiring_managers_for_chat("u1", "Stripe")
    assert out["count"] == 0
    deduct.assert_not_called()


@pytest.mark.unit
def test_cover_letter_needs_job_description():
    with patch.object(job_actions, "_db", return_value=_fake_user_db({})), \
         patch.object(job_actions, "_resolve_job_context",
                      return_value=("PM", "Stripe", "", "")):
        out = asyncio.run(job_actions.cover_letter_for_chat("u1", job_title="PM"))
    assert out["code"] == "NEEDS_JOB_DESCRIPTION"


@pytest.mark.unit
def test_cover_letter_success_returns_letter():
    user = {"resumeParsed": {"name": "Nick"}, "credits": 100}
    async def fake_gen(**kwargs):
        return {"content": "Dear Team, ...", "highlights": [], "tone": "Professional"}
    with patch.object(job_actions, "_db", return_value=_fake_user_db(user)), \
         patch.object(job_actions, "_resolve_job_context",
                      return_value=("PM", "Stripe", "desc", "")), \
         patch("app.services.auth.check_and_reset_credits", return_value=100), \
         patch("app.services.auth.deduct_credits_atomic", return_value=(True, 95)), \
         patch("app.routes.job_board.generate_cover_letter_with_ai", side_effect=fake_gen):
        out = asyncio.run(job_actions.cover_letter_for_chat("u1", job_title="PM"))
    assert out["cover_letter"].startswith("Dear Team")
    assert out["credits_charged"] == 5


@pytest.mark.unit
def test_cover_letter_failure_refunds():
    user = {"resumeParsed": {"name": "Nick"}, "credits": 100}
    async def boom(**kwargs):
        raise RuntimeError("api down")
    with patch.object(job_actions, "_db", return_value=_fake_user_db(user)), \
         patch.object(job_actions, "_resolve_job_context",
                      return_value=("PM", "Stripe", "desc", "")), \
         patch("app.services.auth.check_and_reset_credits", return_value=100), \
         patch("app.services.auth.deduct_credits_atomic", return_value=(True, 95)), \
         patch("app.services.auth.refund_credits_atomic", return_value=(True, 100)) as refund, \
         patch("app.routes.job_board.generate_cover_letter_with_ai", side_effect=boom):
        out = asyncio.run(job_actions.cover_letter_for_chat("u1", job_title="PM"))
    assert out["code"] == "INTERNAL"
    refund.assert_called_once()


@pytest.mark.unit
def test_tailor_resume_needs_resume():
    with patch.object(job_actions, "_db", return_value=_fake_user_db({})), \
         patch.object(job_actions, "_resolve_job_context",
                      return_value=("PM", "Stripe", "desc", "")):
        out = job_actions.tailor_resume_for_chat("u1", job_title="PM")
    assert out["code"] == "NEEDS_RESUME"


@pytest.mark.unit
def test_tailor_resume_returns_analysis():
    user = {"resumeText": "my resume"}
    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = (
        '{"fit_score": 72, "verdict": "solid", "strengths": ["a"],'
        ' "gaps": ["b"], "edits": [{"section": "x", "current": "c", "suggested": "s"}]}'
    )
    client = MagicMock()
    client.chat.completions.create.return_value = fake_response
    with patch.object(job_actions, "_db", return_value=_fake_user_db(user)), \
         patch.object(job_actions, "_resolve_job_context",
                      return_value=("PM", "Stripe", "desc", "")), \
         patch("app.services.openai_client.get_openai_client", return_value=client):
        out = job_actions.tailor_resume_for_chat("u1", job_title="PM")
    assert out["fit_score"] == 72
    assert out["edits"][0]["suggested"] == "s"


@pytest.mark.unit
def test_prompt_advertises_job_workflow_tools():
    from app.services.scout_assistant_service import _build_static_system_prompt
    prompt = _build_static_system_prompt()
    for name in ("find_hiring_managers", "generate_cover_letter", "tailor_resume_to_job"):
        assert name in prompt
    assert "Resume and cover letters from chat" in prompt


def _svc():
    from app.services.scout_assistant_service import ScoutAssistantService
    return ScoutAssistantService.__new__(ScoutAssistantService)


@pytest.mark.unit
def test_workflow_cta_fallbacks_for_job_tools():
    svc = _svc()
    r1 = svc._enrich_workflow_ctas(
        {"tool": "answer", "message": "m", "cta": None},
        [{"name": "find_hiring_managers", "result": {"count": 2, "managers": []}}])
    assert r1["cta"]["route"] == "/find?tab=hiring-managers"
    r2 = svc._enrich_workflow_ctas(
        {"tool": "answer", "message": "m", "cta": None},
        [{"name": "generate_cover_letter", "result": {"cover_letter": "Dear", "company": "Stripe"}}])
    assert r2["cta"] is None
    r3 = svc._enrich_workflow_ctas(
        {"tool": "answer", "message": "m", "cta": None},
        [{"name": "tailor_resume_to_job", "result": {"fit_score": 55}}])
    assert r3["cta"] is None


@pytest.mark.unit
def test_cover_letter_enrichment_appends_verbatim_letter():
    svc = _svc()
    helpers = [{"name": "generate_cover_letter",
                "result": {"cover_letter": "Dear Team,\nI am excited to apply.\nSincerely, Nick"}}]
    out = svc._enrich_cover_letter_report(
        {"tool": "answer", "message": "Here's your letter (paraphrased badly)."}, helpers)
    assert "Dear Team," in out["message"]
    # Already-verbatim messages are left alone (no duplicate letter).
    out2 = svc._enrich_cover_letter_report(
        {"tool": "answer",
         "message": "Here you go:\n\nDear Team,\nI am excited to apply.\nSincerely, Nick"},
        helpers)
    assert out2["message"].count("Dear Team,") == 1
