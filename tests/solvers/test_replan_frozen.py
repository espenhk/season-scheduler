"""Re-planning with frozen matches, run against each solver backend (#13)."""

from __future__ import annotations

import random
from datetime import date, timedelta

import factories as f
import pytest
from test_solver_contract import HAS_ORTOOLS, _get_scheduler, _small_world

from terminliste.contingency import ContingencyPlanner
from terminliste.model.calendar import build_calendar, calendars_by_competition
from terminliste.model.travel import ApiTravelModel
from terminliste.replan import ReplanSpec, check_frozen, diff_schedules
from terminliste.rounds.commitments import CommitmentSet
from terminliste.scoring.base import EvalContext, ScheduleIndex
from terminliste.scoring.registry import build_constraints
from terminliste.solvers import local_search as ls
from terminliste.solvers.base import SolveRequest, resolve_slot_plans

SOLVERS = ["local"] + (["cpsat"] if HAS_ORTOOLS else [])
FREEZE = date(2026, 5, 20)


def _request(spec=None, commitments=None, seed=5, **overrides) -> SolveRequest:
    world, season, competitions = _small_world()
    commitments = commitments or CommitmentSet()
    constraints = build_constraints(
        world,
        season,
        competitions,
        commitments,
        reference=spec.reference_dates if spec else None,
        frozen=spec.frozen_keys if spec else frozenset(),
        earliest=spec.freeze_before if spec else None,
    )
    return SolveRequest(
        world=world,
        season=season,
        competitions=competitions,
        commitments=commitments,
        constraints=constraints,
        ctx=EvalContext(world=world, season=season, travel=ApiTravelModel(world)),
        seed=seed,
        top_n=1,
        time_budget_s=20.0,
        iterations=4_000,
        replan=spec,
        **overrides,
    )


@pytest.fixture(scope="module")
def base():
    """A fresh season, with a kickoff time on one frozen match that no
    assignment rule would produce."""
    result = _get_scheduler("local").solve(_request())
    matches = list(result.best.matches)
    first = next(i for i, m in enumerate(matches) if m.date < FREEZE)
    matches[first] = matches[first].model_copy(update={"kickoff_time": "13:37"})
    return matches


def _spec(base, freeze=FREEZE, locked=frozenset()):
    return ReplanSpec.build(base, freeze, locked)


