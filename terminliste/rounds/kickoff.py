"""Kickoff-time assignment for a placed schedule.

Kickoff time is not a search variable the way date and venue are — both
solver backends only ever decide dates (see `rounds/greedy.py` and
`solvers/cpsat.py`). This module fills in `Match.kickoff_time` once a
schedule's dates are already fixed:

* The final round of every league is forced onto one date by
  `resolve_round_pins` (`rounds/greedy.py`); here it also gets one shared
  kickoff time (`Competition.final_round_kickoff_time`), which is what lets
  `FinalRoundSameSlot` (`scoring/hard.py`) actually pass.
* Any match a hard `FixedRequirement` names an explicit `kickoff_time` for
  (Tromsø's Midnight Sun Match) gets that time.
* Every other match gets one of `Competition.kickoff_slots`, chosen
  deterministically per fixture (a stable hash of its key) so a schedule's
  kickoffs do not change from one otherwise-identical run to the next.
"""

from __future__ import annotations

import zlib

from ..model.schema import Competition, FixedRequirement, Match


def final_round_index(competition: Competition) -> int:
    """0-based index of a league's last round — its last leg's last round."""
    return competition.rounds - 1


def _slot_for(match: Match, slots: list[str]) -> str:
    digest = zlib.crc32(match.key.encode("utf-8"))
    return slots[digest % len(slots)]


def assign_kickoff_times(
    matches: list[Match],
    competitions: list[Competition],
    requirements: list[FixedRequirement] | None = None,
) -> list[Match]:
    """Return `matches` with `kickoff_time` filled in.

    Non-destructive: returns a new list, leaving the input untouched.
    """
    by_competition = {c.id: c for c in competitions}
    explicit: dict[tuple[str, str, "object"], str] = {
        (r.competition, r.home_team, r.date): r.kickoff_time
        for r in requirements or []
        if r.kickoff_time is not None
    }

    result: list[Match] = []
    for m in matches:
        competition = by_competition.get(m.competition_id)
        kickoff = explicit.get((m.competition_id, m.home_team, m.date))
        if kickoff is None and competition is not None:
            if m.round_index == final_round_index(competition):
                kickoff = competition.final_round_kickoff_time
            elif competition.kickoff_slots:
                kickoff = _slot_for(m, competition.kickoff_slots)
        result.append(m if kickoff == m.kickoff_time else m.model_copy(update={"kickoff_time": kickoff}))
    return result


__all__ = ["assign_kickoff_times", "final_round_index"]
