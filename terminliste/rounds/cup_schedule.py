"""Resolves a cup's rounds to actual dates — the cup's own, lightweight solve.

A cup's "fixtures" are never modelled as pairings: who plays whom is drawn
round by round. What is known per round is either an exact date
(`forced_date`) or a window it must fall inside. A windowed round resolves to
ONE date shared by every entrant, assuming every team survives through the
final: NFF lays the cup weeks before the season, midweek and clear of UEFA
weeks, and the league is fitted around them (#5). `team_overrides` move
individual teams off that date, e.g. a club still in Europe.

A separate one-shot pass rather than another dimension of the league's
annealing: month- or quarter-wide windows are far too coarse to need a search,
and resolving them once lets the league scheduler treat the result as fixed
input.

Home/away is resolved per round rather than per fixture, since the opponent is
unknown until the draw: every team plays away for its first three rounds (a
top-division side is assumed to draw a lower-division one and travel), then
alternates, except the final, which is always neutral ground. See
`_venue_type`.

Round N is fully placed before round N+1 starts, leaving `min_gap_days`
between them. `CupSchedulingError` is raised when a round's window (or a
forced round's confirmed date) leaves no room; a forced round never moves
to make room, so that is the data's problem to fix.

A date is chosen by preference, not guaranteed: a round whose window offers no
date clear of blackouts, midweek and UEFA legs settles for the least bad one
and says so in a warning rather than failing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from ..model.schema import WEEKDAYS, Competition, CupRound, Season
from .european_schedule import EuropeanCommitmentDate

VenueType = Literal["home", "away", "neutral"]

# The final of both the men's and women's Norwegian Cup is played at neutral
# ground, not either finalist's home venue — Ullevål stadion, Oslo.
NEUTRAL_CUP_VENUE = "Ullevål stadion"

# Days a windowed round may use when its preferred weekday is unavailable.
_MIDWEEK = ("tuesday", "wednesday", "thursday")
_WEEKEND = ("saturday", "sunday")


class CupSchedulingError(Exception):
    """A cup's declared rounds cannot be resolved to a valid schedule — a data
    problem (widen the window, or revisit the forced date), not something a
    solver retry can fix."""


@dataclass(frozen=True)
class CupRoundPlacement:
    """One round, resolved: every entered team's actual date for it."""

    round_id: str
    round_name: str
    dates: dict[str, date]
    venue_type: VenueType = "away"
    note: str = ""

    @property
    def earliest_date(self) -> date:
        return min(self.dates.values())

    @property
    def latest_date(self) -> date:
        return max(self.dates.values())

    @property
    def spread_days(self) -> int:
        return (self.latest_date - self.earliest_date).days


@dataclass(frozen=True)
class CupSchedule:
    """A cup competition's rounds, resolved to dates, in play order."""

    competition_id: str
    competition_name: str
    competition_short_name: str | None
    min_rest_days: int
    rounds: list[CupRoundPlacement] = field(default_factory=list)
    solver_commitments: bool = True


def schedule_cup(
    competition: Competition,
    blackouts: set[date],
    european_commitments: dict[str, list[EuropeanCommitmentDate]] | None = None,
) -> tuple[CupSchedule, list[str]]:
    """Resolve one cup competition's rounds to dates.

    Returns the schedule plus any warnings (empty in the normal case). Raises
    `CupSchedulingError` if the declared rounds cannot be placed in order at
    all — a data problem, not something worth degrading gracefully over.
    """
    if competition.format != "cup":
        raise CupSchedulingError(f"{competition.id!r} is not a cup competition")
    if not competition.cup_rounds:
        raise CupSchedulingError(f"cup competition {competition.id!r} declares no cup_rounds")

    gap = max(competition.min_gap_days, 1)
    uefa = _uefa_legs(competition.teams, european_commitments)

    placements: list[CupRoundPlacement] = []
    warnings: list[str] = []
    previous_latest: date | None = None
    total_rounds = len(competition.cup_rounds)

    for index, round_ in enumerate(competition.cup_rounds):
        main_teams = [t for t in competition.teams if t not in round_.team_overrides]
        if round_.is_forced:
            main_date = _check_forced(competition, round_, previous_latest, gap)
            if main_date in blackouts:
                warnings.append(
                    f"{competition.name}: {round_.name} has no legal (non-blackout) date in its "
                    f"window — placed on {main_date} anyway"
                )
        else:
            main_date, round_warnings = _pick_date(
                competition, round_, previous_latest, gap, blackouts, uefa, set(main_teams)
            )
            warnings.extend(round_warnings)

        team_dates = {team: main_date for team in main_teams}
        team_dates.update(
            {t: d for t, d in round_.team_overrides.items() if t in competition.teams}
        )
        for team_id, day in sorted(round_.team_overrides.items()):
            if day in blackouts:
                warnings.append(
                    f"{competition.name}: {round_.name} override for {team_id} falls on "
                    f"blackout date {day}"
                )
        placement = CupRoundPlacement(
            round_id=round_.id,
            round_name=round_.name,
            dates=team_dates,
            venue_type=_venue_type(index, total_rounds),
            note=round_.note,
        )
        placements.append(placement)
        if round_.is_forced:
            warnings.extend(_uefa_warnings(competition, round_, team_dates, uefa))
        previous_latest = placement.latest_date

    return (
        CupSchedule(
            competition_id=competition.id,
            competition_name=competition.name,
            competition_short_name=competition.short_name,
            min_rest_days=competition.min_rest_days,
            rounds=placements,
            solver_commitments=competition.solver_commitments,
        ),
        warnings,
    )


