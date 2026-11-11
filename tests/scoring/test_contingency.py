"""Fallback dates for matches a later cup or UEFA round may displace (#9)."""

from __future__ import annotations

from datetime import date

import factories as f

from terminliste.contingency import (
    DAY_COST,
    KIND_COST,
    ContingencyPlan,
    ContingencyPlanner,
    Fallback,
    _bracket_caps,
)
from terminliste.model.calendar import build_calendar
from terminliste.model.schema import ContingencyPolicy, DatedNote, ReserveDate
from terminliste.scoring.base import EvalContext, ScheduleIndex, evaluate
from terminliste.scoring.hard import (
    ContingencyCollision,
    ContingencyCoverage,
    ReserveDateUnused,
)
from terminliste.scoring.registry import build_constraints
from terminliste.scoring.soft import ExpectedDisruption


class _StubTravel:
    def hours(self, a, b):
        return 1.0


_SUNDAY = date(2026, 8, 2)  # a Sunday
_TUESDAY = date(2026, 8, 4)


def _world(team_ids=("a", "b", "c", "d")):
    venues = [f.venue(f"v_{t}") for t in team_ids]
    teams = [f.team(t, f"club_{t}", f"v_{t}") for t in team_ids]
    clubs = [f.club(f"club_{t.id}", [t]) for t in teams]
    league = f.competition("lg", list(team_ids), min_rest_days=2)
    return f.world(clubs, venues, [league]), league


def _ctx(world, season):
    return EvalContext(world=world, season=season, travel=_StubTravel(), detail=True)


def _planner(commitments, *, season=None, kinds=None, reserve=()):
    world, league = _world()
    policy = ContingencyPolicy(**({"fallback_kinds": kinds} if kinds else {}))
    season = season or f.season(
        competitions=["lg"],
        contingency_policy=policy,
        reserve_dates=[ReserveDate(date=d, capacity=2) for d in reserve],
    )
    return world, season, ContingencyPlanner(world, season, [league], commitments)


def _contingent(team, day, *, node=None, p=0.5, exclusive=frozenset(), source="uefa"):
    return f.commitment(
        team,
        day,
        hard=False,
        probability=p,
        min_rest_days=2,
        node_id=node or f"{team}/uecl/{day}",
        exclusive_with=exclusive,
        source=source,
    )


def _match(home, away, day, round_index=0):
    return f.match("lg", home, away, day, f"v_{home}", round_index=round_index)


# -- coverage -----------------------------------------------------------------


def test_a_colliding_match_gets_a_same_slot_fallback_valid_in_its_scenario():
    commitment = _contingent("a", _TUESDAY)
    _, _, planner = _planner(f.commitment_set(commitment))
    match = _match("a", "b", _SUNDAY)

    plan = planner.plan([match])

    fallback = plan.fallbacks[match.key]
    assert plan.colliding == {match.key: (commitment.node_id,)}
    assert fallback.kind == "same_slot"
    # Sat 1 Aug leaves 2 rest days before Tue 4 Aug; Mon 3 Aug would not.
    assert fallback.date == date(2026, 8, 1)
    assert fallback.trigger_node_ids == (commitment.node_id,)
    assert not commitment.clashes(fallback.date)


def test_a_match_clear_of_every_contingent_commitment_needs_no_fallback():
    _, _, planner = _planner(f.commitment_set(_contingent("a", _TUESDAY)))

    plan = planner.plan([_match("a", "b", date(2026, 8, 16))])

    assert plan.colliding == {} and plan.fallbacks == {}


def test_coverage_is_a_hard_violation_only_when_no_fallback_is_valid():
    commitments = f.commitment_set(_contingent("a", _TUESDAY))
    world, league = _world()
    season = f.season(
        competitions=["lg"],
        global_blackouts=[DatedNote(date=date(2026, 7, 31)), DatedNote(date=date(2026, 8, 1))],
        contingency_policy=ContingencyPolicy(fallback_kinds=["same_slot"]),
    )
    planner = ContingencyPlanner(world, season, [league], commitments)
    coverage = ContingencyCoverage(planner=planner)
    matches = [_match("a", "b", _SUNDAY)]

    broken = evaluate(matches, [coverage], _ctx(world, season))
    assert broken.hard_violations == 1

    open_season = f.season(competitions=["lg"])
    open_planner = ContingencyPlanner(world, open_season, [league], commitments)
    fine = evaluate(
        matches, [ContingencyCoverage(planner=open_planner)], _ctx(world, open_season)
    )
    assert fine.hard_violations == 0


