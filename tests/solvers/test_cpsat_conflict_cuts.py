"""CP-SAT Level 1 cuts learned from Level 2 failures (conflict-directed, shared across passes)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_solver_contract import HAS_ORTOOLS, _cpsat_request, _fail_level_two, _get_scheduler

pytestmark = pytest.mark.skipif(not HAS_ORTOOLS, reason="needs ortools")

CONFLICT = [("men", 2), ("men", 5)]


def _problem():
    from terminliste.model.calendar import build_calendar
    from terminliste.solvers.base import resolve_slot_plans
    from terminliste.solvers.cpsat import _Problem

    request = _cpsat_request()
    calendar = build_calendar(request.world, request.season)
    return _Problem(request, calendar, resolve_slot_plans(request, calendar))


def _solve_level_one(cp_model, problem, cuts, tried, pin=()):
    """Solve Level 1 under extra literals `pin` (callable on the built model)."""
    from terminliste.solvers.cpsat import _build_level_one, _read_level_one

    built = _build_level_one(cp_model, problem, cuts, tried)
    for literal in pin(built) if callable(pin) else pin:
        built.model.Add(literal == 1)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 20
    status = solver.Solve(built.model)
    ok = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    return (_read_level_one(solver, built, problem) if ok else None), status


def test_cut_forbids_only_the_conflicting_rounds_slots_and_orientations():
    from ortools.sat.python import cp_model

    problem = _problem()
    failed, _ = _solve_level_one(cp_model, problem, [], [])
    assert failed is not None
    lg = problem.by_id["men"]
    pairs = {
        (lg.id, frozenset((fx.home_team, fx.away_team)))
        for c, r in CONFLICT
        for fx in lg.round_fixtures[r]
    }
    other = next(r for r in range(lg.rounds) if ("men", r) not in CONFLICT)

    def repeat_conflict_and_move(rounds_to_repeat, moved):
        def pin(built):
            literals = [built.x[(c, r, failed.slot_of[(c, r)])] for c, r in rounds_to_repeat]
            literals += [
                built.orient[k] if not failed.flipped[k] else built.orient[k].Not()
                for k in pairs
            ]
            literals.append(built.x[(moved[0], moved[1], failed.slot_of[moved])].Not())
            return literals

        return pin

    # Same conflicting rounds, a different slot elsewhere: the narrow cut rules it out.
    narrow, status = _solve_level_one(
        cp_model, problem, [], [(failed, CONFLICT)],
        pin=repeat_conflict_and_move(CONFLICT, ("men", other)),
    )
    assert narrow is None and status == cp_model.INFEASIBLE

    # The full-assignment cut (no conflict known) would have let it through.
    wide, status = _solve_level_one(
        cp_model, problem, [], [(failed, None)],
        pin=repeat_conflict_and_move(CONFLICT, ("men", other)),
    )
    assert wide is not None, status

    # Changing one conflicting round's slot is allowed again.
    moved_conflict, status = _solve_level_one(
        cp_model, problem, [], [(failed, CONFLICT)],
        pin=repeat_conflict_and_move(CONFLICT[:1], CONFLICT[1]),
    )
    assert moved_conflict is not None, status


def _fake_level_two(monkeypatch, conflict, seen, clock=None):
    """Level 2 builder that is infeasible exactly when `conflict` is among the rounds."""
    from ortools.sat.python import cp_model

    from terminliste.solvers import cpsat

    def build(cp, problem, placed, previous_dates, relax):
        seen.append((previous_dates, relax))
        keys = {(p.fixture.competition_id, p.fixture.round_index) for p in placed}
        model = cp_model.CpModel()
        flag = model.NewBoolVar("f")
        model.Add(flag == 1)
        if set(conflict) <= keys:
            model.Add(flag == 0)
        if clock is not None:
            clock[0] += 100
        return SimpleNamespace(model=model)

    monkeypatch.setattr(cpsat, "_build_level_two", build)


def _placed(rounds):
    return [
        SimpleNamespace(fixture=SimpleNamespace(competition_id=c, round_index=r))
        for c, r in rounds
    ]


def test_conflicting_rounds_shrinks_to_the_pair_without_diversity_in_the_used_model(monkeypatch):
    from ortools.sat.python import cp_model

    from terminliste.solvers.cpsat import _conflicting_rounds

    seen: list = []
    _fake_level_two(monkeypatch, CONFLICT, seen)
    rounds = [("men", r) for r in range(8)] + [("women", r) for r in range(3)]
    far = float("inf")
    keep = _conflicting_rounds(cp_model, None, _placed(rounds), far, relax=True)

    assert keep == CONFLICT
    assert seen and all(dates == [] and relax is True for dates, relax in seen)


def test_conflicting_rounds_keeps_everything_when_the_subsets_are_feasible(monkeypatch):
    from ortools.sat.python import cp_model

    from terminliste.solvers.cpsat import _conflicting_rounds

    _fake_level_two(monkeypatch, [("men", 99)], [])
    rounds = [("men", r) for r in range(4)]
    keep = _conflicting_rounds(cp_model, None, _placed(rounds), float("inf"))

    assert keep == rounds


def test_conflicting_rounds_returns_the_progress_made_when_time_runs_out(monkeypatch):
    from ortools.sat.python import cp_model

    from terminliste.solvers import cpsat

    clock = [0.0]
    monkeypatch.setattr(cpsat, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    _fake_level_two(monkeypatch, CONFLICT, [], clock)
    rounds = [("men", r) for r in range(8)]
    keep = cpsat._conflicting_rounds(cp_model, None, _placed(rounds), 10.0)

    # One round dropped before the clock passed the deadline; still a conflict, not minimal.
    assert keep == rounds[1:] and set(CONFLICT) <= set(keep)


def test_learned_cuts_are_one_list_shared_by_every_pass(monkeypatch):
    from ortools.sat.python import cp_model

    from terminliste.solvers import cpsat

    entered: list[tuple[int, int]] = []
    real = cpsat._run_pass

    def spy(*args):
        learned = args[-1]
        entered.append((id(learned), len(learned)))
        return real(*args)

    monkeypatch.setattr(cpsat, "_run_pass", spy)
    _fail_level_two(monkeypatch, [(cp_model.INFEASIBLE, "INFEASIBLE")])
    result = _get_scheduler("cpsat").solve(_cpsat_request(top_n=2, budget=60.0))

    assert len(result.candidates) == 2, result.notes
    assert len({ident for ident, _ in entered}) == 1
    assert entered[0][1] == 0 and entered[1][1] >= 1


def test_a_failed_pass_hands_its_conflict_to_the_next_one(monkeypatch):
    from ortools.sat.python import cp_model

    from terminliste.solvers import cpsat

    monkeypatch.setattr(cpsat, "_MAX_LEVEL_TWO_RETRIES", 1)
    _fail_level_two(monkeypatch, [(cp_model.INFEASIBLE, "INFEASIBLE")])
    result = _get_scheduler("cpsat").solve(_cpsat_request(top_n=2, budget=60.0))

    assert any("pass 0: no slot choice admitted" in n for n in result.notes), result.notes
    assert len(result.candidates) == 1
    assert result.candidates[0].seed == 2  # found by pass 1, resuming after pass 0 failed
