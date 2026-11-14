"""Fallback plans on the real 2026 data (#9)."""

from __future__ import annotations

import json

import pytest

from terminliste.frame import build_season_frame
from terminliste.model.calendar import build_calendar
from terminliste.model.travel import ApiTravelModel
from terminliste.report.render import write_json
from terminliste.scoring.base import EvalContext, ScheduleIndex
from terminliste.scoring.registry import build_constraints
from terminliste.slot_planner import plan_slots
from terminliste.solvers.base import SolveRequest
from terminliste.solvers.local_search import LocalSearchScheduler


@pytest.fixture(scope="module")
def frame(world, season):
    return build_season_frame(world, season)[0]


def test_reserve_dates_are_not_offered_to_any_league_round(world, season, competitions, frame):
    plans = plan_slots(world, season, competitions, build_calendar(world, season), frame)

    for plan in plans.values():
        for slot in plan.candidates:
            assert not set(slot.days) & season.reserve_date_set


def test_no_round_slot_is_shared_with_a_contingent_commitment_of_many_teams(
    world, season, competitions, frame
):
    plans = plan_slots(world, season, competitions, build_calendar(world, season), frame)

    for plan in plans.values():
        assert max(slot.contingent_clashes for slot in plan.chosen) <= 3


@pytest.fixture(scope="module")
def result(world, season, competitions, frame):
    request = SolveRequest(
        world=world,
        season=season,
        competitions=competitions,
        constraints=build_constraints(world, season, competitions, frame.commitments),
        ctx=EvalContext(world=world, season=season, travel=ApiTravelModel(world)),
        seed=1,
        top_n=1,
        time_budget_s=10.0,
        cup_schedules=frame.cup_schedules,
        commitments=frame.commitments,
        frame=frame,
    )
    return LocalSearchScheduler().solve(request), request


def test_every_candidate_carries_a_plan_whose_fallbacks_check_out(result):
    solved, request = result
    planner = next(c.planner for c in request.constraints if hasattr(c, "planner"))
    best = solved.candidates[0]

    assert best.contingency_plan is not None
    analysis = planner.check(ScheduleIndex(best.matches), best.contingency_plan)
    assert analysis.invalid == [] and analysis.collisions == []
    assert best.contingency_plan.fallbacks


def test_the_base_plan_never_uses_a_reserve_date(result, season):
    best = result[0].candidates[0]

    assert not {m.date for m in best.matches} & season.reserve_date_set


def test_the_plan_is_written_to_the_json(result, tmp_path):
    path = write_json(result[0], tmp_path / "out.json")

    plan = json.loads(path.read_text(encoding="utf-8"))["options"][0]["contingency_plan"]
    assert plan["fallbacks"]
    assert {"match", "trigger", "from", "to", "kind"} <= set(plan["fallbacks"][0])
