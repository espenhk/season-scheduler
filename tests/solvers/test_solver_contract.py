"""The contract both solver backends must satisfy, run against each in turn.

Uses a small synthetic world (8 + 6 teams, ~10-week season) rather than the
full 28-team dataset, so both backends finish in a couple of seconds and the
suite stays fast. The full 2026 data is exercised separately in
test_integration_coupled_leagues.py, where solve time actually matters.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import factories as f
import pytest

from terminliste.model.travel import ApiTravelModel
from terminliste.rounds.commitments import CommitmentSet
from terminliste.rounds.cup_schedule import cup_commitments, schedule_cups
from terminliste.scoring.base import EvalContext
from terminliste.scoring.registry import build_constraints
from terminliste.solvers.base import SolveRequest, divergence
from terminliste.solvers.local_search import LocalSearchScheduler

try:
    import ortools  # noqa: F401

    HAS_ORTOOLS = True
except ImportError:
    HAS_ORTOOLS = False


def _small_world(min_rest_days: int = 3):
    venues = [f.venue(f"v{i}", lat=59.0 + i * 0.3, lon=10.0 + i * 0.3) for i in range(7)]
    men_teams = [f.team(f"m{i}", f"club{i}", f"v{i % 7}", gender="men") for i in range(8)]
    women_teams = [f.team(f"w{i}", f"club{i}", f"v{i % 7}", gender="women") for i in range(6)]

    clubs = []
    for i in range(6):
        clubs.append(f.club(f"club{i}", [men_teams[i], women_teams[i]]))
    for i in range(6, 8):
        clubs.append(f.club(f"club{i}", [men_teams[i]]))

    comp_m = f.competition(
        "men", [t.id for t in men_teams], gender="men", preferred_weekday="sunday",
        min_rest_days=min_rest_days,
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
    the candidate set up front (see `_commitment_clear` in `solvers/cpsat.py`), or a
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

    # A threshold of 0.5 makes round 2 (reached with probability 0.5) hard too.
    commitments = cup_commitments([cup], cup_schedules, hard_threshold=0.5)
    assert all(c.hard for c in commitments)
    constraints = build_constraints(world, season, competitions, commitments)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions, cup_schedules=cup_schedules,
        commitments=commitments, constraints=constraints, ctx=ctx, seed=11, top_n=1,
        time_budget_s=15.0,
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


def test_polish_phase_runs_as_its_own_restart_index_beyond_the_explore_restarts():
    """The polish phase (#98) must resume a shortlisted candidate rather than
    kick off another fresh restart. A progress update carrying a
    `restart_index` beyond `LocalSearchScheduler.restarts` is direct evidence
    it ran as a distinct, later phase."""
    world, season, competitions = _small_world()
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    updates = []
    scheduler = LocalSearchScheduler(restarts=2, polish_fraction=0.3)
    request = SolveRequest(
        world=world, season=season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=9, top_n=2, time_budget_s=6.0, progress=updates.append,
    )
    scheduler.solve(request)

    assert updates
    restart_indexes = {u.restart for u in updates}
    assert any(index >= scheduler.restarts for index in restart_indexes), (
        "expected at least one progress update from a polish-phase restart index, "
        f"got only {sorted(restart_indexes)} against restarts={scheduler.restarts}"
    )


def test_polish_phase_disabled_by_zero_fraction_keeps_only_explore_restarts():
    """The counterpart to the test above: `polish_fraction=0.0` must fall back
    to exactly phase one's old behaviour, with no restart index beyond
    `restarts` and no polish note — the escape hatch for anyone who wants the
    pre-#98 search exactly as it was."""
    world, season, competitions = _small_world()
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    updates = []
    scheduler = LocalSearchScheduler(restarts=2, polish_fraction=0.0)
    request = SolveRequest(
        world=world, season=season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=9, top_n=2, time_budget_s=6.0, progress=updates.append,
    )
    result = scheduler.solve(request)

    assert updates
    restart_indexes = {u.restart for u in updates}
    assert all(index < scheduler.restarts for index in restart_indexes)
    assert not any("polish" in note for note in result.notes)


def test_polish_phase_never_regresses_a_shortlisted_candidate():
    """Direct unit test of `_polish_candidates`'s replace-only-if-better rule:
    seed it with a candidate whose recorded score is far better than anything
    the tiny budget below could find fresh, and confirm the candidate comes
    back unchanged rather than overwritten by a worse polished attempt."""
    from terminliste.scoring.base import Score
    from terminliste.solvers.local_search import WorkingMatch, _polish_candidates

    world, season, competitions = _small_world()
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=10, top_n=1, time_budget_s=6.0,
    )

    scheduler = LocalSearchScheduler(restarts=1)
    result = scheduler.solve(request)
    candidate = result.best
    assert candidate is not None

    working = [
        WorkingMatch(
            competition_id=m.competition_id, home_team=m.home_team, away_team=m.away_team,
            leg=m.leg, round_index=m.round_index, date=m.date, venue=m.venue,
        )
        for m in candidate.matches
    ]
    # An unbeatable score forces `_polish_candidates`'s comparison to always
    # find the fresh attempt worse, so the candidate must survive untouched.
    candidate.score = Score(hard_violations=0, soft_total=1_000_000.0, num_matches=len(working))
    original_matches = list(candidate.matches)

    from terminliste.model.calendar import build_calendar, calendars_by_competition

    calendar = build_calendar(world, season)
    by_competition = calendars_by_competition(calendar, competitions)

    _polish_candidates(
        chosen=[candidate],
        candidates=[candidate],
        restart_states=[(working, set())],
        request=request,
        calendars=by_competition,
        budget_s=1.0,
        restarts=1,
        start_temperature=0.05,
        end_temperature=0.01,
        notes=[],
    )

    assert candidate.matches == original_matches


