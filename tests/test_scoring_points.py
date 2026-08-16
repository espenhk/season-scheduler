"""`Score.points` — the 0-100 normalised quality score.

Constructed directly against `Score` rather than through `evaluate()`: the
scale's contract (bounded range, infeasible cap, monotonic decay) doesn't
depend on any particular constraint or world, so a bare `Score` is the
smallest fixture that can test it.
"""

from __future__ import annotations

from terminliste.scoring.base import INFEASIBLE_CEILING, Score


def _score(hard_violations: int = 0, soft_total: float = 0.0, num_matches: int = 100) -> Score:
    return Score(hard_violations=hard_violations, soft_total=soft_total, num_matches=num_matches)


def test_points_always_within_0_and_100():
    cases = [
        _score(hard_violations=0, soft_total=-1000.0),
        _score(hard_violations=0, soft_total=0.0),
        _score(hard_violations=0, soft_total=1_000_000.0),
        _score(hard_violations=1, soft_total=500.0),
        _score(hard_violations=50, soft_total=500.0),
        _score(hard_violations=0, soft_total=100.0, num_matches=0),
    ]
    for score in cases:
        assert 0.0 <= score.points <= 100.0


def test_hard_violation_never_scores_above_the_infeasible_ceiling():
    # However good the soft score, one broken hard rule caps the result.
    barely_broken = _score(hard_violations=1, soft_total=1_000_000.0)
    assert barely_broken.points <= INFEASIBLE_CEILING


def test_more_hard_violations_score_lower():
    one = _score(hard_violations=1)
    five = _score(hard_violations=5)
    twenty = _score(hard_violations=20)
    assert one.points > five.points > twenty.points > 0.0


def test_feasible_schedule_always_outscores_an_infeasible_one():
    # Even a feasible schedule with a rock-bottom soft score beats an
    # infeasible one with an enormous soft score — the two ranges never
    # overlap.
    worst_feasible = _score(hard_violations=0, soft_total=-1_000_000.0)
    best_infeasible = _score(hard_violations=1, soft_total=1_000_000.0)
    assert worst_feasible.points > best_infeasible.points


def test_feasible_zero_soft_score_lands_at_the_midpoint():
    neutral = _score(hard_violations=0, soft_total=0.0)
    assert neutral.points == (INFEASIBLE_CEILING + 100.0) / 2


def test_feasible_soft_score_saturates_towards_100():
    great = _score(hard_violations=0, soft_total=1_000_000.0)
    assert 99.0 < great.points <= 100.0


def test_feasible_negative_soft_score_stays_above_the_infeasible_ceiling():
    # A feasible schedule can still have a negative soft total (penalties
    # outweighing rewards); it must still read as strictly better than any
    # infeasible one.
    poor = _score(hard_violations=0, soft_total=-500.0)
    assert poor.points > INFEASIBLE_CEILING


def test_feasible_extreme_negative_soft_score_still_stays_strictly_above_the_ceiling():
    # At an extreme negative per-match soft score, the sigmoid underflows to
    # exactly 0.0 — without a saturation floor that would put points exactly
    # on INFEASIBLE_CEILING instead of strictly above it.
    catastrophic = _score(hard_violations=0, soft_total=-1e12)
    assert catastrophic.points > INFEASIBLE_CEILING


def test_points_with_no_matches_does_not_divide_by_zero():
    empty = _score(hard_violations=0, soft_total=0.0, num_matches=0)
    assert empty.points == (INFEASIBLE_CEILING + 100.0) / 2
