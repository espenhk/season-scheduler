"""The season frame: the fixed reservations a league calendar is planned
around, resolved before any league match is placed (#110).

UEFA match weeks come first (#112): each leg of each European round, with
the teams that might play it. Cup days follow (#5): each cup round's resolved
date, midweek (`cup_week`) or on a league weekend (`cup_weekend`). The frame
also holds the season's `CommitmentSet` (#8): every cup and UEFA date with the
probability the team reaches it, hard only above the contingency threshold.
League round slots are planned from this frame in `slot_planner.py` (#6). Play-off
dates are declared by the leagues and reserved here too (#15).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from .model.loader import World
from .model.schema import WEEKDAYS, Competition, Gender, Season, Weekday
from .rounds.commitments import Commitment, CommitmentSet
from .rounds.cup_schedule import CupSchedule, build_cup_trees, schedule_cups
from .rounds.european_schedule import commitments_from_trees, resolve_european_trees
from .rounds.outcome_tree import OutcomeTree

# Fri-Mon is the span a league round occupies, so a cup date inside it
# competes with league slots.
_LEAGUE_WEEKEND = {"friday", "saturday", "sunday", "monday"}


@dataclass(frozen=True)
class UefaWeek:
    """One UEFA leg night, reserved for every team that might play it.

    `hard_teams` is the subset for whom the night is a hard reservation; the
    rest only reach it with some probability below the contingency threshold.
    """

    competition_id: str
    label: str
    date: date
    min_rest_days: int
    teams: tuple[str, ...]
    hard_teams: tuple[str, ...]
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
    falls Fri-Mon, where league rounds live, else `cup_week`. `hard_teams` are
    those for whom the round is a hard commitment rather than a contingent one.
    """

    competition_id: str
    gender: Gender
    round_id: str
    label: str
    date: date
    min_rest_days: int
    teams: tuple[str, ...]
    hard_teams: tuple[str, ...] = ()

    @property
    def weekday(self) -> Weekday:
        return WEEKDAYS[self.date.weekday()]

    @property
    def kind(self) -> Literal["cup_week", "cup_weekend"]:
        return "cup_weekend" if self.weekday in _LEAGUE_WEEKEND else "cup_week"


@dataclass(frozen=True)
class PlayoffWeek:
    """One play-off date reserved in the frame.

    `reserves_league` is False for another division's matches, shown for
    context only.
    """

    competition_id: str
    play_off_id: str
    label: str
    kind: str
    date: date
    reserves_league: bool = True
    source: str = ""

    @property
    def weekday(self) -> Weekday:
        return WEEKDAYS[self.date.weekday()]

    @property
    def iso_week(self) -> tuple[int, int]:
        year, week, _ = self.date.isocalendar()
        return year, week


@dataclass
class SeasonFrame:
    uefa_weeks: list[UefaWeek] = field(default_factory=list)
    commitments: CommitmentSet = field(default_factory=CommitmentSet)
    cup_schedules: list[CupSchedule] = field(default_factory=list)
    cup_days: list[CupDay] = field(default_factory=list)
    uefa_trees: dict[str, OutcomeTree] = field(default_factory=dict)
    cup_trees: dict[str, OutcomeTree] = field(default_factory=dict)
    playoff_weeks: list[PlayoffWeek] = field(default_factory=list)

    @classmethod
    def from_resolved(
        cls,
        world: World,
        cup_schedules: list[CupSchedule],
        commitments: CommitmentSet,
        competitions: list[Competition] | None = None,
    ) -> SeasonFrame:
        """A frame rebuilt from already-resolved cups and commitments, for
        callers that hold those rather than the frame."""
        genders = {c.competition_id: _cup_gender(world, c) for c in cup_schedules}
        return cls(
            uefa_weeks=build_uefa_weeks(commitments),
            commitments=commitments,
            cup_schedules=cup_schedules,
            cup_days=build_cup_days(cup_schedules, genders, commitments),
            playoff_weeks=build_playoff_weeks(competitions or []),
        )

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

    def _hard_cup_weekends(self, gender: Gender | None) -> list[CupDay]:
        # A contingent cup weekend leaves the slot in place; the teams that
        # might play it pay a soft penalty instead of the league losing a weekend.
        return [d for d in self.cup_days_of(gender=gender, kind="cup_weekend") if d.hard_teams]

    def playoff_dates(self, competition_id: str) -> frozenset[date]:
        """Dates a regular-season round of `competition_id` must not use."""
        return frozenset(
            w.date for w in self.playoff_weeks if w.competition_id == competition_id and w.reserves_league
        )

    def cup_weekend_dates(self, *, gender: Gender | None = None) -> frozenset[date]:
        """Dates a league round must not use: the Fri-Mon cup days some team
        is committed to."""
        return frozenset(d.date for d in self._hard_cup_weekends(gender))

    def cup_weekend_blocks(self, *, gender: Gender | None = None) -> dict[date, str]:
        """`cup_weekend_dates` with a reason per date, in the shape
        `SeasonCalendar.without` takes."""
        blocks: dict[date, str] = {}
        for day in self._hard_cup_weekends(gender):
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


