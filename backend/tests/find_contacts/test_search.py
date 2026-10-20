from unittest.mock import MagicMock

import pytest

from app.services.find_contacts import search as S
from app.services.find_contacts.search import Budget, find_slot_candidates

FACTS = {"company": "Acme", "team": "Payments Risk", "office_city": "New York",
         "function": "other", "named_people": []}
SPECS = {
    "leader": {"titles": ["Vice President"], "keywords": ["payments"]},
    "process": {"titles": ["Director"], "keywords": ["payments"]},
    "recruiter": {"titles": ["Campus Recruiter"], "keywords": []},
}


def _c(first, last, title, company="Acme"):
    return {"FirstName": first, "LastName": last, "Title": title, "Company": company,
            "City": "", "LinkedIn": f"https://linkedin.com/in/{first}{last}".lower(),
            "College": "", "PhotoUrl": ""}


@pytest.fixture
def m(monkeypatch):
    mocks = {
        "warehouse": MagicMock(return_value=([], 0, [], {})),
        "search_people": MagicMock(return_value=[]),
        "collect": MagicMock(return_value=[]),
        "by_name": MagicMock(return_value=None),
        "fullenrich": MagicMock(return_value=([], 0, [], {})),
    }
    monkeypatch.setattr(S, "engine_search_contacts", mocks["warehouse"])
    monkeypatch.setattr(S, "search_people", mocks["search_people"])
    monkeypatch.setattr(S, "collect_contacts", mocks["collect"])
    monkeypatch.setattr(S, "find_person_by_name", mocks["by_name"])
    monkeypatch.setattr(S, "search_contacts_from_prompt", mocks["fullenrich"])
    return mocks


def test_budget():
    b = Budget(collects=3, fullenrich_calls=1)
    assert b.take_collects(2) == 2
    assert b.take_collects(2) == 1
    assert b.take_collects(2) == 0
    assert b.take_fullenrich() is True
    assert b.take_fullenrich() is False


def test_named_person_first(m):
    m["by_name"].return_value = _c("Kristen", "Rietow", "Vice President")
    facts = dict(FACTS, named_people=[{"name": "Kristen Rietow", "role": "VP"}])
    budget = Budget()
    out = find_slot_candidates(facts, SPECS, budget)
    m["by_name"].assert_called_once_with("Kristen Rietow", "Acme")
    assert out["process"][0]["FirstName"] == "Kristen"
    assert out["process"][0]["_found_by"] == "named"
    assert budget.collects == 5  # the hit counted as one collect
    # her slot never searched further: process titles never reach Coresignal
    assert all(c.args[1] != ["Director"] for c in m["search_people"].call_args_list)

    from app.services.find_contacts.scoring import fill_slots
    people = fill_slots(out, facts, SPECS, None)
    kristen = [p for p in people if p["name"] == "Kristen Rietow"][0]
    assert kristen["slot"] == "process" and kristen["label"] == "Named in the posting"


@pytest.mark.parametrize("role,slot", [
    ("VP", "process"), ("Managing Director", "process"), ("Hiring Manager", "process"),
    ("Head of Payments Risk", "leader"), ("Chief Technology Officer", "leader"),
    ("Campus Recruiter", "recruiter"), ("Talent Acquisition Partner", "recruiter"),
])
def test_named_role_to_slot(role, slot):
    assert S.named_slot(role, SPECS) == slot


def test_named_recruiter_without_recruiter_slot_goes_to_process():
    startup = {"leader": SPECS["leader"], "process": SPECS["process"]}
    assert S.named_slot("Recruiter", startup) == "process"


def test_named_person_missing_falls_back(m):
    facts = dict(FACTS, named_people=[{"name": "Nobody Here", "role": "VP"}])
    m["warehouse"].return_value = ([_c("Al", "Bo", "Director")], 0, [], {})
    budget = Budget()
    out = find_slot_candidates(facts, {"process": SPECS["process"]}, budget)
    assert out["process"][0]["_found_by"] == "warehouse"
    assert budget.collects == 6


