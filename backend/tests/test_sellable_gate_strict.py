"""sellable_gate's require_verdict flag.

The lenient default exists for FullEnrich results: those arrive already
verified upstream, so "our checkers gave no answer" means our checkers were
down, not that the address is bad — rejecting there turned a Hunter quota
outage into a deck of false misses (Rylan, 2026-08-26).

Hunter-found addresses have no such backstop. Hunter scores deliverability
and never SMTP-checks, so silence has to read as "no" before one of its
addresses lands on the shared warehouse, where a bounce costs every future
user rather than only the one who asked.
"""
import pytest

from app.services import fullenrich_client


@pytest.fixture
def no_verdict(monkeypatch):
    """Both second-opinion checkers silent: NeverBounce off, no Hunter key."""
    from app.services import neverbounce_client
    monkeypatch.setattr(neverbounce_client, "is_configured", lambda: False)
    monkeypatch.delenv("HUNTER_API_KEY", raising=False)


class TestRequireVerdict:
    def test_no_verdict_is_rejected_when_strict(self, no_verdict):
        assert fullenrich_client.sellable_gate(
            "someone@example.com", "", require_verdict=True) is False

    def test_no_verdict_is_accepted_by_default(self, no_verdict):
        """The FullEnrich path must keep its lenient behaviour unchanged."""
        assert fullenrich_client.sellable_gate(
            "someone@example.com", "") is True

    def test_deliverable_short_circuits_before_either_checker(self, no_verdict):
        """DELIVERABLE is FullEnrich's own verdict — strict mode never
        downgrades it, and no second opinion is spent on it."""
        assert fullenrich_client.sellable_gate(
            "someone@example.com", "DELIVERABLE", require_verdict=True) is True

    def test_empty_email_is_rejected_either_way(self, no_verdict):
        assert fullenrich_client.sellable_gate("", "DELIVERABLE") is False
        assert fullenrich_client.sellable_gate(
            "", "DELIVERABLE", require_verdict=True) is False


class TestRealVerdictsStillDecide:
    def test_affirmative_verdict_passes_strict(self, monkeypatch):
        from app.services import neverbounce_client
        monkeypatch.setattr(neverbounce_client, "is_configured", lambda: True)
        monkeypatch.setattr(
            neverbounce_client, "verify_email",
            lambda _e: {"result": neverbounce_client.RESULT_VALID})
        assert fullenrich_client.sellable_gate(
            "real@example.com", "HIGH_PROBABILITY", require_verdict=True) is True

    def test_negative_verdict_rejects_regardless_of_flag(self, monkeypatch):
        from app.services import neverbounce_client
        invalid = getattr(neverbounce_client, "RESULT_INVALID", "invalid")
        monkeypatch.setattr(neverbounce_client, "is_configured", lambda: True)
        monkeypatch.setattr(
            neverbounce_client, "verify_email", lambda _e: {"result": invalid})
        assert fullenrich_client.sellable_gate(
            "bad@example.com", "HIGH_PROBABILITY") is False
        assert fullenrich_client.sellable_gate(
            "bad@example.com", "HIGH_PROBABILITY", require_verdict=True) is False