def build_playoff_weeks(competitions: list[Competition]) -> list[PlayoffWeek]:
    """Every declared play-off date of the given leagues, in date order."""
    weeks = [
        PlayoffWeek(
            competition_id=competition.id,
            play_off_id=playoff.id,
            label=playoff.label,
            kind=playoff.kind,
            date=day,
            reserves_league=playoff.reserves_league,
            source=playoff.source,
        )
        for competition in competitions
        if competition.format == "league"
        for playoff in competition.playoff_rounds
        for day in playoff.dates
    ]
    return sorted(weeks, key=lambda w: (w.date, w.competition_id, w.play_off_id))


def build_uefa_weeks(commitments: CommitmentSet) -> list[UefaWeek]:
    """Group the UEFA commitments back into one entry per competition leg, in
    date order."""
    grouped: dict[tuple[str, str, date], list[Commitment]] = {}
    for commitment in commitments:
        if commitment.source != "uefa":
            continue
        key = (commitment.competition_id, commitment.label, commitment.date)
        grouped.setdefault(key, []).append(commitment)

    weeks = [
        UefaWeek(
            competition_id=competition_id,
            label=label,
            date=day,
            min_rest_days=max(c.min_rest_days for c in group),
            teams=tuple(sorted({c.team_id for c in group})),
            hard_teams=tuple(sorted({c.team_id for c in group if c.hard})),
        )
        for (competition_id, label, day), group in grouped.items()
    ]
    return sorted(weeks, key=lambda w: (w.date, w.competition_id, w.label))


def build_cup_days(
    schedules: list[CupSchedule], genders: dict[str, Gender], commitments: CommitmentSet
) -> list[CupDay]:
    """Group every cup round's per-team dates into one entry per date."""
    hard_nodes = {c.node_id for c in commitments if c.source == "cup" and c.hard}
    days: list[CupDay] = []
    for schedule in schedules:
        for placement in schedule.rounds:
            by_date: dict[date, list[str]] = {}
            for team_id, day in placement.dates.items():
                by_date.setdefault(day, []).append(team_id)
            for day, teams in by_date.items():
                hard = [
                    t
                    for t in teams
                    if f"{t}/{schedule.competition_id}/{placement.round_id}" in hard_nodes
                ]
                days.append(
                    CupDay(
                        competition_id=schedule.competition_id,
                        gender=genders[schedule.competition_id],
                        round_id=placement.round_id,
                        label=f"{schedule.competition_name}: {placement.round_name}",
                        date=day,
                        min_rest_days=schedule.min_rest_days,
                        teams=tuple(sorted(teams)),
                        hard_teams=tuple(sorted(hard)),
                    )
                )
    return sorted(days, key=lambda d: (d.date, d.competition_id, d.round_id))


def build_season_frame(world: World, season: Season) -> tuple[SeasonFrame, list[str]]:
    """Resolve the season's UEFA weeks, then its cup rounds around them.

    Both become outcome trees; their commitments are hard where the team
    reaches them with at least `season.contingency_policy.hard_threshold`.

    Raises `EuropeanCascadeError` on a data problem in the European cascade
    and `CupSchedulingError` on one in the cup rounds.
    """
    leagues = [world.competition(c) for c in season.competitions]
    frame = SeasonFrame(playoff_weeks=build_playoff_weeks(leagues))
    warnings: list[str] = []
    threshold = season.contingency_policy.hard_threshold
    if season.european_competitions:
        competitions = [world.competition(c) for c in season.european_competitions]
        trees, warnings = resolve_european_trees(competitions, season)
        commitments = commitments_from_trees(trees.values(), threshold)
        warnings = [*warnings, *(w for tree in trees.values() for w in tree.warnings)]
        frame = SeasonFrame(
            uefa_weeks=build_uefa_weeks(commitments), commitments=commitments, uefa_trees=trees,
            playoff_weeks=frame.playoff_weeks,
        )
    if season.cup_competitions:
        cups = [world.competition(c) for c in season.cup_competitions]
        frame.cup_schedules, cup_warnings = schedule_cups(
            cups, season, frame.commitments, world.team_label
        )
        frame.cup_trees = build_cup_trees(cups, frame.cup_schedules)
        cup_set = CommitmentSet(
            c for tree in frame.cup_trees.values() for c in tree.commitments(threshold)
        )
        frame.commitments = frame.commitments.merged(cup_set)
        frame.cup_days = build_cup_days(
            frame.cup_schedules, {c.id: c.gender for c in cups}, frame.commitments
        )
        warnings = [
            *warnings, *cup_warnings, *(w for tree in frame.cup_trees.values() for w in tree.warnings)
        ]
    return frame, warnings


__all__ = [
    "CupDay",
    "PlayoffWeek",
    "SeasonFrame",
    "UefaWeek",
    "build_cup_days",
    "build_playoff_weeks",
    "build_season_frame",
    "build_uefa_weeks",
]
