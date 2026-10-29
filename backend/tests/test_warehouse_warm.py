"""Tests for warehouse_warm._demand_ranked_firms — the sparse-profile seed source.

The function was missing from the codebase (its import was wrapped in
try/except in person_ranker.py so the error was silent). These tests
pin the behavior that was promised by the comment but never delivered:

  1. Empty DB → empty list, no exception
  2. No demand docs → empty list
  3. Aggregates across days
  4. Normalizes legacy hyphen keys (_safe_key output) AND new underscore
     keys (slugify_company output) into the same firm_cache slug, so
     multi-word firms don't bucket under two slugs
  5. Returns highest-score first
  6. Caps at MAX_FIRMS
"""
from __future__ import annotations

from unittest.mock import MagicMock
from datetime import datetime, timedelta, timezone

import pytest

from app.services.warehouse_warm import (
    _demand_ranked_firms,
    DEMAND_WINDOW_DAYS,
    MAX_FIRMS,
)


# ─────────────────────────────────────────────────────────────────────
# Fake Firestore helpers
# ─────────────────────────────────────────────────────────────────────

class _FakeSnap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _FakeDoc:
    def __init__(self, data):
        self._data = data

    def get(self):
        return _FakeSnap(self._data)


class _FakeCollection:
    def __init__(self, by_doc_id):
        self._by_doc_id = by_doc_id

    def document(self, doc_id):
        return _FakeDoc(self._by_doc_id.get(doc_id))


class _FakeMetaDoc:
    """Mocks db.collection("meta").document("warehouseDemand").collection("daily")."""
    def __init__(self, by_day):
        self._daily = _FakeCollection(by_day)

    def collection(self, name):
        assert name == "daily"
        return self._daily


class _FakeDB:
    def __init__(self, daily_docs):
        self._daily_docs = daily_docs

    def collection(self, name):
        assert name == "meta"
        return _FakeMetaMeta(self._daily_docs)


class _FakeMetaMeta:
    def __init__(self, daily_docs):
        self._daily_docs = daily_docs

    def document(self, name):
        assert name == "warehouseDemand"
        return _FakeMetaDoc(self._daily_docs)


def _today_iso():
    return datetime.now(timezone.utc).date().isoformat()


def _day_offset_iso(days: int):
    return (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()


# ─────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────

def test_none_db_returns_empty():
    assert _demand_ranked_firms(None) == []


def test_no_docs_returns_empty():
    db = _FakeDB(daily_docs={})
    assert _demand_ranked_firms(db) == []


def test_aggregates_across_days():
    """A firm searched 2x yesterday and 3x today reports score=5."""
    db = _FakeDB(daily_docs={
        _today_iso():        {"firms": {"mckinsey": 3}},
        _day_offset_iso(1):  {"firms": {"mckinsey": 2}},
    })
    out = _demand_ranked_firms(db)
    assert out, "expected at least one firm"
    display, slug, score = out[0]
    assert slug == "mckinsey"
    assert score == 5


def test_normalizes_hyphen_and_underscore_keys_to_same_slug():
    """Legacy _safe_key wrote 'boston-consulting-group'; the new log_demand
    writes 'boston_consulting_group'. Both must bucket into the same
    firm_cache slug so the counts aren't split."""
    db = _FakeDB(daily_docs={
        _today_iso():        {"firms": {"boston-consulting-group": 4}},  # legacy
        _day_offset_iso(1):  {"firms": {"boston_consulting_group": 6}},  # new
    })
    out = _demand_ranked_firms(db)
    # Both should merge under the firm_cache slug (underscored).
    slugs = {slug for _d, slug, _c in out}
    assert "boston_consulting_group" in slugs, f"expected merged bucket, got {slugs}"
    merged = [t for t in out if t[1] == "boston_consulting_group"][0]
    assert merged[2] == 10, f"expected merged score=10, got {merged[2]}"


def test_sorted_highest_first():
    db = _FakeDB(daily_docs={
        _today_iso(): {"firms": {
            "mckinsey": 2,
            "google": 10,
            "stripe": 5,
        }},
    })
    out = _demand_ranked_firms(db)
    scores = [c for _d, _s, c in out]
    assert scores == sorted(scores, reverse=True), f"not sorted desc: {scores}"
    top_slug = out[0][1]
    assert top_slug == "google"


def test_caps_at_max_firms():
    """More than MAX_FIRMS distinct firms → result capped."""
    big_firms = {f"firm{i}": i + 1 for i in range(MAX_FIRMS + 25)}
    db = _FakeDB(daily_docs={_today_iso(): {"firms": big_firms}})
    out = _demand_ranked_firms(db)
    assert len(out) == MAX_FIRMS


def test_zero_or_negative_counts_dropped():
    db = _FakeDB(daily_docs={
        _today_iso(): {"firms": {"a": 0, "b": -3, "c": 2}},
    })
    out = _demand_ranked_firms(db)
    slugs = {s for _d, s, _c in out}
    assert slugs == {"c"}


def test_non_dict_firms_field_ignored():
    """Defensive: if some writer wrote firms as a list by mistake,
    don't crash — just skip that day."""
    db = _FakeDB(daily_docs={
        _today_iso(): {"firms": ["bad", "shape"]},  # non-dict
        _day_offset_iso(1): {"firms": {"stripe": 3}},
    })
    out = _demand_ranked_firms(db)
    assert out and out[0][1] == "stripe" and out[0][2] == 3


def test_window_limited_to_demand_window_days():
    """A doc older than DEMAND_WINDOW_DAYS is not aggregated."""
    db = _FakeDB(daily_docs={
        _day_offset_iso(DEMAND_WINDOW_DAYS + 5): {"firms": {"ancient_firm": 100}},
        _today_iso(): {"firms": {"recent_firm": 1}},
    })
    out = _demand_ranked_firms(db)
    slugs = {s for _d, s, _c in out}
    assert "ancient_firm" not in slugs
    assert "recent_firm" in slugs


def test_firestore_exception_returns_empty():
    db = MagicMock()
    db.collection.side_effect = RuntimeError("firestore down")
    assert _demand_ranked_firms(db) == []
