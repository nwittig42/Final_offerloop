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


def test_v1_floats_a_target_firm(flow, monkeypatch):
    monkeypatch.setenv(pr.V1_PERCENT_ENV, "100")
    flow["profile"] = rp.build_ranking_profile({"dreamCompanies": ["Evercore"]})
    flow["cands"] = [_cand("Ann", "Google"), _cand("Bob", "Evercore")]
    feats: dict = {}
    out = pr.rank_people_for_user("u1", db=object(), limit=10, features_out=feats)
    assert [o["person"]["company"] for o in out] == ["Evercore", "Google"]
    assert feats["bob-evercore"]["target_firm"] == pr.TARGET_FIRM_POINTS
    assert feats["bob-evercore"]["rank_score"] == 20 + pr.TARGET_FIRM_POINTS
    # The score cards draw is untouched; only the order changed.
    assert [o["score"] for o in out] == [20, 20]


def test_v1_sources_from_saved_job_targets(flow, monkeypatch):
    """A student with no dreamCompanies but a saved job gets that firm's deck."""
    monkeypatch.setenv(pr.V1_PERCENT_ENV, "100")
    flow["ctx"] = _ctx(slugs=())
    flow["profile"] = rp.build_ranking_profile({}, saved_jobs=[{"company": "Lazard"}])
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
    flow["profile"] = rp.build_ranking_profile({"dreamCompanies": ["Evercore"]})
    flow["cands"] = [_cand("Ann", "Google"), _cand("Bob", "Evercore")]
    feats: dict = {}
    out = pr.rank_people_for_user("u1", db=object(), limit=10, features_out=feats)
    assert all(o["score"] == 20 for o in out)
    assert feats["bob-evercore"] == {"warmth": 20, "rank_score": 20, "tier": "neutral"}


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


class _FakeQuery:
    """firm_employees stand-in: where(company ==), order by document id,
    start_at, limit, stream. Records every limit asked for."""

    def __init__(self, rosters, limits, firm=None, start=None, n=None):
        self.rosters, self.limits, self.firm, self.start, self.n = rosters, limits, firm, start, n

    def _with(self, **kw):
        args = dict(firm=self.firm, start=self.start, n=self.n)
        args.update(kw)
        return _FakeQuery(self.rosters, self.limits, **args)

    def where(self, field, op, value):
        return self._with(firm=value) if field == "company" else self

    def order_by(self, _field):
        return self

    def document(self, doc_id):
        return MagicMock(id=doc_id)

    def start_at(self, cursor):
        return self._with(start=next(iter(cursor.values())).id)

    def limit(self, n):
        self.limits.append(n)
        return self._with(n=n)

    def stream(self):
        ids = sorted(self.rosters.get(self.firm, []))
        if self.start is not None:
            ids = [i for i in ids if i >= self.start]
        return [
            MagicMock(id=i, **{"to_dict.return_value": {"first_name": i, "last_name": "x"}})
            for i in ids[: self.n]
        ]


def _fake_db(rosters, limits):
    db = MagicMock()
    db.collection.return_value = _FakeQuery(rosters, limits)
    return db


def test_each_target_firm_gets_a_share_of_the_pool():
    limits = []
    db = _fake_db({}, limits)
    ctx = _ctx(slugs=[f"firm_{i}" for i in range(5)])
    pr._fetch_candidates(ctx, db, cap=400)
    # Equal shares first (each read as a random start plus a wrap); every
    # firm here is empty, so each is then asked for the full cap.
    assert limits == [80, 80] * 5 + [400, 400] * 5


def test_a_deep_firm_still_fills_the_pool_when_others_are_thin():
    db = _fake_db({"deep": [f"deep-{i:04d}" for i in range(1000)]}, [])
    ctx = _ctx(slugs=["thin_a", "thin_b", "thin_c", "deep"])
    with patch.object(pr, "firm_employee_doc_to_app_contact", side_effect=lambda d: d):
        out = pr._fetch_candidates(ctx, db, cap=400)
    assert len(out) == 400


