"""Soft constraints: reward/penalty direction and the away-day travel cutoff."""

from __future__ import annotations

from datetime import date

import factories as f
from terminliste.scoring.base import EvalContext, evaluate
from terminliste.scoring.soft import (
    ConsecutiveAwayDays,
    ConsecutiveHomeDays,
    HomeAwayBalance,
    HomeAwayBreaks,
    PreferredWeekday,
    RestComfort,
    SoftVenuePreference,
)


class _FixedTravel:
    """Returns a caller-set number of hours for every pair."""

    def __init__(self, hours: float):
        self._hours = hours

    def hours(self, a, b):
        return self._hours


def _ctx(world, season, travel=None):
    return EvalContext(world=world, season=season, travel=travel or _FixedTravel(1.0), detail=True)


def _dual_club_world(venue_a="v1", venue_b="v2"):
    va, vb = f.venue(venue_a), f.venue(venue_b)
    men = f.team("dual_m", "dual", venue_a, gender="men")
    women = f.team("dual_w", "dual", venue_b, gender="women")
    opp_m = f.team("opp_m", "opp1", venue_a, gender="men")
    opp_w = f.team("opp_w", "opp2", venue_b, gender="women")
    clubs = [f.club("dual", [men, women]), f.club("opp1", [opp_m]), f.club("opp2", [opp_w])]
    comp_m = f.competition("elite", ["dual_m", "opp_m"], gender="men", weights={"consecutive_home_days": 25.0, "consecutive_away_days": 15.0})
    comp_w = f.competition("topp", ["dual_w", "opp_w"], gender="women", weights={"consecutive_home_days": 25.0, "consecutive_away_days": 15.0})
    world = f.world(clubs, [va, vb], [comp_m, comp_w])
    season = f.season(competitions=["elite", "topp"])
    return world, season, comp_m, comp_w


# -- preferred_weekday -----------------------------------------------------


