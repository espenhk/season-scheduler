"""Resolving a cup's declared rounds (forced dates / windows) to real dates."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import factories as f
from terminliste.model.schema import Competition, CupRound, DatedNote
from terminliste.rounds.cup_schedule import (
    CupSchedulingError,
    resolved_cup_windows,
    schedule_cup,
    schedule_cups,
)


# -- CupRound itself: forced_date XOR window ---------------------------------


def test_cup_round_needs_forced_date_or_window():
    with pytest.raises(ValueError, match="needs either forced_date or a window"):
        CupRound(id="r1", name="Round 1")


def test_cup_round_rejects_both_forced_date_and_window():
    with pytest.raises(ValueError, match="not both"):
        CupRound(
            id="r1",
            name="Round 1",
            forced_date=date(2026, 8, 22),
            window_start=date(2026, 9, 1),
            window_end=date(2026, 9, 30),
        )


def test_cup_round_rejects_a_reversed_window():
    with pytest.raises(ValueError, match="before window_start"):
        CupRound(
            id="r1", name="Round 1", window_start=date(2026, 9, 30), window_end=date(2026, 9, 1)
        )


# -- schedule_cup: ordering ---------------------------------------------------


def test_consecutive_forced_rounds_resolve_in_order():
    cup = f.competition(
        "cup",
        ["t1", "t2"],
        format="cup",
        min_rest_days=3,
        cup_rounds=[
            f.cup_round("r1", forced_date=date(2026, 8, 22), name="Round 1"),
            f.cup_round("r2", forced_date=date(2026, 9, 16), name="Round 2"),
        ],
    )
    schedule, warnings = schedule_cup(cup, blackouts=set())
    assert [p.round_id for p in schedule.rounds] == ["r1", "r2"]
    assert schedule.rounds[0].earliest_date == date(2026, 8, 22)
    assert schedule.rounds[1].earliest_date >= date(2026, 9, 16)
    assert warnings == []


def test_forced_round_not_after_the_previous_round_is_rejected():
    cup = f.competition(
        "cup",
        ["t1"],
        format="cup",
        cup_rounds=[
            f.cup_round("r1", forced_date=date(2026, 9, 1), name="Round 1"),
            # Forced to a date before round 1 even happens.
            f.cup_round("r2", forced_date=date(2026, 8, 1), name="Round 2"),
        ],
    )
    with pytest.raises(CupSchedulingError, match="does not leave room"):
        schedule_cup(cup, blackouts=set())


def test_windowed_round_starts_at_the_earliest_point_the_gap_allows():
    cup = f.competition(
        "cup",
        ["t1"],
        format="cup",
        min_rest_days=5,
        match_window_days=0,  # one date per round, easiest to assert on
        cup_rounds=[
            f.cup_round("r1", forced_date=date(2026, 8, 22), name="Round 1"),
            f.cup_round(
                "r2",
                window_start=date(2026, 8, 24),
                window_end=date(2026, 9, 30),
                granularity="month",
                name="Round 2",
            ),
        ],
    )
    schedule, warnings = schedule_cup(cup, blackouts=set())
    # Round 1 lands on 22 Aug; 5 full rest days (23-27 Aug) means round 2
    # cannot start before 28 Aug, even though its own window opens on the 24th.
    assert schedule.rounds[1].earliest_date == date(2026, 8, 28)
    assert warnings == []


def test_windowed_round_too_narrow_after_the_gap_is_rejected():
    cup = f.competition(
        "cup",
        ["t1"],
        format="cup",
        min_rest_days=10,
        cup_rounds=[
            f.cup_round("r1", forced_date=date(2026, 8, 22), name="Round 1"),
            # Window closes well before the 10-day gap from round 1 elapses.
            f.cup_round(
                "r2",
                window_start=date(2026, 8, 23),
                window_end=date(2026, 8, 26),
                granularity="week",
                name="Round 2",
            ),
        ],
    )
    with pytest.raises(CupSchedulingError, match="leaves no room"):
        schedule_cup(cup, blackouts=set())


# -- schedule_cup: team spread within a windowed round -----------------------


def test_teams_spread_across_a_windowed_rounds_span_not_all_on_one_day():
    cup = f.competition(
        "cup",
        [f"t{i}" for i in range(8)],
        format="cup",
        match_window_days=3,
        cup_rounds=[
            f.cup_round(
                "r1",
                window_start=date(2026, 9, 1),
                window_end=date(2026, 9, 30),
                granularity="month",
                name="Round 1",
            )
        ],
    )
    schedule, warnings = schedule_cup(cup, blackouts=set())
    placement = schedule.rounds[0]
    assert len(set(placement.dates.values())) > 1, "8 teams over several legal days should not clump"
    assert placement.spread_days <= 6
    assert warnings == []


# -- schedule_cup: home/away resolution --------------------------------------


def _n_round_cup(n: int) -> Competition:
    """A cup with `n` windowed rounds, each a month apart, far enough apart
    that ordering/rest-gap constraints never come into play — these tests are
    only about the home/away resolution, not placement."""
    rounds = [
        f.cup_round(
            f"r{i}",
            window_start=date(2026, 1, 1) + timedelta(days=i * 30),
            window_end=date(2026, 1, 28) + timedelta(days=i * 30),
            granularity="month",
            name=f"Round {i + 1}",
        )
        for i in range(n)
    ]
    return f.competition("cup", ["t1", "t2"], format="cup", cup_rounds=rounds)


def test_first_three_rounds_are_always_away():
    cup = _n_round_cup(7)
    schedule, _ = schedule_cup(cup, blackouts=set())
    assert [p.venue_type for p in schedule.rounds[:3]] == ["away", "away", "away"]


def test_rounds_after_the_third_alternate_home_and_away():
    cup = _n_round_cup(7)
    schedule, _ = schedule_cup(cup, blackouts=set())
    # Round 4 (index 3) starts the alternation at home, then away, home, ...
    assert [p.venue_type for p in schedule.rounds[3:6]] == ["home", "away", "home"]


def test_final_round_is_always_neutral_even_if_the_alternation_would_land_on_home():
    cup = _n_round_cup(7)
    schedule, _ = schedule_cup(cup, blackouts=set())
    assert schedule.rounds[-1].venue_type == "neutral"


def test_final_is_neutral_even_in_a_cup_short_enough_to_still_be_in_the_away_stretch():
    """A hypothetical 2-round cup: round 1 would normally be "away" (first
    three rounds), but it's also the last round, so the final-is-neutral
    override wins."""
    cup = _n_round_cup(2)
    schedule, _ = schedule_cup(cup, blackouts=set())
    assert [p.venue_type for p in schedule.rounds] == ["away", "neutral"]


def test_a_single_round_cup_is_just_the_final_and_is_neutral():
    cup = _n_round_cup(1)
    schedule, _ = schedule_cup(cup, blackouts=set())
    assert schedule.rounds[0].venue_type == "neutral"


# -- schedule_cup: a forced round never moves -------------------------------


def test_a_forced_round_puts_every_team_on_the_confirmed_date_regardless_of_match_window_days():
    """match_window_days would spread a windowed round's placements across
    several days — it must have no effect on a forced one, since a confirmed
    date isn't something placement is free to drift away from."""
    cup = f.competition(
        "cup",
        ["t1", "t2", "t3"],
        format="cup",
        match_window_days=3,
        cup_rounds=[f.cup_round("r1", forced_date=date(2026, 8, 22), name="Round 1")],
    )
    schedule, warnings = schedule_cup(cup, blackouts=set())
    assert set(schedule.rounds[0].dates.values()) == {date(2026, 8, 22)}
    assert warnings == []