def test_a_big_firm_is_not_cut_off_alphabetically(monkeypatch):
    """Goldman's 107 used to stop at "k": a share of 60 always read the
    alphabetical head. A random start reaches the whole roster."""
    roster = [f"{c}{i}" for c in "abcdefghijklmnopqrstuvwxyz" for i in range(4)]
    db = _fake_db({"gs": roster}, [])
    ctx = _ctx(slugs=["gs"] + [f"empty_{i}" for i in range(11)])
    seen = set()
    with patch.object(pr, "firm_employee_doc_to_app_contact", side_effect=lambda d: d):
        for start in ("a", "m", "t", "x"):
            monkeypatch.setattr(pr, "_random_id_start", lambda s=start: s)
            out = pr._fetch_candidates(ctx, db, cap=400)
            assert len(out) == len(roster)  # thin others: deep fill reads it all
            # The first 60 come from the random start, not from "a".
            assert out[0]["_id"] >= start
            seen.update(d["_id"] for d in out[:60])
    assert any(i.startswith("z") for i in seen)


def test_a_small_firm_is_read_whole_with_no_repeats(monkeypatch):
    roster = [f"p{i:02d}" for i in range(30)]
    db = _fake_db({"evercore": roster}, [])
    monkeypatch.setattr(pr, "_random_id_start", lambda: "p20")
    with patch.object(pr, "firm_employee_doc_to_app_contact", side_effect=lambda d: d):
        out = pr._fetch_candidates(_ctx(slugs=["evercore"]), db, cap=400)
    ids = [d["_id"] for d in out]
    assert sorted(ids) == roster and len(ids) == len(set(ids))
    assert ids[0] == "p20"


def test_declared_firms_survive_the_target_cap():
    profile = {"dreamCompanies": [f"Firm {i}" for i in range(14)]}
    saved = [{"company": "Saved Co"}]
    t = rp.collect_targets(profile, saved)
    assert len(t) == 14 and "saved_co" not in t


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
         patch("backend.app.routes.ranker._run_in_background", side_effect=lambda fn, *a, **k: fn(*a, **k)), \
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
    sets = db.collection.return_value.document.return_value.collection.return_value \
        .document.return_value.set.call_args_list
    (pref,) = [c.args[0] for c in sets if "signal" in c.args[0]]
    assert pref["company"] == "evercore"
    # The swipe also takes them out of the saved deck.
    assert any("removed" in c.args[0] for c in sets)


def test_general_deck_comes_from_the_saved_list(http_client, authed):
    db = MagicMock()
    db.get_all.return_value = []
    served = ([_deck_item("ann", "Evercore")], {"ann": {"rank_score": 30}})
    with patch("backend.app.routes.ranker.get_db", return_value=db), \
         patch("backend.app.routes.ranker.people_deck_cache.serve", return_value=served), \
         patch("backend.app.routes.ranker.rank_people_for_user") as live, \
         patch("backend.app.routes.ranker._run_in_background"):
        r = http_client.get("/api/ranker/candidates?limit=10&prefetch=0",
                            headers={"Authorization": "Bearer fake"})
    assert r.status_code == 200
    assert [c["person"]["id"] for c in r.get_json()["candidates"]] == ["ann"]
    live.assert_not_called()


def test_company_deck_stays_live(http_client, authed):
    db = MagicMock()
    db.get_all.return_value = []
    with patch("backend.app.routes.ranker.get_db", return_value=db), \
         patch("backend.app.routes.ranker.people_deck_cache.serve") as saved, \
         patch("backend.app.routes.ranker.rank_people_for_user",
               return_value=[_deck_item("bob", "Stripe")]) as live, \
         patch("backend.app.routes.ranker.suggest_chips", return_value=[]), \
         patch("backend.app.routes.ranker._run_in_background"):
        r = http_client.get("/api/ranker/candidates?limit=10&prefetch=0&company=Stripe",
                            headers={"Authorization": "Bearer fake"})
    assert r.status_code == 200
    saved.assert_not_called()
    live.assert_called_once()
