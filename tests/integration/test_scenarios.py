"""The scenario evaluator on the real 2026 data: seeded, deterministic, and
consistent with the plan it re-scores (#10)."""

from __future__ import annotations

import pytest

from terminliste.contingency import ContingencyPlan, ContingencyPlanner
from terminliste.frame import build_season_frame
from terminliste.scenarios import ScenarioEvaluator, evaluate_robustness
from terminliste.scoring.base import EvalContext, evaluate
from terminliste.scoring.registry import build_constraints
from terminliste.solvers.greedy import build_initial_schedule


@pytest.fixture(scope="module")
def setup(world, season, competitions, travel):
    frame = build_season_frame(world, season)[0]
    matches, _, _ = build_initial_schedule(
        world,
        season,
        competitions,
        seed=42,
        cup_schedules=frame.cup_schedules,
        commitments=frame.commitments,
    )
    ctx = EvalContext(world=world, season=season, travel=travel)
    base = evaluate(matches, build_constraints(world, season, competitions, frame.commitments), ctx)
    plan = ContingencyPlanner(world, season, competitions, frame.commitments).plan(matches)
    return frame, matches, ctx, base, plan


def _run(world, season, competitions, setup, count, seed):
    frame, matches, ctx, base, plan = setup
    return evaluate_robustness(world, season, competitions, frame, matches, plan, base, ctx, count, seed)


def test_same_seed_gives_the_same_report(world, season, competitions, setup):
    first = _run(world, season, competitions, setup, 60, seed=3)
    second = _run(world, season, competitions, setup, 60, seed=3)

    assert first.to_dict() == second.to_dict()
    assert first.outcomes == 60 and not first.exhaustive


def test_report_statistics_are_consistent(world, season, competitions, setup):
    report = _run(world, season, competitions, setup, 60, seed=3)

    assert 0.0 <= report.feasible_share <= 1.0
    assert report.expected_moves <= report.max_moves
    assert report.min_points <= report.mean_points <= 100
    assert report.worst_case is not None
    assert report.worst_case.moves >= report.expected_moves


def test_played_commitments_are_hard_in_a_scenario(world, season, competitions, setup):
    frame, matches, ctx, _, plan = setup
    evaluator = ScenarioEvaluator(
        world,
        season,
        competitions,
        [*frame.uefa_trees.values(), *frame.cup_trees.values()],
        frame.commitments,
        ctx,
    )
    # Without any fallbacks, playing every trigger must break a hard rule.
    score, moves = evaluator.score(matches, ContingencyPlan(), evaluator.worst_case_nodes(plan))

    assert moves == 0
    assert score.hard_violations > 0


def test_few_outcomes_are_enumerated_exactly_with_probability_weights():
    from datetime import date

    from terminliste.rounds.commitments import CommitmentSet
    from terminliste.rounds.outcome_tree import OutcomeNode, OutcomeTree

    tree = OutcomeTree("t")
    for node_id, win, loss in (("a", "b", "c"), ("b", None, None), ("c", None, None)):
        tree.add(
            OutcomeNode(node_id, "t", "c", node_id, "uefa", 2, ((node_id, date(2026, 8, 1)),), win, loss),
            root=node_id == "a",
        )
    tree.finalize()
    evaluator = ScenarioEvaluator(None, None, [], [tree], CommitmentSet(), EvalContext(None, None, None))

    exhaustive, draws = evaluator._draws(10, seed=1)

    assert exhaustive
    assert sorted(w for _, w in draws) == [0.5, 0.5]


def test_report_renders_robustness_and_moves(world, season, competitions, setup, tmp_path):
    from terminliste.report.render import render_report
    from terminliste.solvers.base import Candidate, SolverResult

    frame, matches, ctx, base, plan = setup
    candidate = Candidate(matches=matches, score=base, label="Option 1", contingency_plan=plan)
    candidate.robustness = _run(world, season, competitions, setup, 30, seed=1)
    out = render_report(
        world, season, SolverResult(candidates=[candidate], solver="test"), tmp_path / "r.html",
        commitments=frame.commitments,
    )

    page = out.read_text(encoding="utf-8")

    assert "Robustness" in page and "outcomes feasible" in page
    if plan.fallbacks:
        assert "cmove" in page and "→" in page and "p=" in page
