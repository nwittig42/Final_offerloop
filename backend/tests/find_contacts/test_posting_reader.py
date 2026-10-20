"""Posting reader: the OpenAI client is mocked, no network."""
import json
from unittest.mock import MagicMock, patch

from app.services.find_contacts.posting_reader import read_posting

KEYS = {
    "company", "team", "team_source", "function", "office_city", "level",
    "is_internship", "named_people", "size_hint",
}


def _client(payload=None, raises=None):
    client = MagicMock()
    if raises:
        client.chat.completions.create.side_effect = raises
    else:
        resp = MagicMock()
        resp.choices[0].message.content = json.dumps(payload)
        client.chat.completions.create.return_value = resp
    return client


def _read(job, **client_kwargs):
    client = _client(**client_kwargs)
    with patch("app.services.find_contacts.posting_reader.get_openai_client", return_value=client):
        return read_posting(job), client


def test_department_field_wins():
    job = {"title": "Analyst", "company": "Acme", "department": "Investment Banking"}
    out, _ = _read(job, payload={"team": "Something Else", "team_source": "description", "function": "ib"})
    assert out["team"] == "Investment Banking"
    assert out["team_source"] == "department field"
    assert out["function"] == "ib"
    assert set(out) == KEYS


def test_team_from_title():
    job = {"title": "OCTO Strategy Intern", "company": "Acme", "type": "Internship",
           "location": "Seattle, WA", "linkedin_org_employees": 5000}
    out, client = _read(job, payload={
        "team": "Office of the CTO", "team_source": "job title", "function": "strategy",
        "office_city": "Seattle", "level": "intern",
        "named_people": [{"name": "Jane Roe", "role": "Hiring manager"}, {"name": "", "role": "x"}],
    })
    assert (out["team"], out["team_source"]) == ("Office of the CTO", "job title")
    assert out["is_internship"] is True and out["level"] == "intern"
    assert out["size_hint"] == "large"
    assert out["named_people"] == [{"name": "Jane Roe", "role": "Hiring manager"}]
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["temperature"] == 0 and kwargs["max_tokens"] == 500


def test_office_city_sanitized():
    job = {"title": "SWE", "company": "Acme", "location": "Remote"}
    out, _ = _read(job, payload={"office_city": "Remote", "function": "software"})
    assert out["office_city"] is None


def test_llm_failure_returns_fallback():
    job = {"title": "SWE", "company": "Acme", "location": "Austin, TX", "linkedin_org_employees": 40}
    out, _ = _read(job, raises=RuntimeError("boom"))
    assert set(out) == KEYS
    assert out["team"] == "" and out["team_source"] == ""
    assert out["function"] == "other"
    assert out["office_city"] == "Austin"
    assert out["size_hint"] == "startup"
    assert out["named_people"] == []


def test_unknown_function_coerced():
    out, _ = _read({"title": "x", "company": "Acme"}, payload={"function": "banking stuff", "level": "wizard"})
    assert out["function"] == "other"
    assert out["level"] == "other"
    assert out["size_hint"] == "unknown"


def test_description_is_truncated_in_prompt():
    job = {"title": "x", "company": "Acme", "description_raw": "a" * 9000}
    _, client = _read(job, payload={})
    user = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
    assert "a" * 5000 in user and "a" * 5001 not in user
