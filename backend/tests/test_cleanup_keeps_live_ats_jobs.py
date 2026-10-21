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


LIVE = {"kept_job_ids": [], "dormant": False}


def _states(mapping=None):
    mapping = mapping or {}
    return lambda platform, slug: mapping.get((platform, slug), LIVE)


def _deleted(batch):
    return [c.args[0] for c in batch.delete.call_args_list]


def test_live_ats_jobs_survive_ttl():
    docs = [
        _doc("direct_greenhouse_stripe_1", source="greenhouse"),
        _doc("direct_lever_acme_2", source="lever", expired=False),
        _doc("direct_ashby_ramp_3", source="ashby"),
    ]
    db, batch = _db(docs)
    with patch.object(writer, "get_db", return_value=db), \
         patch.object(writer.crawl_state, "read_state", side_effect=_states()):
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
    with patch.object(writer, "get_db", return_value=db), \
         patch.object(writer.crawl_state, "read_state", side_effect=_states()):
        assert writer.delete_expired_jobs() == 3
    assert _deleted(batch) == ["ref:direct_greenhouse_stripe_1", "ref:simplify_abc", "ref:fj_123"]


def test_jobs_on_boards_no_longer_crawled_are_deleted():
    docs = [
        _doc("direct_greenhouse_dead_co_1", source="greenhouse"),   # dormant board
        _doc("direct_lever_gone_2", source="lever"),                # no crawl state
        _doc("direct_ashby_live_3", source="ashby"),                # live board
    ]
    db, batch = _db(docs)
    states = {("greenhouse", "dead_co"): {"dormant": True}, ("lever", "gone"): None}
    with patch.object(writer, "get_db", return_value=db), \
         patch.object(writer.crawl_state, "read_state", side_effect=_states(states)):
        assert writer.delete_expired_jobs() == 2
    assert _deleted(batch) == ["ref:direct_greenhouse_dead_co_1", "ref:direct_lever_gone_2"]


def test_board_state_read_once_per_board():
    docs = [_doc(f"direct_greenhouse_stripe_{i}", source="greenhouse") for i in range(5)]
    db, _ = _db(docs)
    with patch.object(writer, "get_db", return_value=db), \
         patch.object(writer.crawl_state, "read_state", side_effect=_states()) as rs:
        writer.delete_expired_jobs()
    assert rs.call_count == 1
