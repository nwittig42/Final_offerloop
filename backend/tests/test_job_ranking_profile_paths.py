"""The ranker must read profile fields from the paths the app actually writes.

`job_ranking` read `goals.careerTrack` and `goals.dreamCompanies` exclusively,
but nothing in the live codebase writes a populated `goals` map:

  * AccountSettings (the only surface that can still set a career track or
    dream companies) writes them FLAT at the top level, and mirrors only
    preferredLocation / jobTypes / interests into `updates.location`. It never
    assigns `updates.goals`.
  * The current onboarding flow writes `goals: {careerTrack: "", careerTracks:
    [], targetIndustries: []}` — empty — because career tracks were removed
    from onboarding. `OnboardingGoals.tsx` still exists but is rendered
    nowhere.

So the 40-point field alignment always fell through to the resume's major, the
+15 dream-company bonus could not fire for anyone, and the GPT reranker was
never told either. `job_ranker_signals` already read both paths, which is why
the separate +25 dream boost worked and masked this.

Separately, `preferredLocation` is a LIST for multi-city users and `_safe_str`
joins lists on ", ", so ["New York", "Chicago"] became "new york, chicago" —
never a substring of a real job location, so the +10 was silently lost.
"""
import pytest

from app.utils.job_ranking import infer_field, deterministic_score


def _job(**overrides):
    job = {
        "company": "Goldman Sachs",
        "title": "2027 Summer Analyst Program",
        "type": "INTERNSHIP",
        "category": "finance_banking",   # real taxonomy value
        "location": "New York, NY",
        "description_raw": "",
    }
    job.update(overrides)
    return job


def _flat_profile(**overrides):
    """The shape AccountSettings actually persists: flat keys, no goals map."""
    profile = {
        "careerTrack": "investment banking",
        "dreamCompanies": ["Goldman Sachs", "Citi"],
        "location": {
            "preferredLocation": ["New York", "Chicago"],
            "jobTypes": ["Internship"],
        },
        "resumeParsed": {
            # a major that maps to nothing, so field alignment can only come
            # from careerTrack
            "education": {"major": "history", "graduation_year": 2028},
            "skills": [],
        },
    }
    profile.update(overrides)
    return profile


@pytest.mark.unit
def test_career_track_read_from_top_level():
    """AccountSettings writes careerTrack flat; goals-only returned None."""
    assert infer_field(_flat_profile()) == "finance"


@pytest.mark.unit
def test_career_track_still_read_from_goals():
    """Legacy users with a populated goals map must keep working."""
    profile = _flat_profile(careerTrack="")
    profile["goals"] = {"careerTrack": "consulting"}
    assert infer_field(profile) == "consulting"


@pytest.mark.unit
def test_goals_path_wins_when_both_present():
    profile = _flat_profile()
    profile["goals"] = {"careerTrack": "consulting"}
    assert infer_field(profile) == "consulting"


@pytest.mark.unit
def test_field_alignment_awards_40_via_career_track():
    """With the track resolving, a finance job hits the 40-point alignment."""
    with_track = deterministic_score(_job(), _flat_profile())
    without_track = deterministic_score(_job(), _flat_profile(careerTrack=""))
    assert with_track - without_track == 40


@pytest.mark.unit
def test_dream_company_bonus_fires_from_top_level():
    scored = deterministic_score(_job(), _flat_profile())
    no_dream = deterministic_score(_job(), _flat_profile(dreamCompanies=[]))
    assert scored - no_dream == 15


@pytest.mark.unit
def test_multi_city_preferred_location_still_matches():
    """["New York","Chicago"] must match a New York job.

    _safe_str would have joined these into "new york, chicago".
    """
    scored = deterministic_score(_job(), _flat_profile())
    no_loc = deterministic_score(
        _job(), _flat_profile(location={"jobTypes": ["Internship"]}))
    assert scored - no_loc == 10


@pytest.mark.unit
def test_single_string_preferred_location_still_matches():
    """Legacy single-string shape must not regress."""
    profile = _flat_profile(
        location={"preferredLocation": "New York", "jobTypes": ["Internship"]})
    no_loc = _flat_profile(location={"jobTypes": ["Internship"]})
    assert deterministic_score(_job(), profile) - deterministic_score(_job(), no_loc) == 10


@pytest.mark.unit
def test_non_matching_location_gets_no_bonus():
    profile = _flat_profile(
        location={"preferredLocation": ["Boston"], "jobTypes": ["Internship"]})
    no_loc = _flat_profile(location={"jobTypes": ["Internship"]})
    assert deterministic_score(_job(), profile) == deterministic_score(_job(), no_loc)


@pytest.mark.unit
def test_remote_job_gets_remote_bonus_not_location_bonus():
    profile = _flat_profile(
        location={"preferredLocation": ["Boston"], "jobTypes": ["Internship"]})
    remote = deterministic_score(_job(location="Remote - US"), profile)
    onsite = deterministic_score(_job(location="Austin, TX"), profile)
    assert remote - onsite == 5
