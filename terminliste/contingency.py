"""Fallback dates for league matches a later cup or UEFA round may displace (#9).

The base plan treats a contingent commitment (one a team reaches only with
some probability) as free. `ContingencyPlanner` then gives every match that
sits within rest distance of one a fallback: the date it moves to if that
commitment turns out to be real. Fallbacks are derived from the base plan,
never searched on their own, so local search, CP-SAT and the external-schedule
scorer all see the same plan for the same fixtures; a base move that strands a
match simply re-plans it.

A fallback is checked in the scenario its trigger describes: the team's hard
commitments plus every contingent commitment that can be played alongside the
trigger. Two fallbacks may only share a team, venue or club date when no
scenario fires both.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median

from .model.loader import World
from .model.schema import Competition, FallbackKind, Season
from .rounds.commitments import Commitment, CommitmentSet

# Disruption cost of one move: a flat price per kind plus a price per day
# shifted, so a Sunday-to-Monday swap is cheap and a three-week slide is not.
KIND_COST: dict[FallbackKind, float] = {
    "same_slot": 0.5,
    "reserve_date": 1.5,
    "next_free_slot": 2.5,
}
DAY_COST = 0.25

# Fri-Mon is the span a league round occupies (see `model/slots.py`).
_FRIDAY = 4
_WEEKEND_WEEKDAYS = frozenset({4, 5, 6, 0})
_SLOT_SPAN_DAYS = 4

# A cup round whose advance chance is at most this is read as top-flight
# sides drawn against each other.
_PEER_ADVANCE = 0.5 + 1e-9


@dataclass(frozen=True)
class Fallback:
    """Where a match goes if one of its triggers is reached."""

    match_key: str
    competition_id: str
    home_team: str
    away_team: str
    date: date
    venue: str
    kickoff: str | None
    trigger_node_ids: tuple[str, ...]
    kind: FallbackKind
    from_date: date

    @property
    def days_moved(self) -> int:
        return abs((self.date - self.from_date).days)


@dataclass(frozen=True)
class ContingencyMove:
    """One fallback seen from one trigger: what happens to a match if the
    trigger's team reaches that round."""

    match_key: str
    trigger: str
    from_date: date
    to_date: date
    kind: FallbackKind
    probability: float


@dataclass
class ContingencyPlan:
    """Every fallback of a schedule, and the colliding matches left without one."""

    fallbacks: dict[str, Fallback] = field(default_factory=dict)
    # Colliding match key -> the node ids it collides with.
    colliding: dict[str, tuple[str, ...]] = field(default_factory=dict)
    probabilities: dict[str, float] = field(default_factory=dict)

    @property
    def uncovered(self) -> list[str]:
        return sorted(k for k in self.colliding if k not in self.fallbacks)

    def moves(self) -> list[ContingencyMove]:
        return [
            ContingencyMove(
                match_key=f.match_key,
                trigger=trigger,
                from_date=f.from_date,
                to_date=f.date,
                kind=f.kind,
                probability=self.probabilities.get(trigger, 0.0),
            )
            for f in sorted(self.fallbacks.values(), key=lambda f: (f.from_date, f.match_key))
            for trigger in f.trigger_node_ids
        ]

    def stats(self) -> dict[str, object]:
        days = [f.days_moved for f in self.fallbacks.values()]
        return {
            "colliding": len(self.colliding),
            "covered": len(self.fallbacks),
            "uncovered": len(self.uncovered),
            "median_days": median(days) if days else 0,
            "max_days": max(days, default=0),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self.stats(),
            "uncovered_matches": self.uncovered,
            "fallbacks": [
                {
                    "match": f.match_key,
                    "from": f.from_date.isoformat(),
                    "date": f.date.isoformat(),
                    "venue": f.venue,
                    "kickoff": f.kickoff,
                    "kind": f.kind,
                    "triggers": list(f.trigger_node_ids),
                }
                for f in sorted(self.fallbacks.values(), key=lambda f: (f.from_date, f.match_key))
            ],
            "moves": [
                {
                    "match": m.match_key,
                    "trigger": m.trigger,
                    "from": m.from_date.isoformat(),
                    "to": m.to_date.isoformat(),
                    "kind": m.kind,
                    "probability": round(m.probability, 4),
                }
                for m in self.moves()
            ],
        }