def test_local_search_is_deterministic_for_a_fixed_seed():
    world, season, competitions = _small_world()
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    scheduler = _get_scheduler("local")

    def run():
        # A move count rather than a time budget: a paced run follows the
        # machine's speed, so only a fixed count is reproducible (#11).
        request = SolveRequest(
            world=world, season=season, competitions=competitions, constraints=constraints,
            ctx=ctx, seed=7, top_n=1, time_budget_s=4.0, iterations=3_000,
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


@pytest.mark.skipif(not HAS_ORTOOLS, reason="needs ortools")
def test_cpsat_never_relaxes_min_rest_days_unless_asked():
    """Two hard requirements put one team at home on Mon 16 and Fri 20 March,
    a day short of the four rest days its league needs. The strict model says
    so rather than quietly trading the rule away; `relax_min_rest` is the
    explicit, loudly reported way to get a schedule anyway."""
    from terminliste.model.schema import FixedRequirement

    world, season, competitions = _small_world(min_rest_days=4)
    reqs = [
        FixedRequirement(id="r1", date=date(2026, 3, 16), home_team="m0", competition="men"),
        FixedRequirement(id="r2", date=date(2026, 3, 20), home_team="m0", competition="men"),
    ]
    season = f.season(
        competitions=season.competitions, start=season.start, end=season.end,
        fixed_requirements=reqs,
    )
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))

    def solve(relax):
        request = SolveRequest(
            world=world, season=season, competitions=competitions, constraints=constraints,
            ctx=ctx, seed=1, top_n=1, time_budget_s=15.0, relax_min_rest=relax,
        )
        return _get_scheduler("cpsat").solve(request)

    strict = solve(False)
    assert strict.best is None
    assert strict.notes
    assert not any("RELAXED" in note for note in strict.notes)

    relaxed = solve(True)
    assert relaxed.best is not None, relaxed.notes
    assert any("min_rest_days RELAXED" in note for note in relaxed.notes)
    assert not relaxed.best.score.feasible
    violated = [r.constraint_id for r in relaxed.best.score.hard_results() if r.count]
    assert violated == ["min_rest_days"]
    stats = relaxed.search_stats
    assert stats.feasible + stats.hard_violation_scenarios == stats.investigated


@pytest.mark.skipif(not HAS_ORTOOLS, reason="needs ortools")
def test_cpsat_does_not_hide_an_unrelated_impossibility_behind_the_rest_relaxation():
    """Two teams sharing a venue forced onto the same home date is a venue
    double-booking no rest-day relaxation can fix, even when asked to."""
    from terminliste.model.schema import FixedRequirement

    world, season, competitions = _small_world()
    # m0 and m7 share venue v0 (`v{i % 7}`).
    reqs = [
        FixedRequirement(id="r1", date=date(2026, 3, 14), home_team="m0", competition="men"),
        FixedRequirement(id="r2", date=date(2026, 3, 14), home_team="m7", competition="men"),
    ]
    season = f.season(
        competitions=season.competitions, start=season.start, end=season.end,
        fixed_requirements=reqs,
    )
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=1, top_n=1, time_budget_s=15.0, relax_min_rest=True,
    )
    result = _get_scheduler("cpsat").solve(request)

    assert result.best is None
    assert result.notes
    assert not any("RELAXED" in note for note in result.notes)