def test_forced_round_never_drifts_even_if_the_confirmed_date_is_blacked_out():
    """A blacked-out confirmed date still gets used exactly as announced —
    falling back to a nearby non-blackout day would silently move a date
    that, by definition, cannot move. It's flagged with a warning instead."""
    cup = f.competition(
        "cup",
        ["t1", "t2"],
        format="cup",
        match_window_days=3,
        cup_rounds=[f.cup_round("r1", forced_date=date(2026, 8, 22), name="Round 1")],
    )
    schedule, warnings = schedule_cup(cup, blackouts={date(2026, 8, 22)})
    assert set(schedule.rounds[0].dates.values()) == {date(2026, 8, 22)}
    assert warnings and "Round 1" in warnings[0]


def test_forced_round_too_close_to_the_previous_round_honours_min_rest_days():
    """The old check only required the forced date to be strictly after the
    previous round's last date — it ignored min_rest_days entirely. A forced
    date that violates the configured rest gap is a genuine data conflict,
    not something placement can resolve by moving a date that cannot move."""
    cup = f.competition(
        "cup",
        ["t1"],
        format="cup",
        min_rest_days=10,
        cup_rounds=[
            f.cup_round("r1", forced_date=date(2026, 8, 22), name="Round 1"),
            # Only 5 days after round 1 — short of the 10-day minimum gap.
            f.cup_round("r2", forced_date=date(2026, 8, 27), name="Round 2"),
        ],
    )
    with pytest.raises(CupSchedulingError, match="does not leave room"):
        schedule_cup(cup, blackouts=set())


