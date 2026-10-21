"""Tests for backend.scripts.backfill_job_team_fields (fake Firestore, mocked fetchers)."""
from backend.scripts import backfill_job_team_fields as bf


class _Snap:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data

    def to_dict(self):
        return dict(self._data)


class _Ref:
    def __init__(self, doc_id):
        self.id = doc_id


class _Batch:
    def __init__(self, db):
        self._db, self._ops = db, []

    def set(self, ref, data, merge=False):
        assert merge is True
        self._ops.append((ref.id, dict(data)))

    def commit(self):
        self._db.commits.append(len(self._ops))
        for doc_id, data in self._ops:
            self._db.data[doc_id].update(data)
            self._db.writes.append((doc_id, data))


class _Query:
    def __init__(self, db, filters):
        self._db, self._filters = db, filters

    def where(self, filter=None):
        return _Query(self._db, self._filters + [filter])

    def stream(self):
        out = []
        for i, d in self._db.data.items():
            if all(f.op_string == "in" and d.get(f.field_path) in f.value for f in self._filters):
                out.append(_Snap(i, d))
        return out


class FakeDB:
    def __init__(self, docs):
        self.data = {k: dict(v) for k, v in docs.items()}
        self.writes, self.commits = [], []

    def collection(self, name):
        assert name == "jobs"
        return self

    def where(self, filter=None):
        return _Query(self, [filter])

    def document(self, doc_id):
        return _Ref(doc_id)

    def batch(self):
        return _Batch(self)


def _docs():
    return {
        "direct_greenhouse_acme_1": {"relevance_tier": 1, "source": "greenhouse"},
        "direct_greenhouse_acme_2": {"relevance_tier": 2, "source": "greenhouse"},
        "direct_lever_beta_x9": {"relevance_tier": 1, "source": "lever"},
        "direct_ashby_gamma_a1": {"relevance_tier": 3, "source": "ashby"},
        "direct_ashby_delta_b1": {"relevance_tier": 1, "source": "ashby", "department": "Eng", "team": "Core"},
        "simplify_1": {"relevance_tier": 1, "source": "simplify"},
    }


def _fetchers(calls):
    def gh(slug):
        calls.append(("greenhouse", slug))
        return [{"job_id": "direct_greenhouse_acme_1", "department": "Finance", "team": "", "title": "X"},
                {"job_id": "direct_greenhouse_acme_2", "department": "Ops", "team": "T"},
                {"job_id": "direct_greenhouse_acme_9", "department": "Other", "team": ""}]

    def lv(slug):
        calls.append(("lever", slug))
        return [{"job_id": "direct_lever_beta_x9", "department": "Eng", "team": "Platform"}]

    return {"greenhouse": gh, "lever": lv, "ashby": lambda s: calls.append(("ashby", s)) or []}


def test_dry_run_writes_nothing():
    db, calls = FakeDB(_docs()), []
    res = bf.backfill(db, confirm=False, fetchers=_fetchers(calls))
    assert db.writes == [] and db.commits == []
    assert res["docs_to_update"] == 3 and res["boards"] == 2 and res["updated"] == 0


def test_only_tier_1_2_missing_fields():
    db, calls = FakeDB(_docs()), []
    bf.backfill(db, confirm=True, fetchers=_fetchers(calls))
    assert {w[0] for w in db.writes} == {
        "direct_greenhouse_acme_1", "direct_greenhouse_acme_2", "direct_lever_beta_x9"}
    assert ("ashby", "gamma") not in calls and ("ashby", "delta") not in calls


def test_groups_by_board():
    db, calls = FakeDB(_docs()), []
    bf.backfill(db, confirm=True, fetchers=_fetchers(calls))
    assert sorted(calls) == [("greenhouse", "acme"), ("lever", "beta")]
    assert ("direct_greenhouse_acme_1", {"department": "Finance", "team": ""}) in db.writes
    assert ("direct_lever_beta_x9", {"department": "Eng", "team": "Platform"}) in db.writes


def test_second_run_noop():
    db = FakeDB(_docs())
    bf.backfill(db, confirm=True, fetchers=_fetchers([]))
    n = len(db.writes)
    calls = []
    res = bf.backfill(db, confirm=True, fetchers=_fetchers(calls))
    assert len(db.writes) == n and calls == [] and res["docs_to_update"] == 0


def test_empty_fetch_skips_board_and_reports():
    db = FakeDB(_docs())
    f = _fetchers([])
    f["lever"] = lambda s: []
    res = bf.backfill(db, confirm=True, fetchers=f)
    assert res["skipped_boards"] == [("lever", "beta")]
    assert "direct_lever_beta_x9" not in {w[0] for w in db.writes}


def test_batches_of_400():
    docs = {f"direct_lever_b_{i}": {"relevance_tier": 1, "source": "lever"} for i in range(850)}
    db = FakeDB(docs)
    f = {"lever": lambda s: [{"job_id": k, "department": "D", "team": ""} for k in docs],
         "greenhouse": lambda s: [], "ashby": lambda s: []}
    bf.backfill(db, confirm=True, fetchers=f)
    assert db.commits == [400, 400, 50]


def test_limit_caps_boards():
    db, calls = FakeDB(_docs()), []
    res = bf.backfill(db, confirm=False, fetchers=_fetchers(calls), limit=1)
    assert len(calls) == 1 and res["boards"] == 1
