"""Cup days in the season frame, built from the real 2026 data (#5)."""

from __future__ import annotations

from datetime import date

import pytest

from terminliste.frame import build_season_frame
from terminliste.model.calendar import anchor_dates, build_calendar


@pytest.fixture(scope="module")
def frame_and_warnings(world, season):
    return build_season_frame(world, season)


@pytest.fixture(scope="module")
def frame(frame_and_warnings):
    return frame_and_warnings[0]


def test_frame_holds_cup_schedules_and_days_for_every_cup(frame, season):
    assert {s.competition_id for s in frame.cup_schedules} == set(season.cup_competitions)
    assert {d.competition_id for d in frame.cup_days} == set(season.cup_competitions)
    assert frame.cup_days == sorted(
        frame.cup_days, key=lambda d: (d.date, d.competition_id, d.round_id)
    )


def test_cup_days_are_queryable_by_gender_kind_and_team(frame):
    men = frame.cup_days_of(gender="men")
    assert men and all(d.gender == "men" for d in men)
    assert {d.gender for d in frame.cup_days_of(gender="women")} == {"women"}

    round_one = next(d for d in men if d.competition_id == "cup_men_2027" and d.round_id == "r1")
    assert round_one.date == date(2026, 8, 22)
    assert round_one.kind == "cup_weekend"  # a Saturday
    assert round_one in frame.cup_days_of(kind="cup_weekend", team_id="brann_m")
    assert date(2026, 8, 22) in frame.cup_weekend_dates(gender="men")
    assert date(2026, 8, 22) not in frame.cup_weekend_dates(gender="women")
    assert frame.cup_weekend_blocks(gender="men")[date(2026, 8, 22)].endswith("Round 1")

    midweek = frame.cup_days_of(kind="cup_week")
    assert midweek and all(d.weekday in ("tuesday", "wednesday", "thursday") for d in midweek)


def test_windowed_cup_rounds_land_midweek_on_a_single_date(world, season, frame):
    for cup_id in season.cup_competitions:
        cup = world.competition(cup_id)
        schedule = next(s for s in frame.cup_schedules if s.competition_id == cup_id)
        for round_, placement in zip(cup.cup_rounds, schedule.rounds, strict=True):
            if round_.is_forced:
                continue
            assert len(set(placement.dates.values())) == 1, (cup_id, round_.id)
            if round_.preferred_weekday in ("tuesday", "wednesday", "thursday"):
                assert placement.earliest_date.weekday() in (1, 2, 3), (cup_id, round_.id)


def test_finals_are_a_single_date(frame):
    for schedule in frame.cup_schedules:
        final = schedule.rounds[-1]
        assert final.venue_type == "neutral"
        assert len(set(final.dates.values())) == 1, schedule.competition_id


def test_no_cup_date_is_within_rest_days_of_an_entrants_uefa_leg(frame):
    """The forced Round 1 is the one known exception; it is warned about."""
    for schedule in frame.cup_schedules:
        for placement in schedule.rounds:
            if (schedule.competition_id, placement.round_id) == ("cup_men_2027", "r1"):
                continue
            for team, day in placement.dates.items():
                for leg in frame.commitments.hard_for(team):
                    if leg.source != "uefa":
                        continue
                    gap = abs((day - leg.date).days) - 1
                    assert gap >= max(schedule.min_rest_days, leg.min_rest_days), (
                        schedule.competition_id,
                        placement.round_id,
                        team,
                        day,
                        leg.date,
                    )


def test_validate_warns_when_a_forced_cup_date_sits_in_a_uefa_week(frame_and_warnings):
    _, warnings = frame_and_warnings
    assert any(
        "Round 1" in w and "forced to 2026-08-22" in w and "UEFA week" in w for w in warnings
    )


def test_blackouts_cover_the_2027_dates_the_cups_run_into(season):
    assert date(2027, 5, 17) in season.blacked_out_dates
    assert date(2027, 3, 26) in season.blacked_out_dates  # Good Friday


def test_cup_dates_avoid_blackouts_including_2027(world, season, frame):
    for cup_id in season.cup_competitions:
        cup = world.competition(cup_id)
        blocked = set(season.blacked_out_dates) | set(season.scoped_blackout_dates(cup))
        for day in frame.cup_days_of(competition_id=cup_id):
            assert day.date not in blocked, (cup_id, day.date)


def test_only_hard_cup_weekends_block_league_rounds(frame):
    """The semi-final is reached with probability 0.25 at best, so its Saturday
    is contingent and stays available to the league (#8); the entry round is not."""
    women = frame.cup_days_of(gender="women", kind="cup_weekend")
    semi = next(d for d in women if d.competition_id == "cup_women_2026" and d.round_id == "sf")
    assert semi.date == date(2026, 6, 27) and semi.hard_teams == ()
    assert date(2026, 6, 27) not in frame.cup_weekend_dates(gender="women")

    round_one = next(
        d for d in frame.cup_days_of(gender="men") if d.competition_id == "cup_men_2027" and d.round_id == "r1"
    )
    assert set(round_one.hard_teams) == set(round_one.teams)
    assert date(2026, 8, 22) in frame.cup_weekend_dates(gender="men")


def test_league_anchors_skip_cup_weekends_when_the_calendar_excludes_them(world, season, frame):
    """The women's 2026 semi-final falls on a Toppserien Saturday. The frame
    supplies the dates to exclude; the solvers don't pass them yet (slot
    planning, #6)."""
    league = world.competition("toppserien_2026")
    blocks = {
        d.date: d.label for d in frame.cup_days_of(gender="women", kind="cup_weekend")
    }
    assert date(2026, 6, 27) in blocks

    calendar = build_calendar(world, season).narrowed(
        league.start or season.start, league.end or season.end
    )
    count = 22
    plain = anchor_dates(calendar, league.preferred_weekday, count, league.min_gap_days)
    clear = anchor_dates(calendar.without(blocks), league.preferred_weekday, count, league.min_gap_days)
    assert date(2026, 6, 27) in calendar.dates_on_weekday("saturday")
    assert not set(clear) & set(blocks)
    assert set(plain) & set(blocks), "the unfiltered anchors should hit a cup weekend"


def test_two_cups_on_one_weekend_date_keep_both_labels():
    from terminliste.frame import CupDay, SeasonFrame

    day = date(2026, 5, 9)
    frame = SeasonFrame(
        cup_days=[
            CupDay("a", "men", "final", "A: Final", day, 2, ("t1",), ("t1",)),
            CupDay("b", "men", "final", "B: Final", day, 2, ("t2",), ("t2",)),
        ]
    )
    assert frame.cup_weekend_blocks(gender="men") == {day: "A: Final; B: Final"}