def _venue_type(index: int, total_rounds: int) -> VenueType:
    """Home/away for round `index` (0-based) of `total_rounds`.

    Uniform across every entered team, since whose ground a tie is really
    played at is settled by a draw made closer to the round. The final
    overrides everything: always neutral ground, even in a cup short enough
    for it to fall inside the first-three-rounds-away stretch.
    """
    if index == total_rounds - 1:
        return "neutral"
    if index < 3:
        return "away"
    return "home" if (index - 3) % 2 == 0 else "away"


def _check_forced(
    competition: Competition, round_: CupRound, previous_latest: date | None, gap: int
) -> date:
    """A forced round's confirmed date, after the same rest-gap check a
    windowed round faces: a date too close to the previous round is a real
    conflict for the data to fix."""
    forced = round_.forced_date
    if previous_latest is not None and (forced - previous_latest).days < gap:
        raise CupSchedulingError(
            f"{competition.id!r}: {round_.name} is forced to {forced}, which does not "
            f"leave room after the previous round's last date ({previous_latest}) — needs "
            f"at least {gap} day(s)"
        )
    return forced


@dataclass(frozen=True)
class _UefaLeg:
    team_id: str
    date: date
    min_rest_days: int
    binding: bool


def _uefa_legs(
    teams: list[str], commitments: dict[str, list[EuropeanCommitmentDate]] | None
) -> list[_UefaLeg]:
    return [
        _UefaLeg(team, c.date, c.min_rest_days, c.binding)
        for team in teams
        for c in (commitments or {}).get(team, ())
    ]


def _clashes(day: date, rest: int, leg: _UefaLeg) -> bool:
    return abs((day - leg.date).days) - 1 < max(rest, leg.min_rest_days)


def _uefa_level(day: date, rest: int, uefa: list[_UefaLeg], teams: set[str]) -> int:
    """0 clear, 1 clashes only with legs a team might reach, 2 clashes with a
    leg some team is certain to play."""
    level = 0
    for leg in uefa:
        if leg.team_id in teams and _clashes(day, rest, leg):
            if leg.binding:
                return 2
            level = 1
    return level


def _binding_clashes(day: date, rest: int, uefa: list[_UefaLeg], teams: set[str]) -> list[str]:
    return sorted(
        {
            leg.team_id
            for leg in uefa
            if leg.team_id in teams and leg.binding and _clashes(day, rest, leg)
        }
    )


