"""CP-SAT backend — a two-level model: rounds to slots, then match days (#12).

Enabled with `--solver cpsat`. Requires OR-Tools, which is an optional
dependency; the import is deferred so the default install stays light.

Level 1 places each league's rounds in the typed slots the planner offers (#6)
and orients every pairing home or away. Rounds keep their order, so the leg
boundary and the final round fall out of the slot order, and a team's
home/away sequence is the round sequence — which is what makes break
minimisation and back-to-back home weekends for dual clubs expressible without
dates. Level 2 then picks a match day per fixture inside its round's slot.

Level 1 carries every condition Level 2 could not repair (a slot a team cannot
play in at all, a home venue blacked out on every day of a slot, the hard
fixed and full-round requirements), so Level 2 is small and rarely fails; when
it does, the Level 1 choice is cut off and Level 1 re-solves.

Hard rules carry assumption literals, so an infeasible level names the rule
families and the frame entries that close the slots, rather than relaxing
anything. `min_rest_days` is only ever relaxed when the request says so.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import NamedTuple

from ..display import short_date
from ..frame import SeasonFrame
from ..model.calendar import SeasonCalendar, build_calendar, calendars_by_competition
from ..model.loader import World
from ..model.schema import WEEKDAYS, Fixture, Match
from ..model.slots import RoundSlot, max_compatible_slots, slots_compatible
from ..rounds.commitments import CommitmentSet
from ..rounds.kickoff import assign_kickoff_times
from ..scoring.base import evaluate
from ..scoring.soft import ContingentCommitmentConflict
from ..slot_planner import SlotPlan
from .base import (
    Candidate,
    SearchStats,
    SolveRequest,
    SolverResult,
    error,
    info,
    resolve_slot_plans,
    select_diverse,
)
from .greedy import PlannedCompetition, plan_competitions

_ONE_DAY = timedelta(days=1)

# Well above any real soft weight, so a relaxed `min_rest_days` is only traded
# away when the model has no legal alternative.
_MIN_REST_RELAX_PENALTY = 500.0

# Level 1 prices (points, scaled by `_scaled`): leaving the planner's slot for
# a round, and playing a round outside a weekend.
_SLOT_DRIFT_WEIGHT = 0.3
_EXTRA_SLOT_WEIGHT = 2.0

# Share of rounds a later pass may leave in an earlier pass's slot.
_SLOT_REUSE_SHARE = 0.8
_MAX_LEVEL_TWO_RETRIES = 6
# Share of a pass's remaining time one conflict shrink may spend.
_SHRINK_BUDGET_SHARE = 0.25
_LEVEL_ONE_BUDGET_SHARE = 0.3
_MAX_LISTED = 12


class _Sides(NamedTuple):
    home_team: str
    away_team: str


@dataclass
class _League:
    plan: PlannedCompetition
    slot_plan: SlotPlan
    calendar: SeasonCalendar
    slots: list[RoundSlot]  # the slots a round can use, in date order
    pairs: list[tuple[str, str]]  # (home, away) as generated; one per pairing
    team_fixture: dict[tuple[str, int], Fixture]
    round_fixtures: dict[int, list[Fixture]]
    closed: dict[RoundSlot, list[str]]  # slots no round can use, with why

    @property
    def id(self) -> str:
        return self.plan.competition.id

    @property
    def rounds(self) -> int:
        return self.plan.competition.rounds

    @property
    def teams(self) -> list[str]:
        return list(self.plan.competition.teams)

    @property
    def gap(self) -> int:
        return self.plan.competition.min_gap_days


_CONFLICT_RULES = {"cup": "cup_round_conflict", "uefa": "european_commitment_conflict"}


class _Problem:
    """Everything both levels read, resolved once per solve."""

    def __init__(self, request: SolveRequest, calendar: SeasonCalendar, slot_plans):
        self.request = request
        self.world: World = request.world
        self.season = request.season
        self.commitments = request.commitments
        self.frame = request.frame or SeasonFrame.from_resolved(
            request.world, request.cup_schedules, request.commitments
        )
        self.slot_plans = slot_plans
        by_competition = calendars_by_competition(calendar, request.competitions)
        planned = plan_competitions(
            request.world, request.season, request.competitions, calendar,
            slot_plans=slot_plans,
        )
        self.planned = planned
        self._clear: dict[tuple[str, date], bool] = {}
        self._days: dict[tuple[str, str, str, RoundSlot], tuple[date, ...]] = {}
        self.leagues = [
            self._league(plan, slot_plans[plan.competition.id], by_competition[plan.competition.id])
            for plan in planned
        ]
        self.by_id = {lg.id: lg for lg in self.leagues}

    # -- clearance ----------------------------------------------------------

    def clear(self, team_id: str, day: date) -> bool:
        key = (team_id, day)
        if key not in self._clear:
            self._clear[key] = not self.commitments.blocked(team_id, day)
        return self._clear[key]

    def blockers(self, team_id: str, day: date) -> list[tuple[str, str]]:
        """Why `team_id` cannot play on `day`: (rule family, frame entry)."""
        found: list[tuple[str, str]] = []
        for commitment in self.commitments.hard_for(team_id):
            if commitment.clashes(day):
                found.append(
                    (_CONFLICT_RULES[commitment.source],
                     f"{commitment.label} on {commitment.date} "
                     f"(needs {commitment.min_rest_days} rest days)")
                )
        return found

    def fixture_days(self, league: _League, home: str, away: str, slot: RoundSlot) -> tuple[date, ...]:
        """Days of `slot` on which `home` can host `away`."""
        key = (league.id, home, away, slot)
        if key not in self._days:
            venue = self.world.team(home).home_venue
            days = [d for d in slot.days if league.calendar.is_allowed(d, venue)]
            days = _commitment_clear(days, _Sides(home, away), self.commitments)
            self._days[key] = tuple(days)
        return self._days[key]

    def common_days(self, league: _League, slot: RoundSlot) -> tuple[date, ...]:
        """Days of `slot` on which every team of the league is clear."""
        return tuple(d for d in slot.days if all(self.clear(t, d) for t in league.teams))

    # -- leagues ------------------------------------------------------------

    def _league(self, plan: PlannedCompetition, slot_plan: SlotPlan, calendar: SeasonCalendar) -> _League:
        competition = plan.competition
        closed: dict[RoundSlot, list[str]] = {}
        slots: list[RoundSlot] = []
        for slot in slot_plan.candidates:
            reasons = self._closure(competition.teams, slot)
            if reasons:
                closed[slot] = reasons
            else:
                slots.append(slot)

        pairs: list[tuple[str, str]] = []
        seen: set[frozenset[str]] = set()
        team_fixture: dict[tuple[str, int], Fixture] = {}
        round_fixtures: dict[int, list[Fixture]] = defaultdict(list)
        for fixture in plan.fixtures:
            round_fixtures[fixture.round_index].append(fixture)
            team_fixture[(fixture.home_team, fixture.round_index)] = fixture
            team_fixture[(fixture.away_team, fixture.round_index)] = fixture
            pair = frozenset((fixture.home_team, fixture.away_team))
            if pair not in seen:
                seen.add(pair)
                pairs.append((fixture.home_team, fixture.away_team))
        return _League(plan, slot_plan, calendar, slots, pairs, team_fixture, dict(round_fixtures), closed)

    def _closure(self, team_ids, slot: RoundSlot) -> list[str]:
        """Teams with no clear day in `slot`, each with the entries blocking them."""
        reasons = []
        for team_id in team_ids:
            if any(self.clear(team_id, d) for d in slot.days):
                continue
            entries = sorted(
                {f"{family}: {entry}" for d in slot.days for family, entry in self.blockers(team_id, d)}
            )
            reasons.append(f"{self.world.team_label(team_id)} blocked by " + "; ".join(entries))
        return reasons

    def oriented(self, league: _League, pair: tuple[str, str], flipped: bool) -> tuple[str, str]:
        return (pair[1], pair[0]) if flipped else pair


@dataclass
class _LevelOne:
    model: object
    assumptions: dict[str, object]
    x: dict[tuple[str, int, int], object]
    orient: dict[tuple[str, frozenset], object]
    counts: tuple[int, int] = (0, 0)


@dataclass
class _LevelOneSolution:
    slot_of: dict[tuple[str, int], int]
    flipped: dict[tuple[str, frozenset], bool]


@dataclass
class _Placed:
    """A fixture with its orientation and slot fixed by Level 1."""

    index: int
    fixture: Fixture
    league: _League
    venue: str
    slot: RoundSlot
    days: list[date]

    @property
    def key(self) -> str:
        f = self.fixture
        return f"{f.competition_id}:{f.round_index}:{f.home_team}-{f.away_team}"


class CpSatScheduler:
    name = "cpsat"

    def solve(self, request: SolveRequest) -> SolverResult:
        try:
            from ortools.sat.python import cp_model
        except ImportError as exc:  # pragma: no cover - exercised by absence
            raise RuntimeError(
                "the cpsat solver needs OR-Tools — install it with "
                "`poetry install --with football-scheduler-cpsat` or `pip install ortools`"
            ) from exc

        started = time.perf_counter()
        calendar = build_calendar(request.world, request.season)
        slot_plans = resolve_slot_plans(request, calendar)
        problem = _Problem(request, calendar, slot_plans)

        notes: list[str] = []
        stats = SearchStats()
        candidates: list[Candidate] = []
        plans_by_candidate: dict[int, dict[str, SlotPlan]] = {}

        shortage = _slot_shortage(problem)
        if shortage:
            notes.extend(shortage)
            stats.hard_violation_counts.update(["round_slot_capacity"])
            return SolverResult(
                candidates=[], solver=self.name, elapsed_s=time.perf_counter() - started,
                notes=notes, search_stats=stats, slot_plans=slot_plans,
            )

        previous: list[_LevelOneSolution] = []
        previous_dates: list[dict[str, date]] = []
        season_deadline = started + request.time_budget_s

        learned: list[tuple[_LevelOneSolution, list | None]] = []
        for pass_index in range(request.top_n):
            # Time an earlier pass left over goes to the later ones.
            now = time.perf_counter()
            deadline = now + (season_deadline - now) / (request.top_n - pass_index)
            known_conflicts = len(learned)
            outcome = _run_pass(
                cp_model, problem, previous, previous_dates, pass_index, deadline, stats, notes,
                learned,
            )
            if outcome is None:
                # With nothing found yet, a later pass resumes from the conflicts this
                # one learned instead of giving up on the rest of the budget.
                if candidates or len(learned) == known_conflicts:
                    break
                continue
            level_one, matches, plans = outcome
            matches.sort(key=lambda m: (m.date, m.competition_id, m.home_team))
            matches = assign_kickoff_times(
                matches, request.competitions, request.season.fixed_requirements
            )
            score = evaluate(matches, request.constraints, _with_detail(request.ctx))
            stats.investigated += 1
            if score.feasible:
                stats.feasible += 1
            else:
                stats.hard_violation_scenarios += 1
                stats.hard_violation_counts.update(
                    r.constraint_id for r in score.hard_results() if r.count
                )
            candidate = Candidate(
                matches=matches,
                score=score,
                label=f"Candidate (CP-SAT pass {pass_index + 1})",
                seed=request.seed + pass_index,
                round_slots={
                    (c, r): plans[c].chosen[r] for c in plans for r in range(plans[c].rounds)
                },
            )
            candidates.append(candidate)
            plans_by_candidate[id(candidate)] = plans
            previous.append(level_one)
            previous_dates.append({m.key: m.date for m in matches})

        chosen = select_diverse(candidates, request.top_n)
        for position, candidate in enumerate(chosen, start=1):
            candidate.label = f"Option {position}"
        if chosen and not any(c.score.feasible for c in chosen):
            notes.append(
                error("No fully feasible schedule found — see the hard-rule rows in the breakdown.")
            )

        best_plans = plans_by_candidate.get(id(chosen[0])) if chosen else None
        return SolverResult(
            candidates=chosen,
            solver=self.name,
            elapsed_s=time.perf_counter() - started,
            notes=notes,
            search_stats=stats,
            slot_plans=best_plans or slot_plans,
        )


# -- one pass ----------------------------------------------------------------


def _run_pass(
    cp_model, problem, previous, previous_dates, pass_index, deadline, stats, notes, learned
):
    """Level 1, then Level 2, re-solving Level 1 when Level 2 is proven infeasible.

    Returns `(level_one_solution, matches, slot_plans)`, or `None` when the
    pass found nothing (the reason is appended to `notes`). Retry diagnostics
    reach `stats` only when the pass ends without a schedule. Level 1 choices
    Level 2 rejected go into `learned`, shared by every pass, since a conflict
    stays a conflict whatever the diversity cuts.
    """
    request = problem.request
    cuts: list[_LevelOneSolution] = list(previous)
    tried = learned
    failed_culprits: list[list[str]] = []

    def give_up_on_level_two() -> None:
        culprits = sorted({c for group in failed_culprits for c in group})
        stats.investigated += 1
        stats.hard_violation_scenarios += 1
        stats.hard_violation_counts.update(culprits or ["unknown"])
        notes.append(
            f"pass {pass_index}: no slot choice admitted a match-day assignment — level 2 "
            "conflicts involve " + ", ".join(culprits)
        )

    for attempt in range(_MAX_LEVEL_TWO_RETRIES):
        remaining = deadline - time.perf_counter()
        if remaining <= 1.0:
            break
        level_one = _build_level_one(cp_model, problem, cuts, tried)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = max(1.0, remaining * _LEVEL_ONE_BUDGET_SHARE)
        solver.parameters.num_search_workers = 8
        solver.parameters.random_seed = request.seed + pass_index + attempt
        status = solver.Solve(level_one.model)

        if attempt == 0 and pass_index == 0:
            notes.append(
                info(
                    f"cpsat level 1 (rounds -> slots, home/away): {level_one.counts[0]} variables, "
                    f"{level_one.counts[1]} constraints"
                )
            )
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            if status != cp_model.INFEASIBLE:
                notes.extend(_level_one_report(problem, solver, status, [], cp_model, pass_index))
            elif failed_culprits:
                give_up_on_level_two()
            elif cuts:
                # Level 1 was feasible before the diversity cuts, so only they close it.
                notes.append(f"pass {pass_index}: no further distinct slot assignment exists")
            else:
                culprits = _culprits(solver, status, level_one.assumptions, cp_model)
                stats.investigated += 1
                stats.hard_violation_scenarios += 1
                stats.hard_violation_counts.update(culprits or ["unknown"])
                notes.extend(
                    _level_one_report(problem, solver, status, culprits, cp_model, pass_index)
                )
            return None

        solution = _read_level_one(solver, level_one, problem)
        remaining = deadline - time.perf_counter()
        placed, domain_error = _place(problem, solution)
        if domain_error:
            tried.append((solution, None))
            failed_culprits.append(["no_day_for_fixture"])
            continue

        built = _build_level_two(cp_model, problem, placed, previous_dates, relax=False)
        two = cp_model.CpSolver()
        two.parameters.max_time_in_seconds = max(1.0, remaining)
        two.parameters.num_search_workers = 8
        two.parameters.random_seed = request.seed + pass_index + attempt
        status2 = two.Solve(built.model)
        if attempt == 0 and pass_index == 0:
            notes.append(
                info(
                    f"cpsat level 2 (match days): {built.counts[0]} variables, "
                    f"{built.counts[1]} constraints"
                )
            )
        if status2 == cp_model.INFEASIBLE and request.relax_min_rest:
            relaxed = _build_level_two(cp_model, problem, placed, previous_dates, relax=True)
            two = cp_model.CpSolver()
            two.parameters.max_time_in_seconds = max(1.0, deadline - time.perf_counter())
            two.parameters.num_search_workers = 8
            two.parameters.random_seed = request.seed + pass_index + attempt
            relaxed_status = two.Solve(relaxed.model)
            # Failure analysis below must describe the model that was last tried.
            status2, built = relaxed_status, relaxed
            if relaxed_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                notes.append(
                    f"pass {pass_index}: min_rest_days RELAXED on request — some matches "
                    "fall on short rest; see the min_rest_days row in the score breakdown."
                )

        if status2 in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            if failed_culprits:
                notes.append(
                    info(
                        f"pass {pass_index}: {len(failed_culprits)} level 1 choice(s) rejected by "
                        "level 2 before one fit"
                    )
                )
            matches = _extract(two, built.placed, built.days)
            return solution, matches, _slot_plans(problem, solution)

        if status2 != cp_model.INFEASIBLE:
            notes.append(
                f"pass {pass_index}: level 2 timed out ({two.StatusName(status2)}) — "
                "the match-day assignment was neither found nor proven impossible"
            )
            return None

        failed_culprits.append(
            _culprits(two, status2, built.assumptions, cp_model) or [two.StatusName(status2)]
        )
        shrink_deadline = time.perf_counter() + _SHRINK_BUDGET_SHARE * (
            deadline - time.perf_counter()
        )
        tried.append(
            (
                solution,
                _conflicting_rounds(
                    cp_model, problem, placed, shrink_deadline, relax=request.relax_min_rest
                ),
            )
        )

    if failed_culprits:
        give_up_on_level_two()
    else:
        notes.append(f"pass {pass_index}: time budget ran out before a schedule was found")
    return None


# -- level 1 ------------------------------------------------------------------


def _build_level_one(cp_model, problem: _Problem, cuts, tried) -> _LevelOne:
    model = cp_model.CpModel()
    season = problem.season
    assumptions: dict[str, object] = {}

    def assume(name: str):
        if name not in assumptions:
            assumptions[name] = model.NewBoolVar(f"assume_{name}")
        return assumptions[name]

    x: dict[tuple[str, int, int], object] = {}
    pos: dict[tuple[str, int], object] = {}
    orient: dict[tuple[str, frozenset], object] = {}
    domain: dict[tuple[str, int], range] = {}

    for lg in problem.leagues:
        count, rounds = len(lg.slots), lg.rounds
        for r in range(rounds):
            # Rounds keep their order, so round r sits at least r slots in and
            # leaves one slot for each later round.
            domain[(lg.id, r)] = range(r, count - (rounds - 1 - r))
            for s in domain[(lg.id, r)]:
                x[(lg.id, r, s)] = model.NewBoolVar(f"x_{lg.id}_{r}_{s}")
            model.AddExactlyOne([x[(lg.id, r, s)] for s in domain[(lg.id, r)]])
            pos[(lg.id, r)] = model.NewIntVar(
                domain[(lg.id, r)].start, domain[(lg.id, r)].stop - 1, f"pos_{lg.id}_{r}"
            )
            model.Add(
                pos[(lg.id, r)] == sum(s * x[(lg.id, r, s)] for s in domain[(lg.id, r)])
            )
        for r in range(rounds - 1):
            model.Add(pos[(lg.id, r + 1)] >= pos[(lg.id, r)] + 1)
        for s in range(count):
            at_slot = [x[(lg.id, r, s)] for r in range(rounds) if (lg.id, r, s) in x]
            if len(at_slot) > 1:
                model.AddAtMostOne(at_slot)
        for pair in lg.pairs:
            orient[(lg.id, frozenset(pair))] = model.NewBoolVar(f"keep_{lg.id}_{pair[0]}_{pair[1]}")

    def home(lg: _League, team: str, r: int):
        """The literal for `team` hosting in round `r`: the pairing's
        orientation variable, or its negation for the other side."""
        fixture = lg.team_fixture[(team, r)]
        keep = orient[(lg.id, frozenset((fixture.home_team, fixture.away_team)))]
        return keep if fixture.home_team == team else keep.Not()

    # -- spacing: consecutive rounds must be compatible slots -----------------
    spacing = assume("round_slot_spacing")
    for lg in problem.leagues:
        for r in range(lg.rounds - 1):
            for s in domain[(lg.id, r)]:
                for t in domain[(lg.id, r + 1)]:
                    if t > s and not slots_compatible(lg.slots[s], lg.slots[t], lg.gap):
                        model.AddBoolOr(
                            [x[(lg.id, r, s)].Not(), x[(lg.id, r + 1, t)].Not()]
                        ).OnlyEnforceIf(spacing)

    # -- orientation / slot pruning from what Level 2 could not repair ---------
    final_lit = assume("final_round_same_slot")
    hard_full_rounds = [q for q in season.full_round_requirements if q.hard]
    for lg in problem.leagues:
        pin_ids = {q.date: q.id for q in hard_full_rounds if q.competition == lg.id}
        pinned = set(pin_ids)
        fixed = _fixed_round_dates(lg)
        for r in range(lg.rounds):
            for s in domain[(lg.id, r)]:
                slot = lg.slots[s]
                var = x[(lg.id, r, s)]
                if r in fixed:
                    day_pin = fixed[r] if fixed[r] in slot.days else None
                else:
                    day_pin = next((d for d in pinned if slot.contains(d) and d in slot.days), None)
                if r == lg.rounds - 1 and not _final_days(problem, lg, slot, day_pin):
                    model.AddBoolOr([var.Not()]).OnlyEnforceIf(final_lit)
                    continue
                for fixture in lg.round_fixtures[r]:
                    keep = orient[(lg.id, frozenset((fixture.home_team, fixture.away_team)))]
                    for kept in (True, False):
                        h, a = (fixture.home_team, fixture.away_team) if kept else (
                            fixture.away_team, fixture.home_team)
                        days = problem.fixture_days(lg, h, a, slot)
                        if day_pin is not None:
                            days = tuple(d for d in days if d == day_pin)
                        if days:
                            continue
                        model.AddBoolOr(
                            [var.Not(), (keep.Not() if kept else keep).Not()]
                        ).OnlyEnforceIf(
                            _blocker_literals(
                                problem, assume, lg, h, a, slot, pin_ids.get(day_pin)
                            )
                        )

    # -- hard full-round requirements -----------------------------------------
    for requirement in hard_full_rounds:
        lg = problem.by_id.get(requirement.competition)
        if lg is None:
            continue
        literal = assume(f"full_round_requirement:{requirement.id}")
        holding = [
            x[(lg.id, r, s)]
            for s, slot in enumerate(lg.slots)
            if slot.contains(requirement.date) and requirement.date in slot.days
            for r in range(lg.rounds)
            if (lg.id, r, s) in x
        ]
        model.AddBoolOr(holding or [literal.Not()]).OnlyEnforceIf(literal)

    # -- TV-fixed round dates: the round sits in the slot holding its date (#15) --
    for lg in problem.leagues:
        for r, day in _fixed_round_dates(lg).items():
            literal = assume(f"fixed_round_date:{lg.id}:{r + 1}")
            holding = [
                x[(lg.id, r, s)]
                for s, slot in enumerate(lg.slots)
                if day in slot.days and (lg.id, r, s) in x
            ]
            model.AddBoolOr(holding or [literal.Not()]).OnlyEnforceIf(literal)

    # -- hard fixed requirements: the team hosts in the round at its slot ------
    home_at_cache: dict[tuple[str, str, int], object] = {}
    exact_done: set[tuple[str, str, int]] = set()

    def home_at(lg: _League, team: str, s: int, *, exact: bool = False):
        """A Boolean that, when true, says `team` hosts in whichever round sits
        in slot `s`. With `exact` it is also true whenever that holds."""
        key = (lg.id, team, s)
        if key not in home_at_cache:
            var = model.NewBoolVar(f"home_{team}_{s}")
            rounds_here = [r for r in range(lg.rounds) if (lg.id, r, s) in x]
            model.Add(var <= sum(x[(lg.id, r, s)] for r in rounds_here))
            for r in rounds_here:
                model.AddBoolOr([x[(lg.id, r, s)].Not(), var.Not(), home(lg, team, r)])
            home_at_cache[key] = var
        var = home_at_cache[key]
        if exact and key not in exact_done:
            exact_done.add(key)
            for r in range(lg.rounds):
                if (lg.id, r, s) in x:
                    model.AddBoolOr([x[(lg.id, r, s)].Not(), home(lg, team, r).Not(), var])
        return var

    for requirement in season.fixed_requirements:
        if not requirement.hard:
            continue
        lg = problem.by_id.get(requirement.competition)
        if lg is None or requirement.home_team not in lg.teams:
            continue
        literal = assume(f"fixed_requirement:{requirement.id}")
        hosting = []
        for s, slot in enumerate(lg.slots):
            if not (slot.contains(requirement.date) and requirement.date in slot.days):
                continue
            var = home_at(lg, requirement.home_team, s)
            hosting.append(var)
            for r in range(lg.rounds):
                if (lg.id, r, s) not in x:
                    continue
                fixture = lg.team_fixture[(requirement.home_team, r)]
                opponent = (
                    fixture.away_team if fixture.home_team == requirement.home_team
                    else fixture.home_team
                )
                if requirement.date not in problem.fixture_days(
                    lg, requirement.home_team, opponent, slot
                ):
                    model.AddBoolOr([x[(lg.id, r, s)].Not(), var.Not()]).OnlyEnforceIf(literal)
        model.AddBoolOr(hosting or [literal.Not()]).OnlyEnforceIf(literal)

    # -- a club's two teams cannot both host when only one day is available ----
    club_lit = assume("club_home_clash")
    for club in problem.world.dual_clubs():
        senior = [t.id for t in club.teams if t.level == "senior"]
        for i, team_a in enumerate(senior):
            for team_b in senior[i + 1:]:
                lg_a, lg_b = _league_of(problem, team_a), _league_of(problem, team_b)
                if lg_a is None or lg_b is None:
                    continue
                for sa, slot_a in enumerate(lg_a.slots):
                    for sb, slot_b in enumerate(lg_b.slots):
                        if len(set(slot_a.days) | set(slot_b.days)) != 1:
                            continue
                        model.AddBoolOr([
                            home_at(lg_a, team_a, sa, exact=True).Not(),
                            home_at(lg_b, team_b, sb, exact=True).Not(),
                        ]).OnlyEnforceIf(club_lit)

    # -- objective ---------------------------------------------------------------
    terms: list[tuple[int, object]] = []

    for lg in problem.leagues:
        competition = lg.plan.competition
        chosen_index = {slot: i for i, slot in enumerate(lg.slots)}
        for r in range(lg.rounds):
            planned_slot = chosen_index.get(lg.slot_plan.chosen[r])
            if planned_slot is not None:
                for s in domain[(lg.id, r)]:
                    model.AddHint(x[(lg.id, r, s)], 1 if s == planned_slot else 0)
            for s in domain[(lg.id, r)]:
                if lg.slots[s].kind != "weekend":
                    terms.append((-_scaled(_EXTRA_SLOT_WEIGHT), x[(lg.id, r, s)]))
            if planned_slot is not None:
                drift = model.NewIntVar(0, len(lg.slots), f"drift_{lg.id}_{r}")
                model.Add(drift >= pos[(lg.id, r)] - planned_slot)
                model.Add(drift >= planned_slot - pos[(lg.id, r)])
                terms.append((-_scaled(_SLOT_DRIFT_WEIGHT), drift))

        # Runs of three or more home or away matches.
        break_weight = competition.weights.get("home_away_breaks", 8.0)
        for team in lg.teams:
            for r in range(lg.rounds - 2):
                window = [home(lg, team, r + k) for k in range(3)]
                broken = model.NewBoolVar(f"break_{team}_{r}")
                model.Add(sum(window) <= 2 + broken)
                model.Add(sum(window) >= 1 - broken)
                terms.append((-_scaled(break_weight), broken))

        # Home/away drift within each half of the season.
        balance_weight = competition.weights.get("home_away_balance", 6.0)
        midpoint = lg.rounds // 2
        for team in lg.teams:
            for first, last in ((0, midpoint), (midpoint, lg.rounds)):
                size = last - first
                if size == 0:
                    continue
                hosted = sum(home(lg, team, r) for r in range(first, last))
                excess = model.NewIntVar(0, size, f"drift_{team}_{first}")
                model.Add(excess >= 2 * hosted - size - 2)
                model.Add(excess >= size - 2 * hosted - 2)
                terms.append((-_scaled(balance_weight), excess))

    # Back-to-back home weekends for a club's two teams; dual-club coupling
    # lives here, not in `align_dual_clubs`, which this path does not use.
    for club in problem.world.dual_clubs():
        senior = [t.id for t in club.teams if t.level == "senior"]
        for i, team_a in enumerate(senior):
            for team_b in senior[i + 1:]:
                lg_a, lg_b = _league_of(problem, team_a), _league_of(problem, team_b)
                if lg_a is None or lg_b is None:
                    continue
                weight = _dual_weight(problem.planned, [team_a, team_b], "consecutive_home_days", 25.0)
                for sa, slot_a in enumerate(lg_a.slots):
                    for sb, slot_b in enumerate(lg_b.slots):
                        if not _can_be_adjacent(slot_a, slot_b):
                            continue
                        both = model.NewBoolVar(f"weekend_{team_a}_{team_b}_{sa}_{sb}")
                        model.AddImplication(both, home_at(lg_a, team_a, sa))
                        model.AddImplication(both, home_at(lg_b, team_b, sb))
                        terms.append((_scaled(weight), both))

    # -- diversity ---------------------------------------------------------------
    total_rounds = sum(lg.rounds for lg in problem.leagues)
    for earlier in cuts:
        same = [
            x[(c, r, s)] for (c, r), s in earlier.slot_of.items() if (c, r, s) in x
        ]
        model.Add(sum(same) <= int(total_rounds * _SLOT_REUSE_SHARE))
    for failed, rounds in tried:
        if rounds is None:
            same = [x[(c, r, s)] for (c, r), s in failed.slot_of.items() if (c, r, s) in x]
        else:
            # Only the rounds Level 2 could not fit together, with their home/away
            # choices, so the next Level 1 solution can't repeat the same conflict.
            same = [
                x[(c, r, failed.slot_of[(c, r)])]
                for c, r in rounds
                if (c, r, failed.slot_of[(c, r)]) in x
            ]
            pairs = {
                (lg.id, frozenset((fx.home_team, fx.away_team)))
                for lg in problem.leagues
                for c, r in rounds
                if c == lg.id
                for fx in lg.round_fixtures[r]
            }
            same += [orient[key].Not() if failed.flipped[key] else orient[key] for key in pairs]
        model.Add(sum(same) <= len(same) - 1)

    model.Maximize(sum(coefficient * var for coefficient, var in terms))
    for literal in assumptions.values():
        model.AddAssumption(literal)

    proto = model.Proto()
    return _LevelOne(
        model, assumptions, x, orient, counts=(len(proto.variables), len(proto.constraints))
    )


def _final_days(problem: _Problem, lg: _League, slot: RoundSlot, pinned_day) -> tuple[date, ...]:
    days = problem.common_days(lg, slot)
    return tuple(d for d in days if pinned_day is None or d == pinned_day)


def _blocker_literals(problem, assume, lg, home, away, slot, pin_id):
    """The assumption literals that, together, empty `slot` for `home` v `away`."""
    families = {"blackout_dates"}
    for d in slot.days:
        for team in (home, away):
            families.update(family for family, _ in problem.blockers(team, d))
    names = sorted(families)
    if pin_id is not None:
        names.append(f"full_round_requirement:{pin_id}")
    return [assume(n) for n in names]


def _league_of(problem: _Problem, team_id: str) -> _League | None:
    return next((lg for lg in problem.leagues if team_id in lg.teams), None)


def _can_be_adjacent(first: RoundSlot, second: RoundSlot) -> bool:
    """Whether some day of each lands on consecutive calendar days."""
    return any(abs((a - b).days) == 1 for a in first.days for b in second.days)


def _conflicting_rounds(cp_model, problem: _Problem, placed, deadline, relax: bool = False):
    """A small set of `(league, round)` whose placed fixtures Level 2 still
    cannot fit, found by dropping rounds one at a time.

    Runs without the per-pass diversity cap and with the Level 2 model the pass
    used (`relax`), so infeasibility is monotone in the rounds kept and the set
    is a conflict in every pass. If the time runs out, the set so far is
    returned: it is infeasible, just not minimal.
    """
    keep = sorted({(p.fixture.competition_id, p.fixture.round_index) for p in placed})
    for key in list(keep):
        if deadline - time.perf_counter() <= 0:
            break
        trial = [k for k in keep if k != key]
        subset = [p for p in placed if (p.fixture.competition_id, p.fixture.round_index) in trial]
        if not subset:
            continue
        built = _build_level_two(cp_model, problem, subset, [], relax=relax)
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = min(0.5, max(0.05, deadline - time.perf_counter()))
        solver.parameters.num_search_workers = 8
        if solver.Solve(built.model) == cp_model.INFEASIBLE:
            keep = trial
    return keep


def _read_level_one(solver, level_one: _LevelOne, problem: _Problem) -> _LevelOneSolution:
    slot_of = {
        (c, r): s for (c, r, s), var in level_one.x.items() if solver.Value(var)
    }
    flipped = {key: not solver.Value(var) for key, var in level_one.orient.items()}
    return _LevelOneSolution(slot_of, flipped)


def _slot_plans(problem: _Problem, solution: _LevelOneSolution) -> dict[str, SlotPlan]:
    plans: dict[str, SlotPlan] = {}
    for lg in problem.leagues:
        chosen = [lg.slots[solution.slot_of[(lg.id, r)]] for r in range(lg.rounds)]
        plan = SlotPlan(lg.id, lg.rounds, lg.slot_plan.candidates, chosen)
        for round_index, slot in plan.clashing_rounds.items():
            teams = ", ".join(problem.world.team_label(t) for t in slot)
            plan.warnings.append(
                f"{problem.world.competition(lg.id).name}: round {round_index + 1} on "
                f"{short_date(chosen[round_index].default_day)} clashes with a hard cup or UEFA commitment for {teams}"
            )
        plans[lg.id] = plan
    return plans


# -- placing Level 1's result for Level 2 ---------------------------------------


def _place(problem: _Problem, solution: _LevelOneSolution):
    """Orient each fixture, attach its slot and the days Level 2 may choose.

    Returns `(placed, error)`; `error` names a fixture left with no day.
    """
    placed: list[_Placed] = []
    season = problem.season
    for lg in problem.leagues:
        pins = {q.date for q in season.full_round_requirements if q.hard and q.competition == lg.id}
        fixed = _fixed_round_dates(lg)
        required: dict[tuple[str, int], date] = {}
        for requirement in season.fixed_requirements:
            if requirement.hard and requirement.competition == lg.id:
                for r in range(lg.rounds):
                    slot = lg.slots[solution.slot_of[(lg.id, r)]]
                    if slot.contains(requirement.date) and requirement.date in slot.days:
                        fixture = lg.team_fixture[(requirement.home_team, r)]
                        if _oriented(fixture, solution, lg).home_team == requirement.home_team:
                            required[(fixture.competition_id, id(fixture))] = requirement.date
        for fixture in lg.plan.fixtures:
            oriented = _oriented(fixture, solution, lg)
            slot = lg.slots[solution.slot_of[(lg.id, fixture.round_index)]]
            days = list(problem.fixture_days(lg, oriented.home_team, oriented.away_team, slot))
            pinned = fixed.get(fixture.round_index) or next(
                (d for d in pins if slot.contains(d) and d in slot.days), None
            )
            forced = required.get((fixture.competition_id, id(fixture)), pinned)
            if forced is not None:
                days = [d for d in days if d == forced]
            if fixture.round_index == lg.rounds - 1:
                common = set(problem.common_days(lg, slot))
                days = [d for d in days if d in common]
            if not days:
                return [], (
                    f"no day for {oriented.home_team} v {oriented.away_team} in {lg.id} "
                    f"round {fixture.round_index + 1}"
                )
            placed.append(
                _Placed(
                    len(placed), oriented, lg, problem.world.team(oriented.home_team).home_venue,
                    slot, days,
                )
            )
    return placed, None


def _oriented(fixture: Fixture, solution: _LevelOneSolution, lg: _League) -> Fixture:
    if solution.flipped[(lg.id, frozenset((fixture.home_team, fixture.away_team)))]:
        return fixture.model_copy(
            update={"home_team": fixture.away_team, "away_team": fixture.home_team}
        )
    return fixture


# -- level 2 --------------------------------------------------------------------


@dataclass
class _LevelTwo:
    model: object
    placed: list[_Placed]
    days: dict[int, dict[date, object]]
    assumptions: dict[str, object]
    counts: tuple[int, int] = (0, 0)


def _build_level_two(cp_model, problem: _Problem, placed: list[_Placed], previous_dates, relax: bool):
    model = cp_model.CpModel()
    world = problem.world
    season = problem.season
    assumptions: dict[str, object] = {}

    def assume(name: str):
        if name not in assumptions:
            assumptions[name] = model.NewBoolVar(f"assume_{name}")
        return assumptions[name]

    days: dict[int, dict[date, object]] = {}
    for item in placed:
        days[item.index] = {d: model.NewBoolVar(f"d_{item.index}_{d}") for d in item.days}
        model.AddExactlyOne(days[item.index].values())

    # The final round of a league shares one date.
    for lg in problem.leagues:
        last = [p for p in placed if p.league is lg and p.fixture.round_index == lg.rounds - 1]
        if len(last) < 2:
            continue
        shared = {d: model.NewBoolVar(f"final_{lg.id}_{d}") for d in sorted(last[0].slot.days)}
        model.AddExactlyOne(shared.values())
        for item in last:
            for d, var in days[item.index].items():
                model.Add(var == shared[d]) if d in shared else model.Add(var == 0)
            for d, flag in shared.items():
                if d not in days[item.index]:
                    model.Add(flag == 0)

    team_on_date: dict[tuple[str, date], list[object]] = defaultdict(list)
    home_on_date: dict[tuple[str, date], list[object]] = defaultdict(list)
    venue_on_date: dict[tuple[str, date], list[object]] = defaultdict(list)
    owner: dict[int, int] = {}
    for item in placed:
        for d, var in days[item.index].items():
            team_on_date[(item.fixture.home_team, d)].append(var)
            team_on_date[(item.fixture.away_team, d)].append(var)
            home_on_date[(item.fixture.home_team, d)].append(var)
            venue_on_date[(item.venue, d)].append(var)
            owner[id(var)] = item.index

    venue_lit = assume("venue_double_booking")
    for vars_ in venue_on_date.values():
        if len({owner[id(v)] for v in vars_}) > 1:
            model.Add(sum(vars_) <= 1).OnlyEnforceIf(venue_lit)

    rest_lit = assume("min_rest_days")
    minimum_by_team: dict[str, int] = {}
    for plan in problem.planned:
        for team_id in plan.competition.teams:
            minimum_by_team[team_id] = max(
                minimum_by_team.get(team_id, 0), plan.competition.min_gap_days
            )
    dates_by_team: dict[str, list[date]] = defaultdict(list)
    for team_id, day in team_on_date:
        dates_by_team[team_id].append(day)

    relax_terms: list[tuple[int, object]] = []
    for team_id, team_days in dates_by_team.items():
        minimum = minimum_by_team.get(team_id, 1)
        for start in sorted(set(team_days)):
            window = [
                v for offset in range(minimum)
                for v in team_on_date.get((team_id, start + timedelta(days=offset)), [])
            ]
            if len({owner[id(v)] for v in window}) <= 1:
                continue
            if relax:
                ok = model.NewBoolVar(f"restok_{team_id}_{start}")
                model.Add(sum(window) <= 1).OnlyEnforceIf(ok)
                relax_terms.append((_scaled(_MIN_REST_RELAX_PENALTY), ok))
            else:
                model.Add(sum(window) <= 1).OnlyEnforceIf(rest_lit)

    club_lit = assume("club_home_clash")
    for club in world.dual_clubs():
        team_ids = [t.id for t in club.teams]
        club_days: set[date] = set()
        for team_id in team_ids:
            club_days |= {d for (t, d) in home_on_date if t == team_id}
        for d in club_days:
            vars_ = [v for t in team_ids for v in home_on_date.get((t, d), [])]
            if len({owner[id(v)] for v in vars_}) > 1:
                model.Add(sum(vars_) <= 1).OnlyEnforceIf(club_lit)

    # At most N matches of one round per weekday (#15).
    quota_vars: dict[tuple[str, int, str], list[object]] = defaultdict(list)
    for item in placed:
        competition = item.league.plan.competition
        if item.fixture.round_index in competition.day_quota_exempt_rounds:
            continue
        for d, var in days[item.index].items():
            weekday = WEEKDAYS[d.weekday()]
            if weekday in competition.day_quota:
                quota_vars[(competition.id, item.fixture.round_index, weekday)].append(var)
    for (competition_id, _, weekday), vars_ in quota_vars.items():
        model.Add(sum(vars_) <= problem.by_id[competition_id].plan.competition.day_quota[weekday])

    # -- objective --------------------------------------------------------------
    terms: list[tuple[int, object]] = []
    competitions = {p.competition.id: p.competition for p in problem.planned}
    discouraged = {d.date for d in season.discouraged_dates}
    for item in placed:
        competition = competitions[item.fixture.competition_id]
        wanted = WEEKDAYS.index(competition.preferred_weekday)
        weekday_weight = competition.weights.get("preferred_weekday", 4.0)
        venue_weight = competition.weights.get("soft_venue_preference", 5.0)
        for d, var in days[item.index].items():
            if d.weekday() == wanted:
                terms.append((_scaled(weekday_weight), var))
            if d in discouraged:
                terms.append((-_scaled(venue_weight), var))

    # Same price as the scorer's `ContingentCommitmentConflict`, so a day near a
    # commitment the team only maybe reaches is steered away from (#8).
    contingent_weight = ContingentCommitmentConflict.weight
    for item in placed:
        for team_id in (item.fixture.home_team, item.fixture.away_team):
            for commitment in problem.commitments.contingent_for(team_id):
                for d, var in days[item.index].items():
                    shortfall = commitment.min_rest_days - (abs((d - commitment.date).days) - 1)
                    if shortfall > 0:
                        penalty = contingent_weight * shortfall * commitment.reached_probability
                        terms.append((-_scaled(penalty), var))

    for club in world.dual_clubs():
        seniors = [t.id for t in club.teams if t.level == "senior"]
        weight = _dual_weight(problem.planned, seniors, "consecutive_home_days", 25.0)
        for i, team_a in enumerate(seniors):
            for team_b in seniors[i + 1:]:
                for (team_id, d), vars_a in list(home_on_date.items()):
                    if team_id != team_a:
                        continue
                    for other_day in (d + _ONE_DAY, d - _ONE_DAY):
                        vars_b = home_on_date.get((team_b, other_day))
                        if not vars_b:
                            continue
                        pair = model.NewBoolVar(f"chd_{team_a}_{team_b}_{d}_{other_day}")
                        model.Add(sum(vars_a) >= 1).OnlyEnforceIf(pair)
                        model.Add(sum(vars_b) >= 1).OnlyEnforceIf(pair)
                        terms.append((_scaled(weight), pair))

    # Rest comfort is graduated, not a threshold: a window of L days with at
    # most one match is the analogue of L-1 rest days, and summing a bonus over
    # every length from minimum+1 to comfortable gives the scorer's penalty up
    # to a constant.
    for team_id, team_days in dates_by_team.items():
        comfortable = _comfort_for(problem.planned, team_id)
        minimum = minimum_by_team.get(team_id, 1)
        if comfortable <= minimum:
            continue
        weight = _comfort_weight(problem.planned, team_id)
        for length in range(minimum + 1, comfortable + 1):
            for start in sorted(set(team_days)):
                window = [
                    v for offset in range(length)
                    for v in team_on_date.get((team_id, start + timedelta(days=offset)), [])
                ]
                if len({owner[id(v)] for v in window}) <= 1:
                    continue
                clear = model.NewBoolVar(f"comfort_{team_id}_{start}_{length}")
                model.Add(sum(window) <= 1).OnlyEnforceIf(clear)
                terms.append((_scaled(weight), clear))

    for earlier in previous_dates:
        same = [
            days[item.index][earlier[item.key]]
            for item in placed
            if item.key in earlier and earlier[item.key] in days[item.index]
        ]
        if same:
            model.Add(sum(same) <= int(len(same) * 0.8))

    model.Maximize(sum(coefficient * var for coefficient, var in terms + relax_terms))
    for literal in assumptions.values():
        model.AddAssumption(literal)

    proto = model.Proto()
    return _LevelTwo(
        model, placed, days, assumptions, counts=(len(proto.variables), len(proto.constraints))
    )


def _extract(solver, placed: list[_Placed], days) -> list[Match]:
    matches: list[Match] = []
    for item in placed:
        chosen = next((d for d, var in days[item.index].items() if solver.Value(var)), None)
        if chosen is None:
            continue
        f = item.fixture
        matches.append(
            Match(
                competition_id=f.competition_id,
                home_team=f.home_team,
                away_team=f.away_team,
                leg=f.leg,
                round_index=f.round_index,
                date=chosen,
                venue=item.venue,
            )
        )
    return matches


# -- infeasibility reports -------------------------------------------------------


def _slot_shortage(problem: _Problem) -> list[str]:
    """Notes when a league has fewer usable slots than rounds, naming what closes them."""
    notes: list[str] = []
    for lg in problem.leagues:
        usable = max_compatible_slots(lg.slots, lg.gap)
        if usable >= lg.rounds:
            continue
        notes.append(
            f"{lg.id}: {usable} usable round slots for {lg.rounds} rounds — "
            f"{len(lg.closed)} of {len(lg.slot_plan.candidates)} candidate slots are closed by the frame"
        )
        notes.extend(_closed_slot_lines(problem, lg))
    return notes


def _closed_slot_lines(problem: _Problem, lg: _League) -> list[str]:
    lines = []
    for slot, reasons in list(lg.closed.items())[:_MAX_LISTED]:
        lines.append(f"  {lg.id} slot {_span(slot)} closed: " + " | ".join(reasons[:3]))
    if len(lg.closed) > _MAX_LISTED:
        lines.append(f"  ...and {len(lg.closed) - _MAX_LISTED} more closed slots in {lg.id}")
    return lines


def _span(slot: RoundSlot) -> str:
    if slot.start == slot.end:
        return f"{slot.start:%a %d %b}"
    return f"{slot.start:%a %d %b}-{slot.end:%a %d %b}"


def _level_one_report(problem, solver, status, culprits, cp_model, pass_index) -> list[str]:
    if status != cp_model.INFEASIBLE:
        return [f"pass {pass_index}: level 1 returned {solver.StatusName(status)}"]
    lines = [
        f"pass {pass_index}: infeasible at level 1 (rounds -> slots) — the conflict involves "
        + (", ".join(sorted(culprits)) if culprits else "rules the solver could not isolate")
    ]
    for name in sorted(culprits):
        lines.extend(_describe_culprit(problem, name))
    closed = [lg for lg in problem.leagues if lg.closed]
    if closed:
        lines.append("  slots the frame closed to a league (no clear day for some team):")
        for lg in closed:
            lines.extend(_closed_slot_lines(problem, lg))
    return lines


def _describe_culprit(problem: _Problem, name: str) -> list[str]:
    family, _, ident = name.partition(":")
    if family == "fixed_requirement":
        req = next((r for r in problem.season.fixed_requirements if r.id == ident), None)
        if req:
            return [
                f"  {name}: {problem.world.team_label(req.home_team)} home on {req.date} "
                f"({req.reason or req.id})"
            ]
    if family == "full_round_requirement":
        req = next((r for r in problem.season.full_round_requirements if r.id == ident), None)
        if req:
            return [f"  {name}: every {req.competition} team plays on {req.date}"]
    if family == "fixed_round_date":
        competition_id, _, round_number = ident.rpartition(":")
        lg = problem.by_id.get(competition_id)
        if lg is not None:
            day = _fixed_round_dates(lg).get(int(round_number) - 1)
            return [f"  {name}: {competition_id} round {round_number} is fixed to {day}"]
    return []


def _fixed_round_dates(lg: _League) -> dict[int, date]:
    """Round index -> the date a TV pick fixes that round to (#15)."""
    return {f.round - 1: f.date for f in lg.plan.competition.fixed_round_dates}


def _culprits(solver, status, assumptions, cp_model) -> list[str]:
    """The rules the solver names as responsible for an INFEASIBLE result."""
    if status != cp_model.INFEASIBLE:
        return []
    try:
        core = set(solver.SufficientAssumptionsForInfeasibility())
        return [name for name, literal in assumptions.items() if literal.Index() in core]
    except Exception:  # pragma: no cover - depends on solver build
        return []


# -- shared helpers ---------------------------------------------------------------


def _with_detail(ctx):
    from ..scoring.base import EvalContext

    return EvalContext(
        world=ctx.world,
        season=ctx.season,
        travel=ctx.travel,
        detail=True,
        away_pairing_max_hours=ctx.away_pairing_max_hours,
    )


def _commitment_clear(window: list[date], fixture, commitments: CommitmentSet) -> list[date]:
    """Drop candidate dates within rest of either side's hard commitment;
    contingent ones stay in the domain and are scored instead."""
    return [
        day
        for day in window
        if not commitments.blocked(fixture.home_team, day)
        and not commitments.blocked(fixture.away_team, day)
    ]


def _scaled(weight: float) -> int:
    """CP-SAT wants integer coefficients; scale to keep one decimal place."""
    return int(round(weight * 10))


def _dual_weight(planned, team_ids: list[str], key: str, default: float) -> float:
    best = default
    for plan in planned:
        if any(t in plan.competition.teams for t in team_ids):
            best = max(best, plan.competition.weights.get(key, default))
    return best


def _comfort_for(planned, team_id: str) -> int:
    for plan in planned:
        if team_id in plan.competition.teams:
            return plan.competition.comfortable_gap_days
    return 0


def _comfort_weight(planned, team_id: str) -> float:
    for plan in planned:
        if team_id in plan.competition.teams:
            return plan.competition.weights.get("rest_comfort", 3.0)
    return 3.0


__all__ = ["CpSatScheduler"]
