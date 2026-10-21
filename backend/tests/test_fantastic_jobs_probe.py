from unittest.mock import MagicMock

from backend.scripts import fantastic_jobs_probe as probe


def _raw(i, title, url=None, hm=None):
    return {"id": str(i), "title": title, "organization": "Acme",
            "url": url or f"https://x.test/{i}", "ai_hiring_manager_name": hm}


def _page(n, start=0):
    titles = ["Summer Intern", "Senior Engineer", "Financial Analyst", "Staff Designer"]
    return [_raw(start + i, titles[i % 4], hm="Jo" if i % 2 == 0 else None) for i in range(n)]


def test_no_key_exits_cleanly(monkeypatch, capsys):
    monkeypatch.delenv("RAPIDAPI_KEY", raising=False)
    fetch = MagicMock()
    monkeypatch.setattr(probe, "_fj_fetch_page", fetch)
    assert probe.main(["--confirm"]) == 0
    assert "RAPIDAPI_KEY" in capsys.readouterr().out
    fetch.assert_not_called()


def test_without_confirm_makes_no_requests(monkeypatch, capsys):
    monkeypatch.setenv("RAPIDAPI_KEY", "k")
    fetch = MagicMock()
    monkeypatch.setattr(probe, "_fj_fetch_page", fetch)
    db = MagicMock()
    monkeypatch.setattr(probe, "_get_db", lambda: db)
    assert probe.main([]) == 0
    assert "dry run, nothing requested" in capsys.readouterr().out
    fetch.assert_not_called()
    db.collection.assert_not_called()


def test_stops_at_max_requests():
    fetch = MagicMock(side_effect=lambda params, label: _page(100, params["offset"]))
    r = probe.run_probe(confirm=True, max_requests=3, fetch_page=fetch, db=None)
    assert fetch.call_count == 3
    assert r["requests_used"] == 3
    assert r["jobs_returned"] == 300


def test_request_carries_title_filter():
    fetch = MagicMock(return_value=[])
    probe.run_probe(confirm=True, max_requests=1, fetch_page=fetch, db=None)
    params = fetch.call_args[0][0]
    for t in ("intern", "internship", "analyst", "new grad", "early career", "summer"):
        assert t in params["title_filter"]


def _db_with(existing_urls):
    db = MagicMock()

    def where(filter=None, **kw):
        q = MagicMock()
        vals = filter.value
        docs = []
        for u in vals:
            if u in existing_urls:
                d = MagicMock()
                d.to_dict.return_value = {"apply_url": u}
                docs.append(d)
        q.stream.return_value = docs
        return q

    db.collection.return_value.where.side_effect = where
    return db


def test_report_counts():
    pages = [_page(8), []]  # 8 jobs: titles cycle -> intern, senior, analyst, designer x2
    fetch = MagicMock(side_effect=lambda params, label: pages.pop(0) if pages else [])
    db = _db_with({"https://x.test/0", "https://x.test/1", "https://x.test/2"})
    r = probe.run_probe(confirm=True, max_requests=10, fetch_page=fetch, db=db)
    assert r["jobs_returned"] == 8
    assert r["early_career"] == 4        # ids 0,2,4,6
    assert r["in_firestore"] == 3
    assert r["with_hiring_manager"] == 4  # even ids
    assert r["requests_used"] == 2        # page of 8 (<100) ends the loop
