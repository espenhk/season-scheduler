"""The reference-data client, cache, and diff/refresh layer.

`client.fetch_teams` is tested by monkeypatching `urllib.request.urlopen` —
nothing here makes a real network call, on purpose: the sandbox this project
is developed in blocks the API host outright (see README), so a test that
needed the real network would never run anywhere this ships from.
"""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import pytest

from terminliste.refdata import cache
from terminliste.refdata.client import FetchError, TeamRecord, fetch_teams
from terminliste.refdata.refresh import FieldDiff, refresh_competition


class _FakeResponse:
    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_teams_payload(teams: list[dict]) -> bytes:
    return json.dumps({"teams": teams}).encode()


# -- client -------------------------------------------------------------


def test_fetch_teams_parses_the_documented_fields(monkeypatch):
    payload = _fake_teams_payload(
        [
            {
                "idTeam": "133664",
                "strTeam": "Rosenborg BK",
                "strStadium": "Lerkendal Stadion",
                "intStadiumCapacity": "21166",
            }
        ]
    )
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=None: _FakeResponse(payload)
    )

    records = fetch_teams("Norwegian Eliteserien")
    assert records == [
        TeamRecord(
            api_team_id="133664",
            name="Rosenborg BK",
            stadium_name="Lerkendal Stadion",
            stadium_capacity=21166,
        )
    ]


def test_fetch_teams_tolerates_missing_optional_fields(monkeypatch):
    payload = _fake_teams_payload([{"idTeam": "1", "strTeam": "Some Club"}])
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=None: _FakeResponse(payload)
    )
    records = fetch_teams("Norwegian Eliteserien")
    assert records == [
        TeamRecord(api_team_id="1", name="Some Club", stadium_name=None, stadium_capacity=None)
    ]


def test_fetch_teams_treats_explicit_nulls_as_empty_strings_not_none(monkeypatch):
    """The API can return an explicit JSON null for a field it has no value
    for, distinct from omitting the key. `raw.get(key, default)` only ever
    applies `default` for a *missing* key, so a null needs its own handling
    or it leaks a `None` into `TeamRecord.name`/`.api_team_id` — which later
    breaks `_normalize()`'s `.casefold()` call in refresh.py."""
    payload = _fake_teams_payload([{"idTeam": None, "strTeam": None}])
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=None: _FakeResponse(payload)
    )
    records = fetch_teams("Norwegian Eliteserien")
    assert records == [
        TeamRecord(api_team_id="", name="", stadium_name=None, stadium_capacity=None)
    ]


