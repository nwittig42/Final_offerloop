"""Recovering FullEnrich jobs the search stopped waiting for.

Reproduces the 2026-10-18 observation: a job abandoned at 75s had finished
with two DELIVERABLE addresses by the time anyone looked. Before this
module those were lost — the student got nothing and the warehouse cached
nothing, so the next student paid to rediscover the same people.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services import fullenrich_pending as fep


# ── Fakes ────────────────────────────────────────────────────────────────


class FakeDoc:
    def __init__(self, doc_id, data, store):
        self.id = doc_id
        self._data = dict(data)
        self._store = store

    def to_dict(self):
        return dict(self._data)

    @property
    def reference(self):
        return self

    def set(self, payload, merge=False):
        if merge:
            self._data.update(payload)
        else:
            self._data = dict(payload)
        self._store[self.id] = self._data


class FakeCollection:
    def __init__(self, store):
        self._store = store
        self._filtered = None

    def document(self, doc_id):
        return FakeDoc(doc_id, self._store.get(doc_id, {}), self._store)

    def where(self, field, op, value):
        self._filtered = [
            FakeDoc(k, v, self._store)
            for k, v in self._store.items()
            if (v.get(field) if op == "==" else None) == value
        ]
        return self

    def limit(self, n):
        self._n = n
        return self

    def stream(self):
        docs = self._filtered if self._filtered is not None else [
            FakeDoc(k, v, self._store) for k, v in self._store.items()
        ]
        return docs[: getattr(self, "_n", len(docs))]


class FakeDB:
    def __init__(self):
        self.store = {}

    def collection(self, name):
        return FakeCollection(self.store)


@pytest.fixture
def db():
    return FakeDB()


PEOPLE = [
    ("0", "Andrew", "An", "Apple", "https://www.linkedin.com/in/andrewan"),
    ("1", "Andrew", "Sonnabend", "Apple", ""),
    ("2", "Katie", "Cheng", "Apple", ""),
]


# ── Parking ──────────────────────────────────────────────────────────────


class TestRecordPending:
    def test_parks_the_job_with_enough_to_rebuild_contacts(self, db):
        assert fep.record_pending("job-abc", PEOPLE, db=db) is True
        doc = db.store["job-abc"]
        assert doc["enrichment_id"] == "job-abc"
        assert doc["resolved_at"] is None
        assert [p["pid"] for p in doc["people"]] == ["0", "1", "2"]
        assert doc["people"][0]["company"] == "Apple"

    def test_refuses_empty_input(self, db):
        assert fep.record_pending("", PEOPLE, db=db) is False
        assert fep.record_pending("job-abc", [], db=db) is False
        assert db.store == {}


# ── Sweeping ─────────────────────────────────────────────────────────────


class TestSweep:
    def test_recovers_finished_job_into_the_warehouse(self, db, monkeypatch):
        """The real case: abandoned at 75s, finished with two addresses."""
        fep.record_pending("job-abc", PEOPLE, db=db)

        from app.services import fullenrich_client
        monkeypatch.setattr(
            fullenrich_client, "fetch_bulk",
            lambda eid, wait_seconds=0, db=None: (True, {
                "0": {"email": "aan3@apple.com", "status": "DELIVERABLE"},
                "2": {"email": "katherine_l_cheng@apple.com", "status": "DELIVERABLE"},
            }))

        written = []
        import app.services.firm_cache.writer as writer
        monkeypatch.setattr(
            writer, "cache_pdl_contacts",
            lambda contacts, **kw: written.extend(contacts) or len(contacts))

        assert fep.sweep(db=db) == 2

        assert {c["Email"] for c in written} == {
            "aan3@apple.com", "katherine_l_cheng@apple.com"}
        # Identity must survive the round trip, or the warehouse row is junk.
        andrew = next(c for c in written if c["Email"] == "aan3@apple.com")
        assert (andrew["FirstName"], andrew["LastName"]) == ("Andrew", "An")
        assert andrew["Company"] == "Apple"
        assert andrew["EmailVerified"] is True

        assert db.store["job-abc"]["outcome"] == "recovered"
        assert db.store["job-abc"]["resolved_at"] is not None

    def test_unfinished_job_stays_parked_and_counts_the_attempt(self, db, monkeypatch):
        fep.record_pending("job-abc", PEOPLE, db=db)
        from app.services import fullenrich_client
        monkeypatch.setattr(fullenrich_client, "fetch_bulk",
                            lambda eid, wait_seconds=0, db=None: (False, {}))

        assert fep.sweep(db=db) == 0
        assert db.store["job-abc"]["resolved_at"] is None
        assert db.store["job-abc"]["attempts"] == 1

    def test_finished_but_empty_is_closed_not_retried_forever(self, db, monkeypatch):
        fep.record_pending("job-abc", PEOPLE, db=db)
        from app.services import fullenrich_client
        monkeypatch.setattr(fullenrich_client, "fetch_bulk",
                            lambda eid, wait_seconds=0, db=None: (True, {}))

        assert fep.sweep(db=db) == 0
        assert db.store["job-abc"]["outcome"] == "empty"
        assert db.store["job-abc"]["resolved_at"] is not None

    def test_old_job_expires_without_calling_the_vendor(self, db, monkeypatch):
        fep.record_pending("job-abc", PEOPLE, db=db)
        stale = datetime.now(timezone.utc) - timedelta(seconds=fep.MAX_AGE_SECONDS + 60)
        db.store["job-abc"]["created_at"] = stale.isoformat()

        called = []
        from app.services import fullenrich_client
        monkeypatch.setattr(
            fullenrich_client, "fetch_bulk",
            lambda eid, wait_seconds=0, db=None: called.append(eid) or (True, {}))

        assert fep.sweep(db=db) == 0
        assert called == [], "expired jobs must not spend a vendor call"
        assert db.store["job-abc"]["outcome"] == "expired"

    def test_vendor_blowing_up_never_propagates(self, db, monkeypatch):
        """A sweep runs inside a user's search thread — it must not be able
        to break that search."""
        fep.record_pending("job-abc", PEOPLE, db=db)
        from app.services import fullenrich_client

        def boom(*a, **kw):
            raise RuntimeError("FullEnrich exploded")

        monkeypatch.setattr(fullenrich_client, "fetch_bulk", boom)
        assert fep.sweep(db=db) == 0

    def test_hits_for_unknown_pids_are_ignored(self, db, monkeypatch):
        """Guards against writing a warehouse row with no identity."""
        fep.record_pending("job-abc", PEOPLE, db=db)
        from app.services import fullenrich_client
        monkeypatch.setattr(
            fullenrich_client, "fetch_bulk",
            lambda eid, wait_seconds=0, db=None: (True, {
                "99": {"email": "ghost@apple.com", "status": "DELIVERABLE"},
            }))

        written = []
        import app.services.firm_cache.writer as writer
        monkeypatch.setattr(
            writer, "cache_pdl_contacts",
            lambda contacts, **kw: written.extend(contacts) or len(contacts))

        assert fep.sweep(db=db) == 0
        assert written == []
        assert db.store["job-abc"]["outcome"] == "empty"
