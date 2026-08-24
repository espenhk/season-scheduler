"""Kickoff-time assignment for a placed schedule.

Kickoff time is not searched — both solver backends only decide dates — so
this fills in `Match.kickoff_time` once a schedule's dates are fixed:

* Every league's final round gets one shared time
  (`Competition.final_round_kickoff_time`), which is what lets
  `FinalRoundSameSlot` pass.
* A match named by a hard `FixedRequirement` with an explicit `kickoff_time`
  (Tromsø's Midnight Sun Match) gets that time.
* A competition opted into `Competition.tv_time_spread` (#76) gets its
  preferred-day matches shaped for TV — see `_tv_time_spread_assignments`.
* Everything else gets one of `Competition.kickoff_slots`, picked by a stable
  hash of the fixture key so kickoffs don't move between identical runs.
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
    """Early/primary/late kickoff times per round, for every competition that
    opts into `Competition.tv_time_spread`.

    Covers only matches on the competition's preferred weekday; off-day and
    final-round matches fall back to `kickoff_slots`. Which match gets shifted
    early or late comes from a stable hash of its key, the same determinism
    `_slot_for` gives ordinary slots.
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
