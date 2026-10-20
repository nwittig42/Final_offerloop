from unittest.mock import patch

from app.services import coresignal_client as cc


def _nested(extra_must):
    return {
        "nested": {
            "path": "experience",
            "query": {"bool": {"must": [
                {"term": {"experience.active_experience": 1}},
                {"match_phrase": {"experience.company_name": "Stripe"}},
            ] + extra_must}},
        }
    }


def _titles(titles):
    return {"bool": {
        "should": [{"match_phrase": {"experience.position_title": t}} for t in titles],
        "minimum_should_match": 1,
    }}


def test_titles_only():
    with patch.object(cc, "_search_ids", return_value=[1, 2]) as s:
        out = cc.search_people("Stripe", ["Recruiter", "Technical Recruiter"])
    assert out == [1, 2]
    assert s.call_args.args[0] == {"query": {"bool": {"must": [
        _nested([_titles(["Recruiter", "Technical Recruiter"])])
    ]}}}
    assert s.call_args.kwargs["page_size"] == 10


def test_titles_and_keywords():
    with patch.object(cc, "_search_ids", return_value=[]) as s:
        cc.search_people("Stripe", ["Director"], keywords=["engineering", "payments"], page_size=5)
    assert s.call_args.args[0] == {"query": {"bool": {"must": [
        _nested([
            _titles(["Director"]),
            {"match_phrase": {"experience.position_title": "engineering"}},
            {"match_phrase": {"experience.position_title": "payments"}},
        ])
    ]}}}


def test_titles_and_city():
    with patch.object(cc, "_search_ids", return_value=[]) as s:
        cc.search_people("Stripe", ["Director"], city="San Francisco")
    assert s.call_args.args[0] == {"query": {"bool": {"must": [
        _nested([_titles(["Director"])]),
        {"match_phrase": {"location_full": "San Francisco"}},
    ]}}}


def test_no_distinctive_filter():
    with patch.object(cc, "_search_ids", return_value=[]) as s, \
         patch.object(cc, "_distinctive_variations") as d:
        cc.search_people("Stripe", ["Manager"])
    d.assert_not_called()
    assert "Manager" in str(s.call_args.args[0])


def test_collect_contacts_limit():
    with patch.object(cc, "_collect_profiles_parallel", return_value=[{"id": 1}]) as c, \
         patch.object(cc, "_normalize_to_contact", side_effect=lambda p, *a, **k: {"n": p["id"]}):
        out = cc.collect_contacts([1, 2, 3, 4, 5, 6, 7], 6)
    assert c.call_args.args[0] == [1, 2, 3, 4, 5, 6]
    assert out == [{"n": 1}]


def test_find_person_by_name():
    with patch.object(cc, "_search_ids", return_value=[9, 10]) as s, \
         patch.object(cc, "_collect_profiles_parallel", return_value=[{"id": 9}]) as c, \
         patch.object(cc, "_normalize_to_contact", return_value={"full_name": "A B"}):
        out = cc.find_person_by_name("A B", "Stripe")
    assert out == {"full_name": "A B"}
    assert s.call_args.args[0] == {"query": {"bool": {"must": [
        {"match_phrase": {"full_name": "A B"}},
        _nested([]),
    ]}}}
    assert c.call_args.args[0] == [9]


def test_find_person_by_name_none():
    with patch.object(cc, "_search_ids", return_value=[]):
        assert cc.find_person_by_name("A B", "Stripe") is None
