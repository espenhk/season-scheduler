"""Surface and air travel time between venues.

Used by the away-day pairing bonus: two away matches on consecutive days are
only a good thing if the squad can actually get from one to the other.

Ground travel comes from a routing API (OSRM), fetched ahead of time by
`cli.py refresh-travel-times` and read only from
`data/.refdata_cache/travel.json`, so validate/generate/score never touch the
network. Nothing approximates roads: Norwegian ones follow fjords and go
around mountains, and the routing API already knows that.

Air travel *is* well approximated — great-circle distance is what a plane
actually covers — as distance over cruise speed plus a door-to-door overhead.
The cheaper of the two wins; a pair where neither gets under the threshold is
unreachable. `travel_overrides.yml` beats both, for the rare pair either gets
wrong.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .loader import World
from .schema import Venue

EARTH_RADIUS_KM = 6371.0

# Cruise speed for a short-haul domestic jet.
AIR_CRUISE_SPEED_KMH = 800.0

# Door-to-door overhead on top of flight time: check-in, security, boarding,
# taxi at both ends.
AIR_OVERHEAD_HOURS = 2.5

# A pair where neither ground nor air travel gets under this many hours is
# flagged as un-travelable within a normal scheduling window.
UNTRAVELABLE_THRESHOLD_HOURS = 8.0

# Returned for pairs with no reasonable connection at all.
UNREACHABLE = math.inf

DEFAULT_GROUND_CACHE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / ".refdata_cache" / "travel.json"
)


class TravelModel(Protocol):
    def hours(self, venue_a: str, venue_b: str) -> float:
        """Best available travel hours between two venues; `math.inf` if
        neither ground nor air travel is viable."""
        ...


def great_circle_km(a: Venue, b: Venue) -> float:
    """Straight-line distance — what a flight actually covers, so it feeds the
    air-time estimate only, never a road one."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def air_hours(a: Venue, b: Venue) -> float:
    return AIR_OVERHEAD_HOURS + great_circle_km(a, b) / AIR_CRUISE_SPEED_KMH


@dataclass(frozen=True)
class TravelEstimate:
    """Both travel times for a pair, and the effective one scoring should use."""

    ground_hours: float | None  # None: no cached route for this pair yet
    air_hours: float | None  # None: not computed — a curated override settled it
    threshold_hours: float

    @property
    def hours(self) -> float:
        return min(h for h in (self.ground_hours, self.air_hours) if h is not None)

    @property
    def unreachable(self) -> bool:
        return self.hours > self.threshold_hours


class ApiTravelModel:
    """Ground time from the cached OSRM fetch; air time computed on the fly;
    curated overrides win over both."""

    def __init__(
        self,
        world: World,
        ground_cache_path: Path = DEFAULT_GROUND_CACHE_PATH,
        threshold_hours: float = UNTRAVELABLE_THRESHOLD_HOURS,
    ) -> None:
        self._world = world
        self._threshold = threshold_hours
        self._ground = _load_ground_cache(ground_cache_path)
        self._overrides: dict[tuple[str, str], float] = {}
        for override in world.travel_overrides:
            value = UNREACHABLE if override.hours is None else float(override.hours)
            self._overrides[_pair(override.a, override.b)] = value
        self._estimates: dict[tuple[str, str], TravelEstimate] = {}

    def estimate(self, venue_a: str, venue_b: str) -> TravelEstimate:
        if venue_a == venue_b:
            return TravelEstimate(ground_hours=0.0, air_hours=0.0, threshold_hours=self._threshold)
        key = _pair(venue_a, venue_b)
        if key in self._overrides:
            return TravelEstimate(
                ground_hours=self._overrides[key], air_hours=None, threshold_hours=self._threshold
            )
        if key not in self._estimates:
            a, b = self._world.venue(venue_a), self._world.venue(venue_b)
            self._estimates[key] = TravelEstimate(
                ground_hours=self._ground.get(key),
                air_hours=air_hours(a, b),
                threshold_hours=self._threshold,
            )
        return self._estimates[key]

    def hours(self, venue_a: str, venue_b: str) -> float:
        estimate = self.estimate(venue_a, venue_b)
        return UNREACHABLE if estimate.unreachable else estimate.hours


def _load_ground_cache(path: Path) -> dict[tuple[str, str], float]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        payload = raw.get("payload", {})
    except (json.JSONDecodeError, OSError, AttributeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[tuple[str, str], float] = {}
    for key, entry in payload.items():
        try:
            venue_a, venue_b = key.split("|", 1)
            result[_pair(venue_a, venue_b)] = float(entry["duration_hours"])
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _pair(a: str, b: str) -> tuple[str, str]:
    """Order-independent key — travel time is symmetric."""
    return (a, b) if a <= b else (b, a)