def test_a_fallback_clears_commitments_that_come_with_its_trigger():
    """A later round the team may also reach is in the trigger's scenario, so
    the fallback keeps its rest from that round too."""
    early = _contingent("a", _TUESDAY, node="a/uecl/r1")
    later = _contingent("a", date(2026, 8, 8), node="a/uecl/r2")
    match = _match("a", "b", _SUNDAY)
    _, _, alone = _planner(f.commitment_set(early), kinds=["next_free_slot"])
    _, _, planner = _planner(f.commitment_set(early, later), kinds=["next_free_slot"])

    assert alone.plan([match]).fallbacks[match.key].date == date(2026, 8, 7)
    fallback = planner.plan([match]).fallbacks[match.key]

    assert not early.clashes(fallback.date) and not later.clashes(fallback.date)
    assert fallback.date == date(2026, 8, 14)


def test_alternatives_exclusive_with_the_trigger_do_not_constrain_the_fallback():
    early = _contingent("a", _TUESDAY, node="a/uecl/r1", exclusive=frozenset({"a/uecl/alt"}))
    alt = _contingent("a", date(2026, 8, 7), node="a/uecl/alt", exclusive=frozenset({"a/uecl/r1"}))
    _, _, planner = _planner(f.commitment_set(early, alt), kinds=["next_free_slot"])
    match = _match("a", "b", _SUNDAY)

    fallback = planner.plan([match]).fallbacks[match.key]

    # Fri 7 Aug is the alternative's own date, which this scenario never plays.
    assert fallback.date == date(2026, 8, 7)


def test_a_fallback_day_must_clear_the_opponents_maybe_rounds():
    commitments = f.commitment_set(
        _contingent("a", _TUESDAY), _contingent("b", date(2026, 8, 10))
    )
    match = _match("a", "b", _SUNDAY)

    _, _, only_risky = _planner(
        commitments, kinds=["reserve_date"], reserve=[date(2026, 8, 12)]
    )
    assert only_risky.plan([match]).fallbacks == {}

    _, _, with_clear = _planner(
        commitments, kinds=["reserve_date"], reserve=[date(2026, 8, 12), date(2026, 8, 20)]
    )
    assert with_clear.plan([match]).fallbacks[match.key].date == date(2026, 8, 20)


def test_a_fallback_keeps_the_teams_rest_and_one_match_per_day():
    commitment = _contingent("a", _TUESDAY)
    _, _, planner = _planner(f.commitment_set(commitment), kinds=["same_slot"])
    colliding = _match("a", "b", _SUNDAY)
    # c-v-a the day before leaves no room on Sat 1 Aug for team a.
    friday = f.match("lg", "c", "a", date(2026, 7, 31), "v_c", round_index=1)

    plan = planner.plan([colliding, friday])

    assert colliding.key not in plan.fallbacks


def test_a_fallback_avoids_a_clubs_other_home_match():
    commitment = _contingent("a", _TUESDAY)
    venues = [f.venue("v_a"), f.venue("v_w"), f.venue("v_b")]
    men = f.team("a", "club_a", "v_a")
    women = f.team("a_w", "club_a", "v_w", gender="women")
    other = f.team("b", "club_b", "v_b")
    world = f.world(
        [f.club("club_a", [men, women]), f.club("club_b", [other])],
        venues,
        [f.competition("lg", ["a", "b"], min_rest_days=2)],
    )
    season = f.season(competitions=["lg"], contingency_policy=ContingencyPolicy(fallback_kinds=["same_slot"]))
    planner = ContingencyPlanner(
        world, season, [world.competitions["lg"]], f.commitment_set(commitment)
    )
    match = _match("a", "b", _SUNDAY)
    # The club's women are home on Sat 1 Aug at their own ground.
    women_home = f.match("wl", "a_w", "x", date(2026, 8, 1), "v_w")

    plan = planner.plan([match, women_home])

    # Sat 1 Aug is taken by the women's home match; Fri 31 Jul is clear.
    assert plan.fallbacks[match.key].date == date(2026, 7, 31)