def test_fetch_teams_raises_fetch_error_on_network_failure(monkeypatch):
    import urllib.error

    def _raise(url, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    with pytest.raises(FetchError):
        fetch_teams("Norwegian Eliteserien")


def test_fetch_teams_raises_fetch_error_on_bad_json(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=None: _FakeResponse(b"not json")
    )
    with pytest.raises(FetchError):
        fetch_teams("Norwegian Eliteserien")


# -- cache ----------------------------------------------------------------


def test_cache_round_trips_a_payload(tmp_path):
    path = tmp_path / "eliteserien_2026.json"
    cache.write(path, [{"name": "Rosenborg BK"}])
    entry = cache.read(path)
    assert entry is not None
    assert entry.payload == [{"name": "Rosenborg BK"}]


def test_cache_entry_freshness_respects_ttl(tmp_path):
    path = tmp_path / "x.json"
    cache.write(path, {})
    entry = cache.read(path)
    assert entry.is_fresh(ttl_s=3600)
    assert not entry.is_fresh(ttl_s=-1)  # already "expired" the instant it was written


def test_cache_read_of_missing_file_returns_none(tmp_path):
    assert cache.read(tmp_path / "missing.json") is None


def test_cache_read_of_corrupt_file_returns_none(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("{not valid json")
    assert cache.read(path) is None


# -- refresh / diff ---------------------------------------------------------


@pytest.fixture
def small_world():
    import factories as f

    v1 = f.venue("lerkendal_stadion", capacity=21166)
    v2 = f.venue("brann_stadion", capacity=17317)
    t1 = f.team("rosenborg_m", "rosenborg", "lerkendal_stadion")
    t2 = f.team("brann_m", "brann", "brann_stadion")
    clubs = [f.club("rosenborg", [t1]), f.club("brann", [t2])]
    # factories.club() derives name from id.replace("_", " ").title(), which
    # doesn't match the real club names — override directly for a realistic diff.
    clubs[0].name = "Rosenborg BK"
    clubs[1].name = "SK Brann"
    comp = f.competition("eliteserien_2026", ["rosenborg_m", "brann_m"])
    return f.world(clubs, [v1, v2], [comp])


def test_refresh_uses_the_api_and_writes_the_cache_when_none_exists(monkeypatch, tmp_path, small_world):
    payload = _fake_teams_payload(
        [
            {"idTeam": "1", "strTeam": "Rosenborg BK", "strStadium": "Lerkendal Stadion",
             "intStadiumCapacity": "21166"},
            {"idTeam": "2", "strTeam": "SK Brann", "strStadium": "Brann Stadion",
             "intStadiumCapacity": "17317"},
        ]
    )
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=None: _FakeResponse(payload)
    )

    report = refresh_competition(
        small_world, "eliteserien_2026", "Norwegian Eliteserien", cache_dir=tmp_path
    )
    assert report.source == "api"
    assert report.diffs == []
    assert (tmp_path / "eliteserien_2026.json").exists()


def test_refresh_flags_a_capacity_beyond_tolerance(monkeypatch, tmp_path, small_world):
    payload = _fake_teams_payload(
        [
            {"idTeam": "1", "strTeam": "Rosenborg BK", "strStadium": "Lerkendal Stadion",
             "intStadiumCapacity": "21405"},  # 239 above the 21166 on file
            {"idTeam": "2", "strTeam": "SK Brann", "strStadium": "Brann Stadion",
             "intStadiumCapacity": "17317"},
        ]
    )
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=None: _FakeResponse(payload)
    )

    report = refresh_competition(
        small_world, "eliteserien_2026", "Norwegian Eliteserien", cache_dir=tmp_path
    )
    assert report.diffs == [FieldDiff("rosenborg_m", "venue.capacity", 21166, 21405)]


def test_refresh_ignores_a_capacity_within_tolerance(monkeypatch, tmp_path, small_world):
    payload = _fake_teams_payload(
        [
            {"idTeam": "1", "strTeam": "Rosenborg BK", "strStadium": "Lerkendal Stadion",
             "intStadiumCapacity": "21170"},  # 4 above — within tolerance
            {"idTeam": "2", "strTeam": "SK Brann", "strStadium": "Brann Stadion",
             "intStadiumCapacity": "17317"},
        ]
    )
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=None: _FakeResponse(payload)
    )
    report = refresh_competition(
        small_world, "eliteserien_2026", "Norwegian Eliteserien", cache_dir=tmp_path
    )
    assert report.diffs == []


def test_refresh_reports_unmatched_teams_on_both_sides(monkeypatch, tmp_path, small_world):
    payload = _fake_teams_payload(
        [
            {"idTeam": "1", "strTeam": "Rosenborg BK", "strStadium": "Lerkendal Stadion",
             "intStadiumCapacity": "21166"},
            {"idTeam": "9", "strTeam": "Some Other Club", "strStadium": "Somewhere",
             "intStadiumCapacity": "1000"},
        ]
    )
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=None: _FakeResponse(payload)
    )
    report = refresh_competition(
        small_world, "eliteserien_2026", "Norwegian Eliteserien", cache_dir=tmp_path
    )
    assert report.unmatched_local_teams == ["brann_m"]
    assert report.unmatched_api_teams == ["Some Other Club"]


def test_refresh_falls_back_to_a_fresh_cache_without_calling_the_api(monkeypatch, tmp_path, small_world):
    cache_path = tmp_path / "eliteserien_2026.json"
    cache.write(
        cache_path,
        [
            {"api_team_id": "1", "name": "Rosenborg BK", "stadium_name": "Lerkendal Stadion",
             "stadium_capacity": 21166},
            {"api_team_id": "2", "name": "SK Brann", "stadium_name": "Brann Stadion",
             "stadium_capacity": 17317},
        ],
    )

    def _fail(url, timeout=None):
        raise AssertionError("should not hit the network when the cache is fresh")

    monkeypatch.setattr("urllib.request.urlopen", _fail)
    report = refresh_competition(
        small_world, "eliteserien_2026", "Norwegian Eliteserien", cache_dir=tmp_path
    )
    assert report.source == "cache"
    assert report.diffs == []


