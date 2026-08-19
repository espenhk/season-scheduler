"""Hard constraints, tested at their boundary rather than the happy path.

Each rule gets one test that it fires exactly at the edge (not one day inside
it) and one test that a clean schedule scores it at zero — a constraint that
always fires quietly, even on a perfectly good schedule, is the easiest kind
of bug to ship undetected.
"""

from __future__ import annotations

from datetime import date

import factories as f
from terminliste.scoring.base import EvalContext, evaluate
from terminliste.rounds.european_schedule import EuropeanCommitmentDate
from terminliste.scoring.hard import (
    BlackoutDates,
    ClubHomeClash,
    CupRoundConflict,
    EuropeanCommitmentConflict,
    FinalRoundSameSlot,
    FixedDateRequirement,
    FullRoundOnDate,
    LegOrdering,
    MinRestDays,
    OneMatchPerTeamPerDay,
    VenueDoubleBooking,
)


class _StubTravel:
    def hours(self, a, b):
        return 1.0


def _ctx(world, season):
    return EvalContext(world=world, season=season, travel=_StubTravel(), detail=True)


# -- min_rest_days -----------------------------------------------------------


def test_min_rest_fires_at_two_rest_days_but_not_three():
    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v2")
    v2 = f.venue("v2")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    comp = f.competition("comp", ["t1", "t2"], min_rest_days=3)
    world = f.world(clubs, [v, v2], [comp])
    season = f.season(competitions=["comp"])

    constraint = MinRestDays(competitions=[comp])

    # Exactly the minimum: 3 full rest days (2-4 Jun) apart -> no violation.
    ok_matches = [
        f.match("comp", "t1", "t2", date(2026, 6, 1), "v1", round_index=0),
        f.match("comp", "t2", "t1", date(2026, 6, 5), "v2", round_index=1),
    ]
    result = evaluate(ok_matches, [constraint], _ctx(world, season))
    assert result.hard_violations == 0

    # One day short: 2 full rest days apart -> violation.
    bad_matches = [
        f.match("comp", "t1", "t2", date(2026, 6, 1), "v1", round_index=0),
        f.match("comp", "t2", "t1", date(2026, 6, 4), "v2", round_index=1),
    ]
    result = evaluate(bad_matches, [constraint], _ctx(world, season))
    # Counted once per team whose own sequence has the short gap — both teams'
    # sequences see it here, since it's the same two matches for both.
    assert result.hard_violations == 2


def test_min_rest_fires_on_same_day_double_booking():
    v = f.venue("v1")
    v2 = f.venue("v2")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v2")
    t3 = f.team("t3", "c3", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2]), f.club("c3", [t3])]
    comp = f.competition("comp", ["t1", "t2", "t3"], min_rest_days=3)
    world = f.world(clubs, [v, v2], [comp])
    season = f.season(competitions=["comp"])

    matches = [
        f.match("comp", "t1", "t2", date(2026, 6, 1), "v1", round_index=0),
        f.match("comp", "t1", "t3", date(2026, 6, 1), "v1", round_index=1),
    ]
    result = evaluate(matches, [MinRestDays(competitions=[comp])], _ctx(world, season))
    assert result.hard_violations >= 1


# -- blackout_dates ------------------------------------------------------


def test_blackout_dates_fires_only_on_the_blacked_out_date():
    from terminliste.model.schema import DatedNote

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    comp = f.competition("comp", ["t1", "t2"])
    world = f.world(clubs, [v], [comp])
    blackout_day = date(2026, 5, 17)
    season = f.season(
        competitions=["comp"], global_blackouts=[DatedNote(date=blackout_day, reason="national day")]
    )

    on_blackout = [f.match("comp", "t1", "t2", blackout_day, "v1")]
    result = evaluate(on_blackout, [BlackoutDates()], _ctx(world, season))
    assert result.hard_violations == 1

    day_after = [f.match("comp", "t1", "t2", blackout_day.replace(day=18), "v1")]
    result = evaluate(day_after, [BlackoutDates()], _ctx(world, season))
    assert result.hard_violations == 0


