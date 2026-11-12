"""Commitments: the dates a team may have to keep clear, each with the chance
it is really played (#8).

Cup rounds and UEFA legs alike come out of an outcome tree
(`rounds/outcome_tree.py`) as `Commitment`s. A commitment is *hard* when its
team reaches it with at least the season's `contingency_policy.hard_threshold`
probability; the rest are contingent and only cost a soft penalty weighted by
that probability. Every consumer — the greedy placer, local search, CP-SAT,
the slot planner and the scoring rules — reads one `CommitmentSet`, so they
agree on which dates are hard.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Literal

CommitmentSource = Literal["cup", "uefa"]

# Floating-point slack when comparing a computed probability to the threshold.
_EPSILON = 1e-9


@dataclass(frozen=True)
class Commitment:
    """One date a team is committed to: a cup round, or one leg of a UEFA tie.

    `node_id` names the outcome-tree node the date belongs to (both legs of a
    tie share it). `exclusive_with` holds the other nodes of the same team's
    tree that can never be played in the same scenario, e.g. Lillestrøm's
    Europa League and Conference League league phases.
    """

    team_id: str
    date: date
    min_rest_days: int
    label: str
    source: CommitmentSource
    competition_id: str
    node_id: str
    reached_probability: float = 1.0
    exclusive_with: frozenset[str] = frozenset()
    hard: bool = True

    def clashes(self, day: date) -> bool:
        """Whether a match on `day` leaves fewer than `min_rest_days` between
        it and this commitment."""
        return abs((day - self.date).days) - 1 < self.min_rest_days


def is_hard(probability: float, hard_threshold: float) -> bool:
    return probability >= hard_threshold - _EPSILON


class CommitmentSet:
    """Every commitment of a season, indexed by team.

    Hard and contingent commitments are split once up front: the solvers ask
    "is this day blocked" in their innermost loops.
    """

    def __init__(self, commitments: Iterable[Commitment] = ()) -> None:
        self.commitments = tuple(commitments)
        hard: dict[str, list[Commitment]] = {}
        contingent: dict[str, list[Commitment]] = {}
        for commitment in self.commitments:
            bucket = hard if commitment.hard else contingent
            bucket.setdefault(commitment.team_id, []).append(commitment)
        self._hard = {team: tuple(items) for team, items in hard.items()}
        self._contingent = {team: tuple(items) for team, items in contingent.items()}

    def __len__(self) -> int:
        return len(self.commitments)

    def __bool__(self) -> bool:
        return bool(self.commitments)

    def __iter__(self):
        return iter(self.commitments)

    @property
    def team_ids(self) -> list[str]:
        return sorted({c.team_id for c in self.commitments})

    def hard_for(self, team_id: str) -> tuple[Commitment, ...]:
        return self._hard.get(team_id, ())

    def contingent_for(self, team_id: str) -> tuple[Commitment, ...]:
        return self._contingent.get(team_id, ())

    def for_team(self, team_id: str) -> list[Commitment]:
        return [c for c in self.commitments if c.team_id == team_id]

    def only(self, source: CommitmentSource) -> CommitmentSet:
        return CommitmentSet(c for c in self.commitments if c.source == source)

    def merged(self, other: CommitmentSet) -> CommitmentSet:
        return CommitmentSet([*self.commitments, *other.commitments])

    def blocked(self, team_id: str, day: date) -> bool:
        """Whether `day` is too close to one of the team's hard commitments."""
        return any(c.clashes(day) for c in self._hard.get(team_id, ()))

    def contingent_weight(self, team_id: str, day: date) -> float:
        """The summed probability of the team's contingent commitments `day`
        is too close to: how likely a match on `day` is to be disrupted."""
        return sum(
            c.reached_probability for c in self._contingent.get(team_id, ()) if c.clashes(day)
        )

    def blocked_days(self, team_id: str, days: Iterable[date]) -> int:
        return sum(1 for day in days if self.blocked(team_id, day))


__all__ = [
    "Commitment",
    "CommitmentSet",
    "CommitmentSource",
    "is_hard",
]
