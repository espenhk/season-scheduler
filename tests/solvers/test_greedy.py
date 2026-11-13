"""Unit tests for `solvers/greedy.py`'s round-pin resolution (#94).

A pinned round — a league's final, or a hard `FullRoundRequirement`'s — must
clear hard cup and European commitments the same way every other
fixture's candidate window does. `resolve_round_pins` is shared by both
solver backends.
"""

from __future__ import annotations

from datetime import date, timedelta

import factories as f

from terminliste.display import short_date
from terminliste.model.calendar import build_calendar, calendars_by_competition
from terminliste.solvers.greedy import plan_competitions, resolve_round_pins


def _world_and_plan(competition):
    venues = [f.venue("v0"), f.venue("v1"), f.venue("v2"), f.venue("v3")]
    teams = [f.team(f"t{i}", f"c{i}", f"v{i}") for i in range(4)]
    clubs = [f.club(f"c{i}", [teams[i]]) for i in range(4)]
    world = f.world(clubs, venues, [competition])
    season = f.season(competitions=[competition.id], start=date(2026, 3, 1), end=date(2026, 9, 30))
    calendar = build_calendar(world, season)
    by_competition = calendars_by_competition(calendar, [competition])
    planned = plan_competitions(world, season, [competition], calendar)
    return season, planned, by_competition


def _final_round(planned, competition_id):
    plan = next(p for p in planned if p.competition.id == competition_id)
    final_round = plan.competition.rounds - 1
    return final_round, plan.anchors[final_round]


def test_final_round_pin_moves_off_a_date_that_conflicts_with_a_european_commitment():
    competition = f.competition("men", [f"t{i}" for i in range(4)])
    season, planned, by_competition = _world_and_plan(competition)
    final_round, anchor = _final_round(planned, "men")

    # A commitment on the final round's anchor that also blocks the day
    # either side: the pin moves to another day of the same slot.
    commitments = f.commitment_set(f.commitment("t0", anchor, min_rest_days=1))

    pins, warnings = resolve_round_pins(season, planned, by_competition, commitments)

    chosen = pins[("men", final_round)]
    assert chosen != anchor
    assert planned[0].slots[final_round].contains(chosen)
    assert not commitments.blocked("t0", chosen)
    assert warnings == []


def test_final_round_pin_stays_put_and_flags_when_every_team_is_committed():
    """Every team in the competition has a commitment on the anchor and every
    date nearby, so no widened search can find daylight — the pin has to
    stay and the conflict must be reported, not silently dropped."""
    competition = f.competition("men", [f"t{i}" for i in range(4)])
    season, planned, by_competition = _world_and_plan(competition)
    final_round, anchor = _final_round(planned, "men")

    commitments = f.commitment_set(
        *(
            f.commitment(f"t{i}", anchor + timedelta(days=offset), min_rest_days=3)
            for i in range(4)
            for offset in range(-9, 10, 6)
        )
    )

    pins, warnings = resolve_round_pins(season, planned, by_competition, commitments)

    chosen = pins[("men", final_round)]
    assert chosen == anchor
    assert len(warnings) == 1
    assert "men" in warnings[0] and short_date(anchor) in warnings[0]


def test_full_round_requirement_pin_cannot_move_and_flags_instead():
    """A `FullRoundRequirement`'s date is itself a hard requirement — unlike
    the final round's anchor, it must not be moved even when it conflicts;
    the conflict can only be flagged."""
    from terminliste.model.schema import FullRoundRequirement

    competition = f.competition("men", [f"t{i}" for i in range(4)])
    season, planned, by_competition = _world_and_plan(competition)
    _, anchor = _final_round(planned, "men")
    # Pick a date near — but not on — the final round anchor so the full-round
    # requirement resolves to a different, earlier round.
    required_date = anchor - timedelta(days=21)
    season = f.season(
        competitions=["men"],
        start=season.start,
        end=season.end,
        full_round_requirements=[
            FullRoundRequirement(id="fr1", date=required_date, competition="men")
        ],
    )
    commitments = f.commitment_set(f.commitment("t0", required_date, min_rest_days=3))

    pins, warnings = resolve_round_pins(season, planned, by_competition, commitments)

    round_index = min(planned[0].anchors, key=lambda r: abs((planned[0].anchors[r] - required_date).days))
    assert pins[("men", round_index)] == required_date
    assert len(warnings) == 1
    assert "cannot move" in warnings[0]


def test_cup_conflict_also_moves_the_pin():
    """`_round_clear` reads cup commitments too, not just European ones."""
    competition = f.competition("men", [f"t{i}" for i in range(4)])
    season, planned, by_competition = _world_and_plan(competition)
    final_round, anchor = _final_round(planned, "men")

    commitments = f.commitment_set(f.commitment("t1", anchor, min_rest_days=1, source="cup"))

    pins, warnings = resolve_round_pins(season, planned, by_competition, commitments)

    chosen = pins[("men", final_round)]
    assert chosen != anchor
    assert not commitments.blocked("t1", chosen)
    assert warnings == []
