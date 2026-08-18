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
from terminliste.model.schema import (
    Club,
    Competition,
    CupRound,
    EuropeanLeg,
    EuropeanRound,
    EuropeanTie,
    FixedRequirement,
    FullRoundRequirement,
    Match,
    RivalryFixture,
    Season,
    Team,
    Venue,
)
from terminliste.rounds.cup_schedule import CupRoundPlacement, CupSchedule


def venue(
    id: str,
    lat: float = 60.0,
    lon: float = 10.0,
    capacity: int = 1000,
    surface: str = "grass",
) -> Venue:
    return Venue(
        id=id,
        name=id.replace("_", " ").title(),
        city=id,
        lat=lat,
        lon=lon,
        capacity=capacity,
        surface=surface,
    )


def team(id: str, club_id: str, home_venue: str, gender: str = "men", level: str = "senior") -> Team:
    t = Team(id=id, gender=gender, level=level, home_venue=home_venue)
    t.club_id = club_id
    return t


def club(id: str, teams: list[Team], color: str = "#336699") -> Club:
    return Club(
        id=id,
        name=id.replace("_", " ").title(),
        short_name=id[:3].upper(),
        city=id,
        color=color,
        teams=teams,
    )


def competition(
    id: str,
    teams: list[str],
    gender: str = "men",
    preferred_weekday: str = "sunday",
    min_rest_days: int = 3,
    match_window_days: int = 3,
    comfortable_rest_days: int = 5,
    weights: dict | None = None,
    rounds_per_pairing: int = 2,
    format: str = "league",
    movable: bool | None = None,
    cup_rounds: list[CupRound] | None = None,
    european_rounds: list[EuropeanRound] | None = None,
    start: date | None = None,
    end: date | None = None,
    final_round_kickoff_time: str = "18:00",
    kickoff_slots: list[str] | None = None,
) -> Competition:
    return Competition(
        id=id,
        name=id,
        season=2026,
        gender=gender,
        format=format,
        movable=movable if movable is not None else (format == "league"),
        team_count=len(teams),
        teams=teams,
        preferred_weekday=preferred_weekday,
        min_rest_days=min_rest_days,
        match_window_days=match_window_days,
        comfortable_rest_days=comfortable_rest_days,
        weights=weights or {},
        rounds_per_pairing=rounds_per_pairing,
        cup_rounds=cup_rounds or [],
        european_rounds=european_rounds or [],
        start=start,
        end=end,
        final_round_kickoff_time=final_round_kickoff_time,
        kickoff_slots=kickoff_slots or ["14:00", "18:00", "20:00"],
    )


def cup_round(
    id: str,
    forced_date: date | None = None,
    window_start: date | None = None,
    window_end: date | None = None,
    granularity: str | None = None,
    name: str | None = None,
    note: str = "",
) -> CupRound:
    return CupRound(
        id=id,
        name=name or id,
        forced_date=forced_date,
        window_start=window_start,
        window_end=window_end,
        granularity=granularity,
        note=note,
    )


def european_leg(
    forced_date: date | None = None,
    window_start: date | None = None,
    window_end: date | None = None,
    granularity: str | None = None,
) -> EuropeanLeg:
    return EuropeanLeg(
        forced_date=forced_date,
        window_start=window_start,
        window_end=window_end,
        granularity=granularity,
    )


def european_tie(team: str, opponent: str = "TBD", home_leg: str | None = None) -> EuropeanTie:
    return EuropeanTie(team=team, opponent=opponent, home_leg=home_leg)


def european_round(
    id: str,
    entrants: list[str] | None = None,
    forced_date: date | None = None,
    window_start: date | None = None,
    window_end: date | None = None,
    granularity: str | None = None,
    first_leg: EuropeanLeg | None = None,
    second_leg: EuropeanLeg | None = None,
    ties: list[EuropeanTie] | None = None,
    name: str | None = None,
    note: str = "",
    drop_to_competition: str | None = None,
    drop_to_round: str | None = None,
) -> EuropeanRound:
    """`forced_date`/`window_*` give both legs the same date/window — the
    common case for a test that doesn't care about leg-level granularity.
    Pass `first_leg`/`second_leg` explicitly for one that does. `entrants`
    builds a plain `EuropeanTie` (opponent "TBD") per team; pass `ties`
    directly for a test that needs opponent/home_leg detail."""
    if first_leg is None:
        first_leg = european_leg(forced_date, window_start, window_end, granularity)
    if second_leg is None:
        second_leg = european_leg(forced_date, window_start, window_end, granularity)
    return EuropeanRound(
        id=id,
        name=name or id,
        first_leg=first_leg,
        second_leg=second_leg,
        ties=ties if ties is not None else [european_tie(team) for team in (entrants or [])],
        note=note,
        drop_to_competition=drop_to_competition,
        drop_to_round=drop_to_round,
    )


def cup_placement(
    round_id: str,
    dates: dict[str, date],
    round_name: str | None = None,
    venue_type: str = "away",
    note: str = "",
) -> CupRoundPlacement:
    return CupRoundPlacement(
        round_id=round_id,
        round_name=round_name or round_id,
        dates=dates,
        venue_type=venue_type,
        note=note,
    )


def cup_schedule(
    competition_id: str,
    rounds: list[CupRoundPlacement],
    min_rest_days: int = 3,
    competition_name: str | None = None,
) -> CupSchedule:
    return CupSchedule(
        competition_id=competition_id,
        competition_name=competition_name or competition_id,
        min_rest_days=min_rest_days,
        rounds=rounds,
    )


def season(
    id: str = "test",
    competitions: list[str] | None = None,
    cup_competitions: list[str] | None = None,
    european_competitions: list[str] | None = None,
    global_blackouts=None,
    venue_blackouts=None,
    fixed_requirements: list[FixedRequirement] | None = None,
    full_round_requirements: list[FullRoundRequirement] | None = None,
    rivalry_fixtures: list[RivalryFixture] | None = None,
    discouraged_dates=None,
    start: date = date(2026, 1, 1),
    end: date = date(2026, 12, 31),
    year: int = 2026,
) -> Season:
    return Season(
        id=id,
        year=year,
        start=start,
        end=end,
        competitions=competitions or [],
        cup_competitions=cup_competitions or [],
        european_competitions=european_competitions or [],
        global_blackouts=global_blackouts or [],
        venue_blackouts=venue_blackouts or [],
        fixed_requirements=fixed_requirements or [],
        full_round_requirements=full_round_requirements or [],
        rivalry_fixtures=rivalry_fixtures or [],
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
    kickoff_time: str | None = None,
) -> Match:
    return Match(
        competition_id=competition_id,
        home_team=home,
        away_team=away,
        leg=leg,
        round_index=round_index,
        date=day,
        venue=venue_id,
        kickoff_time=kickoff_time,
    )