def _pick_date(
    competition: Competition,
    round_: CupRound,
    previous_latest: date | None,
    gap: int,
    blackouts: set[date],
    uefa: list[_UefaLeg],
    teams: set[str],
) -> tuple[date, list[str]]:
    """The one date a windowed round is played on for `teams`.

    Candidates are ranked: inside the preferred weekday's class (midweek, or
    Sat/Sun for a weekend preference), then UEFA-clear, then the preferred
    weekday itself, then earliest. The window's earliest legal day is the
    last resort when blackouts leave nothing.
    """
    earliest = round_.window_start
    if previous_latest is not None:
        earliest = max(earliest, previous_latest + timedelta(days=gap))
    if earliest > round_.window_end:
        raise CupSchedulingError(
            f"{competition.id!r}: {round_.name}'s window ({round_.window_start} – "
            f"{round_.window_end}) leaves no room after the previous round's last date "
            f"({previous_latest}) — it would need to start on or after {earliest}"
        )

    preferred = WEEKDAYS.index(round_.preferred_weekday)
    day_class = _WEEKEND if round_.preferred_weekday in _WEEKEND else _MIDWEEK
    class_days = {WEEKDAYS.index(w) for w in day_class} | {preferred}
    rest = competition.min_rest_days

    ranked: list[tuple[tuple[int, int, int, date], date, int]] = []
    day = earliest
    while day <= round_.window_end:
        if day not in blackouts:
            level = _uefa_level(day, rest, uefa, teams)
            key = (
                0 if day.weekday() in class_days else 1,
                level,
                0 if day.weekday() == preferred else 1,
                day,
            )
            ranked.append((key, day, level))
        day += timedelta(days=1)

    if not ranked:
        return earliest, [
            f"{competition.name}: {round_.name} has no legal (non-blackout) date in its "
            f"window — placed on {earliest} anyway"
        ]

    key, chosen, level = min(ranked)
    warnings: list[str] = []
    if key[0]:
        warnings.append(
            f"{competition.name}: {round_.name} has no {round_.preferred_weekday}-class date "
            f"in its window — placed on {chosen} ({chosen.strftime('%A')})"
        )
    if level == 2:
        clashing = ", ".join(_binding_clashes(chosen, rest, uefa, teams))
        warnings.append(
            f"{competition.name}: {round_.name} on {chosen} sits inside a UEFA week for {clashing}"
        )
    return chosen, warnings


def _uefa_warnings(
    competition: Competition,
    round_: CupRound,
    team_dates: dict[str, date],
    uefa: list[_UefaLeg],
) -> list[str]:
    """One warning per forced-round date that sits in a UEFA week of an
    entrant: the date cannot move, so the clash is surfaced rather than fixed."""
    warnings: list[str] = []
    for day in sorted(set(team_dates.values())):
        teams = {t for t, d in team_dates.items() if d == day}
        clashing = _binding_clashes(day, competition.min_rest_days, uefa, teams)
        if clashing:
            warnings.append(
                f"{competition.name}: {round_.name} is forced to {day}, inside a UEFA week "
                f"for {', '.join(clashing)}"
            )
    return warnings


def schedule_cups(
    competitions: list[Competition],
    season: Season,
    european_commitments: dict[str, list[EuropeanCommitmentDate]] | None = None,
) -> tuple[list[CupSchedule], list[str]]:
    """Resolve every cup competition, pooling their warnings.

    Global blackouts plus the ranges scoped to the cup's gender apply, so a
    FIFA window stops the matching cup. Venue blackouts don't: cups book no
    venues. Blackouts are plain dates, so they cover rounds outside the
    season's own start..end too (the cup runs into 2027).
    """
    schedules: list[CupSchedule] = []
    warnings: list[str] = []
    for competition in competitions:
        blackouts = set(season.blacked_out_dates) | set(season.scoped_blackout_dates(competition))
        schedule, round_warnings = schedule_cup(competition, blackouts, european_commitments)
        schedules.append(schedule)
        warnings.extend(round_warnings)
    return schedules, warnings


def resolved_cup_windows(schedules: list[CupSchedule]) -> dict[str, list[tuple[date, int]]]:
    """Per-team (resolved date, min rest days) pairs, for conflict avoidance
    by `CupRoundConflict` and both solver backends."""
    windows: dict[str, list[tuple[date, int]]] = {}
    for schedule in schedules:
        for placement in schedule.rounds:
            for team_id, day in placement.dates.items():
                windows.setdefault(team_id, []).append((day, schedule.min_rest_days))
    return windows


def cup_conflict(cup_windows: dict[str, list[tuple[date, int]]], team_id: str, day: date) -> bool:
    """Whether `day` falls inside one of `team_id`'s cup-round rest windows.

    The greedy placer uses it to steer initial placement; CP-SAT uses it to
    exclude conflicting dates from a fixture's candidate set up front, since
    its fixtures cannot route around a conflict found after the fact the way
    annealing can relocate a whole round.
    """
    return any(
        abs((day - cup_date).days) - 1 < minimum
        for cup_date, minimum in cup_windows.get(team_id, ())
    )


__all__ = [
    "NEUTRAL_CUP_VENUE",
    "CupRoundPlacement",
    "CupSchedule",
    "CupSchedulingError",
    "VenueType",
    "cup_conflict",
    "resolved_cup_windows",
    "schedule_cup",
    "schedule_cups",
]
