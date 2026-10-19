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
        "link_chapterYear": 2017, "link_classOfYear": None, "personKey": "email:jamie@gmail.com",
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
