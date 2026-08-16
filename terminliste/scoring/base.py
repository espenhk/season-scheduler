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

import math
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

# `Score.points` calibration. INFEASIBLE_CEILING is the hard cap a schedule
# with any hard violation can reach; a feasible schedule with a soft score of
# exactly zero lands on the midpoint of the range above it (currently 60, the
# dead centre between 20 and 100). SOFT_SCALE is the soft-reward-per-match
# that pulls a feasible schedule halfway from that midpoint to 100 — chosen
# so that the shipped Eliteserien + Toppserien data, which nets 6-8 soft
# points/match once searched for a minute, lands in the low-to-mid 90s.
INFEASIBLE_CEILING = 20.0
SOFT_SCALE = 3.0

# Floor for the feasible-side saturation factor. `_sigmoid` underflows to an
# exact 0.0 once its input is a few hundred past zero, which an extreme (if
# unrealistic) negative soft score can reach — left unclamped, that would let
# a feasible schedule's points hit INFEASIBLE_CEILING exactly rather than
# staying strictly above it.
_MIN_SATURATION = 1e-9


def _sigmoid(x: float) -> float:
    """Numerically stable logistic function — plain `1/(1+exp(-x))` overflows
    `math.exp` once `x` gets a few hundred past zero in either direction, which
    an extreme (if unrealistic) soft score can reach."""
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
    # The team(s) this event's delta is attributed to, when the rule is
    # naturally team-scoped — used to build the per-team/per-club fairness
    # view in the report (see `report/render.py::_fairness_rows`). Empty for
    # rules with no natural single-team attribution (e.g. `preferred_weekday`,
    # which is scored per competition).
    team_ids: tuple[str, ...] = ()


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
    num_matches: int = 0

    @property
    def sort_key(self) -> tuple[int, float]:
        """Lexicographic: feasibility first, then soft score. Lower is better."""
        return (self.hard_violations, -self.soft_total)

    @property
    def points(self) -> float:
        """Overall schedule quality on a fixed 0-100 scale.

        Comparable across seasons of any size: soft reward is normalised per
        match before being squashed into range, rather than letting a bigger
        league outscore a smaller one just by having more matches to earn
        points from.

        Any hard violation caps the result at `INFEASIBLE_CEILING`, decaying
        towards 0 as violations pile up — a broken schedule can never
        outscore a working one here, however good its soft score is. Every
        feasible schedule lands strictly above that ceiling, up to 100 for
        one that saturates the soft rules.
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
