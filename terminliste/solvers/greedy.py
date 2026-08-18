"""Constructive first pass: place every fixture on a plausible date, fast.

Greedy and myopic by design. Its job is to hand the local search a complete,
usually-feasible schedule to improve, not to produce a good one — early choices
lock in badly and it cannot see what it costs later.

It scores candidate dates with a cheap local heuristic rather than the full
constraint set. A full evaluation per candidate date would mean roughly 2,500
evaluations of a 372-match schedule just to build the starting point, which is
slower than the entire annealing run that follows. The local heuristic mirrors
the real constraints closely enough to land in the right neighbourhood, and the
real scorer takes over from there.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..model.calendar import SeasonCalendar, anchor_dates, build_calendar, calendars_by_competition
from ..model.loader import World
from ..model.schema import WEEKDAYS, Competition, Fixture, Match, Season
from ..rounds.cup_schedule import CupSchedule, cup_conflict, resolved_cup_windows
from ..rounds.round_robin import generate_fixtures

# Local-heuristic weights. Deliberately crude — the real scoring lives in
# terminliste/scoring/, and these only have to rank candidate dates sensibly.
_BLOCKED = 1e9
_REST_VIOLATION = 1e6
_VENUE_CLASH = 1e6
_CLUB_HOME_CLASH = 1e6
_CUP_CONFLICT = 1e6
_OFF_PREFERRED_WEEKDAY = 12.0
_DISCOURAGED_DATE = 20.0
_CONSECUTIVE_HOME_BONUS = 40.0
_DRIFT_FROM_ANCHOR = 3.0

_ONE_DAY = timedelta(days=1)


@dataclass
class PlacementState:
    """Incremental bookkeeping, so a candidate date is O(1) to judge."""

    team_dates: dict[str, set[date]] = field(default_factory=dict)
    venue_dates: set[tuple[str, date]] = field(default_factory=set)
    club_home_dates: dict[str, dict[date, list[str]]] = field(default_factory=dict)

    def place(self, world: World, match: Match) -> None:
        self.team_dates.setdefault(match.home_team, set()).add(match.date)
        self.team_dates.setdefault(match.away_team, set()).add(match.date)
        self.venue_dates.add((match.venue, match.date))
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


def plan_competitions(
    world: World,
    season: Season,
    competitions: list[Competition],
    calendar: SeasonCalendar,
    rng: random.Random | None = None,
) -> list[PlannedCompetition]:
    """Generate fixtures and pick a target date per round, per competition.

    Passing an `rng` shuffles the team order before the circle method runs.
    That is the only lever that produces *structurally* different tournaments —
    different opponents in different rounds — rather than the same fixture list
    with the dates nudged. Without it every restart converges on near-identical
    schedules and the three options offered at the end are not a real choice.
    """
    planned: list[PlannedCompetition] = []
    by_competition = calendars_by_competition(calendar, competitions)
    for competition in competitions:
        teams = list(competition.teams)
        if rng is not None:
            rng.shuffle(teams)
        fixtures = generate_fixtures(competition.id, teams, competition.rounds_per_pairing)
        round_indexes = sorted({f.round_index for f in fixtures})
        dates = anchor_dates(
            by_competition[competition.id],
            competition.preferred_weekday,
            count=len(round_indexes),
            min_gap_days=competition.min_gap_days,
        )
        if len(dates) < len(round_indexes):
            raise ValueError(
                f"{competition.id}: season has room for only {len(dates)} of "
                f"{len(round_indexes)} rounds"
            )
        planned.append(
            PlannedCompetition(
                competition=competition,
                fixtures=fixtures,
                anchors=dict(zip(round_indexes, dates)),
            )
        )
    return planned


def align_dual_clubs(
    world: World, planned: list[PlannedCompetition], rounds: int = 400
) -> None:
    """Relabel teams in the later competitions so dual clubs line up.

    The back-to-back home weekend — Brann women on Saturday, Brann men on
    Sunday — only happens if the two leagues put that club's two teams at home
    in rounds whose anchor dates are a day apart. Whether they do is decided by
    where each team sits in the circle-method rotation, which is otherwise
    arbitrary.

    Leaving it to chance is what makes the difference between a schedule with
    thirty back-to-back weekends and one with five, so this hill-climbs over
    team labels in every competition after the first, maximising the number of
    home rounds that land next to a sibling team's home round.

    Relabelling preserves the round-robin structure exactly — it only changes
    which club wears which slot — so this can never produce an invalid
    tournament.
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

        teams = list(plan.competition.teams)
        # Only teams belonging to a dual club can score, but any team may need
        # to move out of the way, so the swap pool is the whole roster.
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

    Fixed for the duration of the hill-climb, because only the later
    competition's labels move.
    """
    # Which of this competition's rounds fall a day either side of each of the
    # anchor competition's rounds.
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
    season: Season, planned: list[PlannedCompetition]
) -> dict[tuple[str, int], date]:
    """Rounds that must land on one single date, keyed by (competition_id, round_index).

    Two sources, both forced entirely onto one date rather than searched for:
    every league's own final round (`FinalRoundSameSlot` in `scoring/hard.py`),
    and any hard `FullRoundRequirement` — the round whose anchor already sits
    closest to the required date is the one forced onto it exactly
    (`FullRoundOnDate` in `scoring/hard.py`).
    """
    pins: dict[tuple[str, int], date] = {}
    for plan in planned:
        if plan.competition.format != "league":
            continue
        final_round = plan.competition.rounds - 1
        anchor = plan.anchors.get(final_round)
        if anchor is not None:
            pins[(plan.competition.id, final_round)] = anchor

    for requirement in season.full_round_requirements:
        if not requirement.hard:
            continue
        plan = next((p for p in planned if p.competition.id == requirement.competition), None)
        if plan is None or not plan.anchors:
            continue
        round_index = min(
            plan.anchors, key=lambda r: abs((plan.anchors[r] - requirement.date).days)
        )
        pins[(plan.competition.id, round_index)] = requirement.date

    return pins


def align_home_teams_to_round_pins(
    season: Season,
    planned: list[PlannedCompetition],
    round_pins: dict[tuple[str, int], date],
) -> None:
    """Flip fixtures so a hard `FixedRequirement`'s team is home in its pinned round.

    A round pinned onto a specific date (see `resolve_round_pins`) is placed
    with whatever home/away orientation the round-robin generator happened to
    give it — which has no reason to already put e.g. Brann at home on May 16.
    This finds each hard requirement whose date coincides with a pinned
    round and, if its team is away there, flips that fixture (and its
    season's mirror, to keep every pairing meeting home once and away once).
    """
    by_id = {p.competition.id: p for p in planned}
    round_by_competition_date = {
        (competition_id, day): round_index
        for (competition_id, round_index), day in round_pins.items()
    }
    for requirement in season.fixed_requirements:
        if not requirement.hard:
            continue
        round_index = round_by_competition_date.get((requirement.competition, requirement.date))
        if round_index is None:
            continue
        plan = by_id.get(requirement.competition)
        if plan is None:
            continue
        _ensure_home_in_round(plan, requirement.home_team, round_index)


def _ensure_home_in_round(plan: PlannedCompetition, team_id: str, round_index: int) -> None:
    """Make `team_id` the home side of its `round_index` fixture, if it isn't already.

    Flips the fixture in place, and its season mirror (the same pairing's
    other leg) alongside it — flipping only one leg would leave the pair
    meeting twice at the same ground, exactly as `_flip_orientation` in
    `solvers/local_search.py` guards against during search.
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
) -> tuple[list[Match], set[str]]:
    """Return a complete schedule plus the keys of fixtures pinned to a date.

    Pinned fixtures are the ones satisfying a hard `fixed_requirement`; the
    local search must not move them.

    `align=False` skips the dual-club alignment pass — used by the integration
    test to show what the coupled scheduling is actually worth, by comparing
    against the schedule this same greedy placer would produce if the two
    leagues were built without any awareness of each other.

    `cup_schedules` (already resolved to a date per team) steers placement
    away from each team's cup round dates up front, so the local search
    starts from a schedule that mostly already respects `CupRoundConflict`
    instead of having to fight its way there.
    """
    rng = random.Random(seed)
    calendar = calendar or build_calendar(world, season)
    by_competition = calendars_by_competition(calendar, competitions)
    planned = plan_competitions(world, season, competitions, calendar, rng)
    if align:
        align_dual_clubs(world, planned)
    round_pins = resolve_round_pins(season, planned)
    align_home_teams_to_round_pins(season, planned, round_pins)
    cup_windows = resolved_cup_windows(cup_schedules or [])

    state = PlacementState()
    matches: list[Match] = []
    pinned: set[str] = set()

    remaining = {p.competition.id: list(p.fixtures) for p in planned}
    by_id = {p.competition.id: p for p in planned}

    # Fixed requirements come first — they are the least flexible thing in the
    # season, and placing them last would mean unpicking everything around them.
    for requirement in season.fixed_requirements:
        if not requirement.hard:
            continue
        plan = by_id.get(requirement.competition)
        if plan is None:
            continue
        fixture = _pick_fixture_for_requirement(plan, requirement.home_team, requirement.date)
        if fixture is None:
            continue
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

    # Rounds pinned to a single date next — a league's final round, or a
    # FullRoundRequirement's round (see `resolve_round_pins`). Placed directly
    # and pinned, the same way a single-team fixed requirement is: local
    # search must not be free to drift one match off its round's shared date
    # while leaving the rest behind.
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

    # Then everything else, in chronological round order across both leagues so
    # the two competitions compete for dates fairly instead of the first one
    # scheduled taking every good slot.
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
        )
        matches.append(match)
        state.place(world, match)

    matches.sort(key=lambda m: (m.date, m.competition_id, m.home_team))
    return matches, pinned


