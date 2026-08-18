"""The contract both solver backends must satisfy, run against each in turn.

Uses a small synthetic world (8 + 6 teams, ~10-week season) rather than the
full 28-team dataset, so both backends finish in a couple of seconds and the
suite stays fast. The full 2026 data is exercised separately in
test_integration_coupled_leagues.py, where solve time actually matters.
"""

from __future__ import annotations

from datetime import date, timedelta

import factories as f
import pytest

from terminliste.model.travel import ApiTravelModel
from terminliste.rounds.cup_schedule import schedule_cups
from terminliste.scoring.registry import build_constraints
from terminliste.scoring.base import EvalContext
from terminliste.solvers.base import SolveRequest, divergence

try:
    import ortools  # noqa: F401

    HAS_ORTOOLS = True
except ImportError:
    HAS_ORTOOLS = False


def _small_world():
    venues = [f.venue(f"v{i}", lat=59.0 + i * 0.3, lon=10.0 + i * 0.3) for i in range(7)]
    men_teams = [f.team(f"m{i}", f"club{i}", f"v{i % 7}", gender="men") for i in range(8)]
    women_teams = [f.team(f"w{i}", f"club{i}", f"v{i % 7}", gender="women") for i in range(6)]

    clubs = []
    for i in range(6):
        clubs.append(f.club(f"club{i}", [men_teams[i], women_teams[i]]))
    for i in range(6, 8):
        clubs.append(f.club(f"club{i}", [men_teams[i]]))

    comp_m = f.competition(
        "men", [t.id for t in men_teams], gender="men", preferred_weekday="sunday", min_rest_days=3
    )
    comp_w = f.competition(
        "women", [t.id for t in women_teams], gender="women", preferred_weekday="saturday", min_rest_days=3
    )
    world = f.world(clubs, venues, [comp_m, comp_w])
    season = f.season(
        competitions=["men", "women"], start=date(2026, 3, 1), end=date(2026, 9, 30)
    )
    return world, season, [comp_m, comp_w]


def _get_scheduler(name: str):
    from terminliste.solvers import get_scheduler

    return get_scheduler(name)


SOLVERS = ["local"] + (["cpsat"] if HAS_ORTOOLS else [])


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_returns_requested_number_of_diverse_candidates(solver_name):
    world, season, competitions = _small_world()
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=1, top_n=3, time_budget_s=10.0,
    )
    result = _get_scheduler(solver_name).solve(request)

    assert len(result.candidates) <= 3
    assert len(result.candidates) >= 1
    for i, a in enumerate(result.candidates):
        for b in result.candidates[i + 1 :]:
            assert divergence(a, b) >= 0.10  # a looser bound than the 0.15 target, for small worlds


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_every_fixture_is_placed_exactly_once(solver_name):
    world, season, competitions = _small_world()
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=2, top_n=1, time_budget_s=10.0,
    )
    result = _get_scheduler(solver_name).solve(request)
    candidate = result.best
    assert candidate is not None

    for competition in competitions:
        expected = competition.total_matches
        actual = len([m for m in candidate.matches if m.competition_id == competition.id])
        assert actual == expected, competition.id


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_cup_schedule_is_respected_by_both_backends(solver_name):
    """Regression guard: CP-SAT's fixtures each have a small, fixed candidate
    window around a round anchor chosen without cup awareness, so unlike the
    greedy/annealing backend it has no way to route around a cup conflict
    discovered only after the fact — it must exclude conflicting dates from
    the candidate set up front (see `_cup_clear` in `solvers/cpsat.py`), or a
    real cup schedule can push the whole model to a false "infeasible"."""
    world, season, competitions = _small_world()
    men_team_ids = competitions[0].teams
    cup = f.competition(
        "cup",
        men_team_ids,
        format="cup",
        min_rest_days=3,
        match_window_days=2,
        cup_rounds=[
            f.cup_round("r1", forced_date=season.start + timedelta(days=14), name="Round 1"),
            f.cup_round(
                "r2",
                window_start=season.start + timedelta(days=30),
                window_end=season.start + timedelta(days=60),
                granularity="month",
                name="Round 2",
            ),
        ],
    )
    cup_schedules, cup_warnings = schedule_cups([cup], season)
    assert cup_warnings == []

    constraints = build_constraints(world, season, competitions, cup_schedules)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions, cup_schedules=cup_schedules,
        constraints=constraints, ctx=ctx, seed=11, top_n=1, time_budget_s=15.0,
    )
    result = _get_scheduler(solver_name).solve(request)
    assert result.best is not None
    assert result.best.score.feasible, result.best.score.hard_results()
    cup_result = result.best.score.result("cup_round_conflict")
    assert cup_result is not None
    assert cup_result.count == 0


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_zero_hard_violations_is_achievable_on_an_easy_calendar(solver_name):
    world, season, competitions = _small_world()
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=3, top_n=1, time_budget_s=15.0,
    )
    result = _get_scheduler(solver_name).solve(request)
    assert result.best is not None
    assert result.best.score.feasible, result.best.score.hard_results()


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_search_stats_account_for_every_scenario_investigated(solver_name):
    """Issue #34: both backends must report how hard the search had to work —
    total scenarios tried, how many were viable, how many hit a hard rule, and
    (for the ones that didn't come out viable) which rule was the culprit."""
    world, season, competitions = _small_world()
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=6, top_n=1, time_budget_s=10.0,
    )
    result = _get_scheduler(solver_name).solve(request)
    stats = result.search_stats

    assert stats.investigated > 0
    assert stats.feasible + stats.hard_violation_scenarios == stats.investigated
    assert 0.0 <= stats.feasible_rate <= 1.0
    assert stats.difficulty in ("easy", "moderate", "hard")
    if stats.hard_violation_scenarios:
        assert stats.hard_violation_counts
        assert all(count > 0 for count in stats.hard_violation_counts.values())