def test_blackout_dates_fires_anywhere_inside_a_global_blackout_range():
    from terminliste.model.schema import GlobalBlackoutRange

    v = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    comp = f.competition("comp", ["t1", "t2"])
    world = f.world(clubs, [v], [comp])
    season = f.season(
        competitions=["comp"],
        global_blackout_ranges=[
            GlobalBlackoutRange(
                start=date(2026, 6, 1), end=date(2026, 6, 14), reason="international break"
            )
        ],
    )

    mid_range = [f.match("comp", "t1", "t2", date(2026, 6, 7), "v1")]
    result = evaluate(mid_range, [BlackoutDates()], _ctx(world, season))
    assert result.hard_violations == 1

    outside_range = [f.match("comp", "t1", "t2", date(2026, 6, 15), "v1")]
    result = evaluate(outside_range, [BlackoutDates()], _ctx(world, season))
    assert result.hard_violations == 0


def test_blackout_dates_fires_only_at_the_venue_a_blackout_range_covers():
    from terminliste.model.schema import VenueBlackoutRange

    v1 = f.venue("v1")
    v2 = f.venue("v2")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v2")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    comp = f.competition("comp", ["t1", "t2"])
    world = f.world(clubs, [v1, v2], [comp])
    season = f.season(
        competitions=["comp"],
        venue_blackout_ranges=[
            VenueBlackoutRange(
                venue="v1", start=date(2026, 6, 1), end=date(2026, 6, 3), reason="ground works"
            )
        ],
    )

    at_blacked_out_venue = [f.match("comp", "t1", "t2", date(2026, 6, 2), "v1")]
    result = evaluate(at_blacked_out_venue, [BlackoutDates()], _ctx(world, season))
    assert result.hard_violations == 1

    same_day_other_venue = [f.match("comp", "t2", "t1", date(2026, 6, 2), "v2")]
    result = evaluate(same_day_other_venue, [BlackoutDates()], _ctx(world, season))
    assert result.hard_violations == 0


# -- venue_double_booking -------------------------------------------------


def test_venue_double_booking_fires_when_two_matches_share_a_venue_and_date():
    v1 = f.venue("v1")
    teams = [f.team(f"t{i}", f"c{i}", "v1") for i in range(4)]
    clubs = [f.club(f"c{i}", [teams[i]]) for i in range(4)]
    comp = f.competition("comp", [t.id for t in teams])
    world = f.world(clubs, [v1], [comp])
    season = f.season(competitions=["comp"])

    same_day = [
        f.match("comp", "t0", "t1", date(2026, 6, 1), "v1", round_index=0),
        f.match("comp", "t2", "t3", date(2026, 6, 1), "v1", round_index=0),
    ]
    result = evaluate(same_day, [VenueDoubleBooking()], _ctx(world, season))
    assert result.hard_violations == 1

    different_days = [
        f.match("comp", "t0", "t1", date(2026, 6, 1), "v1", round_index=0),
        f.match("comp", "t2", "t3", date(2026, 6, 2), "v1", round_index=1),
    ]
    result = evaluate(different_days, [VenueDoubleBooking()], _ctx(world, season))
    assert result.hard_violations == 0


# -- club_home_clash -------------------------------------------------------


def test_club_home_clash_fires_when_a_clubs_two_teams_are_both_home_at_different_venues():
    v1, v2 = f.venue("v1"), f.venue("v2")
    men = f.team("dual_m", "dual", "v1", gender="men")
    women = f.team("dual_w", "dual", "v2", gender="women")
    other_m = f.team("other_m", "other", "v1", gender="men")
    other_w = f.team("other_w", "other2", "v2", gender="women")
    clubs = [
        f.club("dual", [men, women]),
        f.club("other", [other_m]),
        f.club("other2", [other_w]),
    ]
    comp_m = f.competition("elite", ["dual_m", "other_m"], gender="men")
    comp_w = f.competition("topp", ["dual_w", "other_w"], gender="women")
    world = f.world(clubs, [v1, v2], [comp_m, comp_w])
    season = f.season(competitions=["elite", "topp"])

    both_home = [
        f.match("elite", "dual_m", "other_m", date(2026, 6, 1), "v1"),
        f.match("topp", "dual_w", "other_w", date(2026, 6, 1), "v2"),
    ]
    result = evaluate(both_home, [ClubHomeClash()], _ctx(world, season))
    assert result.hard_violations == 1

    # One away instead -> no clash.
    one_away = [
        f.match("elite", "dual_m", "other_m", date(2026, 6, 1), "v1"),
        f.match("topp", "other_w", "dual_w", date(2026, 6, 1), "v2"),
    ]
    result = evaluate(one_away, [ClubHomeClash()], _ctx(world, season))
    assert result.hard_violations == 0


