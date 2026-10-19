"""Org alumni directories (OFF-42): prompt matching, membership gating,
member -> contact conversion, filtering and ranking."""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import org_directory as od

TKE = {
    "id": "usc_tke", "name": "USC Tau Kappa Epsilon", "shortName": "USC TKE",
    "school": "University of Southern California", "type": "fraternity",
    "memberCount": 2127, "aliases": ["tke", "tau kappa epsilon", "teke"],
}
ACTIVE = {"orgMemberships": {"usc_tke": {"status": "active"}}}


def _member(**kw):
    m = {
        "firstName": "Jamie", "lastName": "Park", "email": "jamie@gmail.com",
        "directoryHeadline": "Goldman Sachs Investment Banking Analyst",
        "link_membershipType": "alumni", "link_membershipStatus": "active",
        "link_chapterYear": 2017, "link_classOfYear": None, "personKey": "email:jamie@gmail.com", "phone": "(213) 555-0100",
    }
    m.update(kw)
    return m


def test_match_strips_alias_and_filler():
    assert od.match_orgs_in_prompt("TKE alumni at Goldman Sachs", [TKE]) == (["usc_tke"], "Goldman Sachs")
    assert od.match_orgs_in_prompt("tke alumni", [TKE]) == (["usc_tke"], "")
    assert od.match_orgs_in_prompt("Analysts at JPMorgan who are USC TKE brothers", [TKE]) == (["usc_tke"], "Analysts at JPMorgan")


def test_alias_needs_word_boundary():
    assert od.match_orgs_in_prompt("tkexyz analysts", [TKE]) == ([], "tkexyz analysts")


def test_resolve_requires_active_membership():
    pending = {"orgMemberships": {"usc_tke": {"status": "pending"}}}
    assert od.resolve_org_targets("TKE alumni", None, pending, [TKE]) == ([], "TKE alumni")
    assert od.resolve_org_targets("anyone", ["usc_tke"], {}, [TKE]) == ([], "anyone")
    assert od.resolve_org_targets("TKE alumni at Bain", None, ACTIVE, [TKE]) == (["usc_tke"], "Bain")
    # Filter pill alone keeps the typed prompt as-is.
    assert od.resolve_org_targets("Consultants", ["usc_tke"], ACTIVE, [TKE]) == (["usc_tke"], "Consultants")
    # Unknown org ids in the filter are ignored.
    assert od.resolve_org_targets("x", ["nope"], ACTIVE, [TKE]) == ([], "x")


def test_member_contact_excludes_deceased_undergrad_unreachable():
    assert od.member_contact(_member(link_membershipStatus="deceased"), TKE) is None
    assert od.member_contact(_member(link_membershipType="undergrad"), TKE) is None
    # Lost with no recovered email or LinkedIn: the directory email is stale.
    assert od.member_contact(_member(link_membershipStatus="lost"), TKE) is None
    lost_found = od.member_contact(_member(link_membershipStatus="lost", linkedinUrl="https://linkedin.com/in/jp"), TKE)
    assert lost_found["Email"] == "" and lost_found["LinkedIn"]


def test_member_contact_prefers_work_email_and_badge():
    c = od.member_contact(_member(workEmail="jpark@gs.com", workEmailStatus="DELIVERABLE"), TKE)
    assert c["Email"] == "jpark@gs.com"
    assert c["EmailVerified"] is True
    assert c["OrgBadge"] == "USC TKE Alumni '17"
    assert c["College"] == "University of Southern California"
    assert c["WorkSummary"] == "Goldman Sachs Investment Banking Analyst"
    assert c["Phone"] == ""  # never surfaced, even when the directory has one
    no_year = od.member_contact(_member(link_chapterYear=None), TKE)
    assert no_year["OrgBadge"] == "USC TKE Alumni"
    class_year = od.member_contact(_member(link_chapterYear=None, link_classOfYear=2009), TKE)
    assert class_year["OrgBadge"] == "USC TKE Alumni '09"


def test_filter_by_query_company_and_title():
    gs = od.member_contact(_member(), TKE)
    bain = od.member_contact(_member(email="b@x.com", directoryHeadline="Bain & Company Consultant"), TKE)
    cs = od.member_contact(_member(email="c@x.com", directoryHeadline="x", coresignalCompany="Goldman Sachs", coresignalTitle="Vice President"), TKE)
    everyone = [gs, bain, cs]
    assert od.filter_by_query(everyone, {}) == everyone
    assert od.filter_by_query(everyone, {"companies": [{"name": "Goldman Sachs"}]}) == [gs, cs]
    assert od.filter_by_query(everyone, {"companies": [{"name": "Goldman Sachs"}], "title_variations": ["vice president"]}) == [cs]
    # Locations are ignored (the directory has none).
    assert od.filter_by_query(everyone, {"locations": ["New York"]}) == everyone


