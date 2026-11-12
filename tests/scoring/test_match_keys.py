"""Every event that is about a fixture names it in `match_keys`, so the report
can badge it (#29). Gap and run rules pin the later / extending match."""

from __future__ import annotations

from datetime import date

import factories as f
from test_soft_constraints import _ctx, _dual_club_world, _FixedTravel, _tv_spread_world

from terminliste.scoring.base import evaluate
from terminliste.scoring.hard import ClubHomeClash, MinRestDays, OneMatchPerTeamPerDay
from terminliste.scoring.soft import (
    ConsecutiveAwayDays,
    ConsecutiveHomeDays,
    HomeAwayBalance,
    HomeAwayBreaks,
    PreferredWeekday,
    RestComfort,
    RivalryFixtureOnDate,
    TvTimeSpread,
)


def _events(score, rule_id):
    return score.result(rule_id).events


def _small_league(n_teams=4, **competition_kwargs):
    v = f.venue("v1")
    teams = [f.team(f"t{i}", f"c{i}", "v1") for i in range(n_teams)]
    clubs = [f.club(f"c{i}", [teams[i]]) for i in range(n_teams)]
    comp = f.competition("comp", [t.id for t in teams], **competition_kwargs)
    return f.world(clubs, [v], [comp]), f.season(competitions=["comp"]), comp


def test_preferred_weekday_pins_only_the_rewarded_matches():
    world, season, comp = _small_league(preferred_weekday="sunday", weights={"preferred_weekday": 4.0})
    on_day = f.match("comp", "t0", "t1", date(2026, 6, 7), "v1")
    off_day = f.match("comp", "t2", "t3", date(2026, 6, 8), "v1")

    score = evaluate([on_day, off_day], [PreferredWeekday(competitions=[comp])], _ctx(world, season))

    (event,) = _events(score, "preferred_weekday")
    assert event.match_keys == (on_day.key,)
    assert event.delta == 4.0


def test_home_and_away_pairs_pin_both_matches_of_the_pair():
    world, season, comp_m, comp_w = _dual_club_world()
    home = [
        f.match("elite", "dual_m", "opp_m", date(2026, 6, 6), "v1"),
        f.match("topp", "dual_w", "opp_w", date(2026, 6, 7), "v2"),
    ]
    score = evaluate(home, [ConsecutiveHomeDays(competitions=[comp_m, comp_w])], _ctx(world, season))
    (event,) = _events(score, "consecutive_home_days")
    assert set(event.match_keys) == {m.key for m in home}

    away = [
        f.match("elite", "opp_m", "dual_m", date(2026, 6, 6), "v1"),
        f.match("topp", "opp_w", "dual_w", date(2026, 6, 7), "v2"),
    ]
    score = evaluate(
        away,
        [ConsecutiveAwayDays(competitions=[comp_m, comp_w])],
        _ctx(world, season, travel=_FixedTravel(2.0)),
    )
    (event,) = _events(score, "consecutive_away_days")
    assert set(event.match_keys) == {m.key for m in away}


def test_home_away_breaks_pins_the_matches_that_extend_the_run():
    world, season, comp = _small_league(5, weights={"home_away_breaks": 8.0})
    run = [
        f.match("comp", "t0", f"t{i + 1}", date(2026, 6, 1 + 4 * i), "v1", round_index=i)
        for i in range(4)
    ]

    score = evaluate(run, [HomeAwayBreaks(competitions=[comp])], _ctx(world, season))

    (event,) = _events(score, "home_away_breaks")
    assert event.delta == -16.0
    assert event.match_keys == (run[2].key, run[3].key)


def test_rest_comfort_pins_the_later_match_of_the_gap():
    world, season, comp = _small_league(
        2, min_rest_days=3, comfortable_rest_days=6, weights={"rest_comfort": 3.0}
    )
    earlier = f.match("comp", "t0", "t1", date(2026, 6, 1), "v1", round_index=0)
    later = f.match("comp", "t1", "t0", date(2026, 6, 5), "v1", round_index=1)

    score = evaluate([earlier, later], [RestComfort(competitions=[comp])], _ctx(world, season))

    assert {e.match_keys for e in _events(score, "rest_comfort")} == {(later.key,)}