def test_club_home_clash_does_not_double_count_a_shared_venue_case():
    """Same club, same venue, same day is venue_double_booking's job — this
    rule should stay silent so the report doesn't charge the same problem
    twice under two different names."""
    v1 = f.venue("v1")
    t1 = f.team("t1", "dual", "v1", gender="men")
    t2 = f.team("t2", "dual", "v1", gender="women")
    clubs = [f.club("dual", [t1, t2])]
    comp_m = f.competition("elite", ["t1"], gender="men")
    comp_w = f.competition("topp", ["t2"], gender="women")
    world = f.world(clubs, [v1], [comp_m, comp_w])
    season = f.season(competitions=["elite", "topp"])

    matches = [
        f.match("elite", "t1", "t1", date(2026, 6, 1), "v1"),  # nonsense pairing, venue only matters
    ]
    result = evaluate(matches, [ClubHomeClash()], _ctx(world, season))
    assert result.hard_violations == 0


# -- leg_ordering -----------------------------------------------------------


def test_leg_ordering_fires_when_a_second_leg_match_starts_too_early():
    v1 = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    comp = f.competition("comp", ["t1", "t2"], rounds_per_pairing=2)
    world = f.world(clubs, [v1], [comp])
    season = f.season(competitions=["comp"])

    bad = [
        f.match("comp", "t1", "t2", date(2026, 6, 10), "v1", leg=1, round_index=0),
        f.match("comp", "t2", "t1", date(2026, 6, 5), "v1", leg=2, round_index=1),
    ]
    result = evaluate(bad, [LegOrdering()], _ctx(world, season))
    assert result.hard_violations == 1

    good = [
        f.match("comp", "t1", "t2", date(2026, 6, 5), "v1", leg=1, round_index=0),
        f.match("comp", "t2", "t1", date(2026, 6, 10), "v1", leg=2, round_index=1),
    ]
    result = evaluate(good, [LegOrdering()], _ctx(world, season))
    assert result.hard_violations == 0


def test_leg_ordering_fires_on_a_same_day_tie():
    """Date-only precision: a leg-2 match landing on the same day the last
    leg-1 match was played is not 'first meeting precedes second meeting' —
    it should count as a violation, not be waved through as a tie."""
    v1 = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v1")
    t3 = f.team("t3", "c3", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2]), f.club("c3", [t3])]
    comp = f.competition("comp", ["t1", "t2", "t3"], rounds_per_pairing=2)
    world = f.world(clubs, [v1], [comp])
    season = f.season(competitions=["comp"])

    same_day = [
        f.match("comp", "t1", "t2", date(2026, 6, 10), "v1", leg=1, round_index=0),
        f.match("comp", "t3", "t1", date(2026, 6, 10), "v1", leg=2, round_index=1),
    ]
    result = evaluate(same_day, [LegOrdering()], _ctx(world, season))
    assert result.hard_violations == 1

    one_day_later = [
        f.match("comp", "t1", "t2", date(2026, 6, 10), "v1", leg=1, round_index=0),
        f.match("comp", "t3", "t1", date(2026, 6, 11), "v1", leg=2, round_index=1),
    ]
    result = evaluate(one_day_later, [LegOrdering()], _ctx(world, season))
    assert result.hard_violations == 0


# -- fixed_requirement --------------------------------------------------


def test_fixed_requirement_fires_when_the_named_team_is_not_home():
    from terminliste.model.schema import FixedRequirement

    v1, v2 = f.venue("v1"), f.venue("v2")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v2")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    comp = f.competition("comp", ["t1", "t2"])
    world = f.world(clubs, [v1, v2], [comp])
    requirement = FixedRequirement(
        id="req1", date=date(2026, 5, 16), home_team="t1", competition="comp", hard=True
    )
    season = f.season(competitions=["comp"], fixed_requirements=[requirement])
    constraint = FixedDateRequirement(requirements=[requirement])

    satisfied = [f.match("comp", "t1", "t2", date(2026, 5, 16), "v1")]
    assert evaluate(satisfied, [constraint], _ctx(world, season)).hard_violations == 0

    wrong_team_home = [f.match("comp", "t2", "t1", date(2026, 5, 16), "v2")]
    assert evaluate(wrong_team_home, [constraint], _ctx(world, season)).hard_violations == 1

    no_match_at_all = [f.match("comp", "t1", "t2", date(2026, 5, 20), "v1")]
    assert evaluate(no_match_at_all, [constraint], _ctx(world, season)).hard_violations == 1


