"""Fetchers keep department/team from Greenhouse, Lever and Ashby."""
from unittest.mock import MagicMock, patch

from pipeline import fetcher
from pipeline.normalizer import _normalize_board_job


def _resp(payload):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


def _gh(job):
    base = {"id": 1, "title": "Analyst", "location": {"name": "NYC"},
            "content": "", "absolute_url": "http://x", "updated_at": None}
    base.update(job)
    with patch.object(fetcher, "_polite_sleep"), \
         patch.object(fetcher._SESSION_GREENHOUSE, "get", return_value=_resp({"jobs": [base]})):
        return fetcher._fetch_greenhouse("acme")[0]


def test_greenhouse_departments_joined():
    j = _gh({"departments": [{"name": "Investment Banking"}, {"name": "TMT"}]})
    assert j["department"] == "Investment Banking / TMT"
    assert j["team"] == ""


def test_greenhouse_no_departments():
    assert _gh({"departments": []})["department"] == ""
    assert _gh({})["department"] == ""


def test_lever_categories_team():
    posting = {"id": "a", "text": "Analyst", "descriptionPlain": "",
               "categories": {"team": "Payments Platform", "department": "Engineering"}}
    with patch.object(fetcher, "_polite_sleep"), \
         patch.object(fetcher._SESSION_LEVER, "get", return_value=_resp([posting])):
        j = fetcher._fetch_lever("acme")[0]
    assert j["team"] == "Payments Platform"
    assert j["department"] == "Engineering"


def test_ashby_department_and_team():
    job = {"id": "a", "title": "Analyst", "department": "Strategy", "team": "Office of the CTO"}
    with patch.object(fetcher, "_polite_sleep"), \
         patch.object(fetcher._SESSION_ASHBY, "get", return_value=_resp({"jobs": [job]})):
        j = fetcher._fetch_ashby("acme")[0]
    assert j["department"] == "Strategy"
    assert j["team"] == "Office of the CTO"


def test_normalized_doc_keeps_team_fields():
    raw = {"job_id": "j1", "title": "Analyst", "company": "Acme",
           "department": "Strategy", "team": "Office of the CTO"}
    out = _normalize_board_job(raw)
    assert out["department"] == "Strategy"
    assert out["team"] == "Office of the CTO"
    del raw["department"], raw["team"]
    out = _normalize_board_job(raw)
    assert out["department"] == "" and out["team"] == ""
