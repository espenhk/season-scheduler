#!/usr/bin/env python3
"""Benchmark the local-search inner loop on the 2026 data: scores per second
for full re-evaluation against delta scoring, over one seeded move sequence.

    poetry run python scripts/bench_scoring.py [--moves 1500] [--seed 1] [--profile]
"""

from __future__ import annotations

import argparse
import cProfile
import pstats
import random
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from terminliste.frame import build_season_frame  # noqa: E402
from terminliste.model.calendar import build_calendar, calendars_by_competition  # noqa: E402
from terminliste.model.loader import load_world  # noqa: E402
from terminliste.model.travel import ApiTravelModel  # noqa: E402
from terminliste.scoring.base import EvalContext  # noqa: E402
from terminliste.scoring.incremental import DeltaScorer  # noqa: E402
from terminliste.scoring.registry import build_constraints  # noqa: E402
from terminliste.slot_planner import plan_slots  # noqa: E402
from terminliste.solvers import local_search as ls  # noqa: E402
from terminliste.solvers.base import SolveRequest  # noqa: E402
from terminliste.solvers.greedy import build_initial_schedule  # noqa: E402


def build_state(season_id: str = "2026", seed: int = 1):
    """The 2026 world, its constraints, and a greedy starting schedule."""
    world = load_world(PROJECT_ROOT / "data")
    season = world.season(season_id)
    competitions = [world.competition(c) for c in season.competitions]
    frame, _ = build_season_frame(world, season)
    calendar = build_calendar(world, season)
    slot_plans = plan_slots(world, season, competitions, calendar, frame)
    constraints = build_constraints(world, season, competitions, frame.commitments)
    ctx = EvalContext(world=world, season=season, travel=ApiTravelModel(world))
    request = SolveRequest(
        world=world,
        season=season,
        competitions=competitions,
        cup_schedules=frame.cup_schedules,
        commitments=frame.commitments,
        frame=frame,
        slot_plans=slot_plans,
        constraints=constraints,
        ctx=ctx,
        seed=seed,
    )
    initial, pinned, _ = build_initial_schedule(
        world,
        season,
        competitions,
        seed,
        calendar,
        cup_schedules=frame.cup_schedules,
        commitments=frame.commitments,
        slot_plans=slot_plans,
    )
    working = [
        ls.WorkingMatch(
            competition_id=m.competition_id,
            home_team=m.home_team,
            away_team=m.away_team,
            leg=m.leg,
            round_index=m.round_index,
            date=m.date,
            venue=m.venue,
            slot=slot_plans[m.competition_id].slot_of(m.round_index),
        )
        for m in initial
    ]
    by_competition = calendars_by_competition(calendar, competitions)
    return request, working, pinned, by_competition


def bench_full(request, working, pinned, calendars, moves: int, seed: int) -> float:
    """Scores/s when every proposed move is followed by a full evaluation."""
    move_set = ls._MoveSet(working, pinned, request, calendars)
    rng = random.Random(seed)
    scored = 0
    started = time.perf_counter()
    while scored < moves:
        undo = move_set.apply(rng)
        if undo is None:
            continue
        ls._cost(working, request.constraints, request.ctx)
        ls._restore(working, undo)
        scored += 1
    return scored / (time.perf_counter() - started)


def bench_delta(request, working, pinned, calendars, moves: int, seed: int) -> float:
    """Scores/s with incremental scoring and the live-index move filter; every
    proposal is applied, scored and undone, as in the annealer's rejections."""
    scorer = DeltaScorer(working, request.constraints, request.ctx)
    move_set = ls._MoveSet(working, pinned, request, calendars, live=scorer.live)
    rng = random.Random(seed)
    scored = 0
    started = time.perf_counter()
    while scored < moves:
        undo = move_set.apply(rng)
        if undo is None:
            continue
        scorer.propose([entry[0] for entry in undo], prune=True)
        ls._restore(working, undo)
        scorer.reject()
        scored += 1
    return scored / (time.perf_counter() - started)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--moves", type=int, default=1500)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--profile", action="store_true", help="cProfile the delta loop")
    args = parser.parse_args()

    request, working, pinned, calendars = build_state(seed=args.seed)
    print(f"{len(working)} matches, {len(request.constraints)} constraints")

    if args.profile:
        profiler = cProfile.Profile()
        profiler.enable()
        bench_delta(request, working, pinned, calendars, args.moves, args.seed)
        profiler.disable()
        pstats.Stats(profiler).sort_stats("tottime").print_stats(25)
        return 0

    full = bench_full(request, working, pinned, calendars, args.moves, args.seed)
    print(f"full evaluation : {full:9.0f} scores/s")
    delta = bench_delta(request, working, pinned, calendars, args.moves * 10, args.seed)
    print(f"delta scoring   : {delta:9.0f} scores/s  ({delta / full:.1f}x)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
