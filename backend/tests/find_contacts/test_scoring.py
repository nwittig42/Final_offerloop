from app.services.find_contacts.scoring import fill_slots, mark_alumni, person_key, score

FACTS = {"company": "Acme", "team": "Payments Risk", "office_city": "New York",
         "function": "other", "named_people": []}
SPECS = {
    "leader": {"titles": ["Vice President", "Head of Payments Risk"], "keywords": ["payments", "risk"]},
    "process": {"titles": ["Director", "Senior Manager"], "keywords": ["payments", "risk"]},
    "recruiter": {"titles": ["Campus Recruiter"], "keywords": []},
}


def _c(first, last, title, city="", **kw):
    base = {"FirstName": first, "LastName": last, "Title": title, "Company": "Acme",
            "City": city, "LinkedIn": f"https://linkedin.com/in/{first.lower()}{last.lower()}",
            "College": "", "PhotoUrl": ""}
    base.update(kw)
    return base


def test_score_prefers_team_words_and_city():
    strong = _c("Ann", "Lee", "Director, Payments Risk", "New York")
    weak = _c("Bob", "Ray", "Director", "Chicago")
    assert score(strong, "process", FACTS, SPECS) > score(weak, "process", FACTS, SPECS)
    # +3 per keyword, +2 title, +2 city
    assert score(strong, "process", FACTS, SPECS) == 3 + 3 + 2 + 2
    assert score(weak, "process", FACTS, SPECS) == 2


def test_decision_maker_bonus_only_for_leader():
    c = _c("Ann", "Lee", "Vice President", _coresignal_is_decision_maker=True)
    assert score(c, "leader", FACTS, SPECS) == 3
    assert score(c, "process", FACTS, SPECS) == 0


def test_one_person_one_slot():
    same = _c("Ann", "Lee", "Vice President Director, Payments Risk", "New York", _found_by="warehouse")
    other = _c("Bob", "Ray", "Director", _found_by="coresignal_r4")
    candidates = {"leader": [same], "process": [same, other], "recruiter": []}
    out = fill_slots(candidates, FACTS, SPECS, None)
    keys = [p["person_key"] for p in out]
    assert len(keys) == len(set(keys))
    by_slot = {p["slot"]: p for p in out}
    assert by_slot["leader"]["name"] == "Ann Lee"
    assert by_slot["process"]["name"] == "Bob Ray"
    assert "recruiter" not in by_slot


def test_fill_slots_output_shape_and_labels():
    leader = _c("Ann", "Lee", "Vice President", "New York", _found_by="warehouse",
                PhotoUrl="p.jpg", College="Stanford University")
    named = _c("Kristen", "Rietow", "VP", _found_by="named")
    rec = _c("Cara", "Dee", "Campus Recruiter", _found_by="coresignal_r4", LinkedIn="")
    out = fill_slots({"leader": [leader], "process": [named], "recruiter": [rec]}, FACTS, SPECS, "Stanford")
    assert [p["slot"] for p in out] == ["leader", "process", "recruiter"]
    first = out[0]
    assert set(first) == {"slot", "label", "name", "first_name", "last_name", "title", "company", "city", "linkedin",
                          "photo", "alumni", "college", "found_by", "person_key"}
    assert first["first_name"] == "Ann" and first["last_name"] == "Lee"
    # "Vice President" with no team words: we can't say they lead this team.
    assert first["label"] == "Senior at Acme · team not confirmed"
    assert first["alumni"] is True and first["college"] == "Stanford University"
    assert first["photo"] == "p.jpg" and first["found_by"] == "warehouse"
    assert out[1]["label"] == "Named in the posting"
    assert out[2]["label"] == "Scheduling and logistics"
    assert out[2]["person_key"] == "cara|dee|acme"


def test_person_key():
    assert person_key({"LinkedIn": "https://LinkedIn.com/in/X/"}) == "https://linkedin.com/in/x"
    assert person_key({"FirstName": "A", "LastName": "B", "Company": "C"}) == "a|b|c"


def test_alumni_flag():
    people = [{"name": "Ann", "college": "University of Southern California"},
              {"name": "Bob", "college": "University of Southern Mississippi"},
              {"name": "Cy", "college": ""}]
    out = mark_alumni(people, "USC")
    assert [p["alumni"] for p in out] == [True, False, False]
    assert "alumni" not in people[0]  # copies, input untouched
    assert all(p["alumni"] is False for p in mark_alumni(people, None))


def test_alumni_flag_school_suffix():
    out = mark_alumni([{"college": "University of Southern California - Marshall School of Business"}], "USC")
    assert out[0]["alumni"] is True


def test_alumni_flag_via_fill_slots():
    c = _c("Ann", "Lee", "Vice President", _found_by="warehouse", College="University of Southern California")
    out = fill_slots({"leader": [c]}, FACTS, SPECS, "USC")
    assert out[0]["alumni"] is True


def test_label_matches_evidence():
    by_query = _c("Ann", "Lee", "Vice President", _found_by="coresignal_r2")
    by_title = _c("Bob", "Ray", "Director, Payments", _found_by="warehouse")
    out = fill_slots({"leader": [by_query], "process": [by_title], "recruiter": []}, FACTS, SPECS, None)
    assert out[0]["label"] == "Leads this team · can vouch for you"
    assert out[1]["label"] == "Likely runs interviews"

    broad_leader = _c("Cy", "Doe", "Managing Director", _found_by="coresignal_r4")
    broad_process = _c("Di", "Fox", "Senior Manager", _found_by="fullenrich")
    out = fill_slots({"leader": [broad_leader], "process": [broad_process]}, FACTS, SPECS, None)
    assert out[0]["label"] == "Senior at Acme · team not confirmed"
    assert out[1]["label"] == "Could run interviews · team not confirmed"


def test_startup_founder_leads():
    specs = {"leader": {"titles": ["Founder", "CEO"], "keywords": []}}
    out = fill_slots({"leader": [_c("Ed", "Gu", "Co-Founder & CEO", _found_by="coresignal_r4")]},
                     FACTS, specs, None)
    assert out[0]["label"] == "Leads this team · can vouch for you"