@pytest.mark.skipif(not HAS_ORTOOLS, reason="needs ortools")
def test_cpsat_names_the_requirement_when_its_date_lies_in_no_slot():
    """Wed 18 March is in no weekend slot of the small world, so the Level 1
    culprit report names the requirement rather than relaxing anything."""
    from terminliste.model.schema import FixedRequirement

    world, season, competitions = _small_world()
    reqs = [FixedRequirement(id="midweek", date=date(2026, 3, 18), home_team="m0", competition="men")]
    season = f.season(
        competitions=season.competitions, start=season.start, end=season.end,
        fixed_requirements=reqs,
    )
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=1, top_n=1, time_budget_s=15.0,
    )
    result = _get_scheduler("cpsat").solve(request)

    assert result.best is None
    assert any("fixed_requirement:midweek" in note for note in result.notes)
    assert result.search_stats.hard_violation_counts["fixed_requirement:midweek"] >= 1


@pytest.mark.skipif(not HAS_ORTOOLS, reason="needs ortools")
def test_cpsat_names_the_frame_entry_that_closes_every_slot():
    """A binding European date whose rest window outlasts the season closes
    every slot to its team; the report names the team and the commitment."""
    from terminliste.rounds.commitments import Commitment, CommitmentSet

    world, season, competitions = _small_world()
    commitments = CommitmentSet(
        [
            Commitment(
                team_id="m0", date=date(2026, 6, 1), min_rest_days=400,
                label="Test qualifier", source="uefa", competition_id="uefa", node_id="q1",
            )
        ]
    )
    constraints = build_constraints(world, season, competitions, commitments)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=1, top_n=1, time_budget_s=15.0, commitments=commitments,
    )
    result = _get_scheduler("cpsat").solve(request)

    assert result.best is None
    text = " ".join(result.notes)
    assert "closed" in text
    assert "Test qualifier" in text
    assert "european_commitment_conflict" in text


@pytest.mark.skipif(not HAS_ORTOOLS, reason="needs ortools")
def test_cpsat_reports_model_sizes_and_orients_pairings_itself():
    world, season, competitions = _small_world()
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=1, top_n=1, time_budget_s=10.0,
    )
    result = _get_scheduler("cpsat").solve(request)

    assert any("level 1" in n and "variables" in n for n in result.notes)
    assert any("level 2" in n and "variables" in n for n in result.notes)
    assert result.best is not None and result.best.score.feasible
    assert set(result.slot_plans) == {"men", "women"}


def _cpsat_request(top_n=1, budget=20.0, season=None):
    world, base_season, competitions = _small_world()
    season = season or base_season
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    return SolveRequest(
        world=world, season=season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=1, top_n=top_n, time_budget_s=budget,
    )


class _Stub:
    """Stands in for a CpSolver that ended with `status` and no solution."""

    def __init__(self, status, name):
        self.parameters = type("P", (), {})()
        self._status, self._name = status, name

    def Solve(self, model):
        return self._status

    def StatusName(self, status):
        return self._name

    def SufficientAssumptionsForInfeasibility(self):
        return []


def _fail_level_two(monkeypatch, statuses):
    """Make the first level 2 solves end as `statuses` (cp_model status, name) in turn."""
    from ortools.sat.python import cp_model

    real = cp_model.CpSolver
    calls = {"n": 0}
    queue = list(statuses)

    def factory():
        calls["n"] += 1
        # Level 1 and level 2 solvers alternate; only level 2 (even calls) is stubbed.
        if calls["n"] % 2 == 0 and queue:
            status, name = queue.pop(0)
            return _Stub(status, name)
        return real()

    monkeypatch.setattr(cp_model, "CpSolver", factory)


@pytest.mark.skipif(not HAS_ORTOOLS, reason="needs ortools")
def test_cpsat_re_solves_level_one_when_level_two_is_proven_infeasible(monkeypatch):
    from ortools.sat.python import cp_model

    _fail_level_two(monkeypatch, [(cp_model.INFEASIBLE, "INFEASIBLE")])
    result = _get_scheduler("cpsat").solve(_cpsat_request())

    assert result.best is not None and result.best.score.feasible
    assert any("rejected by level 2" in n for n in result.notes), result.notes
    stats = result.search_stats
    assert stats.investigated == 1
    assert stats.hard_violation_scenarios == 0
    assert not stats.hard_violation_counts


