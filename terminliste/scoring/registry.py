"""Assembles the constraint list for a season.

One place decides which rules are in play. Both solver backends and the report
consume the same list, so a constraint added here is immediately live
everywhere — that is the whole point of declaring rules as objects rather than
writing them into a solver.
"""

from __future__ import annotations

from ..model.loader import World
from ..model.schema import Competition, Season
from .base import Constraint
from .hard import (
    BlackoutDates,
    ClubHomeClash,
    FixedDateRequirement,
    LegOrdering,
    MinRestDays,
    OneMatchPerTeamPerDay,
    VenueDoubleBooking,
)
from .soft import (
    ConsecutiveAwayDays,
    ConsecutiveHomeDays,
    HomeAwayBalance,
    HomeAwayBreaks,
    PreferredWeekday,
    RestComfort,
    SoftVenuePreference,
)


def build_constraints(
    world: World, season: Season, competitions: list[Competition]
) -> list[Constraint]:
    """Every rule in force for this season, hard first."""
    hard_requirements = [r for r in season.fixed_requirements if r.hard]

    constraints: list[Constraint] = [
        OneMatchPerTeamPerDay(),
        MinRestDays(competitions=competitions),
        BlackoutDates(),
        VenueDoubleBooking(),
        ClubHomeClash(),
        LegOrdering(),
    ]
    if hard_requirements:
        constraints.append(FixedDateRequirement(requirements=hard_requirements))

    constraints.extend(
        [
            PreferredWeekday(competitions=competitions),
            ConsecutiveHomeDays(competitions=competitions),
            ConsecutiveAwayDays(competitions=competitions),
            HomeAwayBreaks(competitions=competitions),
            HomeAwayBalance(competitions=competitions),
            RestComfort(competitions=competitions),
            SoftVenuePreference(competitions=competitions),
        ]
    )

    # Soft fixed requirements ride on the same constraint as hard ones, scored
    # rather than enforced.
    soft_requirements = [r for r in season.fixed_requirements if not r.hard]
    if soft_requirements:
        soft = FixedDateRequirement(requirements=soft_requirements)
        soft.id = "fixed_requirement_preferred"
        soft.kind = "soft"
        soft.weight = max(r.weight for r in soft_requirements)
        constraints.append(soft)

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
    "fixed_requirement_preferred": "A named team should be at home on a named date.",
    "preferred_weekday": "Matches on the league's preferred weekday.",
    "consecutive_home_days": "A club's two teams at home on back-to-back days.",
    "consecutive_away_days": "A club's two teams away on back-to-back days, within travel range.",
    "home_away_breaks": "Runs of three or more consecutive home or away matches.",
    "home_away_balance": "Home and away spread evenly within each half of the season.",
    "rest_comfort": "Rest gaps at or above the comfortable target, not just the legal minimum.",
    "soft_venue_preference": "Matches avoid dates that are legal but discouraged.",
}


def describe(constraint_id: str) -> str:
    return CONSTRAINT_DESCRIPTIONS.get(constraint_id, "")
