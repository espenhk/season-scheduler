"""Hard constraints — rules a finished schedule must not break.

Each one counts violations. The count, not the weight, is what makes a schedule
infeasible; `weight` is only used to shape the gradient the annealer follows
while it is still in infeasible territory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from ..contingency import ContingencyPlanner
from ..model.schema import WEEKDAYS, Competition, FixedRequirement, FullRoundRequirement
from ..rounds.commitments import CommitmentSet, CommitmentSource
from .base import Constraint, ConstraintResult, EvalContext, Event, ScheduleIndex


@dataclass
class MinRestDays:
    """At least N full days off between any team's matches.

    N counts only the days strictly between two matchdays, so Thursday to
    Sunday is two rest days. Gaps against a European leg belong to
    `EuropeanCommitmentConflict`, which reads its minimum from the European
    competition — that split is what makes a Thu-Sun-Thu week legal on both
    sides without either rule knowing about the other.
    """

    competitions: list[Competition]
    id: str = "min_rest_days"
    kind: str = "hard"
    weight: float = 1.0
    _minimum_by_team: dict[str, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        # Strictest applicable minimum, so this stays correct once a team can
        # play in a league and a cup at once.
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
            for earlier, later in zip(matches, matches[1:], strict=False):
                rest_days = (later.date - earlier.date).days - 1
                if rest_days >= minimum:
                    continue
                count += 1
                # Deeper shortfalls hurt more, so the annealer fixes a same-day
                # double-booking before a one-day-short gap.
                shortfall = minimum - rest_days
                penalty -= self.weight * shortfall
                if ctx.detail:
                    events.append(
                        Event(
                            delta=-self.weight * shortfall,
                            detail=(
                                f"{ctx.world.team_label(team_id)} has {rest_days} rest day(s) "
                                f"between {earlier.date} and {later.date} — needs {minimum}"
                            ),
                            match_keys=(later.key,),
                        )
                    )

        return ConstraintResult(self.id, "hard", penalty, count, events)


@dataclass
class BlackoutDates:
    """No match on a globally blacked-out date (a single one, or any day
    inside an excluded date range), or at a blacked-out venue."""

    id: str = "blackout_dates"
    kind: str = "hard"
    weight: float = 1.0

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        season = ctx.season
        required_dates = {r.date for r in season.fixed_requirements if r.hard}
        global_blackouts = {
            day: reason
            for day, reason in season.blacked_out_dates.items()
            if day not in required_dates
        }
        venue_blackouts: dict[tuple[str, date], str] = {
            (venue_id, day): reason
            for venue_id, dated in season.venue_blacked_out_dates.items()
            for day, reason in dated.items()
        }

        count = 0
        penalty = 0.0
        events: list[Event] = []

        scoped_by_competition = {
            competition_id: {
                day: reason
                for day, reason in season.scoped_blackout_dates(competition).items()
                if day not in required_dates
            }
            for competition_id, competition in ctx.world.competitions.items()
        }

        for match in index.matches:
            reason = global_blackouts.get(match.date)
            if reason is None:
                reason = scoped_by_competition.get(match.competition_id, {}).get(match.date)
            if reason is not None:
                if reason:
                    reason = f"season blackout: {reason}"
            else:
                reason = venue_blackouts.get((match.venue, match.date))
                if reason is not None:
                    reason = f"{ctx.world.venue(match.venue).name} unavailable: {reason}"

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

    Also what binds Lillestrøm SK to LSK Kvinner: different clubs, one Åråsen.
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
                    clash_matches = [index.match_on(t, day, home=True) for t in teams_at_home]
                    events.append(
                        Event(
                            delta=-self.weight * clashes,
                            detail=f"{club.name}: {labels} both at home on {day}",
                            match_keys=tuple(m.key for m in clash_matches if m is not None),
                        )
                    )

        return ConstraintResult(self.id, "hard", penalty, count, events)


@dataclass
class LegOrdering:
    """In a double league, every first meeting precedes every second meeting.

    The round generator guarantees this in round order; this guards the
    calendar, where a stretched round window could let a late first-leg match
    slip past an early second-leg one.
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
                # "Precedes" excludes ties: a leg-2 match on the same day the
                # previous leg ends is a violation, not a pass.
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
    """A named team must be at home on a named date — May 16 in the shipped
    data, the eve of the national day."""

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
                kickoff = f" at {requirement.kickoff_time}" if requirement.kickoff_time else ""
                events.append(
                    Event(
                        delta=-self.weight,
                        detail=(
                            f"{ctx.world.team_label(requirement.home_team)} is not at home on "
                            f"{requirement.date}{kickoff} — {requirement.reason or requirement.id}"
                        ),
                        match_keys=(),
                    )
                )

        return ConstraintResult(self.id, "hard", penalty, count, events)


