"""Find contacts: each drafted contact's OutreachRole adds that role's line to its prompt."""
import json
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", "test-credentials.json")

from app.services.reply_generation import batch_generate_emails

ROLE_LINES = {
    "leader": "This person leads the team the role sits in. Ask for 15 minutes of advice about the team, not for a job.",
    "process": "This person likely interviews for the role. Mention the student has applied and ask one specific question about the work.",
    "recruiter": "This person coordinates recruiting. Ask a short logistics question about timeline or next steps.",
}


def _mock_client(n):
    emails = {str(i): {"subject": f"Question {i}", "body": f"Hi P{i},\n\nHello.\n\nBest,\nTest User"} for i in range(n)}
    msg = MagicMock()
    msg.content = json.dumps(emails)
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    client = MagicMock()
    client.with_options.return_value = client
    client.chat.completions.create.return_value = resp
    return client


def _prompt(client):
    kwargs = client.chat.completions.create.call_args[1]
    return " ".join(m.get("content", "") for m in kwargs.get("messages", []))


def _contact(first, role=None):
    c = {"FirstName": first, "LastName": "Doe", "Company": "Acme", "Title": "Director"}
    if role:
        c["OutreachRole"] = role
    return c


@pytest.mark.parametrize("role", ["leader", "process", "recruiter"])
@patch("app.services.reply_generation.get_anthropic_client", return_value=None)
@patch("app.services.reply_generation.get_openai_client")
def test_role_line_in_prompt(mock_client_fn, _anthropic, role):
    client = _mock_client(1)
    mock_client_fn.return_value = client

    batch_generate_emails([_contact("Jane", role)], "Resume", {"academics": {"university": "USC"}}, [])

    prompt = _prompt(client)
    assert ROLE_LINES[role] in prompt
    for other, line in ROLE_LINES.items():
        if other != role:
            assert line not in prompt


@patch("app.services.reply_generation.get_anthropic_client", return_value=None)
@patch("app.services.reply_generation.get_openai_client")
def test_role_line_follows_its_contact(mock_client_fn, _anthropic):
    client = _mock_client(2)
    mock_client_fn.return_value = client

    batch_generate_emails(
        [_contact("Jane", "leader"), _contact("Bob", "recruiter")],
        "Resume", {"academics": {"university": "USC"}}, [],
    )

    prompt = _prompt(client)
    jane = prompt.index("Contact 0: Jane")
    bob = prompt.index("Contact 1: Bob")
    assert jane < prompt.index(ROLE_LINES["leader"]) < bob < prompt.index(ROLE_LINES["recruiter"])


@patch("app.services.reply_generation.get_anthropic_client", return_value=None)
@patch("app.services.reply_generation.get_openai_client")
def test_no_role_or_unknown_role_adds_nothing(mock_client_fn, _anthropic):
    client = _mock_client(2)
    mock_client_fn.return_value = client

    batch_generate_emails(
        [_contact("Jane"), _contact("Bob", "ceo")],
        "Resume", {"academics": {"university": "USC"}}, [],
    )

    prompt = _prompt(client)
    for line in ROLE_LINES.values():
        assert line not in prompt