@pytest.mark.skipif(not HAS_ORTOOLS, reason="needs ortools")
def test_cpsat_reports_a_level_two_timeout_as_a_timeout(monkeypatch):
    from ortools.sat.python import cp_model

    _fail_level_two(monkeypatch, [(cp_model.UNKNOWN, "UNKNOWN")])
    result = _get_scheduler("cpsat").solve(_cpsat_request())

    assert result.best is None
    assert any("level 2 timed out" in n for n in result.notes), result.notes
    assert not any("no slot choice admitted" in n for n in result.notes)
    stats = result.search_stats
    assert stats.hard_violation_scenarios == 0
    assert not stats.hard_violation_counts


@pytest.mark.skipif(not HAS_ORTOOLS, reason="needs ortools")
def test_cpsat_later_options_use_different_slots_from_earlier_ones():
    result = _get_scheduler("cpsat").solve(_cpsat_request(top_n=2, budget=40.0))

    from terminliste.solvers.cpsat import _SLOT_REUSE_SHARE

    assert len(result.candidates) == 2, result.notes
    first, second = (c.round_slots for c in result.candidates)
    shared = sum(1 for key, slot in first.items() if second[key] == slot)
    assert shared <= int(len(first) * _SLOT_REUSE_SHARE), (shared, len(first))


@pytest.mark.skipif(not HAS_ORTOOLS, reason="needs ortools")
def test_place_forces_a_fixed_requirements_date_once_the_home_team_is_placed():
    from terminliste.model.calendar import build_calendar
    from terminliste.model.schema import FixedRequirement
    from terminliste.solvers.base import resolve_slot_plans
    from terminliste.solvers.cpsat import _LevelOneSolution, _place, _Problem

    request = _cpsat_request()
    calendar = build_calendar(request.world, request.season)
    problem = _Problem(request, calendar, resolve_slot_plans(request, calendar))
    men = problem.by_id["men"]
    round_index = 3
    fixture = men.team_fixture[("m0", round_index)]
    slot_index = round_index
    slot = men.slots[slot_index]
    opponent = fixture.away_team if fixture.home_team == "m0" else fixture.home_team
    days = problem.fixture_days(men, "m0", opponent, slot)
    assert len(days) > 1
    target = days[-1]

    problem.season = problem.season.model_copy(update={"fixed_requirements": [
        FixedRequirement(id="pin", date=target, home_team="m0", competition="men")
    ]})
    solution = _LevelOneSolution(
        slot_of={(lg.id, r): r for lg in problem.leagues for r in range(lg.rounds)},
        flipped={
            (lg.id, frozenset(pair)): (
                lg.id == "men"
                and frozenset(pair) == frozenset((fixture.home_team, fixture.away_team))
                and fixture.home_team != "m0"
            )
            for lg in problem.leagues for pair in lg.pairs
        },
    )
    placed, error = _place(problem, solution)

    assert error is None
    pinned = next(
        p for p in placed
        if p.league.id == "men" and p.fixture.round_index == round_index and p.fixture.home_team == "m0"
    )
    assert pinned.days == [target]
    others = [
        p for p in placed
        if p.league.id == "men" and p.fixture.round_index == round_index and p is not pinned
    ]
    assert any(len(p.days) > 1 for p in others)


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_every_round_sits_in_one_planned_slot_and_both_backends_share_the_plan(solver_name):
    """Anchors come from the slot plan (#6): the result carries the plan, and
    every candidate maps each round to a slot of it."""
    world, season, competitions = _small_world()
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=3, top_n=1, time_budget_s=6.0,
    )
    result = _get_scheduler(solver_name).solve(request)

    assert set(result.slot_plans) == {c.id for c in competitions}
    for candidate in result.candidates:
        for competition in competitions:
            plan = result.slot_plans[competition.id]
            slots = [candidate.round_slots[(competition.id, r)] for r in range(competition.rounds)]
            assert len(set(slots)) == competition.rounds
            assert set(slots) == set(plan.chosen)