# -- one_match_per_team_per_day ------------------------------------------


def test_one_match_per_team_per_day():
    v1, v2 = f.venue("v1"), f.venue("v2")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v2")
    t3 = f.team("t3", "c3", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2]), f.club("c3", [t3])]
    comp = f.competition("comp", ["t1", "t2", "t3"])
    world = f.world(clubs, [v1, v2], [comp])
    season = f.season(competitions=["comp"])

    matches = [
        f.match("comp", "t1", "t2", date(2026, 6, 1), "v1", round_index=0),
        f.match("comp", "t3", "t1", date(2026, 6, 1), "v1", round_index=1),
    ]
    result = evaluate(matches, [OneMatchPerTeamPerDay()], _ctx(world, season))
    assert result.hard_violations == 1


# -- cup_round_conflict -------------------------------------------------


def _cup_world():
    v1 = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    world = f.world(clubs, [v1], [])
    season = f.season(competitions=[], cup_competitions=["cup"])
    schedule = f.cup_schedule(
        "cup",
        min_rest_days=3,
        rounds=[
            f.cup_placement("r1", {"t1": date(2026, 8, 22), "t2": date(2026, 8, 22)}, "Round 1")
        ],
    )
    return world, season, schedule


def test_cup_round_conflict_fires_at_two_rest_days_but_not_three():
    world, season, schedule = _cup_world()
    constraint = CupRoundConflict(cup_schedules=[schedule])

    # 3 full rest days from the 22 Aug cup round -> no violation.
    ok = [f.match("league", "t1", "t2", date(2026, 8, 26), "v1")]
    assert evaluate(ok, [constraint], _ctx(world, season)).hard_violations == 0

    # 2 full rest days -> violation.
    bad = [f.match("league", "t1", "t2", date(2026, 8, 25), "v1")]
    result = evaluate(bad, [constraint], _ctx(world, season))
    # Both teams are entered in the cup, so both sides of the league match see
    # the same shortfall.
    assert result.hard_violations == 2


def test_cup_round_conflict_is_silent_for_a_team_not_entered_in_the_cup():
    v1 = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    t3 = f.team("t3", "c3", "v1")
    clubs = [f.club("c1", [t1]), f.club("c3", [t3])]
    world = f.world(clubs, [v1], [])
    season = f.season(competitions=[], cup_competitions=["cup"])
    schedule = f.cup_schedule(
        "cup", min_rest_days=3, rounds=[f.cup_placement("r1", {"t1": date(2026, 8, 22)}, "Round 1")]
    )

    matches = [f.match("league", "t3", "t1", date(2026, 8, 23), "v1")]
    result = evaluate(matches, [CupRoundConflict(cup_schedules=[schedule])], _ctx(world, season))
    # t1 (entered in the cup) is one day from the round — a violation. t3 is
    # not entered, so it contributes nothing of its own.
    assert result.hard_violations == 1


def test_cup_round_conflict_uses_each_teams_own_resolved_date():
    """Two teams in the same round can resolve to different days (the round's
    matches are spread across its window) — the constraint must key off each
    team's own date, not a single shared one."""
    v1 = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    world = f.world(clubs, [v1], [])
    season = f.season(competitions=[], cup_competitions=["cup"])
    schedule = f.cup_schedule(
        "cup",
        min_rest_days=3,
        rounds=[
            f.cup_placement("r1", {"t1": date(2026, 8, 22), "t2": date(2026, 8, 25)}, "Round 1")
        ],
    )
    constraint = CupRoundConflict(cup_schedules=[schedule])

    # t1's cup date is 22 Aug: a league match on 21 Aug is 1 day away (violation).
    # t2's cup date is 25 Aug, 4 days clear of that same match, so only t1's
    # side fires.
    matches = [f.match("league", "t1", "t2", date(2026, 8, 21), "v1")]
    result = evaluate(matches, [constraint], _ctx(world, season))
    assert result.hard_violations == 1


# -- european_commitment_conflict ---------------------------------------


