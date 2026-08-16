"""Small hand-built worlds for constraint unit tests.

Full-data fixtures (`world`, `season` in conftest.py) are right for integration
tests but make it hard to see *why* a constraint fired — a violation in a
16-team schedule is buried in 240 matches. These factories build minimal
worlds with exactly the shape a single test needs, so each constraint test can
state its scenario in four or five lines.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from terminliste.model.loader import World
from terminliste.model.schema import Club, Competition, FixedRequirement, Match, Season, Team, Venue


def venue(id: str, lat: float = 60.0, lon: float = 10.0, capacity: int = 1000) -> Venue:
    return Venue(id=id, name=id.replace("_", " ").title(), city=id, lat=lat, lon=lon, capacity=capacity)


def team(id: str, club_id: str, home_venue: str, gender: str = "men", level: str = "senior") -> Team:
    t = Team(id=id, gender=gender, level=level, home_venue=home_venue)
    t.club_id = club_id
    return t


def club(id: str, teams: list[Team]) -> Club:
    return Club(id=id, name=id.replace("_", " ").title(), short_name=id[:3].upper(), city=id, teams=teams)


def competition(
    id: str,
    teams: list[str],
    gender: str = "men",
    preferred_weekday: str = "sunday",
    min_rest_days: int = 3,
    match_window_days: int = 3,
    comfortable_rest_days: int = 6,
    weights: dict | None = None,
    rounds_per_pairing: int = 2,
    start: date | None = None,
    end: date | None = None,
) -> Competition:
    return Competition(
        id=id,
        name=id,
        season=2026,
        gender=gender,
        team_count=len(teams),
        teams=teams,
        preferred_weekday=preferred_weekday,
        min_rest_days=min_rest_days,
        match_window_days=match_window_days,
        comfortable_rest_days=comfortable_rest_days,
        weights=weights or {},
        rounds_per_pairing=rounds_per_pairing,
        start=start,
        end=end,
    )


def season(
    id: str = "test",
    competitions: list[str] | None = None,
    global_blackouts=None,
    venue_blackouts=None,
    fixed_requirements: list[FixedRequirement] | None = None,
    discouraged_dates=None,
    start: date = date(2026, 1, 1),
    end: date = date(2026, 12, 31),
) -> Season:
    return Season(
        id=id,
        year=2026,
        start=start,
        end=end,
        competitions=competitions or [],
        global_blackouts=global_blackouts or [],
        venue_blackouts=venue_blackouts or [],
        fixed_requirements=fixed_requirements or [],
        discouraged_dates=discouraged_dates or [],
    )


def world(clubs: list[Club], venues: list[Venue], competitions: list[Competition]) -> World:
    return World(
        data_root=Path("."),
        venues={v.id: v for v in venues},
        clubs={c.id: c for c in clubs},
        teams={t.id: t for c in clubs for t in c.teams},
        competitions={c.id: c for c in competitions},
        seasons={},
    )


def match(
    competition_id: str,
    home: str,
    away: str,
    day: date,
    venue_id: str,
    leg: int = 1,
    round_index: int = 0,
) -> Match:
    return Match(
        competition_id=competition_id,
        home_team=home,
        away_team=away,
        leg=leg,
        round_index=round_index,
        date=day,
        venue=venue_id,
    )
