"""Typed round slots: the places a league's rounds can live (#6)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from .calendar import SeasonCalendar
from .schema import WEEKDAYS, Competition, Season

SlotKind = Literal["weekend", "midweek", "holiday"]

_FRIDAY = 4
_WEEKEND_SPAN_DAYS = 4  # Fri, Sat, Sun, Mon


@dataclass(frozen=True)
class RoundSlot:
    """One place a league round can be played.

    `days` are the allowed dates inside `start`..`end`; `default_day` is where
    the round's matches sit unless a clash moves one. `clashing_teams` are the
    teams with a hard cup or UEFA commitment too close to `default_day`.
    """

    competition_id: str
    kind: SlotKind
    start: date
    end: date
    days: tuple[date, ...]
    default_day: date
    label: str = ""
    clashing_teams: tuple[str, ...] = ()
    # Teams whose contingent commitments are too close to `default_day` (#9).
    contingent_teams: tuple[str, ...] = ()

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    @property
    def weekday(self) -> str:
        return WEEKDAYS[self.default_day.weekday()]


def candidate_slots(
    season: Season,
    competition: Competition,
    calendar: SeasonCalendar,
    skip_dates: frozenset[date] = frozenset(),
    pinned_days: frozenset[date] = frozenset(),
) -> list[RoundSlot]:
    """Every slot `competition` could use, in date order.

    `calendar` is the competition's own (see `calendars_by_competition`).
    A slot whose span holds any of `skip_dates` is dropped: those are cup
    weekends the league round must not share. A slot holding a pinned day
    takes it as its default day.
    """
    slots: list[RoundSlot] = []
    for friday in _fridays(calendar):
        span = [friday + timedelta(days=i) for i in range(_WEEKEND_SPAN_DAYS)]
        days = tuple(d for d in span if calendar.is_allowed(d))
        if not days or any(d in skip_dates for d in span):
            continue
        slots.append(
            RoundSlot(
                competition_id=competition.id,
                kind="weekend",
                start=span[0],
                end=span[-1],
                days=days,
                default_day=_default_day(days, competition.preferred_weekday, pinned_days),
            )
        )

    for extra in season.extra_round_slots:
        if extra.competitions and competition.id not in extra.competitions:
            continue
        if not calendar.is_allowed(extra.date) or extra.date in skip_dates:
            continue
        slots.append(
            RoundSlot(
                competition_id=competition.id,
                kind=extra.kind,
                start=extra.date,
                end=extra.date,
                days=(extra.date,),
                default_day=extra.date,
                label=extra.reason,
            )
        )
    return sorted(slots, key=lambda s: (s.default_day, s.start, s.kind))


def _fridays(calendar: SeasonCalendar) -> list[date]:
    if not calendar.all_dates:
        return []
    first, last = calendar.all_dates[0], calendar.all_dates[-1]
    friday = first - timedelta(days=(first.weekday() - _FRIDAY) % 7)
    if friday + timedelta(days=_WEEKEND_SPAN_DAYS - 1) < first:
        friday += timedelta(days=7)
    fridays: list[date] = []
    while friday <= last:
        fridays.append(friday)
        friday += timedelta(days=7)
    return fridays


def _default_day(days: tuple[date, ...], preferred_weekday: str, pinned: frozenset[date]) -> date:
    for day in days:
        if day in pinned:
            return day
    preferred = WEEKDAYS.index(preferred_weekday)
    # Nearest weekday to the preferred one, earlier day on a tie.
    return min(days, key=lambda d: (abs(d.weekday() - preferred), d))


def slots_compatible(earlier: RoundSlot, later: RoundSlot, min_gap_days: int) -> bool:
    """Whether one league can play a round in each: the spans do not overlap
    and the default days are at least `min_gap_days` apart."""
    return later.start > earlier.end and (later.default_day - earlier.default_day).days >= min_gap_days


def max_compatible_slots(slots: list[RoundSlot], min_gap_days: int) -> int:
    """The most rounds `slots` can host together, for the capacity check."""
    longest = [1] * len(slots)
    for j in range(len(slots)):
        for i in range(j):
            if slots_compatible(slots[i], slots[j], min_gap_days):
                longest[j] = max(longest[j], longest[i] + 1)
    return max(longest, default=0)
