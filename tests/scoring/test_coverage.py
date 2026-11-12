"""Achieved-of-opportunities counts (#34): metadata that never moves a score,
checked by hand on a small world."""

from __future__ import annotations

from datetime import date, timedelta

import factories as f

from terminliste.scoring.base import EvalContext, ScheduleIndex
from terminliste.scoring.hard import OneMatchPerTeamPerDay
from terminliste.scoring.soft import (
    ConsecutiveHomeDays,
    HomeAwayBalance,
    HomeAwayBreaks,
    PreferredWeekday,
    RestComfort,
)

SAT = date(2026, 6, 6)
SUN = date(2026, 6, 7)


class _FixedTravel:
    def hours(self, a, b):
        return 1.0


def _world():
    va, vb, vc = f.venue("va"), f.venue("vb"), f.venue("vc")
    a_m = f.team("a_m", "club_a", "va", gender="men")
    a_w = f.team("a_w", "club_a", "va", gender="women")
    b_m = f.team("b_m", "club_b", "vb", gender="men")
    b_w = f.team("b_w", "club_b", "vb", gender="women")
    opp_m = f.team("opp_m", "opp1", "vc", gender="men")
    opp_w = f.team("opp_w", "opp2", "vc", gender="women")
    clubs = [
        f.club("club_a", [a_m, a_w]),
        f.club("club_b", [b_m, b_w]),
        f.club("opp1", [opp_m]),
        f.club("opp2", [opp_w]),
    ]
    comp_m = f.competition("elite", ["a_m", "b_m", "opp_m"], gender="men", preferred_weekday="sunday")
    comp_w = f.competition("topp", ["a_w", "b_w", "opp_w"], gender="women", preferred_weekday="saturday")
    world = f.world(clubs, [va, vb, vc], [comp_m, comp_w])
    return world, f.season(competitions=["elite", "topp"]), [comp_m, comp_w]


def _run(rule, matches):
    world, season, _ = _world()
    ctx = EvalContext(world=world, season=season, travel=_FixedTravel(), detail=False)
    return rule.evaluate(ScheduleIndex(matches), ctx)


def test_preferred_weekday_counts_matches_on_the_preferred_day_of_all_league_matches():
    _, _, comps = _world()
    matches = [
        f.match("elite", "a_m", "opp_m", SUN, "va"),  # Sunday: on the preferred day
        f.match("elite", "b_m", "opp_m", SAT, "vb"),  # Saturday: off it
        f.match("elite", "opp_m", "a_m", SUN, "vc"),  # Sunday
        f.match("topp", "a_w", "opp_w", SAT, "va"),  # Saturday: the women's preferred day
    ]
    result = _run(PreferredWeekday(competitions=comps), matches)
    assert (result.achieved, result.opportunities) == (3, 4)
    assert result.count == result.achieved


def test_back_to_back_home_days_counts_home_matches_that_have_a_partner_out_of_all_club_home_matches():
    _, _, comps = _world()
    matches = [
        f.match("elite", "a_m", "opp_m", SAT, "va"),  # club A: a_m home Saturday
        f.match("topp", "a_w", "opp_w", SUN, "va"),  # club A: a_w home Sunday, the pair
        f.match("elite", "a_m", "opp_m", date(2026, 6, 20), "va"),  # no partner
        f.match("elite", "b_m", "opp_m", date(2026, 6, 13), "vb"),  # club B: no partner
    ]
    result = _run(ConsecutiveHomeDays(competitions=comps), matches)
    assert result.count == 1  # one back-to-back pair
    assert (result.achieved, result.opportunities) == (2, 4)


def test_comfortable_rest_counts_gaps_at_or_above_target_out_of_all_gaps():
    _, _, comps = _world()
    matches = [
        f.match("elite", "a_m", "opp_m", SAT, "va"),
        f.match("elite", "opp_m", "a_m", date(2026, 6, 9), "vc"),  # 2 days' rest: short
        f.match("elite", "a_m", "opp_m", date(2026, 6, 16), "va"),  # 6 days' rest: comfortable
    ]
    result = _run(RestComfort(competitions=comps), matches)
    # a_m has two gaps, opp_m has two gaps; each team's second gap is comfortable.
    assert (result.achieved, result.opportunities) == (2, 4)


def test_home_away_balance_counts_team_halves_that_stay_within_tolerance():
    _, _, comps = _world()
    # a_m hosts in every match but the fifth; opp_m is the mirror image.
    home_pattern = [True, True, True, True, False, True]
    matches = []
    for i, a_m_home in enumerate(home_pattern):
        day = SAT + timedelta(weeks=i)
        if a_m_home:
            matches.append(f.match("elite", "a_m", "opp_m", day, "va"))
        else:
            matches.append(f.match("elite", "opp_m", "a_m", day, "vc"))
    result = _run(HomeAwayBalance(competitions=comps), matches)
    # First halves drift 3 for both teams (unbalanced); second halves drift 1 (balanced).
    assert (result.achieved, result.opportunities) == (2, 4)


def test_rules_without_a_natural_count_and_hard_rules_report_no_coverage():
    _, _, comps = _world()
    matches = [f.match("elite", "a_m", "opp_m", SUN, "va")]
    world, season, _ = _world()
    ctx = EvalContext(world=world, season=season, travel=_FixedTravel(), detail=False)
    hard = OneMatchPerTeamPerDay().evaluate(ScheduleIndex(matches), ctx)
    assert (hard.achieved, hard.opportunities) == (None, None)
    breaks = HomeAwayBreaks(competitions=comps).evaluate(ScheduleIndex(matches), ctx)
    assert (breaks.achieved, breaks.opportunities) == (None, None)
