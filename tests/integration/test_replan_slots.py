"""Slot planning and both solvers around frozen rounds (#13)."""

from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from terminliste.external_schedule import ExternalSchedule
from terminliste.frame import build_season_frame
from terminliste.model.calendar import build_calendar
from terminliste.model.schema import fixture_identity
from terminliste.replan import build_replan_spec, strip_past
from terminliste.scoring.base import EvalContext, evaluate
from terminliste.scoring.registry import build_constraints
from terminliste.slot_planner import plan_slots
from terminliste.solvers import SolveRequest, get_scheduler
from terminliste.solvers.greedy import build_initial_schedule

FREEZE = date(2026, 8, 15)


@pytest.fixture(scope="module")
def setup(world, season, competitions, travel):
    frame, _ = build_season_frame(world, season)
    matches, _, _ = build_initial_schedule(
        world,
        season,
        competitions,
        seed=42,
        cup_schedules=frame.cup_schedules,
        commitments=frame.commitments,
    )
    spec = build_replan_spec(competitions, ExternalSchedule(matches, has_rounds=True), FREEZE)
    past_free = strip_past(frame.commitments, spec.earliest)
    frame = dataclasses.replace(frame, commitments=past_free)
    ctx = EvalContext(world=world, season=season, travel=travel)
    constraints = build_constraints(
        world, season, competitions, past_free, reference=spec.stability_reference
    )
    return frame, matches, spec, ctx, constraints


def _request(world, season, competitions, setup, budget):
    frame, _, spec, ctx, constraints = setup
    return SolveRequest(
        world=world,
        season=season,
        competitions=competitions,
        constraints=constraints,
        ctx=ctx,
        top_n=1,
        time_budget_s=budget,
        cup_schedules=frame.cup_schedules,
        commitments=frame.commitments,
        frame=frame,
        replan=spec,
    )


def test_frozen_rounds_keep_their_slots(world, season, competitions, setup):
    frame, _, spec, _, _ = setup
    plans = plan_slots(
        world, season, competitions, build_calendar(world, season), frame, spec.slot_freezes
    )

    for competition_id, freeze in spec.slot_freezes.items():
        assert freeze.pinned
        for round_index, slot in freeze.pinned.items():
            assert plans[competition_id].chosen[round_index] == slot
        open_slots = [s for r, s in enumerate(plans[competition_id].chosen) if r not in freeze.pinned]
        assert all(s.start >= FREEZE for s in open_slots)


def test_an_unchanged_frame_keeps_every_open_round_where_it_was(
    world, season, competitions, setup
):
    frame, _, spec, _, _ = setup
    plans = plan_slots(
        world, season, competitions, build_calendar(world, season), frame, spec.slot_freezes
    )

    for competition_id, freeze in spec.slot_freezes.items():
        moved = [
            r
            for r, before in freeze.previous.items()
            if plans[competition_id].chosen[r].start > before.end
            or plans[competition_id].chosen[r].end < before.start
        ]
        assert len(moved) <= 2, (competition_id, moved)


def test_local_search_never_touches_a_frozen_match(world, season, competitions, setup):
    _, matches, spec, _, _ = setup
    request = _request(world, season, competitions, setup, budget=8)
    result = get_scheduler("local").solve(request)
    after = {fixture_identity(m): m for m in spec.restore_frozen(result.best.matches)}

    for identity, frozen in spec.frozen.items():
        assert after[identity] == frozen
    assert len(after) == len(matches)


def test_cpsat_fixes_frozen_matches(world, season, competitions, setup):
    pytest.importorskip("ortools")
    _, _, spec, _, _ = setup
    request = _request(world, season, competitions, setup, budget=40)
    result = get_scheduler("cpsat").solve(request)

    assert result.best is not None, result.notes
    after = {fixture_identity(m): m for m in result.best.matches}
    for identity, frozen in spec.frozen.items():
        assert after[identity].date == frozen.date
        assert after[identity].home_team == frozen.home_team
    assert evaluate(result.best.matches, setup[4], setup[3]).soft_results()
