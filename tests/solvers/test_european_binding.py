"""Greedy, local search and CP-SAT agree on which European dates are hard
(#112).

Local search scores with the hard rule and starts from the greedy placement,
so checking the greedy cost, CP-SAT's candidate domain and
`EuropeanCommitmentConflict` against each other covers all three.
"""

from __future__ import annotations

from datetime import date, timedelta

import factories as f
from terminliste.model.schema import Fixture
from terminliste.rounds.european_schedule import EuropeanCommitmentDate
from terminliste.scoring.base import EvalContext, evaluate
from terminliste.scoring.hard import EuropeanCommitmentConflict
from terminliste.scoring.soft import EuropeanCommitmentSoftConflict
from terminliste.solvers.cpsat import _european_clear
from terminliste.solvers.greedy import (
    _EUROPEAN_CONFLICT,
    _round_clear,
    european_cost,
)


class _StubTravel:
    def hours(self, a, b):
        return 1.0


def _commitment(team_id: str, day: date, certain: bool) -> EuropeanCommitmentDate:
    return EuropeanCommitmentDate(
        team_id=team_id,
        date=day,
        min_rest_days=2,
        label=f"uecl: {day} ({'certain' if certain else 'if reached'})",
        competition_id="uecl",
        certain=certain,
    )


def _world():
    v1 = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v1")
    world = f.world([f.club("c1", [t1]), f.club("c2", [t2])], [v1], [])
    season = f.season(competitions=[], european_competitions=["uecl"])
    return world, season


# Thursday 23 Jul is binding for t1, Thursday 20 Aug is only reachable.
_COMMITMENTS = {
    "t1": [
        _commitment("t1", date(2026, 7, 23), certain=True),
        _commitment("t1", date(2026, 8, 20), certain=False),
    ]
}
_FIXTURE = Fixture(competition_id="league", home_team="t1", away_team="t2", leg=1, round_index=0)


def _days():
    start = date(2026, 7, 15)
    return [start + timedelta(days=i) for i in range(45)]


def test_greedy_cpsat_and_hard_rule_agree_on_binding_european_dates():
    world, season = _world()
    ctx = EvalContext(world=world, season=season, travel=_StubTravel())
    hard = EuropeanCommitmentConflict(commitments_by_team=_COMMITMENTS)

    hard_days = set()
    for day in _days():
        match = f.match("league", "t1", "t2", day, "v1")
        rule_says_hard = evaluate([match], [hard], ctx).hard_violations > 0
        greedy_says_hard = european_cost(_COMMITMENTS, _FIXTURE, day) >= _EUROPEAN_CONFLICT
        cpsat_says_hard = day not in _european_clear([day], _FIXTURE, _COMMITMENTS)
        pin_says_hard = not _round_clear(day, ["t1", "t2"], {}, _COMMITMENTS)

        assert rule_says_hard == greedy_says_hard == cpsat_says_hard == pin_says_hard, day
        if rule_says_hard:
            hard_days.add(day)

    # Only the certain Thursday and its two rest days either side bind.
    assert hard_days == {date(2026, 7, 23) + timedelta(days=d) for d in range(-2, 3)}


def test_non_binding_dates_are_a_greedy_nudge_and_a_soft_penalty():
    world, season = _world()
    ctx = EvalContext(world=world, season=season, travel=_StubTravel())
    soft = EuropeanCommitmentSoftConflict(commitments_by_team=_COMMITMENTS)
    day = date(2026, 8, 20)

    assert 0 < european_cost(_COMMITMENTS, _FIXTURE, day) < _EUROPEAN_CONFLICT
    assert _european_clear([day], _FIXTURE, _COMMITMENTS) == [day]
    score = evaluate([f.match("league", "t1", "t2", day, "v1")], [soft], ctx)
    assert score.hard_violations == 0
    assert score.result(soft.id).count == 1
