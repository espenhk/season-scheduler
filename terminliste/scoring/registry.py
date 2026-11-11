"""Assembles the constraint list for a season.

One place decides which rules are in play. Both solver backends and the report
consume the same list, so a constraint added here is immediately live
everywhere — the point of declaring rules as objects rather than writing them
into a solver.
"""

from __future__ import annotations

from ..model.loader import World
from ..model.schema import Competition, Season
from ..rounds.commitments import CommitmentSet
from .base import Constraint
from .hard import (
    BlackoutDates,
    ClubHomeClash,
    CommitmentConflict,
    FinalRoundSameSlot,
    FixedDateRequirement,
    FixedRoundDates,
    FullRoundOnDate,
    LegOrdering,
    MinRestDays,
    OneMatchPerTeamPerDay,
    RoundDayQuota,
    VenueDoubleBooking,
)
from .soft import (
    ConsecutiveAwayDays,
    ConsecutiveHomeDays,
    ContingentCommitmentConflict,
    GrassAwayRoundOne,
    GrassSeasonEdge,
    HomeAwayBalance,
    HomeAwayBreaks,
    LateKickoffLongTravel,
    PreferredWeekday,
    RestComfort,
    RivalryFixtureOnDate,
    SoftVenuePreference,
    TvTimeSpread,
)


def build_constraints(
    world: World,
    season: Season,
    competitions: list[Competition],
    commitments: CommitmentSet | None = None,
) -> list[Constraint]:
    """Every rule in force for this season, hard first.

    `competitions` are the league(s) being scheduled. `commitments` are the
    already-resolved cup and UEFA dates of teams sharing them — never
    scheduled here, only kept clear of: the hard ones by `CommitmentConflict`,
    the contingent ones by `ContingentCommitmentConflict` (#8).
    """
    hard_requirements = [r for r in season.fixed_requirements if r.hard]

    constraints: list[Constraint] = [
        OneMatchPerTeamPerDay(),
        MinRestDays(competitions=competitions),
        BlackoutDates(),
        VenueDoubleBooking(),
        ClubHomeClash(),
        LegOrdering(),
        FinalRoundSameSlot(competitions=competitions),
    ]
    if hard_requirements:
        constraints.append(FixedDateRequirement(requirements=hard_requirements))
    hard_full_rounds = [r for r in season.full_round_requirements if r.hard]
    if hard_full_rounds:
        constraints.append(FullRoundOnDate(requirements=hard_full_rounds))
    if any(c.fixed_round_dates for c in competitions):
        constraints.append(FixedRoundDates(competitions=competitions))
    if any(c.day_quota for c in competitions):
        constraints.append(RoundDayQuota(competitions=competitions))
    commitments = commitments or CommitmentSet()
    for source in ("cup", "uefa"):
        if any(c.source == source for c in commitments):
            constraints.append(CommitmentConflict(commitments=commitments, source=source))

    constraints.extend(
        [
            PreferredWeekday(competitions=competitions),
            ConsecutiveHomeDays(competitions=competitions),
            ConsecutiveAwayDays(competitions=competitions),
            HomeAwayBreaks(competitions=competitions),
            HomeAwayBalance(competitions=competitions),
            RestComfort(competitions=competitions),
            SoftVenuePreference(competitions=competitions),
            GrassAwayRoundOne(competitions=competitions),
            GrassSeasonEdge(competitions=competitions),
            LateKickoffLongTravel(competitions=competitions),
            TvTimeSpread(competitions=competitions),
        ]
    )
    if any(not c.hard for c in commitments):
        constraints.append(ContingentCommitmentConflict(commitments=commitments))

    # Soft fixed requirements ride on the same constraint as hard ones, scored
    # rather than enforced.
    soft_requirements = [r for r in season.fixed_requirements if not r.hard]
    if soft_requirements:
        soft = FixedDateRequirement(requirements=soft_requirements)
        soft.id = "fixed_requirement_preferred"
        soft.kind = "soft"
        soft.weight = max(r.weight for r in soft_requirements)
        constraints.append(soft)

    if season.rivalry_fixtures:
        constraints.append(
            RivalryFixtureOnDate(fixtures=season.rivalry_fixtures, season_year=season.year)
        )

    return constraints


# Human-readable descriptions, shown beside each rule in the HTML report so the
# score breakdown explains itself without the reader opening the source.
CONSTRAINT_DESCRIPTIONS: dict[str, str] = {
    "one_match_per_team_per_day": "A team plays at most one match per day.",
    "min_rest_days": "At least the configured number of days between a team's matches.",
    "blackout_dates": "No match on a blacked-out date, globally or at that venue.",
    "venue_double_booking": "A venue hosts at most one match per day.",
    "club_home_clash": "A club's teams are never both at home on the same day.",
    "leg_ordering": "Every first meeting is played before any second meeting.",
    "fixed_requirement": "A named team must be at home on a named date.",
    "cup_round_conflict": "A team's league matches stay clear of the cup rounds it is certain to play.",
    "european_commitment_conflict": "A team's league matches stay clear of the European legs it is certain to play.",
    "contingent_commitment_conflict": "A team's league matches prefer to stay clear of a cup or European date it only reaches with some probability, weighted by that probability.",
    "fixed_requirement_preferred": "A named team should be at home on a named date.",
    "preferred_weekday": "Matches on the league's preferred weekday.",
    "consecutive_home_days": "A club's two teams at home on back-to-back days.",
    "consecutive_away_days": "A club's two teams away on back-to-back days, within travel range.",
    "home_away_breaks": "Runs of three or more consecutive home or away matches.",
    "home_away_balance": "Home and away spread evenly within each half of the season.",
    "rest_comfort": "Rest gaps at or above the comfortable target, not just the legal minimum.",
    "soft_venue_preference": "Matches avoid dates that are legal but discouraged.",
    "final_round_same_slot": "Every league's final round shares one date and kickoff time.",
    "fixed_round_dates": "A round whose date is fixed (by TV) is played entirely on that date.",
    "round_day_quota": "No weekday hosts more matches of one round than the league's quota.",
    "full_round_on_date": "Every team in a competition has a match on a named date.",
    "grass_away_round_one": "Grass-pitch clubs play away in round 1.",
    "grass_season_edge": "Grass-pitch home matches avoid the cold edges of the season, with the window shifted by latitude.",
    "late_kickoff_long_travel": "Away teams with a long trip avoid late Sunday kickoffs.",
    "rivalry_fixture_on_date": "A fixed annual pairing lands on its date, home side alternating by year.",
    "tv_time_spread": "A round's preferred-weekday matches spread across an early, primary, and late kickoff time, clear of other competitions' kickoffs.",
}


def describe(constraint_id: str) -> str:
    return CONSTRAINT_DESCRIPTIONS.get(constraint_id, "")