def test_preferred_weekday_rewards_matches_on_the_configured_day():
    v = f.venue("v1")
    t1, t2 = f.team("t1", "c1", "v1"), f.team("t2", "c2", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    comp = f.competition("comp", ["t1", "t2"], preferred_weekday="sunday", weights={"preferred_weekday": 4.0})
    world = f.world(clubs, [v], [comp])
    season = f.season(competitions=["comp"])

    sunday = date(2026, 6, 7)
    assert sunday.weekday() == 6
    on_day = [f.match("comp", "t1", "t2", sunday, "v1")]
    result = evaluate(on_day, [PreferredWeekday(competitions=[comp])], _ctx(world, season))
    assert result.result("preferred_weekday").total == 4.0

    off_day = [f.match("comp", "t1", "t2", sunday.replace(day=8), "v1")]  # Monday
    result = evaluate(off_day, [PreferredWeekday(competitions=[comp])], _ctx(world, season))
    assert result.result("preferred_weekday").total == 0.0


# -- consecutive_home_days ---------------------------------------------------


def test_consecutive_home_days_rewards_adjacent_home_dates_either_order():
    world, season, comp_m, comp_w = _dual_club_world()
    constraint = ConsecutiveHomeDays(competitions=[comp_m, comp_w])

    women_then_men = [
        f.match("topp", "dual_w", "opp_w", date(2026, 6, 6), "v2"),
        f.match("elite", "dual_m", "opp_m", date(2026, 6, 7), "v1"),
    ]
    result = evaluate(women_then_men, [constraint], _ctx(world, season))
    assert result.result("consecutive_home_days").count == 1

    men_then_women = [
        f.match("elite", "dual_m", "opp_m", date(2026, 6, 6), "v1"),
        f.match("topp", "dual_w", "opp_w", date(2026, 6, 7), "v2"),
    ]
    result = evaluate(men_then_women, [constraint], _ctx(world, season))
    assert result.result("consecutive_home_days").count == 1


def test_consecutive_home_days_does_not_fire_two_days_apart():
    world, season, comp_m, comp_w = _dual_club_world()
    constraint = ConsecutiveHomeDays(competitions=[comp_m, comp_w])
    matches = [
        f.match("topp", "dual_w", "opp_w", date(2026, 6, 5), "v2"),
        f.match("elite", "dual_m", "opp_m", date(2026, 6, 7), "v1"),
    ]
    result = evaluate(matches, [constraint], _ctx(world, season))
    assert result.result("consecutive_home_days").count == 0


# -- consecutive_away_days ---------------------------------------------------


def test_consecutive_away_days_rewards_within_travel_range_and_not_beyond():
    world, season, comp_m, comp_w = _dual_club_world()
    constraint = ConsecutiveAwayDays(competitions=[comp_m, comp_w])
    matches = [
        f.match("elite", "opp_m", "dual_m", date(2026, 6, 6), "v1"),
        f.match("topp", "opp_w", "dual_w", date(2026, 6, 7), "v2"),
    ]

    close_ctx = _ctx(world, season, travel=_FixedTravel(2.0))
    close_result = evaluate(matches, [constraint], close_ctx)
    assert close_result.result("consecutive_away_days").total > 0

    far_ctx = _ctx(world, season, travel=_FixedTravel(20.0))
    far_result = evaluate(matches, [constraint], far_ctx)
    assert far_result.result("consecutive_away_days").total == 0


def test_consecutive_away_days_zero_hours_scores_near_full_weight():
    world, season, comp_m, comp_w = _dual_club_world()
    constraint = ConsecutiveAwayDays(competitions=[comp_m, comp_w])
    matches = [
        f.match("elite", "opp_m", "dual_m", date(2026, 6, 6), "v1"),
        f.match("topp", "opp_w", "dual_w", date(2026, 6, 7), "v2"),
    ]
    ctx = _ctx(world, season, travel=_FixedTravel(0.0))
    result = evaluate(matches, [constraint], ctx)
    assert result.result("consecutive_away_days").total == 15.0  # full weight, no falloff


# -- home_away_breaks ---------------------------------------------------


def test_home_away_breaks_fires_on_a_run_of_three_but_not_two():
    v = f.venue("v1")
    teams = [f.team(f"t{i}", f"c{i}", "v1") for i in range(4)]
    clubs = [f.club(f"c{i}", [teams[i]]) for i in range(4)]
    comp = f.competition("comp", [t.id for t in teams], weights={"home_away_breaks": 8.0})
    world = f.world(clubs, [v], [comp])
    season = f.season(competitions=["comp"])
    constraint = HomeAwayBreaks(competitions=[comp])

    two_in_a_row = [
        f.match("comp", "t0", "t1", date(2026, 6, 1), "v1", round_index=0),
        f.match("comp", "t0", "t2", date(2026, 6, 5), "v1", round_index=1),
        f.match("comp", "t3", "t0", date(2026, 6, 9), "v1", round_index=2),
    ]
    result = evaluate(two_in_a_row, [constraint], _ctx(world, season))
    assert result.result("home_away_breaks").total == 0.0

    three_in_a_row = [
        f.match("comp", "t0", "t1", date(2026, 6, 1), "v1", round_index=0),
        f.match("comp", "t0", "t2", date(2026, 6, 5), "v1", round_index=1),
        f.match("comp", "t0", "t3", date(2026, 6, 9), "v1", round_index=2),
    ]
    result = evaluate(three_in_a_row, [constraint], _ctx(world, season))
    assert result.result("home_away_breaks").total == -8.0


# -- home_away_balance -----------------------------------------------------


def test_home_away_balance_penalises_a_lopsided_half():
    v = f.venue("v1")
    teams = [f.team(f"t{i}", f"c{i}", "v1") for i in range(6)]
    clubs = [f.club(f"c{i}", [teams[i]]) for i in range(6)]
    comp = f.competition("comp", [t.id for t in teams], weights={"home_away_balance": 6.0})
    world = f.world(clubs, [v], [comp])
    season = f.season(competitions=["comp"])
    constraint = HomeAwayBalance(competitions=[comp])

    # t0 plays all 5 opponents at home first, then all 5 away (a double
    # league's naive leg order, unbalanced within each half). First half is
    # 5 matches, all home -> drift 5, over the tolerance of 2 by 3.
    lopsided = [
        f.match("comp", "t0", "t1", date(2026, 6, 1), "v1", round_index=0),
        f.match("comp", "t0", "t2", date(2026, 6, 5), "v1", round_index=1),
        f.match("comp", "t0", "t3", date(2026, 6, 9), "v1", round_index=2),
        f.match("comp", "t0", "t4", date(2026, 6, 13), "v1", round_index=3),
        f.match("comp", "t0", "t5", date(2026, 6, 17), "v1", round_index=4),
        f.match("comp", "t1", "t0", date(2026, 6, 21), "v1", round_index=5),
        f.match("comp", "t2", "t0", date(2026, 6, 25), "v1", round_index=6),
        f.match("comp", "t3", "t0", date(2026, 6, 29), "v1", round_index=7),
        f.match("comp", "t4", "t0", date(2026, 7, 3), "v1", round_index=8),
        f.match("comp", "t5", "t0", date(2026, 7, 7), "v1", round_index=9),
    ]
    result = evaluate(lopsided, [constraint], _ctx(world, season))
    # Both halves are equally lopsided (5 home then 5 away), so both are
    # charged: 6.0 * (5-2) each, -36 total.
    assert result.result("home_away_balance").total == -36.0


# -- rest_comfort ---------------------------------------------------------


def test_rest_comfort_penalises_gaps_between_minimum_and_comfortable():
    v = f.venue("v1")
    t1, t2 = f.team("t1", "c1", "v1"), f.team("t2", "c2", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    comp = f.competition(
        "comp", ["t1", "t2"], min_rest_days=3, comfortable_rest_days=6, weights={"rest_comfort": 3.0}
    )
    world = f.world(clubs, [v], [comp])
    season = f.season(competitions=["comp"])
    constraint = RestComfort(competitions=[comp])

    # Exactly at the minimum (3 days): comfortable shortfall of 3.
    tight = [
        f.match("comp", "t1", "t2", date(2026, 6, 1), "v1", round_index=0),
        f.match("comp", "t2", "t1", date(2026, 6, 4), "v1", round_index=1),
    ]
    result = evaluate(tight, [constraint], _ctx(world, season))
    # Charged once per team whose own sequence has the gap — both teams see
    # the same shortfall here, so -9 (3.0 * (6-3)) each, -18 total.
    assert result.result("rest_comfort").total == -18.0

    # At the comfortable target: no penalty.
    comfortable = [
        f.match("comp", "t1", "t2", date(2026, 6, 1), "v1", round_index=0),
        f.match("comp", "t2", "t1", date(2026, 6, 7), "v1", round_index=1),
    ]
    result = evaluate(comfortable, [constraint], _ctx(world, season))
    assert result.result("rest_comfort").total == 0.0


# -- soft_venue_preference -------------------------------------------------


def test_soft_venue_preference_penalises_discouraged_dates_only():
    from terminliste.model.schema import DatedNote

    v = f.venue("v1")
    t1, t2 = f.team("t1", "c1", "v1"), f.team("t2", "c2", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    comp = f.competition("comp", ["t1", "t2"], weights={"soft_venue_preference": 5.0})
    world = f.world(clubs, [v], [comp])
    discouraged_day = date(2026, 7, 4)
    season = f.season(
        competitions=["comp"], discouraged_dates=[DatedNote(date=discouraged_day, reason="holiday")]
    )
    constraint = SoftVenuePreference(competitions=[comp])

    on_discouraged = [f.match("comp", "t1", "t2", discouraged_day, "v1")]
    result = evaluate(on_discouraged, [constraint], _ctx(world, season))
    assert result.result("soft_venue_preference").total == -5.0

    elsewhere = [f.match("comp", "t1", "t2", discouraged_day.replace(day=5), "v1")]
    result = evaluate(elsewhere, [constraint], _ctx(world, season))
    assert result.result("soft_venue_preference").total == 0.0