class ContingencyPlanner:
    """Plans fallbacks for a schedule, reading the season's `contingency_policy`
    and `reserve_dates`."""

    def __init__(
        self,
        world: World,
        season: Season,
        competitions: list[Competition],
        commitments: CommitmentSet,
    ) -> None:
        self.world = world
        self.season = season
        self.commitments = commitments
        self.policy = season.contingency_policy
        self._competitions = {c.id: c for c in competitions}

        self._node_team: dict[str, str] = {}
        self._node_probability: dict[str, float] = {}
        self._exclusive: dict[str, frozenset[str]] = {}
        contingent: dict[str, list[Commitment]] = {}
        for commitment in commitments:
            self._node_team[commitment.node_id] = commitment.team_id
            self._node_probability[commitment.node_id] = commitment.reached_probability
            self._exclusive[commitment.node_id] = commitment.exclusive_with
            if not commitment.hard:
                contingent.setdefault(commitment.team_id, []).append(commitment)
        self._contingent = {
            team: sorted(items, key=lambda c: c.date) for team, items in contingent.items()
        }
        self._contingent_ords = {
            team: [c.date.toordinal() for c in items] for team, items in self._contingent.items()
        }
        self._reach = max(
            (c.min_rest_days for items in self._contingent.values() for c in items), default=0
        )
        self._scenarios: dict[tuple[str, frozenset[str]], frozenset[int]] = {}
        self._hard_blocked: dict[str, frozenset[int]] = {}
        self._any_blocked: dict[str, frozenset[int]] = {}
        self._co_cache: dict[tuple[tuple[str, ...], tuple[str, ...]], bool] = {}

        self._blocked = {
            competition.id: frozenset(season.blacked_out_dates)
            | frozenset(season.scoped_blackout_dates(competition))
            for competition in competitions
        }
        self._venue_blocked = {
            venue: frozenset(dated) for venue, dated in season.venue_blacked_out_dates.items()
        }
        self._windows = {
            c.id: (c.start or season.start, c.end or season.end) for c in competitions
        }
        self.partners = _club_partners(world)
        self._reserve = sorted(season.reserve_dates, key=lambda r: r.date)
        self.reserve_capacity = {r.date: r.capacity for r in self._reserve}
        self._bracket_cap = _bracket_caps(commitments)
        self._cup_round = {
            c.node_id: (c.competition_id, c.node_id.rsplit("/", 1)[1])
            for c in commitments
            if c.source == "cup"
        }
        self._fixed = {
            (r.competition, r.home_team, r.date) for r in season.fixed_requirements if r.hard
        }
        self._full_rounds = {
            (r.competition, r.date) for r in season.full_round_requirements if r.hard
        }

    @property
    def active(self) -> bool:
        """Whether any commitment is contingent, i.e. there is anything to plan."""
        return bool(self._contingent)

    # -- public ----------------------------------------------------------

    def plan_for(self, index) -> ContingencyPlan:
        """The plan for a `ScheduleIndex`, computed once per index."""
        cached = index.contingency.get(self)
        if cached is None:
            cached = self.plan(index.matches, index)
            index.contingency[self] = cached
        return cached

    def plan(self, matches, index=None) -> ContingencyPlan:
        """Plan every colliding match, in date order so the result is deterministic."""
        if index is None:
            from .scoring.base import ScheduleIndex

            index = ScheduleIndex(matches)
        plan = ContingencyPlan()
        if not self._contingent:
            return plan
        triggers = self._collisions(index)
        if not triggers:
            return plan
        for node_id in {n for nodes in triggers.values() for ns in nodes.values() for n in ns}:
            plan.probabilities[node_id] = self._node_probability[node_id]

        state = _PlanState(index, self)
        by_key = {m.key: m for m in matches}
        order = sorted(triggers, key=lambda k: (by_key[k].date, k))
        for key in order:
            match = by_key[key]
            per_team = triggers[key]
            plan.colliding[key] = tuple(sorted(n for ns in per_team.values() for n in ns))
            fallback = self._plan_match(match, per_team, state)
            if fallback is not None:
                plan.fallbacks[key] = fallback
                state.add(fallback)
        return plan

    def probability(self, fallback: Fallback) -> float:
        """The chance some trigger of `fallback` fires: independent across
        teams, and within a team a node plus the alternatives exclusive with it."""
        by_team: dict[str, list[str]] = {}
        for node_id in fallback.trigger_node_ids:
            by_team.setdefault(self._node_team[node_id], []).append(node_id)
        none = 1.0
        for nodes in by_team.values():
            lead = max(nodes, key=lambda n: self._node_probability[n])
            p = self._node_probability[lead] + sum(
                self._node_probability[n] for n in nodes if n != lead and n in self._exclusive[lead]
            )
            none *= 1.0 - min(1.0, p)
        return 1.0 - none

    def move_cost(self, fallback: Fallback) -> float:
        return KIND_COST[fallback.kind] + DAY_COST * fallback.days_moved

    def collision_cost(self, probability: float) -> float:
        """The least a match colliding with a commitment reached at `probability`
        costs in `expected_disruption`: its cheapest allowed move, one day away.
        Solvers that cannot plan fallbacks price a colliding day with this."""
        cheapest = min(KIND_COST[kind] for kind in self.policy.fallback_kinds)
        return probability * (cheapest + DAY_COST)

    def co_triggerable(self, a: tuple[str, ...], b: tuple[str, ...]) -> bool:
        """Whether one scenario can fire a trigger of `a` and one of `b`:
        different teams are independent, one team's nodes only when neither
        excludes the other."""
        key = (a, b)
        cached = self._co_cache.get(key)
        if cached is None:
            cached = any(
                x == y
                or self._node_team[x] != self._node_team[y]
                or y not in self._exclusive[x]
                for x in a
                for y in b
            )
            self._co_cache[key] = cached
        return cached

    # -- collisions ------------------------------------------------------

    def _collisions(self, index) -> dict[str, dict[str, frozenset[str]]]:
        """Match key -> team -> contingent nodes the match is too close to."""
        found: dict[str, dict[str, set[str]]] = {}
        reach = self._reach + 1
        for team, items in self._contingent.items():
            ords = self._contingent_ords[team]
            for match in index.by_team.get(team, ()):
                day = match.date.toordinal()
                lo = bisect_left(ords, day - reach - 1)
                hi = bisect_right(ords, day + reach + 1)
                for commitment in items[lo:hi]:
                    if commitment.clashes(match.date):
                        found.setdefault(match.key, {}).setdefault(team, set()).add(
                            commitment.node_id
                        )
        return {
            key: {team: frozenset(nodes) for team, nodes in per_team.items()}
            for key, per_team in found.items()
        }

    # -- planning one match ---------------------------------------------

    def _plan_match(self, match, per_team, state: _PlanState) -> Fallback | None:
        competition = self._competitions.get(match.competition_id)
        if competition is None or self._immovable(match, competition):
            return None
        trigger_ids = tuple(sorted(n for nodes in per_team.values() for n in nodes))
        scenario = {
            team: self._scenario(team, nodes) for team, nodes in per_team.items()
        }
        # Greedy first-valid in date order: an earlier fallback may take the only
        # slot a later match needs, which then reports as uncovered.
        chosen = next(
            (
                (day, kind)
                for kind, day in self._candidates(match)
                if self._valid(match, day, kind, trigger_ids, scenario, competition, state)
            ),
            None,
        )
        if chosen is None:
            return None
        day, kind = chosen
        return Fallback(
            match_key=match.key,
            competition_id=match.competition_id,
            home_team=match.home_team,
            away_team=match.away_team,
            date=day,
            venue=match.venue,
            kickoff=getattr(match, "kickoff_time", None),
            trigger_node_ids=trigger_ids,
            kind=kind,
            from_date=match.date,
        )

    def _immovable(self, match, competition: Competition) -> bool:
        """A match pinned to its date: a final-round match must keep the shared
        final date, and a hard requirement fixes its own."""
        if competition.format == "league" and match.round_index == competition.rounds - 1:
            return True
        return (
            (match.competition_id, match.home_team, match.date) in self._fixed
            or (match.competition_id, match.date) in self._full_rounds
        )

    def _hard_days(self, team: str) -> frozenset[int]:
        cached = self._hard_blocked.get(team)
        if cached is None:
            cached = _blocked_ordinals(self.commitments.hard_for(team))
            self._hard_blocked[team] = cached
        return cached

    def _scenario(self, team: str, triggers: frozenset[str]) -> frozenset[int]:
        """The ordinals the team cannot play in the scenario where `triggers`
        fire: its hard commitments plus every contingent one that can be
        played alongside."""
        key = (team, triggers)
        cached = self._scenarios.get(key)
        if cached is None:
            alongside = tuple(
                c
                for c in self._contingent.get(team, ())
                if c.node_id in triggers
                or any(c.node_id not in self._exclusive[t] for t in triggers)
            )
            cached = self._hard_days(team) | _blocked_ordinals(alongside)
            self._scenarios[key] = cached
        return cached

    def _any_commitment_days(self, team: str) -> frozenset[int]:
        """Ordinals blocked if every contingent commitment of `team` is played:
        a side outside the trigger set is independent of it, so any of its own
        maybe-rounds can co-occur and its fallback day must clear all of them."""
        cached = self._any_blocked.get(team)
        if cached is None:
            cached = self._hard_days(team) | _blocked_ordinals(self._contingent.get(team, ()))
            self._any_blocked[team] = cached
        return cached

    def _candidates(self, match) -> Iterator[tuple[FallbackKind, date]]:
        origin = match.date
        horizon = self.policy.max_fallback_days
        for kind in self.policy.fallback_kinds:
            if kind == "same_slot":
                yield from ((kind, d) for d in _same_slot_days(origin))
            elif kind == "reserve_date":
                near = [
                    r.date for r in self._reserve if 0 < abs((r.date - origin).days) <= horizon
                ]
                near.sort(key=lambda d: (abs((d - origin).days), d))
                yield from ((kind, d) for d in near)
            else:
                span = set(_same_slot_days(origin)) | {origin}
                for offset in range(1, horizon + 1):
                    day = origin + timedelta(days=offset)
                    if (
                        day.weekday() in _WEEKEND_WEEKDAYS
                        and day not in span
                        and day not in self.season.reserve_date_set
                    ):
                        yield kind, day

    # -- validity ---------------------------------------------------------

    def _valid(
        self,
        match,
        day: date,
        kind: FallbackKind,
        trigger_ids: tuple[str, ...],
        scenario: dict[str, frozenset[int]],
        competition: Competition,
        state: _PlanState,
    ) -> bool:
        start, end = self._windows[competition.id]
        if not start <= day <= end:
            return False
        if day in self._blocked[competition.id] or day in self._venue_blocked.get(match.venue, ()):
            return False
        if not state.leg_ok(match, day):
            return False
        index = state.index
        if index.by_venue_date.get((match.venue, day)):
            return False
        ordinal = day.toordinal()
        rest = competition.min_rest_days
        for team in (match.home_team, match.away_team):
            if ordinal in scenario.get(team, self._any_commitment_days(team)):
                return False
            if not state.rest_ok(team, match.date.toordinal(), ordinal, rest, trigger_ids):
                return False
        for partner in self.partners.get(match.home_team, ()):
            if day in index.home_dates_by_team.get(partner, ()):
                return False
        if not state.fallback_ok(match, ordinal, trigger_ids, self):
            return False
        if kind == "reserve_date" and not state.reserve_ok(day, trigger_ids, self):
            return False
        return True

    def reserve_load(self, fallbacks: Iterable[Fallback]) -> int:
        """How many of `fallbacks` can land on one reserve date together.

        Cup fallbacks group by the round that triggers them: of the top-flight
        teams alive in a round, at most `_bracket_caps` can have reached it
        when the field is drawn against itself, so a round never counts more.
        """
        grouped: dict[tuple[str, str], set[str]] = {}
        loose = 0
        for fallback in fallbacks:
            rounds = sorted(
                self._cup_round[n] for n in fallback.trigger_node_ids if n in self._cup_round
            )
            if not rounds:
                loose += 1
                continue
            grouped.setdefault(rounds[0], set()).add(fallback.match_key)
        capped = sum(
            min(len(keys), self._bracket_cap.get(group, len(keys)))
            for group, keys in grouped.items()
        )
        return loose + capped


