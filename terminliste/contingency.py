"""Fallback dates for matches a later cup or UEFA round may displace (#9).

A league match is *exposed* when a commitment the team only maybe reaches (a
contingent one, see `rounds/commitments.py`) sits too close to it. Each exposed
match gets one pre-designated `Fallback`, valid in the scenario where its
trigger rounds are reached. The planner is deterministic, so every solver and
rule sees the same plan for the same schedule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

from .model.loader import World
from .model.schema import Competition, FallbackKind, Match, Season
from .rounds.commitments import CommitmentSet

if TYPE_CHECKING:
    from .scoring.base import ScheduleIndex

# Soft cost of a move by kind, plus `_DAY_COST` per day moved; the order mirrors
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


def _weekend_anchor(day: date) -> date | None:
    """The Friday starting `day`'s Fri-Mon weekend, None for Tue-Thu."""
    offset = (day.weekday() - _WEEKEND_SPAN) % 7
    return day - timedelta(days=offset) if offset < _WEEKEND_SPAN else None


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

        for team_id in (match.home_team, match.away_team):
            minimum = self._min_rest.get(team_id, 0)
            for other in index.by_team.get(team_id, ()):
                if other.key != match.key and abs((other.date - day).days) - 1 < minimum:
                    return f"too close to {other.key} for {team_id}"
            if self.commitments.blocked(team_id, day):
                return f"too close to a hard commitment of {team_id}"

        for node_id in triggers:
            team_id = self._node_team[node_id]
            excluded = self._node_exclusive[node_id]
            for commitment in self._contingent.get(team_id, ()):
                if commitment.node_id in excluded:
                    continue
                if commitment.clashes(day):
                    return f"too close to {commitment.label} of {team_id}, which comes with {node_id}"

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
        if not self._co_triggerable(a[1].trigger_node_ids, b[1].trigger_node_ids):
            return None
        (match_a, fb_a), (match_b, fb_b) = a, b
        for team_id in {match_a.home_team, match_a.away_team} & {match_b.home_team, match_b.away_team}:
            if abs((fb_a.date - fb_b.date).days) - 1 < self._min_rest.get(team_id, 0):
                return f"{team_id} plays both with too little rest"
        if fb_a.date == fb_b.date:
            if fb_a.venue == fb_b.venue:
                return "same venue the same day"
            if match_b.home_team in self._siblings.get(match_a.home_team, ()):
                return "a club's teams both at home the same day"
        return None

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

    def _candidates(self, match: Match, kind: FallbackKind) -> list[date]:
        base = match.date
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

    def _leg_bounds(self, index: ScheduleIndex) -> dict[str, dict[int, tuple[date, date]]]:
        bounds: dict[str, dict[int, tuple[date, date]]] = {}
        for competition_id, matches in index.by_competition.items():
            legs: dict[int, tuple[date, date]] = {}
            for m in matches:
                first, last = legs.get(m.leg, (m.date, m.date))
                legs[m.leg] = (min(first, m.date), max(last, m.date))
            bounds[competition_id] = legs
        return bounds

    def exposed(self, index: ScheduleIndex) -> list[tuple[Match, tuple[str, ...]]]:
        found = [(m, t) for m in index.matches if (t := self.triggers_of(m))]
        found.sort(key=lambda item: (item[0].date, item[0].key))
        return found

    def plan(
        self, index: ScheduleIndex, exposed: list[tuple[Match, tuple[str, ...]]] | None = None
    ) -> ContingencyPlan:
        leg_bounds = self._leg_bounds(index)
        if exposed is None:
            exposed = self.exposed(index)

        plan = ContingencyPlan()
        chosen: dict[str, tuple[Match, Fallback]] = {}
        on_reserve: dict[date, list[Fallback]] = {}
        for match, triggers in exposed:
            fallback = None
            if self._movable(match):
                fallback = self._pick(match, triggers, index, leg_bounds, chosen, on_reserve)
            if fallback is None:
                plan.uncovered.append(match.key)
                continue
            plan.fallbacks[match.key] = fallback
            chosen[match.key] = (match, fallback)
            if fallback.kind == "reserve_date":
                on_reserve.setdefault(fallback.date, []).append(fallback)
        return plan

    def _pick(self, match, triggers, index, leg_bounds, chosen, on_reserve) -> Fallback | None:
        for kind in self.policy.fallback_kinds:
            for day in self._candidates(match, kind):
                if self.invalid_reason(match, day, triggers, index, leg_bounds) is not None:
                    continue
                fallback = Fallback(day, match.venue, match.kickoff_time, triggers, kind)
                if kind == "reserve_date" and (
                    self._reserve_load([*on_reserve.get(day, ()), fallback])
                    > self._reserve_capacity[day]
                ):
                    continue
                if any(self._clash((match, fallback), other) for other in chosen.values()):
                    continue
                return fallback
        return None

    # -- checking -------------------------------------------------------------

    def check(self, index: ScheduleIndex, plan: ContingencyPlan) -> ContingencyAnalysis:
        """Re-verify `plan` against the schedule, independently of how it was made."""
        analysis = ContingencyAnalysis(plan)
        leg_bounds = self._leg_bounds(index)
        by_key = {m.key: m for m in index.matches}
        entries: list[tuple[Match, Fallback]] = []
        for key, fallback in sorted(plan.fallbacks.items()):
            match = by_key.get(key)
            if match is None:
                analysis.invalid.append((key, "no such match"))
                continue
            reason = self.invalid_reason(match, fallback.date, fallback.trigger_node_ids, index, leg_bounds)
            if reason is not None:
                analysis.invalid.append((key, reason))
            entries.append((match, fallback))

        for i, a in enumerate(entries):
            for b in entries[i + 1 :]:
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
        analysis = self.check(index, self.plan(index, exposed))
        analysis.triggers = {m.key: t for m, t in exposed}
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
    "plan_for",
]
