"""Hand-maintained jobs fetcher: reads a CSV, no network."""
import hashlib

import pytest

from backend.pipeline.direct import FetchError, manual

HEADER = "company,title,location,apply_url,department,analyst_entry\n"
URL_A = "https://example.com/careers/analyst"
URL_B = "https://example.com/careers/associate"


def _csv(tmp_path, monkeypatch, *rows):
    path = tmp_path / "manual_jobs.csv"
    path.write_text(HEADER + "".join(r + "\n" for r in rows))
    monkeypatch.setattr(manual, "CSV_PATH", path)
    return path


def _sha(url):
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def test_rows_become_jobs(tmp_path, monkeypatch):
    _csv(tmp_path, monkeypatch,
         f'Acme Capital,Summer Analyst,"New York, NY",{URL_A},Investment Banking,1')
    jobs = manual.fetch("all")
    assert len(jobs) == 1
    j = jobs[0]
    assert j["job_id"] == f"direct_manual_all_{_sha(URL_A)}"
    assert j["source"] == "manual"
    assert j["board_platform"] == "manual"
    assert j["board_slug"] == "all"
    assert j["company"] == "Acme Capital"
    assert j["title"] == "Summer Analyst"
    assert j["location"] == "New York, NY"
    assert j["apply_url"] == URL_A
    assert j["department"] == "Investment Banking"
    assert j["analyst_entry"] is True
    assert j["posted_at"] is None
    assert j["description_raw"] == (
        "Summer Analyst at Acme Capital. Apply on the company's careers site.")
    assert len(j["description_raw"]) >= 50


def test_stable_ids(tmp_path, monkeypatch):
    _csv(tmp_path, monkeypatch, f'A,Analyst,"New York, NY",{URL_A},X,1')
    first = manual.fetch("all")[0]["job_id"]
    _csv(tmp_path, monkeypatch,
         f'B,Renamed Title,"Chicago, IL",{URL_A},Y,0')
    assert manual.fetch("all")[0]["job_id"] == first


def test_removed_row_absent(tmp_path, monkeypatch):
    _csv(tmp_path, monkeypatch,
         f'A,Analyst,"New York, NY",{URL_A},X,1',
         f'A,Associate,"New York, NY",{URL_B},X,1')
    assert len(manual.fetch("all")) == 2
    _csv(tmp_path, monkeypatch, f'A,Associate,"New York, NY",{URL_B},X,1')
    jobs = manual.fetch("all")
    assert [j["apply_url"] for j in jobs] == [URL_B]


def test_only_all_slug_and_bad_rows(tmp_path, monkeypatch):
    _csv(tmp_path, monkeypatch, f'A,Analyst,"New York, NY",{URL_A},X,1')
    with pytest.raises(FetchError):
        manual.fetch("other")
    _csv(tmp_path, monkeypatch, 'A,Analyst,"New York, NY",,X,1')
    with pytest.raises(FetchError):
        manual.fetch("all")


def test_seed_csv_rows_are_complete():
    for j in manual.fetch("all"):
        assert j["company"] and j["title"] and j["apply_url"].startswith("https://")


def test_manual_hot_slug():
    from backend.pipeline import slug_loader
    assert slug_loader.load_slugs("manual", "hot") == ["all"]
