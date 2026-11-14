"""The scenario evaluator: a base plan plus the moves its triggers fire, re-scored (#10)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import factories as f
import pytest

from terminliste.contingency import ContingencyPlan, ContingencyPlanner, Fallback
from terminliste.frame import build_season_frame
from terminliste.model.travel import ApiTravelModel
from terminliste.report.render import write_json
from terminliste.rounds.commitments import CommitmentSet
from terminliste.rounds.outcome_tree import OutcomeNode, OutcomeTree
from terminliste.scenarios import ScenarioEvaluator, evaluate_robustness
from terminliste.scoring.base import EvalContext, ScheduleIndex, evaluate
from terminliste.scoring.registry import build_constraints
from terminliste.solvers.base import SolverResult

THU = date(2026, 8, 20)
SUN = date(2026, 8, 23)
LATER_SUN = date(2026, 9, 6)


class _Travel:
    def hours(self, a, b):
        return 1.0


def _node(node_id, *, on_win=None, offset, win_p=0.5, rest=3):
    return OutcomeNode(
        node_id=node_id, team_id="t1", competition_id="cup", round_id=node_id, source="cup",
        min_rest_days=rest, legs=((node_id, THU + timedelta(days=offset)),), on_win=on_win,
        win_probability=win_p,
    )


@pytest.fixture(scope="module")
def tiny():
    """t1 plays the cup on Thu 20 Aug (p=0.5) and, if it wins, again on Thu 3 Sep
    (p=0.25), next to two of its league matches."""
    teams = [f.team(f"t{i}", f"c{i}", f"v{i}") for i in range(1, 5)]
    clubs = [f.club(f"c{i}", [t]) for i, t in enumerate(teams, start=1)]
    league = f.competition("league", [t.id for t in teams], min_rest_days=3)
    world = f.world(clubs, [f.venue(f"v{i}") for i in range(1, 5)], [league])
    season = f.season(competitions=["league"])

    tree = OutcomeTree("t1")
    tree.add(_node("t1/cup/r0", on_win="t1/cup/r1", offset=-30), root=True)
    tree.add(_node("t1/cup/r1", on_win="t1/cup/r2", offset=0))
    tree.add(_node("t1/cup/r2", offset=14))
    tree.finalize()
    commitments = CommitmentSet(tree.commitments(hard_threshold=0.9))
    matches = [
        f.match("league", "t1", "t2", SUN, "v1"),
        f.match("league", "t1", "t3", LATER_SUN, "v1", round_index=1),
        f.match("league", "t3", "t4", SUN, "v3"),
    ]
    ctx = EvalContext(world=world, season=season, travel=_Travel())
    constraints = build_constraints(world, season, [league], commitments)
    plan = ContingencyPlanner(world, season, [league], commitments).analyse(ScheduleIndex(matches)).plan
    evaluator = ScenarioEvaluator(world, season, [league], [tree], commitments, ctx)
    return evaluator, matches, plan, evaluate(matches, constraints, ctx)


def test_the_tiny_plan_covers_both_exposed_matches(tiny):
    _, matches, plan, _ = tiny

    assert set(plan.fallbacks) == {matches[0].key, matches[1].key}
    assert plan.uncovered == []


def test_few_outcomes_are_enumerated_with_their_probabilities(tiny):
    evaluator, matches, plan, base = tiny

    report = evaluator.evaluate(matches, plan, base, count=50)

    assert report.exhaustive and report.outcomes == evaluator.outcome_count() == 3
    assert report.feasible_share == pytest.approx(1.0)
    # Not through r1 (p=0.5): no move; r1 only (0.25): one; r1 and r2 (0.25): two.
    assert report.expected_moves == pytest.approx(0.25 * 1 + 0.25 * 2)
    assert report.max_moves == 2
    assert report.worst_case.moves == 2 and report.worst_case.feasible


def test_a_broken_fallback_shows_up_as_a_failing_rule_and_a_failed_worst_case(tiny):
    evaluator, matches, plan, base = tiny
    first = matches[0]
    on_the_cup_date = Fallback(THU, first.venue, None, ("t1/cup/r1",), "same_slot")
    broken = ContingencyPlan(fallbacks={**plan.fallbacks, first.key: on_the_cup_date})

    report = evaluator.evaluate(matches, broken, base, count=50)

    assert report.feasible_share == pytest.approx(0.5)
    assert [rule for rule, _, _ in report.top_failing] == ["cup_round_conflict"]
    assert not report.worst_case.feasible
    assert report.min_points < report.base_points


def test_sampling_is_seeded_and_deterministic(tiny):
    evaluator, matches, plan, base = tiny

    first = evaluator.evaluate(matches, plan, base, count=2, seed=7)
    again = evaluator.evaluate(matches, plan, base, count=2, seed=7)

    assert not first.exhaustive and first.outcomes == 2
    assert first.to_dict() == again.to_dict()


# -- the real 2026 season ---------------------------------------------------------


@pytest.fixture(scope="module")
def frame(world, season):
    return build_season_frame(world, season)[0]


@pytest.fixture(scope="module")
def solved(world, season, competitions, frame):
    from terminliste.solvers.base import SolveRequest
    from terminliste.solvers.local_search import LocalSearchScheduler

    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions,
        constraints=build_constraints(world, season, competitions, frame.commitments),
        ctx=ctx, seed=1, top_n=1, time_budget_s=10.0, cup_schedules=frame.cup_schedules,
        commitments=frame.commitments, frame=frame,
    )
    return LocalSearchScheduler().solve(request).candidates[0], ctx


def _robustness(world, season, competitions, frame, solved, count, seed=42):
    best, ctx = solved
    return evaluate_robustness(
        world, season, competitions, frame, best.matches, best.contingency_plan, best.score,
        ctx, count, seed,
    )


def test_a_sampled_run_on_the_real_season_is_repeatable_and_bounded(
    world, season, competitions, frame, solved
):
    first = _robustness(world, season, competitions, frame, solved, 20)
    again = _robustness(world, season, competitions, frame, solved, 20)
    other = _robustness(world, season, competitions, frame, solved, 20, seed=43)

    assert first.to_dict() == again.to_dict()
    assert not first.exhaustive and first.outcomes == 20
    assert 0.0 <= first.feasible_share <= 1.0
    assert 0 < first.expected_moves <= first.max_moves
    assert first.min_points <= first.mean_points
    assert other.outcomes == 20


def test_the_robustness_is_written_to_the_json(world, season, competitions, frame, solved, tmp_path):
    best, _ = solved
    best.robustness = _robustness(world, season, competitions, frame, solved, 5)

    path = write_json(SolverResult(candidates=[best], solver="test"), tmp_path / "out.json")

    robustness = json.loads(path.read_text(encoding="utf-8"))["options"][0]["robustness"]
    assert {"feasible_share", "expected_moves", "max_moves", "top_failing_rules", "worst_case"} <= set(
        robustness
    )
