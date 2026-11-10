"""Constructive first pass: place every fixture on a plausible date, fast.

Greedy and myopic by design. Its job is to hand the local search a complete,
usually-feasible schedule to improve, not to produce a good one — early choices
lock in badly and it cannot see what it costs later.

Candidate dates are ranked by a cheap local heuristic rather than the full
constraint set: a full evaluation per candidate would mean ~2,500 evaluations
of a 372-match schedule just to build the starting point, slower than the whole
annealing run that follows. The heuristic only has to land in the right
neighbourhood.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..frame import SeasonFrame
from ..model.calendar import (
    SeasonCalendar,
    build_calendar,
    calendars_by_competition,
)
from ..model.loader import World
from ..model.schema import WEEKDAYS, Competition, Fixture, Match, Season
from ..model.slots import RoundSlot
from ..rounds.cup_schedule import CupSchedule, cup_conflict, resolved_cup_windows
from ..rounds.european_schedule import EuropeanCommitmentDate, european_conflict
from ..rounds.round_robin import generate_fixtures
from ..slot_planner import SlotPlan, plan_slots

# Local-heuristic weights. Deliberately crude — the real scoring lives in
# terminliste/scoring/, and these only have to rank candidate dates sensibly.
_BLOCKED = 1e9
_REST_VIOLATION = 1e6
_VENUE_CLASH = 1e6
_CLUB_HOME_CLASH = 1e6
_CUP_CONFLICT = 1e6
_EUROPEAN_CONFLICT = 1e6
# A non-binding European date is only a soft penalty in scoring, so placement
# leans away from it without treating it as forbidden.
_EUROPEAN_SOFT_CONFLICT = 30.0
_OFF_PREFERRED_WEEKDAY = 12.0
_DISCOURAGED_DATE = 20.0
_CONSECUTIVE_HOME_BONUS = 40.0
_DRIFT_FROM_ANCHOR = 3.0
# A quota is a hard rule, so breaking it costs more than any preference; it
# stays below the rest and venue clashes, which a repair cannot undo.
_QUOTA_EXCEEDED = 5e5

_ONE_DAY = timedelta(days=1)


@dataclass
class PlacementState:
    """Incremental bookkeeping, so a candidate date is O(1) to judge."""

    team_dates: dict[str, set[date]] = field(default_factory=dict)
    venue_dates: set[tuple[str, date]] = field(default_factory=set)
    club_home_dates: dict[str, dict[date, list[str]]] = field(default_factory=dict)
    # Matches placed per (competition, round, weekday), for `day_quota`.
    weekday_counts: dict[tuple[str, int, int], int] = field(default_factory=dict)

    def place(self, world: World, match: Match) -> None:
        self.team_dates.setdefault(match.home_team, set()).add(match.date)
        self.team_dates.setdefault(match.away_team, set()).add(match.date)
        self.venue_dates.add((match.venue, match.date))
        quota_key = (match.competition_id, match.round_index, match.date.weekday())
        self.weekday_counts[quota_key] = self.weekday_counts.get(quota_key, 0) + 1
        club_id = world.team(match.home_team).club_id
        self.club_home_dates.setdefault(club_id, {}).setdefault(match.date, []).append(
            match.home_team
        )

    def rest_conflict(self, team_id: str, day: date, minimum: int) -> bool:
        taken = self.team_dates.get(team_id)
        if not taken:
            return False
        return any(abs((day - other).days) < minimum for other in taken)


@dataclass
class PlannedCompetition:
    """A competition's fixtures with each round's target date."""

    competition: Competition
    fixtures: list[Fixture]
    anchors: dict[int, date]
    # Each round's planned slot, for the day-within-slot step (#7).
    slots: dict[int, RoundSlot] = field(default_factory=dict)


def plan_competitions(
    world: World,
    season: Season,
    competitions: list[Competition],
    calendar: SeasonCalendar,
    rng: random.Random | None = None,
    slot_plans: dict[str, SlotPlan] | None = None,
) -> list[PlannedCompetition]:
    """Generate fixtures and take each round's slot and anchor from the plan.

    An `rng` shuffles the team order before the circle method runs — the only
    lever producing structurally different tournaments rather than the same
    fixture list with nudged dates. Without it every restart converges on
    near-identical schedules and the options offered at the end aren't a real
    choice.

    `slot_plans` come from the season frame; without them the slots are
    planned against blackouts alone. Raises `SlotPlanningError` (a
    `ValueError`) when a league has fewer slots than rounds.
    """
    if slot_plans is None:
        slot_plans = plan_slots(world, season, competitions, calendar)
    planned: list[PlannedCompetition] = []
    for competition in competitions:
        teams = list(competition.teams)
        if rng is not None:
            rng.shuffle(teams)
        fixtures = generate_fixtures(competition.id, teams, competition.rounds_per_pairing)
        slot_plan = slot_plans[competition.id]
        planned.append(
            PlannedCompetition(
                competition=competition,
                fixtures=fixtures,
                anchors=slot_plan.anchors,
                slots=dict(enumerate(slot_plan.chosen)),
            )
        )
    return planned


def align_dual_clubs(
    world: World, planned: list[PlannedCompetition], rounds: int = 400
) -> None:
    """Relabel teams in the later competitions so dual clubs line up.

    A back-to-back home weekend needs a club's two teams at home in rounds
    whose anchors are a day apart, which is otherwise decided arbitrarily by
    the circle-method rotation — the difference between thirty such weekends
    and five. So hill-climb over team labels in every competition after the
    first, maximising home rounds landing next to a sibling's.

    Relabelling only changes which club wears which slot, so the round-robin
    structure survives exactly.
    """
    if len(planned) < 2:
        return

    anchor_plan, *rest = planned
    home_rounds_anchor = _home_rounds(anchor_plan)

    for plan in rest:
        home_rounds = _home_rounds(plan)
        targets = _alignment_targets(world, anchor_plan, plan, home_rounds_anchor)
        if not targets:
            continue

        # Only dual-club teams can score, but any team may need to move out of
        # the way, so the swap pool is the whole roster.
        teams = list(plan.competition.teams)
        current = _alignment_score(targets, home_rounds)
        improved = True
        budget = rounds
        while improved and budget > 0:
            improved = False
            for i, team_a in enumerate(teams):
                for team_b in teams[i + 1 :]:
                    budget -= 1
                    if budget <= 0:
                        break
                    home_rounds[team_a], home_rounds[team_b] = (
                        home_rounds[team_b],
                        home_rounds[team_a],
                    )
                    candidate = _alignment_score(targets, home_rounds)
                    if candidate > current:
                        current = candidate
                        _relabel(plan, team_a, team_b)
                        improved = True
                    else:
                        home_rounds[team_a], home_rounds[team_b] = (
                            home_rounds[team_b],
                            home_rounds[team_a],
                        )
                if budget <= 0:
                    break


def _home_rounds(plan: PlannedCompetition) -> dict[str, set[int]]:
    rounds: dict[str, set[int]] = {team: set() for team in plan.competition.teams}
    for fixture in plan.fixtures:
        rounds.setdefault(fixture.home_team, set()).add(fixture.round_index)
    return rounds


def _alignment_targets(
    world: World,
    anchor_plan: PlannedCompetition,
    plan: PlannedCompetition,
    home_rounds_anchor: dict[str, set[int]],
) -> dict[str, set[int]]:
    """For each dual-club team here, the rounds that sit next to a sibling's.

    Fixed for the hill-climb's duration, since only the later competition's
    labels move.
    """
    # This competition's rounds falling a day either side of an anchor round.
    adjacent: dict[int, set[int]] = {}
    for anchor_round, anchor_date in anchor_plan.anchors.items():
        neighbours = {
            round_index
            for round_index, day in plan.anchors.items()
            if abs((day - anchor_date).days) == 1
        }
        if neighbours:
            adjacent[anchor_round] = neighbours

    targets: dict[str, set[int]] = {}
    for club in world.dual_clubs():
        team_here = next((t.id for t in club.teams if t.id in plan.competition.teams), None)
        sibling = next(
            (t.id for t in club.teams if t.id in anchor_plan.competition.teams), None
        )
        if team_here is None or sibling is None:
            continue
        wanted: set[int] = set()
        for anchor_round in home_rounds_anchor.get(sibling, ()):
            wanted |= adjacent.get(anchor_round, set())
        if wanted:
            targets[team_here] = wanted
    return targets


def _alignment_score(targets: dict[str, set[int]], home_rounds: dict[str, set[int]]) -> int:
    return sum(len(home_rounds.get(team, set()) & wanted) for team, wanted in targets.items())


def resolve_round_pins(
    season: Season,
    planned: list[PlannedCompetition],
    by_competition: dict[str, SeasonCalendar] | None = None,
    cup_windows: dict[str, list[tuple[date, int]]] | None = None,
    european_commitments: dict[str, list[EuropeanCommitmentDate]] | None = None,
) -> tuple[dict[tuple[str, int], date], list[str]]:
    """Rounds that must land on one single date, keyed by (competition_id, round_index).

    Three sources: every league's final round (`FinalRoundSameSlot`), a
    league's `fixed_round_dates` (`FixedRoundDates`), and any hard
    `FullRoundRequirement`, which claims the round whose anchor already sits
    closest to it (`FullRoundOnDate`).

    A pinned round puts *every* team on the same date, so a resolved cup or
    European commitment for even one of them conflicts — unlike a single
    fixture's window, which need only clear the two teams playing. Conflicts
    are returned as warnings rather than raised, so a caller can surface them
    without failing the solve.

    A final round's date is only a computed anchor, so it moves to the nearest
    clear date; a `FullRoundRequirement`'s date is fixed and can only be
    flagged.
    """
    european_commitments = european_commitments or {}
    cup_windows = cup_windows or {}

    pins: dict[tuple[str, int], date] = {}
    warnings: list[str] = []

    for plan in planned:
        if plan.competition.format != "league":
            continue
        final_round = plan.competition.rounds - 1
        anchor = plan.anchors.get(final_round)
        if anchor is None:
            continue
        calendar = (by_competition or {}).get(plan.competition.id)
        chosen, warning = _clear_pin_date(
            anchor,
            plan,
            calendar,
            cup_windows,
            european_commitments,
            movable=True,
            slot=plan.slots.get(final_round),
        )
        pins[(plan.competition.id, final_round)] = chosen
        if warning:
            warnings.append(warning)

    for plan in planned:
        for fixed in plan.competition.fixed_round_dates:
            round_index = fixed.round - 1
            if round_index not in plan.anchors:
                continue
            _, warning = _clear_pin_date(
                fixed.date, plan, None, cup_windows, european_commitments, movable=False
            )
            pins[(plan.competition.id, round_index)] = fixed.date
            if warning:
                warnings.append(warning)

    for requirement in season.full_round_requirements:
        if not requirement.hard:
            continue
        plan = next((p for p in planned if p.competition.id == requirement.competition), None)
        if plan is None or not plan.anchors:
            continue
        round_index = _round_for_date(plan, requirement.date)
        _, warning = _clear_pin_date(
            requirement.date, plan, None, cup_windows, european_commitments, movable=False
        )
        pins[(plan.competition.id, round_index)] = requirement.date
        if warning:
            warnings.append(warning)

    return pins, warnings


def _round_for_date(plan: PlannedCompetition, day: date) -> int:
    """The round whose slot holds `day`, else the one whose anchor is nearest."""
    for round_index, slot in plan.slots.items():
        if slot.contains(day):
            return round_index
    return min(plan.anchors, key=lambda r: abs((plan.anchors[r] - day).days))


def _clear_pin_date(
    anchor: date,
    plan: PlannedCompetition,
    calendar: SeasonCalendar | None,
    cup_windows: dict[str, list[tuple[date, int]]],
    european_commitments: dict[str, list[EuropeanCommitmentDate]],
    movable: bool,
    slot: RoundSlot | None = None,
) -> tuple[date, str | None]:
    """The pin date to use, plus a warning if it still conflicts.

    Tries the anchor, then — only when `movable` — the other days of the
    round's slot, nearest first.
    """
    team_ids = plan.competition.teams
    if _round_clear(anchor, team_ids, cup_windows, european_commitments):
        return anchor, None

    if movable and slot is not None:
        for day in sorted(slot.days, key=lambda d: (abs((d - anchor).days), d)):
            if calendar is not None and not calendar.is_allowed(day):
                continue
            if _round_clear(day, team_ids, cup_windows, european_commitments):
                return day, None

    round_desc = "final round" if movable else "full-round requirement"
    resolution = "no clear nearby date was found" if movable else "the date cannot move"
    return anchor, (
        f"{plan.competition.id}: {round_desc} on {anchor} conflicts with a resolved cup or "
        f"European commitment for at least one team, and {resolution}"
    )


def _round_clear(
    day: date,
    team_ids: list[str],
    cup_windows: dict[str, list[tuple[date, int]]],
    european_commitments: dict[str, list[EuropeanCommitmentDate]],
) -> bool:
    return not any(
        cup_conflict(cup_windows, team_id, day) or european_conflict(european_commitments, team_id, day)
        for team_id in team_ids
    )


def align_home_teams_to_requirements(season: Season, planned: list[PlannedCompetition]) -> None:
    """Flip fixtures so a hard `FixedRequirement`'s team is home in the round
    whose slot holds its date.

    The round-robin generator has no reason to put e.g. Brann at home on
    May 16. Flips the fixture and its season mirror, keeping every pairing
    meeting home once and away once. Two required teams due at home in one
    round cannot meet each other, so one is relabelled away from the other
    first.
    """
    by_id = {p.competition.id: p for p in planned}
    required: dict[str, list[tuple[str, int]]] = {}
    for requirement in season.fixed_requirements:
        plan = by_id.get(requirement.competition)
        if not requirement.hard or plan is None or not plan.anchors:
            continue
        required.setdefault(plan.competition.id, []).append(
            (requirement.home_team, _round_for_date(plan, requirement.date))
        )

    for competition_id, needs in required.items():
        plan = by_id[competition_id]
        for _ in range(_SEPARATION_ATTEMPTS):
            if not _separate_required_teams(plan, needs):
                break
        for team_id, round_index in needs:
            _ensure_home_in_round(plan, team_id, round_index)


_SEPARATION_ATTEMPTS = 20


def _meet(plan: PlannedCompetition, team_a: str, team_b: str, round_index: int) -> bool:
    return any(
        f.round_index == round_index and {f.home_team, f.away_team} == {team_a, team_b}
        for f in plan.fixtures
    )


def _meetings(plan: PlannedCompetition, needs: list[tuple[str, int]]) -> list[tuple[str, str]]:
    """Required teams that meet each other in the round both must be home."""
    return [
        (team_a, team_b)
        for i, (team_a, round_a) in enumerate(needs)
        for team_b, round_b in needs[i + 1 :]
        if round_a == round_b and _meet(plan, team_a, team_b, round_a)
    ]


def _separate_required_teams(plan: PlannedCompetition, needs: list[tuple[str, int]]) -> bool:
    """Relabel one team of a clashing pair with a team that has no requirement,
    keeping the change only if it leaves fewer clashes. Returns whether it
    changed anything."""
    clashes = _meetings(plan, needs)
    if not clashes:
        return False
    required_teams = {team for team, _ in needs}
    _, team_b = clashes[0]
    for other in plan.competition.teams:
        if other in required_teams:
            continue
        _relabel(plan, team_b, other)
        if len(_meetings(plan, needs)) < len(clashes):
            return True
        _relabel(plan, team_b, other)
    return False


def _ensure_home_in_round(plan: PlannedCompetition, team_id: str, round_index: int) -> None:
    """Make `team_id` the home side of its `round_index` fixture.

    Flips the season mirror alongside it: flipping only one leg would leave
    the pair meeting twice at the same ground.
    """
    fixture = next(
        (
            f
            for f in plan.fixtures
            if f.round_index == round_index and team_id in (f.home_team, f.away_team)
        ),
        None,
    )
    if fixture is None or fixture.home_team == team_id:
        return

    pair = {fixture.home_team, fixture.away_team}
    mirror = next(
        (
            f
            for f in plan.fixtures
            if f.round_index != round_index and {f.home_team, f.away_team} == pair
        ),
        None,
    )
    for target in (f for f in (fixture, mirror) if f is not None):
        index = plan.fixtures.index(target)
        plan.fixtures[index] = target.model_copy(
            update={"home_team": target.away_team, "away_team": target.home_team}
        )


def _relabel(plan: PlannedCompetition, team_a: str, team_b: str) -> None:
    for i, fixture in enumerate(plan.fixtures):
        home = _swap_label(fixture.home_team, team_a, team_b)
        away = _swap_label(fixture.away_team, team_a, team_b)
        if home != fixture.home_team or away != fixture.away_team:
            plan.fixtures[i] = fixture.model_copy(
                update={"home_team": home, "away_team": away}
            )


def _swap_label(team: str, a: str, b: str) -> str:
    if team == a:
        return b
    if team == b:
        return a
    return team


def build_initial_schedule(
    world: World,
    season: Season,
    competitions: list[Competition],
    seed: int = 42,
    calendar: SeasonCalendar | None = None,
    align: bool = True,
    cup_schedules: list[CupSchedule] | None = None,
    european_commitments: dict[str, list[EuropeanCommitmentDate]] | None = None,
    slot_plans: dict[str, SlotPlan] | None = None,
) -> tuple[list[Match], set[str], list[str]]:
    """Return a complete schedule, the keys of fixtures pinned to a date, and
    any warnings from resolving those pins.

    Pinned fixtures satisfy a hard `fixed_requirement`; the local search must
    not move them.

    `align=False` skips the dual-club alignment pass, letting the integration
    test measure what coupled scheduling is worth against the same placer run
    with the leagues unaware of each other.

    `cup_schedules` and `european_commitments` steer placement away from each
    team's resolved commitment dates up front, so the local search starts from
    a schedule that mostly already respects `CupRoundConflict` and
    `EuropeanCommitmentConflict`.

    `slot_plans` fix which slot each round sits in; without them they are
    planned from the same cups and commitments.
    """
    rng = random.Random(seed)
    calendar = calendar or build_calendar(world, season)
    by_competition = calendars_by_competition(calendar, competitions)
    if slot_plans is None:
        frame = SeasonFrame.from_resolved(
            world, cup_schedules or [], european_commitments or {}, competitions
        )
        slot_plans = plan_slots(world, season, competitions, calendar, frame)
    planned = plan_competitions(world, season, competitions, calendar, rng, slot_plans)
    if align:
        align_dual_clubs(world, planned)
    cup_windows = resolved_cup_windows(cup_schedules or [])
    european = european_commitments or {}
    round_pins, pin_warnings = resolve_round_pins(
        season, planned, by_competition, cup_windows, european
    )
    align_home_teams_to_requirements(season, planned)

    state = PlacementState()
    matches: list[Match] = []
    pinned: set[str] = set()

    remaining = {p.competition.id: list(p.fixtures) for p in planned}
    by_id = {p.competition.id: p for p in planned}

    # Fixed requirements first: the least flexible thing in the season, and
    # placing them last would mean unpicking everything around them.
    claimed: set[tuple[str, int, str, str]] = set()
    for requirement in season.fixed_requirements:
        if not requirement.hard:
            continue
        plan = by_id.get(requirement.competition)
        if plan is None:
            continue
        fixture = pick_fixture_for_requirement(
            plan, requirement.home_team, requirement.date, claimed
        )
        if fixture is None:
            continue
        claimed.add((fixture.competition_id, fixture.round_index, fixture.home_team, fixture.away_team))
        if not plan.slots[fixture.round_index].contains(requirement.date):
            pin_warnings.append(
                f"{requirement.competition}: fixed requirement {requirement.id} on "
                f"{requirement.date} lies outside every round slot; the match is placed "
                f"there regardless of its round's slot"
            )
        venue = world.team(fixture.home_team).home_venue
        match = Match(
            competition_id=fixture.competition_id,
            home_team=fixture.home_team,
            away_team=fixture.away_team,
            leg=fixture.leg,
            round_index=fixture.round_index,
            date=requirement.date,
            venue=venue,
        )
        matches.append(match)
        state.place(world, match)
        pinned.add(match.key)
        remaining[plan.competition.id].remove(fixture)

    # Then rounds pinned to a single date (see `resolve_round_pins`), placed
    # and pinned like a fixed requirement so local search cannot drift one
    # match off its round's shared date.
    for plan in planned:
        for fixture in list(remaining[plan.competition.id]):
            pin_date = round_pins.get((plan.competition.id, fixture.round_index))
            if pin_date is None:
                continue
            venue = world.team(fixture.home_team).home_venue
            match = Match(
                competition_id=fixture.competition_id,
                home_team=fixture.home_team,
                away_team=fixture.away_team,
                leg=fixture.leg,
                round_index=fixture.round_index,
                date=pin_date,
                venue=venue,
            )
            matches.append(match)
            state.place(world, match)
            pinned.add(match.key)
            remaining[plan.competition.id].remove(fixture)

    # Everything else in chronological round order across both leagues, so the
    # competitions compete for dates fairly instead of the first one scheduled
    # taking every good slot.
    work: list[tuple[date, int, Fixture]] = []
    for plan in planned:
        for fixture in remaining[plan.competition.id]:
            work.append((plan.anchors[fixture.round_index], rng.randrange(1000), fixture))
    work.sort(key=lambda item: (item[0], item[1]))

    for anchor, _, fixture in work:
        plan = by_id[fixture.competition_id]
        match = _place_fixture(
            world,
            by_competition[fixture.competition_id],
            plan,
            fixture,
            anchor,
            state,
            rng,
            cup_windows,
            european,
        )
        matches.append(match)
        state.place(world, match)

    matches.sort(key=lambda m: (m.date, m.competition_id, m.home_team))
    return matches, pinned, pin_warnings


def pick_fixture_for_requirement(
    plan: PlannedCompetition,
    home_team: str,
    target: date,
    taken: set[tuple[str, int, str, str]] | None = None,
) -> Fixture | None:
    """The team's home fixture in the round whose slot holds `target`, else the
    one whose round sits closest. `taken` holds fixtures already claimed by
    another requirement, as (competition, round, home, away)."""
    taken = taken or set()
    candidates = [
        f
        for f in plan.fixtures
        if f.home_team == home_team
        and (f.competition_id, f.round_index, f.home_team, f.away_team) not in taken
    ]
    if not candidates:
        return None
    round_index = _round_for_date(plan, target)
    in_round = [f for f in candidates if f.round_index == round_index]
    if in_round:
        return in_round[0]
    return min(candidates, key=lambda f: abs((plan.anchors[f.round_index] - target).days))


def _place_fixture(
    world: World,
    calendar: SeasonCalendar,
    plan: PlannedCompetition,
    fixture: Fixture,
    anchor: date,
    state: PlacementState,
    rng: random.Random,
    cup_windows: dict[str, list[tuple[date, int]]],
    european_commitments: dict[str, list[EuropeanCommitmentDate]],
) -> Match:
    competition = plan.competition
    venue = world.team(fixture.home_team).home_venue
    # A blocked day stays a candidate (at `_BLOCKED` cost) so a match never
    # leaves its slot, even when the slot has no clean day for it.
    window = list(plan.slots[fixture.round_index].days)

    preferred_weekday = WEEKDAYS.index(competition.preferred_weekday)
    best_day = window[0]
    best_cost = float("inf")

    for day in window:
        cost = _placement_cost(
            world, calendar, competition, fixture, venue, day, anchor, preferred_weekday, state,
            cup_windows, european_commitments,
        )
        # Noise so different seeds explore different starting points, small
        # enough never to outweigh a real preference.
        cost += rng.random() * 0.5
        limit = competition.day_quota.get(WEEKDAYS[day.weekday()])
        if limit is not None and (
            state.weekday_counts.get((competition.id, fixture.round_index, day.weekday()), 0)
            >= limit
        ):
            cost += _QUOTA_EXCEEDED
        if cost < best_cost:
            best_cost, best_day = cost, day

    return Match(
        competition_id=fixture.competition_id,
        home_team=fixture.home_team,
        away_team=fixture.away_team,
        leg=fixture.leg,
        round_index=fixture.round_index,
        date=best_day,
        venue=venue,
    )


def _placement_cost(
    world: World,
    calendar: SeasonCalendar,
    competition: Competition,
    fixture: Fixture,
    venue: str,
    day: date,
    anchor: date,
    preferred_weekday: int,
    state: PlacementState,
    cup_windows: dict[str, list[tuple[date, int]]],
    european_commitments: dict[str, list[EuropeanCommitmentDate]],
) -> float:
    cost = 0.0

    if not calendar.is_allowed(day, venue):
        cost += _BLOCKED

    minimum = competition.min_gap_days
    if state.rest_conflict(fixture.home_team, day, minimum):
        cost += _REST_VIOLATION
    if state.rest_conflict(fixture.away_team, day, minimum):
        cost += _REST_VIOLATION

    if cup_conflict(cup_windows, fixture.home_team, day) or cup_conflict(
        cup_windows, fixture.away_team, day
    ):
        cost += _CUP_CONFLICT

    cost += european_cost(european_commitments, fixture, day)

    if (venue, day) in state.venue_dates:
        cost += _VENUE_CLASH

    club_id = world.team(fixture.home_team).club_id
    club_days = state.club_home_dates.get(club_id, {})
    if day in club_days:
        cost += _CLUB_HOME_CLASH
    else:
        # Flip side of the clash rule: the day either side of a sibling's home
        # match is the most valuable date in the season.
        if (day - _ONE_DAY) in club_days or (day + _ONE_DAY) in club_days:
            cost -= _CONSECUTIVE_HOME_BONUS

    if day.weekday() != preferred_weekday:
        cost += _OFF_PREFERRED_WEEKDAY
    if calendar.is_discouraged(day):
        cost += _DISCOURAGED_DATE

    cost += _DRIFT_FROM_ANCHOR * abs((day - anchor).days)
    return cost


def european_cost(
    european_commitments: dict[str, list[EuropeanCommitmentDate]], fixture: Fixture, day: date
) -> float:
    """Placement cost of `fixture` on `day` from either team's European dates:
    forbidding for a binding one, a nudge for a non-binding one (#112)."""
    teams = (fixture.home_team, fixture.away_team)
    if any(european_conflict(european_commitments, team, day) for team in teams):
        return _EUROPEAN_CONFLICT
    if any(european_conflict(european_commitments, team, day, binding=False) for team in teams):
        return _EUROPEAN_SOFT_CONFLICT
    return 0.0