def test_solve_fails_clearly_when_a_league_has_too_few_slots():
    from terminliste.slot_planner import SlotPlanningError

    world, season, competitions = _small_world()
    season = f.season(competitions=["men", "women"], start=date(2026, 3, 1), end=date(2026, 3, 22))
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=3, top_n=1, time_budget_s=2.0,
    )

    with pytest.raises(SlotPlanningError, match="round slots for"):
        LocalSearchScheduler(restarts=1).solve(request)


def test_a_round_swap_moves_the_slot_with_the_round_and_undo_restores_it():
    import random

    from terminliste.model.calendar import build_calendar, calendars_by_competition
    from terminliste.slot_planner import plan_slots
    from terminliste.solvers.greedy import build_initial_schedule
    from terminliste.solvers.local_search import WorkingMatch, _MoveSet, _restore

    world, season, competitions = _small_world()
    calendar = build_calendar(world, season)
    plans = plan_slots(world, season, competitions, calendar)
    initial, pinned, _ = build_initial_schedule(
        world, season, competitions, 5, calendar, slot_plans=plans
    )
    working = [
        WorkingMatch(
            competition_id=m.competition_id, home_team=m.home_team, away_team=m.away_team,
            leg=m.leg, round_index=m.round_index, date=m.date, venue=m.venue,
            slot=plans[m.competition_id].slot_of(m.round_index),
        )
        for m in initial
    ]
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions,
        constraints=build_constraints(world, season, competitions), ctx=ctx,
    )
    moves = _MoveSet(
        working, pinned, request, calendars_by_competition(calendar, competitions)
    )
    before = [(m.date, m.slot) for m in working]

    rng = random.Random(1)
    snapshot = None
    while snapshot is None:
        snapshot = moves._swap_rounds(rng)
    moved = [i for i, (m, b) in enumerate(zip(working, before, strict=True)) if (m.date, m.slot) != b]
    assert moved
    for i in moved:
        assert working[i].slot != before[i][1]
        assert working[i].date in working[i].slot.days

    _restore(working, snapshot)
    assert [(m.date, m.slot) for m in working] == before


@pytest.mark.parametrize("solver_name", SOLVERS)
def test_every_match_stays_on_a_day_of_its_rounds_slot(solver_name):
    """Stage 4 (#7): no solver moves a match out of its round's slot."""
    world, season, competitions = _small_world()
    constraints = build_constraints(world, season, competitions)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions, constraints=constraints,
        ctx=ctx, seed=4, top_n=2, time_budget_s=8.0,
    )
    result = _get_scheduler(solver_name).solve(request)

    assert result.candidates
    for candidate in result.candidates:
        for match in candidate.matches:
            slot = candidate.round_slots[(match.competition_id, match.round_index)]
            assert match.date in slot.days, (match.key, match.date, slot.days)


def test_slot_days_leave_every_team_its_rest_before_a_single_day_slot():
    """A Monday two days before a midweek round would break `min_rest_days`
    for the whole league, so the planner drops it from the slot."""
    from terminliste.model.calendar import build_calendar
    from terminliste.slot_planner import plan_slots

    world, season, competitions = _small_world()
    plans = plan_slots(world, season, competitions, build_calendar(world, season))
    for competition in competitions:
        chosen = plans[competition.id].chosen
        gap = competition.min_rest_days + 1
        for earlier, later in zip(chosen, chosen[1:], strict=False):
            assert any((b - a).days >= gap for a in earlier.days for b in later.days)


