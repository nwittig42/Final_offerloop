"""Personalized people ranking (OFF-41): variant split, ranking-profile
points, web school field, fair per-firm pool, outcome events."""
from unittest.mock import MagicMock, patch

import pytest

from app.services import person_ranker as pr
from app.services import ranking_profile as rp


def _ctx(profile=None, slugs=("google",)):
    return {
        "uid": "u1", "profile": profile or {}, "resume_parsed": {},
        "comparison": {"search_titles": []}, "normalized_user": None,
        "dream_company_slugs": list(slugs), "school_slugs": [],
    }


def _cand(first, company, title="Analyst", slug=None):
    slug = slug or f"{first.lower()}-{company.lower()}"
    return {"FirstName": first, "LastName": "X", "LinkedIn": f"https://linkedin.com/in/{slug}",
            "Company": company, "Title": title, "pdlId": ""}


@pytest.fixture()
def flow(monkeypatch):
    """Stub every loader so rank_people_for_user runs on fixed inputs.
    Every candidate gets the same warmth, so personal points decide."""
    state = {"cands": [], "profile": rp.build_ranking_profile({}), "ctx": _ctx()}
    monkeypatch.setattr(pr, "_load_user_context", lambda uid, db: state["ctx"])
    monkeypatch.setattr(pr, "_already_known_keys", lambda uid, db: set())
    monkeypatch.setattr(pr, "_swiped_person_ids", lambda uid, db: set())
    monkeypatch.setattr(pr, "_fetch_candidates", lambda user_ctx, db, **kw: list(state["cands"]))
    monkeypatch.setattr(pr, "load_ranking_profile", lambda uid, db, profile: state["profile"])
    monkeypatch.setattr(pr, "_score_one", lambda ctx, c: {
        "score": 20, "tier": "neutral", "reasons": [], "briefing": "", "warmth_signals": [],
    })
    return state


# ── variant ──────────────────────────────────────────────────────────


def test_variant_is_stable_and_env_controlled(monkeypatch):
    monkeypatch.setenv(pr.V1_PERCENT_ENV, "0")
    assert {pr.ranker_variant(f"u{i}") for i in range(50)} == {"v0"}
    monkeypatch.setenv(pr.V1_PERCENT_ENV, "100")
    assert {pr.ranker_variant(f"u{i}") for i in range(50)} == {"v1"}
    monkeypatch.setenv(pr.V1_PERCENT_ENV, "50")
    assert pr.ranker_variant("abc") == pr.ranker_variant("abc")
    assert pr.model_version_for("abc") == f"person_ranker_{pr.ranker_variant('abc')}"


def test_default_split_puts_some_users_in_each_arm(monkeypatch):
    monkeypatch.delenv(pr.V1_PERCENT_ENV, raising=False)
    arms = [pr.ranker_variant(f"user-{i}") for i in range(200)]
    assert 60 < arms.count("v1") < 140


# ── v1 scoring ───────────────────────────────────────────────────────


def test_v1_floats_a_loop_target_firm(flow, monkeypatch):
    monkeypatch.setenv(pr.V1_PERCENT_ENV, "100")
    flow["profile"] = rp.build_ranking_profile(
        {}, loops=[{"status": "running", "briefParsed": {"companies": ["Evercore"]}}])
    flow["cands"] = [_cand("Ann", "Google"), _cand("Bob", "Evercore")]
    feats: dict = {}
    out = pr.rank_people_for_user("u1", db=object(), limit=10, features_out=feats)
    assert [o["person"]["company"] for o in out] == ["Evercore", "Google"]
    assert feats["bob-evercore"]["target_firm"] == pr.TARGET_FIRM_POINTS
    assert out[0]["score"] == 20 + pr.TARGET_FIRM_POINTS


def test_v1_sources_from_loop_targets(flow, monkeypatch):
    """A student with no dreamCompanies but a Loop gets that firm's deck."""
    monkeypatch.setenv(pr.V1_PERCENT_ENV, "100")
    flow["ctx"] = _ctx(slugs=())
    flow["profile"] = rp.build_ranking_profile(
        {}, loops=[{"status": "idle", "briefParsed": {"companies": ["Lazard"]}}])
    seen = {}
    def fake_fetch(user_ctx, db, **kw):
        seen["slugs"] = list(user_ctx["dream_company_slugs"])
        return [_cand("Cy", "Lazard")]
    monkeypatch.setattr(pr, "_fetch_candidates", fake_fetch)
    out = pr.rank_people_for_user("u1", db=object(), limit=10)
    assert seen["slugs"] == ["lazard"]
    assert len(out) == 1


