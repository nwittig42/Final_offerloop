"""get_user_school: an explicit "not in school" from onboarding beats the resume."""
from app.utils.users import get_user_school

RESUME = {"resumeParsed": {"education": {"university": "University of Michigan"}}}
CONFIRMED = {"onboarding": {"profileConfirmedAt": "2026-10-20T00:00:00Z"}}


def test_resume_school_when_nothing_confirmed():
    assert get_user_school(dict(RESUME)) == "University of Michigan"


def test_confirmed_not_in_school_beats_resume():
    doc = {**RESUME, **CONFIRMED, "universityOptOut": True, "academics": {"university": ""}, "university": ""}
    assert get_user_school(doc) == ""


def test_school_set_after_opting_out_counts():
    doc = {**RESUME, **CONFIRMED, "universityOptOut": True, "university": "Santa Monica College"}
    assert get_user_school(doc) == "Santa Monica College"
    doc = {**RESUME, **CONFIRMED, "universityOptOut": True, "academics": {"university": "UCLA"}}
    assert get_user_school(doc) == "UCLA"


def test_confirmed_school_beats_resume():
    doc = {**RESUME, **CONFIRMED, "academics": {"university": "UCLA"}}
    assert get_user_school(doc) == "UCLA"


def test_legacy_opt_out_without_confirm_keeps_old_chain():
    assert get_user_school({**RESUME, "universityOptOut": True}) == "University of Michigan"