def test_fully_blacked_out_window_falls_back_to_the_anchor_with_a_warning():
    """When every legal candidate day is blacked out, placement still
    succeeds — falling back onto the round's own anchor — but is flagged
    with a warning rather than silently ignoring the blackout."""
    cup = f.competition(
        "cup",
        [f"t{i}" for i in range(4)],
        format="cup",
        match_window_days=1,
        cup_rounds=[
            f.cup_round(
                "r1",
                window_start=date(2026, 9, 1),
                window_end=date(2026, 9, 10),
                granularity="week",
                name="Round 1",
            )
        ],
    )
    # Blacks out both candidate days: the anchor (window_start) and the one
    # day after it that match_window_days=1 would otherwise reach.
    blackouts = {date(2026, 9, 1), date(2026, 9, 2)}
    schedule, warnings = schedule_cup(cup, blackouts=blackouts)
    assert set(schedule.rounds[0].dates.values()) == {date(2026, 9, 1)}
    assert warnings and "Round 1" in warnings[0]


def test_schedule_cup_rejects_a_league_competition():
    league = f.competition("league", ["t1", "t2"])
    with pytest.raises(CupSchedulingError, match="not a cup"):
        schedule_cup(league, blackouts=set())


def test_schedule_cup_rejects_a_cup_with_no_rounds():
    cup = f.competition("cup", ["t1"], format="cup")
    with pytest.raises(CupSchedulingError, match="no cup_rounds"):
        schedule_cup(cup, blackouts=set())


# -- schedule_cups / resolved_cup_windows ------------------------------------


def test_schedule_cups_pools_warnings_across_competitions():
    tight = f.competition(
        "tight",
        [f"t{i}" for i in range(4)],
        format="cup",
        match_window_days=1,
        cup_rounds=[
            f.cup_round(
                "r1", window_start=date(2026, 9, 1), window_end=date(2026, 9, 10), name="Round 1"
            )
        ],
    )
    roomy = f.competition(
        "roomy",
        ["u1", "u2"],
        format="cup",
        cup_rounds=[f.cup_round("r1", forced_date=date(2026, 8, 22), name="Round 1")],
    )
    season = f.season(
        cup_competitions=["tight", "roomy"],
        global_blackouts=[
            DatedNote(date=date(2026, 9, 1)),
            DatedNote(date=date(2026, 9, 2)),
        ],
    )
    schedules, warnings = schedule_cups([tight, roomy], season)
    assert len(schedules) == 2
    assert any("tight" in w or "Round 1" in w for w in warnings)


def test_resolved_cup_windows_reads_each_teams_own_date():
    schedule = f.cup_schedule(
        "cup",
        min_rest_days=4,
        rounds=[
            f.cup_placement("r1", {"t1": date(2026, 8, 22), "t2": date(2026, 8, 24)}, "Round 1"),
            f.cup_placement("r2", {"t1": date(2026, 9, 16)}, "Round 2"),
        ],
    )
    windows = resolved_cup_windows([schedule])
    assert windows["t1"] == [(date(2026, 8, 22), 4), (date(2026, 9, 16), 4)]
    assert windows["t2"] == [(date(2026, 8, 24), 4)]