def test_v1_learns_from_connections_and_passes(flow, monkeypatch):
    monkeypatch.setenv(pr.V1_PERCENT_ENV, "100")
    flow["profile"] = rp.build_ranking_profile(
        {},
        contacts=[{"company": "Bain", "jobTitle": "Consultant", "pipelineStage": "connected"}],
        prefs=[{"signal": "left", "company": "amazon"}] * 3,
    )
    flow["cands"] = [_cand("Amy", "Amazon"), _cand("Ben", "Google"), _cand("Cal", "Bain", "Consultant")]
    out = pr.rank_people_for_user("u1", db=object(), limit=10)
    assert [o["person"]["company"] for o in out] == ["Bain", "Google", "Amazon"]


def test_v1_stage_fit_prefers_recruiters_for_seniors(flow, monkeypatch):
    monkeypatch.setenv(pr.V1_PERCENT_ENV, "100")
    flow["profile"] = rp.build_ranking_profile({"academics": {"graduationYear": "2027"}})
    flow["cands"] = [_cand("Dee", "Google", "Software Engineer"),
                     _cand("Eve", "Google", "University Recruiter")]
    out = pr.rank_people_for_user("u1", db=object(), limit=10)
    assert out[0]["person"]["title"] == "University Recruiter"


def test_declared_dream_firm_is_not_double_paid(flow, monkeypatch):
    monkeypatch.setenv(pr.V1_PERCENT_ENV, "100")
    flow["profile"] = rp.build_ranking_profile({"dreamCompanies": ["Google"]})
    monkeypatch.setattr(pr, "_score_one", lambda ctx, c: {
        "score": 30, "tier": "neutral", "reasons": [], "briefing": "",
        "warmth_signals": ["dream_company"],
    })
    flow["cands"] = [_cand("Fay", "Google")]
    feats: dict = {}
    pr.rank_people_for_user("u1", db=object(), limit=10, features_out=feats)
    assert feats["fay-google"]["target_firm"] == pr.TARGET_FIRM_POINTS - 10


def test_v0_ignores_the_profile(flow, monkeypatch):
    monkeypatch.setenv(pr.V1_PERCENT_ENV, "0")
    flow["profile"] = rp.build_ranking_profile(
        {}, loops=[{"status": "running", "briefParsed": {"companies": ["Evercore"]}}])
    flow["cands"] = [_cand("Ann", "Google"), _cand("Bob", "Evercore")]
    feats: dict = {}
    out = pr.rank_people_for_user("u1", db=object(), limit=10, features_out=feats)
    assert all(o["score"] == 20 for o in out)
    assert feats["bob-evercore"] == {"warmth": 20, "score": 20, "tier": "neutral"}


def test_response_never_leaks_features_or_signals(flow, monkeypatch):
    monkeypatch.setenv(pr.V1_PERCENT_ENV, "100")
    flow["cands"] = [_cand("Ann", "Google")]
    out = pr.rank_people_for_user("u1", db=object(), limit=10)
    assert set(out[0]) == {"person", "score", "tier", "reasons", "briefing"}


# ── loaders ──────────────────────────────────────────────────────────


def test_load_user_context_reads_web_school_field():
    db = MagicMock()
    snap = db.collection.return_value.document.return_value.get.return_value
    snap.exists = True
    snap.to_dict.return_value = {"academics": {"university": "University of Southern California"}}
    ctx = pr._load_user_context("u1", db)
    assert ctx["school_slugs"] == ["usc"]


def test_each_target_firm_gets_a_share_of_the_pool():
    db = MagicMock()
    limits = []
    query = MagicMock()
    query.where.return_value = query
    query.limit.side_effect = lambda n: (limits.append(n), query)[1]
    query.stream.return_value = []
    db.collection.return_value = query
    ctx = _ctx(slugs=[f"firm_{i}" for i in range(5)])
    pr._fetch_candidates(ctx, db, cap=400)
    assert limits == [80] * 5


# ── deck route helper ────────────────────────────────────────────────


def test_ready_first_stays_inside_each_tier_run():
    from app.routes.ranker import _ready_first_within_tier_runs
    deck = [{"tier": t, "person": {"id": i}} for t, i in
            [("warm", "a"), ("warm", "b"), ("neutral", "c"), ("warm", "d"), ("warm", "e")]]
    out = _ready_first_within_tier_runs(deck, {"b", "e"})
    assert [c["person"]["id"] for c in out] == ["b", "a", "c", "e", "d"]


