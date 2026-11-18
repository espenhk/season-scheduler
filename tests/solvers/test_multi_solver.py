"""Running several solvers side by side (#81)."""

from __future__ import annotations

import json

import pytest

from terminliste.model.travel import ApiTravelModel
from terminliste.report.render import render_report, write_json
from terminliste.scoring.base import EvalContext
from terminliste.scoring.registry import build_constraints
from terminliste.solvers import get_scheduler, parse_solver_names, solve_many
from terminliste.solvers.base import SolveRequest, SolverResult
from tests.solvers.test_solver_contract import _small_world


def _request(iterations: int = 400):
    world, season, competitions = _small_world()
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions,
        constraints=build_constraints(world, season, competitions),
        ctx=ctx, seed=1, top_n=3, iterations=iterations,
    )
    return world, season, request


class _SecondLocal:
    """A second backend that is local search under another name."""

    name = "local-b"

    def solve(self, request):
        result = get_scheduler("local").solve(request)
        result.solver = self.name
        return result


class _Missing:
    name = "cpsat"

    def solve(self, request):
        raise RuntimeError("OR-Tools is not installed")


def _factory(name):
    return {"local": get_scheduler("local"), "cpsat": _Missing(), "b": _SecondLocal()}[name]


def test_parse_accepts_repeats_and_commas_and_dedupes():
    assert parse_solver_names(None) == ["local"]
    assert parse_solver_names(["local", "cpsat,local"]) == ["local", "cpsat"]
    with pytest.raises(ValueError):
        parse_solver_names(["nope"])


def test_each_solver_contributes_three_distinct_tagged_options():
    _, _, request = _request()
    result = solve_many(["local", "b"], request, _factory)

    assert len(result.candidates) == 6
    assert [r.options for r in result.runs] == [3, 3]
    for solver in ("local", "local-b"):
        own = [c for c in result.candidates if c.solver == solver]
        assert [c.index for c in own] == [1, 2, 3]
        assert len({tuple(sorted(c.assignment().items())) for c in own}) == 3


def test_unavailable_solver_is_reported_without_failing_the_others():
    _, _, request = _request()
    result = solve_many(["cpsat", "local"], request, _factory)

    assert len(result.candidates) == 3
    assert [(r.solver, r.status) for r in result.runs] == [("cpsat", "unavailable"), ("local", "ok")]
    assert any("OR-Tools" in str(n) and "cpsat" in str(n) for n in result.notes)


def test_single_solver_run_is_untouched():
    _, _, request = _request()
    result = solve_many(["local"], request, _factory)

    assert result.runs == []
    assert result.solver == "local"
    assert all(c.label.startswith("Option") for c in result.candidates)
    with pytest.raises(RuntimeError):
        solve_many(["cpsat"], request, _factory)


def test_json_and_report_group_options_by_solver(tmp_path):
    world, season, request = _request()
    result: SolverResult = solve_many(["local", "b"], request, _factory)
    write_json(result, tmp_path / "s.json")
    render_report(world, season, result, tmp_path / "s.html")

    data = json.loads((tmp_path / "s.json").read_text())
    assert [o["solver"] for o in data["options"]] == ["local"] * 3 + ["local-b"] * 3
    assert [o["index"] for o in data["options"]] == [1, 2, 3, 1, 2, 3]
    assert [s["solver"] for s in data["solvers"]] == ["local", "local-b"]
    html = (tmp_path / "s.html").read_text()
    assert "Solvers side by side" in html
