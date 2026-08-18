"""Populate the ground-travel-time cache from OSRM, one venue pair at a time.

Same shape as `refresh.py`: a cache miss or an unreachable API degrades to
"kept whatever was already cached" rather than a crash, so this can be run
occasionally without becoming a liability the day the API is down — which,
inside this project's own sandboxed dev environment, it always is (see
README on the egress proxy's host allowlist).

Unlike `refresh.py` (which only ever diffs, never writes `data/*.yml`), this
writes straight into `data/.refdata_cache/travel.json`: that file is a cache
of API responses, not hand-authored data, so it's exactly as safe to
regenerate as any other cache.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

from ..model.loader import World
from . import cache
from .travel_client import FetchError, fetch_ground_route

DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / ".refdata_cache" / "travel.json"

# Road networks don't change week to week — a long TTL keeps `generate`/
# `score` cheap to run repeatedly without re-fetching every pair each time.
DEFAULT_TTL_S = 30 * 24 * 60 * 60


@dataclass(frozen=True)
class TravelRefreshReport:
    pairs_total: int
    fetched: int = 0
    reused: int = 0
    failed: list[str] = field(default_factory=list)


def refresh_travel_times(
    world: World,
    cache_path: Path = DEFAULT_CACHE_PATH,
    ttl_s: float = DEFAULT_TTL_S,
    force: bool = False,
) -> TravelRefreshReport:
    """Fetch ground travel time/distance for every venue pair not already
    freshly cached, and write the merged result back to `cache_path`.

    Never raises for a network failure on an individual pair — that pair is
    just left out of this run's report and whatever was cached before (if
    anything) is kept as-is.
    """
    entry = cache.read(cache_path)
    stored: dict = entry.payload if entry is not None and isinstance(entry.payload, dict) else {}
    is_fresh = entry is not None and entry.is_fresh(ttl_s)

    venues = sorted(world.venues)
    pairs = list(combinations(venues, 2))
    fetched = 0
    reused = 0
    failed: list[str] = []

    for venue_a, venue_b in pairs:
        key = _key(venue_a, venue_b)
        if not force and is_fresh and key in stored:
            reused += 1
            continue
        a, b = world.venue(venue_a), world.venue(venue_b)
        try:
            route = fetch_ground_route(a.lon, a.lat, b.lon, b.lat)
        except FetchError as exc:
            failed.append(f"{venue_a}-{venue_b}: {exc}")
            continue
        stored[key] = {"duration_hours": route.duration_hours, "distance_km": route.distance_km}
        fetched += 1

    if fetched:
        cache.write(cache_path, stored)

    return TravelRefreshReport(pairs_total=len(pairs), fetched=fetched, reused=reused, failed=failed)


def _key(venue_a: str, venue_b: str) -> str:
    a, b = (venue_a, venue_b) if venue_a <= venue_b else (venue_b, venue_a)
    return f"{a}|{b}"
