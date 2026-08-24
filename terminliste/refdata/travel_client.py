"""HTTP client for ground travel times, via OSRM's free public routing API.

OSRM's no-signup demo server routes real road networks — actual driving time
and distance, not a straight-line guess. Same posture as `client.py`: a thin
`urllib.request` wrapper, called only by `cli.py refresh-travel-times`;
`validate`, `generate` and `score` just read the cache it populates.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

API_BASE = "https://router.project-osrm.org/route/v1/driving"
TIMEOUT_S = 10.0


class FetchError(Exception):
    """The API could not be reached, or returned something unparseable.

    Callers catch this and fall back to cached data: a refresh failing is
    never worse than not refreshing.
    """


@dataclass(frozen=True)
class RouteRecord:
    duration_hours: float
    distance_km: float


def fetch_ground_route(lon_a: float, lat_a: float, lon_b: float, lat_b: float) -> RouteRecord:
    """Real driving time/distance between two points, via OSRM.

    Raises `FetchError` on any network, HTTP, or parsing failure — never
    returns a partial or best-guess result silently.
    """
    url = f"{API_BASE}/{lon_a},{lat_a};{lon_b},{lat_b}?overview=false"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:
            body = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FetchError(f"could not reach {API_BASE}: {exc}") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise FetchError(f"response from {url} was not valid JSON: {exc}") from exc

    routes = payload.get("routes") or []
    if not routes:
        raise FetchError(f"no route returned for {url}: {payload.get('message', payload)}")

    route = routes[0]
    try:
        return RouteRecord(
            duration_hours=float(route["duration"]) / 3600.0,
            distance_km=float(route["distance"]) / 1000.0,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FetchError(f"unexpected route shape from {url}: {exc}") from exc
