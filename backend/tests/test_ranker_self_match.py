"""Never recommend a user to themselves (OFF-60)."""
from unittest.mock import MagicMock

import pytest

from app.services import people_deck_cache as pdc
from app.services import person_ranker as pr
from app.services import ranking_profile as rp

GIO = "https://www.linkedin.com/in/giovanna-hughes/"


def _cand(slug, first="X"):
    return {"FirstName": first, "LastName": "Y", "LinkedIn": f"https://linkedin.com/in/{slug}",
            "Company": "Google", "Title": "Analyst", "pdlId": ""}


def test_own_id_matches_candidate_id():
    got = pr.own_candidate_id({"profile": {"linkedinUrl": GIO}})
    assert got == pr._candidate_id({"LinkedIn": GIO}) == "giovanna-hughes"


def test_own_id_falls_back_to_top_level_and_none():
    assert pr.own_candidate_id({"linkedinUrl": GIO}) == "giovanna-hughes"
    assert pr.own_candidate_id({"profile": {"linkedinUrl": ""}, "linkedinUrl": GIO}) == "giovanna-hughes"
    assert pr.own_candidate_id({}) is None
    assert pr.own_candidate_id({"profile": {}}) is None


def test_ranking_excludes_own_profile(monkeypatch):
    ctx = {"uid": "u1", "profile": {"profile": {"linkedinUrl": GIO}}, "resume_parsed": {},
           "comparison": {"search_titles": []}, "normalized_user": None,
           "dream_company_slugs": ["google"], "school_slugs": []}
    monkeypatch.setattr(pr, "_load_user_context", lambda uid, db: ctx)
    monkeypatch.setattr(pr, "_already_known_keys", lambda uid, db: set())
    monkeypatch.setattr(pr, "_swiped_person_ids", lambda uid, db: set())
    monkeypatch.setattr(pr, "_fetch_candidates",
                        lambda user_ctx, db, **kw: [_cand("giovanna-hughes"), _cand("bob-lee")])
    monkeypatch.setattr(pr, "load_ranking_profile",
                        lambda uid, db, profile: rp.build_ranking_profile({}))
    monkeypatch.setattr(pr, "_score_one", lambda c, x: {
        "score": 20, "tier": "neutral", "reasons": [], "briefing": "", "warmth_signals": []})
    out = pr.rank_people_for_user("u1", db=object(), limit=10)
    assert [o["person"]["id"] for o in out] == ["bob-lee"]


def _snap(doc_id, data):
    s = MagicMock()
    s.id, s.exists = doc_id, True
    s.to_dict.return_value = data
    return s


def test_cached_deck_excludes_own_profile(monkeypatch):
    items = [{"c": {"person": {"id": i}}, "f": {}, "rs": r}
             for i, r in (("giovanna-hughes", 99), ("bob-lee", 50))]
    deck = _snap(pdc.DECK_DOC_ID, {"items": items})
    user = _snap("u1", {"profile": {"linkedinUrl": GIO}})
    db = MagicMock()
    db.get_all.return_value = [deck, user]
    monkeypatch.setattr(pdc, "needs_rebuild", lambda *a, **k: False)
    monkeypatch.setattr(pr, "model_version_for", lambda uid: "v")
    candidates, feats = pdc.serve("u1", db, 10, set())
    assert [c["person"]["id"] for c in candidates] == ["bob-lee"]
    assert "giovanna-hughes" not in feats
