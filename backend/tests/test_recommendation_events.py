"""log_recommendation_event: the deck's training labels (OFF-41).

The ranker routes pass doc_id= for idempotent writes and log swipe,
reveal, outreach and connection events. When the logger did not accept
doc_id, the call raised TypeError outside its own try block and the
/feedback, /reveal-email and /outreach endpoints returned 500 after their
writes had already landed.
"""
from unittest.mock import MagicMock, patch

import pytest

from app.utils import recommendation_events as rec


@pytest.fixture()
def fake_db():
    db = MagicMock()
    with patch.object(rec, "get_db", return_value=db):
        yield db


def test_doc_id_writes_deterministic_doc(fake_db):
    rec.log_recommendation_event("person_swipe_right", "u1", contact_id="c1", doc_id="person_swipe_d1_c1")
    fake_db.collection.assert_called_with("recommendation_events")
    fake_db.collection.return_value.document.assert_called_with("person_swipe_d1_c1")
    doc = fake_db.collection.return_value.document.return_value.set.call_args[0][0]
    assert doc["event_type"] == "person_swipe_right"
    assert doc["contact_id"] == "c1"


def test_without_doc_id_uses_auto_id(fake_db):
    rec.log_recommendation_event("recommendation_shown", "u1", contact_id="c1")
    fake_db.collection.return_value.add.assert_called_once()


@pytest.mark.parametrize("event_type", [
    "person_swipe_right", "person_swipe_left", "person_swipe_skip", "person_swipe_restore",
    "email_revealed", "outreach_sent", "outreach_drafted",
    "deck_served", "meeting_scheduled", "connection_made",
])
def test_ranker_event_types_are_accepted(fake_db, event_type):
    rec.log_recommendation_event(event_type, "u1", contact_id="c1", doc_id=f"{event_type}_x")
    fake_db.collection.return_value.document.return_value.set.assert_called_once()


def test_unknown_type_is_dropped(fake_db):
    rec.log_recommendation_event("made_up", "u1", doc_id="x")
    fake_db.collection.assert_not_called()
