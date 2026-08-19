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
* A competition opted into `Competition.tv_time_spread` (issue #76) gets its
  round's matches on the preferred weekday shaped for TV: most at the
  primary time, one shifted early and one shifted late — see
  `_tv_time_spread_assignments` below.
* Every other match gets one of `Competition.kickoff_slots`, chosen
  deterministically per fixture (a stable hash of its key) so a schedule's
  kickoffs do not change from one otherwise-identical run to the next.
"""

from __future__ import annotations

import zlib

from ..model.schema import WEEKDAYS, Competition, FixedRequirement, Match


def final_round_index(competition: Competition) -> int:
    """0-based index of a league's last round — its last leg's last round."""
    return competition.rounds - 1


def _slot_for(match: Match, slots: list[str]) -> str:
    digest = zlib.crc32(match.key.encode("utf-8"))
    return slots[digest % len(slots)]


def _tv_time_spread_assignments(
    matches: list[Match], competitions: list[Competition]
) -> dict[str, str]:
    """Early/primary/late kickoff times for every competition that opts into
    `Competition.tv_time_spread`, grouped by round and restricted to matches
    landing on that competition's preferred weekday — off-day matches, and
    every match of a final round (already forced onto its own shared slot),
    are left for the caller to fall back to `kickoff_slots`.

    Which match in a round gets shifted early or late is chosen by a stable
    hash of its key, the same determinism `_slot_for` gives ordinary slots.
    A round with only one matching match gets the primary time; zero means
    nothing to assign.
    """
    spread_by_competition = {
        c.id: c for c in competitions if c.tv_time_spread is not None
    }
    if not spread_by_competition:
        return {}

    rounds: dict[tuple[str, int], list[Match]] = {}
    for m in matches:
        competition = spread_by_competition.get(m.competition_id)
        if competition is None:
            continue
        if m.round_index == final_round_index(competition):
            continue
        if m.date.weekday() != WEEKDAYS.index(competition.preferred_weekday):
            continue
        rounds.setdefault((m.competition_id, m.round_index), []).append(m)

    assigned: dict[str, str] = {}
    for (competition_id, _round_index), round_matches in rounds.items():
        spread = spread_by_competition[competition_id].tv_time_spread
        ordered = sorted(round_matches, key=lambda m: zlib.crc32(m.key.encode("utf-8")))
        if len(ordered) >= 2:
            assigned[ordered[0].key] = spread.early_kickoff_time
            assigned[ordered[1].key] = spread.late_kickoff_time
            for m in ordered[2:]:
                assigned[m.key] = spread.primary_kickoff_time
        elif ordered:
            assigned[ordered[0].key] = spread.primary_kickoff_time
    return assigned


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
    tv_spread = _tv_time_spread_assignments(matches, competitions)

    result: list[Match] = []
    for m in matches:
        competition = by_competition.get(m.competition_id)
        kickoff = explicit.get((m.competition_id, m.home_team, m.date))
        if kickoff is None and competition is not None:
            if m.round_index == final_round_index(competition):
                kickoff = competition.final_round_kickoff_time
            elif m.key in tv_spread:
                kickoff = tv_spread[m.key]
            elif competition.kickoff_slots:
                kickoff = _slot_for(m, competition.kickoff_slots)
        result.append(m if kickoff == m.kickoff_time else m.model_copy(update={"kickoff_time": kickoff}))
    return result


__all__ = ["assign_kickoff_times", "final_round_index"]
