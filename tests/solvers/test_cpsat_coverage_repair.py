"""CP-SAT moves matches the contingency planner finds no fallback for (#53)."""

from __future__ import annotations

import time

import pytest
from test_solver_contract import HAS_ORTOOLS, _cpsat_request, _get_scheduler

from terminliste.frame import build_season_frame
from terminliste.model.calendar import build_calendar
from terminliste.model.travel import ApiTravelModel
from terminliste.scoring.base import EvalContext
from terminliste.scoring.registry import build_constraints
from terminliste.solvers.base import SolveRequest, resolve_slot_plans

pytestmark = pytest.mark.skipif(not HAS_ORTOOLS, reason="needs ortools")


@pytest.fixture(scope="module")
def level_two(world, season, competitions):
    """The 2026 problem with Level 1 solved and Level 2's fixtures placed."""
    from ortools.sat.python import cp_model

    from terminliste.solvers.cpsat import _build_level_one, _place, _Problem, _read_level_one

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
    calendar = build_calendar(world, season)
    problem = _Problem(request, calendar, resolve_slot_plans(request, calendar))
    built = _build_level_one(cp_model, problem, [], [])
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 20
    assert solver.Solve(built.model) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    placed, error = _place(problem, _read_level_one(solver, built, problem))
    assert error is None
    return problem, placed


def _solve(cp_model, problem, placed, **kwargs):
    from terminliste.solvers.cpsat import _build_level_two, _extract

    built = _build_level_two(cp_model, problem, placed, [], relax=False, **kwargs)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10
    solver.parameters.num_search_workers = 8
    assert solver.Solve(built.model) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    return _extract(solver, built.placed, built.days)


def test_level_two_never_picks_a_banned_day(level_two):
    from ortools.sat.python import cp_model

    problem, placed = level_two
    first = _solve(cp_model, problem, placed)
    victim = next(m for m in first if len(next(p for p in placed if p.key == m.key).days) > 1)
    again = _solve(cp_model, problem, placed, banned={(victim.key, victim.date)})

    assert next(m for m in again if m.key == victim.key).date != victim.date


def test_repair_moves_an_uncovered_match_and_stops_once_covered(level_two, monkeypatch):
    from ortools.sat.python import cp_model

    from terminliste.solvers import cpsat

    problem, placed = level_two
    first = _solve(cp_model, problem, placed)
    victim = next(m for m in first if len(next(p for p in placed if p.key == m.key).days) > 1)
    reported = []

    def uncovered(_problem, matches):
        # The planner reports the match only while it stays on the day it was banned from.
        found = [m.key for m in matches if m.key == victim.key and m.date == victim.date]
        reported.append(found)
        return found

    monkeypatch.setattr(cpsat, "_uncovered", uncovered)
    matches, left = cpsat._repair_coverage(
        cp_model, problem, placed, [], first, time.perf_counter() + 40, 1, False
    )

    assert left == []
    assert next(m for m in matches if m.key == victim.key).date != victim.date
    assert [bool(found) for found in reported] == [True, False]


def test_repair_keeps_the_best_schedule_when_the_bans_leave_no_day(level_two, monkeypatch):
    from ortools.sat.python import cp_model

    from terminliste.solvers import cpsat

    problem, placed = level_two
    first = _solve(cp_model, problem, placed)
    victim = first[0].key
    # Whatever day the match is on, report it uncovered: the bans soon exhaust its days.
    monkeypatch.setattr(cpsat, "_uncovered", lambda _problem, matches: [victim])
    matches, left = cpsat._repair_coverage(
        cp_model, problem, placed, [], first, time.perf_counter() + 40, 1, False
    )

    assert left == [victim]
    assert len(matches) == len(first)


def test_rounds_of_reads_league_and_round_from_match_keys():
    from terminliste.solvers.cpsat import _rounds_of

    keys = ["eliteserien_2026:19:a-b", "eliteserien_2026:19:c-d", "toppserien_2026:3:e-f"]
    assert _rounds_of(keys) == [("eliteserien_2026", 19), ("toppserien_2026", 3)]


def test_a_slot_choice_with_uncovered_matches_is_cut_and_level_one_re_solved(monkeypatch):
    from terminliste.solvers import cpsat

    calls = []

    def repair(cp, problem, placed, previous_dates, matches, deadline, seed, relax):
        calls.append(matches)
        return matches, ([matches[0].key] if len(calls) == 1 else [])

    monkeypatch.setattr(cpsat, "_repair_coverage", repair)
    # Short solves, so the second attempt fits the budget.
    monkeypatch.setattr(cpsat, "_LEVEL_ONE_BUDGET_SHARE", 0.1)
    monkeypatch.setattr(cpsat, "_FIRST_SOLVE_SHARE", 0.1)
    monkeypatch.setattr(cpsat, "_has_contingency", lambda request: True)
    result = _get_scheduler("cpsat").solve(_cpsat_request())

    assert len(calls) == 2
    assert result.best is not None and result.best.score.feasible


def test_a_coverage_cut_stays_in_its_pass(monkeypatch):
    from terminliste.solvers import cpsat

    learned_seen = []
    real_pass = cpsat._run_pass

    def spy(*args):
        learned_seen.append(args[-1])
        return real_pass(*args)

    cut_rounds = []

    def repair(cp, problem, placed, previous_dates, matches, deadline, seed, relax):
        if not cut_rounds:
            cut_rounds.append(cpsat._rounds_of([matches[0].key]))
            return matches, [matches[0].key]
        return matches, []

    monkeypatch.setattr(cpsat, "_run_pass", spy)
    monkeypatch.setattr(cpsat, "_repair_coverage", repair)
    monkeypatch.setattr(cpsat, "_LEVEL_ONE_BUDGET_SHARE", 0.1)
    monkeypatch.setattr(cpsat, "_FIRST_SOLVE_SHARE", 0.1)
    monkeypatch.setattr(cpsat, "_has_contingency", lambda request: True)
    _get_scheduler("cpsat").solve(_cpsat_request(top_n=2))

    assert cut_rounds
    # `learned` is shared by every pass, so it must hold proven conflicts only.
    assert all(rounds != cut_rounds[0] for _, rounds in learned_seen[0])
