"""The Hunter fast rung actually issues a request.

Regression test for a silent dead rung: email_fast.hunter_email passed
max_retries=0 to find_email_with_hunter, whose loop is
`for attempt in range(max_retries)`. Zero attempts, no HTTP call, straight
to "all retry attempts exhausted" and a reported miss.

It looked like Hunter was useless — 0 hits across three companies on
2026-10-18 — when in fact it was never called. A direct lookup for one of
those "missed" contacts returned score 97 in 0.7s.

Live since 2026-09-07, the same commit that added this rung to rescue the
extension from FullEnrich's 90-second waterfall. The rescue never ran.
"""
import pytest

from app.services import email_fast


@pytest.fixture
def hunter_configured(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEY", "test-key")
    import app.services.hunter as hunter
    monkeypatch.setattr(hunter, "HUNTER_API_KEY", "test-key")
    monkeypatch.setattr(hunter, "get_smart_company_domain", lambda c: "gs.com")
    return hunter


class TestHunterRungIsReachable:
    def test_the_lookup_is_actually_invoked(self, hunter_configured, monkeypatch):
        """The bug this guards: zero calls, silent miss."""
        calls = []

        def spy(first, last, domain, timeout=None, max_retries=None):
            calls.append({"max_retries": max_retries})
            return "arooshi.barua@gs.com", 97

        monkeypatch.setattr(hunter_configured, "find_email_with_hunter", spy)

        out = email_fast.hunter_email("Arooshi", "Barua", "Goldman Sachs")

        assert len(calls) == 1, "Hunter was never called — the rung is dead again"
        assert calls[0]["max_retries"] >= 1, (
            "max_retries is max ATTEMPTS; anything < 1 makes the loop empty "
            "and the rung silently reports a miss without issuing a request"
        )
        assert out["email"] == "arooshi.barua@gs.com"
        assert out["confidence"] == 97
        assert out["source"] == "hunter_finder"
        # Hunter scores deliverability, it does not SMTP-verify.
        assert out["verified"] is False

    def test_low_confidence_is_rejected(self, hunter_configured, monkeypatch):
        monkeypatch.setattr(
            hunter_configured, "find_email_with_hunter",
            lambda *a, **kw: ("guess@gs.com", 42))
        assert email_fast.hunter_email("Arooshi", "Barua", "Goldman Sachs") is None

    def test_no_result_is_a_clean_miss(self, hunter_configured, monkeypatch):
        monkeypatch.setattr(
            hunter_configured, "find_email_with_hunter",
            lambda *a, **kw: (None, 0))
        assert email_fast.hunter_email("Nobody", "Here", "Goldman Sachs") is None

    def test_missing_inputs_short_circuit_before_any_call(self, hunter_configured, monkeypatch):
        calls = []
        monkeypatch.setattr(
            hunter_configured, "find_email_with_hunter",
            lambda *a, **kw: calls.append(1) or (None, 0))
        assert email_fast.hunter_email("", "Barua", "Goldman Sachs") is None
        assert email_fast.hunter_email("Arooshi", "", "Goldman Sachs") is None
        assert email_fast.hunter_email("Arooshi", "Barua", "") is None
        assert calls == [], "no vendor call should be spent on incomplete input"

    def test_vendor_exception_is_a_miss_not_a_crash(self, hunter_configured, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("hunter down")

        monkeypatch.setattr(hunter_configured, "find_email_with_hunter", boom)
        assert email_fast.hunter_email("Arooshi", "Barua", "Goldman Sachs") is None
