"""Assembles the constraint list for a season.

One place decides which rules are in play. Both solver backends and the report
consume the same list, so a constraint added here is immediately live
everywhere — the point of declaring rules as objects rather than writing them
into a solver.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from ..contingency import ContingencyPlanner
from ..model.loader import World
from ..model.schema import Competition, Season
from ..rounds.commitments import CommitmentSet
from .base import Constraint
from .hard import (
    BlackoutDates,
    ClubHomeClash,
    CommitmentConflict,
    ContingencyCollision,
    ContingencyCoverage,
    FinalRoundSameSlot,
    FixedDateRequirement,
    FixedRoundDates,
    FullRoundOnDate,
    LegOrdering,
    MinRestDays,
    OneMatchPerTeamPerDay,
    ReserveDateUnused,
    RoundDayQuota,
    VenueDoubleBooking,
)
from .soft import (
    ConsecutiveAwayDays,
    ConsecutiveHomeDays,
    ExpectedDisruption,
    GrassAwayRoundOne,
    GrassSeasonEdge,
    HomeAwayBalance,
    HomeAwayBreaks,
    LateKickoffLongTravel,
    PreferredWeekday,
    RestComfort,
    RivalryFixtureOnDate,
    ScheduleStability,
    SoftVenuePreference,
    TvTimeSpread,
)


def build_constraints(
    world: World,
    season: Season,
    competitions: list[Competition],
    commitments: CommitmentSet | None = None,
    *,
    reference: Mapping[str, date] | None = None,
    frozen: frozenset[str] = frozenset(),
    earliest: date | None = None,
) -> list[Constraint]:
    """Every rule in force for this season, hard first.

    `competitions` are the league(s) being scheduled. `commitments` are the
    already-resolved cup and UEFA dates of teams sharing them — never
    scheduled here, only kept clear of: the hard ones by `CommitmentConflict`,
    the contingent ones by the fallback rules over a `ContingencyPlanner` (#9).

    A re-plan (#13) passes `reference`, each fixture's date in the schedule
    being revised, which adds the stability rule, and `frozen`, the keys of
    matches that stay as they are and so are never given a fallback;
    `earliest` is the freeze date, before which no fallback is planned.
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
    if season.reserve_dates:
        constraints.append(ReserveDateUnused())
    commitments = commitments or CommitmentSet()
    for source in ("cup", "uefa"):
        if any(c.source == source for c in commitments):
            constraints.append(CommitmentConflict(commitments=commitments, source=source))
    planner = None
    if any(not c.hard for c in commitments):
        planner = ContingencyPlanner(world, season, competitions, commitments, frozen=frozen, earliest=earliest)
        constraints.append(ContingencyCoverage(planner=planner))
        constraints.append(ContingencyCollision(planner=planner))

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
    if planner is not None:
        constraints.append(ExpectedDisruption(planner=planner))

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

    if reference is not None:
        constraints.append(ScheduleStability(reference=dict(reference)))

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
    "reserve_date_unused": "No league fixture on a reserve date; those are kept free for fallbacks.",
    "contingency_coverage": "Every match a possible later cup or European round could displace has a valid fallback date.",
    "contingency_collision": "Two fallbacks share a team, venue or club date only if their triggers can never both happen.",
    "expected_disruption": "The expected cost of the fallback moves, weighted by the chance each cup or European round is reached.",
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
    "schedule_stability": "In a re-plan, a fixture stays on the date it had in the schedule being revised.",
}


# Display names for the report's scorecards, lens and fairness rows. The raw
# constraint id is a code identifier; these are what a reader sees.
RULE_NAMES: dict[str, str] = {
    "one_match_per_team_per_day": "One match per team per day",
    "min_rest_days": "Minimum rest between matches",
    "blackout_dates": "Blackout dates",
    "venue_double_booking": "Venue double-booking",
    "club_home_clash": "Club home clash",
    "leg_ordering": "Leg ordering",
    "fixed_requirement": "Fixed home date",
    "cup_round_conflict": "Cup round clash",
    "european_commitment_conflict": "European clash",
    "reserve_date_unused": "Reserve date used",
    "contingency_coverage": "Fallback coverage",
    "contingency_collision": "Fallback collision",
    "expected_disruption": "Expected disruption",
    "fixed_requirement_preferred": "Preferred home date",
    "preferred_weekday": "Preferred weekday",
    "consecutive_home_days": "Back-to-back home days",
    "consecutive_away_days": "Back-to-back away days",
    "home_away_breaks": "Long home or away runs",
    "home_away_balance": "Home and away balance",
    "rest_comfort": "Comfortable rest",
    "soft_venue_preference": "Discouraged dates",
    "final_round_same_slot": "Final round in one slot",
    "fixed_round_dates": "TV-fixed round dates",
    "round_day_quota": "Weekday quota per round",
    "full_round_on_date": "Full round on one date",
    "grass_away_round_one": "Grass pitch away in round one",
    "grass_season_edge": "Grass pitch at season edges",
    "late_kickoff_long_travel": "Late kickoff on long trips",
    "rivalry_fixture_on_date": "Rivalry fixture date",
    "tv_time_spread": "TV kickoff spread",
    "schedule_stability": "Schedule stability",
}


# Short badge text for a fixture the rule fired on (#29); the full wording
# stays in the fixture's tooltip.
BADGE_LABELS: dict[str, str] = {
    "one_match_per_team_per_day": "double-booked",
    "min_rest_days": "rest below minimum",
    "blackout_dates": "blackout",
    "venue_double_booking": "venue clash",
    "club_home_clash": "club home clash",
    "leg_ordering": "leg order",
    "fixed_requirement": "fixed date",
    "cup_round_conflict": "cup clash",
    "european_commitment_conflict": "Europe clash",
    "reserve_date_unused": "reserve date",
    "contingency_coverage": "no fallback",
    "contingency_collision": "fallback clash",
    "expected_disruption": "may move",
    "fixed_requirement_preferred": "preferred date",
    "preferred_weekday": "off-day",
    "consecutive_home_days": "home pair",
    "consecutive_away_days": "away pair",
    "home_away_breaks": "long run",
    "home_away_balance": "imbalance",
    "rest_comfort": "short rest",
    "soft_venue_preference": "discouraged date",
    "final_round_same_slot": "final-round slot",
    "fixed_round_dates": "off TV date",
    "round_day_quota": "day over quota",
    "full_round_on_date": "round date",
    "grass_away_round_one": "grass: away R1",
    "grass_season_edge": "cold grass",
    "late_kickoff_long_travel": "late + long trip",
    "rivalry_fixture_on_date": "rivalry date",
    "tv_time_spread": "TV slots",
    "schedule_stability": "moved",
}


def badge_label(constraint_id: str) -> str:
    return BADGE_LABELS.get(constraint_id, constraint_id.replace("_", " "))


def describe(constraint_id: str) -> str:
    return CONSTRAINT_DESCRIPTIONS.get(constraint_id, "")


# What a rule's achieved-of-opportunities count is counting, for rules that
# report one (#34).
COVERAGE_UNITS: dict[str, str] = {
    "preferred_weekday": "matches",
    "consecutive_home_days": "home matches of two-team clubs",
    "rest_comfort": "gaps",
    "home_away_balance": "team half-seasons",
}


def rule_name(constraint_id: str) -> str:
    return RULE_NAMES.get(constraint_id, constraint_id.replace("_", " "))
