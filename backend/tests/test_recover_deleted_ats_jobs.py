"""Tests for backend.scripts.recover_deleted_ats_jobs (fake Firestore, no network)."""
from backend.scripts.recover_deleted_ats_jobs import recover


class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _Ref:
    def __init__(self, db, coll, doc_id):
        self._db, self._coll, self.id = db, coll, doc_id

    def update(self, fields):
        self._db.updates.append((self._coll, self.id, dict(fields)))
        self._db.data[self._coll][self.id].update(fields)


class _Coll:
    def __init__(self, db, name):
        self._db, self._name = db, name

    def document(self, doc_id):
        return _Ref(self._db, self._name, doc_id)

    def stream(self):
        return [_Snap(i, d) for i, d in self._db.data[self._name].items()]


class FakeDB:
    def __init__(self, states, job_ids):
        self.data = {
            "ats_crawl_state": {k: dict(v) for k, v in states.items()},
            "jobs": {j: {"job_id": j} for j in job_ids},
        }
        self.updates = []
        self.batch_sizes = []

    def collection(self, name):
        return _Coll(self, name)

    def get_all(self, refs):
        refs = list(refs)
        self.batch_sizes.append(len(refs))
        return [_Snap(r.id, self.data[r._coll].get(r.id)) for r in refs]


def _two_boards():
    states = {
        "greenhouse:acme": {"platform": "greenhouse", "slug": "acme",
                            "kept_job_ids": ["a1", "a2", "a3"], "board_hash": "h1"},
        "lever:beta": {"platform": "lever", "slug": "beta",
                       "kept_job_ids": ["b1", "b2"], "board_hash": "h2"},
    }
    return FakeDB(states, job_ids=["a1", "b1"])  # a2, a3, b2 missing


def test_dry_run_reports_and_writes_nothing():
    db = _two_boards()
    assert recover(db, confirm=False) == {"boards": 2, "missing": 3, "updated": 0}
    assert db.updates == []


def test_confirm_drops_missing_and_clears_hash():
    db = _two_boards()
    result = recover(db, confirm=True)
    assert result == {"boards": 2, "missing": 3, "updated": 2}
    assert ("ats_crawl_state", "greenhouse:acme",
            {"kept_job_ids": ["a1"], "board_hash": None}) in db.updates
    assert ("ats_crawl_state", "lever:beta",
            {"kept_job_ids": ["b1"], "board_hash": None}) in db.updates
    assert all(c == "ats_crawl_state" for c, _, _ in db.updates)


def test_board_with_nothing_missing_untouched():
    states = {
        "ashby:ok": {"platform": "ashby", "slug": "ok",
                     "kept_job_ids": ["x1"], "board_hash": "h"},
        "workday:other": {"platform": "workday", "slug": "other",
                          "kept_job_ids": ["w1"], "board_hash": "h"},
    }
    db = FakeDB(states, job_ids=["x1"])  # workday ignored even though w1 missing
    assert recover(db, confirm=True) == {"boards": 0, "missing": 0, "updated": 0}
    assert db.updates == []
    assert db.data["ats_crawl_state"]["ashby:ok"]["board_hash"] == "h"


def test_second_run_noop():
    db = _two_boards()
    recover(db, confirm=True)
    db.updates.clear()
    assert recover(db, confirm=True) == {"boards": 0, "missing": 0, "updated": 0}
    assert db.updates == []


def test_lookups_batched_at_300():
    ids = [f"j{i}" for i in range(650)]
    states = {"greenhouse:big": {"platform": "greenhouse", "slug": "big",
                                 "kept_job_ids": ids, "board_hash": "h"}}
    db = FakeDB(states, job_ids=[])
    assert recover(db, confirm=False)["missing"] == 650
    assert sorted(db.batch_sizes) == [50, 300, 300]
