"""Every school a person attended rides from the warehouse doc to the UI."""
from app.services.firm_cache.reader import firm_employee_doc_to_app_contact
from app.services.person_ranker import _to_response_dict


def _doc(**extra):
    return {"first_name": "Mia", "last_name": "Ortiz", "company_display": "Google", **extra}


def test_reader_exposes_all_schools():
    out = firm_employee_doc_to_app_contact(
        _doc(schools_display=["University of Michigan", "Harvard Business School"])
    )
    assert out["Schools"] == ["University of Michigan", "Harvard Business School"]
    assert out["College"] == "University of Michigan"


def test_reader_schools_empty_when_missing():
    assert firm_employee_doc_to_app_contact(_doc())["Schools"] == []


def _scored(candidate):
    return {"id": "x", "candidate": candidate, "score": 7, "tier": "t", "reasons": [], "briefing": ""}


def test_response_dict_sends_schools():
    out = _to_response_dict(_scored({"Schools": ["A", "B"], "College": "A"}))
    assert out["person"]["schools"] == ["A", "B"]
    assert _to_response_dict(_scored({"College": "A"}))["person"]["schools"] == []
