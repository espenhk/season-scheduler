"""Greedy, local search and CP-SAT agree on which commitments are hard (#112, #8).

Local search scores with the hard rule and starts from the greedy placement,
so checking the greedy cost, CP-SAT's candidate domain and
`CommitmentConflict` against each other covers all three.
"""

from __future__ import annotations

from datetime import date, timedelta

import factories as f

from terminliste.contingency import ContingencyPlanner
from terminliste.model.schema import Fixture
from terminliste.scoring.base import EvalContext, evaluate
from terminliste.scoring.hard import CommitmentConflict
from terminliste.scoring.soft import ExpectedDisruption
from terminliste.solvers.cpsat import _commitment_clear
from terminliste.solvers.greedy import (
    _COMMITMENT_CONFLICT,
    _round_clear,
    commitment_cost,
)


class _StubTravel:
    def hours(self, a, b):
        return 1.0


def _world():
    v1 = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v1")
    world = f.world([f.club("c1", [t1]), f.club("c2", [t2])], [v1], [])
    season = f.season(competitions=[], european_competitions=["uecl"])
    return world, season


# Thursday 23 Jul is hard for t1, Thursday 20 Aug is only reachable.
_COMMITMENTS = f.commitment_set(
    f.commitment("t1", date(2026, 7, 23), hard=True),
    f.commitment("t1", date(2026, 8, 20), hard=False, probability=0.5),
)
_FIXTURE = Fixture(competition_id="league", home_team="t1", away_team="t2", leg=1, round_index=0)


def _days():
    start = date(2026, 7, 15)
    return [start + timedelta(days=i) for i in range(45)]


def test_greedy_cpsat_and_hard_rule_agree_on_hard_commitments():
    world, season = _world()
    ctx = EvalContext(world=world, season=season, travel=_StubTravel())
    hard = CommitmentConflict(commitments=_COMMITMENTS, source="uefa")

    hard_days = set()
    for day in _days():
        match = f.match("league", "t1", "t2", day, "v1")
        rule_says_hard = evaluate([match], [hard], ctx).hard_violations > 0
        greedy_says_hard = commitment_cost(_COMMITMENTS, _FIXTURE, day) >= _COMMITMENT_CONFLICT
        cpsat_says_hard = day not in _commitment_clear([day], _FIXTURE, _COMMITMENTS)
        pin_says_hard = not _round_clear(day, ["t1", "t2"], _COMMITMENTS)

        assert rule_says_hard == greedy_says_hard == cpsat_says_hard == pin_says_hard, day
        if rule_says_hard:
            hard_days.add(day)

    # Only the hard Thursday and its two rest days either side bind.
    assert hard_days == {date(2026, 7, 23) + timedelta(days=d) for d in range(-2, 3)}


def test_contingent_dates_are_a_greedy_nudge_and_a_soft_penalty():
    world, season = _world()
    ctx = EvalContext(world=world, season=season, travel=_StubTravel())
    soft = ExpectedDisruption(planner=ContingencyPlanner(world, season, [], _COMMITMENTS))
    day = date(2026, 8, 20)

    assert 0 < commitment_cost(_COMMITMENTS, _FIXTURE, day) < _COMMITMENT_CONFLICT
    assert _commitment_clear([day], _FIXTURE, _COMMITMENTS) == [day]
    score = evaluate([f.match("league", "t1", "t2", day, "v1")], [soft], ctx)
    assert score.hard_violations == 0
    assert score.result(soft.id).count == 1
