"""Fallback planning for matches a later cup or UEFA round may displace (#9)."""

from __future__ import annotations

from datetime import date

import factories as f

from terminliste.contingency import ContingencyPlan, ContingencyPlanner, Fallback
from terminliste.model.schema import ContingencyPolicy, ReserveDate, VenueBlackout
from terminliste.scoring.base import EvalContext, ScheduleIndex, evaluate
from terminliste.scoring.hard import ContingencyCollision, ContingencyCoverage, ReserveDateUnused
from terminliste.scoring.soft import ExpectedDisruption

SUN = date(2026, 8, 23)
THU = date(2026, 8, 20)
MON = date(2026, 8, 24)


class _Travel:
    def hours(self, a, b):
        return 1.0


def _world():
    teams = [f.team(f"t{i}", f"c{i}", f"v{i}") for i in range(1, 5)]
    clubs = [f.club(f"c{i}", [t]) for i, t in enumerate(teams, start=1)]
    league = f.competition("league", [t.id for t in teams], min_rest_days=3)
    return f.world(clubs, [f.venue(f"v{i}") for i in range(1, 5)], [league]), league


def _planner(commitments, *, season_args=None, world=None):
    world_, league = world or _world()
    season = f.season(competitions=["league"], **(season_args or {}))
    return ContingencyPlanner(world_, season, [league], f.commitment_set(*commitments)), season, world_


def _maybe(team, day, node, p=0.5, exclusive=frozenset()):
    return f.commitment(
        team, day, hard=False, probability=p, min_rest_days=3,
        node_id=f"{team}/uecl/{node}", exclusive_with=exclusive,
    )


def _analyse(planner, matches):
    return planner.analyse(ScheduleIndex(matches))


def _evaluate(planner, season, world, matches, rules):
    ctx = EvalContext(world=world, season=season, travel=_Travel(), detail=True)
    return evaluate(matches, [rule(planner=planner) for rule in rules], ctx)


def _only_same_day_blackout(day, venue="v1"):
    return {"venue_blackouts": [VenueBlackout(venue=venue, date=day, reason="x")]}


def test_a_match_clear_of_every_contingent_commitment_needs_no_fallback():
    planner, _, _ = _planner([_maybe("t1", THU, "a")])
    far = [f.match("league", "t1", "t2", date(2026, 9, 20), "v1")]

    analysis = _analyse(planner, far)

    assert analysis.plan.fallbacks == {} and analysis.plan.uncovered == []


def test_an_exposed_match_shifts_within_its_weekend_first():
    planner, season, world = _planner([_maybe("t1", THU, "a")])
    match = f.match("league", "t1", "t2", SUN, "v1")

    plan = _analyse(planner, [match]).plan

    fallback = plan.fallbacks[match.key]
    # Fri and Sat are still within three days of the Thursday; Monday clears it.
    assert (fallback.date, fallback.kind) == (MON, "same_slot")
    assert fallback.trigger_node_ids == ("t1/uecl/a",)
    result = _evaluate(
        planner, season, world, [match], [ContingencyCoverage, ContingencyCollision]
    )
    assert result.hard_violations == 0


def test_a_reserve_date_is_used_when_the_weekend_has_no_valid_day():
    reserve = date(2026, 9, 2)
    planner, _, _ = _planner(
        [_maybe("t1", THU, "a")],
        season_args={
            **_only_same_day_blackout(MON),
            "reserve_dates": [ReserveDate(date=reserve)],
        },
    )
    match = f.match("league", "t1", "t2", SUN, "v1")

    fallback = _analyse(planner, [match]).plan.fallbacks[match.key]

    assert (fallback.date, fallback.kind) == (reserve, "reserve_date")


def test_a_full_reserve_date_pushes_the_next_match_to_a_free_weekend():
    reserve = date(2026, 9, 2)
    planner, _, _ = _planner(
        [_maybe("t1", THU, "a"), _maybe("t3", THU, "b")],
        season_args={
            "venue_blackouts": [
                VenueBlackout(venue="v1", date=MON, reason="x"),
                VenueBlackout(venue="v3", date=MON, reason="x"),
            ],
            "reserve_dates": [ReserveDate(date=reserve, capacity=1)],
        },
    )
    first = f.match("league", "t1", "t2", SUN, "v1")
    second = f.match("league", "t3", "t4", SUN, "v3")

    plan = _analyse(planner, [first, second]).plan

    kinds = {plan.fallbacks[first.key].kind, plan.fallbacks[second.key].kind}
    assert kinds == {"reserve_date", "next_free_slot"}
    assert plan.uncovered == []