def test_local_moves_never_leave_the_round_slot():
    import random

    from terminliste.model.calendar import build_calendar, calendars_by_competition
    from terminliste.slot_planner import plan_slots
    from terminliste.solvers.greedy import build_initial_schedule
    from terminliste.solvers.local_search import WorkingMatch, _MoveSet

    world, season, competitions = _small_world()
    calendar = build_calendar(world, season)
    plans = plan_slots(world, season, competitions, calendar)
    initial, pinned, _ = build_initial_schedule(
        world, season, competitions, 5, calendar, slot_plans=plans
    )
    working = [
        WorkingMatch(
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
    moves = _MoveSet(working, pinned, request, calendars_by_competition(calendar, competitions))
    rng = random.Random(2)
    for _ in range(1500):
        moves.apply(rng)
        assert all(m.date in m.slot.days for m in working)


def _cup_request(rounds, **overrides):
    world, season, competitions = _small_world()
    cup = f.competition(
        "cup", competitions[0].teams, format="cup", min_rest_days=3, match_window_days=2,
        cup_rounds=rounds,
    )
    schedules, _ = schedule_cups([cup], season)
    commitments = cup_commitments([cup], schedules, hard_threshold=1.0)
    constraints = build_constraints(world, season, competitions, commitments)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world, season=season, competitions=competitions, cup_schedules=schedules,
        commitments=commitments, constraints=constraints, ctx=ctx, seed=1, top_n=1,
        time_budget_s=20.0, **overrides,
    )
    return request, commitments


@pytest.mark.skipif(not HAS_ORTOOLS, reason="needs ortools")
def test_cpsat_excludes_hard_cup_dates_but_keeps_contingent_ones_in_the_domain():
    from terminliste.model.calendar import build_calendar
    from terminliste.solvers.base import resolve_slot_plans
    from terminliste.solvers.cpsat import _Problem

    _, season, _ = _small_world()
    entry = season.start + timedelta(days=21)
    later = season.start + timedelta(days=42)
    request, commitments = _cup_request([
        f.cup_round("r1", forced_date=entry, name="Round 1"),
        f.cup_round("r2", forced_date=later, name="Round 2"),
    ])
    hard = {c.date for c in commitments if c.hard}
    contingent = {c.date for c in commitments if not c.hard}
    assert hard == {entry} and contingent == {later}

    calendar = build_calendar(request.world, request.season)
    problem = _Problem(request, calendar, resolve_slot_plans(request, calendar))
    men = problem.by_id["men"]
    later_slot = next(s for s in men.slot_plan.candidates if later in s.days)
    assert later_slot in men.slots and later_slot not in men.closed
    # Every club shares the cup, so treated as hard the later weekend would
    # close its slot.
    hard_everywhere = CommitmentSet(replace(c, hard=True) for c in request.commitments)
    forced = _Problem(
        replace(request, commitments=hard_everywhere), calendar, resolve_slot_plans(request, calendar)
    )
    assert later_slot in forced.by_id["men"].closed or later_slot not in forced.by_id["men"].slots

    domain: set[date] = set()
    for slot in men.slots:
        for team in men.teams:
            opponent = next(t for t in men.teams if t != team)
            days = problem.fixture_days(men, team, opponent, slot)
            assert not any(abs((d - entry).days) - 1 < 3 for d in days)
            domain.update(days)
    assert later in domain

    result = _get_scheduler("cpsat").solve(request)
    assert result.best is not None, result.notes
    assert result.best.score.feasible, result.best.score.hard_results()
    assert result.search_stats.hard_violation_counts.get("round_slot_capacity", 0) == 0


@pytest.mark.skipif(not HAS_ORTOOLS, reason="needs ortools")
def test_cpsat_level_two_prefers_a_day_clear_of_a_contingent_commitment():
    from ortools.sat.python import cp_model

    from terminliste.model.calendar import build_calendar
    from terminliste.model.schema import WEEKDAYS
    from terminliste.solvers.base import resolve_slot_plans
    from terminliste.solvers.cpsat import _build_level_two, _Placed, _Problem

    world, season, competitions = _small_world()
    base = _cpsat_request()
    calendar = build_calendar(world, season)
    problem = _Problem(base, calendar, resolve_slot_plans(base, calendar))
    men = problem.by_id["men"]
    slot = men.slots[3]
    team = men.teams[0]
    fixture = men.team_fixture[(team, 3)]
    venue = world.team(fixture.home_team).home_venue
    days = list(problem.fixture_days(men, fixture.home_team, fixture.away_team, slot))
    assert len(days) > 2
    clear = [d for d in days if d.weekday() == WEEKDAYS.index("sunday")]
    assert clear, "the competition's preferred weekday must be in the slot"
    target = clear[0]
    # The preferred weekday is the cheapest day, so only the contingent penalty
    # (3.0 x 2 x 1.0 > the 4.0 weekday bonus) can move the match off it.
    problem.commitments = f.commitment_set(
        f.commitment(team, target, hard=False, probability=1.0, min_rest_days=1, source="cup")
    )
    fixture = men.team_fixture[(team, 3)]

    item = _Placed(0, fixture, men, venue, slot, days)
    built = _build_level_two(cp_model, problem, [item], [], relax=False)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10
    assert solver.Solve(built.model) in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    chosen = next(d for d, var in built.days[0].items() if solver.Value(var))
    assert chosen != target
