"""Resolves a cup's rounds to actual dates — the cup's own, lightweight solve.

A cup's "fixtures" are never modelled as pairings: who plays whom is drawn
round by round. What is known per round is either an exact date
(`forced_date`) or a window it must fall inside, so resolving a round means
picking one placement per entered team inside that constraint, assuming every
team survives through the final.

A separate one-shot pass rather than another dimension of the league's
annealing: month- or quarter-wide windows are far too coarse to need a search,
and resolving them once lets the league scheduler treat the result as fixed
input.

Home/away is resolved per round rather than per fixture, since the opponent is
unknown until the draw: every team plays away for its first three rounds (a
top-division side is assumed to draw a lower-division one and travel), then
alternates, except the final, which is always neutral ground. See
`_venue_type`.

Two rules hold by construction rather than being discovered as violations
later:

- Round N is fully placed before round N+1 starts, leaving `min_gap_days`
  between them. `CupSchedulingError` is raised when a round's window (or a
  forced round's confirmed date) leaves no room; a forced round never moves
  to make room, so that is the data's problem to fix.
- Every team's placement for a round falls inside one calendar week: a
  windowed round runs forward from its earliest legal start across at most
  `match_window_days` days, and a forced round has no spread at all.

What can't be guaranteed either way is a *legal* date surviving blackouts. If
none does, placement falls back to the round's anchor with a warning rather
than an error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from ..model.schema import Competition, CupRound, Season

VenueType = Literal["home", "away", "neutral"]

# The final of both the men's and women's Norwegian Cup is played at neutral
# ground, not either finalist's home venue — Ullevål stadion, Oslo.
NEUTRAL_CUP_VENUE = "Ullevål stadion"


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


def schedule_cup(competition: Competition, blackouts: set[date]) -> tuple[CupSchedule, list[str]]:
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
    spread = max(competition.match_window_days, 0)

    placements: list[CupRoundPlacement] = []
    warnings: list[str] = []
    previous_latest: date | None = None
    total_rounds = len(competition.cup_rounds)

    for index, round_ in enumerate(competition.cup_rounds):
        anchor, lower_bound, upper_bound = _resolve_anchor(
            competition, round_, previous_latest, gap
        )
        team_dates, blacked_out = _spread_teams(
            competition.teams, anchor, spread, blackouts, lower_bound, upper_bound
        )
        placement = CupRoundPlacement(
            round_id=round_.id,
            round_name=round_.name,
            dates=team_dates,
            venue_type=_venue_type(index, total_rounds),
            note=round_.note,
        )
        placements.append(placement)
        if blacked_out:
            warnings.append(
                f"{competition.name}: {round_.name} has no legal (non-blackout) date in its "
                f"window — placed on {anchor} anyway"
            )
        previous_latest = placement.latest_date

    return (
        CupSchedule(
            competition_id=competition.id,
            competition_name=competition.name,
            competition_short_name=competition.short_name,
            min_rest_days=competition.min_rest_days,
            rounds=placements,
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


def _resolve_anchor(
    competition: Competition,
    round_: CupRound,
    previous_latest: date | None,
    gap: int,
) -> tuple[date, date | None, date | None]:
    """The round's anchor date, plus the bounds team placements must respect.

    A forced round's bounds collapse to the confirmed date at both ends, so
    `_spread_teams` cannot drift off it. It still faces the same rest-gap
    check a windowed round does: a confirmed date too close to the previous
    round is a real conflict for the data to fix.
    """
    if round_.is_forced:
        anchor = round_.forced_date
        if previous_latest is not None and (anchor - previous_latest).days < gap:
            raise CupSchedulingError(
                f"{competition.id!r}: {round_.name} is forced to {anchor}, which does not "
                f"leave room after the previous round's last date ({previous_latest}) — needs "
                f"at least {gap} day(s)"
            )
        return anchor, anchor, anchor

    earliest = round_.window_start
    if previous_latest is not None:
        earliest = max(earliest, previous_latest + timedelta(days=gap))
    if earliest > round_.window_end:
        raise CupSchedulingError(
            f"{competition.id!r}: {round_.name}'s window ({round_.window_start} – "
            f"{round_.window_end}) leaves no room after the previous round's last date "
            f"({previous_latest}) — it would need to start on or after {earliest}"
        )
    return earliest, earliest, round_.window_end


def _spread_teams(
    teams: list[str],
    anchor: date,
    spread: int,
    blackouts: set[date],
    lower_bound: date | None,
    upper_bound: date | None,
) -> tuple[dict[str, date], bool]:
    """Distribute `teams` round-robin over the days from `anchor` onward.

    Candidates run forward only, `anchor` to `anchor + spread`: the anchor is
    either a confirmed date or a window's earliest legal start, so nothing
    should land before it. The spread reflects a real cup round playing out
    over a few days — a forced round has its bounds collapsed to the anchor
    and gets none.

    Blackouts are dropped from whatever survives the bounds; if nothing is
    left every team falls back onto the bare anchor, flagged by the second
    return value, since that beats failing outright over one clashing day.
    """
    candidates: list[date] = []
    for offset in range(0, spread + 1):
        day = anchor + timedelta(days=offset)
        if lower_bound is not None and day < lower_bound:
            continue
        if upper_bound is not None and day > upper_bound:
            continue
        if day in blackouts:
            continue
        candidates.append(day)

    blacked_out = False
    if not candidates:
        candidates = [anchor]
        blacked_out = anchor in blackouts

    dates = {team_id: candidates[i % len(candidates)] for i, team_id in enumerate(sorted(teams))}
    return dates, blacked_out


def schedule_cups(
    competitions: list[Competition], season: Season
) -> tuple[list[CupSchedule], list[str]]:
    """Resolve every cup competition, pooling their warnings.

    Only global blackouts are consulted: cups book no venues, so
    venue-specific blackouts don't apply to them.
    """
    blackouts = set(season.blacked_out_dates)
    schedules: list[CupSchedule] = []
    warnings: list[str] = []
    for competition in competitions:
        schedule, round_warnings = schedule_cup(competition, blackouts)
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
