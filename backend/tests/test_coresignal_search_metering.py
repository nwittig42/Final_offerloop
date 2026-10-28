"""
OFF-63: every Coresignal Search credit lands in provider_calls exactly once.

The leak this guards: `_search_ids` was unmetered, so the four code paths that
reach it (search_contacts_from_prompt, alumni_first_ids, enrich_linkedin_profile,
linkedin_backfill) spent Search credits invisibly. The ranker's per-deck-load
alumni count was the worst of them.

The companion trap: `member_search` rows used to bill "1 + collects", where the
1 was the search. Now that `_search_ids` logs its own row, leaving that +1 in
place would double-count every search. These tests pin both halves so the two
cannot drift back apart.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.services import coresignal_client
from app.services.metering import PROVIDER_RATES, _cost_for, _credits_for


def _resp(status=200, payload=None, raises_json=False):
    r = MagicMock()
    r.status_code = status
    r.text = "body"
    if raises_json:
        r.json.side_effect = ValueError("not json")
    else:
        r.json.return_value = payload
    return r


# ── _search_ids meters exactly one Search credit per billed call ──────────


@pytest.mark.unit
def test_search_ids_logs_one_search_row_on_200():
    with patch.object(coresignal_client._session, "post",
                      return_value=_resp(payload=[1, 2, 3])), \
         patch.object(coresignal_client, "log_provider_spend") as spend:
        ids = coresignal_client._search_ids({"query": {}}, page_size=10)

    assert ids == [1, 2, 3]
    spend.assert_called_once_with("coresignal", "search", 1, returned=3)


@pytest.mark.unit
def test_search_ids_meters_a_200_it_cannot_parse():
    """Coresignal bills on the 200, not on our ability to read the body."""
    with patch.object(coresignal_client._session, "post",
                      return_value=_resp(raises_json=True)), \
         patch.object(coresignal_client, "log_provider_spend") as spend:
        ids = coresignal_client._search_ids({"query": {}})

    assert ids == []
    spend.assert_called_once_with("coresignal", "search", 1, returned=0)


@pytest.mark.unit
@pytest.mark.parametrize("status", [401, 429, 500])
def test_search_ids_does_not_meter_a_non_200(status):
    with patch.object(coresignal_client._session, "post",
                      return_value=_resp(status=status)), \
         patch.object(coresignal_client, "log_provider_spend") as spend:
        assert coresignal_client._search_ids({"query": {}}) == []

    spend.assert_not_called()


@pytest.mark.unit
def test_search_ids_does_not_meter_a_transport_error():
    import requests

    with patch.object(coresignal_client._session, "post",
                      side_effect=requests.RequestException("boom")), \
         patch.object(coresignal_client, "log_provider_spend") as spend:
        assert coresignal_client._search_ids({"query": {}}) == []

    spend.assert_not_called()


# ── member_search rows carry collects only, so nothing double-counts ──────


@pytest.mark.unit
def test_member_search_credits_are_collects_only():
    result = ([], 0, [], {"collected_count": 7})
    assert _credits_for("coresignal", "member_search", result, {}) == 7


@pytest.mark.unit
def test_member_search_with_no_collects_costs_nothing():
    """A search that collected nobody is a search credit and no more — and
    that credit is the `search` row's, not this one's."""
    result = ([], 0, [], {"collected_count": 0})
    assert _credits_for("coresignal", "member_search", result, {}) == 0
    assert _cost_for("coresignal", "member_search", 0) == 0.0


@pytest.mark.unit
def test_member_search_prices_every_credit_at_the_collect_rate():
    collect_rate = PROVIDER_RATES["coresignal"]["member_collect"]
    assert _cost_for("coresignal", "member_search", 7) == pytest.approx(7 * collect_rate)


@pytest.mark.unit
def test_rate_sheet_has_no_member_search_entry():
    """A `member_search` rate would silently resurrect the old 1-is-a-search
    pricing the moment someone drops the special case in _cost_for."""
    assert "member_search" not in PROVIDER_RATES["coresignal"]
    assert PROVIDER_RATES["coresignal"]["search"] == 0.04
