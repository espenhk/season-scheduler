"""Round selection for fixed requirements, and the solvers' fallbacks around
it: greedy and CP-SAT agree on which round a requirement belongs to, required
teams are kept from meeting, and CP-SAT keeps a fully blocked slot's days."""

from __future__ import annotations

from datetime import date, timedelta

import factories as f
import pytest

from terminliste.model.calendar import build_calendar
from terminliste.model.schema import DatedNote, FixedRequirement
from terminliste.model.travel import ApiTravelModel
from terminliste.scoring.base import EvalContext
from terminliste.scoring.registry import build_constraints
from terminliste.solvers import cpsat
from terminliste.solvers.base import SolveRequest, resolve_slot_plans, round_for_date
from terminliste.solvers.greedy import (
    _meetings,
    _separate_required_teams,
    align_home_teams_to_requirements,
    pick_fixture_for_requirement,
    plan_competitions,
)

pytest.importorskip("ortools")

TEAMS = [f"t{i}" for i in range(8)]


def _setup(season_kwargs=None):
    venues = [f.venue(f"v{i}", lat=59.0 + i * 0.3, lon=10.0 + i * 0.3) for i in range(8)]
    teams = [f.team(t, f"c{i}", f"v{i}") for i, t in enumerate(TEAMS)]
    clubs = [f.club(f"c{i}", [teams[i]]) for i in range(8)]
    competition = f.competition("men", TEAMS)
    world = f.world(clubs, venues, [competition])
    season = f.season(
        competitions=["men"], start=date(2026, 3, 1), end=date(2026, 9, 30),
        **(season_kwargs or {}),
    )
    return world, season, competition


def _request(world, season, competition, time_budget_s=10.0):
    constraints = build_constraints(world, season, [competition])
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    return SolveRequest(
        world=world, season=season, competitions=[competition], constraints=constraints,
        ctx=ctx, seed=1, top_n=1, time_budget_s=time_budget_s,
    )


def _planned(world, season, competition):
    calendar = build_calendar(world, season)
    request = _request(world, season, competition)
    slot_plans = resolve_slot_plans(request, calendar)
    return plan_competitions(world, season, [competition], calendar, slot_plans=slot_plans)


def test_round_for_date_prefers_the_slot_over_the_nearest_anchor():
    world, season, competition = _setup()
    plan = _planned(world, season, competition)[0]
    day = plan.slots[5].days[-1]
    anchors = dict(plan.anchors)
    anchors[5] = day - timedelta(days=6)
    anchors[6] = day + timedelta(days=1)

    assert round_for_date(plan.slots, anchors, day) == 5
    assert round_for_date({}, anchors, day) == 6


def test_greedy_and_cpsat_pin_a_requirement_to_the_same_round(monkeypatch):
    world, season, competition = _setup()
    probe = _planned(world, season, competition)[0]
    round_index = 5
    day = probe.slots[round_index].days[-1]
    team = next(x for x in probe.fixtures if x.round_index == round_index).home_team
    requirement = FixedRequirement(
        id="r1", date=day, home_team=team, competition="men"
    )
    season = f.season(
        competitions=["men"], start=season.start, end=season.end,
        fixed_requirements=[requirement],
    )

    # Trimming can move a slot's default day away from the requested date, so
    # the nearest anchor belongs to the next round.
    def skewed(*args, **kwargs):
        planned = plan_competitions(*args, **kwargs)
        for plan in planned:
            plan.anchors[round_index] = day - timedelta(days=6)
            plan.anchors[round_index + 1] = day + timedelta(days=1)
        return planned

    monkeypatch.setattr(cpsat, "plan_competitions", skewed)

    planned = skewed(
        world, season, [competition], build_calendar(world, season),
        slot_plans=resolve_slot_plans(
            _request(world, season, competition), build_calendar(world, season)
        ),
    )
    greedy_pick = pick_fixture_for_requirement(planned[0], team, day)
    assert greedy_pick.round_index == round_index

    result = cpsat.CpSatScheduler().solve(_request(world, season, competition))
    assert result.best is not None
    pinned = next(
        m for m in result.best.matches if m.home_team == team and m.date == day
    )
    assert pinned.round_index == greedy_pick.round_index


def test_cpsat_keeps_a_fully_blocked_slot_so_the_scorer_can_report_it():
    from ortools.sat.python import cp_model

    world, season, competition = _setup()
    plan = _planned(world, season, competition)[0]
    blocked_days = plan.slots[3].days
    blocked_season = f.season(
        competitions=["men"], start=season.start, end=season.end,
        global_blackouts=[DatedNote(date=d, reason="blocked") for d in blocked_days],
    )
    request = _request(world, blocked_season, competition)

    built = cpsat._build_model(
        cp_model, request, [plan], build_calendar(world, blocked_season), [], {}
    )

    assert built is not None
    _model, placement, fixtures, _assumptions = built
    for index, (fixture, _, _) in enumerate(fixtures):
        if fixture.round_index == 3:
            assert set(placement[index]) == set(blocked_days)


def _two_required_teams_meeting(plan):
    fixture = next(x for x in plan.fixtures if x.round_index == 2)
    return fixture.home_team, fixture.away_team


def test_separate_required_teams_relabels_a_clashing_pair_apart():
    world, season, competition = _setup()
    plan = _planned(world, season, competition)[0]
    team_a, team_b = _two_required_teams_meeting(plan)
    needs = [(team_a, 2), (team_b, 2)]
    assert _meetings(plan, needs)

    assert _separate_required_teams(plan, needs) is True

    assert _meetings(plan, needs) == []
    pairs = [frozenset((x.home_team, x.away_team)) for x in plan.fixtures]
    assert len(pairs) == len(set(pairs)) * 2  # still a double round-robin


def test_separate_required_teams_leaves_a_plan_without_clashes_alone():
    world, season, competition = _setup()
    plan = _planned(world, season, competition)[0]
    team_a, team_b = _two_required_teams_meeting(plan)
    before = list(plan.fixtures)

    assert _separate_required_teams(plan, [(team_a, 2)]) is False
    assert plan.fixtures == before


def test_separate_required_teams_gives_up_when_no_team_is_free_to_swap():
    world, season, competition = _setup()
    plan = _planned(world, season, competition)[0]
    team_a, team_b = _two_required_teams_meeting(plan)
    needs = [(team_a, 2), (team_b, 2)] + [
        (t, 9) for t in TEAMS if t not in (team_a, team_b)
    ]
    before = list(plan.fixtures)

    assert _separate_required_teams(plan, needs) is False
    assert plan.fixtures == before


def test_two_required_home_teams_that_meet_both_end_up_home_in_the_round():
    world, season, competition = _setup()
    plan = _planned(world, season, competition)[0]
    team_a, team_b = _two_required_teams_meeting(plan)
    day = plan.slots[2].days[0]
    season = f.season(
        competitions=["men"], start=season.start, end=season.end,
        fixed_requirements=[
            FixedRequirement(id="a", date=day, home_team=team_a, competition="men"),
            FixedRequirement(id="b", date=day, home_team=team_b, competition="men"),
        ],
    )

    align_home_teams_to_requirements(season, [plan])

    home_in_round = {x.home_team for x in plan.fixtures if x.round_index == 2}
    assert {team_a, team_b} <= home_in_round
