"""Resolves a cup's rounds to actual dates — the cup's own, lightweight solve.

Unlike a league, a cup's "fixtures" are never modelled as team-vs-team
pairings: who plays whom is drawn round by round and unknown ahead of time.
What *is* known, per round, is either an exact date (`forced_date` — already
announced) or a window it must fall inside (`window_start`/`window_end` — a
week, month or quarter, per `CupRound.granularity`). Resolving a round means
picking one placement per entered team inside that constraint, on the base
assumption (see the round-robin module's cup docs) that every team is still
in the competition through the final.

This is deliberately a separate, one-shot pass rather than another dimension
of the league's simulated annealing: cup rounds are far too coarse-grained
(month- or quarter-wide windows) to need a search, and resolving them once
lets the league scheduler treat the result as fixed input — exactly as it
already treated a cup's dates before this module existed.

Two rules are enforced here, matching how the season's own round-robin
guarantees things by construction rather than leaving them to be discovered
as violations later:

- Hard: round N is fully placed before round N+1's first placement, honouring
  `min_rest_days` as the required gap between them. Enforced by construction
  — each round's earliest possible placement is bumped past the previous
  round's latest actual one, plus the gap — and `CupSchedulingError` is
  raised if a round's own window (or, for a forced round, the confirmed date
  itself) leaves no room left to satisfy it. A forced round never moves to
  make room; if it's too close to the previous round, that is the data's
  problem to fix, not something placement can paper over.
- Soft ("as far as possible"): every team's placement for a round falls
  inside one calendar week. Enforced by construction too: a windowed round's
  placements run forward from its earliest legal start across at most
  `match_window_days` days (`match_window_days: 3` gives a 4-day span), so
  the competition's own data is what keeps this inside a week rather than a
  runtime check that could fail. A forced round has no spread at all — every
  team lands on the exact confirmed date, since spreading it across nearby
  days would be moving a date that, by definition, cannot move. What
  genuinely can't be guaranteed, in either case, is a *legal* date existing
  at all once blackouts are subtracted; if every candidate is blacked out
  (including a forced round's own confirmed date), placement falls back to
  the round's own anchor regardless, and that gets a plain-English warning
  rather than a raised error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from ..model.schema import Competition, CupRound, Season


class CupSchedulingError(Exception):
    """A cup's declared rounds cannot be resolved to a valid schedule.

    Raised when a round's own window (or forced date) leaves no room to
    satisfy round N-before-round-N+1 ordering and the required rest gap
    between them — a data problem (the window needs widening, or the forced
    date needs revisiting), not something a solver retry can fix.
    """


@dataclass(frozen=True)
class CupRoundPlacement:
    """One round, resolved: every entered team's actual date for it."""

    round_id: str
    round_name: str
    dates: dict[str, date]
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

    gap = max(competition.min_rest_days, 1)
    spread = max(competition.match_window_days, 0)

    placements: list[CupRoundPlacement] = []
    warnings: list[str] = []
    previous_latest: date | None = None

    for round_ in competition.cup_rounds:
        anchor, lower_bound, upper_bound = _resolve_anchor(
            competition, round_, previous_latest, gap
        )
        team_dates, blacked_out = _spread_teams(
            competition.teams, anchor, spread, blackouts, lower_bound, upper_bound
        )
        placement = CupRoundPlacement(
            round_id=round_.id, round_name=round_.name, dates=team_dates, note=round_.note
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
            min_rest_days=competition.min_rest_days,
            rounds=placements,
        ),
        warnings,
    )


def _resolve_anchor(
    competition: Competition,
    round_: CupRound,
    previous_latest: date | None,
    gap: int,
) -> tuple[date, date | None, date | None]:
    """The round's anchor date, plus the bounds team placements must respect.

    A forced round's bounds are the confirmed date itself, both ends — a
    confirmed date is a confirmed date, not a suggestion `_spread_teams`
    should feel free to drift away from onto a nearby day (e.g. because the
    exact date happens to be a blackout). It still goes through the same
    minimum-rest gap check a windowed round's earliest placement does, since
    a confirmed date too close to the previous round's last one is a genuine
    conflict — one the data needs to fix, not something scheduling can paper
    over by moving a date that, by definition, cannot move.
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

    Candidate days run `anchor` to `anchor + spread` — forward only, never
    before it: `anchor` is either a confirmed date or a window's earliest
    legal start, and no team's placement should ever land earlier than that.
    A real cup round genuinely plays out over a few days, not all at once,
    which is what `spread` (`match_window_days`) is for — except for a
    forced round, where `lower_bound`/`upper_bound` are both set to `anchor`
    itself, collapsing the candidate range to that one exact day: a confirmed
    date isn't something to spread away from. Blackout dates are dropped from
    whatever range survives the bounds; if that leaves nothing at all, every
    team falls back onto the bare `anchor` and the second return value flags
    it, since a blacked-out anchor is better than failing outright over one
    clashing day.
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

    `season.global_blackouts` is the only blackout source consulted — cups
    don't book venues, so venue-specific blackouts don't apply to them.
    """
    blackouts = {b.date for b in season.global_blackouts}
    schedules: list[CupSchedule] = []
    warnings: list[str] = []
    for competition in competitions:
        schedule, round_warnings = schedule_cup(competition, blackouts)
        schedules.append(schedule)
        warnings.extend(round_warnings)
    return schedules, warnings


def resolved_cup_windows(schedules: list[CupSchedule]) -> dict[str, list[tuple[date, int]]]:
    """Per-team (resolved date, min rest days) pairs, for conflict-avoidance.

    The successor to the old `cup_rest_windows`: same shape and same
    consumers (`CupRoundConflict`, both solver backends), but reading each
    team's own resolved date from a `CupSchedule` instead of one date shared
    by the whole round.
    """
    windows: dict[str, list[tuple[date, int]]] = {}
    for schedule in schedules:
        for placement in schedule.rounds:
            for team_id, day in placement.dates.items():
                windows.setdefault(team_id, []).append((day, schedule.min_rest_days))
    return windows


def cup_conflict(cup_windows: dict[str, list[tuple[date, int]]], team_id: str, day: date) -> bool:
    """Whether `day` falls inside one of `team_id`'s cup-round rest windows.

    Shared by both solver backends: the greedy placer uses it to steer
    initial placement away from cup dates, and CP-SAT uses it to exclude
    conflicting dates from a fixture's candidate set outright, up front —
    the same treatment a blackout date already gets, and for the same
    reason. A fixture whose candidate window is a fixed handful of days
    around a *fixed* round anchor has no way to route around a conflict
    discovered only after the fact, unlike the greedy/annealing backend,
    which can still relocate the whole round.
    """
    return any(
        abs((day - cup_date).days) < minimum for cup_date, minimum in cup_windows.get(team_id, ())
    )


__all__ = [
    "CupRoundPlacement",
    "CupSchedule",
    "CupSchedulingError",
    "cup_conflict",
    "resolved_cup_windows",
    "schedule_cup",
    "schedule_cups",
]
