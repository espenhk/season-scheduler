#!/usr/bin/env python3
"""Scores per second of the local search's inner loop on the 2026 data (#11).

Replays one seeded sequence of the annealer's own moves twice: scoring each
proposal from scratch (`local_search._cost`, what the loop did before delta
scoring) and through `DeltaScorer`. Rates depend on the machine, so compare
the two against each other, and run with `--rules` to see which rules still
cost time.

    python scripts/bench_scoring.py [--moves 3000] [--seed 1] [--rules]
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from terminliste.frame import build_season_frame  # noqa: E402
from terminliste.model.calendar import build_calendar, calendars_by_competition  # noqa: E402
from terminliste.model.loader import load_world  # noqa: E402
from terminliste.model.travel import ApiTravelModel  # noqa: E402
from terminliste.scoring.base import EvalContext, ScheduleIndex  # noqa: E402
from terminliste.scoring.incremental import DeltaScorer  # noqa: E402
from terminliste.scoring.registry import build_constraints  # noqa: E402
from terminliste.slot_planner import plan_slots  # noqa: E402
from terminliste.solvers import SolveRequest  # noqa: E402
from terminliste.solvers import local_search as ls  # noqa: E402
from terminliste.solvers.greedy import build_initial_schedule  # noqa: E402

# Where the annealer sits while it does most of its work, so the walk drifts
# the way a real run does instead of piling up violations.
_TEMPERATURE = 0.2


def build_state(seed: int = 1, season_id: str = "2026"):
    """The request, a greedy starting schedule as `WorkingMatch`es, its pinned
    keys and the per-competition calendars, as `local_search._anneal` sees them."""
    world = load_world(PROJECT_ROOT / "data")
    season = world.season(season_id)
    competitions = [world.competition(c) for c in season.competitions]
    frame, _ = build_season_frame(world, season)
    calendar = build_calendar(world, season)
    slot_plans = plan_slots(world, season, competitions, calendar, frame)
    request = SolveRequest(
        world=world,
        season=season,
        competitions=competitions,
        cup_schedules=frame.cup_schedules,
        commitments=frame.commitments,
        frame=frame,
        slot_plans=slot_plans,
        constraints=build_constraints(world, season, competitions, frame.commitments),
        ctx=EvalContext(world=world, season=season, travel=ApiTravelModel(world)),
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
    return request, working, pinned, calendars_by_competition(calendar, competitions)


def bench_full(request, working, pinned, calendars, count: int, seed: int) -> float:
    moves = ls._MoveSet(working, pinned, request, calendars)
    rng = random.Random(seed)
    done = 0
    spent = 0.0
    current = ls._cost(working, request.constraints, request.ctx).cost
    while done < count:
        undo = moves.apply(rng)
        if undo is None:
            continue
        started = time.perf_counter()
        cost = ls._cost(working, request.constraints, request.ctx).cost
        spent += time.perf_counter() - started
        done += 1
        delta = cost - current
        if delta <= 0 or rng.random() < math.exp(-delta / _TEMPERATURE):
            current = cost
        else:
            ls._restore(working, undo)
    return done / spent


def bench_delta(request, working, pinned, calendars, count: int, seed: int) -> tuple[float, float]:
    """(scores/s of the scorer alone, proposals/s including move generation)."""
    scorer = DeltaScorer(working, request.constraints, request.ctx)
    moves = ls._MoveSet(working, pinned, request, calendars, live=scorer.live)
    rng = random.Random(seed)
    done = 0
    spent = 0.0
    started_all = time.perf_counter()
    while done < count:
        undo = moves.apply(rng)
        if undo is None:
            continue
        started = time.perf_counter()
        cost, _ = scorer.propose([entry[0] for entry in undo], prune=True)
        spent += time.perf_counter() - started
        done += 1
        delta = cost - scorer.cost
        if delta <= 0 or rng.random() < math.exp(-delta / _TEMPERATURE):
            scorer.accept()
        else:
            ls._restore(working, undo)
            scorer.reject()
    return done / spent, done / (time.perf_counter() - started_all)


def rule_times(request, working, pinned, calendars, count: int, seed: int) -> Counter:
    """Seconds each rule spends over `count` full evaluations."""
    moves = ls._MoveSet(working, pinned, request, calendars)
    rng = random.Random(seed)
    spent: Counter = Counter()
    done = 0
    while done < count:
        undo = moves.apply(rng)
        if undo is None:
            continue
        index = ScheduleIndex(working)
        for constraint in request.constraints:
            started = time.perf_counter()
            constraint.evaluate(index, request.ctx)
            spent[constraint.id] += time.perf_counter() - started
        done += 1
        if rng.random() < 0.5:
            ls._restore(working, undo)
    return spent


def _copy(working):
    return [ls.WorkingMatch(**{f: getattr(m, f) for f in m.__slots__}) for m in working]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--moves", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--rules", action="store_true", help="also list per-rule full-evaluation time")
    args = parser.parse_args()

    request, working, pinned, calendars = build_state(args.seed)
    print(f"{len(working)} matches, {len(request.constraints)} rules")
    full = bench_full(request, _copy(working), pinned, calendars, max(200, args.moves // 6), args.seed)
    delta, whole = bench_delta(request, _copy(working), pinned, calendars, args.moves, args.seed)
    print(f"full re-score : {full:9,.0f} scores/s")
    print(f"delta scoring : {delta:9,.0f} scores/s  ({delta / full:.1f}x)")
    print(f"whole loop    : {whole:9,.0f} proposals/s with move generation")
    if args.rules:
        spent = rule_times(request, _copy(working), pinned, calendars, 300, args.seed)
        total = sum(spent.values())
        print(f"\nfull evaluation, ms per score (total {1000 * total / 300:.2f}):")
        for rule, seconds in spent.most_common(12):
            print(f"  {rule:32s} {1000 * seconds / 300:6.3f}")


if __name__ == "__main__":
    main()