def _european_world():
    v1 = f.venue("v1")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    world = f.world(clubs, [v1], [])
    season = f.season(competitions=[], european_competitions=["euro"])
    commitment = EuropeanCommitmentDate(
        team_id="t1",
        date=date(2026, 8, 4),
        min_rest_days=3,
        label="cl: q3 (first leg)",
        competition_id="cl",
    )
    return world, season, commitment


def test_european_commitment_conflict_fires_on_the_commitment_date_itself():
    world, season, commitment = _european_world()
    constraint = EuropeanCommitmentConflict(commitments_by_team={"t1": [commitment]})

    on_date = [f.match("league", "t1", "t2", date(2026, 8, 4), "v1")]
    result = evaluate(on_date, [constraint], _ctx(world, season))
    assert result.hard_violations == 1


def test_european_commitment_conflict_fires_within_min_rest_days_but_not_beyond():
    world, season, commitment = _european_world()
    constraint = EuropeanCommitmentConflict(commitments_by_team={"t1": [commitment]})

    # 1 full rest day before (4 Aug): a violation, short of the 3 required.
    too_close_before = [f.match("league", "t1", "t2", date(2026, 8, 2), "v1")]
    assert evaluate(too_close_before, [constraint], _ctx(world, season)).hard_violations == 1

    # Exactly 3 full rest days before: clear — this is the Thu-Sun-Thu case (a
    # European leg on Tuesday 4 Aug, a league match the preceding Friday 31
    # July has Sat/Sun/Mon — three full days — between them).
    far_enough_before = [f.match("league", "t1", "t2", date(2026, 7, 31), "v1")]
    assert evaluate(far_enough_before, [constraint], _ctx(world, season)).hard_violations == 0

    # 1 full rest day after: a violation.
    too_close_after = [f.match("league", "t1", "t2", date(2026, 8, 6), "v1")]
    assert evaluate(too_close_after, [constraint], _ctx(world, season)).hard_violations == 1

    # Exactly 3 full rest days after: clear.
    far_enough_after = [f.match("league", "t1", "t2", date(2026, 8, 8), "v1")]
    assert evaluate(far_enough_after, [constraint], _ctx(world, season)).hard_violations == 0


def test_european_commitment_conflict_is_silent_for_a_team_with_no_commitments():
    world, season, commitment = _european_world()
    constraint = EuropeanCommitmentConflict(commitments_by_team={"t1": [commitment]})

    matches = [f.match("league", "t2", "t1", date(2026, 8, 5), "v1")]
    result = evaluate(matches, [constraint], _ctx(world, season))
    # t1's commitment fires (it plays the match, 1 day from its 4 Aug
    # commitment); t2 has no commitments of its own, so it contributes
    # nothing beyond that.
    assert result.hard_violations == 1


def test_european_commitment_conflict_ignores_an_uncertain_commitment():
    """A commitment reachable only via one of several mutually-exclusive
    cascade branches (`certain=False`, issue #93) isn't a guaranteed
    fixture, so it must not gate a hard exclusion —
    `EuropeanCommitmentSoftConflict` (scoring/soft.py) is where it shows up
    instead."""
    world, season, commitment = _european_world()
    uncertain = EuropeanCommitmentDate(
        team_id="t1",
        date=date(2026, 8, 4),
        min_rest_days=3,
        label="uecl: playoff (first leg)",
        competition_id="uecl",
        certain=False,
    )
    constraint = EuropeanCommitmentConflict(commitments_by_team={"t1": [uncertain]})

    on_date = [f.match("league", "t1", "t2", date(2026, 8, 4), "v1")]
    result = evaluate(on_date, [constraint], _ctx(world, season))
    assert result.hard_violations == 0


# -- clean schedule: every hard constraint reports zero ----------------------


# -- final_round_same_slot -----------------------------------------------


def _final_round_world():
    v1, v2, v3 = f.venue("v1"), f.venue("v2"), f.venue("v3")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v2")
    t3 = f.team("t3", "c3", "v3")
    t4 = f.team("t4", "c4", "v1")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2]), f.club("c3", [t3]), f.club("c4", [t4])]
    comp = f.competition("comp", ["t1", "t2", "t3", "t4"], rounds_per_pairing=2)
    world = f.world(clubs, [v1, v2, v3], [comp])
    season = f.season(competitions=["comp"])
    return world, season, comp


