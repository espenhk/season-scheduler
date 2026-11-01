"""The season frame: the fixed reservations a league calendar is planned
around, resolved before any league match is placed (#110).

UEFA match weeks come first (#112): each leg of each European round, with
the teams that might play it. Later stages add cup weeks and league slots
alongside them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from .model.loader import World
from .model.schema import WEEKDAYS, Season, Weekday
from .rounds.european_schedule import EuropeanCommitmentDate, resolve_european_commitments


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


@dataclass
class SeasonFrame:
    uefa_weeks: list[UefaWeek] = field(default_factory=list)
    european_commitments: dict[str, list[EuropeanCommitmentDate]] = field(default_factory=dict)


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


def build_season_frame(world: World, season: Season) -> tuple[SeasonFrame, list[str]]:
    """Resolve the season's UEFA weeks. Raises `EuropeanCascadeError` on a
    data problem in the European cascade."""
    if not season.european_competitions:
        return SeasonFrame(), []
    competitions = [world.competition(c) for c in season.european_competitions]
    commitments, warnings = resolve_european_commitments(competitions, season)
    frame = SeasonFrame(uefa_weeks=build_uefa_weeks(commitments), european_commitments=commitments)
    return frame, warnings


__all__ = ["SeasonFrame", "UefaWeek", "build_season_frame", "build_uefa_weeks"]