def test_a_cup_round_counts_half_its_teams_against_reserve_capacity():
    """Four teams can reach the same cup round, but top-flight sides can meet
    before it, so only two of them advance together: capacity 2 holds all four."""
    reserve = date(2026, 9, 2)
    cup = [
        f.commitment(
            f"t{i}", THU, hard=False, probability=0.5, min_rest_days=3, source="cup",
            competition_id="cup", node_id=f"t{i}/cup/qf",
        )
        for i in range(1, 5)
    ]
    planner, _, _ = _planner(
        cup,
        season_args={
            "venue_blackouts": [
                VenueBlackout(venue=f"v{i}", date=MON, reason="x") for i in (1, 3)
            ],
            "reserve_dates": [ReserveDate(date=reserve, capacity=2)],
            "contingency_policy": ContingencyPolicy(fallback_kinds=["reserve_date"]),
        },
    )
    matches = [
        f.match("league", "t1", "t2", SUN, "v1"),
        f.match("league", "t3", "t4", SUN, "v3"),
    ]

    plan = _analyse(planner, matches).plan

    # Both matches share the cup round, so they count once between them.
    assert {fb.date for fb in plan.fallbacks.values()} == {reserve}
    assert plan.uncovered == []


def test_a_match_without_any_valid_fallback_breaks_coverage():
    planner, season, world = _planner(
        [_maybe("t1", THU, "a")],
        season_args={"contingency_policy": ContingencyPolicy(fallback_kinds=["reserve_date"])},
    )
    # Only reserve dates are allowed and the season has none.
    match = f.match("league", "t1", "t2", SUN, "v1")

    result = _evaluate(planner, season, world, [match], [ContingencyCoverage])

    assert result.result("contingency_coverage").count == 1
    assert "no valid fallback" in result.result("contingency_coverage").events[0].detail


def test_a_fallback_must_leave_rest_against_the_teams_other_matches():
    planner, _, _ = _planner([_maybe("t1", THU, "a")])
    match = f.match("league", "t1", "t2", SUN, "v1")
    # t1 plays again the Thursday after: Monday would leave only 2 rest days.
    other = f.match("league", "t1", "t3", date(2026, 8, 27), "v3", round_index=1)

    fallback = _analyse(planner, [match, other]).plan.fallbacks[match.key]

    assert fallback.date != MON


def test_a_fallback_must_clear_the_commitments_that_come_with_its_trigger():
    # Reaching "a" can lead to "b" on the Thursday after, which Monday is too close to.
    planner, _, _ = _planner(
        [_maybe("t1", THU, "a"), _maybe("t1", date(2026, 8, 27), "b")]
    )
    match = f.match("league", "t1", "t2", SUN, "v1")

    fallback = _analyse(planner, [match]).plan.fallbacks[match.key]

    assert fallback.date != MON


def test_commitments_of_an_exclusive_branch_do_not_come_along():
    a = _maybe("t1", THU, "a", exclusive=frozenset({"t1/uecl/b"}))
    b = _maybe("t1", date(2026, 8, 27), "b", exclusive=frozenset({"t1/uecl/a"}))
    planner, _, _ = _planner([a, b])
    match = f.match("league", "t1", "t2", SUN, "v1")

    fallback = _analyse(planner, [match]).plan.fallbacks[match.key]

    assert fallback.date == MON


def test_a_fallback_never_lands_on_a_day_a_club_sibling_is_at_home():
    men = f.team("m", "c", "v1")
    women = f.team("w", "c", "v2", gender="women")
    opp_m, opp_w = f.team("om", "o1", "v3"), f.team("ow", "o2", "v4", gender="women")
    clubs = [f.club("c", [men, women]), f.club("o1", [opp_m]), f.club("o2", [opp_w])]
    league = f.competition("league", ["m", "om"], min_rest_days=3)
    women_league = f.competition("topp", ["w", "ow"], gender="women", min_rest_days=3)
    world = f.world(
        clubs, [f.venue(f"v{i}") for i in range(1, 5)], [league, women_league]
    )
    season = f.season(competitions=["league", "topp"])
    planner = ContingencyPlanner(
        world, season, [league, women_league], f.commitment_set(_maybe("m", THU, "a"))
    )
    match = f.match("league", "m", "om", SUN, "v1")
    sibling = f.match("topp", "w", "ow", MON, "v2", round_index=0)

    fallback = _analyse(planner, [match, sibling]).plan.fallbacks[match.key]

    assert fallback.date != MON


def test_the_final_round_gets_no_fallback():
    planner, _, _ = _planner([_maybe("t1", THU, "a")])
    league = _world()[1]
    final = f.match("league", "t1", "t2", SUN, "v1", round_index=league.rounds - 1)

    plan = _analyse(planner, [final]).plan

    assert plan.uncovered == [final.key]


def _two_matches_for_one_team(*, exclusive: bool):
    a = _maybe("t1", date(2026, 8, 13), "a", exclusive=frozenset({"t1/uecl/b"}) if exclusive else frozenset())
    b = _maybe("t1", THU, "b", exclusive=frozenset({"t1/uecl/a"}) if exclusive else frozenset())
    planner, _, _ = _planner(
        [a, b],
        season_args={
            "reserve_dates": [
                ReserveDate(date=date(2026, 9, 2)),
                ReserveDate(date=date(2026, 9, 9)),
            ],
            "contingency_policy": ContingencyPolicy(fallback_kinds=["reserve_date"]),
        },
    )
    first = f.match("league", "t1", "t2", date(2026, 8, 16), "v1", round_index=0)
    second = f.match("league", "t1", "t3", SUN, "v1", round_index=1)
    return planner, first, second


