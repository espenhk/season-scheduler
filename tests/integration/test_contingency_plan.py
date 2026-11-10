"""Fallbacks on the real 2026 data: reserve dates stay clear of the base plan
and every collision the search leaves has a valid fallback (#9)."""

from __future__ import annotations

import pytest

from terminliste.frame import build_season_frame
from terminliste.model.calendar import build_calendar
from terminliste.scoring.base import EvalContext
from terminliste.scoring.registry import build_constraints
from terminliste.slot_planner import plan_slots
from terminliste.solvers import SolveRequest, get_scheduler
from terminliste.solvers.greedy import build_initial_schedule


@pytest.fixture(scope="module")
def frame(world, season):
    return build_season_frame(world, season)[0]


def test_the_2026_data_declares_reserve_dates_clear_of_every_commitment(season, frame):
    assert season.reserve_dates
    committed = {c.date for c in frame.commitments}
    for reserve in season.reserve_dates:
        assert reserve.date not in committed, reserve.date
        assert reserve.verified is False


def test_greedy_base_plan_never_uses_a_reserve_date(world, season, competitions, frame):
    matches, _, _ = build_initial_schedule(
        world,
        season,
        competitions,
        seed=42,
        cup_schedules=frame.cup_schedules,
        commitments=frame.commitments,
    )

    assert not {m.date for m in matches} & season.reserve_date_set


def test_local_search_covers_every_collision_and_keeps_reserve_dates_free(
    world, season, competitions, travel, frame
):
    constraints = build_constraints(world, season, competitions, frame.commitments)
    slot_plans = plan_slots(world, season, competitions, build_calendar(world, season), frame)
    request = SolveRequest(
        world=world,
        season=season,
        competitions=competitions,
        cup_schedules=frame.cup_schedules,
        commitments=frame.commitments,
        frame=frame,
        slot_plans=slot_plans,
        constraints=constraints,
        ctx=EvalContext(world=world, season=season, travel=travel),
        seed=1,
        top_n=1,
        time_budget_s=8.0,
    )

    best = get_scheduler("local").solve(request).best

    assert best is not None and best.contingency_plan is not None
    assert not {m.date for m in best.matches} & season.reserve_date_set
    plan = best.contingency_plan
    assert set(plan.uncovered) <= set(plan.colliding)
    # Coverage is a hard rule, so a feasible schedule has no uncovered match.
    uncovered_violations = best.score.result("contingency_coverage").count
    assert uncovered_violations == len(plan.uncovered)
    horizon = season.contingency_policy.max_fallback_days
    reserve = season.reserve_date_set
    for fallback in plan.fallbacks.values():
        assert fallback.days_moved <= horizon
        assert (fallback.kind == "reserve_date") == (fallback.date in reserve)
