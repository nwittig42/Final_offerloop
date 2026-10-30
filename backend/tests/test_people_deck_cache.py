"""Saved People deck (OFF-41): built in the background, served instantly,
fresh on every visit, never empty."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.services import people_deck_cache as dc
from app.services import person_ranker as pr

NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def _item(pid, rs):
    return {"c": {"person": {"id": pid}, "score": 20, "tier": "neutral", "reasons": [], "briefing": ""},
            "f": {"warmth": 20, "rank_score": rs}, "rs": rs}


def _doc(**kw):
    base = {"items": [_item("a", 50), _item("b", 40), _item("c", 30), _item("d", 20)],
            "built_at": NOW.isoformat(), "fingerprint": dc.profile_fingerprint({}),
            "model_version": "person_ranker_v1", "stale": False, "shown": {}, "removed": []}
    base.update(kw)
    return base


def _ids(items):
    return [it["c"]["person"]["id"] for it in items]


# ── pick ─────────────────────────────────────────────────────────────


def test_pick_keeps_rank_order():
    assert _ids(dc.pick(_doc(), 3)) == ["a", "b", "c"]


def test_pick_drops_acted_on_and_on_screen_people():
    assert _ids(dc.pick(_doc(removed=["a"]), 10, exclude_ids={"c"})) == ["b", "d"]


def test_people_shown_without_action_sink_so_visits_feel_new():
    # a (50) has been shown three times and sinks below c (30), who hasn't.
    assert 50 - 3 * dc.SEEN_PENALTY < 30
    assert _ids(dc.pick(_doc(shown={"a": 3}), 2)) == ["b", "c"]


# ── staleness ────────────────────────────────────────────────────────


def test_fresh_list_is_not_rebuilt():
    assert not dc.needs_rebuild(_doc(), {}, "person_ranker_v1", now=NOW)


@pytest.mark.parametrize("change", [
    {"stale": True},
    {"model_version": "person_ranker_v0"},
    {"built_at": (NOW - timedelta(days=2)).isoformat()},
    {"fingerprint": "something-else"},
])
def test_rebuild_triggers(change):
    assert dc.needs_rebuild(_doc(**change), {}, "person_ranker_v1", now=NOW)


def test_profile_edit_changes_the_fingerprint():
    a = dc.profile_fingerprint({"dreamCompanies": ["Stripe"]})
    b = dc.profile_fingerprint({"dreamCompanies": ["Stripe", "Ramp"]})
    assert a != b
    assert dc.profile_fingerprint({"credits": 5}) == dc.profile_fingerprint({"credits": 9})


# ── serve ────────────────────────────────────────────────────────────


def _snap(doc_id, data, exists=True):
    s = MagicMock()
    s.id = doc_id
    s.exists = exists
    s.to_dict.return_value = data
    return s


def _db(deck_doc, profile=None):
    db = MagicMock()
    db.get_all.return_value = [
        _snap("u1", profile or {}),
        _snap(dc.DECK_DOC_ID, deck_doc or {}, exists=deck_doc is not None),
    ]
    return db


def test_serve_without_a_saved_list_queues_a_build_and_falls_back(monkeypatch):
    queued = []
    monkeypatch.setattr(dc, "schedule_build", lambda uid, reason="": queued.append(reason))
    assert dc.serve("u1", _db(None), 10) is None
    assert queued == ["missing"]


def test_serve_returns_saved_people_and_counts_the_showing(monkeypatch):
    monkeypatch.setattr(dc, "schedule_build", lambda *a, **k: None)
    monkeypatch.setattr(pr, "model_version_for", lambda uid: "person_ranker_v1")
    db = _db(_doc())
    cands, feats = dc.serve("u1", db, 2)
    assert _ids([{"c": c} for c in cands]) == ["a", "b"]
    assert feats["a"]["served_from"] == "saved_deck"
    db.collection.return_value.document.return_value.collection.return_value \
        .document.return_value.set.assert_called_once()


def test_load_more_does_not_count_a_showing(monkeypatch):
    monkeypatch.setattr(dc, "schedule_build", lambda *a, **k: None)
    monkeypatch.setattr(pr, "model_version_for", lambda uid: "person_ranker_v1")
    db = _db(_doc())
    cands, _ = dc.serve("u1", db, 2, exclude_ids={"a", "b"})
    assert [c["person"]["id"] for c in cands] == ["c", "d"]
    db.collection.return_value.document.return_value.collection.return_value \
        .document.return_value.set.assert_not_called()


def test_stale_list_still_serves_while_it_rebuilds(monkeypatch):
    queued = []
    monkeypatch.setattr(dc, "schedule_build", lambda uid, reason="": queued.append(reason))
    monkeypatch.setattr(pr, "model_version_for", lambda uid: "person_ranker_v1")
    cands, _ = dc.serve("u1", _db(_doc(stale=True)), 2)
    assert len(cands) == 2 and queued == ["refresh"]


def test_exhausted_list_falls_back_to_live(monkeypatch):
    monkeypatch.setattr(dc, "schedule_build", lambda *a, **k: None)
    monkeypatch.setattr(pr, "model_version_for", lambda uid: "person_ranker_v1")
    assert dc.serve("u1", _db(_doc(removed=["a", "b", "c", "d"])), 5) is None


# ── build ────────────────────────────────────────────────────────────


def test_build_saves_the_ranked_list_and_keeps_live_swipes(monkeypatch):
    def fake_rank(**kw):
        kw["features_out"].update({"a": {"rank_score": 50}, "b": {"rank_score": 40}})
        assert kw["limit"] == dc.CACHE_SIZE and kw["max_limit"] == dc.CACHE_SIZE
        return [{"person": {"id": "a"}, "score": 20}, {"person": {"id": "b"}, "score": 20}]
    monkeypatch.setattr(pr, "rank_people_for_user", fake_rank)
    monkeypatch.setattr(pr, "model_version_for", lambda uid: "person_ranker_v1")
    db = MagicMock()
    deck_ref = db.collection.return_value.document.return_value.collection.return_value.document.return_value
    deck_ref.get.return_value = _snap(dc.DECK_DOC_ID, {"removed": ["b", "gone"]})
    assert dc.build_deck("u1", db) == 2
    saved = deck_ref.set.call_args[0][0]
    assert [it["rs"] for it in saved["items"]] == [50, 40]
    assert saved["removed"] == ["b"]


def test_one_build_at_a_time_per_student(monkeypatch):
    started = []
    monkeypatch.setattr(dc._build_pool, "submit", lambda fn: started.append(fn))
    assert dc.schedule_build("u9", "resume") is True
    assert dc.schedule_build("u9", "loop") is False
    dc._building.discard("u9")


# ── never empty ──────────────────────────────────────────────────────


def test_starter_firms_follow_the_students_lane():
    firms = pr.default_seed_firms({"careerTrack": "Investment Banking"})
    assert firms[0] == "Goldman Sachs" and "Evercore" in firms


def test_starter_firms_exist_with_nothing_to_go_on():
    assert len(pr.default_seed_firms({})) >= 8


def test_ranker_allows_the_long_saved_list(monkeypatch):
    seen = {}
    monkeypatch.setattr(pr, "_load_user_context", lambda uid, db: {
        "uid": uid, "profile": {}, "resume_parsed": {}, "comparison": {}, "normalized_user": None,
        "dream_company_slugs": ["google"], "school_slugs": []})
    monkeypatch.setattr(pr, "_already_known_keys", lambda uid, db: set())
    monkeypatch.setattr(pr, "_swiped_person_ids", lambda uid, db: set())
    monkeypatch.setattr(pr, "ranker_variant", lambda uid: "v0")
    monkeypatch.setattr(pr, "_fetch_candidates", lambda ctx, db, **kw: [
        {"FirstName": f"P{i}", "LastName": "X", "LinkedIn": f"https://linkedin.com/in/p{i}",
         "Company": f"Co{i % 30}", "Title": "Analyst", "pdlId": ""} for i in range(300)])
    monkeypatch.setattr(pr, "_score_one", lambda ctx, c: {
        "score": 10, "tier": "cold", "reasons": [], "briefing": "", "warmth_signals": []})
    assert len(pr.rank_people_for_user("u1", object(), limit=200, max_limit=200)) == 200
    assert len(pr.rank_people_for_user("u1", object(), limit=200)) == pr.MAX_RANKED_LIMIT


# ── triggers ─────────────────────────────────────────────────────────


def test_resume_save_starts_the_deck():
    from app.routes import resume as resume_routes
    with patch.object(resume_routes, "get_db", return_value=MagicMock()), \
         patch("app.services.people_deck_cache.schedule_build") as sched:
        assert resume_routes.save_resume_to_firebase("u1", "text", "url", {"name": "A"}) is True
    sched.assert_called_once_with("u1", "resume")
