from unittest.mock import patch

from app.services.coresignal_client import _build_es_query
from app.services.recruiter_finder import _run_engine_tier_search, determine_job_type
from app.utils.locations import office_city_or_none


def test_tier_titles_not_gutted():
    q = _build_es_query({
        "companies": [{"name": "bybit"}],
        "title_variations": ["staffing manager", "director", "vp"],
        "skip_title_guard": True,
    })
    s = str(q)
    for t in ("staffing manager", "director", "vp"):
        assert f"'experience.position_title': '{t}'" in s


def test_remote_location_dropped():
    assert office_city_or_none("Europe - Remote") is None
    assert office_city_or_none("Remote") is None
    assert office_city_or_none("New York, NY") == "New York"
    assert office_city_or_none("Multiple Locations") is None


def test_engine_tier_search_passes_city_only():
    with patch("app.services.engine_search.engine_search_contacts",
               return_value=([], None, 0, {})) as m:
        _run_engine_tier_search("bybit", ["director"], "Europe - Remote", 5)
    parsed = m.call_args[0][0]
    assert parsed["locations"] == []
    assert parsed["skip_title_guard"] is True


def test_job_type_whole_words():
    assert determine_job_type("Business Development Assistant",
                              "Join our international team") == "sales"
    assert determine_job_type("Summer Analyst Intern", "") == "intern"
