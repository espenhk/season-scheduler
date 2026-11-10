"""Round slots planned from the real 2026 season frame (#6)."""

from __future__ import annotations

from datetime import date

import factories as f
import pytest

from terminliste.frame import CupDay, SeasonFrame, build_season_frame
from terminliste.model.calendar import build_calendar
from terminliste.model.schema import ExtraRoundSlot
from terminliste.rounds.european_schedule import EuropeanCommitmentDate
from terminliste.slot_planner import SlotPlanningError, plan_slots
from terminliste.solvers.greedy import plan_competitions


@pytest.fixture(scope="module")
def frame(world, season):
    return build_season_frame(world, season)[0]


@pytest.fixture(scope="module")
def plans(world, season, competitions, frame):
    return plan_slots(world, season, competitions, build_calendar(world, season), frame)


def test_every_round_has_exactly_one_distinct_slot(plans, competitions):
    for competition in competitions:
        plan = plans[competition.id]
        assert len(plan.chosen) == competition.rounds
        spans = [(s.start, s.end) for s in plan.chosen]
        assert spans == sorted(spans)
        assert all(a[1] < b[0] for a, b in zip(spans, spans[1:], strict=False))
        assert all(s.start <= s.default_day <= s.end for s in plan.chosen)
        assert all(s.default_day in s.days for s in plan.chosen)


def test_no_round_anchor_clashes_with_a_certain_cup_or_uefa_date(
    plans, competitions, frame
):
    """Checked from the raw resolved commitments, not the planner's own flags."""
    for competition in competitions:
        for round_index, anchor in plans[competition.id].anchors.items():
            for team_id in competition.teams:
                cup = [
                    (day, schedule.min_rest_days)
                    for schedule in frame.cup_schedules
                    for placement in schedule.rounds
                    for t, day in placement.dates.items()
                    if t == team_id
                ]
                european = [
                    (c.date, c.min_rest_days)
                    for c in frame.european_commitments.get(team_id, ())
                    if c.certain
                ]
                for day, min_rest in cup + european:
                    rest = abs((anchor - day).days) - 1
                    assert rest >= min_rest, (competition.id, round_index, anchor, team_id, day)


def test_no_league_round_shares_a_weekend_with_a_cup_day_of_its_gender(
    plans, competitions, frame
):
    """Closes the cup-weekend criterion deferred from #5."""
    for competition in competitions:
        blocked = frame.cup_weekend_dates(gender=competition.gender)
        assert blocked
        for slot in plans[competition.id].chosen:
            assert not any(slot.contains(day) for day in blocked), (competition.id, slot)


def test_full_round_and_final_round_requirements_land_on_their_dates(plans):
    eliteserien = plans["eliteserien_2026"]
    assert date(2026, 5, 16) in {s.default_day for s in eliteserien.chosen}
    for plan in plans.values():
        final = plan.chosen[-1]
        assert final.default_day == max(s.default_day for s in plan.chosen)


def test_hard_fixed_requirement_dates_sit_inside_a_chosen_slot(plans, season):
    for requirement in season.fixed_requirements:
        if not requirement.hard:
            continue
        plan = plans[requirement.competition]
        assert any(s.contains(requirement.date) for s in plan.chosen), requirement.id


def test_holiday_and_midweek_slots_are_declared_in_the_data(season):
    kinds = {e.kind for e in season.extra_round_slots}
    assert kinds == {"holiday", "midweek"}
    assert {date(2026, 4, 6), date(2026, 5, 14), date(2026, 5, 25)} <= {
        e.date for e in season.extra_round_slots
    }


def test_world_cup_break_and_fifa_windows_hold_no_eliteserien_round(plans):
    for slot in plans["eliteserien_2026"].chosen:
        assert not slot.contains(date(2026, 6, 10))
        assert slot.default_day not in (date(2026, 3, 28), date(2026, 9, 27), date(2026, 11, 14))


def test_planner_is_deterministic(world, season, competitions, frame, plans):
    again = plan_slots(world, season, competitions, build_calendar(world, season), frame)
    for competition_id, plan in plans.items():
        assert again[competition_id].chosen == plan.chosen


def test_plan_competitions_takes_anchors_and_slots_from_the_plan(
    world, season, competitions, plans
):
    planned = plan_competitions(
        world, season, competitions, build_calendar(world, season), slot_plans=plans
    )
    for plan in planned:
        slot_plan = plans[plan.competition.id]
        assert plan.anchors == slot_plan.anchors
        assert plan.slots == dict(enumerate(slot_plan.chosen))


