"""OFF-54: country names must match as words, not substrings.

`_is_non_us_non_remote` scanned the excluded-country list with a plain
`country in location` substring test, so any US location whose text happened to
contain a country name was silently dropped. Both `normalize_all` and
`run_crawl_ats` gate on this function, so the loss applied to every source —
Greenhouse, Lever, Ashby, Simplify and the new direct-ATS platforms alike.

Measured casualties before the fix (11 of 19 real US locations in this file):
  "mexico" inside New Mexico          — an entire US state
  "india"  inside Indiana / Indianapolis
  "uk"     inside Milwaukee / Tukwila
  "panama" inside Panama City, FL
  "peru"   inside Peru, IL
  "china"  inside China, TX
  "mexico" inside Mexico, MO

Dropping a US job is the expensive direction: it is invisible (no log, no
counter) and it discards inventory we already paid to crawl. Both checks
therefore fail open.
"""
import pytest

from pipeline.normalizer import _is_non_us_non_remote


def _loc(location):
    return {"location": location}


class TestUsLocationsAreKept:
    # Every one of these is a real US location.
    US_LOCATIONS = [
        # the substring casualties
        "Indianapolis, IN",
        "Indiana",
        "Indianapolis",
        "Milwaukee, WI",
        "Milwaukee",
        "Tukwila, WA",
        "New Mexico",
        "Albuquerque, NM",
        "Panama City, FL",
        "Peru, IL",
        "China, TX",
        "Mexico, MO",
        # ordinary US rows that must obviously survive
        "New York, NY, United States",
        "Chicago, IL",
        "Charlotte, North Carolina",
        "Lima, OH",
        "Toledo, OH",
        "Remote - US",
        "Remote",
    ]

    @pytest.mark.unit
    @pytest.mark.parametrize("location", US_LOCATIONS)
    def test_us_location_is_not_dropped(self, location):
        assert _is_non_us_non_remote(_loc(location)) is False, \
            f"{location!r} is in the US and must not be dropped"


class TestNonUsLocationsStillDropped:
    NON_US_LOCATIONS = [
        "London, United Kingdom",
        "Remote - UK",
        "Singapore",
        "Remote, Singapore",
        "Warsaw, Poland",
        "Toronto, Canada",
        "Bengaluru, India",
        "Mexico City, Mexico",
        "Dublin, Ireland",
        "Tel Aviv, Israel",
        "Dubai, United Arab Emirates",
        "Sydney, Australia",
        "Tokyo, Japan",
        "Sao Paulo, Brazil",
    ]

    @pytest.mark.unit
    @pytest.mark.parametrize("location", NON_US_LOCATIONS)
    def test_non_us_location_is_dropped(self, location):
        assert _is_non_us_non_remote(_loc(location)) is True, \
            f"{location!r} is outside the US and should be dropped"


class TestShapeHandling:
    """The location field arrives as a string, dict or list depending on source."""

    @pytest.mark.unit
    def test_dict_location_us(self):
        assert _is_non_us_non_remote({"location": {"name": "Indianapolis, IN"}}) is False

    @pytest.mark.unit
    def test_dict_location_non_us(self):
        assert _is_non_us_non_remote({"location": {"name": "London, United Kingdom"}}) is True

    @pytest.mark.unit
    def test_list_location_us(self):
        assert _is_non_us_non_remote({"location": ["Indianapolis", "IN"]}) is False

    @pytest.mark.unit
    def test_list_location_non_us(self):
        assert _is_non_us_non_remote({"location": ["Bengaluru", "India"]}) is True

    @pytest.mark.unit
    @pytest.mark.parametrize("empty", ["", "   ", None])
    def test_empty_location_is_kept(self, empty):
        """No location is not evidence of being foreign — fail open."""
        assert _is_non_us_non_remote({"location": empty}) is False

    @pytest.mark.unit
    def test_missing_location_key_is_kept(self):
        assert _is_non_us_non_remote({}) is False


class TestUsMarkerWinsOverCountryWord:
    """A US state or country tag settles it even when a country word is present."""

    @pytest.mark.unit
    @pytest.mark.parametrize("location", [
        "China, TX",                    # real town in Texas
        "Mexico, MO",                   # real town in Missouri
        "New Mexico, United States",
        "Remote, US",
        "Panama City, Florida",
    ])
    def test_us_marker_overrides(self, location):
        assert _is_non_us_non_remote(_loc(location)) is False

    @pytest.mark.unit
    def test_longest_country_name_wins(self):
        """"united kingdom" must not be defeated by a short alternative."""
        assert _is_non_us_non_remote(_loc("Manchester, United Kingdom")) is True
        assert _is_non_us_non_remote(_loc("Dubai, United Arab Emirates")) is True
