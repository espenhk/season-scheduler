"""Picks one slot per league round from the season frame, avoiding cup and UEFA clashes (#6)."""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from .frame import SeasonFrame
from .model.calendar import SeasonCalendar, calendars_by_competition, competition_window
from .model.loader import World
from .model.schema import WEEKDAYS, Competition, Season
from .model.slots import RoundSlot, candidate_slots, max_compatible_slots, slots_compatible

_CLASH_COST = 1000.0
_PIN_COST = 1e9
_UNCOVERED_REQUIREMENT_COST = 50.0
_OFF_WEEKDAY_COST = 3.0
_EXTRA_SLOT_COST = 2.0
_DISCOURAGED_COST = 3.0
# Every team clashing with a contingent commitment needs its own fallback date,
# so a slot hitting more than a few at once gets quadratically worse (#9).
_CONTINGENT_CROWDING_COST = 0.5
_CONTINGENT_CROWD_FREE = 3
# A re-planned round leaving the slot it had in the schedule being revised (#13).
_MOVED_SLOT_COST = 20.0
_MOVED_SLOT_WEEK_COST = 2.0


class SlotPlanningError(ValueError):
    """A league has fewer usable round slots than rounds."""


@dataclass(frozen=True)
class SlotFreeze:
    """What a re-plan keeps of one league's existing round slots (#13).

    `pinned` rounds keep the slot they have; every other round gets a slot of
    `earliest` or later, as near its `previous` slot as the frame allows. Both
    are keyed by round index. Rounds keep their order in time, so a pinned
    round is still the n-th round once the others are re-placed.
    """

    earliest: date
    pinned: dict[int, RoundSlot]
    previous: dict[int, RoundSlot]

    def order(self) -> list[int]:
        """Every round index, earliest slot first."""
        slots = {**self.previous, **self.pinned}
        return sorted(slots, key=lambda r: (slots[r].start, r))


@dataclass
class SlotPlan:
    """One league's candidate slots and the one chosen for each round."""

    competition_id: str
    rounds: int
    candidates: list[RoundSlot]
    chosen: list[RoundSlot]
    warnings: list[str] = field(default_factory=list)
    min_gap_days: int = 1

    @property
    def anchors(self) -> dict[int, date]:
        return {round_index: slot.default_day for round_index, slot in enumerate(self.chosen)}

    def slot_of(self, round_index: int) -> RoundSlot:
        return self.chosen[round_index]

    @property
    def clashing_rounds(self) -> dict[int, tuple[str, ...]]:
        return {i: s.clashing_teams for i, s in enumerate(self.chosen) if s.clashing_teams}

    @property
    def overlapping(self) -> int:
        """Holiday and midweek candidates sitting inside a weekend candidate."""
        weekends = [s for s in self.candidates if s.kind == "weekend"]
        return sum(
            1
            for s in self.candidates
            if s.kind != "weekend" and any(w.start <= s.start <= w.end for w in weekends)
        )

    @property
    def combinations(self) -> int:
        """Ways to choose `rounds` mutually compatible slots, in date order."""
        slots = self.candidates
        ways = [1] * len(slots)
        for _ in range(self.rounds - 1):
            ways = [
                sum(
                    ways[i]
                    for i in range(j)
                    if slots_compatible(slots[i], slots[j], self.min_gap_days)
                )
                for j in range(len(slots))
            ]
        return sum(ways)


def plan_slots(
    world: World,
    season: Season,
    competitions: list[Competition],
    calendar: SeasonCalendar,
    frame: SeasonFrame | None = None,
    freezes: dict[str, SlotFreeze] | None = None,
) -> dict[str, SlotPlan]:
    """A `SlotPlan` per league. Raises `SlotPlanningError` for the first
    league with too few slots. A league in `freezes` is re-planned around the
    slots it already has (#13)."""
    frame = frame or SeasonFrame()
    by_competition = calendars_by_competition(calendar, competitions)
    freezes = freezes or {}
    return {
        competition.id: plan_league_slots(
            world,
            season,
            competition,
            by_competition[competition.id],
            frame,
            freezes.get(competition.id),
        )
        for competition in competitions
        if competition.format == "league"
    }


