"""Results already known, folded into the outcome trees (#13).

A real result settles part of a team's tree: `won` and `lost` say a round was
played and which edge was taken, `reached` only that the team is in it. The
branch not taken is cut, nodes nothing leads to any more go with it, and the
tree is re-finalized, so what was a contingent commitment becomes certain and
what can no longer happen stops producing contingencies.

Keys read `team -> competition -> round -> result`, e.g.
`brann_m: {conference_league_2026: {q3: lost}}`.
"""

from __future__ import annotations

from dataclasses import replace

from .outcome_tree import OutcomeNode, OutcomeTree

KnownOutcomes = dict[str, dict[str, dict[str, str]]]

RESULTS: tuple[str, ...] = ("won", "lost", "reached")


class KnownOutcomeError(ValueError):
    """An outcome names a round no team's path has, or a result other than
    won, lost or reached."""


def merge_outcomes(*sources: KnownOutcomes | None) -> KnownOutcomes:
    """Later sources win where they name the same round."""
    merged: KnownOutcomes = {}
    for source in sources:
        for team, competitions in (source or {}).items():
            for competition, rounds in competitions.items():
                merged.setdefault(team, {}).setdefault(competition, {}).update(rounds)
    return merged


def apply_known_outcomes(
    trees: dict[str, OutcomeTree], outcomes: KnownOutcomes
) -> dict[str, OutcomeTree]:
    """`trees` with the entries of `outcomes` that name their nodes applied, as
    new finalized trees. Entries naming nothing here are left for the caller's
    other trees; `check_outcomes` is what rejects an entry matching none."""
    results = _results(outcomes)
    applied = {}
    for team_id, tree in trees.items():
        own = {n: r for n, r in results.get(team_id, {}).items() if n in tree.nodes}
        applied[team_id] = _apply(tree, own) if own else tree
    return applied


def check_outcomes(tree_sets: list[dict[str, OutcomeTree]], outcomes: KnownOutcomes) -> None:
    """Raise `KnownOutcomeError` for an entry matching no node in any of `tree_sets`."""
    for team_id, by_node in _results(outcomes).items():
        for node_id in by_node:
            if not any(node_id in trees[team_id].nodes for trees in tree_sets if team_id in trees):
                raise KnownOutcomeError(
                    f"known outcome {node_id!r} matches no cup or UEFA round on {team_id}'s path"
                )


def _results(outcomes: KnownOutcomes) -> dict[str, dict[str, str]]:
    """Each team's `node_id -> result`, with results validated."""
    resolved: dict[str, dict[str, str]] = {}
    for team_id, competitions in outcomes.items():
        for competition_id, rounds in competitions.items():
            for round_id, result in rounds.items():
                node_id = f"{team_id}/{competition_id}/{round_id}"
                if result not in RESULTS:
                    raise KnownOutcomeError(
                        f"{node_id}: result {result!r} must be one of {', '.join(RESULTS)}"
                    )
                resolved.setdefault(team_id, {})[node_id] = result
    return resolved


def _apply(tree: OutcomeTree, results: dict[str, str]) -> OutcomeTree:
    nodes = dict(tree.nodes)
    for node_id, result in results.items():
        node = nodes[node_id]
        if result == "won":
            node = replace(node, known_reached=True, on_loss=None, win_probability=1.0)
        elif result == "lost":
            node = replace(node, known_reached=True, on_win=None, win_probability=0.0)
        else:
            node = replace(node, known_reached=True)
        nodes[node_id] = node

    live = _reachable(nodes, tree.roots)
    rebuilt = OutcomeTree(tree.team_id)
    for node_id, node in nodes.items():
        if node_id in live:
            rebuilt.add(node, root=node_id in tree.roots)
    return rebuilt.finalize()


def _reachable(nodes: dict[str, OutcomeNode], roots: list[str]) -> set[str]:
    live: set[str] = set()
    stack = list(roots)
    while stack:
        node_id = stack.pop()
        if node_id in live:
            continue
        live.add(node_id)
        node = nodes[node_id]
        stack.extend(t for t in (node.on_win, node.on_loss) if t is not None)
    return live


__all__ = [
    "KnownOutcomeError",
    "KnownOutcomes",
    "apply_known_outcomes",
    "check_outcomes",
    "merge_outcomes",
]