class _PlanState:
    """What a planning pass knows: the base schedule's per-team dates and the
    fallbacks chosen so far."""

    def __init__(self, index, planner: ContingencyPlanner) -> None:
        self.index = index
        self._planner = planner
        self._team_ords: dict[str, list[int]] = {}
        self._planned_team: dict[str, list[tuple[int, tuple[str, ...]]]] = {}
        self._planned_venue: dict[tuple[str, int], list[tuple[str, ...]]] = {}
        self._planned_home: dict[tuple[str, int], list[tuple[str, ...]]] = {}
        self._on_reserve: dict[date, list[Fallback]] = {}
        self._legs: dict[str, dict[int, tuple[date, date]]] = {}
        for competition_id, matches in index.by_competition.items():
            span: dict[int, tuple[date, date]] = {}
            for m in matches:
                first, last = span.get(m.leg, (m.date, m.date))
                span[m.leg] = (min(first, m.date), max(last, m.date))
            self._legs[competition_id] = span

    def _ords(self, team: str) -> list[int]:
        cached = self._team_ords.get(team)
        if cached is None:
            cached = [m.date.toordinal() for m in self.index.by_team.get(team, ())]
            self._team_ords[team] = cached
        return cached

    def leg_ok(self, match, day: date) -> bool:
        legs = self._legs.get(match.competition_id, {})
        earlier = legs.get(match.leg - 1)
        later = legs.get(match.leg + 1)
        return (earlier is None or day > earlier[1]) and (later is None or day < later[0])

    def rest_ok(
        self, team: str, origin: int, day: int, rest: int, triggers: tuple[str, ...]
    ) -> bool:
        """Enough days between `day` and the team's other base matches, and its
        fallbacks that can fire together with this one."""
        ords = self._ords(team)
        position = bisect_left(ords, day)
        for neighbour in (position - 1, position, position + 1, position - 2):
            if 0 <= neighbour < len(ords):
                other = ords[neighbour]
                if other == origin:
                    continue
                if abs(other - day) - 1 < rest:
                    return False
        for other, other_triggers in self._planned_team.get(team, ()):
            if abs(other - day) - 1 < rest and self._co(triggers, other_triggers):
                return False
        return True

    def fallback_ok(self, match, day: int, triggers, planner: ContingencyPlanner) -> bool:
        for other in self._planned_venue.get((match.venue, day), ()):
            if planner.co_triggerable(triggers, other):
                return False
        for partner in planner.partners.get(match.home_team, ()):
            for other in self._planned_home.get((partner, day), ()):
                if planner.co_triggerable(triggers, other):
                    return False
        for other in self._planned_home.get((match.home_team, day), ()):
            if planner.co_triggerable(triggers, other):
                return False
        return True

    def reserve_ok(self, day: date, triggers, planner: ContingencyPlanner) -> bool:
        placed = self._on_reserve.get(day, [])
        probe = Fallback(
            match_key="",
            competition_id="",
            home_team="",
            away_team="",
            date=day,
            venue="",
            kickoff=None,
            trigger_node_ids=triggers,
            kind="reserve_date",
            from_date=day,
        )
        return planner.reserve_load([*placed, probe]) <= planner.reserve_capacity.get(day, 0)

    def _co(self, a, b) -> bool:
        return self._planner.co_triggerable(a, b)

    def add(self, fallback: Fallback) -> None:
        ordinal = fallback.date.toordinal()
        triggers = fallback.trigger_node_ids
        for team in (fallback.home_team, fallback.away_team):
            self._planned_team.setdefault(team, []).append((ordinal, triggers))
        self._planned_venue.setdefault((fallback.venue, ordinal), []).append(triggers)
        self._planned_home.setdefault((fallback.home_team, ordinal), []).append(triggers)
        if fallback.kind == "reserve_date":
            self._on_reserve.setdefault(fallback.date, []).append(fallback)