def plan_league_slots(
    world: World,
    season: Season,
    competition: Competition,
    calendar: SeasonCalendar,
    frame: SeasonFrame,
    freeze: SlotFreeze | None = None,
) -> SlotPlan:
    rounds = competition.rounds
    pins = frozenset(
        r.date
        for r in season.full_round_requirements
        if r.hard
        and r.competition == competition.id
        and (freeze is None or r.date >= freeze.earliest)
    )
    candidates = candidate_slots(
        season,
        competition,
        calendar,
        skip_dates=frame.cup_weekend_dates(gender=competition.gender),
        pinned_days=pins,
    )
    candidates = [_with_clashes(s, competition, frame, pins) for s in candidates]
    if freeze is not None:
        return _plan_frozen_league(world, season, competition, candidates, pins, freeze, calendar)

    if len(candidates) < rounds:
        raise SlotPlanningError(
            f"{competition.id}: {len(candidates)} round slots for {rounds} rounds — "
            f"too few free weekends and declared holiday/midweek slots; widen the "
            f"window, add `extra_round_slots`, or reduce rounds"
        )
    compatible = max_compatible_slots(candidates, competition.min_gap_days)
    if compatible < rounds:
        raise SlotPlanningError(
            f"{competition.id}: {len(candidates)} round slots, but only {compatible} can "
            f"be used together (overlap or closer than {competition.min_gap_days} days) "
            f"for {rounds} rounds"
        )

    window_start, window_end = competition_window(season, competition)
    requirements = sorted(
        r.date
        for r in season.fixed_requirements
        if r.hard
        and r.competition == competition.id
        and window_start <= r.date <= window_end
    )
    chosen = _choose(candidates, competition, season, calendar, pins, requirements)
    if chosen is None:
        raise SlotPlanningError(
            f"{competition.id}: no way to place {rounds} rounds in the {len(candidates)} "
            f"slots while covering the full-round requirement dates"
        )

    protected = pins | {
        r.date for r in season.fixed_requirements if r.hard and r.competition == competition.id
    }
    chosen = _trim_unusable_days(chosen, competition, protected)

    plan = SlotPlan(
        competition.id, rounds, candidates, chosen, min_gap_days=competition.min_gap_days
    )
    for round_index, slot in plan.clashing_rounds.items():
        teams = ", ".join(world.team_label(t) for t in slot)
        plan.warnings.append(
            f"{competition.id}: round {round_index + 1} on {plan.chosen[round_index].default_day} "
            f"clashes with a hard cup or UEFA commitment for {teams}; no clear slot was available"
        )
    return plan


def _plan_frozen_league(
    world: World,
    season: Season,
    competition: Competition,
    candidates: list[RoundSlot],
    pins: frozenset[date],
    freeze: SlotFreeze,
    calendar: SeasonCalendar,
) -> SlotPlan:
    """Re-plan one league keeping `freeze.pinned` rounds where they are.

    The n-th round in time stays the n-th round, so a pinned slot is forced
    into the position its round already had and the others fill the rest, each
    pulled towards the slot it held before (#13).
    """
    order = freeze.order()
    if len(order) != competition.rounds:
        raise SlotPlanningError(
            f"{competition.id}: the schedule being revised has {len(order)} rounds, "
            f"the competition {competition.rounds}"
        )
    position = {round_index: i for i, round_index in enumerate(order, start=1)}
    usable = [t for s in candidates if (t := _from_day(s, freeze.earliest)) is not None]
    pinned = [freeze.pinned[r] for r in order if r in freeze.pinned]
    slots = sorted([*usable, *pinned], key=lambda s: (s.default_day, s.start, s.kind))
    forced = {
        next(i for i, slot in enumerate(slots) if slot is freeze.pinned[r]): position[r]
        for r in freeze.pinned
    }
    stay = {position[r]: slot for r, slot in freeze.previous.items() if r not in freeze.pinned}

    window_start, window_end = competition_window(season, competition)
    requirements = sorted(
        r.date
        for r in season.fixed_requirements
        if r.hard
        and r.competition == competition.id
        and max(window_start, freeze.earliest) <= r.date <= window_end
    )
    picked = _choose(
        slots, competition, season, calendar, pins, requirements, forced=forced, stay=stay
    )
    if picked is None:
        raise SlotPlanningError(
            f"{competition.id}: no way to place the {competition.rounds - len(freeze.pinned)} "
            f"open rounds in the slots after {freeze.earliest} around the frozen ones"
        )
    protected = set(pins) | {d for slot in pinned for d in slot.days} | set(requirements)
    picked = _trim_unusable_days(picked, competition, protected)

    chosen: list[RoundSlot | None] = [None] * competition.rounds
    for round_index in order:
        chosen[round_index] = picked[position[round_index] - 1]
    plan = SlotPlan(
        competition.id, competition.rounds, slots, chosen, min_gap_days=competition.min_gap_days
    )
    for round_index, slot in plan.clashing_rounds.items():
        teams = ", ".join(world.team_label(t) for t in slot)
        plan.warnings.append(
            f"{competition.id}: round {round_index + 1} on {plan.chosen[round_index].default_day} "
            f"clashes with a hard cup or UEFA commitment for {teams}; no clear slot was available"
        )
    return plan


