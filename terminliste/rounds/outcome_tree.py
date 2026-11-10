"""A team's possible paths through cups and UEFA competitions (#8).

Nodes are rounds a team may play; each has a win edge and a loss edge, either
of which leads to another node or ends the team's run. The tree is a DAG, not
a strict tree: a Europa League play-off loss and a Champions League Q3 loss
can both land on the same league phase.

Two things come out of it: each node's `reached_probability`, and which nodes
can never be played in the same scenario (`exclusive_with`). Both feed
`Commitment` (`rounds/commitments.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .commitments import Commitment, CommitmentSource, is_hard


class OutcomeTreeError(Exception):
    """The tree has a cycle or an edge to a node that does not exist — a data
    problem, not something a solver retry can fix."""


@dataclass(frozen=True)
class OutcomeNode:
    """One round a team may play, with the dates it blocks.

    `legs` are the (label, date) pairs: two for a two-legged tie, one for a
    cup round. `known_reached` marks a round the team is certain to be in.
    """

    node_id: str
    team_id: str
    competition_id: str
    round_id: str
    source: CommitmentSource
    min_rest_days: int
    legs: tuple[tuple[str, date], ...]
    on_win: str | None = None
    on_loss: str | None = None
    win_probability: float = 0.5
    known_reached: bool = False


@dataclass
class OutcomeTree:
    team_id: str
    nodes: dict[str, OutcomeNode] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)
    probabilities: dict[str, float] = field(default_factory=dict)
    _successors: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)
    _roots_of: dict[str, frozenset[str]] = field(default_factory=dict, repr=False)
    _win_chance: dict[str, float] = field(default_factory=dict, repr=False)

    def add(self, node: OutcomeNode, *, root: bool = False) -> None:
        self.nodes[node.node_id] = node
        if root and node.node_id not in self.roots:
            self.roots.append(node.node_id)

    def finalize(self) -> OutcomeTree:
        """Compute reachability, then every node's reached probability."""
        for node in self.nodes.values():
            for target in (node.on_win, node.on_loss):
                if target is not None and target not in self.nodes:
                    raise OutcomeTreeError(
                        f"{self.team_id}: {node.node_id} leads to unknown node {target!r}"
                    )
        order = self._topological_order()
        self._successors = {}
        for node_id in reversed(order):
            node = self.nodes[node_id]
            reach: set[str] = set()
            for target in (node.on_win, node.on_loss):
                if target is not None:
                    reach.add(target)
                    reach |= self._successors[target]
            self._successors[node_id] = frozenset(reach)

        self._roots_of = {
            node_id: frozenset(
                root for root in self.roots if root == node_id or node_id in self._successors[root]
            )
            for node_id in self.nodes
        }

        # A node with a certain descendant was certainly reached on the way.
        known = {
            node_id
            for node_id, node in self.nodes.items()
            if node.known_reached or any(self.nodes[s].known_reached for s in self._successors[node_id])
        }
        probabilities = {node_id: 0.0 for node_id in self.nodes}
        self._win_chance = {}
        for root in self.roots:
            probabilities[root] = 1.0
        for node_id in order:
            node = self.nodes[node_id]
            if node_id in known:
                probabilities[node_id] = 1.0
            mass = probabilities[node_id]
            win_p = node.win_probability
            # A branch already known to have been taken rules out the other.
            if node.on_win in known and node.on_loss not in known:
                win_p = 1.0
            elif node.on_loss in known and node.on_win not in known:
                win_p = 0.0
            self._win_chance[node_id] = win_p
            if node.on_win is not None:
                probabilities[node.on_win] += mass * win_p
            if node.on_loss is not None:
                probabilities[node.on_loss] += mass * (1.0 - win_p)
        self.probabilities = {k: min(1.0, v) for k, v in probabilities.items()}
        return self

    def _topological_order(self) -> list[str]:
        state: dict[str, int] = {}
        order: list[str] = []

        def visit(node_id: str) -> None:
            if state.get(node_id) == 2:
                return
            if state.get(node_id) == 1:
                raise OutcomeTreeError(f"{self.team_id}: cycle through {node_id!r}")
            state[node_id] = 1
            node = self.nodes[node_id]
            for target in (node.on_win, node.on_loss):
                if target is not None:
                    visit(target)
            state[node_id] = 2
            order.append(node_id)

        for node_id in self.nodes:
            visit(node_id)
        order.reverse()
        return order

    def win_chance(self, node_id: str) -> float:
        """The chance of winning `node_id` once played, after any branch
        already known to have been taken is accounted for."""
        return self._win_chance[node_id]

    def paths(self) -> list[tuple[frozenset[str], float]]:
        """Every distinct set of nodes the team can end up playing, with its
        probability. Separate roots (e.g. a cup and a UEFA entry) are played
        independently; the probabilities sum to 1."""
        per_root = [self._walk(root) for root in self.roots]
        combined: dict[frozenset[str], float] = {frozenset(): 1.0}
        for options in per_root:
            merged: dict[frozenset[str], float] = {}
            for played, p in combined.items():
                for extra, q in options:
                    key = played | extra
                    merged[key] = merged.get(key, 0.0) + p * q
            combined = merged
        return sorted(combined.items(), key=lambda item: sorted(item[0]))

    def _walk(self, node_id: str) -> list[tuple[frozenset[str], float]]:
        node = self.nodes[node_id]
        win_p = self._win_chance[node_id]
        outcomes: dict[frozenset[str], float] = {}
        for target, p in ((node.on_win, win_p), (node.on_loss, 1.0 - win_p)):
            if p <= 0.0:
                continue
            branches = [(frozenset(), 1.0)] if target is None else self._walk(target)
            for played, q in branches:
                key = played | {node_id}
                outcomes[key] = outcomes.get(key, 0.0) + p * q
        return list(outcomes.items())

    def co_occur(self, a: str, b: str) -> bool:
        """Whether some scenario plays both nodes: one must lead to the other,
        or they sit under separate entries (e.g. a cup and a UEFA path)."""
        if a == b or b in self._successors[a] or a in self._successors[b]:
            return True
        return not (self._roots_of[a] & self._roots_of[b])

    def exclusive_with(self, node_id: str) -> frozenset[str]:
        return frozenset(other for other in self.nodes if not self.co_occur(node_id, other))

    def commitments(self, hard_threshold: float) -> list[Commitment]:
        result: list[Commitment] = []
        for node_id, node in self.nodes.items():
            probability = self.probabilities[node_id]
            exclusive = self.exclusive_with(node_id)
            for label, day in node.legs:
                result.append(
                    Commitment(
                        team_id=self.team_id,
                        date=day,
                        min_rest_days=node.min_rest_days,
                        label=label,
                        source=node.source,
                        competition_id=node.competition_id,
                        node_id=node_id,
                        reached_probability=probability,
                        exclusive_with=exclusive,
                        hard=is_hard(probability, hard_threshold),
                    )
                )
        return result


__all__ = ["OutcomeNode", "OutcomeTree", "OutcomeTreeError"]
