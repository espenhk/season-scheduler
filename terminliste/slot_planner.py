"""Picks one slot per league round from the season frame, avoiding cup and UEFA clashes (#6)."""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from .display import short_date
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


class SlotPlanningError(ValueError):
    """A league has fewer usable round slots than rounds."""


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
) -> dict[str, SlotPlan]:
    """A `SlotPlan` per league. Raises `SlotPlanningError` for the first
    league with too few slots."""
    frame = frame or SeasonFrame()
    by_competition = calendars_by_competition(calendar, competitions)
    return {
        competition.id: plan_league_slots(
            world, season, competition, by_competition[competition.id], frame
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
) -> SlotPlan:
    rounds = competition.rounds
    fixed_rounds = {f.round - 1: f.date for f in competition.fixed_round_dates}
    pins = frozenset(
        {
            r.date
            for r in season.full_round_requirements
            if r.hard and r.competition == competition.id
        }
        | set(fixed_rounds.values())
    )
    playoff_dates = frame.playoff_dates(competition.id) | {
        day for p in competition.playoff_rounds if p.reserves_league for day in p.dates
    }
    clashing_pins = sorted(pins & playoff_dates)
    if clashing_pins:
        raise SlotPlanningError(
            f"{competition.id}: pinned round date {clashing_pins[0]} is a play-off reservation"
        )
    candidates = candidate_slots(
        season,
        competition,
        calendar,
        skip_dates=frame.cup_weekend_dates(gender=competition.gender) | playoff_dates,
        pinned_days=pins,
    )
    candidates = [
        _with_clashes(s, competition, frame, pins)
        for s in candidates
        # A pinned day hosts a fixed or final-style round, which the quota exempts.
        if _fits_quota(competition, s) or any(d in pins for d in s.days)
    ]

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
    required_slots = _fixed_round_slots(candidates, competition, fixed_rounds)
    chosen = _choose(
        candidates, competition, season, calendar, pins, requirements, required_slots
    )
    if chosen is None:
        raise SlotPlanningError(
            f"{competition.id}: no way to place {rounds} rounds in the {len(candidates)} "
            f"slots while covering the full-round requirement dates"
        )

    protected = pins | {
        r.date for r in season.fixed_requirements if r.hard and r.competition == competition.id
    }
    chosen = _trim_unusable_days(chosen, competition, protected)

    exempt = competition.day_quota_exempt_rounds
    if not all(
        _fits_quota(competition, slot) for i, slot in enumerate(chosen) if i not in exempt
    ):
        raise SlotPlanningError(
            f"{competition.id}: trimming days for rest left a round slot that cannot hold "
            f"its matches under day_quota"
        )

    plan = SlotPlan(
        competition.id, rounds, candidates, chosen, min_gap_days=competition.min_gap_days
    )
    for round_index, slot in plan.clashing_rounds.items():
        teams = ", ".join(world.team_label(t) for t in slot)
        plan.warnings.append(
            f"{competition.name}: round {round_index + 1} on "
            f"{short_date(plan.chosen[round_index].default_day, season.year)} clashes with a "
            f"hard cup or UEFA commitment for {teams}; no clear slot was available"
        )
    return plan


def _fixed_round_slots(
    candidates: list[RoundSlot], competition: Competition, fixed_rounds: dict[int, date]
) -> dict[int, int]:
    """The candidate slot index each fixed round must take."""
    required: dict[int, int] = {}
    for round_index, day in sorted(fixed_rounds.items()):
        if round_index >= competition.rounds:
            raise SlotPlanningError(
                f"{competition.id}: round {round_index + 1} is fixed on {day}, but the "
                f"league has {competition.rounds} rounds"
            )
        slot_index = next(
            (i for i, slot in enumerate(candidates) if day in slot.days), None
        )
        if slot_index is None:
            raise SlotPlanningError(
                f"{competition.id}: round {round_index + 1} is fixed on {day}, which is in "
                f"no usable round slot (blackout, cup weekend, play-off or outside the window)"
            )
        required[round_index] = slot_index
    return required


def _fits_quota(competition: Competition, slot: RoundSlot) -> bool:
    """Whether a round's matches fit the quota over the slot's weekdays; a
    weekday without a quota is unbounded."""
    if not competition.day_quota:
        return True
    limits = [competition.day_quota.get(WEEKDAYS[day.weekday()]) for day in slot.days]
    return None in limits or sum(limits) >= competition.team_count // 2


def _with_clashes(
    slot: RoundSlot, competition: Competition, frame: SeasonFrame, pins: frozenset[date]
) -> RoundSlot:
    """The slot with its default day moved to the day fewest teams clash on,
    nearest the preferred weekday among equals. A pinned day stays put."""
    dates = {team_id: _hard_dates(frame, team_id) for team_id in competition.teams}

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
    return replace(slot, default_day=day, clashing_teams=clashing(day))


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
    required_slots: dict[int, int],
) -> list[RoundSlot] | None:
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
    inf = math.inf
    # best[c][j]: cheapest plan whose c-th round (1-based) is slot j.
    best = [[inf] * n for _ in range(k + 1)]
    back = [[-1] * n for _ in range(k + 1)]

    def allowed(c: int, j: int) -> bool:
        return required_slots.get(c - 1, j) == j

    for j in range(n):
        if not allowed(1, j):
            continue
        best[1][j] = (
            node[j] + (positions[j] - low) ** 2 + uncovered(None, slots[j].start)
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
) -> float:
    """log10 of the per-match date combinations: each match picks one day of
    its round's slot."""
    total = 0.0
    matches_per_round = len(competition.teams) // 2
    for slot in plan.chosen:
        total += matches_per_round * math.log10(max(1, len(slot.days)))
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
