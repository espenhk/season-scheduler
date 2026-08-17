"""Solver protocol and the shared result type.

Both backends — local search and CP-SAT — consume the same constraint objects
and return the same `SolverResult`, so the report, the CLI and every test are
backend-agnostic. Choosing a solver changes how the schedule is found, never
what a schedule is or how it is judged.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..model.loader import World
from ..model.schema import Competition, Match, Season
from ..rounds.cup_schedule import CupSchedule
from ..scoring.base import Constraint, EvalContext, Score

# Two candidates must differ on at least this share of their match dates to
# count as genuinely different schedules. Three near-identical options are not
# a choice.
DIVERSITY_THRESHOLD = 0.15

# `SearchStats.feasible_rate` thresholds for the qualitative "how hard was
# this" read-out (issue #34). Calibrated loosely, not measured: below 5%
# feasible draws is a search that had to fight for every viable date: above
# 50% is one where a workable arrangement was the common case.
_DIFFICULTY_HARD = 0.05
_DIFFICULTY_EASY = 0.50


@dataclass
class SearchStats:
    """How hard the search had to work — issue #34.

    One "scenario" is one candidate schedule state the search actually
    evaluated against the full constraint set: a proposed move in local
    search, a solved pass in CP-SAT. `hard_violation_counts` tallies which
    hard rule was broken in a scenario that wasn't viable, so a report can
    say not just how many dead ends there were but *why* — e.g. "min_rest_days
    was the wall this search kept hitting."

    Deliberately thin: everything here is either a counter already being
    incremented as a side effect of work the search does anyway (an
    iteration, a constraint check), or a rate derived from those counters —
    nothing that requires a second, otherwise-unneeded evaluation pass.
    """

    investigated: int = 0
    feasible: int = 0
    hard_violation_scenarios: int = 0
    hard_violation_counts: Counter[str] = field(default_factory=Counter)

    def __iadd__(self, other: "SearchStats") -> "SearchStats":
        self.investigated += other.investigated
        self.feasible += other.feasible
        self.hard_violation_scenarios += other.hard_violation_scenarios
        self.hard_violation_counts.update(other.hard_violation_counts)
        return self

    def copy(self) -> "SearchStats":
        """An independent snapshot — needed because the search loop mutates
        one `SearchStats` in place; a live `ProgressUpdate` callback needs its
        own frozen-in-time copy rather than a reference that keeps changing
        underneath it."""
        return SearchStats(
            investigated=self.investigated,
            feasible=self.feasible,
            hard_violation_scenarios=self.hard_violation_scenarios,
            hard_violation_counts=Counter(self.hard_violation_counts),
        )

    @property
    def feasible_rate(self) -> float:
        return self.feasible / self.investigated if self.investigated else 0.0

    @property
    def difficulty(self) -> str:
        """A one-word read on how hard the search had to work.

        `"unknown"` when nothing was tracked (e.g. `score`'s external-schedule
        path, which never runs a search at all).
        """
        if not self.investigated:
            return "unknown"
        if self.feasible_rate < _DIFFICULTY_HARD:
            return "hard"
        if self.feasible_rate < _DIFFICULTY_EASY:
            return "moderate"
        return "easy"


@dataclass
class ProgressUpdate:
    """A live snapshot of `SearchStats` mid-run, for `SolveRequest.progress`."""

    restart: int
    total_restarts: int
    iterations: int
    total_iterations: int
    stats: SearchStats


@dataclass
class Candidate:
    """One complete schedule and its score."""

    matches: list[Match]
    score: Score
    label: str = ""
    seed: int = 0

    def assignment(self) -> dict[str, object]:
        """Fixture key -> date, the fingerprint used for diversity checks."""
        return {m.key: m.date for m in self.matches}


@dataclass
class SolverResult:
    candidates: list[Candidate] = field(default_factory=list)
    solver: str = ""
    iterations: int = 0
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)
    search_stats: SearchStats = field(default_factory=SearchStats)

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None


@dataclass
class SolveRequest:
    """Everything a solver needs. Bundled so backends share one signature."""

    world: World
    season: Season
    competitions: list[Competition]
    constraints: list[Constraint]
    ctx: EvalContext
    seed: int = 42
    top_n: int = 3
    time_budget_s: float = 30.0
    # Real-world-dated cups sharing teams with `competitions`, already
    # resolved to a date per team (see `rounds/cup_schedule.py`) — not
    # scheduled themselves but used to steer league placement away from them.
    cup_schedules: list[CupSchedule] = field(default_factory=list)
    # Called periodically (throttled by iteration count, never by wall-clock
    # polling) with a running `SearchStats` snapshot, so a caller — the CLI —
    # can print live progress during a long solve. `None` means don't bother.
    progress: Callable[[ProgressUpdate], None] | None = None


class Scheduler(Protocol):
    name: str

    def solve(self, request: SolveRequest) -> SolverResult: ...


def divergence(a: Candidate, b: Candidate) -> float:
    """Share of fixtures the two schedules place on different dates."""
    left, right = a.assignment(), b.assignment()
    keys = set(left) | set(right)
    if not keys:
        return 0.0
    differing = sum(1 for k in keys if left.get(k) != right.get(k))
    return differing / len(keys)


def select_diverse(
    candidates: list[Candidate], top_n: int, threshold: float = DIVERSITY_THRESHOLD
) -> list[Candidate]:
    """Best candidates that are also meaningfully different from each other.

    Greedy: take the best, then keep walking down the ranking accepting anything
    far enough from everything already chosen. If too few clear the bar, the
    remaining slots are filled with the next best regardless — three options
    with a caveat beats one option.
    """
    ranked = sorted(candidates, key=lambda c: c.score.sort_key)
    chosen: list[Candidate] = []
    for candidate in ranked:
        if all(divergence(candidate, other) >= threshold for other in chosen):
            chosen.append(candidate)
        if len(chosen) == top_n:
            return chosen

    for candidate in ranked:
        if len(chosen) == top_n:
            break
        if candidate not in chosen:
            chosen.append(candidate)
    return chosen[:top_n]