# ── outcome events ───────────────────────────────────────────────────


def test_meeting_and_connection_events_fire_once_with_deck_id():
    from app.services import outbox_service as ob
    with patch("app.utils.recommendation_events.log_recommendation_event") as log:
        data = {"company": "Bain", "jobTitle": "Consultant", "email": "c@bain.com"}
        ob._log_connection_outcomes("u1", "ranker_cal-bain", data,
                                    {"meetingScheduledAt": "t", "connectedAt": "t"})
        types = [c.args[0] for c in log.call_args_list]
        assert types == ["meeting_scheduled", "connection_made"]
        kw = log.call_args_list[0].kwargs
        assert kw["contact_id"] == "cal-bain"
        assert kw["attribution_source"] == "ranker_deck"
        assert kw["doc_id"] == "meeting_u1_ranker_cal-bain"
        log.reset_mock()
        ob._log_connection_outcomes("u1", "abc", data, {"pipelineStage": "replied"})
        log.assert_not_called()


# ── route: deck_served impression + variant on swipe events ──────────

FAKE_USER = {"uid": "test-uid", "email": "tester@example.com"}


@pytest.fixture(scope="module")
def http_client():
    import importlib
    import sys
    stubbed = False
    try:
        importlib.import_module("weasyprint")
    except (ImportError, OSError):
        # WeasyPrint needs the system pango/gobject libs; these route tests
        # never render a PDF, so a stub keeps the app importable without them.
        sys.modules["weasyprint"] = MagicMock()
        stubbed = True
    try:
        with patch("firebase_admin._apps", {"[DEFAULT]": MagicMock()}):
            from backend.wsgi import create_app
            app = create_app()
            app.config["TESTING"] = True
            yield app.test_client()
    finally:
        if stubbed:
            sys.modules.pop("weasyprint", None)


@pytest.fixture()
def authed():
    with patch("firebase_admin._apps", {"[DEFAULT]": MagicMock()}), \
         patch("firebase_admin.auth.verify_id_token", return_value=FAKE_USER):
        yield


def _deck_item(pid, company):
    return {"person": {"id": pid, "company": company, "title": "Analyst"},
            "score": 30, "tier": "neutral", "reasons": [], "briefing": ""}


def test_candidates_logs_one_deck_served_row(http_client, authed):
    def fake_rank(**kw):
        kw["features_out"]["ann"] = {"warmth": 20, "target_firm": 12}
        return [_deck_item("ann", "Evercore")]
    db = MagicMock()
    db.get_all.return_value = []
    with patch("backend.app.routes.ranker.get_db", return_value=db), \
         patch("backend.app.routes.ranker.rank_people_for_user", side_effect=fake_rank), \
         patch("backend.app.routes.ranker.log_recommendation_event") as log:
        r = http_client.get("/api/ranker/candidates?limit=10&prefetch=0",
                            headers={"Authorization": "Bearer fake"})
    assert r.status_code == 200
    deck_id = r.get_json()["deck_id"]
    (call,) = [c for c in log.call_args_list if c.kwargs.get("event_type") == "deck_served"]
    kw = call.kwargs
    assert kw["doc_id"] == f"deck_test-uid_{deck_id}"
    assert kw["model_version"] == pr.model_version_for("test-uid")
    assert kw["extra"]["items"][0]["features"] == {"warmth": 20, "target_firm": 12}


def test_feedback_returns_200_and_tags_the_variant(http_client, authed):
    """A swipe stores the firm (for negatives) and tags the event with the arm."""
    db = MagicMock()
    snap = db.collection.return_value.document.return_value.get.return_value
    snap.exists = True
    snap.to_dict.return_value = {"company": "evercore", "title": "analyst"}
    with patch("backend.app.routes.ranker.get_db", return_value=db), \
         patch("backend.app.routes.ranker.log_recommendation_event") as log:
        r = http_client.post("/api/ranker/feedback",
                             json={"candidate_id": "ann", "signal": "left", "deck_id": "d1", "rank": 0},
                             headers={"Authorization": "Bearer fake"})
    assert r.status_code == 200
    assert log.call_args.kwargs["variant"] == pr.ranker_variant("test-uid")
    written = db.collection.return_value.document.return_value.collection.return_value \
        .document.return_value.set.call_args[0][0]
    assert written["company"] == "evercore"
