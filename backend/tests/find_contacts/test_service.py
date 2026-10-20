"""Service tests against a small in-memory Firestore fake. No vendor calls."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from app.services.find_contacts import service


# ── In-memory Firestore fake ────────────────────────────────────────────────

class _Snap:
    def __init__(self, data):
        self._data = copy.deepcopy(data)
        self.exists = data is not None

    def to_dict(self):
        return copy.deepcopy(self._data)


class _Doc:
    def __init__(self, store, path):
        self._store, self.path = store, path

    def collection(self, name):
        return _Col(self._store, f"{self.path}/{name}")

    def get(self, transaction=None):
        return _Snap(self._store.get(self.path))

    def set(self, data, merge=False):
        data = {k: v for k, v in data.items() if v is not service.firestore.SERVER_TIMESTAMP} | {
            k: "ts" for k, v in data.items() if v is service.firestore.SERVER_TIMESTAMP}
        if merge and self.path in self._store:
            self._store[self.path].update(copy.deepcopy(data))
        else:
            self._store[self.path] = copy.deepcopy(data)


    def update(self, data):
        if self.path not in self._store:
            raise KeyError(f"no document {self.path}")
        self._store[self.path].update(copy.deepcopy(data))


class _Col:
    def __init__(self, store, path):
        self._store, self.path = store, path

    def document(self, doc_id):
        return _Doc(self._store, f"{self.path}/{doc_id}")


class _Txn:
    def set(self, ref, data, merge=False):
        ref.set(data, merge=merge)


class FakeDb:
    def __init__(self):
        self.store: dict[str, dict] = {}

    def collection(self, name):
        return _Col(self.store, name)

    def transaction(self):
        return _Txn()


RESULT = "jobs/j1/contacts/result"
JOB = {"title": "Investment Banking Summer Analyst", "company": "Goldman Sachs",
       "location": "New York, NY", "description_raw": "..."}
FACTS = {"company": "Goldman Sachs", "team": "Investment Banking", "team_source": "job title",
         "function": "ib", "office_city": "New York", "level": "intern", "is_internship": True,
         "named_people": [], "size_hint": "large"}


def _contact(first, last, title, found_by, college=""):
    return {"FirstName": first, "LastName": last, "Title": title, "Company": "Goldman Sachs",
            "City": "New York", "LinkedIn": f"https://linkedin.com/in/{first.lower()}",
            "PhotoUrl": "", "College": college, "_found_by": found_by}


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(service.firestore, "transactional", lambda fn: fn)
    fake = FakeDb()
    fake.store["jobs/j1"] = dict(JOB)
    return fake


@pytest.fixture
def calls(monkeypatch):
    seen = {"read": 0, "search": 0, "email": []}

    def read_posting(job):
        seen["read"] += 1
        return dict(FACTS)

    def find_slot_candidates(facts, specs, budget):
        seen["search"] += 1
        seen["budget"] = (budget.collects, budget.fullenrich_calls)
        return {"leader": [_contact("Ann", "Lee", "Managing Director", "warehouse",
                                    "University of Southern California")],
                "process": [_contact("Bob", "Ray", "Vice President", "coresignal_r2")],
                "recruiter": []}

    def find_work_email(**kw):
        seen["email"].append(kw)
        return {"email": "ann.lee@gs.com", "status": "valid"}

    monkeypatch.setattr(service, "read_posting", read_posting)
    monkeypatch.setattr(service, "find_slot_candidates", find_slot_candidates)
    monkeypatch.setattr(service, "find_work_email", find_work_email)
    monkeypatch.setattr(service, "fullenrich_enabled", lambda db: True)
    return seen


def _saved_doc(**over):
    doc = {"status": "ready", "company": "Goldman Sachs", "team": "Investment Banking",
           "team_source": "job title", "steps": {"leader": "warehouse"}, "created_at": "ts",
           "people": [{"slot": "leader", "label": "Leads this team · can vouch for you",
                       "name": "Ann Lee", "first_name": "Ann", "last_name": "Lee",
                       "title": "Managing Director", "company": "Goldman Sachs",
                       "city": "New York", "linkedin": "https://linkedin.com/in/ann", "photo": "",
                       "college": "University of Southern California", "found_by": "warehouse",
                       "person_key": "https://linkedin.com/in/ann"}]}
    doc.update(over)
    return doc


# ── find_contacts_for_job ──────────────────────────────────────────────────

def test_returns_saved_without_search(db, calls):
    db.store[RESULT] = _saved_doc()
    out = service.find_contacts_for_job(db, "j1", "USC")
    assert calls["search"] == 0 and calls["read"] == 0
    assert out["status"] == "ready" and out["company"] == "Goldman Sachs"
    assert out["team"] == "Investment Banking" and out["team_source"] == "job title"
    assert out["people"][0]["name"] == "Ann Lee"
    assert out["people"][0]["alumni"] is True
    # alumni is per student: a different school sees False
    assert service.find_contacts_for_job(db, "j1", "Stanford")["people"][0]["alumni"] is False
    assert "alumni" not in db.store[RESULT]["people"][0]


def test_first_call_searches_and_saves(db, calls):
    out = service.find_contacts_for_job(db, "j1", "USC")
    assert calls["search"] == 1 and calls["read"] == 1
    assert calls["budget"] == (6, 1)
    assert out["status"] == "ready"
    assert [p["slot"] for p in out["people"]] == ["leader", "process"]
    assert out["people"][0]["alumni"] is True and out["people"][1]["alumni"] is False

    saved = db.store[RESULT]
    assert saved["status"] == "ready" and saved["created_at"] == "ts"
    assert saved["company"] == "Goldman Sachs" and saved["team"] == "Investment Banking"
    assert "lock_until" not in saved
    for p in saved["people"]:
        assert "alumni" not in p and "college" in p
    assert saved["people"][0]["first_name"] == "Ann" and saved["people"][0]["last_name"] == "Lee"
    assert "USC" not in repr(saved)
    # R3: posting facts cached on the job doc, original fields kept
    assert db.store["jobs/j1"]["posting_facts"]["team"] == "Investment Banking"
    assert db.store["jobs/j1"]["title"] == JOB["title"]

    # Second click reuses the saved result.
    again = service.find_contacts_for_job(db, "j1", "USC")
    assert calls["search"] == 1 and again["people"] == out["people"]


def test_reuses_cached_posting_facts(db, calls):
    db.store["jobs/j1"]["posting_facts"] = dict(FACTS)
    service.find_contacts_for_job(db, "j1", None)
    assert calls["read"] == 0 and calls["search"] == 1


def test_empty_search_saved_as_ready(db, calls, monkeypatch):
    monkeypatch.setattr(service, "find_slot_candidates", lambda f, s, b: {k: [] for k in s})
    out = service.find_contacts_for_job(db, "j1", "USC")
    assert out["status"] == "ready" and out["people"] == []
    assert db.store[RESULT]["status"] == "ready"
    assert db.store[RESULT]["steps"] == {"leader": "empty", "process": "empty",
                                         "recruiter": "empty"}


def test_concurrent_call_sees_searching(db, calls):
    live = datetime.now(timezone.utc) + timedelta(seconds=30)
    db.store[RESULT] = {"status": "searching", "lock_until": live}
    out = service.find_contacts_for_job(db, "j1", "USC")
    assert out == {"status": "searching"}
    assert calls["search"] == 0 and calls["read"] == 0


def test_lock_written_before_search(db, calls, monkeypatch):
    seen = {}

    def search(facts, specs, budget):
        seen["doc"] = dict(db.store[RESULT])
        return {k: [] for k in specs}

    monkeypatch.setattr(service, "find_slot_candidates", search)
    before = datetime.now(timezone.utc)
    service.find_contacts_for_job(db, "j1", None)
    assert seen["doc"]["status"] == "searching"
    lock = seen["doc"]["lock_until"]
    assert seen["doc"]["lock_owner"]
    assert before + timedelta(seconds=175) <= lock <= before + timedelta(seconds=185)


def test_second_caller_during_search_sees_searching(db, calls, monkeypatch):
    real_search = service.find_slot_candidates
    inner = {}

    def search(facts, specs, budget):
        inner["out"] = service.find_contacts_for_job(db, "j1", "USC")
        return real_search(facts, specs, budget)

    monkeypatch.setattr(service, "find_slot_candidates", search)
    out = service.find_contacts_for_job(db, "j1", "USC")
    assert inner["out"] == {"status": "searching"}
    assert calls["search"] == 1 and out["status"] == "ready"
    assert len(out["people"]) == 2


def test_caller_who_lost_lock_does_not_overwrite(db, calls, monkeypatch):
    winner = _saved_doc(team="Winner Team")

    def search(facts, specs, budget):
        # Our lock expired mid-search; another caller retook it and saved.
        db.store[RESULT] = dict(winner)
        return {k: [] for k in specs}

    monkeypatch.setattr(service, "find_slot_candidates", search)
    out = service.find_contacts_for_job(db, "j1", "USC")
    assert db.store[RESULT]["team"] == "Winner Team"
    assert out["status"] == "ready" and out["team"] == "Winner Team"


def test_caller_who_lost_lock_mid_search_returns_searching(db, calls, monkeypatch):
    def search(facts, specs, budget):
        db.store[RESULT]["lock_owner"] = "someone-else"
        return {k: [] for k in specs}

    monkeypatch.setattr(service, "find_slot_candidates", search)
    assert service.find_contacts_for_job(db, "j1", "USC") == {"status": "searching"}
    assert db.store[RESULT]["lock_owner"] == "someone-else"
    assert "people" not in db.store[RESULT]


def test_lost_lock_before_search_spends_nothing(db, calls, monkeypatch):
    def read_posting(job):
        calls["read"] += 1
        db.store[RESULT]["lock_owner"] = "someone-else"
        return dict(FACTS)

    monkeypatch.setattr(service, "read_posting", read_posting)
    assert service.find_contacts_for_job(db, "j1", "USC") == {"status": "searching"}
    assert calls["search"] == 0


def test_stale_lock_retaken(db, calls):
    stale = datetime.now(timezone.utc) - timedelta(seconds=5)
    db.store[RESULT] = {"status": "searching", "lock_until": stale}
    out = service.find_contacts_for_job(db, "j1", "USC")
    assert calls["search"] == 1 and out["status"] == "ready"
    assert db.store[RESULT]["status"] == "ready"


def test_expired_job_not_found(db, calls):
    assert service.find_contacts_for_job(db, "missing", "USC") == {"status": "not_found"}
    db.store["jobs/j1"]["expired"] = True
    db.store[RESULT] = _saved_doc()
    assert service.find_contacts_for_job(db, "j1", "USC") == {"status": "not_found"}
    assert calls["search"] == 0


def test_steps_logged(db, calls):
    service.find_contacts_for_job(db, "j1", None)
    assert db.store[RESULT]["steps"] == {"leader": "warehouse", "process": "coresignal_r2",
                                         "recruiter": "empty"}


def test_steps_logged_startup(db, calls, monkeypatch):
    monkeypatch.setattr(service, "company_size", lambda facts, job: "startup")
    service.find_contacts_for_job(db, "j1", None)
    assert db.store[RESULT]["steps"] == {"leader": "warehouse", "process": "coresignal_r2"}


def test_response_hides_saved_email(db, calls):
    doc = _saved_doc()
    doc["people"][0].update(email="ann.lee@gs.com", email_status="valid")
    db.store[RESULT] = doc
    person = service.find_contacts_for_job(db, "j1", None)["people"][0]
    assert "email" not in person and "email_status" not in person


# ── reveal_email ───────────────────────────────────────────────────────────

def test_reveal_email_saves_and_reuses(db, calls):
    db.store[RESULT] = _saved_doc()
    key = "https://linkedin.com/in/ann"
    first = service.reveal_email(db, "j1", key)
    assert first == {"email": "ann.lee@gs.com", "status": "valid"}
    assert calls["email"] == [{"first_name": "Ann", "last_name": "Lee",
                               "company": "Goldman Sachs",
                               "linkedin_url": "https://linkedin.com/in/ann", "db": db}]
    person = db.store[RESULT]["people"][0]
    assert person["email"] == "ann.lee@gs.com" and person["email_status"] == "valid"

    second = service.reveal_email(db, "j1", key)
    assert second == first and len(calls["email"]) == 1


def test_reveal_email_unknown_person_or_no_hit(db, calls, monkeypatch):
    db.store[RESULT] = _saved_doc()
    assert service.reveal_email(db, "j1", "nobody") is None
    assert service.reveal_email(db, "other-job", "https://linkedin.com/in/ann") is None
    assert calls["email"] == []


def test_reveal_email_miss_saved_and_not_retried(db, calls, monkeypatch):
    db.store[RESULT] = _saved_doc()
    misses = []
    monkeypatch.setattr(service, "find_work_email", lambda **kw: misses.append(kw))
    key = "https://linkedin.com/in/ann"
    assert service.reveal_email(db, "j1", key) is None
    person = db.store[RESULT]["people"][0]
    assert "email" not in person and person["email_status"] == "not_found"
    checked = person["email_checked_at"]
    assert abs(datetime.now(timezone.utc) - checked) < timedelta(seconds=5)
    assert service.reveal_email(db, "j1", key) is None
    assert len(misses) == 1
    assert "email_checked_at" not in service.find_contacts_for_job(db, "j1", None)["people"][0]


def test_reveal_email_disabled_saves_nothing(db, calls, monkeypatch):
    db.store[RESULT] = _saved_doc()
    monkeypatch.setattr(service, "fullenrich_enabled", lambda db: False)
    assert service.reveal_email(db, "j1", "https://linkedin.com/in/ann") is None
    assert calls["email"] == []
    person = db.store[RESULT]["people"][0]
    assert "email_status" not in person and "email_checked_at" not in person


def test_reveal_email_recent_miss_not_retried(db, calls):
    doc = _saved_doc()
    doc["people"][0].update(email_status="not_found",
                            email_checked_at=datetime.now(timezone.utc) - timedelta(days=6))
    db.store[RESULT] = doc
    assert service.reveal_email(db, "j1", "https://linkedin.com/in/ann") is None
    assert calls["email"] == []


def test_reveal_email_old_miss_retried(db, calls):
    doc = _saved_doc()
    doc["people"][0].update(email_status="not_found",
                            email_checked_at=datetime.now(timezone.utc) - timedelta(days=8))
    db.store[RESULT] = doc
    key = "https://linkedin.com/in/ann"
    assert service.reveal_email(db, "j1", key) == {"email": "ann.lee@gs.com", "status": "valid"}
    assert len(calls["email"]) == 1
    person = db.store[RESULT]["people"][0]
    assert person["email"] == "ann.lee@gs.com" and person["email_status"] == "valid"


def test_posting_facts_not_written_to_deleted_job(db, calls, monkeypatch):
    def read_posting(job):
        del db.store["jobs/j1"]  # job deleted while the LLM ran
        return dict(FACTS)

    monkeypatch.setattr(service, "read_posting", read_posting)
    service.find_contacts_for_job(db, "j1", None)
    assert "jobs/j1" not in db.store
