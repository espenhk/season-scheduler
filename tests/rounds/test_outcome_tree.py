"""Outcome-tree structure, probabilities and the hard/contingent split (#8)."""

from __future__ import annotations

from datetime import date, timedelta

import factories as f
import pytest

from terminliste.rounds.cup_schedule import build_cup_trees, schedule_cups
from terminliste.rounds.outcome_tree import OutcomeNode, OutcomeTree, OutcomeTreeError

DAY = date(2026, 8, 1)


def _node(node_id, *, on_win=None, on_loss=None, offset=0, known=False, source="uefa", win_p=0.5):
    return OutcomeNode(
        node_id=node_id, team_id="t", competition_id="c", round_id=node_id, source=source,
        min_rest_days=2, legs=((node_id, DAY + timedelta(days=offset)),), on_win=on_win,
        on_loss=on_loss, known_reached=known, win_probability=win_p,
    )


def _fork(win_p: float = 0.5) -> OutcomeTree:
    tree = OutcomeTree("t")
    tree.add(_node("entry", on_win="won", on_loss="lost", win_p=win_p), root=True)
    tree.add(_node("won", offset=7))
    tree.add(_node("lost", offset=8))
    return tree.finalize()


def test_a_cycle_raises():
    tree = OutcomeTree("t")
    tree.add(_node("a", on_win="b"), root=True)
    tree.add(_node("b", on_loss="a"))
    with pytest.raises(OutcomeTreeError, match="cycle"):
        tree.finalize()


def test_an_edge_to_a_missing_node_raises():
    tree = OutcomeTree("t")
    tree.add(_node("a", on_win="ghost"), root=True)
    with pytest.raises(OutcomeTreeError, match="unknown node"):
        tree.finalize()


def test_a_team_in_two_roots_plays_both_and_each_keeps_its_own_branches_exclusive():
    tree = OutcomeTree("t")
    tree.add(_node("cup1", on_win="cup2", source="cup"), root=True)
    tree.add(_node("cup2", offset=14, source="cup"))
    tree.add(_node("entry", offset=3, on_win="won", on_loss="lost"), root=True)
    tree.add(_node("won", offset=10))
    tree.add(_node("lost", offset=11))
    tree.finalize()

    assert tree.probabilities["cup1"] == tree.probabilities["entry"] == 1.0
    assert tree.probabilities["cup2"] == 0.5
    # Separate roots are independent, so cup and UEFA nodes co-occur.
    assert not tree.co_occur("won", "lost")
    assert tree.co_occur("won", "cup2") and tree.co_occur("lost", "cup1")
    assert tree.exclusive_with("won") == frozenset({"lost"})
    assert tree.exclusive_with("cup2") == frozenset()


def test_equally_likely_exclusive_nodes_keep_the_lower_id_hard_and_warn():
    tree = _fork()
    commitments = {c.node_id: c for c in tree.commitments(hard_threshold=0.5)}
    assert commitments["entry"].hard
    assert commitments["lost"].hard and not commitments["won"].hard
    assert len(tree.warnings) == 1
    assert "won" in tree.warnings[0] and "lost" in tree.warnings[0]


def test_the_likelier_exclusive_node_stays_hard_whatever_its_id():
    tree = _fork(win_p=0.6)
    hard = {c.node_id for c in tree.commitments(hard_threshold=0.4) if c.hard}
    assert hard == {"entry", "won"}


def test_no_demotion_or_warning_at_the_default_threshold():
    tree = _fork()
    assert {c.node_id for c in tree.commitments(1.0) if c.hard} == {"entry"}
    assert tree.warnings == []


def test_reached_teams_on_a_cup_round_make_the_earlier_rounds_certain():
    cup = f.competition(
        "cup", ["a"], format="cup", advance_probability=0.5,
        cup_rounds=[
            f.cup_round("r1", forced_date=date(2026, 4, 1)),
            f.cup_round("r2", forced_date=date(2026, 4, 15)),
            f.cup_round("r3", forced_date=date(2026, 5, 1), reached_teams=["a"]),
            f.cup_round("r4", forced_date=date(2026, 5, 15)),
        ],
    )
    schedules, _ = schedule_cups([cup], f.season())
    tree = build_cup_trees([cup], schedules)["a"]
    assert [tree.probabilities[f"a/cup/{r}"] for r in ("r1", "r2", "r3", "r4")] == [1.0, 1.0, 1.0, 0.5]
    hard = {c.node_id for c in tree.commitments(1.0) if c.hard}
    assert hard == {"a/cup/r1", "a/cup/r2", "a/cup/r3"}


def test_paths_enumerate_every_run_and_sum_to_one():
    paths = dict(_fork(0.25).paths())

    assert paths == {frozenset({"entry", "won"}): 0.25, frozenset({"entry", "lost"}): 0.75}


def test_paths_never_play_exclusive_branches_together_and_keep_roots_independent():
    tree = OutcomeTree("t")
    tree.add(_node("cup1", on_win="cup2", source="cup"), root=True)
    tree.add(_node("cup2", offset=14, source="cup"))
    tree.add(_node("entry", offset=3, on_win="won", on_loss="lost"), root=True)
    tree.add(_node("won", offset=10))
    tree.add(_node("lost", offset=11))
    tree.finalize()

    paths = tree.paths()

    assert sum(p for _, p in paths) == pytest.approx(1.0)
    assert len(paths) == 4
    assert all(not {"won", "lost"} <= played for played, _ in paths)
    assert dict(paths)[frozenset({"cup1", "cup2", "entry", "won"})] == pytest.approx(0.25)


def test_paths_honour_a_branch_already_known_to_have_been_taken():
    tree = OutcomeTree("t")
    tree.add(_node("entry", on_win="won", on_loss="lost"), root=True)
    tree.add(_node("won", offset=7, known=True))
    tree.add(_node("lost", offset=8))
    tree.finalize()

    assert tree.paths() == [(frozenset({"entry", "won"}), 1.0)]
