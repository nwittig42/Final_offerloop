"""ranking_profile: what the people ranker knows about a student (OFF-41)."""
from datetime import datetime, timedelta, timezone

from app.services import ranking_profile as rp

NOW = datetime(2026, 10, 18, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


# ── targets ──────────────────────────────────────────────────────────


def test_targets_merge_declared_and_saved_jobs():
    profile = {"dreamCompanies": ["Goldman Sachs"]}
    saved = [{"company": "Evercore"}, {"company": "Goldman Sachs"}]
    t = rp.collect_targets(profile, saved)
    assert list(t) == ["goldman_sachs", "evercore"]
    assert t["goldman_sachs"]["sources"] == ["declared", "saved_job"]
    assert t["evercore"]["strength"] == rp.TARGET_STRENGTH["saved_job"]


def test_targets_read_goals_dicts():
    profile = {"goals": {"dreamCompanies": [{"name": "McKinsey & Company"}]}}
    t = rp.collect_targets(profile)
    assert list(t) == ["mckinsey"]


def test_inferred_targets_are_capped():
    saved = [{"company": f"Firm {i}"} for i in range(30)]
    assert len(rp.collect_targets({}, saved)) == rp.MAX_TARGET_FIRMS


def test_saved_job_never_outranks_a_declared_firm():
    saved = [{"company": "Stripe"}]
    t = rp.collect_targets({"dreamCompanies": ["Stripe"]}, saved)
    assert t["stripe"]["strength"] == 1.0


# ── roles + stage ────────────────────────────────────────────────────


def test_target_roles_from_settings_deduped():
    profile = {"goals": {"targetRoles": ["Analyst", "Product Manager"]},
               "preferredJobRolesOrTitles": ["analyst", "Associate"]}
    assert rp.collect_target_roles(profile) == ["Analyst", "Product Manager", "Associate"]


def test_stage_from_grad_year():
    assert rp.recruiting_stage({"academics": {"graduationYear": "2029"}}, now=NOW) == "explore"
    assert rp.recruiting_stage({"academics": {"graduationYear": "2027"}}, now=NOW) == "recruit"
    assert rp.recruiting_stage({"graduationYear": "May 2026"}, now=NOW) == "recruit"
    assert rp.recruiting_stage({}, now=NOW) == ""


# ── affinity ─────────────────────────────────────────────────────────


def test_connections_outweigh_saves():
    contacts = [
        {"company": "Goldman Sachs", "jobTitle": "Analyst", "pipelineStage": "connected", "updatedAt": _iso(1)},
        {"company": "Stripe", "jobTitle": "Software Engineer", "pipelineStage": "new", "updatedAt": _iso(1)},
    ]
    by_co, _ = rp.affinity_from_contacts(contacts, now=NOW)
    assert by_co["goldman_sachs"] > 5 * by_co["stripe"]


def test_affinity_decays_with_age():
    fresh = [{"company": "Bain", "pipelineStage": "replied", "updatedAt": _iso(0)}]
    old = [{"company": "Bain", "pipelineStage": "replied", "updatedAt": _iso(rp.AFFINITY_HALF_LIFE_DAYS)}]
    f, _ = rp.affinity_from_contacts(fresh, now=NOW)
    o, _ = rp.affinity_from_contacts(old, now=NOW)
    assert abs(o["bain"] - f["bain"] / 2) < 1e-6


def test_bounced_contacts_teach_nothing():
    by_co, by_fam = rp.affinity_from_contacts(
        [{"company": "Bain", "jobTitle": "Consultant", "pipelineStage": "bounced"}], now=NOW)
    assert by_co == {} and by_fam == {}


# ── negatives ────────────────────────────────────────────────────────


def test_repeated_passes_make_a_negative_unless_engaged():
    prefs = [{"signal": "left", "company": "amazon"}] * 3 + [{"signal": "left", "company": "bain"}] * 3
    assert rp.negatives_from_prefs(prefs, positive_companies=["bain"]) == {"amazon"}


def test_right_swipes_and_few_passes_are_not_negative():
    prefs = [{"signal": "right", "company": "amazon"}] * 5 + [{"signal": "skip", "company": "meta"}] * 2
    assert rp.negatives_from_prefs(prefs, ()) == set()


# ── build ────────────────────────────────────────────────────────────


def test_build_reads_web_school_field():
    """Web onboarding writes academics.university, not academics.school."""
    prof = rp.build_ranking_profile({"academics": {"university": "University of Southern California"}}, now=NOW)
    assert prof["school_slugs"] == ["usc"]


def test_load_never_raises_on_a_broken_db():
    prof = rp.load_ranking_profile("u1", object(), {"dreamCompanies": ["Stripe"]})
    assert list(prof["targets"]) == ["stripe"]
