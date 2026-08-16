"""Surface travel time between venues.

Used by the away-day pairing bonus: two away matches on consecutive days are
only a good thing if the squad can actually get from one to the other.

The default model is great-circle distance divided by an effective average
speed. That is crude — Norwegian roads follow fjords and go around mountains,
so the real journey is routinely twice the straight line — which is exactly why
`travel_overrides.yml` exists and takes precedence. Swapping in a real routing
API later means writing one more `TravelModel`; nothing else changes.
"""

from __future__ import annotations

import math
from typing import Protocol

from .loader import World
from .schema import Venue

EARTH_RADIUS_KM = 6371.0

# Effective door-to-door speed for a team bus or a train, well below the road
# speed limit once stops, transfers and loading are counted.
DEFAULT_SPEED_KMH = 60.0

# Fixed overhead per journey: getting the squad on and off the bus.
DEFAULT_OVERHEAD_HOURS = 0.75

# Returned for pairs with no reasonable surface connection.
UNREACHABLE = math.inf


class TravelModel(Protocol):
    def hours(self, venue_a: str, venue_b: str) -> float:
        """Surface travel hours between two venues; `math.inf` if impractical."""
        ...


def haversine_km(a: Venue, b: Venue) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


class HaversineTravelModel:
    """Great-circle distance over an effective speed, with curated overrides."""

    def __init__(
        self,
        world: World,
        speed_kmh: float = DEFAULT_SPEED_KMH,
        overhead_hours: float = DEFAULT_OVERHEAD_HOURS,
    ) -> None:
        self._world = world
        self._speed_kmh = speed_kmh
        self._overhead_hours = overhead_hours
        self._overrides: dict[tuple[str, str], float] = {}
        for override in world.travel_overrides:
            value = UNREACHABLE if override.hours is None else float(override.hours)
            self._overrides[_pair(override.a, override.b)] = value
        self._cache: dict[tuple[str, str], float] = {}

    def hours(self, venue_a: str, venue_b: str) -> float:
        if venue_a == venue_b:
            return 0.0
        key = _pair(venue_a, venue_b)
        if key in self._overrides:
            return self._overrides[key]
        if key not in self._cache:
            a = self._world.venue(venue_a)
            b = self._world.venue(venue_b)
            distance = haversine_km(a, b)
            self._cache[key] = self._overhead_hours + distance / self._speed_kmh
        return self._cache[key]


def _pair(a: str, b: str) -> tuple[str, str]:
    """Order-independent key — travel time is symmetric."""
    return (a, b) if a <= b else (b, a)
