"""CP-SAT backend — declarative date assignment via OR-Tools.

Enabled with `--solver cpsat`. Requires OR-Tools, which is an optional
dependency; the import is deferred so the default install stays light.

**What this model does and does not decide.** It assigns *dates*, given the
pairing structure produced by the same round generator the local-search backend
uses. Who plays whom, and which way round, is fixed before the model is built.
That keeps the model to a few thousand booleans and solves in seconds; letting
CP-SAT choose the pairings as well would make it a genuinely large problem and
is the obvious next step rather than something quietly assumed away here.

One consequence worth knowing: `home_away_breaks` is a property of the pairing
order, not the dates, so it is near-constant across this model's solutions and
is left out of the objective. It is still *reported*, by the same scorer both
backends share — the report tells the truth about a schedule regardless of
which solver produced it.

Hard constraints are registered with assumption literals, so an infeasible
model names the rules responsible instead of returning a bare INFEASIBLE.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from datetime import date, timedelta

from ..model.calendar import build_calendar, calendars_by_competition
from ..model.schema import Match
from ..rounds.cup_schedule import cup_conflict, resolved_cup_windows
from ..rounds.kickoff import assign_kickoff_times
from ..scoring.base import evaluate
from .base import Candidate, SearchStats, SolveRequest, SolverResult, select_diverse
from .greedy import align_dual_clubs, align_home_teams_to_round_pins, plan_competitions, resolve_round_pins

_ONE_DAY = timedelta(days=1)


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
        planned = plan_competitions(
            request.world, request.season, request.competitions, calendar
        )
        align_dual_clubs(request.world, planned)
        round_pins = resolve_round_pins(request.season, planned)
        align_home_teams_to_round_pins(request.season, planned, round_pins)

        candidates: list[Candidate] = []
        notes: list[str] = []
        # Each pass forbids re-using the previous solutions' date assignments
        # too closely, which is how a solution *pool* becomes three genuinely
        # different options rather than three trivial variations.
        forbidden: list[dict[str, date]] = []
        budget_per_pass = request.time_budget_s / max(1, request.top_n)

        # One CP-SAT pass is this backend's unit of "scenario investigated"
        # (issue #34) — the same unit local search uses is one proposed move,
        # which has no CP-SAT analogue; a pass is the coarsest thing both
        # backends' stats can be compared at. `hard_violation_counts` is
        # populated from whichever hard rule(s) either made the model
        # infeasible (the assumption culprits) or are still broken in the
        # extracted schedule our own scorer checked.
        stats = SearchStats()

        for pass_index in range(request.top_n):
            built = _build_model(
                cp_model, request, planned, calendar, forbidden, round_pins
            )
            if built is None:
                notes.append(f"pass {pass_index}: no candidate dates could be built")
                break

            model, placement, fixtures, assumptions = built
            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = budget_per_pass
            solver.parameters.num_search_workers = 8
            solver.parameters.random_seed = request.seed + pass_index

            status = solver.Solve(model)
            stats.investigated += 1
            if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                culprits = _infeasibility_culprits(solver, status, assumptions, cp_model)
                stats.hard_violation_scenarios += 1
                stats.hard_violation_counts.update(culprits or ["unknown"])
                notes.append(_infeasibility_note(solver, status, culprits, cp_model, pass_index))
                break

            matches = _extract(solver, placement, fixtures, request)
            matches.sort(key=lambda m: (m.date, m.competition_id, m.home_team))
            matches = assign_kickoff_times(
                matches, request.competitions, request.season.fixed_requirements
            )
            detail_ctx = _with_detail(request.ctx)
            score = evaluate(matches, request.constraints, detail_ctx)
            if score.feasible:
                stats.feasible += 1
            else:
                stats.hard_violation_scenarios += 1
                stats.hard_violation_counts.update(
                    r.constraint_id for r in score.hard_results() if r.count
                )
            candidates.append(
                Candidate(
                    matches=matches,
                    score=score,
                    label=f"Candidate (CP-SAT pass {pass_index + 1})",
                    seed=request.seed + pass_index,
                )
            )
            forbidden.append({m.key: m.date for m in matches})

        chosen = select_diverse(candidates, request.top_n)
        for position, candidate in enumerate(chosen, start=1):
            candidate.label = f"Option {position}"

        if chosen and not any(c.score.feasible for c in chosen):
            notes.append(
                "No fully feasible schedule found — see the hard-rule rows in the breakdown."
            )

        return SolverResult(
            candidates=chosen,
            solver=self.name,
            elapsed_s=time.perf_counter() - started,
            notes=notes,
            search_stats=stats,
        )


def _with_detail(ctx):
    from ..scoring.base import EvalContext

    return EvalContext(
        world=ctx.world,
        season=ctx.season,
        travel=ctx.travel,
        detail=True,
        away_pairing_max_hours=ctx.away_pairing_max_hours,
    )


def _build_model(cp_model, request, planned, calendar, forbidden, round_pins):
    """Assemble the CP-SAT model. Returns None if no fixture can be placed."""
    world = request.world
    season = request.season
    model = cp_model.CpModel()
    by_competition = calendars_by_competition(calendar, request.competitions)
    cup_windows = resolved_cup_windows(request.cup_schedules)

    # -- decision variables: one boolean per (fixture, candidate date) --------
    fixtures: list[tuple[object, object, str]] = []  # (fixture, competition, venue)
    placement: dict[int, dict[date, object]] = {}

    # A hard fixed requirement can name a date no round window reaches — May 16
    # 2026 is a Saturday, and the Sunday after it is a national-day blackout, so
    # no Eliteserien anchor comes within reach. The date is added to the
    # candidate set of exactly one fixture: the home fixture whose round already
    # sits closest to it. Adding it to every fixture of that team instead would
    # let an early date leak into a late round and drag the leg boundary with
    # it, which is how this model first came out infeasible.
    required_dates: dict[tuple[str, int, str, str], date] = {}
    for requirement in season.fixed_requirements:
        if not requirement.hard:
            continue
        plan = next(
            (p for p in planned if p.competition.id == requirement.competition), None
        )
        if plan is None:
            continue
        options = [f for f in plan.fixtures if f.home_team == requirement.home_team]
        if not options:
            continue
        target = min(
            options, key=lambda f: abs((plan.anchors[f.round_index] - requirement.date).days)
        )
        required_dates[_fixture_id(target)] = requirement.date

    for plan in planned:
        competition = plan.competition
        competition_calendar = by_competition[competition.id]
        for fixture in plan.fixtures:
            venue = world.team(fixture.home_team).home_venue
            anchor = plan.anchors[fixture.round_index]
            pin_date = round_pins.get((competition.id, fixture.round_index))
            if pin_date is not None:
                # A round pinned to one date (the final round, or a
                # FullRoundRequirement's round — see `resolve_round_pins`)
                # gets exactly one candidate date, which forces it without
                # needing a separate assumption literal: there is nothing
                # else for `AddExactlyOne` below to choose.
                window = [pin_date]
            else:
                window = _cup_clear(
                    competition_calendar.window(anchor, competition.match_window_days, venue),
                    fixture,
                    cup_windows,
                )
                if not window:
                    window = _cup_clear(
                        competition_calendar.window(
                            anchor, competition.match_window_days * 3, venue
                        ),
                        fixture,
                        cup_windows,
                    )
                extra = required_dates.get(_fixture_id(fixture))
                if extra is not None:
                    window = sorted(set(window) | {extra})
            if not window:
                return None
            index = len(fixtures)
            fixtures.append((fixture, competition, venue))
            placement[index] = {
                day: model.NewBoolVar(f"x_{index}_{day}") for day in window
            }
            model.AddExactlyOne(placement[index].values())

    assumptions: dict[str, object] = {}

    def assume(name: str):
        """A literal that, when relaxed, isolates one rule as the culprit."""
        if name not in assumptions:
            literal = model.NewBoolVar(f"assume_{name}")
            assumptions[name] = literal
        return assumptions[name]

    # -- fixed requirements: pin a chosen fixture to its date ----------------
    pinned: set[int] = set()
    for requirement in season.fixed_requirements:
        if not requirement.hard:
            continue
        best_index, best_gap = None, None
        for index, (fixture, competition, _) in enumerate(fixtures):
            if index in pinned:
                continue
            if (
                fixture.home_team != requirement.home_team
                or competition.id != requirement.competition
            ):
                continue
            if requirement.date not in placement[index]:
                continue
            plan = next(p for p in planned if p.competition.id == competition.id)
            gap = abs((plan.anchors[fixture.round_index] - requirement.date).days)
            if best_gap is None or gap < best_gap:
                best_index, best_gap = index, gap
        if best_index is not None:
            literal = assume(f"fixed_requirement:{requirement.id}")
            model.Add(placement[best_index][requirement.date] == 1).OnlyEnforceIf(literal)
            pinned.add(best_index)

    # -- per-team, per-date occupancy, the basis of the rest ----------------
    team_on_date: dict[tuple[str, date], list[object]] = defaultdict(list)
    home_on_date: dict[tuple[str, date], list[object]] = defaultdict(list)
    venue_on_date: dict[tuple[str, date], list[object]] = defaultdict(list)

    for index, (fixture, _, venue) in enumerate(fixtures):
        for day, var in placement[index].items():
            team_on_date[(fixture.home_team, day)].append(var)
            team_on_date[(fixture.away_team, day)].append(var)
            home_on_date[(fixture.home_team, day)].append(var)
            venue_on_date[(venue, day)].append(var)

    # H3 venue double booking
    venue_literal = assume("venue_double_booking")
    for vars_ in venue_on_date.values():
        if len(vars_) > 1:
            model.Add(sum(vars_) <= 1).OnlyEnforceIf(venue_literal)

    # H1 minimum rest, and by implication one match per team per day: within
    # any window of `min_rest_days` consecutive days a team plays at most once.
    rest_literal = assume("min_rest_days")
    minimum_by_team: dict[str, int] = {}
    for plan in planned:
        for team_id in plan.competition.teams:
            minimum_by_team[team_id] = max(
                minimum_by_team.get(team_id, 0), plan.competition.min_rest_days
            )

    dates_by_team: dict[str, list[date]] = defaultdict(list)
    for (team_id, day) in team_on_date:
        dates_by_team[team_id].append(day)

    for team_id, days in dates_by_team.items():
        minimum = minimum_by_team.get(team_id, 1)
        ordered = sorted(set(days))
        for start in ordered:
            window_vars: list[object] = []
            for offset in range(minimum):
                window_vars.extend(team_on_date.get((team_id, start + timedelta(days=offset)), []))
            if len(window_vars) > 1:
                model.Add(sum(window_vars) <= 1).OnlyEnforceIf(rest_literal)

    # H4 club home clash: a club's teams never both at home on one day.
    club_literal = assume("club_home_clash")
    for club in world.dual_clubs():
        team_ids = [t.id for t in club.teams]
        days: set[date] = set()
        for team_id in team_ids:
            days |= {d for (t, d) in home_on_date if t == team_id}
        for day in days:
            vars_ = [v for team_id in team_ids for v in home_on_date.get((team_id, day), [])]
            if len(vars_) > 1:
                model.Add(sum(vars_) <= 1).OnlyEnforceIf(club_literal)

    # H5 leg ordering: every first meeting before every second meeting.
    #
    # Expressed with a split date per competition rather than by comparing
    # candidate windows. Comparing windows looks cheaper but couples the
    # boundary to whichever fixture happens to own the earliest candidate — one
    # out-of-window required date is enough to make the whole model infeasible.
    # A split variable says exactly what the rule means and cannot be perturbed
    # that way.
    leg_literal = assume("leg_ordering")
    for plan in planned:
        by_leg: dict[int, list[int]] = defaultdict(list)
        for index, (fixture, competition, _) in enumerate(fixtures):
            if competition.id == plan.competition.id:
                by_leg[fixture.leg].append(index)
        if len(by_leg) < 2:
            continue

        all_days = [day for i in sum(by_leg.values(), []) for day in placement[i]]
        low, high = min(all_days).toordinal(), max(all_days).toordinal()

        for leg in sorted(by_leg)[1:]:
            split = model.NewIntVar(low, high, f"split_{plan.competition.id}_{leg}")
            for prior in range(1, leg):
                for i in by_leg[prior]:
                    for day, var in placement[i].items():
                        model.Add(day.toordinal() <= split).OnlyEnforceIf([var, leg_literal])
            for i in by_leg[leg]:
                for day, var in placement[i].items():
                    model.Add(day.toordinal() > split).OnlyEnforceIf([var, leg_literal])

    # H6 cup round conflict is not modelled as a constraint here at all — it
    # is enforced up front, by excluding conflicting dates from a fixture's
    # candidate set (see `_cup_clear`). That is stricter than an
    # `OnlyEnforceIf` constraint would be: this model's fixtures each have a
    # small, fixed candidate window around a round anchor chosen without cup
    # awareness, so a fixture boxed in by a conflict discovered only *after*
    # the candidate set was built would have no legal date left to fall back
    # on and the whole model would come out infeasible — cup conflicts
    # literally cannot occur in a solution this model produces.

    # -- objective: the soft rules CP-SAT can see ---------------------------
    objective_terms: list[tuple[int, object]] = []

    # S1 preferred weekday
    for index, (_, competition, _) in enumerate(fixtures):
        weight = competition.weights.get("preferred_weekday", 4.0)
        from ..model.schema import WEEKDAYS

        wanted = WEEKDAYS.index(competition.preferred_weekday)
        for day, var in placement[index].items():
            if day.weekday() == wanted:
                objective_terms.append((_scaled(weight), var))

    # S7 discouraged dates
    discouraged = {d.date for d in season.discouraged_dates}
    for index, (_, competition, _) in enumerate(fixtures):
        weight = competition.weights.get("soft_venue_preference", 5.0)
        for day, var in placement[index].items():
            if day in discouraged:
                objective_terms.append((-_scaled(weight), var))

    # S2 consecutive home days for a club's two teams
    for club in world.dual_clubs():
        seniors = [t.id for t in club.teams if t.level == "senior"]
        weight = _dual_weight(planned, seniors, "consecutive_home_days", 25.0)
        for i, team_a in enumerate(seniors):
            for team_b in seniors[i + 1 :]:
                for (team_id, day), vars_a in list(home_on_date.items()):
                    if team_id != team_a:
                        continue
                    for other_day, sign in ((day + _ONE_DAY, 1), (day - _ONE_DAY, 1)):
                        vars_b = home_on_date.get((team_b, other_day))
                        if not vars_b:
                            continue
                        pair = model.NewBoolVar(f"chd_{team_a}_{team_b}_{day}_{other_day}")
                        model.Add(sum(vars_a) >= 1).OnlyEnforceIf(pair)
                        model.Add(sum(vars_b) >= 1).OnlyEnforceIf(pair)
                        objective_terms.append((sign * _scaled(weight), pair))

    # S6 rest comfort. The scorer charges `weight * (comfortable - gap)` for a
    # gap between the legal minimum and the comfortable target — a graduated
    # penalty, not a threshold. Transcribing it faithfully matters: rewarding
    # only fully-clear windows made CP-SAT trade away rest wholesale to buy
    # back-to-back home days, and score worse on the real scorer while
    # believing it had improved.
    #
    # Summing a bonus over every window length from minimum+1 to comfortable
    # gives `weight * (gap - minimum)`, which differs from the scorer's penalty
    # by a constant — the same objective, in the direction CP-SAT maximises.
    for team_id, days in dates_by_team.items():
        comfortable = _comfort_for(planned, team_id)
        minimum = minimum_by_team.get(team_id, 1)
        if comfortable <= minimum:
            continue
        weight = _comfort_weight(planned, team_id)
        ordered = sorted(set(days))
        for length in range(minimum + 1, comfortable + 1):
            for start in ordered:
                window_vars: list[object] = []
                for offset in range(length):
                    window_vars.extend(
                        team_on_date.get((team_id, start + timedelta(days=offset)), [])
                    )
                if len(window_vars) <= 1:
                    continue
                clear = model.NewBoolVar(f"comfort_{team_id}_{start}_{length}")
                model.Add(sum(window_vars) <= 1).OnlyEnforceIf(clear)
                objective_terms.append((_scaled(weight), clear))

    # -- diversity: forbid re-deriving an earlier pass's answer --------------
    for previous in forbidden:
        same: list[object] = []
        for index, (fixture, _, _) in enumerate(fixtures):
            wanted = previous.get(fixture.key)
            if wanted is not None and wanted in placement[index]:
                same.append(placement[index][wanted])
        if same:
            # Force at least a fifth of the fixtures onto different dates.
            model.Add(sum(same) <= int(len(same) * 0.8))

    model.Maximize(sum(coefficient * var for coefficient, var in objective_terms))

    # Assumption literals default to true; the solver only relaxes them when
    # asked to explain an infeasibility.
    for literal in assumptions.values():
        model.AddAssumption(literal)

    return model, placement, fixtures, assumptions


def _extract(solver, placement, fixtures, request) -> list[Match]:
    matches: list[Match] = []
    for index, (fixture, competition, venue) in enumerate(fixtures):
        chosen = next(
            (day for day, var in placement[index].items() if solver.Value(var)),
            None,
        )
        if chosen is None:
            continue
        matches.append(
            Match(
                competition_id=fixture.competition_id,
                home_team=fixture.home_team,
                away_team=fixture.away_team,
                leg=fixture.leg,
                round_index=fixture.round_index,
                date=chosen,
                venue=venue,
            )
        )
    return matches


def _infeasibility_culprits(solver, status, assumptions, cp_model) -> list[str]:
    """The hard rules the solver names as responsible for an INFEASIBLE result.

    Empty if the status wasn't INFEASIBLE, or if this OR-Tools build can't
    isolate a conflicting core.
    """
    if status != cp_model.INFEASIBLE:
        return []
    culprits: list[str] = []
    try:
        core = set(solver.SufficientAssumptionsForInfeasibility())
        for name, literal in assumptions.items():
            if literal.Index() in core:
                culprits.append(name)
    except Exception:  # pragma: no cover - depends on solver build
        pass
    return culprits


def _infeasibility_note(solver, status, culprits: list[str], cp_model, pass_index: int) -> str:
    """Name the rules responsible instead of reporting a bare INFEASIBLE."""
    if status != cp_model.INFEASIBLE:
        return f"pass {pass_index}: solver returned {solver.StatusName(status)}"

    if culprits:
        return (
            f"pass {pass_index}: infeasible — the conflict involves "
            + ", ".join(sorted(culprits))
        )
    return f"pass {pass_index}: infeasible, and the solver could not isolate which rules conflict"


def _cup_clear(window: list[date], fixture, cup_windows: dict[str, list[tuple[date, int]]]) -> list[date]:
    """Drop candidate dates that conflict with either side's cup commitment.

    Mirrors how a blackout date is already never offered as a candidate: a
    date a team can't legally play is excluded up front rather than modelled
    as a constraint to satisfy after the fact.
    """
    return [
        day
        for day in window
        if not cup_conflict(cup_windows, fixture.home_team, day)
        and not cup_conflict(cup_windows, fixture.away_team, day)
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
            return plan.competition.comfortable_rest_days
    return 0


def _comfort_weight(planned, team_id: str) -> float:
    for plan in planned:
        if team_id in plan.competition.teams:
            return plan.competition.weights.get("rest_comfort", 3.0)
    return 3.0


__all__ = ["CpSatScheduler"]


def _fixture_id(fixture) -> tuple[str, int, str, str]:
    return (
        fixture.competition_id,
        fixture.round_index,
        fixture.home_team,
        fixture.away_team,
    )
