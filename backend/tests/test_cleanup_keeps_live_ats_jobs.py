"""delete_expired_jobs must not delete direct-ATS jobs that are still live.

Direct Greenhouse/Lever/Ashby jobs are written once and never rewritten on
unchanged boards, so their expires_at lapses while they are still posted.
Their liveness comes from sync_board_jobs setting expired=True instead.
"""
from unittest.mock import MagicMock, patch

import pytest

from backend.pipeline import slug_loader, writer


@pytest.fixture(autouse=True)
def _listed_slugs():
    """Every board in these tests is still in the slug lists unless a test
    overrides load_slugs."""
    listed = {"greenhouse": ["stripe", "dead_co"], "lever": ["acme", "gone"],
              "ashby": ["ramp", "live"]}
    with patch.object(slug_loader, "load_slugs",
                      side_effect=lambda platform, tier="hot", shard=None: listed.get(platform, [])):
        yield


def _doc(doc_id, **data):
    d = MagicMock()
    d.id = doc_id
    d.to_dict.return_value = data
    d.reference = f"ref:{doc_id}"
    return d


def _db(docs):
    db = MagicMock()
    db.collection.return_value.where.return_value.select.return_value.stream.return_value = iter(docs)
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


def test_cleanup_reads_only_the_expired_field():
    db, _ = _db([])
    with patch.object(writer, "get_db", return_value=db):
        writer.delete_expired_jobs()
    db.collection.return_value.where.return_value.select.assert_called_once_with(["expired"])


def test_dormant_board_deletions_are_forgotten_from_kept_ids():
    """Deleting a dormant board's jobs also drops them from kept_job_ids and
    clears board_hash, so the board re-adds them if it comes back."""
    docs = [
        _doc("direct_greenhouse_dead_co_1", source="greenhouse"),
        _doc("direct_greenhouse_dead_co_2", source="greenhouse"),
        _doc("direct_lever_gone_2", source="lever"),                # no state: nothing to update
        _doc("direct_ashby_ramp_9", source="ashby", expired=True),  # already out of kept
    ]
    dead = {"dormant": True, "kept_job_ids": [
        "direct_greenhouse_dead_co_1", "direct_greenhouse_dead_co_2", "direct_greenhouse_dead_co_3"]}
    db, _ = _db(docs)
    states = {("greenhouse", "dead_co"): dead, ("lever", "gone"): None}
    with patch.object(writer, "get_db", return_value=db), \
         patch.object(writer.crawl_state, "read_state", side_effect=_states(states)), \
         patch.object(writer.crawl_state, "forget_jobs") as forget:
        assert writer.delete_expired_jobs() == 4
    forget.assert_called_once_with(
        "greenhouse", "dead_co",
        ["direct_greenhouse_dead_co_1", "direct_greenhouse_dead_co_2"], dead)


def test_forget_jobs_writes_remaining_kept_and_clears_hash():
    from backend.pipeline import crawl_state

    db = MagicMock()
    with patch.object(crawl_state, "get_db", return_value=db):
        crawl_state.forget_jobs("greenhouse", "dead_co", ["a", "b"],
                                {"kept_job_ids": ["a", "b", "c"], "board_hash": "h"})
    db.collection.return_value.document.assert_called_with("greenhouse:dead_co")
    db.collection.return_value.document.return_value.set.assert_called_once_with(
        {"kept_job_ids": ["c"], "board_hash": None}, merge=True)


def test_jobs_on_boards_dropped_from_slug_lists_are_deleted():
    """A board removed from hot_slugs.txt / its CSV keeps a live state doc,
    but nothing crawls it any more, so its lapsed jobs are deleted."""
    docs = [
        _doc("direct_workday_oldco_1", source="workday"),
        _doc("direct_workday_citi_2", source="workday"),
    ]
    db, _ = _db(docs)
    with patch.object(writer, "get_db", return_value=db), \
         patch.object(writer.crawl_state, "read_state", side_effect=_states()), \
         patch.object(writer.crawl_state, "forget_jobs") as forget, \
         patch.object(slug_loader, "load_slugs", return_value=["citi"]) as ls:
        assert writer.delete_expired_jobs() == 1
    assert _deleted(_db_batch(db)) == ["ref:direct_workday_oldco_1"]
    forget.assert_called_once()
    assert forget.call_args.args[:3] == ("workday", "oldco", ["direct_workday_oldco_1"])
    ls.assert_called_once_with("workday", "all")  # cached per platform


def test_empty_slug_list_never_deletes_live_boards():
    """If a platform's slug list loads empty (missing CSV), the unlisted rule
    is not applied: that would delete every live job on the platform."""
    docs = [_doc("direct_oracle_jpmorgan_1", source="oracle")]
    db, batch = _db(docs)
    with patch.object(writer, "get_db", return_value=db), \
         patch.object(writer.crawl_state, "read_state", side_effect=_states()), \
         patch.object(slug_loader, "load_slugs", return_value=[]):
        assert writer.delete_expired_jobs() == 0
    assert _deleted(batch) == []


def _db_batch(db):
    return db.batch.return_value
