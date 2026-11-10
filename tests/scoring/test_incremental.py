"""Delta scoring must agree with full evaluation after every move (#11)."""

from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path

import pytest

from terminliste.scoring.base import ScheduleIndex
from terminliste.scoring.incremental import DeltaScorer
from terminliste.solvers import local_search as ls

_BENCH = Path(__file__).resolve().parents[2] / "scripts" / "bench_scoring.py"


def _load_bench():
    spec = importlib.util.spec_from_file_location("bench_scoring", _BENCH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("bench_scoring", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def state():
    return _load_bench().build_state(seed=3)


def _assert_agrees(scorer: DeltaScorer, working, request) -> None:
    index = ScheduleIndex(working)
    for ci, constraint in enumerate(request.constraints):
        result = constraint.evaluate(index, request.ctx)
        assert scorer._count[ci] == result.count, constraint.id
        assert scorer._total[ci] == pytest.approx(result.total, abs=1e-6), constraint.id
    assert scorer.cost == pytest.approx(ls._cost(working, request.constraints, request.ctx).cost)


def test_delta_matches_full_evaluation_on_random_move_sequences(state):
    request, working, pinned, calendars = state
    working = [ls.WorkingMatch(**{f: getattr(m, f) for f in m.__slots__}) for m in working]
    scorer = DeltaScorer(working, request.constraints, request.ctx)
    moves = ls._MoveSet(working, pinned, request, calendars, live=scorer.live)
    rng = random.Random(11)

    _assert_agrees(scorer, working, request)
    seen_operators = set()
    steps = 0
    violations = 0
    while steps < 400:
        if rng.random() < 0.3:
            # Unfiltered shift, so rest, venue and club violations get scored.
            i = rng.randrange(len(working))
            undo = [ls._snap(i, working[i])]
            working[i].date = rng.choice(working[i].slot.days)
        else:
            undo = moves.apply(rng)
            if undo is None:
                continue
            seen_operators.add(moves._last_operator)
        steps += 1
        cost, broken = scorer.propose([entry[0] for entry in undo])
        full = ls._cost(working, request.constraints, request.ctx)
        assert cost == pytest.approx(full.cost)
        assert broken == full.hard_ids
        violations += bool(broken)
        if rng.random() < 0.5:
            scorer.accept()
        else:
            ls._restore(working, undo)
            scorer.reject()
        _assert_agrees(scorer, working, request)

    assert {0, 1, 2} <= seen_operators
    assert violations > 20


def test_every_declared_scope_is_exercised_and_resync_is_a_noop(state):
    request, working, _, _ = state
    working = [ls.WorkingMatch(**{f: getattr(m, f) for f in m.__slots__}) for m in working]
    scorer = DeltaScorer(working, request.constraints, request.ctx)
    scopes = {getattr(c, "delta_scope", None) for c in request.constraints}
    assert {"team", "club", "venue_date", "match", "competition", "round"} <= scopes

    before = scorer.cost
    scorer.resync()
    assert scorer.cost == pytest.approx(before)


def test_pruned_proposal_is_rejected_and_leaves_the_scorer_intact(state):
    request, working, pinned, calendars = state
    working = [ls.WorkingMatch(**{f: getattr(m, f) for f in m.__slots__}) for m in working]
    scorer = DeltaScorer(working, request.constraints, request.ctx)
    moves = ls._MoveSet(working, pinned, request, calendars, live=scorer.live)
    rng = random.Random(5)

    pruned = 0
    for _ in range(300):
        undo = moves.apply(rng)
        if undo is None:
            continue
        cost, _ = scorer.propose([entry[0] for entry in undo], prune=True)
        if cost == float("inf"):
            pruned += 1
        ls._restore(working, undo)
        scorer.reject()
    _assert_agrees(scorer, working, request)
    assert pruned > 0


def test_live_index_refuses_the_away_venue_dict_it_does_not_maintain(state):
    request, working, _, _ = state
    scorer = DeltaScorer(working, request.constraints, request.ctx)
    with pytest.raises(AttributeError):
        scorer.live.away_venue_by_team_date  # noqa: B018
    assert scorer.live.away_venues