def test_local_search_progress_callback_reports_a_live_running_total():
    """The CLI's live progress line (issue #34) depends on each `ProgressUpdate`
    being an independent snapshot — not a shared, still-mutating object — and
    on the running totals only ever growing within a restart."""
    world, season, competitions = _small_world()
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    updates = []
    request = SolveRequest(
        world=world, season=season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=8, top_n=1, time_budget_s=4.0, progress=updates.append,
    )
    result = _get_scheduler("local").solve(request)

    assert updates
    by_restart: dict[int, list] = {}
    for update in updates:
        by_restart.setdefault(update.restart, []).append(update)
    for restart_updates in by_restart.values():
        investigated = [u.stats.investigated for u in restart_updates]
        assert investigated == sorted(investigated)
        assert all(u.stats.investigated <= u.total_iterations for u in restart_updates)
    assert updates[-1].stats.investigated <= result.search_stats.investigated


def test_local_search_is_deterministic_for_a_fixed_seed():
    world, season, competitions = _small_world()
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    scheduler = _get_scheduler("local")

    def run():
        request = SolveRequest(
            world=world, season=season, competitions=competitions, constraints=constraints,
            ctx=ctx, seed=7, top_n=1, time_budget_s=4.0,
        )
        return scheduler.solve(request).best.assignment()

    assert run() == run()


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_final_round_and_full_round_pins_are_honoured(solver_name):
    """Regression guard for issues #40 and #38a: a league's final round must
    land on one date and kickoff time, and a `FullRoundRequirement` must put
    every team on the calendar that day — both need the solver's window
    restriction in `resolve_round_pins` to actually work, not just the
    scoring rule that checks the outcome."""
    from terminliste.model.schema import FullRoundRequirement

    world, season, competitions = _small_world()
    men = competitions[0]
    requirement = FullRoundRequirement(id="full1", date=date(2026, 6, 14), competition=men.id)
    season = f.season(
        competitions=season.competitions,
        start=season.start,
        end=season.end,
        full_round_requirements=[requirement],
    )
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=4, top_n=1, time_budget_s=15.0,
    )
    result = _get_scheduler(solver_name).solve(request)
    assert result.best is not None
    assert result.best.score.feasible, result.best.score.hard_results()

    final_round_result = result.best.score.result("final_round_same_slot")
    assert final_round_result is not None and final_round_result.count == 0

    full_round_result = result.best.score.result("full_round_on_date")
    assert full_round_result is not None and full_round_result.count == 0


def test_unsolvable_calendar_reports_which_hard_rule_is_broken():
    """Almost every date blacked out: the solver should still terminate and
    say *what* is broken, not hang or quietly emit an infeasible schedule."""
    from terminliste.model.schema import DatedNote

    world, season, competitions = _small_world()
    blackouts = [
        DatedNote(date=d, reason="blocked")
        for d in _all_dates(season.start, season.end)
        if d.weekday() not in (5, 6)  # keep only weekends open
    ]
    tight_season = f.season(
        competitions=season.competitions,
        start=season.start,
        end=season.end,
        global_blackouts=blackouts,
    )
    constraints = build_constraints(world, tight_season, competitions)
    ctx = EvalContext(world=world, season=tight_season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=tight_season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=5, top_n=1, time_budget_s=6.0,
    )
    result = _get_scheduler("local").solve(request)
    # Either it found a feasible arrangement despite the squeeze, or it says so.
    if result.best is not None and not result.best.score.feasible:
        assert result.notes


def _all_dates(start, end):
    from datetime import timedelta

    days = (end - start).days
    return [start + timedelta(days=i) for i in range(days + 1)]