def _tiny_league(**extra):
    extra.setdefault("rounds_per_pairing", 1)
    teams = [f.team(f"t{i}", f"c{i}", "v1") for i in range(4)]
    clubs = [f.club(f"c{i}", [teams[i]]) for i in range(4)]
    competition = f.competition("league", [t.id for t in teams], **extra)
    return f.world(clubs, [f.venue("v1")], [competition]), competition


def test_too_few_slots_is_a_clear_error():
    world, league = _tiny_league(rounds_per_pairing=2)  # 6 rounds
    season = f.season(competitions=["league"], start=date(2026, 3, 1), end=date(2026, 3, 22))

    with pytest.raises(SlotPlanningError, match=r"league: 4 round slots for 6 rounds"):
        plan_slots(world, season, [league], build_calendar(world, season))


def test_slots_too_close_together_are_reported_with_the_usable_count():
    world, league = _tiny_league(rounds_per_pairing=2)
    extras = [ExtraRoundSlot(date=date(2026, 3, d), kind="holiday") for d in (16, 17, 18)]
    season = f.season(
        competitions=["league"],
        start=date(2026, 3, 1),
        end=date(2026, 3, 22),
        extra_round_slots=extras,
    )

    with pytest.raises(SlotPlanningError, match=r"7 round slots, but only 4 can be used together"):
        plan_slots(world, season, [league], build_calendar(world, season))


def test_a_cup_weekend_that_leaves_too_few_slots_fails_the_plan():
    world, league = _tiny_league(gender="men")  # 3 rounds
    season = f.season(competitions=["league"], start=date(2026, 3, 13), end=date(2026, 3, 30))
    calendar = build_calendar(world, season)
    assert len(plan_slots(world, season, [league], calendar)["league"].chosen) == 3

    cup_day = CupDay(
        competition_id="cup",
        gender="men",
        round_id="final",
        label="Cup final",
        date=date(2026, 3, 14),
        min_rest_days=2,
        teams=("t0", "t1"),
    )
    assert cup_day.kind == "cup_weekend"
    with pytest.raises(SlotPlanningError, match="2 round slots for 3 rounds"):
        plan_slots(world, season, [league], calendar, SeasonFrame(cup_days=[cup_day]))


def test_clashes_are_ranked_not_fatal_when_no_clear_slot_exists():
    """With a long rest window on every Sunday no day of any slot is clear for
    t0, yet the plan comes back: it keeps t1's single early clash out and
    reports what remains."""
    world, league = _tiny_league()  # 3 rounds
    season = f.season(competitions=["league"], start=date(2026, 3, 1), end=date(2026, 4, 5))
    sundays = [date(2026, 3, d) for d in (1, 8, 15, 22, 29)]

    def commitment(team, day):
        return EuropeanCommitmentDate(
            team_id=team, date=day, min_rest_days=10, label="x", competition_id="cl"
        )

    commitments = {
        "t0": [commitment("t0", day) for day in sundays],
        "t1": [commitment("t1", sundays[0])],
    }
    frame = SeasonFrame.from_resolved(world, [], commitments)

    plan = plan_slots(world, season, [league], build_calendar(world, season), frame)["league"]

    assert len(plan.chosen) == 3
    assert all(s.clashing_teams == ("t0",) for s in plan.chosen)
    assert len(plan.warnings) == 3
    assert all("no clear slot was available" in w for w in plan.warnings)


def _slot(kind, start, end=None):
    from terminliste.model.slots import RoundSlot

    end = end or start
    return RoundSlot("l", kind, start, end, (start, end), start)


def test_combinations_count_only_mutually_compatible_slots():
    from terminliste.slot_planner import SlotPlan

    weekend = _slot("weekend", date(2026, 4, 3), date(2026, 4, 6))
    easter_monday = _slot("holiday", date(2026, 4, 6))
    later = _slot("weekend", date(2026, 4, 10), date(2026, 4, 13))
    plan = SlotPlan("l", 2, [weekend, easter_monday, later], [weekend, later])
    # The holiday slot is inside the weekend, so only two pairs are usable.
    assert plan.combinations == 2
    assert plan.overlapping == 1


def test_2026_holiday_slots_inside_a_weekend_are_reported_as_overlapping(plans):
    import math

    plan = plans["eliteserien_2026"]
    assert plan.overlapping >= 1
    assert plan.combinations < math.comb(len(plan.candidates), plan.rounds)