def _open(spec):
    return [m for m in spec.reference if m.key not in spec.frozen_keys]


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_frozen_matches_come_out_byte_identical(solver_name, base):
    spec = _spec(base)
    result = _get_scheduler(solver_name).solve(_request(spec))
    revised = result.best.matches

    frozen = [m for m in base if m.date < FREEZE]
    assert len(frozen) > 20 and len(revised) == len(base)
    assert check_frozen(spec, revised) == []
    by_key = {m.key: m for m in revised}
    assert all(by_key[m.key].model_dump_json() == m.model_dump_json() for m in frozen)


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_a_kickoff_that_came_with_a_frozen_match_survives(solver_name, base):
    spec = _spec(base)
    marked = next(m for m in base if m.kickoff_time == "13:37")
    result = _get_scheduler(solver_name).solve(_request(spec))

    assert {m.key: m for m in result.best.matches}[marked.key].kickoff_time == "13:37"


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_locked_matches_after_the_freeze_stay_too(solver_name, base):
    late = [m for m in base if m.date >= FREEZE + timedelta(days=30)][:6]
    spec = _spec(base, locked=frozenset(m.key for m in late))
    result = _get_scheduler(solver_name).solve(_request(spec))
    by_key = {m.key: m for m in result.best.matches}

    assert all(m.date >= FREEZE for m in late)
    assert all(by_key[m.key] == m for m in late)
    assert check_frozen(spec, result.best.matches) == []


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_an_untouched_frame_changes_nothing(solver_name, base):
    spec = _spec(base)
    result = _get_scheduler(solver_name).solve(_request(spec))

    diff = diff_schedules(spec, result.best.matches)
    assert diff.moved == []
    assert result.best.score.feasible


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_a_new_commitment_moves_only_the_match_it_hits(solver_name, base):
    """The reference puts an open match on a day its home side now has a hard
    European leg: the revision moves it, and only it."""
    spec = _spec(base)
    victim = next(m for m in _open(spec) if m.date >= FREEZE + timedelta(days=20))
    commitments = CommitmentSet(
        [f.commitment(victim.home_team, victim.date, min_rest_days=1, source="uefa")]
    )
    result = _get_scheduler(solver_name).solve(_request(spec, commitments))
    revised = result.best.matches

    diff = diff_schedules(spec, revised)
    assert result.best.score.feasible, result.best.score.hard_results()
    assert [(m.home_team, m.away_team) for m in diff.moved] == [(victim.home_team, victim.away_team)]
    assert diff.moved[0].to_date >= FREEZE
    assert check_frozen(spec, revised) == []


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_no_open_match_lands_before_the_freeze_date(solver_name, base):
    """Mid-round freeze: the round's early days are already played, so its
    open matches may not take them."""
    mid_round = sorted({m.date for m in base})[len(base) // 8]
    spec = _spec(base, freeze=mid_round + timedelta(days=1))
    result = _get_scheduler(solver_name).solve(_request(spec))

    assert any(m.key in spec.frozen_keys for m in base if m.date == mid_round)
    assert all(m.date >= spec.freeze_before for m in result.best.matches if m.key not in spec.frozen_keys)


def test_local_search_only_ever_makes_the_moves_a_revision_may(base):
    spec = _spec(base)
    request = _request(spec)
    calendar = build_calendar(request.world, request.season)
    calendars = calendars_by_competition(calendar, request.competitions)
    slot_plans = resolve_slot_plans(request, calendar)
    working = [
        ls.WorkingMatch(
            competition_id=m.competition_id, home_team=m.home_team, away_team=m.away_team,
            leg=m.leg, round_index=m.round_index, date=m.date, venue=m.venue,
            slot=slot_plans[m.competition_id].slot_of(m.round_index),
        )
        for m in spec.reference
    ]
    moves = ls._MoveSet(working, set(spec.frozen_keys), request, calendars)
    frozen_before = {i: ls._snap(i, m) for i, m in enumerate(working) if m.key in spec.frozen_keys}
    rng = random.Random(3)
    operators: set[int] = set()
    applied = 0
    for _ in range(4_000):
        undo = moves.apply(rng)
        if undo is None:
            continue
        applied += 1
        operators.add(moves.last_operator)
        assert all(working[i].date >= FREEZE for i, *_ in undo)
        # Keep the move so later ones build on it.
    assert applied > 100
    assert operators <= {0, 2}
    assert {i: ls._snap(i, working[i]) for i in frozen_before} == frozen_before
    assert [(m.round_index, m.slot) for m in working] == [
        (m.round_index, slot_plans[m.competition_id].slot_of(m.round_index)) for m in working
    ]


def test_the_slot_of_each_round_is_the_one_its_dates_sit_in(base):
    spec = _spec(base)
    request = _request(spec)
    plans = resolve_slot_plans(request, build_calendar(request.world, request.season))

    for competition_id, rounds in spec.round_dates().items():
        for round_index, days in rounds.items():
            assert all(plans[competition_id].slot_of(round_index).contains(d) for d in days)


def test_a_frozen_match_gets_no_fallback():
    world, league = (
        f.world(
            [f.club(f"c{i}", [f.team(f"t{i}", f"c{i}", f"v{i}")]) for i in range(1, 5)],
            [f.venue(f"v{i}") for i in range(1, 5)],
            [f.competition("league", [f"t{i}" for i in range(1, 5)], min_rest_days=3)],
        ),
        None,
    )
    league = world.competition("league")
    season = f.season(competitions=["league"])
    maybe = f.commitment("t1", date(2026, 8, 20), hard=False, min_rest_days=3, node_id="t1/uecl/a")
    commitments = f.commitment_set(maybe)
    match = f.match("league", "t1", "t2", date(2026, 8, 23), "v1")
    matches = [match]

    open_plan = ContingencyPlanner(world, season, [league], commitments).analyse(ScheduleIndex(matches))
    frozen_plan = ContingencyPlanner(
        world, season, [league], commitments, frozen=frozenset({match.key})
    ).analyse(ScheduleIndex(matches))

    assert match.key in open_plan.plan.fallbacks
    assert frozen_plan.plan.fallbacks == {} and frozen_plan.plan.uncovered == []


def test_a_fallback_is_never_planned_for_a_day_before_the_freeze():
    world = f.world(
        [f.club(f"c{i}", [f.team(f"t{i}", f"c{i}", f"v{i}")]) for i in range(1, 5)],
        [f.venue(f"v{i}") for i in range(1, 5)],
        [f.competition("league", [f"t{i}" for i in range(1, 5)], min_rest_days=3)],
    )
    league = world.competition("league")
    season = f.season(competitions=["league"])
    # Thu 20 Aug is within three days of Sun 23 Aug, so Fri and Sat are out.
    commitments = f.commitment_set(
        f.commitment("t1", date(2026, 8, 20), hard=False, min_rest_days=3, node_id="t1/uecl/a")
    )
    match = f.match("league", "t1", "t2", date(2026, 8, 23), "v1")
    freeze = date(2026, 8, 25)

    index = ScheduleIndex([match])
    anytime = ContingencyPlanner(world, season, [league], commitments).analyse(index).plan
    after = ContingencyPlanner(world, season, [league], commitments, earliest=freeze).analyse(index).plan

    # Monday the 24th clears it, but with the freeze on the 25th it is past.
    assert anytime.fallbacks[match.key].date == date(2026, 8, 24)
    assert after.fallbacks[match.key].date >= freeze


def test_a_stability_penalty_charges_each_displaced_fixture(base):
    spec = _spec(base)
    request = _request(spec)
    rule = next(c for c in request.constraints if c.id == "schedule_stability")
    assert rule.evaluate(ScheduleIndex(list(base)), request.ctx).total == 0

    shifted = list(base)
    shifted[-1] = shifted[-1].model_copy(update={"date": shifted[-1].date - timedelta(days=1)})
    result = rule.evaluate(ScheduleIndex(shifted), request.ctx)
    assert (result.count, result.total) == (1, -rule.weight)


def test_settling_the_frame_keeps_only_commitments_that_can_still_touch_an_open_day():
    from terminliste.frame import SeasonFrame, build_uefa_weeks
    from terminliste.replan import settle_frame

    freeze = date(2026, 8, 18)
    over = f.commitment("t1", date(2026, 8, 6), min_rest_days=2)
    reaches = f.commitment("t1", date(2026, 8, 16), min_rest_days=2)  # rest days run to the 18th
    ahead = f.commitment("t2", date(2026, 8, 25), hard=False)
    commitments = CommitmentSet([over, reaches, ahead])
    frame = SeasonFrame(commitments=commitments, uefa_weeks=build_uefa_weeks(commitments))

    settled = settle_frame(frame, freeze)

    assert [c.date for c in settled.commitments] == [reaches.date, ahead.date]
    assert {w.date for w in settled.uefa_weeks} == {reaches.date, ahead.date}
    assert settle_frame(frame, None) is frame


def test_the_diff_names_a_turned_fixture_and_a_moved_one(base):
    spec = _spec(base)
    revised = list(base)
    first, second, *_ = (i for i, m in enumerate(base) if m.key not in spec.frozen_keys)
    revised[first] = base[first].model_copy(update={"date": base[first].date + timedelta(days=1)})
    revised[second] = base[second].model_copy(
        update={"home_team": base[second].away_team, "away_team": base[second].home_team}
    )

    diff = diff_schedules(spec, revised)

    assert sorted(m.flipped for m in diff.moved) == [False, True]
    assert (diff.total, diff.frozen) == (len(base), len(spec.frozen_keys))


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_a_frozen_match_that_came_without_a_kickoff_stays_without_one(solver_name, base):
    """A published CSV rarely has kickoff times; frozen rows are still output
    exactly as read, and the open ones get theirs."""
    bare = [m.model_copy(update={"kickoff_time": None}) for m in base]
    spec = _spec(bare)
    revised = _get_scheduler(solver_name).solve(_request(spec)).best.matches

    assert check_frozen(spec, revised) == []
    assert all(m.kickoff_time is None for m in revised if m.key in spec.frozen_keys)
    assert any(m.kickoff_time for m in revised if m.key not in spec.frozen_keys)


def _swap_two_rounds(base):
    """`base` with the dates of two rounds of one league exchanged, as
    `swap_rounds` leaves a searched schedule: round order no longer follows
    date order. Returns the schedule, the round now in the later slot and the
    day before the earlier slot starts."""
    by_round: dict[int, list] = {}
    for m in base:
        if m.competition_id == "men":
            by_round.setdefault(m.round_index, []).append(m.date)
    chronological = sorted(by_round, key=lambda r: min(by_round[r]))
    first, second = chronological[4], chronological[6]
    shift = 7 * round((min(by_round[second]) - min(by_round[first])).days / 7)
    out = []
    for m in base:
        if m.competition_id == "men" and m.round_index == first:
            m = m.model_copy(update={"date": m.date + timedelta(days=shift)})
        elif m.competition_id == "men" and m.round_index == second:
            m = m.model_copy(update={"date": m.date - timedelta(days=shift)})
        out.append(m)
    return out, first, min(by_round[first]) - timedelta(days=1)


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_rounds_out_of_date_order_keep_their_other_days(solver_name, base):
    """Slot days are trimmed against the neighbouring weekends by date. By round
    index the swapped rounds' neighbours are wrong, almost every day is cut and
    an open match cannot leave its reference date."""
    from terminliste.model.calendar import build_calendar

    swapped, later_round, freeze = _swap_two_rounds(base)
    spec = _spec(swapped, freeze=freeze)
    victim = next(
        m for m in _open(spec) if m.competition_id == "men" and m.round_index == later_round
    )
    commitments = CommitmentSet(
        [f.commitment(victim.home_team, victim.date, min_rest_days=1, source="uefa")]
    )
    request = _request(spec, commitments)
    plans = resolve_slot_plans(request, build_calendar(request.world, request.season))
    slot = plans["men"].slot_of(later_round)
    assert len(slot.days) > 1

    result = _get_scheduler(solver_name).solve(request)

    moved = diff_schedules(spec, result.best.matches).moved
    assert result.best.score.feasible, result.best.score.hard_results()
    assert [(m.home_team, m.away_team) for m in moved] == [(victim.home_team, victim.away_team)]
    assert slot.contains(moved[0].to_date)


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_only_matches_of_movable_teams_move(solver_name, base):
    spec = _spec(base)
    open_matches = [m for m in _open(spec) if m.date >= FREEZE + timedelta(days=20)]
    victim = open_matches[0]
    other = next(
        m for m in open_matches if {m.home_team, m.away_team}.isdisjoint((victim.home_team, victim.away_team))
    )
    clashes = [
        f.commitment(m.home_team, m.date, min_rest_days=1, source="uefa") for m in (victim, other)
    ]
    movable = frozenset({victim.home_team})
    spec = spec.with_movable_teams(movable)
    result = _get_scheduler(solver_name).solve(_request(spec, CommitmentSet(clashes)))

    moved = diff_schedules(spec, result.best.matches).moved
    assert moved
    assert all(not movable.isdisjoint((m.home_team, m.away_team)) for m in moved)
    assert other.key in spec.pinned_keys and victim.key not in spec.pinned_keys
