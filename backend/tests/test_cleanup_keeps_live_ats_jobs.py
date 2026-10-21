"""delete_expired_jobs must not delete direct-ATS jobs that are still live.

Direct Greenhouse/Lever/Ashby jobs are written once and never rewritten on
unchanged boards, so their expires_at lapses while they are still posted.
Their liveness comes from sync_board_jobs setting expired=True instead.
"""
from unittest.mock import MagicMock, patch

from backend.pipeline import writer


def _doc(doc_id, **data):
    d = MagicMock()
    d.id = doc_id
    d.to_dict.return_value = data
    d.reference = f"ref:{doc_id}"
    return d


def _db(docs):
    db = MagicMock()
    db.collection.return_value.where.return_value.stream.return_value = iter(docs)
    batch = MagicMock()
    db.batch.return_value = batch
    return db, batch


def _deleted(batch):
    return [c.args[0] for c in batch.delete.call_args_list]


def test_live_ats_jobs_survive_ttl():
    docs = [
        _doc("direct_greenhouse_stripe_1", source="greenhouse"),
        _doc("direct_lever_acme_2", source="lever", expired=False),
        _doc("direct_ashby_ramp_3", source="ashby"),
    ]
    db, batch = _db(docs)
    with patch.object(writer, "get_db", return_value=db):
        assert writer.delete_expired_jobs() == 0
    assert _deleted(batch) == []


def test_removed_ats_jobs_and_other_sources_are_deleted():
    docs = [
        _doc("direct_greenhouse_stripe_1", source="greenhouse", expired=True),
        _doc("simplify_abc", source="simplify"),
        _doc("fj_123", source="fantasticjobs"),
        _doc("direct_ashby_ramp_3", source="ashby"),
    ]
    db, batch = _db(docs)
    with patch.object(writer, "get_db", return_value=db):
        assert writer.delete_expired_jobs() == 3
    assert _deleted(batch) == ["ref:direct_greenhouse_stripe_1", "ref:simplify_abc", "ref:fj_123"]
