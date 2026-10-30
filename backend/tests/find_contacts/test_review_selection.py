from scripts.find_contacts_review import is_target, select_jobs, size_of


def _job(i, company, title="Analyst", **kw):
    return {"id": f"j{i}", "company": company, "title": title, **kw}


def test_senior_titles_are_skipped():
    for title in ("Senior Associate", "Associate Director, Accounting", "Team Lead",
                  "Sr. Analyst", "Product Manager", "VP, Strategy"):
        assert not is_target(_job(0, "Acme", title)), title


def test_early_career_titles_stay():
    for title in ("Software Engineer, New Grad", "Associate Project Manager",
                  "Assistant Manager", "Staff Accountant", "Leadership Development Program"):
        assert is_target(_job(0, "Acme", title)), title


def test_unknown_size_is_not_large():
    assert size_of(_job(0, "Goldman Sachs")) == "large"
    assert size_of(_job(0, "Tiny Co")) == "unknown"
    assert size_of(_job(0, "Tiny Co", linkedin_org_employees=40)) == "startup"


def test_unknown_size_fills_the_sample():
    jobs = [_job(i, "Goldman Sachs", f"Analyst {i}") for i in range(5)]
    jobs += [_job(100 + i, f"Startup {i}") for i in range(20)]
    chosen = select_jobs(jobs, 10)
    assert len(chosen) == 10
    assert sum(j["size"] == "large" for j in chosen) == 3  # per-company cap
    assert sum(j["size"] == "unknown" for j in chosen) == 7


def test_repeat_postings_count_once():
    jobs = [_job(i, f"Co {i % 2}", "Sales Associate") for i in range(6)]
    assert len(select_jobs(jobs, 6)) == 2
