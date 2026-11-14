"""Delta scoring must agree with full evaluation after every move (#11).

Rule authors: a rule's `delta_scope` is only verified here, so make sure the
state below activates the rule.
"""

from __future__ import annotations

import importlib.util
import random
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from terminliste.model.schema import FixedRoundDate
from terminliste.scoring.base import EvalContext, ScheduleIndex
from terminliste.scoring.hard import FixedRoundDates, RoundDayQuota
from terminliste.scoring.incremental import DeltaScorer
from terminliste.solvers import local_search as ls

_BENCH = Path(__file__).resolve().parents[2] / "scripts" / "bench_scoring.py"


def _load_bench():
    spec = importlib.util.spec_from_file_location("bench_scoring", _BENCH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("bench_scoring", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def state():
    return _load_bench().build_state(seed=3)


def _copy(working):
    return [ls.WorkingMatch(**{f: getattr(m, f) for f in m.__slots__}) for m in working]


def _with_format_rules(state):
    """The state with `fixed_round_dates` and `round_day_quota` active, which
    the 2026 data does not use, so their scopes are exercised too."""
    request, working, pinned, calendars = state
    first = next(m for m in working if m.round_index == 2)
    competitions = [
        c.model_copy(
            update={
                "day_quota": {"saturday": 2, "sunday": 2},
                "fixed_round_dates": [FixedRoundDate(round=3, date=first.date)],
            }
        )
        if c.format == "league"
        else c
        for c in request.competitions
    ]
    constraints = [
        *request.constraints,
        FixedRoundDates(competitions=competitions),
        RoundDayQuota(competitions=competitions),
    ]
    return replace(request, constraints=constraints), working, pinned, calendars


def _keys(mapping) -> dict:
    return {k: sorted(m.key for m in v) for k, v in mapping.items() if v}


def _plan_of(analysis):
    return (
        {k: (f.date, f.kind, f.trigger_node_ids) for k, f in analysis.plan.fallbacks.items()},
        sorted(analysis.plan.uncovered),
        sorted(analysis.invalid),
        sorted(analysis.collisions),
    )


def _assert_agrees(scorer: DeltaScorer, working, request) -> None:
    """Per-rule totals and counts, the index and the fallback plan all equal
    what a from-scratch evaluation of the same matches gives."""
    index = ScheduleIndex(working)
    for ci, constraint in enumerate(request.constraints):
        result = constraint.evaluate(index, request.ctx)
        assert scorer._count[ci] == result.count, constraint.id
        assert scorer._total[ci] == pytest.approx(result.total, abs=1e-6), constraint.id
    assert scorer.cost == pytest.approx(ls._cost(working, request.constraints, request.ctx).cost)

    live = scorer.live
    assert _keys(live.by_team) == _keys(index.by_team)
    assert _keys(live.by_venue_date) == _keys(index.by_venue_date)
    assert _keys(live.by_date) == _keys(index.by_date)
    assert _keys(live.by_competition) == _keys(index.by_competition)
    assert {t: d for t, d in live.home_dates_by_team.items() if d} == {
        t: d for t, d in index.home_dates_by_team.items() if d
    }
    assert {t: d for t, d in live.away_dates_by_team.items() if d} == {
        t: d for t, d in index.away_dates_by_team.items() if d
    }
    assert live.away_venue_by_team_date == index.away_venue_by_team_date

    if scorer.tracker is not None:
        fresh = scorer._planner.analyse(ScheduleIndex(working))
        assert _plan_of(scorer.tracker.analysis) == _plan_of(fresh)
        assert scorer.tracker._bounds == scorer._planner.leg_bounds_of(ScheduleIndex(working))
        assert [(m.key, t) for m, t in scorer.tracker.analysis.exposed] == [
            (m.key, t) for m, t in fresh.exposed
        ]


def _raw_team_swap(working, rng) -> list:
    """Two teams of one competition exchanged in every match, with no
    `_date_ok` filtering, so any resulting violation reaches the scorer."""
    competition_id = rng.choice(sorted({m.competition_id for m in working}))
    in_competition = [(i, m) for i, m in enumerate(working) if m.competition_id == competition_id]
    teams = sorted({t for _, m in in_competition for t in (m.home_team, m.away_team)})
    a, b = rng.sample(teams, 2)
    undo = []
    for i, match in in_competition:
        if {a, b}.isdisjoint((match.home_team, match.away_team)):
            continue
        undo.append(ls._snap(i, match))
        match.home_team = ls._swap(match.home_team, a, b)
        match.away_team = ls._swap(match.away_team, a, b)
    return undo


def _raw_move(working, rng, moves) -> list | None:
    """One of the annealer's operators or an unfiltered edit, as snapshots."""
    roll = rng.random()
    if roll < 0.2:
        # Unfiltered shift inside the slot: rest, venue and club violations.
        i = rng.randrange(len(working))
        undo = [ls._snap(i, working[i])]
        working[i].date = rng.choice(working[i].slot.days)
        return undo
    if roll < 0.3:
        # Unfiltered shift anywhere within a month: also leaves the slot, so
        # leg order, blackouts and the fallback footprints are stressed.
        i = rng.randrange(len(working))
        undo = [ls._snap(i, working[i])]
        working[i].date += timedelta(days=rng.randint(-30, 30))
        return undo
    if roll < 0.37:
        i, j = rng.sample(range(len(working)), 2)
        undo = [ls._snap(i, working[i]), ls._snap(j, working[j])]
        working[i].date, working[j].date = working[j].date, working[i].date
        return undo
    if roll < 0.45:
        # One leg flipped alone: a key change with no mirrored partner.
        i = rng.randrange(len(working))
        match = working[i]
        undo = [ls._snap(i, match)]
        match.home_team, match.away_team = match.away_team, match.home_team
        match.venue = moves._world.team(match.home_team).home_venue
        return undo
    if roll < 0.52:
        return _raw_team_swap(working, rng) or None
    return moves.apply(rng)


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_delta_matches_full_evaluation_on_random_move_sequences(state, seed):
    request, working, pinned, calendars = _with_format_rules(state)
    working = _copy(working)
    scorer = DeltaScorer(working, request.constraints, request.ctx)
    moves = ls._MoveSet(working, pinned, request, calendars, live=scorer.live)
    rng = random.Random(seed)

    _assert_agrees(scorer, working, request)
    operators = set()
    steps = violations = replans = 0
    hard_seen: set[str] = set()
    while steps < 350:
        undo = _raw_move(working, rng, moves)
        if not undo:
            continue
        operators.add(moves.last_operator)
        steps += 1
        changed = [entry[0] for entry in undo]
        replans_before = scorer.tracker.replans
        prune = rng.random() < 0.3
        cost, broken = scorer.propose(changed, prune=prune)
        full = ls._cost(working, request.constraints, request.ctx)
        assert broken == full.hard_ids
        if cost != float("inf"):
            assert cost == pytest.approx(full.cost)
        else:
            assert full.cost >= scorer.cost + 5_000
        violations += bool(broken)
        hard_seen.update(broken)
        replans += scorer.tracker.replans - replans_before
        if rng.random() < 0.5 and cost != float("inf"):
            scorer.accept()
        else:
            ls._restore(working, undo)
            scorer.reject()
        _assert_agrees(scorer, working, request)

    assert {0, 1, 2} <= operators
    assert violations > 20
    assert {"fixed_round_dates", "round_day_quota", "contingency_coverage"} & hard_seen
    assert replans, "the fallback plan was never recomputed"


def test_the_fallback_plan_is_skipped_for_moves_that_cannot_reach_it(state):
    """Most proposals touch nothing the plan reads; the tracker must not
    replan for those, or the speedup is gone."""
    request, working, pinned, calendars = state
    working = _copy(working)
    scorer = DeltaScorer(working, request.constraints, request.ctx)
    moves = ls._MoveSet(working, pinned, request, calendars, live=scorer.live)
    rng = random.Random(2)

    proposals = 0
    while proposals < 400:
        undo = moves.apply(rng)
        if undo is None:
            continue
        proposals += 1
        scorer.propose([entry[0] for entry in undo], prune=True)
        ls._restore(working, undo)
        scorer.reject()
    assert 0 < scorer.tracker.replans < proposals / 3


def test_every_declared_scope_is_exercised_and_resync_is_a_noop(state):
    request, working, _, _ = state
    working = _copy(working)
    scorer = DeltaScorer(working, request.constraints, request.ctx)
    scopes = {getattr(c, "delta_scope", None) for c in request.constraints}
    assert {"team", "club", "venue_date", "match", "competition", "contingency"} <= scopes

    before = scorer.cost
    scorer.resync()
    assert abs(scorer.cost - before) < 1e-6


def test_pruned_proposal_is_rejected_and_leaves_the_scorer_intact(state):
    request, working, pinned, calendars = state
    working = _copy(working)
    scorer = DeltaScorer(working, request.constraints, request.ctx)
    moves = ls._MoveSet(working, pinned, request, calendars, live=scorer.live)
    rng = random.Random(5)

    pruned = 0
    for _ in range(300):
        undo = moves.apply(rng)
        if undo is None:
            continue
        cost, _ = scorer.propose([entry[0] for entry in undo], prune=True)
        if cost == float("inf"):
            pruned += 1
        ls._restore(working, undo)
        scorer.reject()
    _assert_agrees(scorer, working, request)
    assert pruned > 0


# -- a small world where the fallback plan is crowded --------------------------


class _Hours:
    def hours(self, a, b):
        return 1.0


def _round_robin(teams: list[str]) -> list[list[tuple[str, str]]]:
    """Circle-method rounds of (home, away); the second leg mirrors the first."""
    rotating = list(teams)
    rounds = []
    for r in range(len(teams) - 1):
        pairs = [(rotating[i], rotating[-1 - i]) for i in range(len(teams) // 2)]
        rounds.append([(a, b) if (r + i) % 2 == 0 else (b, a) for i, (a, b) in enumerate(pairs)])
        rotating = [rotating[0], rotating[-1], *rotating[1:-1]]
    return rounds + [[(b, a) for a, b in pairs] for pairs in rounds]


def _crowded_state():
    """Two leagues with clubs sharing a ground and a club, contingent
    commitments on most teams and reserve dates: every footprint cell (rest,
    venue, club sibling, leg edge) can be hit by a random edit."""
    import factories as f

    from terminliste.model.schema import ReserveDate
    from terminliste.scoring.registry import build_constraints

    men = ["a_m", "b_m", "c_m", "d_m", "e_m", "f_m"]
    women = ["a_w", "b_w", "g_w", "h_w"]
    teams = [
        f.team("a_m", "A", "va"),
        f.team("a_w", "A", "va_w", gender="women"),  # a club with two grounds
        f.team("b_m", "B", "vb"),
        f.team("b_w", "B", "vb_w", gender="women"),
        *(f.team(t, t[0].upper(), f"v{t[0]}") for t in ("c_m", "d_m", "e_m", "f_m")),
        f.team("g_w", "G", "vc", gender="women"),  # shares c_m's ground
        f.team("h_w", "H", "vh", gender="women"),
    ]
    clubs = {}
    for team in teams:
        clubs.setdefault(team.club_id, []).append(team)
    venues = sorted({t.home_venue for t in teams})
    leagues = [
        f.competition("men", men, min_rest_days=3),
        f.competition("women", women, gender="women", preferred_weekday="saturday", min_rest_days=3),
    ]
    world = f.world(
        [f.club(cid, members) for cid, members in clubs.items()],
        [f.venue(v) for v in venues],
        leagues,
    )
    season = f.season(
        competitions=["men", "women"],
        start=date(2026, 3, 1),
        end=date(2026, 11, 30),
        reserve_dates=[ReserveDate(date=date(2026, 6, 10), capacity=2)],
    )

    commitments = []
    # a_w has none, so its home dates are read by the plan only through its
    # sibling a_m and no rest cell of its own.
    for k, team in enumerate(t for t in men + women if t != "a_w"):
        for n, day in enumerate((date(2026, 5, 3), date(2026, 7, 5), date(2026, 9, 6))):
            moved = day + timedelta(days=7 * ((k + n) % 4))
            commitments.append(
                f.commitment(
                    team, moved, hard=False, probability=0.3 + 0.1 * (k % 4), min_rest_days=3,
                    node_id=f"{team}/uecl/{n}",
                )
            )
    commitments.append(f.commitment("c_m", date(2026, 4, 19), hard=True, min_rest_days=3))
    commitment_set = f.commitment_set(*commitments)
    constraints = build_constraints(world, season, leagues, commitment_set)

    working = []
    for comp, league_teams, first_day in (("men", men, date(2026, 3, 8)), ("women", women, date(2026, 3, 7))):
        for r, pairs in enumerate(_round_robin(league_teams)):
            for home, away in pairs:
                working.append(
                    ls.WorkingMatch(
                        competition_id=comp, home_team=home, away_team=away,
                        leg=1 if r < len(league_teams) - 1 else 2, round_index=r,
                        date=first_day + timedelta(days=14 * r), venue=world.team(home).home_venue,
                    )
                )
    return constraints, EvalContext(world=world, season=season, travel=_Hours()), working


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_delta_matches_full_evaluation_in_a_crowded_world(seed):
    constraints, ctx, working = _crowded_state()
    request = SimpleNamespace(constraints=constraints, ctx=ctx)
    scorer = DeltaScorer(working, constraints, ctx)
    assert scorer.tracker is not None and scorer.tracker.analysis.exposed
    rng = random.Random(seed)
    _assert_agrees(scorer, working, request)

    hard_seen: set[str] = set()
    for _ in range(500):
        kind = rng.random()
        if kind < 0.5:
            i = rng.randrange(len(working))
            undo = [ls._snap(i, working[i])]
            working[i].date += timedelta(days=rng.choice([-14, -7, -3, -2, -1, 1, 2, 3, 7, 14]))
        elif kind < 0.7:
            i, j = rng.sample(range(len(working)), 2)
            undo = [ls._snap(i, working[i]), ls._snap(j, working[j])]
            working[i].date, working[j].date = working[j].date, working[i].date
        elif kind < 0.85:
            i = rng.randrange(len(working))
            match = working[i]
            undo = [ls._snap(i, match)]
            match.home_team, match.away_team = match.away_team, match.home_team
            match.venue = ctx.world.team(match.home_team).home_venue
        else:
            undo = _raw_team_swap(working, rng)
            if not undo:
                continue
        cost, broken = scorer.propose([entry[0] for entry in undo])
        full = ls._cost(working, constraints, ctx)
        assert cost == pytest.approx(full.cost)
        assert broken == full.hard_ids
        hard_seen.update(broken)
        if rng.random() < 0.5:
            scorer.accept()
        else:
            ls._restore(working, undo)
            scorer.reject()
        _assert_agrees(scorer, working, request)

    assert {"contingency_coverage", "venue_double_booking", "club_home_clash"} <= hard_seen


def _foreign_cell_edit(kind, working, scorer):
    """An edit of one non-exposed match onto a single cell the plan reads for an
    exposed match, touching no rest-by-team cell and no first or last day of a
    leg and exposing nothing, so only that cell can tell the tracker to replan.
    None when the world has no such edit."""
    tracker = scorer.tracker
    footprint = tracker._footprint
    exposed_keys = {m.key for m, _ in tracker.analysis.exposed}
    if kind == "key":
        cells = [(m, m.date) for m, _ in tracker.analysis.exposed]
    elif kind == "venue":
        cells = sorted(footprint.venue_days)
    else:
        cells = sorted(footprint.home_days)

    for cell in cells:
        for i, match in enumerate(working):
            if match.key in exposed_keys:
                continue
            home, away, venue, day = match.home_team, match.away_team, match.venue, match.date
            if kind == "venue":
                venue, day = cell
                only = (home, day) not in footprint.home_days
            elif kind == "home":
                if home != cell[0]:
                    continue
                day = cell[1]
                only = (venue, day) not in footprint.venue_days
            else:
                # The plan finds matches by key; a later duplicate of the
                # exposed match shadows it.
                target = cell[0]
                home, away = target.home_team, target.away_team
                only = i > working.index(target)
            if not only or any(day in footprint.team_days.get(t, ()) for t in (home, away)):
                continue
            undo = [ls._snap(i, match)]
            saved = (match.round_index, match.competition_id)
            match.home_team, match.away_team, match.venue, match.date = home, away, venue, day
            if kind == "key":
                match.round_index, match.competition_id = target.round_index, target.competition_id
            moved_edge = any(tracker._edge_value(e) != v for e, v in tracker._edges.items())
            if not tracker.planner.triggers_of(match) and not moved_edge:
                return undo
            ls._restore(working, undo)
            match.round_index, match.competition_id = saved
    return None


@pytest.mark.parametrize("kind", ["venue", "home", "key"])
def test_the_plan_is_recomputed_when_a_move_lands_on_a_cell_it_reads(kind):
    constraints, ctx, working = _crowded_state()
    request = SimpleNamespace(constraints=constraints, ctx=ctx)
    scorer = DeltaScorer(working, constraints, ctx)
    undo = _foreign_cell_edit(kind, working, scorer)
    if undo is None:
        pytest.skip(f"no {kind}-only edit available in this world")

    before = scorer.tracker.replans
    scorer.propose([undo[0][0]])
    assert scorer.tracker.replans == before + 1
    scorer.accept()
    _assert_agrees(scorer, working, request)