def _blocked_ordinals(commitments: Iterable[Commitment]) -> frozenset[int]:
    """Every day ordinal too close to one of `commitments` to host a match."""
    blocked: set[int] = set()
    for c in commitments:
        centre = c.date.toordinal()
        reach = c.min_rest_days + 1
        blocked.update(range(centre - reach + 1, centre + reach))
    return frozenset(blocked)


def _same_slot_days(origin: date) -> list[date]:
    """The other days of the Fri-Mon span `origin` sits in, nearest first,
    the earlier day on a tie. A midweek date has no slot mates."""
    weekday = origin.weekday()
    if weekday not in _WEEKEND_WEEKDAYS:
        return []
    friday = origin - timedelta(days=(weekday - _FRIDAY) % 7)
    days = [friday + timedelta(days=i) for i in range(_SLOT_SPAN_DAYS)]
    return sorted((d for d in days if d != origin), key=lambda d: (abs((d - origin).days), d))


def _club_partners(world: World) -> dict[str, tuple[str, ...]]:
    partners: dict[str, tuple[str, ...]] = {}
    for club in world.dual_clubs():
        seniors = [t.id for t in club.teams if t.level == "senior"]
        for team in seniors:
            partners[team] = tuple(t for t in seniors if t != team)
    return partners


def _bracket_caps(commitments: CommitmentSet) -> dict[tuple[str, str], int]:
    """Per cup round, the most tracked teams that can be playing it at once.

    The first round's field is everyone entered. Each later round keeps all of
    the previous one, or only half when that round's advance chance (read off
    the reach probabilities) says the sides were drawn against each other,
    plus any team entering fresh.
    """
    rounds: dict[tuple[str, str], dict[str, tuple[date, float]]] = {}
    for c in commitments:
        if c.source != "cup":
            continue
        # Cup node ids are `cup/<competition>/<round>`; a `/` in a competition id breaks this.
        _, competition, round_id = c.node_id.split("/")
        rounds.setdefault((competition, round_id), {})[c.team_id] = (c.date, c.reached_probability)

    caps: dict[tuple[str, str], int] = {}
    by_competition: dict[str, list[tuple[date, str]]] = {}
    for (competition, round_id), teams in rounds.items():
        by_competition.setdefault(competition, []).append(
            (min(d for d, _ in teams.values()), round_id)
        )
    for competition, ordered in by_competition.items():
        previous: tuple[str, str] | None = None
        for _, round_id in sorted(ordered):
            teams = rounds[(competition, round_id)]
            if previous is None:
                caps[(competition, round_id)] = len(teams)
            else:
                before = rounds[previous]
                shared = [t for t in teams if t in before]
                peers = bool(shared) and all(
                    before[t][1] > 0 and teams[t][1] / before[t][1] <= _PEER_ADVANCE
                    for t in shared
                )
                alive = caps[previous]
                advancing = alive - alive // 2 if peers else alive
                entrants = len(teams) - len(shared)
                caps[(competition, round_id)] = min(len(teams), advancing + entrants)
            previous = (competition, round_id)
    return caps


__all__ = [
    "DAY_COST",
    "KIND_COST",
    "ContingencyMove",
    "ContingencyPlan",
    "ContingencyPlanner",
    "Fallback",
]
