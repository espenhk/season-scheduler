"""Local search's inner loop (#11): reproducible runs, pacing to the budget,
adaptive operator weights, the live-index neighbour check and the day domains."""

from __future__ import annotations

import random
from datetime import date, timedelta

import factories as f
import pytest
from test_solver_contract import _small_world

from terminliste.model.calendar import build_calendar, calendars_by_competition
from terminliste.model.slots import RoundSlot
from terminliste.model.travel import ApiTravelModel
from terminliste.rounds.commitments import CommitmentSet
from terminliste.scoring.base import EvalContext
from terminliste.scoring.incremental import DeltaScorer
from terminliste.scoring.registry import build_constraints
from terminliste.slot_planner import plan_slots
from terminliste.solvers import local_search as ls
from terminliste.solvers.base import SolveRequest
from terminliste.solvers.domains import DayDomains
from terminliste.solvers.greedy import build_initial_schedule


def _request(seed: int = 7, **overrides) -> SolveRequest:
    world, season, competitions = _small_world()
    return SolveRequest(
        world=world,
        season=season,
        competitions=competitions,
        constraints=build_constraints(world, season, competitions),
        ctx=EvalContext(world=world, season=season, travel=ApiTravelModel(world)),
        seed=seed,
        top_n=2,
        **overrides,
    )


def test_the_same_seed_and_move_count_give_the_same_schedule():
    scheduler = ls.LocalSearchScheduler(restarts=2)

    def run(seed):
        result = scheduler.solve(_request(seed=seed, iterations=2_000, time_budget_s=1.0))
        return [c.assignment() for c in result.candidates], result.iterations

    first, moves = run(7)
    assert (first, moves) == run(7)
    assert moves == 2_000
    assert first != run(8)[0]


def test_a_time_budgeted_run_uses_the_budget_it_is_given():
    scheduler = ls.LocalSearchScheduler(restarts=1)
    short = scheduler.solve(_request(time_budget_s=1.0))
    long = scheduler.solve(_request(time_budget_s=4.0))

    # Generous for a loaded runner; a run that ignored its budget would take
    # the unpaced calibration length or a fixed count, not scale with it.
    assert short.elapsed_s < 6.0
    assert 2.5 < long.elapsed_s < 12.0
    assert long.elapsed_s > 1.5 * short.elapsed_s
    assert long.iterations > 1.5 * short.iterations


def _moves_and_scorer(seed: int = 5):
    world, season, competitions = _small_world()
    calendar = build_calendar(world, season)
    plans = plan_slots(world, season, competitions, calendar)
    initial, pinned, _ = build_initial_schedule(
        world, season, competitions, seed, calendar, slot_plans=plans
    )
    working = [
        ls.WorkingMatch(
            competition_id=m.competition_id, home_team=m.home_team, away_team=m.away_team,
            leg=m.leg, round_index=m.round_index, date=m.date, venue=m.venue,
            slot=plans[m.competition_id].slot_of(m.round_index),
        )
        for m in initial
    ]
    request = SolveRequest(
        world=world, season=season, competitions=competitions,
        constraints=build_constraints(world, season, competitions),
        ctx=EvalContext(world=world, season=season, travel=ApiTravelModel(world)),
    )
    scorer = DeltaScorer(working, request.constraints, request.ctx)
    calendars = calendars_by_competition(calendar, competitions)
    return working, ls._MoveSet(working, pinned, request, calendars, live=scorer.live), scorer


def test_the_live_index_neighbour_check_agrees_with_scanning_every_match():
    working, moves, scorer = _moves_and_scorer()
    rng = random.Random(3)
    checked = refused = 0
    for _ in range(300):
        undo = moves.apply(rng)
        if undo is not None and rng.random() < 0.5:
            scorer.resync()  # keep the live index on the schedule as it now stands
        elif undo is not None:
            ls._restore(working, undo)
        for _ in range(5):
            index = rng.randrange(len(working))
            slot = working[index].slot
            day = rng.choice(slot.days) if rng.random() < 0.7 else (
                slot.days[0] + timedelta(days=rng.randint(-8, 8))
            )
            expected = moves._neighbours_ok_scan(index, day)
            assert moves._neighbours_ok(index, day) == expected, (index, day)
            checked += 1
            refused += not expected
    assert checked and 0 < refused < checked