def test_rounds_widen_in_order(m):
    specs = {"leader": SPECS["leader"]}
    m["search_people"].side_effect = lambda company, titles, keywords=None, city=None, page_size=10: (
        [] if city else [101, 102, 103])
    m["collect"].return_value = [_c("Ann", "Lee", "VP Payments")]
    out = find_slot_candidates(FACTS, specs, Budget())
    assert out["leader"][0]["_found_by"] == "coresignal_r2"
    calls = m["search_people"].call_args_list
    assert len(calls) == 2  # r1 then r2; r3/r4 never called
    assert calls[0].kwargs == {"keywords": ["payments"], "city": "New York"}
    assert calls[1].kwargs == {"keywords": ["payments"], "city": None}
    m["collect"].assert_called_once_with([101, 102], 2)
    m["fullenrich"].assert_not_called()


def test_duplicate_rounds_skipped(m):
    # recruiter: no keywords and no city -> r1, r2, r3 all equal r4
    facts = dict(FACTS, office_city=None)
    find_slot_candidates(facts, {"recruiter": SPECS["recruiter"]}, Budget())
    assert len(m["search_people"].call_args_list) == 1


def test_stops_when_slots_full_from_warehouse(m):
    m["warehouse"].side_effect = lambda parsed, n, **kw: (
        [_c(parsed["title_variations"][0].replace(" ", ""), "X", parsed["title_variations"][0])], 0, [], {})
    out = find_slot_candidates(FACTS, SPECS, Budget())
    assert all(out[s][0]["_found_by"] == "warehouse" for s in SPECS)
    for call in m["warehouse"].call_args_list:
        assert call.kwargs == {"allow_vendor": False, "source": "find_contacts"}
    m["search_people"].assert_not_called()
    m["collect"].assert_not_called()
    m["fullenrich"].assert_not_called()


def test_budget_caps_collects(m):
    counter = iter(range(1000))
    m["search_people"].side_effect = lambda *a, **k: [next(counter) for _ in range(5)]
    collected = []

    def collect_track(ids, limit):
        collected.extend(ids[:limit])
        return []  # never fills, forces every round
    m["collect"].side_effect = collect_track
    find_slot_candidates(FACTS, SPECS, Budget())
    assert len(collected) <= 6
    assert all(c.args[1] <= 2 for c in m["collect"].call_args_list)


def test_fullenrich_fallback_once(m):
    m["fullenrich"].return_value = ([_c("Fe", "One", "Campus Recruiter")], 0, [], {})
    out = find_slot_candidates(FACTS, SPECS, Budget())
    m["fullenrich"].assert_called_once()
    parsed = m["fullenrich"].call_args.args[0]
    assert parsed["companies"] == [{"name": "Acme"}]
    assert set(parsed["title_variations"]) == {"Vice President", "Director", "Campus Recruiter"}
    assert out["recruiter"][0]["_found_by"] == "fullenrich"
    assert out["leader"] == [] and out["process"] == []


def test_same_person_not_in_two_slots(m):
    ann = _c("Ann", "Lee", "Vice President Director")
    m["warehouse"].return_value = ([ann], 0, [], {})
    out = find_slot_candidates(FACTS, {"leader": SPECS["leader"], "process": SPECS["process"]}, Budget())
    assert len(out["leader"]) == 1
    assert out["process"] == [] or out["process"][0]["FirstName"] != "Ann"


def test_vendor_errors_do_not_crash(m):
    m["warehouse"].side_effect = RuntimeError("boom")
    m["search_people"].side_effect = RuntimeError("boom")
    m["fullenrich"].side_effect = RuntimeError("boom")
    out = find_slot_candidates(FACTS, SPECS, Budget())
    assert out == {"leader": [], "process": [], "recruiter": []}
