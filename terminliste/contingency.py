"""Fallback dates for matches a later cup or UEFA round may displace (#9).

A league match is *exposed* when a commitment the team only maybe reaches (a
contingent one, see `rounds/commitments.py`) sits too close to it. Each exposed
match gets one pre-designated `Fallback`, valid in the scenario where its
trigger rounds are reached. The planner is deterministic, so every solver and
rule sees the same plan for the same schedule.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from .model.loader import World
from .model.schema import Competition, FallbackKind, Match, Season
from .rounds.commitments import CommitmentSet

if TYPE_CHECKING:
    from .scoring.base import ScheduleIndex

# Soft cost of a move by kind, plus `DAY_COST` per day moved; the order mirrors
# the preference order in the policy.
KIND_COST: dict[str, float] = {"same_slot": 1.0, "reserve_date": 2.0, "next_free_slot": 3.0}
DAY_COST = 0.25

# Friday to Monday: the span a league round occupies.
_WEEKEND_SPAN = 4


@dataclass(frozen=True)
class Fallback:
    date: date
    venue: str
    kickoff_time: str | None
    trigger_node_ids: tuple[str, ...]
    kind: FallbackKind


@dataclass(frozen=True)
class ContingencyMove:
    """What happens to one match when one trigger round is reached."""

    match_key: str
    trigger: str
    from_date: date
    to_date: date
    kind: FallbackKind


@dataclass
class ContingencyPlan:
    """Every exposed match's fallback, keyed by match key; `uncovered` lists
    the exposed matches no valid fallback was found for."""

    fallbacks: dict[str, Fallback] = field(default_factory=dict)
    uncovered: list[str] = field(default_factory=list)

    def moves(self, matches: list[Match]) -> list[ContingencyMove]:
        by_key = {m.key: m for m in matches}
        return [
            ContingencyMove(key, trigger, by_key[key].date, fallback.date, fallback.kind)
            for key, fallback in self.fallbacks.items()
            if key in by_key
            for trigger in fallback.trigger_node_ids
        ]

    def to_dict(self, matches: list[Match]) -> dict[str, Any]:
        return {
            "fallbacks": [
                {
                    "match": move.match_key,
                    "trigger": move.trigger,
                    "from": move.from_date.isoformat(),
                    "to": move.to_date.isoformat(),
                    "kind": move.kind,
                    "venue": self.fallbacks[move.match_key].venue,
                    "kickoff_time": self.fallbacks[move.match_key].kickoff_time,
                }
                for move in self.moves(matches)
            ],
            "uncovered": list(self.uncovered),
        }


@dataclass
class ContingencyAnalysis:
    """A plan plus what is wrong with it: fallbacks that are not valid, and
    pairs of fallbacks that can both be needed and clash."""

    plan: ContingencyPlan
    # Trigger nodes of every exposed match, keyed by match key.
    triggers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    invalid: list[tuple[str, str]] = field(default_factory=list)
    collisions: list[tuple[str, str, str]] = field(default_factory=list)
    # The exposed matches with their triggers, in planning order.
    exposed: list[tuple[Match, tuple[str, ...]]] = field(default_factory=list)


@dataclass(frozen=True)
class PlanFootprint:
    """What planning reads of the schedule besides the exposed matches
    themselves: a change outside it cannot alter the plan (#11).

    `team_days[team]` are dates on which another match of `team` could make a
    candidate fallback date invalid for lack of rest; `venue_days` and
    `home_days` the (venue, day) and (sibling team, day) cells a candidate date
    is checked against; `leg_edges` the (competition, leg, "start"|"end")
    extremes the leg-order check reads; `keys` the exposed matches' keys, which
    the plan looks matches up by.
    """

    team_days: dict[str, frozenset[date]]
    venue_days: frozenset[tuple[str, date]]
    home_days: frozenset[tuple[str, date]]
    leg_edges: frozenset[tuple[str, int, str]]
    keys: frozenset[str]

    @classmethod
    def union(cls, parts: list[PlanFootprint]) -> PlanFootprint:
        team_days: dict[str, set[date]] = {}
        for part in parts:
            for team, days in part.team_days.items():
                team_days.setdefault(team, set()).update(days)
        return cls(
            team_days={team: frozenset(days) for team, days in team_days.items()},
            venue_days=frozenset().union(*(p.venue_days for p in parts)),
            home_days=frozenset().union(*(p.home_days for p in parts)),
            leg_edges=frozenset().union(*(p.leg_edges for p in parts)),
            keys=frozenset().union(*(p.keys for p in parts)),
        )


def _date_of(match: Match) -> date:
    return match.date


def _weekend_anchor(day: date) -> date | None:
    """The Friday starting `day`'s Fri-Mon weekend, None for Tue-Thu."""
    offset = (day.weekday() - _WEEKEND_SPAN) % 7
    return day - timedelta(days=offset) if offset < _WEEKEND_SPAN else None


class _Chosen:
    """The fallbacks picked so far, indexed by team and by date: two fallbacks
    can only clash through a shared team or a shared date, so a candidate is
    only tested against those (#11)."""

    def __init__(self) -> None:
        self.entries: dict[str, tuple[Match, Fallback]] = {}
        self._by_team: dict[str, list[str]] = {}
        self._by_day: dict[date, list[str]] = {}

    def add(self, match: Match, fallback: Fallback) -> None:
        key = match.key
        self.entries[key] = (match, fallback)
        for team_id in {match.home_team, match.away_team}:
            self._by_team.setdefault(team_id, []).append(key)
        self._by_day.setdefault(fallback.date, []).append(key)

    def near(self, match: Match, fallback: Fallback):
        """Entries sharing a team or the fallback date with the candidate."""
        seen: set[str] = set()
        for keys in (
            self._by_team.get(match.home_team, ()),
            self._by_team.get(match.away_team, ()),
            self._by_day.get(fallback.date, ()),
        ):
            for key in keys:
                if key not in seen:
                    seen.add(key)
                    yield self.entries[key]


class ReasonCache:
    """`invalid_reason` results by (match, day), reused while nothing the match's
    footprint covers has changed (#11).

    `committed` holds the results valid for the schedule as it stands. A match
    in `stale` was touched by the proposal being scored, so its results go to
    `scratch` instead and only replace the committed ones if the proposal is
    kept; the rest are valid either way and are shared.
    """

    def __init__(
        self,
        committed: dict[int, dict[date, str | None]],
        stale: frozenset[int] = frozenset(),
    ) -> None:
        self.committed = committed
        self.stale = stale
        self.scratch: dict[int, dict[date, str | None]] = {}

    def of(self, match: Match) -> dict[date, str | None]:
        key = id(match)
        target = self.scratch if key in self.stale else self.committed
        reasons = target.get(key)
        if reasons is None:
            reasons = target[key] = {}
        return reasons


class ContingencyPlanner:
    def __init__(
        self,
        world: World,
        season: Season,
        competitions: list[Competition],
        commitments: CommitmentSet,
    ) -> None:
        self.world = world
        self.season = season
        self.policy = season.contingency_policy
        self.commitments = commitments
        self._competitions = {c.id: c for c in competitions}
        self._reserve_capacity = {r.date: r.capacity for r in season.reserve_dates}
        self._scoped_blackouts = {
            c.id: season.scoped_blackout_dates(c) for c in competitions
        }

        self._min_rest: dict[str, int] = {}
        for competition in competitions:
            for team_id in competition.teams:
                self._min_rest[team_id] = max(
                    self._min_rest.get(team_id, 0), competition.min_rest_days
                )

        self._siblings: dict[str, tuple[str, ...]] = {}
        for club in world.dual_clubs():
            seniors = [t.id for t in club.teams if t.level == "senior"]
            for team_id in seniors:
                self._siblings[team_id] = tuple(t for t in seniors if t != team_id)

        self._contingent = {
            team_id: tuple(c for c in commitments.contingent_for(team_id) if c.reached_probability > 0)
            for team_id in commitments.team_ids
        }
        # (ordinal, rest days, node) per team: `triggers_of` is the hot loop.
        self._contingent_flat = {
            team_id: tuple((c.date.toordinal(), c.min_rest_days, c.node_id) for c in items)
            for team_id, items in self._contingent.items()
        }
        # What `invalid_reason` scans per trigger: (ordinal, rest days, node,
        # label), so the loop needs no method call per commitment.
        self._clash_flat = {
            team_id: tuple((c.date.toordinal(), c.min_rest_days, c.node_id, c.label) for c in items)
            for team_id, items in self._contingent.items()
        }
        # Every day too close to a hard commitment, so `blocked` is one lookup.
        self._hard_blocked = {
            team_id: frozenset(
                c.date + timedelta(days=offset)
                for c in commitments.hard_for(team_id)
                for offset in range(-c.min_rest_days, c.min_rest_days + 1)
            )
            for team_id in commitments.team_ids
        }
        self._candidate_days: dict[tuple[date, str], tuple[date, ...]] = {}
        self._node_team: dict[str, str] = {}
        self._node_exclusive: dict[str, frozenset[str]] = {}
        self._node_probability: dict[str, float] = {}
        self._node_group: dict[str, tuple[str, int]] = {}
        teams_in_group: dict[str, set[str]] = {}
        for items in self._contingent.values():
            for c in items:
                self._node_team[c.node_id] = c.team_id
                self._node_exclusive[c.node_id] = c.exclusive_with
                self._node_probability[c.node_id] = c.reached_probability
                if c.source == "cup":
                    teams_in_group.setdefault(c.node_id.split("/", 1)[1], set()).add(c.team_id)
        # Days (as ordinals) too close to a commitment that comes with each
        # node, so a clear day is one lookup and only a clash scans for its label.
        self._node_forbidden = {
            node_id: frozenset(
                day
                for ordinal, rest, other_node, _ in self._clash_flat.get(team_id, ())
                if other_node not in self._node_exclusive[node_id]
                for day in range(ordinal - rest, ordinal + rest + 1)
            )
            for node_id, team_id in self._node_team.items()
        }
        # Top-flight sides can meet in a cup round, so at most half of those
        # alive in it get through together: a reserve date is sized for that.
        self._bracket_cap = {
            group: max(1, math.ceil(len(teams) / 2)) for group, teams in teams_in_group.items()
        }

        self._pinned = {
            (r.date, r.home_team, r.competition) for r in season.fixed_requirements if r.hard
        }
        self._full_round_dates = {
            (r.date, r.competition) for r in season.full_round_requirements if r.hard
        }

    # -- exposure -------------------------------------------------------------

    def triggers_of(self, match: Match) -> tuple[str, ...]:
        """Outcome-tree nodes whose commitments sit too close to `match`."""
        day = match.date.toordinal()
        nodes: set[str] = set()
        for team_id in (match.home_team, match.away_team):
            for ordinal, rest, node_id in self._contingent_flat.get(team_id, ()):
                if abs(day - ordinal) - 1 < rest:
                    nodes.add(node_id)
        return tuple(sorted(nodes))

    def disruption_probability(self, triggers: tuple[str, ...]) -> float:
        """Chance at least one trigger is reached, treating them as independent."""
        stay = 1.0
        for node_id in triggers:
            stay *= 1.0 - self._node_probability[node_id]
        return 1.0 - stay

    def _movable(self, match: Match) -> bool:
        competition = self._competitions.get(match.competition_id)
        if competition is None:
            return False
        if competition.format == "league" and match.round_index == competition.rounds - 1:
            return False
        if (match.date, match.home_team, match.competition_id) in self._pinned:
            return False
        return (match.date, match.competition_id) not in self._full_round_dates

    # -- validity -------------------------------------------------------------

    def invalid_reason(
        self,
        match: Match,
        day: date,
        triggers: tuple[str, ...],
        index: ScheduleIndex,
        leg_bounds: dict[str, dict[int, tuple[date, date]]] | None = None,
    ) -> str | None:
        """Why `match` cannot be played on `day` once `triggers` are reached,
        or None when it can."""
        competition = self._competitions[match.competition_id]
        season = self.season
        if day == match.date:
            return "same date as the base fixture"
        if not (competition.start or season.start) <= day <= (competition.end or season.end):
            return "outside the competition window"
        if day in season.blacked_out_dates or day in self._scoped_blackouts[match.competition_id]:
            return "blackout date"
        if day in season.venue_blacked_out_dates.get(match.venue, {}):
            return "venue blackout"

        key = match.key
        for team_id in (match.home_team, match.away_team):
            minimum = self._min_rest.get(team_id, 0)
            dated = index.by_team.get(team_id, ())
            # Sorted by date, so only the window around `day` can be too close.
            start = bisect_left(dated, day - timedelta(days=minimum), key=_date_of)
            for other in dated[start:]:
                if (other.date - day).days > minimum:
                    break
                if other.key != key:
                    return f"too close to {other.key} for {team_id}"
            if day in self._hard_blocked.get(team_id, ()):
                return f"too close to a hard commitment of {team_id}"

        ordinal = day.toordinal()
        for node_id in triggers:
            if ordinal not in self._node_forbidden[node_id]:
                continue
            team_id = self._node_team[node_id]
            excluded = self._node_exclusive[node_id]
            for other_ordinal, rest, other_node, label in self._clash_flat.get(team_id, ()):
                if other_node not in excluded and abs(ordinal - other_ordinal) - 1 < rest:
                    return f"too close to {label} of {team_id}, which comes with {node_id}"

        if any(m.key != match.key for m in index.by_venue_date.get((match.venue, day), ())):
            return "venue already booked"
        for sibling in self._siblings.get(match.home_team, ()):
            if day in index.home_dates_by_team.get(sibling, ()):
                return f"{sibling} is at home the same day"

        if leg_bounds is not None:
            bounds = leg_bounds.get(match.competition_id, {})
            previous = bounds.get(match.leg - 1)
            if previous is not None and day <= previous[1]:
                return "before the previous leg ends"
            following = bounds.get(match.leg + 1)
            if following is not None and day >= following[0]:
                return "after the next leg starts"
        return None

    def _clash(self, a: tuple[Match, Fallback], b: tuple[Match, Fallback]) -> str | None:
        """Why two fallbacks cannot both be played, or None. Fallbacks whose
        triggers are mutually exclusive never both happen, so never clash."""
        (match_a, fb_a), (match_b, fb_b) = a, b
        reason = None
        for team_id in {match_a.home_team, match_a.away_team} & {match_b.home_team, match_b.away_team}:
            if abs((fb_a.date - fb_b.date).days) - 1 < self._min_rest.get(team_id, 0):
                reason = f"{team_id} plays both with too little rest"
                break
        if reason is None and fb_a.date == fb_b.date:
            if fb_a.venue == fb_b.venue:
                reason = "same venue the same day"
            elif match_b.home_team in self._siblings.get(match_a.home_team, ()):
                reason = "a club's teams both at home the same day"
        # The triggers test is the dear one, so it only runs for a real clash.
        if reason is None or not self._co_triggerable(a[1].trigger_node_ids, b[1].trigger_node_ids):
            return None
        return reason

    def _co_triggerable(self, left: tuple[str, ...], right: tuple[str, ...]) -> bool:
        return any(
            not (
                self._node_team[a] == self._node_team[b]
                and a != b
                and b in self._node_exclusive[a]
            )
            for a in left
            for b in right
        )

    def _reserve_load(self, entries: list[Fallback]) -> int:
        """Matches one reserve date may have to host at once: those whose
        triggers are one cup round count at most the bracket cap."""
        per_group: dict[str, int] = {}
        load = 0
        for fallback in entries:
            group = fallback.trigger_node_ids[0].split("/", 1)[1]
            if group in self._bracket_cap:
                per_group[group] = per_group.get(group, 0) + 1
            else:
                load += 1
        return load + sum(min(n, self._bracket_cap[g]) for g, n in per_group.items())

    # -- planning -------------------------------------------------------------

    def _candidates(self, match: Match, kind: FallbackKind) -> tuple[date, ...]:
        """Where a fallback of `kind` could go, nearest first; a function of the
        match's date alone, so remembered."""
        key = (match.date, kind)
        found = self._candidate_days.get(key)
        if found is None:
            found = self._candidate_days[key] = tuple(self._candidates_for(match.date, kind))
        return found

    def _candidates_for(self, base: date, kind: FallbackKind) -> list[date]:
        horizon = base + timedelta(days=self.policy.max_fallback_days)
        if kind == "same_slot":
            anchor = _weekend_anchor(base)
            if anchor is None:
                return []
            days = [
                d
                for d in (base + timedelta(days=n) for n in range(-3, 4))
                if d != base and _weekend_anchor(d) == anchor
            ]
            return sorted(days, key=lambda d: (abs((d - base).days), d < base))
        if kind == "reserve_date":
            return sorted(d for d in self._reserve_capacity if base < d <= horizon)
        days = (base + timedelta(days=n) for n in range(1, self.policy.max_fallback_days + 1))
        return [d for d in days if d.weekday() in (5, 6) and d not in self._reserve_capacity]

    def footprint(self, exposed: list[tuple[Match, tuple[str, ...]]]) -> PlanFootprint:
        """The `PlanFootprint` of planning and checking `exposed`. A superset of
        what `invalid_reason` reads on any candidate date, since the plan stops
        at the first valid one."""
        team_days: dict[str, set[date]] = {}
        venue_days: set[tuple[str, date]] = set()
        home_days: set[tuple[str, date]] = set()
        leg_edges: set[tuple[str, int, str]] = set()
        for match, _triggers in exposed:
            candidates: set[date] = set()
            for kind in self.policy.fallback_kinds:
                candidates.update(self._candidates(match, kind))
            for team_id in (match.home_team, match.away_team):
                reach = self._min_rest.get(team_id, 0)
                near = team_days.setdefault(team_id, set())
                for day in candidates:
                    near.update(day + timedelta(days=n) for n in range(-reach, reach + 1))
            venue_days.update((match.venue, day) for day in candidates)
            for sibling in self._siblings.get(match.home_team, ()):
                home_days.update((sibling, day) for day in candidates)
            leg_edges.add((match.competition_id, match.leg - 1, "end"))
            leg_edges.add((match.competition_id, match.leg + 1, "start"))
        return PlanFootprint(
            team_days={team: frozenset(days) for team, days in team_days.items()},
            venue_days=frozenset(venue_days),
            home_days=frozenset(home_days),
            leg_edges=frozenset(leg_edges),
            keys=frozenset(match.key for match, _ in exposed),
        )

    def leg_bounds_of(self, index: ScheduleIndex) -> dict[str, dict[int, tuple[date, date]]]:
        by_leg: dict[str, dict[int, list[date]]] = {}
        for competition_id, matches in index.by_competition.items():
            legs = by_leg.setdefault(competition_id, {})
            for m in matches:
                legs.setdefault(m.leg, []).append(m.date)
        return {
            competition_id: {leg: (min(days), max(days)) for leg, days in legs.items()}
            for competition_id, legs in by_leg.items()
        }

    def exposed(self, index: ScheduleIndex) -> list[tuple[Match, tuple[str, ...]]]:
        found = [(m, t) for m in index.matches if (t := self.triggers_of(m))]
        found.sort(key=lambda item: (item[0].date, item[0].key))
        return found

    def plan(
        self,
        index: ScheduleIndex,
        exposed: list[tuple[Match, tuple[str, ...]]] | None = None,
        leg_bounds: dict[str, dict[int, tuple[date, date]]] | None = None,
        cache: ReasonCache | None = None,
    ) -> ContingencyPlan:
        if leg_bounds is None:
            leg_bounds = self.leg_bounds_of(index)
        if exposed is None:
            exposed = self.exposed(index)

        plan = ContingencyPlan()
        chosen = _Chosen()
        on_reserve: dict[date, list[Fallback]] = {}
        for match, triggers in exposed:
            fallback = None
            if self._movable(match):
                fallback = self._pick(match, triggers, index, leg_bounds, chosen, on_reserve, cache)
            if fallback is None:
                plan.uncovered.append(match.key)
                continue
            plan.fallbacks[match.key] = fallback
            chosen.add(match, fallback)
            if fallback.kind == "reserve_date":
                on_reserve.setdefault(fallback.date, []).append(fallback)
        return plan

    def _reason(
        self, match, day, triggers, index, leg_bounds, cache: ReasonCache | None
    ) -> str | None:
        if cache is None:
            return self.invalid_reason(match, day, triggers, index, leg_bounds)
        reasons = cache.of(match)
        if day in reasons:
            return reasons[day]
        reason = reasons[day] = self.invalid_reason(match, day, triggers, index, leg_bounds)
        return reason

    def _pick(
        self, match, triggers, index, leg_bounds, chosen, on_reserve, cache=None
    ) -> Fallback | None:
        for kind in self.policy.fallback_kinds:
            for day in self._candidates(match, kind):
                if self._reason(match, day, triggers, index, leg_bounds, cache) is not None:
                    continue
                fallback = Fallback(day, match.venue, match.kickoff_time, triggers, kind)
                if kind == "reserve_date" and (
                    self._reserve_load([*on_reserve.get(day, ()), fallback])
                    > self._reserve_capacity[day]
                ):
                    continue
                if any(self._clash((match, fallback), other) for other in chosen.near(match, fallback)):
                    continue
                return fallback
        return None

    # -- checking -------------------------------------------------------------

    def check(
        self,
        index: ScheduleIndex,
        plan: ContingencyPlan,
        leg_bounds: dict[str, dict[int, tuple[date, date]]] | None = None,
        cache: ReasonCache | None = None,
    ) -> ContingencyAnalysis:
        """Re-verify `plan` against the schedule, independently of how it was made."""
        analysis = ContingencyAnalysis(plan)
        if leg_bounds is None:
            leg_bounds = self.leg_bounds_of(index)
        by_key = {m.key: m for m in index.matches}
        entries: list[tuple[Match, Fallback]] = []
        for key, fallback in sorted(plan.fallbacks.items()):
            match = by_key.get(key)
            if match is None:
                analysis.invalid.append((key, "no such match"))
                continue
            reason = self._reason(
                match, fallback.date, fallback.trigger_node_ids, index, leg_bounds, cache
            )
            if reason is not None:
                analysis.invalid.append((key, reason))
            entries.append((match, fallback))

        # Only entries sharing a team or a fallback date can clash.
        buckets: list[list[int]] = []
        by_team: dict[str, list[int]] = {}
        by_day: dict[date, list[int]] = {}
        for n, (match, fallback) in enumerate(entries):
            for team_id in {match.home_team, match.away_team}:
                by_team.setdefault(team_id, []).append(n)
            by_day.setdefault(fallback.date, []).append(n)
        buckets.extend(by_team.values())
        buckets.extend(by_day.values())
        pairs = {
            (bucket[x], bucket[y])
            for bucket in buckets
            for x in range(len(bucket))
            for y in range(x + 1, len(bucket))
        }
        for i, j in sorted(pairs):
            a, b = entries[i], entries[j]
            reason = self._clash(a, b)
            if reason is not None:
                analysis.collisions.append((a[0].key, b[0].key, reason))

        reserve: dict[date, list[Fallback]] = {}
        for _, fallback in entries:
            if fallback.date in self._reserve_capacity:
                reserve.setdefault(fallback.date, []).append(fallback)
        for day, items in reserve.items():
            if self._reserve_load(items) > self._reserve_capacity[day]:
                keys = sorted(k for k, f in plan.fallbacks.items() if f.date == day)
                analysis.invalid.append((keys[-1], f"reserve date {day} is over capacity"))
        return analysis

    def analyse(self, index: ScheduleIndex) -> ContingencyAnalysis:
        """The plan for `index`, checked; computed once per schedule index."""
        cached = index.contingency
        if cached is not None and cached[0] is self:
            return cached[1]
        exposed = self.exposed(index)
        leg_bounds = self.leg_bounds_of(index)
        analysis = self.check(index, self.plan(index, exposed, leg_bounds), leg_bounds)
        analysis.triggers = {m.key: t for m, t in exposed}
        analysis.exposed = exposed
        index.contingency = (self, analysis)
        return analysis

    def move_cost(self, match: Match, fallback: Fallback | None) -> float:
        """The cost of the move; a match with no fallback is priced as the
        worst kind at the full horizon."""
        if fallback is None:
            return max(KIND_COST.values()) + DAY_COST * self.policy.max_fallback_days
        return KIND_COST[fallback.kind] + DAY_COST * abs((fallback.date - match.date).days)


def plan_for(matches: list[Match], constraints: list[Any]) -> ContingencyPlan | None:
    """The contingency plan of `matches` under the planner the constraint list
    carries, None when the season has no contingent commitments."""
    from .scoring.base import ScheduleIndex

    planner = next((p for p in (getattr(c, "planner", None) for c in constraints) if p), None)
    if planner is None:
        return None
    return planner.analyse(ScheduleIndex(matches)).plan


__all__ = [
    "ContingencyAnalysis",
    "ContingencyMove",
    "ContingencyPlan",
    "ContingencyPlanner",
    "Fallback",
    "PlanFootprint",
    "ReasonCache",
    "plan_for",
]