def test_reweighting_follows_the_improvements_and_never_starves_an_operator():
    _, moves, _ = _moves_and_scorer()
    base = list(moves._weights)
    assert sum(base) == pytest.approx(1.0)

    moves.reweight([0, 0, 0, 0])
    assert moves._weights == pytest.approx(base, abs=0.01)

    for _ in range(12):
        moves.reweight([0, 60, 0, 0])
    weights = moves._weights
    assert sum(weights) == pytest.approx(1.0)
    assert weights[1] > 0.6
    assert min(weights) >= ls._MIN_WEIGHT / 1.1


def test_every_operator_is_still_drawn_after_reweighting():
    _, moves, _ = _moves_and_scorer()
    for _ in range(20):
        moves.reweight([0, 0, 0, 200])
    rng = random.Random(1)
    drawn = set()
    for _ in range(4_000):
        moves.apply(rng)
        drawn.add(moves.last_operator)
    assert drawn == {0, 1, 2, 3}


# -- day domains ---------------------------------------------------------------


def _slot(competition_id: str, days: list[date]) -> RoundSlot:
    return RoundSlot(
        competition_id=competition_id, kind="weekend", start=days[0], end=days[-1],
        days=tuple(days), default_day=days[0],
    )


def test_a_domain_is_the_slot_days_that_clear_blackouts_and_hard_commitments():
    world, season, competitions = _small_world()
    men = competitions[0]
    days = [date(2026, 5, 15) + timedelta(days=n) for n in range(4)]
    home, away = men.teams[0], men.teams[1]
    venue = world.team(home).home_venue
    season = f.season(
        competitions=["men", "women"], start=season.start, end=season.end,
        global_blackouts=[], venue_blackouts=[],
    )
    calendars = calendars_by_competition(build_calendar(world, season), competitions)
    # `away` has a European leg on day 3, so day 2..4 are within its rest.
    commitments = CommitmentSet([f.commitment(away, days[2], min_rest_days=1)])
    domains = DayDomains(calendars, commitments)

    slot = _slot("men", days)
    assert domains.days(slot, home, away, venue) == (days[0],)
    assert domains.days(slot, home, men.teams[2], venue) == tuple(days)
    # Same answer from the memo.
    assert domains.days(slot, home, away, venue) == (days[0],)
    assert domains.allows(home, away, venue, "men", days[0])
    assert not domains.allows(home, away, venue, "men", days[2])


def test_fixtures_with_no_admissible_day_are_reported():
    world, season, competitions = _small_world()
    men = competitions[0]
    days = [date(2026, 5, 15) + timedelta(days=n) for n in range(3)]
    home, away = men.teams[0], men.teams[1]
    blocked_for = [f.commitment(away, day, min_rest_days=0) for day in days]
    calendars = calendars_by_competition(build_calendar(world, season), competitions)
    domains = DayDomains(calendars, CommitmentSet(blocked_for))

    slot = _slot("men", days)
    stuck = ls.WorkingMatch(
        competition_id="men", home_team=home, away_team=away, leg=1, round_index=0,
        date=days[0], venue=world.team(home).home_venue, slot=slot,
    )
    fine = ls.WorkingMatch(
        competition_id="men", home_team=men.teams[2], away_team=men.teams[3], leg=1,
        round_index=0, date=days[0], venue=world.team(men.teams[2]).home_venue, slot=slot,
    )
    assert domains.empty_fixtures([stuck, fine]) == [stuck]
