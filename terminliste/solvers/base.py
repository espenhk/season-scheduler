"""Solver protocol and the shared result type.

Both backends — local search and CP-SAT — consume the same constraint objects
and return the same `SolverResult`, so the report, the CLI and every test are
backend-agnostic. Choosing a solver changes how the schedule is found, never
what a schedule is or how it is judged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..model.loader import World
from ..model.schema import Competition, Match, Season
from ..scoring.base import Constraint, EvalContext, Score

# Two candidates must differ on at least this share of their match dates to
# count as genuinely different schedules. Three near-identical options are not
# a choice.
DIVERSITY_THRESHOLD = 0.15


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
