"""Kickoff-time assignment: final round forced, requirements honoured, rest deterministic."""

from __future__ import annotations

from datetime import date

import factories as f
from terminliste.model.schema import FixedRequirement
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