def _from_day(slot: RoundSlot, earliest: date) -> RoundSlot | None:
    """The part of `slot` on or after `earliest`, or None if nothing is left."""
    days = tuple(d for d in slot.days if d >= earliest)
    if not days:
        return None
    if len(days) == len(slot.days):
        return slot
    default = slot.default_day if slot.default_day in days else days[0]
    return replace(slot, days=days, start=days[0], default_day=default)


def _with_clashes(
    slot: RoundSlot, competition: Competition, frame: SeasonFrame, pins: frozenset[date]
) -> RoundSlot:
    """The slot with its default day moved to the day fewest teams clash on,
    nearest the preferred weekday among equals. A pinned day stays put."""
    dates = {team_id: _hard_dates(frame, team_id) for team_id in competition.teams}
    contingent = {
        team_id: frame.commitments.contingent_for(team_id)
        for team_id in competition.teams
        if frame.commitments.contingent_for(team_id)
    }

    def contingent_clashes(day: date) -> tuple[str, ...]:
        return tuple(
            sorted(
                team_id
                for team_id, items in contingent.items()
                if any(c.clashes(day) for c in items)
            )
        )

    def clashing(day: date) -> tuple[str, ...]:
        return tuple(
            team_id
            for team_id, hard in dates.items()
            if any(abs((day - d).days) - 1 < min_rest for d, min_rest in hard)
        )

    preferred = WEEKDAYS.index(competition.preferred_weekday)
    pinned = [d for d in slot.days if d in pins]
    options = pinned or list(slot.days)
    day = min(
        options, key=lambda d: (len(clashing(d)), abs(d.weekday() - preferred), d)
    )
    return replace(
        slot,
        default_day=day,
        clashing_teams=clashing(day),
        contingent_teams=contingent_clashes(day),
    )


def _trim_unusable_days(
    slots: list[RoundSlot], competition: Competition, protected: frozenset[date] | set[date]
) -> list[RoundSlot]:
    """Drop days that leave a team too little rest before or after the
    neighbouring round, whatever day that round uses.

    Every team plays every round, so a Monday followed two days later by a
    midweek round breaks `min_rest_days` for the whole league, not for one
    club. Protected days (pinned dates) stay.
    """
    rest_gap = timedelta(days=competition.min_rest_days + 1)
    days = [list(s.days) for s in slots]

    def has_rest(day: date, i: int) -> bool:
        before = i == 0 or any(day - e >= rest_gap for e in days[i - 1])
        after = i + 1 == len(days) or any(e - day >= rest_gap for e in days[i + 1])
        return before and after

    changed = True
    while changed:
        changed = False
        for i, current in enumerate(days):
            kept = [d for d in current if d in protected or has_rest(d, i)]
            if kept and len(kept) != len(current):
                days[i] = kept
                changed = True
    result = []
    for slot, kept in zip(slots, days, strict=True):
        if len(kept) == len(slot.days):
            result.append(slot)
            continue
        default = slot.default_day if slot.default_day in kept else min(
            kept, key=lambda d: (abs((d - slot.default_day).days), d)
        )
        result.append(replace(slot, days=tuple(kept), default_day=default))
    return result


def _hard_dates(frame: SeasonFrame, team_id: str) -> list[tuple[date, int]]:
    return [(c.date, c.min_rest_days) for c in frame.commitments.hard_for(team_id)]


def _slot_cost(slot: RoundSlot, competition: Competition, calendar: SeasonCalendar) -> float:
    cost = _CLASH_COST * len(slot.clashing_teams)
    crowd = max(0, len(slot.contingent_teams) - _CONTINGENT_CROWD_FREE)
    cost += _CONTINGENT_CROWDING_COST * crowd**2
    if slot.weekday != competition.preferred_weekday:
        cost += _OFF_WEEKDAY_COST
    if slot.kind != "weekend":
        cost += _EXTRA_SLOT_COST
    if calendar.is_discouraged(slot.default_day):
        cost += _DISCOURAGED_COST
    return cost


def _positions(slots: list[RoundSlot]) -> list[float]:
    """Each slot's place on a time axis counting only weeks that can host a
    round, so a long break (the World Cup) is not read as uneven spacing."""
    weekends = [s.default_day for s in slots if s.kind == "weekend"]
    if len(weekends) < 2:
        return [s.default_day.toordinal() / 7 for s in slots]
    ordinals = [d.toordinal() for d in weekends]
    positions = []
    for slot in slots:
        day = slot.default_day.toordinal()
        upper = bisect_right(ordinals, day)
        if upper == 0:
            positions.append((day - ordinals[0]) / 7)
        elif upper == len(ordinals):
            positions.append(len(ordinals) - 1 + (day - ordinals[-1]) / 7)
        else:
            lo, hi = ordinals[upper - 1], ordinals[upper]
            positions.append(upper - 1 + (day - lo) / (hi - lo))
    return positions


