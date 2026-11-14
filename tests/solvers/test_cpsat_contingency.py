"""CP-SAT Level 1 keeps rounds off slots that many teams share with a contingent commitment (#9)."""

from __future__ import annotations

import pytest
from test_solver_contract import HAS_ORTOOLS

from terminliste.frame import build_season_frame
from terminliste.model.calendar import build_calendar
from terminliste.model.travel import ApiTravelModel
from terminliste.scoring.base import EvalContext
from terminliste.scoring.registry import build_constraints
from terminliste.slot_planner import CONTINGENT_FREE_TEAMS
from terminliste.solvers.base import SolveRequest, resolve_slot_plans

pytestmark = pytest.mark.skipif(not HAS_ORTOOLS, reason="needs ortools")


def test_level_one_slots_stay_within_the_contingent_clash_allowance(world, season, competitions):
    from ortools.sat.python import cp_model

    from terminliste.solvers.cpsat import _build_level_one, _Problem, _read_level_one

    frame = build_season_frame(world, season)[0]
    request = SolveRequest(
        world=world,
        season=season,
        competitions=competitions,
        constraints=build_constraints(world, season, competitions, frame.commitments),
        ctx=EvalContext(world=world, season=season, travel=ApiTravelModel(world)),
        cup_schedules=frame.cup_schedules,
        commitments=frame.commitments,
        frame=frame,
    )
    problem = _Problem(request, build_calendar(world, season), resolve_slot_plans(request, build_calendar(world, season)))
    built = _build_level_one(cp_model, problem, [], [])
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 60
    status = solver.Solve(built.model)
    assert status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    solution = _read_level_one(solver, built, problem)

    for lg in problem.leagues:
        for r in range(lg.rounds):
            slot = lg.slots[solution.slot_of[(lg.id, r)]]
            assert slot.contingent_clashes <= CONTINGENT_FREE_TEAMS, (lg.id, r, slot.start)
