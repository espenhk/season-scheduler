"""Kickoff-time assignment: final round forced, requirements honoured, rest deterministic."""

from __future__ import annotations

from datetime import date

import factories as f
from terminliste.model.schema import FixedRequirement, TvTimeSpread
from terminliste.rounds.kickoff import assign_kickoff_times, final_round_index


def test_final_round_index_is_the_last_round_of_the_last_leg():
    comp = f.competition("comp", ["t1", "t2", "t3", "t4"], rounds_per_pairing=2)
    assert final_round_index(comp) == comp.rounds - 1 == 5


def test_assign_kickoff_times_forces_the_final_round_slot():
    comp = f.competition(
        "comp", ["t1", "t2"], rounds_per_pairing=2, final_round_kickoff_time="19:00"
    )
    final_round = final_round_index(comp)
    matches = [
        f.match("comp", "t1", "t2", date(2026, 3, 1), "v1", round_index=0),
        f.match("comp", "t2", "t1", date(2026, 12, 6), "v2", round_index=final_round),
    ]
    result = assign_kickoff_times(matches, [comp])
    assert result[1].kickoff_time == "19:00"


def test_assign_kickoff_times_is_deterministic_and_stable():
    comp = f.competition("comp", ["t1", "t2", "t3", "t4"], kickoff_slots=["14:00", "20:00"])
    matches = [
        f.match("comp", "t1", "t2", date(2026, 4, 5), "v1", round_index=0),
        f.match("comp", "t3", "t4", date(2026, 4, 5), "v3", round_index=0),
    ]
    first = assign_kickoff_times(matches, [comp])
    second = assign_kickoff_times(matches, [comp])
    assert [m.kickoff_time for m in first] == [m.kickoff_time for m in second]
    for m in first:
        assert m.kickoff_time in comp.kickoff_slots


def test_assign_kickoff_times_honours_an_explicit_fixed_requirement():
    comp = f.competition("comp", ["t1", "t2"])
    requirement = FixedRequirement(
        id="midnight_sun",
        date=date(2026, 7, 11),
        home_team="t1",
        competition="comp",
        hard=True,
        kickoff_time="22:00",
    )
    matches = [f.match("comp", "t1", "t2", date(2026, 7, 11), "v1", round_index=3)]
    result = assign_kickoff_times(matches, [comp], requirements=[requirement])
    assert result[0].kickoff_time == "22:00"


# -- tv_time_spread (issue #76) ----------------------------------------------


def test_tv_time_spread_shapes_a_round_early_late_and_primary():
    spread = TvTimeSpread(
        primary_kickoff_time="17:00", early_kickoff_time="14:30", late_kickoff_time="19:15"
    )
    comp = f.competition(
        "comp", ["t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8"],
        preferred_weekday="sunday", tv_time_spread=spread,
    )
    sunday = date(2026, 6, 7)
    assert sunday.weekday() == 6
    matches = [
        f.match("comp", f"t{i}", f"t{i + 1}", sunday, f"v{i}", round_index=0)
        for i in (1, 3, 5, 7)
    ]
    result = assign_kickoff_times(matches, [comp])
    times = [m.kickoff_time for m in result]
    assert times.count("14:30") == 1
    assert times.count("19:15") == 1
    assert times.count("17:00") == 2


def test_tv_time_spread_is_deterministic_and_stable():
    spread = TvTimeSpread()
    comp = f.competition(
        "comp", ["t1", "t2", "t3", "t4"], preferred_weekday="sunday", tv_time_spread=spread
    )
    sunday = date(2026, 6, 7)
    matches = [
        f.match("comp", "t1", "t2", sunday, "v1", round_index=0),
        f.match("comp", "t3", "t4", sunday, "v3", round_index=0),
    ]
    first = assign_kickoff_times(matches, [comp])
    second = assign_kickoff_times(matches, [comp])
    assert [m.kickoff_time for m in first] == [m.kickoff_time for m in second]
    assert {m.kickoff_time for m in first} == {"14:30", "19:15"}


def test_tv_time_spread_single_match_round_gets_the_primary_time():
    spread = TvTimeSpread(primary_kickoff_time="17:00")
    comp = f.competition("comp", ["t1", "t2"], preferred_weekday="sunday", tv_time_spread=spread)
    sunday = date(2026, 6, 7)
    matches = [f.match("comp", "t1", "t2", sunday, "v1", round_index=0)]
    result = assign_kickoff_times(matches, [comp])
    assert result[0].kickoff_time == "17:00"


def test_tv_time_spread_leaves_off_weekday_matches_to_kickoff_slots():
    spread = TvTimeSpread()
    comp = f.competition(
        "comp", ["t1", "t2"], preferred_weekday="sunday", tv_time_spread=spread,
        kickoff_slots=["11:00"],
    )
    monday = date(2026, 6, 8)
    matches = [f.match("comp", "t1", "t2", monday, "v1", round_index=0)]
    result = assign_kickoff_times(matches, [comp])
    assert result[0].kickoff_time == "11:00"


def test_tv_time_spread_does_not_override_the_final_round_slot():
    spread = TvTimeSpread(primary_kickoff_time="17:00")
    comp = f.competition(
        "comp", ["t1", "t2", "t3", "t4"], preferred_weekday="sunday", tv_time_spread=spread,
        final_round_kickoff_time="18:00",
    )
    final_round = final_round_index(comp)
    sunday = date(2026, 12, 6)
    matches = [f.match("comp", "t1", "t2", sunday, "v1", round_index=final_round)]
    result = assign_kickoff_times(matches, [comp])
    assert result[0].kickoff_time == "18:00"
