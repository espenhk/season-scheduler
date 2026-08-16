"""Constraint protocol, schedule indexing, and the score model.

Every rule — hard or soft — is an object implementing `Constraint`. The solvers
never know what a constraint means; they only know how to make a schedule score
better. Adding a rule is writing one class and registering it, and both solver
backends pick it up for free.

Scores are signed: penalties are negative, rewards positive. That is what lets
the report rank "biggest problems" and "biggest upsides" off the same list,
sorted from either end.

Hard and soft are compared lexicographically. A schedule with zero hard
violations beats one with any, no matter how good its soft score — but during
search hard violations are carried as a large finite penalty rather than an
outright rejection, so the annealer can pass through an infeasible state on its
way somewhere better.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

from ..model.loader import World
from ..model.schema import Match, Season

ConstraintKind = Literal["hard", "soft"]

# Cost of a single hard violation, in soft-score units. Large enough that no
# accumulation of soft rewards can pay for one, small enough to stay finite so
# gradients still exist inside infeasible territory.
HARD_PENALTY = 10_000.0


@dataclass(frozen=True)
class Event:
    """One thing a constraint noticed: a violation to fix or a reward earned."""

    delta: float
    detail: str
    match_keys: tuple[str, ...] = ()


@dataclass
class ConstraintResult:
    constraint_id: str
    kind: ConstraintKind
    total: float
    count: int
    events: list[Event] = field(default_factory=list)


class ScheduleIndex:
    """Precomputed views over a schedule.

    Built once per evaluation. Constraints ask cheap questions of the indexes
    rather than each re-scanning the match list, which is what keeps a full
    evaluation fast enough to run tens of thousands of times during annealing.
    """

    __slots__ = (
        "matches",
        "by_team",
        "by_venue_date",
        "by_date",
        "by_competition",
        "home_dates_by_team",
        "away_dates_by_team",
        "away_venue_by_team_date",
    )

    def __init__(self, matches: list[Match]) -> None:
        self.matches = matches
        self.by_team: dict[str, list[Match]] = {}
        self.by_venue_date: dict[tuple[str, date], list[Match]] = {}
        self.by_date: dict[date, list[Match]] = {}
        self.by_competition: dict[str, list[Match]] = {}

        for match in matches:
            self.by_team.setdefault(match.home_team, []).append(match)
            self.by_team.setdefault(match.away_team, []).append(match)
            self.by_venue_date.setdefault((match.venue, match.date), []).append(match)
            self.by_date.setdefault(match.date, []).append(match)
            self.by_competition.setdefault(match.competition_id, []).append(match)

        for team_matches in self.by_team.values():
            team_matches.sort(key=lambda m: m.date)

        # Home/away date sets per team, and where each away day is played —
        # everything the consecutive-day rules need, built in the one pass they
        # already cost rather than rebuilt inside each constraint.
        self.home_dates_by_team: dict[str, set[date]] = {}
        self.away_dates_by_team: dict[str, set[date]] = {}
        self.away_venue_by_team_date: dict[tuple[str, date], str] = {}
        for match in matches:
            self.home_dates_by_team.setdefault(match.home_team, set()).add(match.date)
            self.away_dates_by_team.setdefault(match.away_team, set()).add(match.date)
            self.away_venue_by_team_date[(match.away_team, match.date)] = match.venue

    def team_matches(self, team_id: str) -> list[Match]:
        return self.by_team.get(team_id, [])


@dataclass
class EvalContext:
    """Everything a constraint needs beyond the schedule itself.

    `detail` is the one knob that matters for speed: during search constraints
    return totals only, and only the final report asks for the per-event
    breakdown that names specific matches.
    """

    world: World
    season: Season
    travel: object  # TravelModel; typed loosely to avoid a circular import
    detail: bool = False
    away_pairing_max_hours: float = 8.0


class Constraint(Protocol):
    id: str
    kind: ConstraintKind
    weight: float

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult: ...


@dataclass
class Score:
    """The verdict on one schedule."""

    hard_violations: int
    soft_total: float
    results: list[ConstraintResult] = field(default_factory=list)

    @property
    def sort_key(self) -> tuple[int, float]:
        """Lexicographic: feasibility first, then soft score. Lower is better."""
        return (self.hard_violations, -self.soft_total)

    @property
    def search_cost(self) -> float:
        """Single number for the annealer — hard violations folded in."""
        return self.hard_violations * HARD_PENALTY - self.soft_total

    @property
    def feasible(self) -> bool:
        return self.hard_violations == 0

    def result(self, constraint_id: str) -> ConstraintResult | None:
        return next((r for r in self.results if r.constraint_id == constraint_id), None)

    def soft_results(self) -> list[ConstraintResult]:
        return [r for r in self.results if r.kind == "soft"]

    def hard_results(self) -> list[ConstraintResult]:
        return [r for r in self.results if r.kind == "hard"]

    def biggest_problems(self, limit: int = 5) -> list[ConstraintResult]:
        """Constraints costing the most points, worst first.

        Hard violations always sort ahead of soft ones here, regardless of
        their point value. A broken hard rule (e.g. one day's shortfall on
        `min_rest_days`, weight 1) matters more than losing a 25-point soft
        reward — magnitude alone would bury it under the soft rows.
        """
        negative = [r for r in self.results if r.total < 0]
        return sorted(negative, key=lambda r: (r.kind != "hard", r.total))[:limit]

    def biggest_upsides(self, limit: int = 5) -> list[ConstraintResult]:
        """Constraints earning the most points, best first."""
        positive = [r for r in self.results if r.total > 0]
        return sorted(positive, key=lambda r: r.total, reverse=True)[:limit]


def evaluate(
    matches: list[Match],
    constraints: list[Constraint],
    ctx: EvalContext,
) -> Score:
    """Run every constraint over a schedule and total up the result."""
    index = ScheduleIndex(matches)
    results = [c.evaluate(index, ctx) for c in constraints]
    hard_violations = sum(r.count for r in results if r.kind == "hard")
    soft_total = sum(r.total for r in results if r.kind == "soft")
    return Score(hard_violations=hard_violations, soft_total=soft_total, results=results)


def evaluate_cost(
    matches: list[Match],
    constraints: list[Constraint],
    ctx: EvalContext,
) -> float:
    """Search-facing shortcut: one number, no per-constraint bookkeeping."""
    index = ScheduleIndex(matches)
    cost = 0.0
    for constraint in constraints:
        result = constraint.evaluate(index, ctx)
        if result.kind == "hard":
            cost += result.count * HARD_PENALTY
        else:
            cost -= result.total
    return cost