# -- collision ----------------------------------------------------------------


def _reserve_matches():
    """Team a plays on 2 Aug and 23 Aug, each near a commitment of its own."""
    return [_match("a", "c", _SUNDAY), _match("a", "d", date(2026, 8, 23), round_index=1)]


def _two_triggers(*, exclusive):
    ids = ("a/uecl/n1", "a/uecl/n2")
    first = _contingent(
        "a", _TUESDAY, node=ids[0], exclusive=frozenset({ids[1]}) if exclusive else frozenset()
    )
    second = _contingent(
        "a", date(2026, 8, 25), node=ids[1], exclusive=frozenset({ids[0]}) if exclusive else frozenset()
    )
    return f.commitment_set(first, second)


def test_exclusive_triggers_may_share_a_reserve_date():
    _, season, planner = _planner(
        _two_triggers(exclusive=True), kinds=["reserve_date"], reserve=[date(2026, 8, 12)]
    )
    matches = _reserve_matches()

    plan = planner.plan(matches)

    assert {f.date for f in plan.fallbacks.values()} == {date(2026, 8, 12)}
    world, _ = _world()
    result = ContingencyCollision(planner=planner).evaluate(
        _indexed(matches, planner, plan), _ctx(world, season)
    )
    assert result.count == 0


def test_independently_triggerable_fallbacks_never_share_a_team_date():
    _, season, planner = _planner(
        _two_triggers(exclusive=False), kinds=["reserve_date"], reserve=[date(2026, 8, 12)]
    )
    matches = _reserve_matches()

    plan = planner.plan(matches)

    assert len(plan.fallbacks) == 1 and len(plan.uncovered) == 1


def test_collision_flags_two_co_triggerable_fallbacks_on_one_team_date():
    _, season, planner = _planner(_two_triggers(exclusive=False))
    world, _ = _world()
    plan = ContingencyPlan(
        fallbacks={
            "m1": _fallback("m1", "n1"),
            "m2": _fallback("m2", "n2"),
        }
    )
    index = _indexed([], planner, plan)

    result = ContingencyCollision(planner=planner).evaluate(index, _ctx(world, season))

    assert result.count == 1


def test_collision_allows_the_same_date_for_exclusive_triggers():
    _, season, planner = _planner(_two_triggers(exclusive=True))
    world, _ = _world()
    plan = ContingencyPlan(
        fallbacks={"m1": _fallback("m1", "n1"), "m2": _fallback("m2", "n2")}
    )

    result = ContingencyCollision(planner=planner).evaluate(
        _indexed([], planner, plan), _ctx(world, season)
    )

    assert result.count == 0


def _fallback(key, node):
    return Fallback(
        match_key=key,
        competition_id="lg",
        home_team="a",
        away_team="c" if key == "m1" else "d",
        date=date(2026, 8, 12),
        venue=f"v_{key}",
        kickoff=None,
        trigger_node_ids=(f"a/uecl/{node}",),
        kind="reserve_date",
        from_date=_SUNDAY,
    )


def _indexed(matches, planner, plan):
    index = ScheduleIndex(matches)
    index.contingency[planner] = plan
    return index


def test_different_teams_triggers_are_independent():
    first = _contingent("a", _TUESDAY, node="a/uecl/n")
    second = _contingent("b", _TUESDAY, node="b/uecl/n")
    _, _, planner = _planner(f.commitment_set(first, second))

    assert planner.co_triggerable(("a/uecl/n",), ("b/uecl/n",))


# -- reserve dates -------------------------------------------------------------


def test_reserve_dates_never_enter_the_base_calendar():
    world, _ = _world()
    reserve = date(2026, 8, 12)
    season = f.season(competitions=["lg"], reserve_dates=[ReserveDate(date=reserve)])

    calendar = build_calendar(world, season)

    assert not calendar.is_allowed(reserve)
    assert calendar.is_allowed(date(2026, 8, 13))


