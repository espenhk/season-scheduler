"""The season frame: the fixed reservations a league calendar is planned
around, resolved before any league match is placed (#110).

UEFA match weeks come first (#112): each leg of each European round, with
the teams that might play it. Cup days follow (#5): each cup round's resolved
date, midweek (`cup_week`) or on a league weekend (`cup_weekend`). League round
slots are planned from this frame in `slot_planner.py` (#6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from .model.loader import World
from .model.schema import WEEKDAYS, Gender, Season, Weekday
from .rounds.cup_schedule import CupSchedule, schedule_cups
from .rounds.european_schedule import EuropeanCommitmentDate, resolve_european_commitments

# Fri-Mon is the span a league round occupies, so a cup date inside it
# competes with league slots.
_LEAGUE_WEEKEND = {"friday", "saturday", "sunday", "monday"}


@dataclass(frozen=True)
class UefaWeek:
    """One UEFA leg night, reserved for every team that might play it.

    `binding_teams` is the subset for whom the night is a hard reservation;
    the rest only reach it via one branch of a qualifying cascade.
    """

    competition_id: str
    label: str
    date: date
    min_rest_days: int
    teams: tuple[str, ...]
    binding_teams: tuple[str, ...]
    kind: Literal["uefa_week"] = "uefa_week"

    @property
    def weekday(self) -> Weekday:
        return WEEKDAYS[self.date.weekday()]

    @property
    def iso_week(self) -> tuple[int, int]:
        year, week, _ = self.date.isocalendar()
        return year, week


@dataclass(frozen=True)
class CupDay:
    """One cup round's date for the teams playing it that day.

    A round yields one entry per distinct date, so a round with
    `team_overrides` yields several. `kind` is `cup_weekend` when the date
    falls Fri-Mon, where league rounds live, else `cup_week`.
    """

    competition_id: str
    gender: Gender
    round_id: str
    label: str
    date: date
    min_rest_days: int
    teams: tuple[str, ...]

    @property
    def weekday(self) -> Weekday:
        return WEEKDAYS[self.date.weekday()]

    @property
    def kind(self) -> Literal["cup_week", "cup_weekend"]:
        return "cup_weekend" if self.weekday in _LEAGUE_WEEKEND else "cup_week"


@dataclass
class SeasonFrame:
    uefa_weeks: list[UefaWeek] = field(default_factory=list)
    european_commitments: dict[str, list[EuropeanCommitmentDate]] = field(default_factory=dict)
    cup_schedules: list[CupSchedule] = field(default_factory=list)
    cup_days: list[CupDay] = field(default_factory=list)

    @classmethod
    def from_resolved(
        cls,
        world: World,
        cup_schedules: list[CupSchedule],
        european_commitments: dict[str, list[EuropeanCommitmentDate]],
    ) -> SeasonFrame:
        """A frame rebuilt from already-resolved cups and commitments, for
        callers that hold those rather than the frame."""
        genders = {c.competition_id: _cup_gender(world, c) for c in cup_schedules}
        return cls(
            uefa_weeks=build_uefa_weeks(european_commitments),
            european_commitments=european_commitments,
            cup_schedules=cup_schedules,
            cup_days=build_cup_days(cup_schedules, genders),
        )

    @property
    def solver_cup_schedules(self) -> list[CupSchedule]:
        """The cups that bind the scorer and solvers (`solver_commitments`)."""
        return [s for s in self.cup_schedules if s.solver_commitments]

    def cup_days_of(
        self,
        *,
        gender: Gender | None = None,
        competition_id: str | None = None,
        kind: Literal["cup_week", "cup_weekend"] | None = None,
        team_id: str | None = None,
    ) -> list[CupDay]:
        """Cup days matching every given filter, in date order."""
        return [
            day
            for day in self.cup_days
            if (gender is None or day.gender == gender)
            and (competition_id is None or day.competition_id == competition_id)
            and (kind is None or day.kind == kind)
            and (team_id is None or team_id in day.teams)
        ]

    def cup_weekend_dates(self, *, gender: Gender | None = None) -> frozenset[date]:
        """Dates a league round must not use: the Fri-Mon cup days."""
        return frozenset(d.date for d in self.cup_days_of(gender=gender, kind="cup_weekend"))

    def cup_weekend_blocks(self, *, gender: Gender | None = None) -> dict[date, str]:
        """`cup_weekend_dates` with a reason per date, in the shape
        `SeasonCalendar.without` takes."""
        blocks: dict[date, str] = {}
        for day in self.cup_days_of(gender=gender, kind="cup_weekend"):
            blocks[day.date] = f"{blocks[day.date]}; {day.label}" if day.date in blocks else day.label
        return blocks


def _cup_gender(world: World, schedule: CupSchedule) -> Gender:
    """The cup's gender: declared, else its entrants'."""
    competition = world.competitions.get(schedule.competition_id)
    if competition is not None:
        return competition.gender
    for placement in schedule.rounds:
        for team_id in placement.dates:
            if team_id in world.teams:
                return world.teams[team_id].gender
    return "men"


def build_uefa_weeks(
    commitments_by_team: dict[str, list[EuropeanCommitmentDate]],
) -> list[UefaWeek]:
    """Group per-team commitments back into one entry per competition leg,
    in date order."""
    grouped: dict[tuple[str, str, date], list[EuropeanCommitmentDate]] = {}
    for commitments in commitments_by_team.values():
        for commitment in commitments:
            key = (commitment.competition_id, commitment.label, commitment.date)
            grouped.setdefault(key, []).append(commitment)

    weeks = [
        UefaWeek(
            competition_id=competition_id,
            label=label,
            date=day,
            min_rest_days=max(c.min_rest_days for c in commitments),
            teams=tuple(sorted({c.team_id for c in commitments})),
            binding_teams=tuple(sorted({c.team_id for c in commitments if c.binding})),
        )
        for (competition_id, label, day), commitments in grouped.items()
    ]
    return sorted(weeks, key=lambda w: (w.date, w.competition_id, w.label))


def build_cup_days(
    schedules: list[CupSchedule], genders: dict[str, Gender]
) -> list[CupDay]:
    """Group every cup round's per-team dates into one entry per date."""
    days: list[CupDay] = []
    for schedule in schedules:
        for placement in schedule.rounds:
            by_date: dict[date, list[str]] = {}
            for team_id, day in placement.dates.items():
                by_date.setdefault(day, []).append(team_id)
            for day, teams in by_date.items():
                days.append(
                    CupDay(
                        competition_id=schedule.competition_id,
                        gender=genders[schedule.competition_id],
                        round_id=placement.round_id,
                        label=f"{schedule.competition_name}: {placement.round_name}",
                        date=day,
                        min_rest_days=schedule.min_rest_days,
                        teams=tuple(sorted(teams)),
                    )
                )
    return sorted(days, key=lambda d: (d.date, d.competition_id, d.round_id))


def build_season_frame(world: World, season: Season) -> tuple[SeasonFrame, list[str]]:
    """Resolve the season's UEFA weeks, then its cup rounds around them.

    Raises `EuropeanCascadeError` on a data problem in the European cascade
    and `CupSchedulingError` on one in the cup rounds.
    """
    frame = SeasonFrame()
    warnings: list[str] = []
    if season.european_competitions:
        competitions = [world.competition(c) for c in season.european_competitions]
        commitments, warnings = resolve_european_commitments(competitions, season)
        frame = SeasonFrame(
            uefa_weeks=build_uefa_weeks(commitments), european_commitments=commitments
        )
    if season.cup_competitions:
        cups = [world.competition(c) for c in season.cup_competitions]
        frame.cup_schedules, cup_warnings = schedule_cups(
            cups, season, frame.european_commitments
        )
        frame.cup_days = build_cup_days(frame.cup_schedules, {c.id: c.gender for c in cups})
        warnings = [*warnings, *cup_warnings]
    return frame, warnings


__all__ = [
    "CupDay",
    "SeasonFrame",
    "UefaWeek",
    "build_cup_days",
    "build_season_frame",
    "build_uefa_weeks",
]