def _choose(
    slots: list[RoundSlot],
    competition: Competition,
    season: Season,
    calendar: SeasonCalendar,
    pins: frozenset[date],
    requirements: list[date],
    forced: dict[int, int] | None = None,
    stay: dict[int, RoundSlot] | None = None,
) -> list[RoundSlot] | None:
    """The cheapest `competition.rounds` compatible slots, in date order.

    `forced` maps a slot index to the 1-based position it must take, and that
    position is then closed to every other slot; `stay` maps a position to the
    slot it held before a re-plan, which other slots pay to replace (#13).
    """
    forced = forced or {}
    stay = stay or {}
    reserved = set(forced.values())
    n, k = len(slots), competition.rounds
    positions = _positions(slots)
    weekend_positions = [p for p, s in zip(positions, slots, strict=True) if s.kind == "weekend"]
    low, high = min(weekend_positions, default=0.0), max(weekend_positions, default=0.0)
    ideal = (high - low) / (k - 1) if k > 1 else 0.0
    gap_days = competition.min_gap_days

    def uncovered(after: date | None, before: date | None) -> float:
        cost = 0.0
        for day in sorted(pins | set(requirements)):
            if (after is None or day > after) and (before is None or day < before):
                cost += _PIN_COST if day in pins else _UNCOVERED_REQUIREMENT_COST
        return cost

    node = [_slot_cost(s, competition, calendar) for s in slots]

    def allowed(c: int, j: int) -> bool:
        return forced.get(j) == c if j in forced else c not in reserved

    def moved(c: int, j: int) -> float:
        before = stay.get(c)
        if before is None or slots[j].end >= before.start and before.end >= slots[j].start:
            return 0.0
        weeks = abs((slots[j].default_day - before.default_day).days) / 7
        return _MOVED_SLOT_COST + _MOVED_SLOT_WEEK_COST * weeks

    inf = math.inf
    # best[c][j]: cheapest plan whose c-th round (1-based) is slot j.
    best = [[inf] * n for _ in range(k + 1)]
    back = [[-1] * n for _ in range(k + 1)]
    for j in range(n):
        if allowed(1, j):
            best[1][j] = (
                node[j] + moved(1, j) + (positions[j] - low) ** 2 + uncovered(None, slots[j].start)
            )
    for c in range(2, k + 1):
        for j in range(n):
            if not allowed(c, j):
                continue
            for i in range(j):
                if best[c - 1][i] == inf or not slots_compatible(slots[i], slots[j], gap_days):
                    continue
                cost = (
                    best[c - 1][i]
                    + node[j]
                    + moved(c, j)
                    + (positions[j] - positions[i] - ideal) ** 2
                    + uncovered(slots[i].end, slots[j].start)
                )
                if cost < best[c][j]:
                    best[c][j], back[c][j] = cost, i

    final, last = inf, -1
    for j in range(n):
        if best[k][j] == inf:
            continue
        cost = best[k][j] + (high - positions[j]) ** 2 + uncovered(slots[j].end, None)
        if cost < final:
            final, last = cost, j
    if last < 0 or final >= _PIN_COST:
        return None

    picked: list[int] = []
    j = last
    for c in range(k, 0, -1):
        picked.append(j)
        j = back[c][j]
    return [slots[j] for j in reversed(picked)]


def search_space_log10(
    plan: SlotPlan,
    competition: Competition,
    calendar: SeasonCalendar,
    within_slot: bool = False,
) -> float:
    """log10 of the per-match date combinations once rounds sit in slots.

    By default each match is free within `match_window_days` of its round's
    default day, as the solvers search today; `within_slot` counts only the
    days of the slot itself.
    """
    total = 0.0
    matches_per_round = len(competition.teams) // 2
    for slot in plan.chosen:
        if within_slot:
            choices = len(slot.days)
        else:
            choices = len(calendar.window(slot.default_day, competition.match_window_days))
        total += matches_per_round * math.log10(max(1, choices))
    return total


def slot_table_lines(plan: SlotPlan, team_label=lambda team_id: team_id) -> list[str]:
    """The chosen slots as aligned text rows, one per round."""
    lines = []
    for round_index, slot in enumerate(plan.chosen, start=1):
        span = (
            f"{slot.start:%d %b}-{slot.end:%d %b}" if slot.start != slot.end else f"{slot.start:%d %b}"
        )
        clash = (
            "  clashes: " + ", ".join(team_label(t) for t in slot.clashing_teams)
            if slot.clashing_teams
            else ""
        )
        lines.append(
            f"R{round_index:<3}{slot.kind:<8}{span:<14}default {slot.default_day:%a %d %b}{clash}"
        )
    return lines


__all__ = [
    "SlotPlan",
    "SlotPlanningError",
    "plan_league_slots",
    "plan_slots",
    "search_space_log10",
    "slot_table_lines",
]