def test_rank_email_first_then_recent():
    old = od.member_contact(_member(email="old@x.com", link_chapterYear=1990), TKE)
    new = od.member_contact(_member(email="new@x.com", link_chapterYear=2020), TKE)
    verified = od.member_contact(_member(email="v@x.com", workEmail="v@co.com", workEmailStatus="DELIVERABLE", link_chapterYear=1980), TKE)
    no_email = od.member_contact(_member(email="", linkedinUrl="https://linkedin.com/in/z", link_chapterYear=2024), TKE)
    assert od.rank([no_email, old, new, verified]) == [verified, new, old, no_email]


def test_search_org_directory_joins_links_and_people():
    od._members_cache.clear()
    db = MagicMock()
    link = MagicMock()
    link.to_dict.return_value = {"personKey": "p1", "orgId": "usc_tke", "membershipType": "alumni",
                                 "membershipStatus": "active", "chapterYear": 2017}
    db.collection.return_value.where.return_value.stream.return_value = [link]
    person = MagicMock(exists=True, id="p1")
    person.to_dict.return_value = {"firstName": "Jamie", "lastName": "Park", "email": "jamie@gmail.com",
                                   "directoryHeadline": "Goldman Sachs Analyst"}
    db.get_all.return_value = [person]
    out = od.search_org_directory(db, ["usc_tke"], {"companies": [{"name": "Goldman"}]}, [TKE])
    assert [c["Email"] for c in out] == ["jamie@gmail.com"]
    assert out[0]["OrgBadge"] == "USC TKE Alumni '17"
    od._members_cache.clear()


def test_active_org_ids_ignores_garbage():
    assert od.active_org_ids(None) == []
    assert od.active_org_ids({"orgMemberships": "x"}) == []
    assert od.active_org_ids({"orgMemberships": {"a": {"status": "active"}, "b": {"status": "pending"}, "c": 1}}) == ["a"]


def test_titles_only_apply_when_a_role_was_typed():
    gs_vp = od.member_contact(_member(email="v@x.com", directoryHeadline="Goldman Sachs Vice President"), TKE)
    gs_md = od.member_contact(_member(email="m@x.com", directoryHeadline="Goldman Sachs Managing Director"), TKE)
    parsed = {"companies": [{"name": "Goldman Sachs", "matched_titles": ["Investment Banking Analyst"]}],
              "title_variations": ["Investment Banking Analyst", "Vice President"]}
    # "Goldman Sachs" alone: the parser's invented titles must not narrow it.
    assert od.filter_by_query([gs_vp, gs_md], parsed, "Goldman Sachs") == [gs_vp, gs_md]
    assert od.filter_by_query([gs_vp, gs_md], parsed, "people at Goldman Sachs") == [gs_vp, gs_md]
    # The parser expands "Goldman" / "GS"; those stay company words, not roles.
    assert od.filter_by_query([gs_vp, gs_md], parsed, "Goldman") == [gs_vp, gs_md]
    assert od.filter_by_query([gs_vp, gs_md], parsed, "GS") == [gs_vp, gs_md]
    # A typed role does narrow, and the generic tail ("President") counts.
    assert od.filter_by_query([gs_vp, gs_md], parsed, "VPs at Goldman Sachs") == [gs_vp]
    assert od.filter_by_query([gs_vp, gs_md], parsed, "vice presidents at Goldman") == [gs_vp]


# ---------------------------------------------------------------------------
# /api/prompt-search in org mode
# ---------------------------------------------------------------------------
import json  # noqa: E402

import pytest  # noqa: E402
from unittest.mock import patch  # noqa: E402

FAKE_USER = {"uid": "test-user-id", "email": "test@example.com"}


@pytest.fixture
def search_client():
    from flask import Flask
    from app.routes.runs import runs_bp
    app = Flask(__name__)
    app.register_blueprint(runs_bp)
    with patch("firebase_admin._apps", {"[DEFAULT]": MagicMock()}), \
         patch("firebase_admin.auth.verify_id_token", return_value=FAKE_USER):
        yield app.test_client()


