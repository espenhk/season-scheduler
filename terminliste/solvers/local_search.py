"""Simulated annealing over the constraint scorer — the default backend.

Takes the greedy schedule and improves it by repeatedly making a small change,
rescoring, and keeping the change if it helped (or, early on, sometimes even if
it did not — that is what lets it climb out of a local optimum).

Four move operators, chosen to span different scales:

* `shift` moves one match a few days inside its round window — fine adjustment.
* `swap_rounds` exchanges two rounds' dates within a leg — medium.
* `flip_orientation` swaps home and away across a mirrored fixture pair — changes
  who travels without touching the calendar.
* `swap_teams` exchanges two teams' entire seasons — the coarsest move, and the
  most effective, because a team stuck with a bad calendar can be relocated
  wholesale. It is a relabelling, so the tournament stays valid by construction.

Hard violations are folded into the cost as a large finite penalty rather than
rejected outright, so the search can cross infeasible ground to reach something
better. Feasibility is only *required* of the answer, and if none is found the
report says which rules are still broken instead of failing silently.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from datetime import date, timedelta

from ..model.calendar import SeasonCalendar, build_calendar, calendars_by_competition
from ..model.loader import World
from ..model.schema import Competition, Match
from ..rounds.kickoff import assign_kickoff_times
from ..scoring.base import HARD_PENALTY, Constraint, EvalContext, ScheduleIndex, evaluate
from .base import Candidate, ProgressUpdate, SearchStats, SolveRequest, SolverResult, select_diverse
from .greedy import build_initial_schedule


@dataclass(slots=True)
class WorkingMatch:
    """Mutable stand-in for `Match` during the search.

    Duck-types the attributes `ScheduleIndex` and the constraints read. Mutating
    in place and undoing on rejection avoids copying the whole schedule tens of
    thousands of times, which otherwise dominates the runtime.
    """

    competition_id: str
    home_team: str
    away_team: str
    leg: int
    round_index: int
    date: date
    venue: str
    # Never mutated by a move — kickoff is assigned once dates are final (see
    # `rounds/kickoff.py::assign_kickoff_times`), not searched. Present here
    # only so this duck-types `Match` for constraints that read it, such as
    # `FinalRoundSameSlot`.
    kickoff_time: str | None = None

    @property
    def key(self) -> str:
        return f"{self.competition_id}:{self.round_index}:{self.home_team}-{self.away_team}"

    def to_match(self) -> Match:
        return Match(
            competition_id=self.competition_id,
            home_team=self.home_team,
            away_team=self.away_team,
            leg=self.leg,
            round_index=self.round_index,
            date=self.date,
            venue=self.venue,
        )


# A snapshot of one match's mutable state, for undo.
_Snapshot = tuple[int, str, str, date, str]


class LocalSearchScheduler:
    """Multi-restart simulated annealing."""

    name = "local"

    # The temperatures are low, and deliberately so. Soft deltas here are small
    # — 4 points for moving off the preferred weekday, 25 for losing a
    # back-to-back home pairing — so a textbook starting temperature accepts
    # almost every bad move and spends the budget destroying the greedy
    # schedule and rebuilding it. Measured over three seeds, 0.5 scored ~2990
    # against ~2880 at 1.0 and no improvement at all over the starting point at
    # 20 or above. In practice this behaves as hill-climbing with an occasional
    # escape, which is what this landscape rewards.
    def __init__(
        self,
        restarts: int = 4,
        start_temperature: float = 0.5,
        end_temperature: float = 0.05,
    ) -> None:
        self.restarts = restarts
        self.start_temperature = start_temperature
        self.end_temperature = end_temperature

    def solve(self, request: SolveRequest) -> SolverResult:
        started = time.perf_counter()
        calendar = build_calendar(request.world, request.season)
        by_competition = calendars_by_competition(calendar, request.competitions)
        budget_per_restart = request.time_budget_s / max(1, self.restarts)

        candidates: list[Candidate] = []
        total_iterations = 0
        notes: list[str] = []
        overall_stats = SearchStats()

        for restart in range(self.restarts):
            seed = request.seed + restart * 7919
            try:
                initial, pinned = build_initial_schedule(
                    request.world,
                    request.season,
                    request.competitions,
                    seed,
                    calendar,
                    cup_schedules=request.cup_schedules,
                )
            except ValueError as exc:
                notes.append(f"restart {restart}: {exc}")
                continue

            working = [
                WorkingMatch(
                    competition_id=m.competition_id,
                    home_team=m.home_team,
                    away_team=m.away_team,
                    leg=m.leg,
                    round_index=m.round_index,
                    date=m.date,
                    venue=m.venue,
                )
                for m in initial
            ]

            best, iterations, restart_stats = _anneal(
                working,
                pinned,
                request,
                by_competition,
                seed=seed,
                budget_s=budget_per_restart,
                start_temperature=self.start_temperature,
                end_temperature=self.end_temperature,
                restart_index=restart,
                total_restarts=self.restarts,
            )
            total_iterations += iterations
            overall_stats += restart_stats

            matches = [m.to_match() for m in best]
            matches.sort(key=lambda m: (m.date, m.competition_id, m.home_team))
            matches = assign_kickoff_times(
                matches, request.competitions, request.season.fixed_requirements
            )
            detail_ctx = _with_detail(request.ctx)
            score = evaluate(matches, request.constraints, detail_ctx)
            candidates.append(
                Candidate(
                    matches=matches,
                    score=score,
                    label=f"Candidate (seed {seed})",
                    seed=seed,
                )
            )

        chosen = select_diverse(candidates, request.top_n)
        for position, candidate in enumerate(chosen, start=1):
            candidate.label = f"Option {position}"

        if not any(c.score.feasible for c in chosen):
            notes.append(
                "No fully feasible schedule found — see the hard-rule rows in the breakdown "
                "for what is still broken."
            )

        return SolverResult(
            candidates=chosen,
            solver=self.name,
            iterations=total_iterations,
            elapsed_s=time.perf_counter() - started,
            notes=notes,
            search_stats=overall_stats,
        )


def _with_detail(ctx: EvalContext) -> EvalContext:
    return EvalContext(
        world=ctx.world,
        season=ctx.season,
        travel=ctx.travel,
        detail=True,
        away_pairing_max_hours=ctx.away_pairing_max_hours,
    )


@dataclass(slots=True)
class _CostResult:
    cost: float
    # Ids of hard constraints this scenario broke — empty means viable. Built
    # from `result.count` the cost loop already computes for every
    # constraint, so tracking it costs nothing extra beyond the append.
    hard_ids: tuple[str, ...]


def _cost(matches: list[WorkingMatch], constraints: list[Constraint], ctx: EvalContext) -> _CostResult:
    index = ScheduleIndex(matches)  # type: ignore[arg-type]
    cost = 0.0
    hard_ids: list[str] = []
    for constraint in constraints:
        result = constraint.evaluate(index, ctx)
        if result.kind == "hard":
            cost += result.count * HARD_PENALTY
            if result.count:
                hard_ids.append(constraint.id)
        else:
            cost -= result.total
    return _CostResult(cost, tuple(hard_ids))


def _anneal(
    matches: list[WorkingMatch],
    pinned: set[str],
    request: SolveRequest,
    calendars: dict[str, SeasonCalendar],
    seed: int,
    budget_s: float,
    start_temperature: float,
    end_temperature: float,
    restart_index: int = 0,
    total_restarts: int = 1,
) -> tuple[list[WorkingMatch], int, SearchStats]:
    ctx = request.ctx
    constraints = request.constraints

    # Cooling is driven by iteration progress towards a target count rather
    # than by polling the clock mid-search, so the exact number of draws made
    # from the seeded RNG depends only on the seed and the calibrated rate
    # below — not on scheduling jitter from one run to the next. The target
    # count itself *is* time-budget-derived (and cached per problem size), so
    # a slower machine still gets proportionally fewer iterations instead of
    # the run simply taking longer.
    total_iterations = max(1, round(budget_s * _iterations_per_second(matches, request, calendars)))

    rng = random.Random(seed)
    moves = _MoveSet(matches, pinned, request.world, request.competitions, calendars)

    current_cost = _cost(matches, constraints, ctx).cost
    best_cost = current_cost
    best_snapshot = _capture_all(matches)

    decay = math.log(end_temperature / start_temperature)
    stats = SearchStats()
    # Throttled by iteration count, not wall-clock: a clock poll every
    # iteration would be the one thing in this loop that actually costs
    # something (see the cooling-schedule note above for why time is kept out
    # of the hot path entirely). ~40 updates per restart is plenty for a
    # human watching a terminal.
    report_every = max(1, total_iterations // 40)

    iterations = 0
    while iterations < total_iterations:
        anneal_progress = iterations / max(1, total_iterations - 1)
        temperature = start_temperature * math.exp(decay * anneal_progress)

        undo = moves.apply(rng)
        if undo is None:
            continue
        iterations += 1

        evaluated = _cost(matches, constraints, ctx)
        new_cost = evaluated.cost
        stats.investigated += 1
        if evaluated.hard_ids:
            stats.hard_violation_scenarios += 1
            stats.hard_violation_counts.update(evaluated.hard_ids)
        else:
            stats.feasible += 1

        delta = new_cost - current_cost

        if delta <= 0 or rng.random() < math.exp(-delta / max(temperature, 1e-9)):
            current_cost = new_cost
            if new_cost < best_cost:
                best_cost = new_cost
                best_snapshot = _capture_all(matches)
        else:
            _restore(matches, undo)

        if request.progress is not None and iterations % report_every == 0:
            request.progress(
                ProgressUpdate(
                    restart=restart_index,
                    total_restarts=total_restarts,
                    iterations=iterations,
                    total_iterations=total_iterations,
                    stats=stats.copy(),
                )
            )

    _restore(matches, best_snapshot)
    return matches, iterations, stats


# Iterations/second this process can push through a given problem size, keyed
# by (match count, constraint count) and measured once — the first time that
# shape is seen — rather than re-measured on every restart or every solve()
# call. Re-measuring per call is exactly what made the search non-reproducible:
# two back-to-back calls with the same seed could time slightly differently
# and so run a different number of iterations before hitting the deadline.
_RATE_CACHE: dict[tuple[int, int], float] = {}

_CALIBRATION_PROBES = 200


def _iterations_per_second(
    matches: list[WorkingMatch],
    request: SolveRequest,
    calendars: dict[str, SeasonCalendar],
) -> float:
    key = (len(matches), len(request.constraints))
    cached = _RATE_CACHE.get(key)
    if cached is not None:
        return cached

    probe_moves = _MoveSet(matches, set(), request.world, request.competitions, calendars)
    probe_rng = random.Random()
    snapshot = _capture_all(matches)

    started = time.perf_counter()
    completed = 0
    while completed < _CALIBRATION_PROBES:
        undo = probe_moves.apply(probe_rng)
        if undo is None:
            continue
        _cost(matches, request.constraints, request.ctx)
        completed += 1
    elapsed = time.perf_counter() - started

    _restore(matches, snapshot)

    rate = completed / elapsed if elapsed > 0 else float(completed)
    _RATE_CACHE[key] = rate
    return rate


def _capture_all(matches: list[WorkingMatch]) -> list[_Snapshot]:
    return [(i, m.home_team, m.away_team, m.date, m.venue) for i, m in enumerate(matches)]


def _restore(matches: list[WorkingMatch], snapshot: list[_Snapshot]) -> None:
    for index, home, away, day, venue in snapshot:
        match = matches[index]
        match.home_team = home
        match.away_team = away
        match.date = day
        match.venue = venue


class _MoveSet:
    """The four move operators, plus the indexes they need to run cheaply."""

    def __init__(
        self,
        matches: list[WorkingMatch],
        pinned: set[str],
        world: World,
        competitions: list[Competition],
        calendars: dict[str, SeasonCalendar],
    ) -> None:
        self._matches = matches
        self._pinned = pinned
        self._world = world
        self._calendars = calendars
        self._competitions = {c.id: c for c in competitions}

        # Pinned matches are tracked by list position, not by key: a key
        # contains the home team, and the orientation moves rewrite that.
        self._pinned_indexes = {i for i, m in enumerate(matches) if m.key in pinned}
        self._movable = [i for i in range(len(matches)) if i not in self._pinned_indexes]

        # Round anchor = the earliest date the round currently occupies. Used as
        # the centre of each match's allowed window.
        self._anchors: dict[tuple[str, int], date] = {}
        for match in matches:
            key = (match.competition_id, match.round_index)
            current = self._anchors.get(key)
            if current is None or match.date < current:
                self._anchors[key] = match.date

        self._rounds_by_leg: dict[tuple[str, int], list[int]] = {}
        for (competition_id, round_index), _ in self._anchors.items():
            leg = next(
                m.leg
                for m in matches
                if m.competition_id == competition_id and m.round_index == round_index
            )
            self._rounds_by_leg.setdefault((competition_id, leg), []).append(round_index)

        self._indexes_by_round: dict[tuple[str, int], list[int]] = {}
        for i, match in enumerate(matches):
            self._indexes_by_round.setdefault(
                (match.competition_id, match.round_index), []
            ).append(i)

        self._indexes_by_competition: dict[str, list[int]] = {}
        for i, match in enumerate(matches):
            self._indexes_by_competition.setdefault(match.competition_id, []).append(i)

        # Mirror map: leg-1 A-v-B <-> leg-2 B-v-A. Precomputed because the
        # orientation move needs it on every call and a linear scan of 240
        # matches would dominate that move's cost.
        self._mirror_of: dict[int, int] = {}
        seen: dict[tuple[str, str, str], int] = {}
        for i, match in enumerate(matches):
            pair = tuple(sorted((match.home_team, match.away_team)))
            lookup = (match.competition_id, pair[0], pair[1])
            partner = seen.pop(lookup, None)
            if partner is None:
                seen[lookup] = i
            else:
                self._mirror_of[i] = partner
                self._mirror_of[partner] = i

    def apply(self, rng: random.Random) -> list[_Snapshot] | None:
        roll = rng.random()
        if roll < 0.55:
            return self._shift(rng)
        if roll < 0.75:
            return self._swap_rounds(rng)
        if roll < 0.90:
            return self._flip_orientation(rng)
        return self._swap_teams(rng)

    # -- fine: move one match within its round window ----------------------

    def _shift(self, rng: random.Random) -> list[_Snapshot] | None:
        if not self._movable:
            return None
        index = rng.choice(self._movable)
        match = self._matches[index]
        competition = self._competitions.get(match.competition_id)
        if competition is None:
            return None

        anchor = self._anchors[(match.competition_id, match.round_index)]
        calendar = self._calendars[match.competition_id]
        window = calendar.window(anchor, competition.match_window_days, match.venue)
        window = [d for d in window if d != match.date]
        if not window:
            return None

        snapshot = [(index, match.home_team, match.away_team, match.date, match.venue)]
        match.date = rng.choice(window)
        return snapshot

    # -- medium: exchange two rounds' dates within a leg -------------------

    def _swap_rounds(self, rng: random.Random) -> list[_Snapshot] | None:
        keys = [k for k, rounds in self._rounds_by_leg.items() if len(rounds) >= 2]
        if not keys:
            return None
        competition_id, leg = rng.choice(keys)
        rounds = self._rounds_by_leg[(competition_id, leg)]
        round_a, round_b = rng.sample(rounds, 2)

        anchor_a = self._anchors[(competition_id, round_a)]
        anchor_b = self._anchors[(competition_id, round_b)]
        offset = (anchor_b - anchor_a).days
        if offset == 0:
            return None

        snapshot: list[_Snapshot] = []
        for round_index, shift in ((round_a, offset), (round_b, -offset)):
            for index in self._indexes_by_round[(competition_id, round_index)]:
                match = self._matches[index]
                if index in self._pinned_indexes:
                    # A pinned match anchors its round; moving the round around
                    # it would silently break the requirement it satisfies.
                    return self._abort(snapshot)
                snapshot.append((index, match.home_team, match.away_team, match.date, match.venue))
                match.date = match.date + timedelta(days=shift)

        self._anchors[(competition_id, round_a)] = anchor_b
        self._anchors[(competition_id, round_b)] = anchor_a
        return snapshot

    def _abort(self, snapshot: list[_Snapshot]) -> None:
        _restore(self._matches, snapshot)
        return None

    # -- who travels: flip a mirrored pair, both legs together --------------

    def _flip_orientation(self, rng: random.Random) -> list[_Snapshot] | None:
        if not self._movable:
            return None
        index = rng.choice(self._movable)
        match = self._matches[index]

        # Flipping only one leg would leave a pair meeting twice at the same
        # ground, so find the mirror and flip both.
        mirror_index = self._find_mirror(index)
        if mirror_index is None:
            return None
        if mirror_index in self._pinned_indexes:
            return None
        mirror = self._matches[mirror_index]

        snapshot = [
            (index, match.home_team, match.away_team, match.date, match.venue),
            (mirror_index, mirror.home_team, mirror.away_team, mirror.date, mirror.venue),
        ]
        for target in (match, mirror):
            target.home_team, target.away_team = target.away_team, target.home_team
            target.venue = self._world.team(target.home_team).home_venue
        return snapshot

    def _find_mirror(self, index: int) -> int | None:
        return self._mirror_of.get(index)

    # -- coarse: exchange two teams' entire seasons -------------------------

    def _swap_teams(self, rng: random.Random) -> list[_Snapshot] | None:
        competition_id = rng.choice(list(self._indexes_by_competition))
        competition = self._competitions.get(competition_id)
        if competition is None or len(competition.teams) < 2:
            return None
        team_a, team_b = rng.sample(list(competition.teams), 2)

        snapshot: list[_Snapshot] = []
        for index in self._indexes_by_competition[competition_id]:
            match = self._matches[index]
            if team_a not in (match.home_team, match.away_team) and team_b not in (
                match.home_team,
                match.away_team,
            ):
                continue
            if index in self._pinned_indexes:
                return self._abort(snapshot)
            snapshot.append((index, match.home_team, match.away_team, match.date, match.venue))
            match.home_team = _swap(match.home_team, team_a, team_b)
            match.away_team = _swap(match.away_team, team_a, team_b)
            match.venue = self._world.team(match.home_team).home_venue

        return snapshot or None


def _swap(team: str, a: str, b: str) -> str:
    if team == a:
        return b
    if team == b:
        return a
    return team
