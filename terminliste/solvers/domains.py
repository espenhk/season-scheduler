"""Which days a fixture may statically take (#11).

A day passes when it clears the blackouts and both teams' hard commitments
plus rest. None of that depends on the rest of the schedule, so it is looked
up rather than recomputed per proposed move, and a fixture with no such day
inside its round's slot is a data or planning problem worth reporting before
any search runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from ..model.calendar import SeasonCalendar
from ..model.slots import RoundSlot
from ..rounds.commitments import CommitmentSet


class DayDomains:
    def __init__(
        self, calendars: dict[str, SeasonCalendar], commitments: CommitmentSet
    ) -> None:
        self._calendars = calendars
        self._commitments = commitments
        self._allowed: dict[tuple[str, str, str, str, date], bool] = {}
        # Keyed by slot identity: slots are shared, long-lived plan objects.
        self._days: dict[tuple[int, str, str, str], tuple[date, ...]] = {}
        self._slots: dict[int, RoundSlot] = {}

    def allows(self, home: str, away: str, venue: str, competition_id: str, day: date) -> bool:
        """Whether `day` clears the blackouts and the hard commitments of both
        teams."""
        key = (competition_id, home, away, venue, day)
        cached = self._allowed.get(key)
        if cached is None:
            commitments = self._commitments
            cached = self._calendars[competition_id].is_allowed(day, venue) and not (
                commitments.blocked(home, day) or commitments.blocked(away, day)
            )
            self._allowed[key] = cached
        return cached

    def days(self, slot: RoundSlot, home: str, away: str, venue: str) -> tuple[date, ...]:
        """The slot's days this fixture may statically be played on."""
        key = (id(slot), home, away, venue)
        cached = self._days.get(key)
        if cached is None:
            self._slots[id(slot)] = slot  # keeps the id from being reused
            cached = tuple(
                day
                for day in slot.days
                if self.allows(home, away, venue, slot.competition_id, day)
            )
            self._days[key] = cached
        return cached

    def empty_fixtures(self, matches: Sequence) -> list:
        """Matches with no admissible day in their round's slot."""
        return [
            m
            for m in matches
            if m.slot is not None and not self.days(m.slot, m.home_team, m.away_team, m.venue)
        ]