def test_home_away_balance_stays_unpinned_because_no_single_match_is_to_blame():
    world, season, comp = _small_league(8, weights={"home_away_balance": 6.0})
    matches = [
        f.match("comp", "t0", f"t{i + 1}", date(2026, 6, 1 + 3 * i), "v1", round_index=i)
        for i in range(7)
    ]

    score = evaluate(matches, [HomeAwayBalance(competitions=[comp])], _ctx(world, season))

    events = _events(score, "home_away_balance")
    assert events
    assert all(e.match_keys == () for e in events)


def test_rivalry_fixture_pins_the_match_on_its_date():
    from terminliste.model.schema import RivalryFixture

    world, season, comp = _small_league(2)
    rivalry = RivalryFixture(
        id="derby",
        date=date(2026, 5, 16),
        competition="comp",
        team_even_year_home="t0",
        team_odd_year_home="t1",
        weight=20.0,
    )
    derby = f.match("comp", "t0", "t1", date(2026, 5, 16), "v1")

    score = evaluate(
        [derby], [RivalryFixtureOnDate(fixtures=[rivalry], season_year=2026)], _ctx(world, season)
    )

    (event,) = _events(score, "rivalry_fixture_on_date")
    assert event.match_keys == (derby.key,)


def test_tv_time_spread_pins_the_rewarded_round_matches():
    world, season, comp = _tv_spread_world()
    sunday = date(2026, 6, 7)
    matches = [
        f.match("comp", "t1", "t2", sunday, "v1", kickoff_time="14:30"),
        f.match("comp", "t3", "t4", sunday, "v1", kickoff_time="19:15"),
        f.match("comp", "t5", "t6", sunday, "v1", kickoff_time="17:00"),
        f.match("comp", "t7", "t8", sunday, "v1", kickoff_time="17:00"),
    ]

    score = evaluate(matches, [TvTimeSpread(competitions=[comp])], _ctx(world, season))

    (event,) = _events(score, "tv_time_spread")
    assert set(event.match_keys) == {m.key for m in matches}


# -- hard rules --------------------------------------------------------------


def test_min_rest_days_pins_the_later_match_of_the_gap():
    world, season, comp = _small_league(2, min_rest_days=3)
    earlier = f.match("comp", "t0", "t1", date(2026, 6, 1), "v1", round_index=0)
    later = f.match("comp", "t1", "t0", date(2026, 6, 3), "v1", round_index=1)

    score = evaluate([earlier, later], [MinRestDays(competitions=[comp])], _ctx(world, season))

    assert {e.match_keys for e in _events(score, "min_rest_days")} == {(later.key,)}


def test_one_match_per_team_per_day_pins_that_days_matches():
    world, season, _ = _small_league(3)
    clash = [
        f.match("comp", "t0", "t1", date(2026, 6, 1), "v1", round_index=0),
        f.match("comp", "t2", "t0", date(2026, 6, 1), "v1", round_index=1),
        f.match("comp", "t0", "t1", date(2026, 6, 9), "v1", round_index=2),
    ]

    score = evaluate(clash, [OneMatchPerTeamPerDay()], _ctx(world=world, season=season))

    (event,) = _events(score, "one_match_per_team_per_day")
    assert set(event.match_keys) == {clash[0].key, clash[1].key}


def test_club_home_clash_pins_the_clubs_home_matches_that_day():
    v1, v2 = f.venue("v1"), f.venue("v2")
    men = f.team("dual_m", "dual", "v1", gender="men")
    women = f.team("dual_w", "dual", "v2", gender="women")
    other_m = f.team("other_m", "other", "v1", gender="men")
    other_w = f.team("other_w", "other2", "v2", gender="women")
    clubs = [f.club("dual", [men, women]), f.club("other", [other_m]), f.club("other2", [other_w])]
    comp_m = f.competition("elite", ["dual_m", "other_m"], gender="men")
    comp_w = f.competition("topp", ["dual_w", "other_w"], gender="women")
    world = f.world(clubs, [v1, v2], [comp_m, comp_w])
    season = f.season(competitions=["elite", "topp"])
    both_home = [
        f.match("elite", "dual_m", "other_m", date(2026, 6, 1), "v1"),
        f.match("topp", "dual_w", "other_w", date(2026, 6, 1), "v2"),
    ]

    score = evaluate(both_home, [ClubHomeClash()], _ctx(world, season))

    (event,) = _events(score, "club_home_clash")
    assert set(event.match_keys) == {m.key for m in both_home}
