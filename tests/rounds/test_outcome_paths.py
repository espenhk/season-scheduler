"""Enumerating the node sets a team can end up playing (#10)."""

from __future__ import annotations

from datetime import date

import pytest

from terminliste.rounds.outcome_tree import OutcomeNode, OutcomeTree


def _node(node_id, on_win=None, on_loss=None):
    return OutcomeNode(
        node_id=node_id,
        team_id="t",
        competition_id="c",
        round_id=node_id,
        source="uefa",
        min_rest_days=2,
        legs=((node_id, date(2026, 8, 1)),),
        on_win=on_win,
        on_loss=on_loss,
    )


def test_paths_cover_every_branch_and_sum_to_one():
    tree = OutcomeTree("t")
    tree.add(_node("a", on_win="b", on_loss="c"), root=True)
    tree.add(_node("b"))
    tree.add(_node("c"))
    tree.finalize()

    paths = dict(tree.paths())

    assert paths == {
        frozenset({"a", "b"}): pytest.approx(0.5),
        frozenset({"a", "c"}): pytest.approx(0.5),
    }


def test_separate_roots_are_played_independently():
    tree = OutcomeTree("t")
    tree.add(_node("a", on_win="b"), root=True)
    tree.add(_node("b"))
    tree.add(_node("x"), root=True)
    tree.finalize()

    paths = dict(tree.paths())

    assert all("x" in nodes for nodes in paths)
    assert sum(paths.values()) == pytest.approx(1.0)
    assert len(paths) == 2
