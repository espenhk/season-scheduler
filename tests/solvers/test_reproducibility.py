"""A fixed iteration count makes local search a pure function of the seed (#11)."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

from terminliste.solvers.local_search import LocalSearchScheduler

_BENCH = Path(__file__).resolve().parents[2] / "scripts" / "bench_scoring.py"


def _request(**overrides):
    spec = importlib.util.spec_from_file_location("bench_scoring", _BENCH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("bench_scoring", module)
    spec.loader.exec_module(module)
    request, *_ = module.build_state(seed=4)
    return replace(request, **overrides)


def _fingerprint(result):
    return [
        (c.score.sort_key, [(m.key, m.date.isoformat()) for m in c.matches])
        for c in result.candidates
    ]


def test_same_seed_and_iteration_count_give_identical_schedules():
    solver = LocalSearchScheduler(restarts=2)
    first = solver.solve(_request(iterations=1200, top_n=2))
    second = solver.solve(_request(iterations=1200, top_n=2))
    assert first.iterations == second.iterations
    assert _fingerprint(first) == _fingerprint(second)


def test_time_budget_run_stays_within_budget():
    result = LocalSearchScheduler(restarts=2).solve(_request(time_budget_s=3.0, top_n=1))
    assert result.elapsed_s < 3.0 * 1.25 + 2