@dataclass
class OneMatchPerTeamPerDay:
    """A team plays at most once a day.

    Implied by `min_rest_days` at any minimum of 1 or more, but stated
    separately to survive a competition that sets a very short minimum, and to
    read plainly in the report.
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
                            match_keys=tuple(m.key for m in matches if m.date == day),
                        )
                    )

        return ConstraintResult(self.id, "hard", penalty, count, events)


_CONFLICT_IDS = {"cup": "cup_round_conflict", "uefa": "european_commitment_conflict"}


@dataclass
class CommitmentConflict:
    """No league match too close to a team's hard commitment (#8).

    One rule for both sources, split into two ids so the report names which
    one a clash came from: `cup_round_conflict` for cup rounds and
    `european_commitment_conflict` for UEFA legs. Cup ties are not `Match`
    objects — real opponents are unknown — and a UEFA leg is one date rather
    than a span, so a normal Thu-Sun-Thu week stays legal.

    Only commitments the season's `contingency_policy` makes hard count;
    `ContingentCommitmentConflict` scores the rest.
    """

    commitments: CommitmentSet
    source: CommitmentSource
    id: str = ""
    kind: str = "hard"
    weight: float = 1.0

    def __post_init__(self) -> None:
        if not self.id:
            self.id = _CONFLICT_IDS[self.source]
        self._by_team = {
            team_id: [c for c in self.commitments.hard_for(team_id) if c.source == self.source]
            for team_id in self.commitments.team_ids
        }

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        count = 0
        penalty = 0.0
        events: list[Event] = []

        for team_id, commitments in self._by_team.items():
            for match in index.by_team.get(team_id, ()):
                for commitment in commitments:
                    rest_days = abs((match.date - commitment.date).days) - 1
                    if rest_days >= commitment.min_rest_days:
                        continue
                    count += 1
                    shortfall = commitment.min_rest_days - rest_days
                    penalty -= self.weight * shortfall
                    if ctx.detail:
                        events.append(
                            Event(
                                delta=-self.weight * shortfall,
                                detail=(
                                    f"{ctx.world.team_label(team_id)} plays {match.date} in "
                                    f"{match.competition_id}, {rest_days} rest day(s) from "
                                    f"{commitment.label} on {commitment.date} — needs "
                                    f"{commitment.min_rest_days}"
                                ),
                                match_keys=(match.key,),
                            )
                        )

        return ConstraintResult(self.id, "hard", penalty, count, events)


@dataclass
class ReserveDateUnused:
    """No league fixture on a reserve date: those are kept for fallbacks (#9)."""

    id: str = "reserve_date_unused"
    kind: str = "hard"
    weight: float = 1.0

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        count = 0
        events: list[Event] = []
        for day in sorted(ctx.season.reserve_date_set):
            for match in index.by_date.get(day, ()):
                count += 1
                if ctx.detail:
                    events.append(
                        Event(
                            delta=-self.weight,
                            detail=f"{match.key} is on the reserve date {day}",
                            match_keys=(match.key,),
                        )
                    )
        return ConstraintResult(self.id, "hard", 0.0 - self.weight * count, count, events)


@dataclass
class ContingencyCoverage:
    """Every match a possible later cup or UEFA round could displace has a
    fallback that is valid once that round is reached (#9).

    Valid means rest, one match per day, venue and global blackouts, venue
    booking, club home clashes and leg order all hold on the fallback date,
    counting the commitments that come along with the trigger.
    """

    planner: ContingencyPlanner
    id: str = "contingency_coverage"
    kind: str = "hard"
    weight: float = 1.0

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        analysis = self.planner.analyse(index)
        by_key = {m.key: m for m in index.matches} if ctx.detail else {}
        events: list[Event] = []
        count = len(analysis.plan.uncovered) + len(analysis.invalid)
        if ctx.detail:
            for key in analysis.plan.uncovered:
                match = by_key[key]
                triggers = ", ".join(analysis.triggers[key])
                events.append(
                    Event(
                        delta=-self.weight,
                        detail=(
                            f"{key} on {match.date} has no valid fallback if {triggers} "
                            f"is reached"
                        ),
                        match_keys=(key,),
                    )
                )
            for key, reason in analysis.invalid:
                events.append(
                    Event(
                        delta=-self.weight,
                        detail=f"the fallback for {key} is not valid: {reason}",
                        match_keys=(key,),
                    )
                )
        return ConstraintResult(self.id, "hard", 0.0 - self.weight * count, count, events)


@dataclass
class ContingencyCollision:
    """Two fallbacks may share a team, venue or club date only when their
    triggers can never both fire, e.g. one team's Europa League play-off and
    Conference League play-off. Independent teams' wins can (#9)."""

    planner: ContingencyPlanner
    id: str = "contingency_collision"
    kind: str = "hard"
    weight: float = 1.0

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        collisions = self.planner.analyse(index).collisions
        events = [
            Event(
                delta=-self.weight,
                detail=f"the fallbacks for {a} and {b} clash: {reason}",
                match_keys=(a, b),
            )
            for a, b, reason in collisions
            if ctx.detail
        ]
        return ConstraintResult(
            self.id, "hard", 0.0 - self.weight * len(collisions), len(collisions), events
        )


@dataclass
class FinalRoundSameSlot:
    """Every league's final round lands on one date, at one kickoff time, so
    no team gains an edge from kicking off after its rivals have finished.

    Checks the outcome only; `solvers/greedy.py::resolve_round_pins` and
    `rounds/kickoff.py` are what make it achievable.
    """

    competitions: list[Competition]
    id: str = "final_round_same_slot"
    kind: str = "hard"
    weight: float = 1.0

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        count = 0
        penalty = 0.0
        events: list[Event] = []

        for competition in self.competitions:
            if competition.format != "league":
                continue
            final_round = competition.rounds - 1
            matches = [
                m
                for m in index.by_competition.get(competition.id, ())
                if m.round_index == final_round
            ]
            if len(matches) < 2:
                continue
            dates = {m.date for m in matches}
            kickoffs = {m.kickoff_time for m in matches}
            if len(dates) <= 1 and len(kickoffs) <= 1:
                continue
            offenders = max(len(dates) - 1, 0) + max(len(kickoffs) - 1, 0)
            count += offenders
            penalty -= self.weight * offenders
            if ctx.detail:
                events.append(
                    Event(
                        delta=-self.weight * offenders,
                        detail=(
                            f"{competition.name}: final round spread across {len(dates)} "
                            f"date(s) and {len(kickoffs)} kickoff time(s)"
                        ),
                        match_keys=tuple(m.key for m in matches),
                    )
                )

        return ConstraintResult(self.id, "hard", penalty, count, events)


@dataclass
class FullRoundOnDate:
    """Every team in a competition has a match on a named date — May 16 in
    Eliteserien is a full round, not just the marquee fixtures
    `FixedDateRequirement` pins."""

    requirements: list[FullRoundRequirement]
    id: str = "full_round_on_date"
    kind: str = "hard"
    weight: float = 1.0

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        count = 0
        penalty = 0.0
        events: list[Event] = []

        for requirement in self.requirements:
            competition = ctx.world.competition(requirement.competition)
            playing: set[str] = set()
            for match in index.by_date.get(requirement.date, ()):
                if match.competition_id != requirement.competition:
                    continue
                playing.add(match.home_team)
                playing.add(match.away_team)
            missing = [t for t in competition.teams if t not in playing]
            if not missing:
                continue
            count += len(missing)
            penalty -= self.weight * len(missing)
            if ctx.detail:
                names = ", ".join(ctx.world.team_label(t) for t in missing)
                events.append(
                    Event(
                        delta=-self.weight * len(missing),
                        detail=(
                            f"{competition.name}: {len(missing)} team(s) without a match on "
                            f"{requirement.date} — {names}"
                        ),
                        match_keys=(),
                    )
                )

        return ConstraintResult(self.id, "hard", penalty, count, events)


def _day_after(day: date) -> date:
    return day + timedelta(days=1)


@dataclass
class FixedRoundDates:
    """Every match of a round fixed from outside (TV) is played on its date."""

    competitions: list[Competition]
    id: str = "fixed_round_dates"
    kind: str = "hard"
    weight: float = 1.0

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        count = 0
        penalty = 0.0
        events: list[Event] = []

        for competition in self.competitions:
            for fixed in competition.fixed_round_dates:
                matches = [
                    m
                    for m in index.by_competition.get(competition.id, ())
                    if m.round_index == fixed.round - 1
                ]
                off = [m for m in matches if m.date != fixed.date]
                if not off:
                    continue
                count += len(off)
                penalty -= self.weight * len(off)
                if ctx.detail:
                    events.append(
                        Event(
                            delta=-self.weight * len(off),
                            detail=(
                                f"{competition.name}: round {fixed.round} is fixed on "
                                f"{fixed.date}, but {len(off)} match(es) are elsewhere"
                            ),
                            match_keys=tuple(m.key for m in off),
                        )
                    )

        return ConstraintResult(self.id, "hard", penalty, count, events)


@dataclass
class RoundDayQuota:
    """No weekday hosts more matches of one round than the league's
    `day_quota` allows. Rounds played on one date are exempt."""

    competitions: list[Competition]
    id: str = "round_day_quota"
    kind: str = "hard"
    weight: float = 1.0

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        count = 0
        penalty = 0.0
        events: list[Event] = []

        for competition in self.competitions:
            if not competition.day_quota:
                continue
            per_day: dict[tuple[int, str], list] = {}
            for match in index.by_competition.get(competition.id, ()):
                weekday = WEEKDAYS[match.date.weekday()]
                if (
                    weekday in competition.day_quota
                    and match.round_index not in competition.day_quota_exempt_rounds
                ):
                    per_day.setdefault((match.round_index, weekday), []).append(match)
            for (round_index, weekday), matches in sorted(per_day.items()):
                excess = len(matches) - competition.day_quota[weekday]
                if excess <= 0:
                    continue
                count += excess
                penalty -= self.weight * excess
                if ctx.detail:
                    events.append(
                        Event(
                            delta=-self.weight * excess,
                            detail=(
                                f"{competition.name}: round {round_index + 1} has "
                                f"{len(matches)} matches on {weekday}, quota "
                                f"{competition.day_quota[weekday]}"
                            ),
                            match_keys=tuple(m.key for m in matches),
                        )
                    )

        return ConstraintResult(self.id, "hard", penalty, count, events)


__all__: list[str] = [
    "BlackoutDates",
    "ClubHomeClash",
    "Constraint",
    "CommitmentConflict",
    "ContingencyCollision",
    "ContingencyCoverage",
    "FinalRoundSameSlot",
    "FixedDateRequirement",
    "FixedRoundDates",
    "FullRoundOnDate",
    "LegOrdering",
    "MinRestDays",
    "OneMatchPerTeamPerDay",
    "ReserveDateUnused",
    "RoundDayQuota",
    "VenueDoubleBooking",
]
