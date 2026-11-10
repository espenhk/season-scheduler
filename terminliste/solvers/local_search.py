"""Simulated annealing over the constraint scorer — the default backend.

Takes the greedy schedule and improves it by repeatedly making a small change,
rescoring, and keeping the change if it helped (or, early on, sometimes even if
it did not — that is what lets it climb out of a local optimum).

Rounds sit in fixed slots chosen up front (`slot_planner.py`), so no move can
take a match out of its round's slot (#7). Four move operators span different
scales:

* `shift_day` moves one match to another day of its round's slot — fine.
* `swap_rounds` exchanges two rounds' slots within a leg, refusing a swap that
  lands any team on a blackout, cup or European date — medium.
* `flip_orientation` swaps home and away across a mirrored fixture pair,
  changing who travels without touching the calendar.
* `swap_teams` exchanges two teams' entire seasons — the coarsest move, and
  the most effective, since a team stuck with a bad calendar can be relocated
  wholesale. It is a relabelling, so the tournament stays valid.

Hard violations are folded into the cost as a large finite penalty rather than
rejected outright, so the search can cross infeasible ground to reach
something better; if the answer is still infeasible the report says which
rules are broken rather than failing silently.

Search runs in two phases (#98). The restarts each rebuild from a fresh greedy
schedule, covering ground broadly. `_polish_candidates` then anneals each
shortlisted candidate further from its own final state at a low temperature,
nudging dates locally — how a published fixture list actually gets adjusted,
rather than one more blind draw.
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, replace
from datetime import date

from ..model.calendar import SeasonCalendar, build_calendar, calendars_by_competition
from ..model.schema import Match
from ..model.slots import RoundSlot
from ..rounds.cup_schedule import cup_conflict, resolved_cup_windows
from ..rounds.european_schedule import european_conflict
from ..rounds.kickoff import assign_kickoff_times
from ..scoring.base import (
    HARD_PENALTY,
    Constraint,
    EvalContext,
    ScheduleIndex,
    evaluate,
)
from ..slot_planner import SlotPlan
from .base import (
    Candidate,
    ProgressUpdate,
    SearchStats,
    SolveRequest,
    SolverResult,
    resolve_slot_plans,
    select_diverse,
)
from .greedy import build_initial_schedule


@dataclass(slots=True)
class WorkingMatch:
    """Mutable stand-in for `Match` during the search.

    Duck-types what `ScheduleIndex` and the constraints read. Mutating in place
    and undoing on rejection avoids copying the whole schedule tens of
    thousands of times, which would otherwise dominate the runtime.
    """

    competition_id: str
    home_team: str
    away_team: str
    leg: int
    round_index: int
    date: date
    venue: str
    # Never mutated by a move — kickoff is assigned once dates are final, not
    # searched. Here only so constraints like `FinalRoundSameSlot` can read it.
    kickoff_time: str | None = None
    # The slot this match's round sits in. Round swaps rewrite it, so it
    # follows the round rather than its index.
    slot: RoundSlot | None = None

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
_Snapshot = tuple[int, str, str, date, str, RoundSlot | None]


class LocalSearchScheduler:
    """Multi-restart simulated annealing."""

    name = "local"

    # Temperatures are deliberately low. Soft deltas are small — 4 points for
    # moving off the preferred weekday, 25 for losing a back-to-back home
    # pairing — so a textbook starting temperature accepts nearly every bad
    # move and spends the budget demolishing the greedy schedule. Over three
    # seeds, 0.5 scored ~2990 against ~2880 at 1.0, and 20 or above never beat
    # the starting point: this landscape rewards hill-climbing with the
    # occasional escape.
    #
    # `polish_fraction` diverts that share of the budget from the restarts to
    # the polish phase (#98), which cools from `end_temperature` down to
    # `polish_end_temperature` — near hill-climbing, to fix the last trouble
    # spots rather than explore again.
    def __init__(
        self,
        restarts: int = 4,
        start_temperature: float = 0.5,
        end_temperature: float = 0.05,
        polish_fraction: float = 0.25,
        polish_end_temperature: float = 0.01,
    ) -> None:
        self.restarts = restarts
        self.start_temperature = start_temperature
        self.end_temperature = end_temperature
        self.polish_fraction = polish_fraction
        self.polish_end_temperature = polish_end_temperature

    def solve(self, request: SolveRequest) -> SolverResult:
        started = time.perf_counter()
        calendar = build_calendar(request.world, request.season)
        by_competition = calendars_by_competition(calendar, request.competitions)
        slot_plans = resolve_slot_plans(request, calendar)
        request = replace(request, slot_plans=slot_plans)
        explore_budget_s = request.time_budget_s * (1 - self.polish_fraction)
        polish_budget_s = request.time_budget_s * self.polish_fraction
        budget_per_restart = explore_budget_s / max(1, self.restarts)

        candidates: list[Candidate] = []
        # Parallel to `candidates`: each restart's final annealed state and
        # pinned set, so the polish phase can resume exactly where the restart
        # left off. The `Match` objects can't serve — they have been sorted and
        # lost the pinned-index bookkeeping.
        restart_states: list[tuple[list[WorkingMatch], set[str]]] = []
        total_iterations = 0
        notes: list[str] = []
        overall_stats = SearchStats()

        for restart in range(self.restarts):
            seed = request.seed + restart * 7919
            try:
                initial, pinned, pin_warnings = build_initial_schedule(
                    request.world,
                    request.season,
                    request.competitions,
                    seed,
                    calendar,
                    cup_schedules=request.cup_schedules,
                    european_commitments=request.european_commitments,
                    slot_plans=slot_plans,
                )
            except ValueError as exc:
                notes.append(f"restart {restart}: {exc}")
                continue
            for warning in pin_warnings:
                if warning not in notes:
                    notes.append(warning)

            working = [
                WorkingMatch(
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
                    round_slots=_round_slots(best),
                )
            )
            restart_states.append((best, pinned))

        chosen = select_diverse(candidates, request.top_n)

        if polish_budget_s > 0 and chosen:
            polish_iterations, polish_stats = _polish_candidates(
                chosen,
                candidates,
                restart_states,
                request,
                by_competition,
                budget_s=polish_budget_s,
                restarts=self.restarts,
                start_temperature=self.end_temperature,
                end_temperature=self.polish_end_temperature,
                notes=notes,
            )
            total_iterations += polish_iterations
            overall_stats += polish_stats

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
            slot_plans=slot_plans,
        )


def _round_slots(matches: list[WorkingMatch]) -> dict[tuple[str, int], RoundSlot]:
    """Where each round ended up: round-swap moves reorder rounds across slots."""
    return {
        (m.competition_id, m.round_index): m.slot for m in matches if m.slot is not None
    }


def _assign_missing_slots(
    matches: list[WorkingMatch], slot_plans: dict[str, SlotPlan]
) -> None:
    """Fallback slots for states built outside `solve` (tests); `solve` sets
    them exactly, so production never reaches this."""
    dates_by_round: dict[tuple[str, int], list[date]] = {}
    for match in matches:
        if match.slot is None:
            dates_by_round.setdefault((match.competition_id, match.round_index), []).append(
                match.date
            )
    slots: dict[tuple[str, int], RoundSlot] = {}
    for (competition_id, round_index), dates in dates_by_round.items():
        earliest = min(dates)
        chosen = slot_plans[competition_id].chosen
        slots[(competition_id, round_index)] = next(
            (slot for slot in chosen if slot.contains(earliest)),
            min(chosen, key=lambda slot: abs((slot.default_day - earliest).days)),
        )
    for match in matches:
        if match.slot is None:
            match.slot = slots[(match.competition_id, match.round_index)]


def _polish_candidates(
    chosen: list[Candidate],
    candidates: list[Candidate],
    restart_states: list[tuple[list[WorkingMatch], set[str]]],
    request: SolveRequest,
    calendars: dict[str, SeasonCalendar],
    budget_s: float,
    restarts: int,
    start_temperature: float,
    end_temperature: float,
    notes: list[str],
) -> tuple[int, SearchStats]:
    """Anneal each shortlisted candidate further from its own final state, at a
    low temperature so moves stay local (#98).

    A polished candidate replaces the original only when it scores strictly
    better, so this phase can never leave a schedule worse than phase one
    found it.
    """
    per_candidate_budget = budget_s / len(chosen)
    total_iterations = 0
    stats = SearchStats()
    improved = 0

    for position, candidate in enumerate(chosen):
        source_index = next(i for i, c in enumerate(candidates) if c is candidate)
        working_state, pinned = restart_states[source_index]
        working_copy = [replace(m) for m in working_state]

        polished, iterations, restart_stats = _anneal(
            working_copy,
            pinned,
            request,
            calendars,
            seed=candidate.seed + 1_000_003 + position,
            budget_s=per_candidate_budget,
            start_temperature=start_temperature,
            end_temperature=end_temperature,
            restart_index=restarts + position,
            total_restarts=restarts + len(chosen),
        )
        total_iterations += iterations
        stats += restart_stats

        matches = [m.to_match() for m in polished]
        matches.sort(key=lambda m: (m.date, m.competition_id, m.home_team))
        matches = assign_kickoff_times(
            matches, request.competitions, request.season.fixed_requirements
        )
        score = evaluate(matches, request.constraints, _with_detail(request.ctx))
        if score.sort_key < candidate.score.sort_key:
            candidate.matches = matches
            candidate.score = score
            candidate.round_slots = _round_slots(polished)
            improved += 1

    if improved:
        notes.append(
            f"polish phase refined {improved} of {len(chosen)} shortlisted candidate(s)"
        )
    return total_iterations, stats


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
    # Ids of hard constraints this scenario broke — empty means viable.
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
    if any(m.slot is None for m in matches):
        plans = request.slot_plans or resolve_slot_plans(
            request, build_calendar(request.world, request.season)
        )
        _assign_missing_slots(matches, plans)

    # Cooling follows iteration progress rather than the clock, so the draws
    # taken from the seeded RNG depend only on the seed and the calibrated
    # rate, not on scheduling jitter. The target count is still budget-derived,
    # so a slower machine runs fewer iterations rather than taking longer.
    total_iterations = max(1, round(budget_s * _iterations_per_second(matches, request, calendars)))

    rng = random.Random(seed)
    moves = _MoveSet(matches, pinned, request, calendars)

    current_cost = _cost(matches, constraints, ctx).cost
    best_cost = current_cost
    best_snapshot = _capture_all(matches)

    decay = math.log(end_temperature / start_temperature)
    stats = SearchStats()
    # Throttled by iteration count: a clock poll every iteration would be the
    # one genuinely costly thing in this loop. ~40 updates per restart is
    # plenty for a human watching a terminal.
    report_every = max(1, total_iterations // 40)

    iterations = 0
    idle_draws = 0
    while iterations < total_iterations:
        anneal_progress = iterations / max(1, total_iterations - 1)
        temperature = start_temperature * math.exp(decay * anneal_progress)

        undo = moves.apply(rng)
        if undo is None:
            # A schedule with nothing movable would otherwise spin here.
            idle_draws += 1
            if idle_draws > _MAX_IDLE_DRAWS:
                break
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


# Iterations/second per problem size, measured once per (match count,
# constraint count). Measuring per call would make the search irreproducible:
# two runs with the same seed could time differently and so take a different
# number of iterations.
_RATE_CACHE: dict[tuple[int, int], float] = {}

_CALIBRATION_PROBES = 200
_MAX_IDLE_DRAWS = 200_000


def _iterations_per_second(
    matches: list[WorkingMatch],
    request: SolveRequest,
    calendars: dict[str, SeasonCalendar],
) -> float:
    key = (len(matches), len(request.constraints))
    cached = _RATE_CACHE.get(key)
    if cached is not None:
        return cached

    probe_moves = _MoveSet(matches, set(), request, calendars)
    probe_rng = random.Random()
    snapshot = _capture_all(matches)

    started = time.perf_counter()
    completed = 0
    idle_draws = 0
    while completed < _CALIBRATION_PROBES:
        undo = probe_moves.apply(probe_rng)
        if undo is None:
            idle_draws += 1
            if idle_draws > _MAX_IDLE_DRAWS:
                break
            continue
        _cost(matches, request.constraints, request.ctx)
        completed += 1
    elapsed = time.perf_counter() - started

    _restore(matches, snapshot)

    rate = completed / elapsed if completed and elapsed > 0 else float(max(1, completed))
    _RATE_CACHE[key] = rate
    return rate


def _capture_all(matches: list[WorkingMatch]) -> list[_Snapshot]:
    return [_snap(i, m) for i, m in enumerate(matches)]


def _snap(index: int, match: WorkingMatch) -> _Snapshot:
    return (index, match.home_team, match.away_team, match.date, match.venue, match.slot)


def _restore(matches: list[WorkingMatch], snapshot: list[_Snapshot]) -> None:
    for index, home, away, day, venue, slot in snapshot:
        match = matches[index]
        match.home_team = home
        match.away_team = away
        match.date = day
        match.venue = venue
        match.slot = slot


class _MoveSet:
    """The four move operators, plus the indexes they need to run cheaply.

    Every operator proposes only dates inside the match's round slot and
    drops a proposal that would put a team on a blackout, cup or European
    date, so the full scorer is spent on moves that can be viable (#7).
    """

    def __init__(
        self,
        matches: list[WorkingMatch],
        pinned: set[str],
        request: SolveRequest,
        calendars: dict[str, SeasonCalendar],
    ) -> None:
        self._matches = matches
        self._pinned = pinned
        self._world = request.world
        self._calendars = calendars
        self._competitions = {c.id: c for c in request.competitions}
        self._cup_windows = resolved_cup_windows(request.cup_schedules)
        self._european = request.european_commitments
        self._club_of = {team_id: team.club_id for team_id, team in request.world.teams.items()}

        # Pinned matches are tracked by list position, not by key: a key
        # contains the home team, and the orientation moves rewrite that.
        self._pinned_indexes = {i for i, m in enumerate(matches) if m.key in pinned}
        self._movable = [i for i in range(len(matches)) if i not in self._pinned_indexes]

        self._indexes_by_round: dict[tuple[str, int], list[int]] = {}
        for i, match in enumerate(matches):
            self._indexes_by_round.setdefault(
                (match.competition_id, match.round_index), []
            ).append(i)

        self._rounds_by_leg: dict[tuple[str, int], list[int]] = {}
        for (competition_id, round_index), indexes in self._indexes_by_round.items():
            leg = matches[indexes[0]].leg
            self._rounds_by_leg.setdefault((competition_id, leg), []).append(round_index)

        self._indexes_by_competition: dict[str, list[int]] = {}
        for i, match in enumerate(matches):
            self._indexes_by_competition.setdefault(match.competition_id, []).append(i)

        # Mirror map: leg-1 A-v-B <-> leg-2 B-v-A. Precomputed because a linear
        # scan of 240 matches would dominate the orientation move's cost.
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
            return self._shift_day(rng)
        if roll < 0.75:
            return self._swap_rounds(rng)
        if roll < 0.90:
            return self._flip_orientation(rng)
        return self._swap_teams(rng)

    # -- date admissibility ---------------------------------------------------

    def _date_ok(self, home: str, away: str, venue: str, competition_id: str, day: date) -> bool:
        """Whether `day` clears the blackouts and the binding cup and European
        dates of both teams."""
        if not self._calendars[competition_id].is_allowed(day, venue):
            return False
        return not any(
            cup_conflict(self._cup_windows, team, day)
            or european_conflict(self._european, team, day)
            for team in (home, away)
        )

    def _neighbours_ok(self, index: int, day: date) -> bool:
        """Whether `day` leaves both teams their rest and double-books neither
        the venue nor the club's home day, given every other match as placed."""
        match = self._matches[index]
        home, away, venue = match.home_team, match.away_team, match.venue
        club = self._club_of[home]
        own_gap = self._gap_of(match)
        for i, other in enumerate(self._matches):
            if i == index:
                continue
            delta = abs((other.date - day).days)
            if delta >= own_gap and delta >= self._gap_of(other):
                continue
            if delta == 0 and (other.venue == venue or self._club_of[other.home_team] == club):
                return False
            if (home in (other.home_team, other.away_team)) or (
                away in (other.home_team, other.away_team)
            ):
                return False
        return True

    def _gap_of(self, match: WorkingMatch) -> int:
        return self._competitions[match.competition_id].min_rest_days + 1

    # -- fine: move one match to another day of its slot --------------------

    def _shift_day(self, rng: random.Random) -> list[_Snapshot] | None:
        if not self._movable:
            return None
        index = rng.choice(self._movable)
        match = self._matches[index]
        if match.slot is None or match.competition_id not in self._competitions:
            return None

        options = [
            day
            for day in match.slot.days
            if day != match.date
            and self._date_ok(
                match.home_team, match.away_team, match.venue, match.competition_id, day
            )
            and self._neighbours_ok(index, day)
        ]
        if not options:
            return None

        snapshot = [_snap(index, match)]
        match.date = rng.choice(options)
        return snapshot

    # -- medium: exchange two rounds' slots within a leg ---------------------

    def _swap_rounds(self, rng: random.Random) -> list[_Snapshot] | None:
        keys = [k for k, rounds in self._rounds_by_leg.items() if len(rounds) >= 2]
        if not keys:
            return None
        competition_id, leg = rng.choice(keys)
        rounds = self._rounds_by_leg[(competition_id, leg)]
        round_a, round_b = rng.sample(rounds, 2)

        indexes_a = self._indexes_by_round[(competition_id, round_a)]
        indexes_b = self._indexes_by_round[(competition_id, round_b)]
        slot_a = self._matches[indexes_a[0]].slot
        slot_b = self._matches[indexes_b[0]].slot
        if slot_a is None or slot_b is None or slot_a == slot_b:
            return None

        snapshot: list[_Snapshot] = []
        for indexes, source, target in ((indexes_a, slot_a, slot_b), (indexes_b, slot_b, slot_a)):
            for index in indexes:
                match = self._matches[index]
                if index in self._pinned_indexes:
                    # A pinned match anchors its round; moving the round would
                    # break the requirement it satisfies.
                    return self._abort(snapshot)
                snapshot.append(_snap(index, match))
                match.date = _carry_over(match.date, source, target)
                match.slot = target
                if not self._date_ok(
                    match.home_team, match.away_team, match.venue, competition_id, match.date
                ):
                    return self._abort(snapshot)

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
            _snap(index, match),
            _snap(mirror_index, mirror),
        ]
        for target in (match, mirror):
            target.home_team, target.away_team = target.away_team, target.home_team
            target.venue = self._world.team(target.home_team).home_venue
            if not self._calendars[target.competition_id].is_allowed(target.date, target.venue):
                return self._abort(snapshot)
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
            snapshot.append(_snap(index, match))
            match.home_team = _swap(match.home_team, team_a, team_b)
            match.away_team = _swap(match.away_team, team_a, team_b)
            match.venue = self._world.team(match.home_team).home_venue
            if not self._date_ok(
                match.home_team, match.away_team, match.venue, competition_id, match.date
            ):
                return self._abort(snapshot)

        return snapshot or None


def _carry_over(day: date, source: RoundSlot, target: RoundSlot) -> date:
    """`day`'s counterpart in `target`: the slot day nearest the same offset
    from the slot's default day."""
    wanted = target.default_day + (day - source.default_day)
    return min(target.days, key=lambda d: (abs((d - wanted).days), d))


def _swap(team: str, a: str, b: str) -> str:
    if team == a:
        return b
    if team == b:
        return a
    return team