def test_a_base_fixture_on_a_reserve_date_is_a_hard_violation():
    world, _ = _world()
    reserve = date(2026, 8, 12)
    season = f.season(competitions=["lg"], reserve_dates=[ReserveDate(date=reserve)])

    on = evaluate([_match("a", "b", reserve)], [ReserveDateUnused()], _ctx(world, season))
    off = evaluate([_match("a", "b", _SUNDAY)], [ReserveDateUnused()], _ctx(world, season))

    assert on.hard_violations == 1 and off.hard_violations == 0


def test_reserve_capacity_follows_the_bracket_bound():
    """Of four top-flight teams in a round drawn against each other, at most
    two reach the next, so only two cup fallbacks can fire on one reserve date."""
    first = [
        f.commitment(
            t, date(2026, 8, 5), source="cup", competition_id="cup", node_id=f"{t}/cup/r1", probability=1.0
        )
        for t in "abcd"
    ]
    second = [
        f.commitment(
            t,
            date(2026, 8, 19),
            hard=False,
            probability=0.5,
            source="cup",
            competition_id="cup",
            node_id=f"{t}/cup/r2",
        )
        for t in "abcd"
    ]
    commitments = f.commitment_set(*first, *second)

    assert _bracket_caps(commitments) == {("cup", "r1"): 4, ("cup", "r2"): 2}

    _, _, planner = _planner(commitments)
    fallbacks = [
        Fallback(
            match_key=f"m{t}",
            competition_id="lg",
            home_team=t,
            away_team="z",
            date=date(2026, 8, 12),
            venue="v",
            kickoff=None,
            trigger_node_ids=(f"{t}/cup/r2",),
            kind="reserve_date",
            from_date=_SUNDAY,
        )
        for t in "abcd"
    ]
    assert planner.reserve_load(fallbacks) == 2


# -- expected disruption --------------------------------------------------------


def test_expected_disruption_is_probability_times_move_cost():
    commitment = _contingent("a", _TUESDAY, p=0.25)
    world, season, planner = _planner(f.commitment_set(commitment))
    matches = [_match("a", "b", _SUNDAY)]

    result = evaluate(matches, [ExpectedDisruption(planner=planner)], _ctx(world, season))

    # Sunday -> Saturday: one day, same slot, p = 0.25, weight 3.0.
    expected = -3.0 * 0.25 * (KIND_COST["same_slot"] + DAY_COST * 1)
    assert abs(result.result("expected_disruption").total - expected) < 1e-9


def test_registry_adds_the_contingency_rules_only_when_something_is_contingent():
    world, league = _world()
    season = f.season(competitions=["lg"])

    without = {c.id for c in build_constraints(world, season, [league], f.commitment_set())}
    with_ = {
        c.id
        for c in build_constraints(
            world, season, [league], f.commitment_set(_contingent("a", _TUESDAY))
        )
    }

    contingency = {"contingency_coverage", "contingency_collision", "expected_disruption"}
    assert not contingency & without
    assert contingency <= with_


def test_the_plan_serialises_for_the_json_output():
    commitment = _contingent("a", _TUESDAY)
    _, _, planner = _planner(f.commitment_set(commitment))

    payload = planner.plan([_match("a", "b", _SUNDAY)]).to_dict()

    assert payload["colliding"] == payload["covered"] == 1
    assert payload["fallbacks"][0]["kind"] == "same_slot"
    assert payload["moves"][0]["trigger"] == commitment.node_id
    assert payload["moves"][0]["probability"] == 0.5


def test_a_final_round_match_gets_no_fallback_so_coverage_fails():
    commitment = _contingent("a", _TUESDAY)
    world, league = _world()
    season = f.season(competitions=["lg"])
    planner = ContingencyPlanner(world, season, [league], f.commitment_set(commitment))
    last = league.rounds - 1
    matches = [_match("a", "b", _SUNDAY, round_index=last)]

    plan = planner.plan(matches)
    score = evaluate(matches, [ContingencyCoverage(planner=planner)], _ctx(world, season))

    assert plan.fallbacks == {} and plan.uncovered == [matches[0].key]
    assert score.hard_violations == 1