def _search(client, prompt, user_data, members, parsed=None, filters=None, saved_emails=()):
    db = MagicMock()
    user_doc = MagicMock(exists=True)
    user_doc.to_dict.return_value = {"subscriptionTier": "pro", "credits": 500, **user_data}
    db.collection.return_value.document.return_value.get.return_value = user_doc
    exclusion = {"identity_set": set(), "email_set": set(saved_emails), "linkedin_set": set(), "name_company_set": set()}
    provider = MagicMock(side_effect=AssertionError("provider ladder called in org mode"))
    with patch("app.routes.runs.get_db", return_value=db), \
         patch("app.routes.runs.check_and_reset_credits", return_value=500), \
         patch("app.routes.runs._build_exclusion_data_from_firestore", return_value=exclusion), \
         patch("app.routes.runs.parse_search_prompt_structured", return_value=parsed or {"confidence": "low"}) as parse, \
         patch("app.routes.runs.coresignal_client.search_contacts_from_prompt", provider), \
         patch("app.routes.runs.deduct_credits_atomic", return_value=(True, 440)), \
         patch("app.services.org_directory.list_orgs", return_value=[TKE]), \
         patch("app.services.org_directory.load_org_members", return_value=members):
        body = {"prompt": prompt, "batchSize": 6, "mode": "preview"}
        if filters:
            body["filters"] = filters
        r = client.post("/api/prompt-search", data=json.dumps(body), content_type="application/json",
                        headers={"Authorization": "Bearer t"})
    return r, parse, provider


def test_prompt_search_tke_alumni_alone_is_not_too_vague(search_client):
    r, parse, provider = _search(search_client, "TKE alumni", ACTIVE, [_member()])
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert [c["OrgBadge"] for c in body["contacts"]] == ["USC TKE Alumni '17"]
    assert body["parsed_query"]["org_ids"] == ["usc_tke"]
    parse.assert_not_called()  # nothing left to parse once "TKE alumni" is stripped
    provider.assert_not_called()


def test_prompt_search_no_org_match_does_not_fall_through(search_client):
    parsed = {"confidence": "high", "companies": [{"name": "Bain & Company"}], "title_variations": []}
    r, _, provider = _search(search_client, "TKE alumni at Bain & Company", ACTIVE, [_member()], parsed=parsed)
    body = r.get_json()
    assert r.status_code == 200
    assert body["contacts"] == []
    assert "No USC TKE alumni matched" in body["message"]
    provider.assert_not_called()


def test_prompt_search_skips_already_saved_members(search_client):
    members = [_member(email="saved@x.com", link_chapterYear=2020), _member(email="new@x.com", link_chapterYear=2010)]
    r, _, _ = _search(search_client, "anything", ACTIVE, members, filters={"organizations": ["usc_tke"]},
                      parsed={"confidence": "low"}, saved_emails=("saved@x.com",))
    assert [c["Email"] for c in r.get_json()["contacts"]] == ["new@x.com"]


def test_prompt_search_all_members_saved_says_so(search_client):
    r, _, _ = _search(search_client, "TKE alumni", ACTIVE, [_member(email="saved@x.com")], saved_emails=("saved@x.com",))
    body = r.get_json()
    assert body["contacts"] == []
    assert "already in your tracker" in body["message"]


def test_prompt_search_non_member_gets_normal_search(search_client):
    r, parse, _ = _search(search_client, "TKE alumni", {}, [_member()],
                          parsed={"confidence": "high", "companies": [], "title_variations": ["analyst"]})
    assert r.status_code == 200, r.get_json()
    parse.assert_called_once_with("TKE alumni")  # full prompt, normal parse
    body = r.get_json()
    assert "org_ids" not in body["parsed_query"]
    assert all(c.get("source") != "org_directory" for c in body["contacts"])
    assert body["parsed_query"]["org_hint"]["id"] == "usc_tke"  # offered the join


def test_prompt_search_vague_org_name_from_non_member_offers_join(search_client):
    r, _, provider = _search(search_client, "TKE alumni", {}, [_member()], parsed={"confidence": "low"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["contacts"] == []
    assert body["parsed_query"]["org_hint"] == {"id": "usc_tke", "name": "USC Tau Kappa Epsilon", "shortName": "USC TKE"}
    assert "members-only" in body["message"]
    provider.assert_not_called()


def test_join_hint_only_for_orgs_you_are_not_in():
    assert od.join_hint("TKE alumni", ACTIVE, [TKE]) is None
    assert od.join_hint("Goldman analysts", {}, [TKE]) is None
    assert od.join_hint("tke brothers at Bain", {}, [TKE])["id"] == "usc_tke"