def _pick_fixture_for_requirement(
    plan: PlannedCompetition, home_team: str, target: date
) -> Fixture | None:
    """The home fixture whose round already sits closest to the required date."""
    candidates = [f for f in plan.fixtures if f.home_team == home_team]
    if not candidates:
        return None
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
) -> Match:
    competition = plan.competition
    venue = world.team(fixture.home_team).home_venue
    window = calendar.window(anchor, competition.match_window_days, venue)

    if not window:
        # Every date in the window is blocked. Widen rather than fail: a match
        # placed badly is fixable by the local search, a missing one is not.
        window = calendar.window(anchor, competition.match_window_days * 3, venue) or [anchor]

    preferred_weekday = WEEKDAYS.index(competition.preferred_weekday)
    best_day = window[0]
    best_cost = float("inf")

    for day in window:
        cost = _placement_cost(
            world, calendar, competition, fixture, venue, day, anchor, preferred_weekday, state,
            cup_windows,
        )
        # A little noise so different seeds explore different starting points;
        # small enough never to outweigh a real preference.
        cost += rng.random() * 0.5
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

    if (venue, day) in state.venue_dates:
        cost += _VENUE_CLASH

    club_id = world.team(fixture.home_team).club_id
    club_days = state.club_home_dates.get(club_id, {})
    if day in club_days:
        cost += _CLUB_HOME_CLASH
    else:
        # The flip side of the clash rule: the day either side of a sibling
        # team's home match is the most valuable date in the season.
        if (day - _ONE_DAY) in club_days or (day + _ONE_DAY) in club_days:
            cost -= _CONSECUTIVE_HOME_BONUS

    if day.weekday() != preferred_weekday:
        cost += _OFF_PREFERRED_WEEKDAY
    if calendar.is_discouraged(day):
        cost += _DISCOURAGED_DATE

    cost += _DRIFT_FROM_ANCHOR * abs((day - anchor).days)
    return cost
