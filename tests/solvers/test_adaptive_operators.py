"""Adaptive operator selection shifts the move mix towards what improves the schedule (#11)."""

from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

from terminliste.solvers import local_search as ls

_BENCH = Path(__file__).resolve().parents[2] / "scripts" / "bench_scoring.py"


def _load_bench():
    spec = importlib.util.spec_from_file_location("bench_scoring", _BENCH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("bench_scoring", module)
    spec.loader.exec_module(module)
    return module


def _moves():
    request, working, pinned, calendars = _load_bench().build_state(seed=2)
    return ls._MoveSet(working, pinned, request, calendars)


def test_operator_with_all_the_wins_gains_weight_and_none_drops_below_the_floor():
    moves = _moves()
    for n in range(ls._REWEIGHT_EVERY * 2):
        moves._last_operator = 2 if n % 2 == 0 else 0
        moves.record(improved=moves._last_operator == 2)

    weights = moves._weights
    assert weights[2] > ls._BASE_WEIGHTS[2]
    assert weights[0] < ls._BASE_WEIGHTS[0]
    assert min(weights) >= ls._MIN_WEIGHT


def test_operator_draws_are_reproducible_for_a_seed():
    first, second = _moves(), _moves()
    draws = []
    for moves in (first, second):
        rng = random.Random(9)
        sequence = []
        for _ in range(60):
            if moves.apply(rng) is not None:
                sequence.append(moves._last_operator)
        draws.append(sequence)
    assert draws[0] == draws[1]
