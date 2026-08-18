"""The ground-travel-time client and refresh layer, mocked at the HTTP layer.

Same posture as test_refdata.py: nothing here makes a real network call — the
sandbox this project is developed in blocks the API host outright (see
README) — so a test that needed the real network would never run anywhere
this ships from.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error

import pytest

import factories as f
from terminliste.refdata import cache
from terminliste.refdata.travel_client import FetchError, RouteRecord, fetch_ground_route
from terminliste.refdata.travel_refresh import refresh_travel_times


class _FakeResponse:
    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_route_payload(duration_s: float, distance_m: float) -> bytes:
    return json.dumps({"routes": [{"duration": duration_s, "distance": distance_m}]}).encode()


# -- client -----------------------------------------------------------------


def test_fetch_ground_route_parses_duration_and_distance(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout=None: _FakeResponse(_fake_route_payload(27000, 305000)),
    )
    route = fetch_ground_route(10.79, 59.91, 5.33, 60.39)
    assert route == RouteRecord(duration_hours=7.5, distance_km=305.0)


def test_fetch_ground_route_raises_fetch_error_on_network_failure(monkeypatch):
    def _raise(url, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    with pytest.raises(FetchError):
        fetch_ground_route(10.79, 59.91, 5.33, 60.39)


def test_fetch_ground_route_raises_fetch_error_on_bad_json(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=None: _FakeResponse(b"not json")
    )
    with pytest.raises(FetchError):
        fetch_ground_route(10.79, 59.91, 5.33, 60.39)


def test_fetch_ground_route_raises_fetch_error_when_no_route_found(monkeypatch):
    payload = json.dumps({"code": "NoRoute", "message": "no route found"}).encode()
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=None: _FakeResponse(payload)
    )
    with pytest.raises(FetchError):
        fetch_ground_route(10.79, 59.91, 5.33, 60.39)


# -- refresh ------------------------------------------------------------------


@pytest.fixture
def small_world():
    v1 = f.venue("intility_arena", lat=59.91, lon=10.79)
    v2 = f.venue("brann_stadion", lat=60.39, lon=5.33)
    v3 = f.venue("lerkendal_stadion", lat=63.41, lon=10.40)
    t1 = f.team("valerenga_m", "valerenga", "intility_arena")
    t2 = f.team("brann_m", "brann", "brann_stadion")
    t3 = f.team("rosenborg_m", "rosenborg", "lerkendal_stadion")
    clubs = [f.club("valerenga", [t1]), f.club("brann", [t2]), f.club("rosenborg", [t3])]
    comp = f.competition("eliteserien_2026", ["valerenga_m", "brann_m", "rosenborg_m"])
    return f.world(clubs, [v1, v2, v3], [comp])


def test_refresh_fetches_every_pair_and_writes_the_cache(monkeypatch, tmp_path, small_world):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout=None: _FakeResponse(_fake_route_payload(3600, 100000)),
    )
    cache_path = tmp_path / "travel.json"

    report = refresh_travel_times(small_world, cache_path=cache_path)

    assert report.pairs_total == 3
    assert report.fetched == 3
    assert report.failed == []
    entry = cache.read(cache_path)
    assert entry is not None
    assert len(entry.payload) == 3
    for record in entry.payload.values():
        assert record == {"duration_hours": 1.0, "distance_km": 100.0}


def test_refresh_reuses_a_fresh_cache_without_calling_the_api(monkeypatch, tmp_path, small_world):
    cache_path = tmp_path / "travel.json"
    cache.write(
        cache_path,
        {
            "brann_stadion|intility_arena": {"duration_hours": 7.5, "distance_km": 305.0},
            "intility_arena|lerkendal_stadion": {"duration_hours": 6.0, "distance_km": 500.0},
            "brann_stadion|lerkendal_stadion": {"duration_hours": 14.0, "distance_km": 700.0},
        },
    )

    def _fail(url, timeout=None):
        raise AssertionError("should not hit the network when the cache is fresh")

    monkeypatch.setattr("urllib.request.urlopen", _fail)
    report = refresh_travel_times(small_world, cache_path=cache_path)

    assert report.fetched == 0
    assert report.reused == 3


def test_refresh_refetches_everything_once_the_cache_is_stale(monkeypatch, tmp_path, small_world):
    cache_path = tmp_path / "travel.json"
    cache.write(cache_path, {"brann_stadion|intility_arena": {"duration_hours": 7.5, "distance_km": 305.0}})
    stale = json.loads(cache_path.read_text())
    stale["fetched_at"] = time.time() - 40 * 24 * 60 * 60  # older than the 30-day TTL
    cache_path.write_text(json.dumps(stale))

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout=None: _FakeResponse(_fake_route_payload(3600, 100000)),
    )
    report = refresh_travel_times(small_world, cache_path=cache_path)

    assert report.fetched == 3
    assert report.reused == 0


def test_refresh_records_a_failed_pair_and_keeps_the_others(monkeypatch, tmp_path, small_world):
    def _urlopen(url, timeout=None):
        if "brann_stadion" in url or "5.33" in url:
            raise urllib.error.URLError("down")
        return _FakeResponse(_fake_route_payload(3600, 100000))

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    report = refresh_travel_times(small_world, cache_path=tmp_path / "travel.json")

    assert len(report.failed) == 2  # both pairs involving brann_stadion
    assert report.fetched == 1  # intility_arena-lerkendal_stadion only


def test_refresh_force_ignores_a_fresh_cache(monkeypatch, tmp_path, small_world):
    cache_path = tmp_path / "travel.json"
    cache.write(
        cache_path,
        {
            "brann_stadion|intility_arena": {"duration_hours": 7.5, "distance_km": 305.0},
            "intility_arena|lerkendal_stadion": {"duration_hours": 6.0, "distance_km": 500.0},
            "brann_stadion|lerkendal_stadion": {"duration_hours": 14.0, "distance_km": 700.0},
        },
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda url, timeout=None: _FakeResponse(_fake_route_payload(3600, 100000)),
    )
    report = refresh_travel_times(small_world, cache_path=cache_path, force=True)

    assert report.fetched == 3
    assert report.reused == 0
