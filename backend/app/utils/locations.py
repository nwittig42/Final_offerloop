"""Location string helpers."""
import re
from typing import Optional

_NON_CITY = re.compile(
    r"\b(remote|hybrid|anywhere|multiple|various|worldwide|global|nationwide|"
    r"emea|apac|latam|europe|asia|africa|americas|north america|south america)\b",
    re.I,
)


def office_city_or_none(location: Optional[str]) -> Optional[str]:
    """Return the city of a "City, ST" style location, else None.

    Remote/hybrid/multiple-location and region-only strings are not a city a
    person can be filtered on, so they return None.
    """
    if not location or not isinstance(location, str):
        return None
    loc = location.strip()
    if not loc or _NON_CITY.search(loc):
        return None
    city = loc.split(",")[0].strip()
    return city or None
