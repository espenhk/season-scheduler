"""Hard constraints — rules a finished schedule must not break.

Each one counts violations. The count, not the weight, is what makes a schedule
infeasible; `weight` is only used to shape the gradient the annealer follows
while it is still in infeasible territory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from ..model.schema import Competition, FixedRequirement
from .base import Constraint, ConstraintResult, EvalContext, Event, ScheduleIndex


@dataclass
class MinRestDays:
    """At least N days between any team's matches.

    A gap of exactly N is fine: Saturday to Tuesday is three days and legal at
    the default setting. Only gaps strictly below the minimum count — including
    a gap of zero, which is a team playing twice in one day.
    """

    competitions: list[Competition]
    id: str = "min_rest_days"
    kind: str = "hard"
    weight: float = 1.0
    _minimum_by_team: dict[str, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        # A team can appear in only one competition today, but taking the
        # strictest applicable minimum is what will keep this correct once a
        # team plays in a league and a cup at the same time.
        for competition in self.competitions:
            for team_id in competition.teams:
                current = self._minimum_by_team.get(team_id)
                if current is None or competition.min_rest_days > current:
                    self._minimum_by_team[team_id] = competition.min_rest_days

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        count = 0
        penalty = 0.0
        events: list[Event] = []

        for team_id, matches in index.by_team.items():
            minimum = self._minimum_by_team.get(team_id)
            if minimum is None:
                continue
            for earlier, later in zip(matches, matches[1:]):
                gap = (later.date - earlier.date).days
                if gap >= minimum:
                    continue
                count += 1
                # Deeper shortfalls hurt more, so the annealer prefers fixing a
                # same-day double-booking before a one-day-short gap.
                shortfall = minimum - gap
                penalty -= self.weight * shortfall
                if ctx.detail:
                    events.append(
                        Event(
                            delta=-self.weight * shortfall,
                            detail=(
                                f"{ctx.world.team_label(team_id)} has {gap} day(s) between "
                                f"{earlier.date} and {later.date} — needs {minimum}"
                            ),
                            match_keys=(earlier.key, later.key),
                        )
                    )

        return ConstraintResult(self.id, "hard", penalty, count, events)


@dataclass
class BlackoutDates:
    """No match on a globally blacked-out date, or at a blacked-out venue."""

    id: str = "blackout_dates"
    kind: str = "hard"
    weight: float = 1.0

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        season = ctx.season
        required_dates = {r.date for r in season.fixed_requirements if r.hard}
        global_blackouts = {
            b.date: b.reason for b in season.global_blackouts if b.date not in required_dates
        }
        venue_blackouts: dict[tuple[str, date], str] = {
            (b.venue, b.date): b.reason for b in season.venue_blackouts
        }

        count = 0
        penalty = 0.0
        events: list[Event] = []

        for match in index.matches:
            reason = global_blackouts.get(match.date)
            if reason is None:
                reason = venue_blackouts.get((match.venue, match.date))
                if reason is not None:
                    reason = f"{ctx.world.venue(match.venue).name} unavailable: {reason}"
            elif reason:
                reason = f"season blackout: {reason}"

            if reason is None:
                competition = ctx.world.competitions.get(match.competition_id)
                window_start = season.start
                window_end = season.end
                if competition is not None:
                    window_start = competition.start or season.start
                    window_end = competition.end or season.end
                if not (window_start <= match.date <= window_end):
                    count += 1
                    penalty -= self.weight
                    if ctx.detail:
                        events.append(
                            Event(
                                delta=-self.weight,
                                detail=(
                                    f"{match.date} falls outside the "
                                    f"{match.competition_id} window "
                                    f"{window_start}..{window_end}"
                                ),
                                match_keys=(match.key,),
                            )
                        )
                continue

            count += 1
            penalty -= self.weight
            if ctx.detail:
                events.append(
                    Event(
                        delta=-self.weight,
                        detail=(
                            f"{ctx.world.team_label(match.home_team)} v "
                            f"{ctx.world.team_label(match.away_team)} on {match.date} — {reason}"
                        ),
                        match_keys=(match.key,),
                    )
                )

        return ConstraintResult(self.id, "hard", penalty, count, events)


@dataclass
class VenueDoubleBooking:
    """One venue cannot host two matches on the same day.

    A physical impossibility, and the rule that binds Lillestrøm SK to LSK
    Kvinner: different clubs, but one Åråsen.
    """

    id: str = "venue_double_booking"
    kind: str = "hard"
    weight: float = 1.0

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        count = 0
        penalty = 0.0
        events: list[Event] = []

        for (venue_id, day), matches in index.by_venue_date.items():
            if len(matches) < 2:
                continue
            # Three matches at one venue is two clashes, not one.
            clashes = len(matches) - 1
            count += clashes
            penalty -= self.weight * clashes
            if ctx.detail:
                names = ", ".join(
                    f"{ctx.world.team_label(m.home_team)} v {ctx.world.team_label(m.away_team)}"
                    for m in matches
                )
                events.append(
                    Event(
                        delta=-self.weight * clashes,
                        detail=f"{ctx.world.venue(venue_id).name} hosts {len(matches)} matches on {day}: {names}",
                        match_keys=tuple(m.key for m in matches),
                    )
                )

        return ConstraintResult(self.id, "hard", penalty, count, events)


@dataclass
class ClubHomeClash:
    """A club's teams must not both be at home on the same day.

    Only fires when the two teams play at *different* venues — the same-venue
    case is already a `venue_double_booking`, and counting it twice would make
    the score breakdown misleading.
    """

    id: str = "club_home_clash"
    kind: str = "hard"
    weight: float = 1.0

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        count = 0
        penalty = 0.0
        events: list[Event] = []

        for club in ctx.world.dual_clubs():
            team_ids = [t.id for t in club.teams]
            home_days: dict[date, list[str]] = {}
            for team_id in team_ids:
                for day in index.home_dates_by_team.get(team_id, ()):  # type: ignore[arg-type]
                    home_days.setdefault(day, []).append(team_id)

            for day, teams_at_home in home_days.items():
                if len(teams_at_home) < 2:
                    continue
                venues = {ctx.world.team(t).home_venue for t in teams_at_home}
                if len(venues) < len(teams_at_home):
                    continue  # same venue — venue_double_booking owns this one
                clashes = len(teams_at_home) - 1
                count += clashes
                penalty -= self.weight * clashes
                if ctx.detail:
                    labels = " and ".join(ctx.world.team_label(t) for t in teams_at_home)
                    events.append(
                        Event(
                            delta=-self.weight * clashes,
                            detail=f"{club.name}: {labels} both at home on {day}",
                            match_keys=(),
                        )
                    )

        return ConstraintResult(self.id, "hard", penalty, count, events)


@dataclass
class LegOrdering:
    """In a double league, every first meeting precedes every second meeting.

    The round generator already guarantees this in round order; this constraint
    guards the *calendar*, where a stretched round window could otherwise let a
    late first-leg match slip past an early second-leg one.
    """

    id: str = "leg_ordering"
    kind: str = "hard"
    weight: float = 1.0

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        count = 0
        penalty = 0.0
        events: list[Event] = []

        for competition_id, matches in index.by_competition.items():
            last_of_leg: dict[int, date] = {}
            for match in matches:
                current = last_of_leg.get(match.leg)
                if current is None or match.date > current:
                    last_of_leg[match.leg] = match.date

            for match in matches:
                if match.leg < 2:
                    continue
                previous_end = last_of_leg.get(match.leg - 1)
                # Date-only precision: "precedes" excludes ties. A leg-2 match
                # landing on the same day the previous leg's last match was
                # played is not a first-meeting-before-second-meeting schedule,
                # so it counts as a violation rather than being waved through.
                if previous_end is None or match.date > previous_end:
                    continue
                count += 1
                penalty -= self.weight
                if ctx.detail:
                    events.append(
                        Event(
                            delta=-self.weight,
                            detail=(
                                f"{competition_id}: leg {match.leg} match on {match.date} starts "
                                f"before leg {match.leg - 1} finishes ({previous_end})"
                            ),
                            match_keys=(match.key,),
                        )
                    )

        return ConstraintResult(self.id, "hard", penalty, count, events)


@dataclass
class FixedDateRequirement:
    """A named team must be at home on a named date.

    May 16 in the shipped data: the eve of the national day, when the big clubs
    want the city full and a home fixture to sell.
    """

    requirements: list[FixedRequirement]
    id: str = "fixed_requirement"
    kind: str = "hard"
    weight: float = 1.0

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        count = 0
        penalty = 0.0
        events: list[Event] = []

        for requirement in self.requirements:
            satisfied = any(
                m.home_team == requirement.home_team
                and m.competition_id == requirement.competition
                for m in index.by_date.get(requirement.date, ())
            )
            if satisfied:
                continue
            count += 1
            penalty -= self.weight
            if ctx.detail:
                events.append(
                    Event(
                        delta=-self.weight,
                        detail=(
                            f"{ctx.world.team_label(requirement.home_team)} is not at home on "
                            f"{requirement.date} — {requirement.reason or requirement.id}"
                        ),
                        match_keys=(),
                    )
                )

        return ConstraintResult(self.id, "hard", penalty, count, events)


@dataclass
class OneMatchPerTeamPerDay:
    """A team plays at most once a day.

    Implied by `min_rest_days` whenever that minimum is 1 or more, but stated
    separately so the rule survives a competition that legitimately sets a very
    short minimum, and so the violation reads plainly in the report.
    """

    id: str = "one_match_per_team_per_day"
    kind: str = "hard"
    weight: float = 1.0

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        count = 0
        penalty = 0.0
        events: list[Event] = []

        for team_id, matches in index.by_team.items():
            seen: dict[date, int] = {}
            for match in matches:
                seen[match.date] = seen.get(match.date, 0) + 1
            for day, total in seen.items():
                if total < 2:
                    continue
                count += total - 1
                penalty -= self.weight * (total - 1)
                if ctx.detail:
                    events.append(
                        Event(
                            delta=-self.weight * (total - 1),
                            detail=f"{ctx.world.team_label(team_id)} plays {total} matches on {day}",
                            match_keys=(),
                        )
                    )

        return ConstraintResult(self.id, "hard", penalty, count, events)


def _day_after(day: date) -> date:
    return day + timedelta(days=1)


__all__: list[str] = [
    "BlackoutDates",
    "ClubHomeClash",
    "Constraint",
    "FixedDateRequirement",
    "LegOrdering",
    "MinRestDays",
    "OneMatchPerTeamPerDay",
    "VenueDoubleBooking",
]