def test_final_round_same_slot_is_silent_when_the_round_shares_date_and_kickoff():
    world, season, comp = _final_round_world()
    constraint = FinalRoundSameSlot(competitions=[comp])
    final_round = comp.rounds - 1
    matches = [
        f.match("comp", "t1", "t2", date(2026, 12, 6), "v1", leg=2, round_index=final_round, kickoff_time="18:00"),
        f.match("comp", "t3", "t4", date(2026, 12, 6), "v3", leg=2, round_index=final_round, kickoff_time="18:00"),
    ]
    assert evaluate(matches, [constraint], _ctx(world, season)).hard_violations == 0


def test_final_round_same_slot_fires_on_a_split_date():
    world, season, comp = _final_round_world()
    constraint = FinalRoundSameSlot(competitions=[comp])

    final_round = comp.rounds - 1
    split = [
        f.match("comp", "t1", "t2", date(2026, 12, 6), "v1", leg=2, round_index=final_round, kickoff_time="18:00"),
        f.match("comp", "t3", "t4", date(2026, 12, 7), "v3", leg=2, round_index=final_round, kickoff_time="18:00"),
    ]
    assert evaluate(split, [constraint], _ctx(world, season)).hard_violations == 1


def test_final_round_same_slot_fires_on_a_split_kickoff_time():
    world, season, comp = _final_round_world()
    constraint = FinalRoundSameSlot(competitions=[comp])

    final_round = comp.rounds - 1
    split_kickoff = [
        f.match("comp", "t1", "t2", date(2026, 12, 6), "v1", leg=2, round_index=final_round, kickoff_time="18:00"),
        f.match("comp", "t3", "t4", date(2026, 12, 6), "v3", leg=2, round_index=final_round, kickoff_time="20:00"),
    ]
    assert evaluate(split_kickoff, [constraint], _ctx(world, season)).hard_violations == 1


# -- full_round_on_date ---------------------------------------------------


def test_full_round_on_date_fires_when_a_team_has_no_match():
    from terminliste.model.schema import FullRoundRequirement

    v1, v2, v3, v4 = f.venue("v1"), f.venue("v2"), f.venue("v3"), f.venue("v4")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v2")
    t3 = f.team("t3", "c3", "v3")
    t4 = f.team("t4", "c4", "v4")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2]), f.club("c3", [t3]), f.club("c4", [t4])]
    comp = f.competition("comp", ["t1", "t2", "t3", "t4"])
    world = f.world(clubs, [v1, v2, v3, v4], [comp])
    requirement = FullRoundRequirement(id="req1", date=date(2026, 5, 16), competition="comp")
    season = f.season(competitions=["comp"], full_round_requirements=[requirement])
    constraint = FullRoundOnDate(requirements=[requirement])

    # t3 and t4 don't play at all on the required date.
    only_half_play = [f.match("comp", "t1", "t2", date(2026, 5, 16), "v1")]
    result = evaluate(only_half_play, [constraint], _ctx(world, season))
    assert result.hard_violations == 2

    everyone_plays = [
        f.match("comp", "t1", "t2", date(2026, 5, 16), "v1", round_index=0),
        f.match("comp", "t3", "t4", date(2026, 5, 16), "v3", round_index=0),
    ]
    result = evaluate(everyone_plays, [constraint], _ctx(world, season))
    assert result.hard_violations == 0


def test_all_hard_constraints_are_silent_on_a_clean_schedule():
    v1, v2 = f.venue("v1"), f.venue("v2")
    t1 = f.team("t1", "c1", "v1")
    t2 = f.team("t2", "c2", "v2")
    clubs = [f.club("c1", [t1]), f.club("c2", [t2])]
    comp = f.competition("comp", ["t1", "t2"], min_rest_days=3, rounds_per_pairing=2)
    world = f.world(clubs, [v1, v2], [comp])
    season = f.season(competitions=["comp"])

    clean = [
        f.match("comp", "t1", "t2", date(2026, 6, 1), "v1", leg=1, round_index=0),
        f.match("comp", "t2", "t1", date(2026, 6, 8), "v2", leg=2, round_index=1),
    ]
    constraints = [
        OneMatchPerTeamPerDay(),
        MinRestDays(competitions=[comp]),
        BlackoutDates(),
        VenueDoubleBooking(),
        ClubHomeClash(),
        LegOrdering(),
    ]
    result = evaluate(clean, constraints, _ctx(world, season))
    assert result.hard_violations == 0
    for constraint_result in result.hard_results():
        assert constraint_result.count == 0, constraint_result.constraint_id
