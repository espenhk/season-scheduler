"""Resolving a cup's declared rounds (forced dates / windows) to real dates."""

from __future__ import annotations

from datetime import date, timedelta

import factories as f
import pytest

from terminliste.model.schema import Competition, CupRound, DatedNote
from terminliste.rounds.cup_schedule import (
    CupSchedulingError,
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
        cup_rounds=[
            f.cup_round("r1", forced_date=date(2026, 8, 22), name="Round 1"),
            f.cup_round(
                "r2",
                window_start=date(2026, 8, 24),
                window_end=date(2026, 9, 30),
                granularity="month",
                name="Round 2",
                preferred_weekday="friday",
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


# -- schedule_cup: one date per windowed round -------------------------------


def test_windowed_round_puts_every_team_on_one_preferred_weekday_date():
    cup = f.competition(
        "cup",
        [f"t{i}" for i in range(8)],
        format="cup",
        cup_rounds=[
            f.cup_round(
                "r1",
                window_start=date(2026, 9, 3),  # a Thursday
                window_end=date(2026, 9, 30),
                granularity="month",
                name="Round 1",
                preferred_weekday="tuesday",
            )
        ],
    )
    schedule, warnings = schedule_cup(cup, blackouts=set())
    placement = schedule.rounds[0]
    assert set(placement.dates.values()) == {date(2026, 9, 8)}
    assert placement.spread_days == 0
    assert warnings == []


def test_windowed_round_skips_a_blacked_out_preferred_weekday():
    cup = f.competition(
        "cup",
        ["t1"],
        format="cup",
        cup_rounds=[
            f.cup_round(
                "r1",
                window_start=date(2026, 9, 1),
                window_end=date(2026, 9, 30),
                name="Round 1",
            )
        ],
    )
    schedule, warnings = schedule_cup(cup, blackouts={date(2026, 9, 2)})
    assert schedule.rounds[0].earliest_date == date(2026, 9, 9)
    assert warnings == []


def test_windowed_round_falls_back_to_another_midweek_day_never_a_weekend():
    cup = f.competition(
        "cup",
        ["t1"],
        format="cup",
        cup_rounds=[
            f.cup_round(
                "r1",
                window_start=date(2026, 9, 1),  # Tue; the window holds one Wednesday
                window_end=date(2026, 9, 6),
                name="Round 1",
            )
        ],
    )
    schedule, warnings = schedule_cup(cup, blackouts={date(2026, 9, 2)})
    assert schedule.rounds[0].earliest_date == date(2026, 9, 1)
    assert warnings == []


def test_windowed_round_without_a_midweek_day_warns_and_settles_for_the_weekend():
    cup = f.competition(
        "cup",
        ["t1"],
        format="cup",
        cup_rounds=[
            f.cup_round(
                "r1", window_start=date(2026, 9, 4), window_end=date(2026, 9, 6), name="Round 1"
            )
        ],
    )
    schedule, warnings = schedule_cup(cup, blackouts=set())
    assert schedule.rounds[0].earliest_date == date(2026, 9, 4)
    assert warnings and "Round 1" in warnings[0]


def test_windowed_round_avoids_every_entrants_uefa_leg():
    cup = f.competition(
        "cup",
        ["t1", "t2"],
        format="cup",
        min_rest_days=2,
        cup_rounds=[
            f.cup_round(
                "r1", window_start=date(2026, 9, 1), window_end=date(2026, 9, 30), name="Round 1"
            )
        ],
    )
    commitments = f.commitment_set(
        _uefa("t1", date(2026, 9, 2)),  # kills Wed 2 Sep and its rest days
        _uefa("t2", date(2026, 9, 9)),  # kills Wed 9 Sep
    )
    schedule, warnings = schedule_cup(cup, blackouts=set(), european_commitments=commitments)
    assert schedule.rounds[0].earliest_date == date(2026, 9, 16)
    assert warnings == []


def test_windowed_round_ignores_a_team_with_an_override_when_dodging_uefa():
    cup = f.competition(
        "cup",
        ["t1", "t2"],
        format="cup",
        cup_rounds=[
            f.cup_round(
                "r1",
                window_start=date(2026, 9, 1),
                window_end=date(2026, 9, 30),
                name="Round 1",
                team_overrides={"t1": date(2026, 9, 10)},
            )
        ],
    )
    commitments = f.commitment_set(_uefa("t1", date(2026, 9, 2)))
    schedule, _ = schedule_cup(cup, blackouts=set(), european_commitments=commitments)
    assert schedule.rounds[0].dates == {"t1": date(2026, 9, 10), "t2": date(2026, 9, 2)}


def test_forced_round_inside_a_uefa_week_warns_but_keeps_its_date():
    cup = f.competition(
        "cup",
        ["t1", "t2"],
        format="cup",
        cup_rounds=[f.cup_round("r1", forced_date=date(2026, 8, 22), name="Round 1")],
    )
    commitments = f.commitment_set(_uefa("t1", date(2026, 8, 20)))
    schedule, warnings = schedule_cup(cup, blackouts=set(), european_commitments=commitments)
    assert set(schedule.rounds[0].dates.values()) == {date(2026, 8, 22)}
    assert len(warnings) == 1 and "UEFA" in warnings[0] and "t1" in warnings[0]


def test_forced_round_clear_of_uefa_does_not_warn():
    cup = f.competition(
        "cup",
        ["t1"],
        format="cup",
        cup_rounds=[f.cup_round("r1", forced_date=date(2026, 8, 22), name="Round 1")],
    )
    commitments = f.commitment_set(_uefa("t1", date(2026, 8, 27)))
    _, warnings = schedule_cup(cup, blackouts=set(), european_commitments=commitments)
    assert warnings == []


def test_team_override_moves_only_that_team():
    cup = f.competition(
        "cup",
        ["t1", "t2"],
        format="cup",
        cup_rounds=[
            f.cup_round(
                "r1", forced_date=date(2026, 8, 22), name="Round 1",
                team_overrides={"t2": date(2026, 9, 2)},
            )
        ],
    )
    schedule, _ = schedule_cup(cup, blackouts=set())
    assert schedule.rounds[0].dates == {"t1": date(2026, 8, 22), "t2": date(2026, 9, 2)}


def test_team_override_on_a_blackout_date_warns():
    cup = f.competition(
        "cup",
        ["t1", "t2"],
        format="cup",
        cup_rounds=[
            f.cup_round(
                "r1", forced_date=date(2026, 8, 22), name="Round 1",
                team_overrides={"t2": date(2026, 9, 2)},
            )
        ],
    )
    _, warnings = schedule_cup(cup, blackouts={date(2026, 9, 2)})
    assert len(warnings) == 1 and "t2" in warnings[0] and "2026-09-02" in warnings[0]


def test_a_weekend_preference_keeps_the_final_on_one_weekend_date():
    cup = f.competition(
        "cup",
        ["t1", "t2"],
        format="cup",
        cup_rounds=[
            f.cup_round(
                "final",
                window_start=date(2027, 5, 24),
                window_end=date(2027, 5, 31),
                name="Final",
                preferred_weekday="sunday",
            )
        ],
    )
    schedule, _ = schedule_cup(cup, blackouts=set())
    assert set(schedule.rounds[0].dates.values()) == {date(2027, 5, 30)}


def _uefa(team: str, day: date, *, hard: bool = True):
    return f.commitment(team, day, hard=hard, label="x", competition_id="cl")


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
    """When every day in the window is blacked out, placement still
    succeeds — falling back onto the window's first day — but is flagged
    with a warning rather than silently ignoring the blackout."""
    cup = f.competition(
        "cup",
        [f"t{i}" for i in range(4)],
        format="cup",
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
    blackouts = {date(2026, 9, 1) + timedelta(days=i) for i in range(10)}
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


# -- schedule_cups ------------------------------------


def test_schedule_cups_pools_warnings_across_competitions():
    tight = f.competition(
        "tight",
        [f"t{i}" for i in range(4)],
        format="cup",
        cup_rounds=[
            f.cup_round(
                "r1", window_start=date(2026, 9, 1), window_end=date(2026, 9, 2), name="Round 1"
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


def test_schedule_cups_avoids_a_global_blackout_range():
    from terminliste.model.schema import GlobalBlackoutRange

    comp = f.competition(
        "cup1",
        ["t1", "t2"],
        format="cup",
        cup_rounds=[
            f.cup_round(
                "r1", window_start=date(2026, 9, 1), window_end=date(2026, 9, 10), name="Round 1",
                preferred_weekday="friday",
            )
        ],
    )
    season = f.season(
        cup_competitions=["cup1"],
        global_blackout_ranges=[
            GlobalBlackoutRange(start=date(2026, 9, 1), end=date(2026, 9, 3), reason="break")
        ],
    )
    schedules, warnings = schedule_cups([comp], season)
    placement = schedules[0].rounds[0]
    assert placement.earliest_date == date(2026, 9, 4)


def test_schedule_cups_ignores_venue_blackout_ranges():
    """Cups don't book venues — a `venue_blackout_ranges` entry must not
    influence cup round resolution, the same as `venue_blackouts` already
    doesn't (see `schedule_cups`'s docstring)."""
    from terminliste.model.schema import VenueBlackoutRange

    comp = f.competition(
        "cup1",
        ["t1", "t2"],
        format="cup",
        cup_rounds=[
            f.cup_round(
                "r1", window_start=date(2026, 9, 1), window_end=date(2026, 9, 10), name="Round 1",
                preferred_weekday="tuesday",
            )
        ],
    )
    season = f.season(
        cup_competitions=["cup1"],
        venue_blackout_ranges=[
            VenueBlackoutRange(
                venue="v1", start=date(2026, 9, 1), end=date(2026, 9, 3), reason="ground works"
            )
        ],
    )
    schedules, warnings = schedule_cups([comp], season)
    placement = schedules[0].rounds[0]
    assert placement.earliest_date == date(2026, 9, 1)
    assert warnings == []


def test_schedule_cups_applies_blackouts_beyond_the_seasons_own_dates():
    """The cup runs into 2027, past the season's end: 17 May 2027 is still off."""
    comp = f.competition(
        "cup1",
        ["t1"],
        format="cup",
        cup_rounds=[
            f.cup_round(
                "r1", window_start=date(2027, 5, 17), window_end=date(2027, 5, 17), name="Round 1"
            )
        ],
    )
    season = f.season(
        cup_competitions=["cup1"],
        end=date(2026, 12, 6),
        global_blackouts=[DatedNote(date=date(2027, 5, 17))],
    )
    _, warnings = schedule_cups([comp], season)
    assert warnings and "Round 1" in warnings[0]


def test_schedule_cups_applies_gender_scoped_blackouts_to_that_genders_cup():
    from terminliste.model.schema import GlobalBlackoutRange

    def cup(cup_id, gender):
        return f.competition(
            cup_id,
            ["t1"],
            format="cup",
            gender=gender,
            cup_rounds=[
                f.cup_round(
                    "r1", window_start=date(2026, 9, 21), window_end=date(2026, 10, 14), name="R"
                )
            ],
        )

    season = f.season(
        cup_competitions=["m", "w"],
        global_blackout_ranges=[
            GlobalBlackoutRange(
                start=date(2026, 9, 21), end=date(2026, 10, 6), gender="men", reason="FIFA"
            )
        ],
    )
    men, women = schedule_cups([cup("m", "men"), cup("w", "women")], season)[0]
    assert men.rounds[0].earliest_date == date(2026, 10, 7)
    assert women.rounds[0].earliest_date == date(2026, 9, 23)