def test_fallbacks_of_mutually_exclusive_triggers_may_share_a_team_date():
    planner, first, second = _two_matches_for_one_team(exclusive=True)

    plan = _analyse(planner, [first, second]).plan

    assert plan.fallbacks[first.key].date == plan.fallbacks[second.key].date == date(2026, 9, 2)


def test_fallbacks_of_co_triggerable_rounds_may_not_share_a_team_date():
    planner, first, second = _two_matches_for_one_team(exclusive=False)

    plan = _analyse(planner, [first, second]).plan

    assert plan.fallbacks[first.key].date == date(2026, 9, 2)
    assert plan.fallbacks[second.key].date == date(2026, 9, 9)


def test_the_collision_rule_flags_co_triggerable_fallbacks_on_one_date():
    planner, season, world = _planner(
        [_maybe("t1", THU, "a"), _maybe("t1", date(2026, 8, 13), "b")]
    )
    first = f.match("league", "t1", "t2", date(2026, 8, 16), "v1", round_index=0)
    second = f.match("league", "t1", "t3", SUN, "v1", round_index=1)
    day = date(2026, 9, 2)
    plan = ContingencyPlan(
        fallbacks={
            first.key: Fallback(day, "v1", None, ("t1/uecl/b",), "reserve_date"),
            second.key: Fallback(day, "v1", None, ("t1/uecl/a",), "reserve_date"),
        }
    )
    index = ScheduleIndex([first, second])
    index.contingency = (planner, planner.check(index, plan))
    ctx = EvalContext(world=world, season=season, travel=_Travel(), detail=True)

    result = ContingencyCollision(planner=planner).evaluate(index, ctx)

    assert result.count == 1
    assert "clash" in result.events[0].detail


def test_the_coverage_rule_flags_a_fallback_that_is_not_valid():
    planner, season, world = _planner([_maybe("t1", THU, "a")])
    match = f.match("league", "t1", "t2", SUN, "v1")
    plan = ContingencyPlan(
        fallbacks={match.key: Fallback(date(2026, 8, 21), "v1", None, ("t1/uecl/a",), "same_slot")}
    )
    index = ScheduleIndex([match])
    index.contingency = (planner, planner.check(index, plan))
    ctx = EvalContext(world=world, season=season, travel=_Travel(), detail=True)

    result = ContingencyCoverage(planner=planner).evaluate(index, ctx)

    assert result.count == 1
    assert "not valid" in result.events[0].detail


def test_expected_disruption_prices_the_move_by_its_probability():
    planner, season, world = _planner([_maybe("t1", THU, "a", p=0.5)])
    match = f.match("league", "t1", "t2", SUN, "v1")

    result = _evaluate(planner, season, world, [match], [ExpectedDisruption])

    # 3.0 * p 0.5 * (same_slot 1.0 + 0.25 per day * 1 day)
    assert result.result("expected_disruption").total == -1.875


def test_expected_disruption_is_zero_for_a_match_nothing_threatens():
    planner, season, world = _planner([_maybe("t1", THU, "a")])
    far = [f.match("league", "t1", "t2", date(2026, 9, 20), "v1")]

    assert _evaluate(planner, season, world, far, [ExpectedDisruption]).soft_total == 0.0


def test_a_hard_commitment_is_not_contingent():
    planner, _, _ = _planner([f.commitment("t1", THU, min_rest_days=3)])
    match = f.match("league", "t1", "t2", date(2026, 8, 21), "v1")

    assert _analyse(planner, [match]).plan.fallbacks == {}


def test_a_fixture_on_a_reserve_date_is_a_hard_violation():
    reserve = date(2026, 9, 2)
    season = f.season(competitions=["league"], reserve_dates=[ReserveDate(date=reserve)])
    world, _ = _world()
    ctx = EvalContext(world=world, season=season, travel=_Travel(), detail=True)

    on_reserve = [f.match("league", "t1", "t2", reserve, "v1")]
    off_reserve = [f.match("league", "t1", "t2", date(2026, 9, 3), "v1")]

    assert evaluate(on_reserve, [ReserveDateUnused()], ctx).hard_violations == 1
    assert evaluate(off_reserve, [ReserveDateUnused()], ctx).hard_violations == 0


def test_the_plan_serialises_every_trigger_as_a_move():
    planner, _, _ = _planner([_maybe("t1", THU, "a"), _maybe("t1", date(2026, 8, 21), "b")])
    match = f.match("league", "t1", "t2", SUN, "v1")

    plan = _analyse(planner, [match]).plan
    payload = plan.to_dict([match])

    assert {move["trigger"] for move in payload["fallbacks"]} == {"t1/uecl/a", "t1/uecl/b"}
    assert {move["from"] for move in payload["fallbacks"]} == {"2026-08-23"}
    assert payload["uncovered"] == []
