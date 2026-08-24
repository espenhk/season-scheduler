"""Constraint protocol, schedule indexing, and the score model.

Solvers never know what a constraint means, only how to make a schedule score
better, so adding a rule is writing one class and registering it.

Scores are signed — penalties negative, rewards positive — which lets the
report rank "biggest problems" and "biggest upsides" off one list. Hard beats
soft lexicographically, but during search a hard violation is a large finite
penalty rather than a rejection, so the annealer can pass through an infeasible
state on its way somewhere better.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

from ..model.loader import World
from ..model.schema import Match, Season

ConstraintKind = Literal["hard", "soft"]

# Cost of one hard violation in soft-score units: large enough that no pile of
# soft rewards can pay for it, finite so gradients still exist inside
# infeasible territory.
HARD_PENALTY = 10_000.0

# `Score.points` calibration. Any hard violation caps a schedule at
# INFEASIBLE_CEILING; a feasible schedule scoring zero soft lands at the
# midpoint above it. SOFT_SCALE (soft reward per match) is tuned so the
# shipped Eliteserien + Toppserien data, netting 6-8 points/match after a
# minute of search, lands in the low-to-mid 90s.
INFEASIBLE_CEILING = 20.0
SOFT_SCALE = 3.0

# Keeps a feasible schedule strictly above INFEASIBLE_CEILING: `_sigmoid`
# underflows to exactly 0.0 for extreme negative soft scores.
_MIN_SATURATION = 1e-9


def _sigmoid(x: float) -> float:
    """Numerically stable logistic — plain `1/(1+exp(-x))` overflows `math.exp`
    for inputs an extreme soft score can reach."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


@dataclass(frozen=True)
class Event:
    """One thing a constraint noticed: a violation to fix or a reward earned."""

    delta: float
    detail: str
    match_keys: tuple[str, ...] = ()
    # Drives the report's per-team fairness view
    # (`report/render.py::_fairness_rows`); empty for rules with no natural
    # single-team attribution, e.g. `preferred_weekday`.
    team_ids: tuple[str, ...] = ()


@dataclass
class ConstraintResult:
    constraint_id: str
    kind: ConstraintKind
    total: float
    count: int
    events: list[Event] = field(default_factory=list)


class ScheduleIndex:
    """Precomputed views over a schedule, built once per evaluation.

    Constraints query these rather than each re-scanning the match list, which
    is what makes an evaluation cheap enough to run tens of thousands of times
    during annealing.
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

        # What the consecutive-day rules need, built once here rather than
        # rebuilt inside each constraint.
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

    `detail` matters for speed: during search constraints return totals only,
    and only the final report asks for the per-event breakdown.
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
    num_matches: int = 0

    @property
    def sort_key(self) -> tuple[int, float]:
        """Lexicographic: feasibility first, then soft score. Lower is better."""
        return (self.hard_violations, -self.soft_total)

    @property
    def points(self) -> float:
        """Overall schedule quality on a fixed 0-100 scale.

        Soft reward is normalised per match so seasons of different sizes stay
        comparable. Any hard violation caps the result at `INFEASIBLE_CEILING`
        and decays towards 0, so a broken schedule can never outscore a
        working one.
        """
        if self.hard_violations:
            return INFEASIBLE_CEILING * math.exp(-0.25 * self.hard_violations)

        per_match = self.soft_total / self.num_matches if self.num_matches else 0.0
        saturation = max(_sigmoid(per_match / SOFT_SCALE), _MIN_SATURATION)
        return INFEASIBLE_CEILING + (100.0 - INFEASIBLE_CEILING) * saturation

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

        Hard sorts ahead of soft regardless of magnitude: a one-day
        `min_rest_days` shortfall matters more than a 25-point soft loss, but
        magnitude alone would bury it.
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
    return Score(
        hard_violations=hard_violations,
        soft_total=soft_total,
        results=results,
        num_matches=len(matches),
    )


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
