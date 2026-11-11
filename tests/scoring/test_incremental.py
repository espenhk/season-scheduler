"""Delta scoring must agree with full evaluation after every move (#11)."""

from __future__ import annotations

import importlib.util
import random
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from terminliste.model.schema import FixedRoundDate
from terminliste.scoring.base import ScheduleIndex
from terminliste.scoring.hard import FixedRoundDates, RoundDayQuota
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


def _with_format_rules(state):
    """The state with `fixed_round_dates` and `round_day_quota` active, which
    the 2026 data does not use, so their scopes are exercised too."""
    request, working, pinned, calendars = state
    first = next(m for m in working if m.round_index == 2)
    competitions = [
        c.model_copy(
            update={
                "day_quota": {"saturday": 2, "sunday": 2},
                "fixed_round_dates": [FixedRoundDate(round=3, date=first.date)],
            }
        )
        if c.format == "league"
        else c
        for c in request.competitions
    ]
    constraints = [
        *request.constraints,
        FixedRoundDates(competitions=competitions),
        RoundDayQuota(competitions=competitions),
    ]
    return replace(request, constraints=constraints), working, pinned, calendars


def _keys(mapping) -> dict:
    return {k: sorted(m.key for m in v) for k, v in mapping.items() if v}


def _assert_agrees(scorer: DeltaScorer, working, request) -> None:
    index = ScheduleIndex(working)
    for ci, constraint in enumerate(request.constraints):
        result = constraint.evaluate(index, request.ctx)
        assert scorer._count[ci] == result.count, constraint.id
        assert scorer._total[ci] == pytest.approx(result.total, abs=1e-6), constraint.id
    assert scorer.cost == pytest.approx(ls._cost(working, request.constraints, request.ctx).cost)
    live = scorer.live
    assert _keys(live.by_team) == _keys(index.by_team)
    assert _keys(live.by_venue_date) == _keys(index.by_venue_date)
    assert _keys(live.by_date) == _keys(index.by_date)
    assert _keys(live.by_competition) == _keys(index.by_competition)


def _raw_team_swap(working, rng) -> list:
    """Two teams of one competition exchanged in every match, with no
    `_date_ok` filtering, so any resulting violation reaches the scorer."""
    competition_id = rng.choice(sorted({m.competition_id for m in working}))
    in_competition = [(i, m) for i, m in enumerate(working) if m.competition_id == competition_id]
    teams = sorted({t for _, m in in_competition for t in (m.home_team, m.away_team)})
    a, b = rng.sample(teams, 2)
    undo = []
    for i, match in in_competition:
        if {a, b}.isdisjoint((match.home_team, match.away_team)):
            continue
        undo.append(ls._snap(i, match))
        match.home_team = ls._swap(match.home_team, a, b)
        match.away_team = ls._swap(match.away_team, a, b)
    return undo


# Rule authors: a new rule's `delta_scope` is only verified here, so make sure
# this test's state activates the rule.
@pytest.mark.parametrize("seed", [11, 12, 13])
def test_delta_matches_full_evaluation_on_random_move_sequences(state, seed):
    request, working, pinned, calendars = _with_format_rules(state)
    working = [ls.WorkingMatch(**{f: getattr(m, f) for f in m.__slots__}) for m in working]
    scorer = DeltaScorer(working, request.constraints, request.ctx)
    moves = ls._MoveSet(working, pinned, request, calendars, live=scorer.live)
    rng = random.Random(seed)

    _assert_agrees(scorer, working, request)
    seen_operators = set()
    steps = 0
    violations = 0
    hard_seen: set[str] = set()
    while steps < 300:
        roll = rng.random()
        if roll < 0.25:
            # Unfiltered shift, so rest, venue and club violations get scored.
            i = rng.randrange(len(working))
            undo = [ls._snap(i, working[i])]
            working[i].date = rng.choice(working[i].slot.days)
        elif roll < 0.35:
            undo = _raw_team_swap(working, rng)
            if not undo:
                continue
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
        hard_seen.update(broken)
        if rng.random() < 0.5:
            scorer.accept()
        else:
            ls._restore(working, undo)
            scorer.reject()
        _assert_agrees(scorer, working, request)

    assert {0, 1, 2} <= seen_operators
    assert violations > 20
    assert {"fixed_round_dates", "round_day_quota"} <= hard_seen


def test_every_declared_scope_is_exercised_and_resync_is_a_noop(state):
    request, working, _, _ = state
    working = [ls.WorkingMatch(**{f: getattr(m, f) for f in m.__slots__}) for m in working]
    scorer = DeltaScorer(working, request.constraints, request.ctx)
    scopes = {getattr(c, "delta_scope", None) for c in request.constraints}
    assert {"team", "club", "venue_date", "match", "competition", "round"} <= scopes

    before = scorer.cost
    scorer.resync()
    assert abs(scorer.cost - before) < 1e-6


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
