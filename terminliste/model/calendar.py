"""The candidate calendar: which dates a match may legally land on.

Blackouts are resolved once, up front, into a set of allowed dates per venue.
Constraints and solvers then ask cheap membership questions instead of
re-deriving the same answer for every candidate placement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .loader import World
from .schema import WEEKDAYS, Competition, Season


@dataclass(frozen=True)
class SeasonCalendar:
    """Allowed dates for a season, globally and per venue."""

    season: Season
    all_dates: tuple[date, ...]
    global_allowed: frozenset[date]
    discouraged: frozenset[date]
    _venue_blocked: dict[str, frozenset[date]]

    def is_allowed(self, day: date, venue_id: str | None = None) -> bool:
        if day not in self.global_allowed:
            return False
        if venue_id is not None and day in self._venue_blocked.get(venue_id, frozenset()):
            return False
        return True

    def is_discouraged(self, day: date) -> bool:
        return day in self.discouraged

    def blocked_dates_for(self, venue_id: str) -> frozenset[date]:
        return self._venue_blocked.get(venue_id, frozenset())

    def dates_on_weekday(self, weekday: str) -> tuple[date, ...]:
        """Every allowed date falling on the named weekday, in order."""
        index = WEEKDAYS.index(weekday)
        return tuple(d for d in self.all_dates if d.weekday() == index and d in self.global_allowed)

    def window(self, anchor: date, days: int, venue_id: str | None = None) -> list[date]:
        """Allowed dates within `days` either side of `anchor`, nearest first.

        Nearest-first ordering matters: it lets a greedy placer try to stay on
        the round's anchor date and only drift when it has to, which keeps
        rounds visually coherent in the report.
        """
        candidates: list[date] = []
        for offset in _outward_offsets(days):
            day = anchor + timedelta(days=offset)
            if self.is_allowed(day, venue_id) and self.season.start <= day <= self.season.end:
                candidates.append(day)
        return candidates

    def narrowed(self, start: date, end: date) -> "SeasonCalendar":
        """A calendar restricted to a tighter [start, end] window inside the
        season, for a competition whose fixture window is shorter.

        Every date-selection method works off `all_dates`/`global_allowed`/
        `discouraged` rather than re-deriving bounds from `season`, so
        narrowing those three is enough.
        """
        return SeasonCalendar(
            season=self.season,
            all_dates=tuple(d for d in self.all_dates if start <= d <= end),
            global_allowed=frozenset(d for d in self.global_allowed if start <= d <= end),
            discouraged=frozenset(d for d in self.discouraged if start <= d <= end),
            _venue_blocked=self._venue_blocked,
        )


def competition_window(season: Season, competition: Competition) -> tuple[date, date]:
    """A competition's effective start/end: its own if set, else the season's."""
    return (competition.start or season.start, competition.end or season.end)


def calendars_by_competition(
    calendar: SeasonCalendar, competitions: list[Competition]
) -> dict[str, SeasonCalendar]:
    """Per-competition calendar, narrowed to each one's own window if it has
    one; competitions without `start`/`end` share the season calendar."""
    result: dict[str, SeasonCalendar] = {}
    for competition in competitions:
        start, end = competition_window(calendar.season, competition)
        if start == calendar.season.start and end == calendar.season.end:
            result[competition.id] = calendar
        else:
            result[competition.id] = calendar.narrowed(start, end)
    return result


def _outward_offsets(days: int) -> list[int]:
    """[0, -1, 1, -2, 2, ...] up to +/- days — nearest to the anchor first."""
    offsets = [0]
    for step in range(1, days + 1):
        offsets.extend((-step, step))
    return offsets


def build_calendar(world: World, season: Season) -> SeasonCalendar:
    all_dates = _date_range(season.start, season.end)
    blackouts = set(season.blacked_out_dates)
    # Belt and braces: the loader already rejects a hard requirement whose
    # date is blacked out.
    required = {r.date for r in season.fixed_requirements if r.hard} | {
        r.date for r in season.full_round_requirements if r.hard
    }

    global_allowed = frozenset(d for d in all_dates if d not in blackouts or d in required)

    venue_blocked = {
        venue_id: frozenset(dated) for venue_id, dated in season.venue_blacked_out_dates.items()
    }

    return SeasonCalendar(
        season=season,
        all_dates=tuple(all_dates),
        global_allowed=global_allowed,
        discouraged=frozenset(d.date for d in season.discouraged_dates),
        _venue_blocked=venue_blocked,
    )


def _date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def anchor_dates(
    calendar: SeasonCalendar,
    preferred_weekday: str,
    count: int,
    min_gap_days: int,
) -> list[date]:
    """Pick `count` round anchors, preferring `preferred_weekday`.

    Spread evenly rather than taken greedily from the front: thirty rounds
    against thirty-five Sundays should use the whole calendar with the odd
    free weekend, not run out in November and leave a month idle.

    Too few preferred-weekday dates and the shortfall goes to midweek anchors
    in the widest remaining gaps, which shows up honestly in the weekday score
    rather than as an unexplained failure.
    """
    if count <= 0:
        return []

    preferred = list(calendar.dates_on_weekday(preferred_weekday))

    if len(preferred) >= count:
        return _evenly_spaced(preferred, count)

    chosen = list(preferred)
    while len(chosen) < count:
        inserted = _insert_into_widest_gap(chosen, calendar, min_gap_days)
        if inserted is None:
            break
        chosen.append(inserted)
        chosen.sort()

    return chosen[:count]


def _evenly_spaced(days: list[date], count: int) -> list[date]:
    """`count` entries drawn from `days`, spread across its full range."""
    if count == 1:
        return [days[0]]
    last = len(days) - 1
    return [days[round(i * last / (count - 1))] for i in range(count)]


def _insert_into_widest_gap(
    chosen: list[date], calendar: SeasonCalendar, min_gap_days: int
) -> date | None:
    if not chosen:
        for day in calendar.all_dates:
            if calendar.is_allowed(day):
                return day
        return None

    best_day: date | None = None
    best_gap = 2 * min_gap_days - 1
    for earlier, later in zip(chosen, chosen[1:]):
        gap = (later - earlier).days
        if gap <= best_gap:
            continue
        midpoint = earlier + timedelta(days=gap // 2)
        for candidate in calendar.window(midpoint, gap // 2):
            if (candidate - earlier).days >= min_gap_days and (
                later - candidate
            ).days >= min_gap_days:
                best_day, best_gap = candidate, gap
                break
    return best_day
