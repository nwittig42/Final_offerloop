from app.services.find_contacts.playbooks import company_size, slot_specs


def _facts(**kw):
    base = {"company": "Acme", "team": None, "function": "other", "size_hint": None}
    base.update(kw)
    return base


def test_goldman_is_large():
    assert company_size(_facts(company="Goldman Sachs & Co."), {}) == "large"
    assert company_size(_facts(company="goldman sachs"), {}) == "large"


def test_other_large_signals():
    assert company_size(_facts(company="Tiny Co"), {"linkedin_org_employees": 200}) == "large"
    assert company_size(_facts(company="Tiny Co", size_hint="large"), {}) == "large"


def test_small_unknown_is_startup():
    assert company_size(_facts(company="Zorblax Labs"), {"linkedin_org_employees": 40}) == "startup"
    assert company_size(_facts(company="Zorblax Labs"), {}) == "startup"


def test_ib_large_specs():
    specs = slot_specs(_facts(function="ib", team="TMT"), "large")
    assert list(specs) == ["leader", "process", "recruiter"]
    assert specs["leader"]["titles"] == ["Managing Director", "Head of TMT"]
    assert specs["process"]["titles"] == ["Vice President", "Director", "Associate"]
    assert specs["recruiter"]["titles"] == [
        "Campus Recruiting", "Campus Recruiter", "Early Careers Recruiter"]
    assert specs["leader"]["keywords"] == ["tmt"]


def test_octo_specs():
    specs = slot_specs(_facts(function="software", team="Office of the CTO"), "large")
    assert "Chief Technology Officer" in specs["leader"]["titles"]
    assert specs["leader"]["keywords"] == ["cto"]


def test_no_team_drops_placeholder_titles():
    specs = slot_specs(_facts(function="software", team=None), "large")
    assert specs["leader"]["titles"] == ["Director"]
    assert specs["leader"]["keywords"] == []


def test_consulting_and_other_large():
    c = slot_specs(_facts(function="consulting"), "large")
    assert c["leader"]["titles"] == ["Partner", "Principal"]
    assert c["recruiter"]["titles"] == ["Campus Recruiter"]
    o = slot_specs(_facts(function="marketing", team="Brand"), "large")
    assert o["leader"]["titles"] == ["Vice President", "Head of Brand"]
    assert o["recruiter"]["titles"] == ["Campus Recruiter", "Early Talent Recruiter"]


def test_startup_has_no_recruiter_slot():
    specs = slot_specs(_facts(function="sales_trading", team="Rates"), "startup")
    assert list(specs) == ["leader", "process"]
    assert specs["leader"]["titles"] == [
        "Founder", "Co-Founder", "CEO", "Head of Sales & Trading"]
    assert specs["process"]["titles"] == ["Sales & Trading Lead"]
    other = slot_specs(_facts(function="other"), "startup")
    assert other["leader"]["titles"] == ["Founder", "Co-Founder", "CEO"]
    assert other["process"]["titles"] == []