def test_refresh_falls_back_to_stale_cache_on_fetch_failure(monkeypatch, tmp_path, small_world):
    import urllib.error

    cache_path = tmp_path / "eliteserien_2026.json"
    cache.write(
        cache_path,
        [{"api_team_id": "1", "name": "Rosenborg BK", "stadium_name": "Lerkendal Stadion",
          "stadium_capacity": 21166}],
    )
    # Force it stale.
    stale = json.loads(cache_path.read_text())
    stale["fetched_at"] = time.time() - 999999
    cache_path.write_text(json.dumps(stale))

    def _raise(url, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    report = refresh_competition(
        small_world, "eliteserien_2026", "Norwegian Eliteserien", cache_dir=tmp_path
    )
    assert report.source == "stale-cache"
    assert report.error is not None


def test_refresh_falls_back_to_the_api_when_the_fresh_cache_is_malformed(
    monkeypatch, tmp_path, small_world
):
    """A cache file that's valid JSON but the wrong shape — hand-edited, or
    left over from an older, incompatible cache format — must not crash the
    refresh path. It's exactly as unusable as no cache at all: fetch fresh."""
    cache_path = tmp_path / "eliteserien_2026.json"
    cache.write(cache_path, {"this": "is not a list of team records"})

    payload = _fake_teams_payload(
        [{"idTeam": "1", "strTeam": "Rosenborg BK", "strStadium": "Lerkendal Stadion",
          "intStadiumCapacity": "21166"},
         {"idTeam": "2", "strTeam": "SK Brann", "strStadium": "Brann Stadion",
          "intStadiumCapacity": "17317"}]
    )
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda url, timeout=None: _FakeResponse(payload)
    )
    report = refresh_competition(
        small_world, "eliteserien_2026", "Norwegian Eliteserien", cache_dir=tmp_path
    )
    assert report.source == "api"
    assert report.diffs == []


def test_refresh_reports_unavailable_when_the_stale_cache_is_malformed(
    monkeypatch, tmp_path, small_world
):
    import urllib.error

    cache_path = tmp_path / "eliteserien_2026.json"
    cache.write(cache_path, [{"unexpected_shape": True}])
    stale = json.loads(cache_path.read_text())
    stale["fetched_at"] = time.time() - 999999
    cache_path.write_text(json.dumps(stale))

    def _raise(url, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    report = refresh_competition(
        small_world, "eliteserien_2026", "Norwegian Eliteserien", cache_dir=tmp_path
    )
    assert report.source == "unavailable"
    assert report.diffs == []


def test_refresh_reports_unavailable_with_no_cache_and_no_network(monkeypatch, tmp_path, small_world):
    import urllib.error

    def _raise(url, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    report = refresh_competition(
        small_world, "eliteserien_2026", "Norwegian Eliteserien", cache_dir=tmp_path
    )
    assert report.source == "unavailable"
    assert report.diffs == []
    assert report.error is not None


def test_refresh_force_ignores_a_fresh_cache(monkeypatch, tmp_path, small_world):
    cache_path = tmp_path / "eliteserien_2026.json"
    cache.write(cache_path, [{"api_team_id": "1", "name": "Rosenborg BK",
                               "stadium_name": "Lerkendal Stadion", "stadium_capacity": 21166}])

    payload = _fake_teams_payload(
        [{"idTeam": "1", "strTeam": "Rosenborg BK", "strStadium": "Lerkendal Stadion",
          "intStadiumCapacity": "21166"},
         {"idTeam": "2", "strTeam": "SK Brann", "strStadium": "Brann Stadion",
          "intStadiumCapacity": "17317"}]
    )
    calls = []

    def _urlopen(url, timeout=None):
        calls.append(url)
        return _FakeResponse(payload)

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    report = refresh_competition(
        small_world, "eliteserien_2026", "Norwegian Eliteserien", cache_dir=tmp_path, force=True
    )
    assert report.source == "api"
    assert calls  # the network path was actually used, not the cache
