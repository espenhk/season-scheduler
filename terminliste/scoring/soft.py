"""Soft constraints — preferences that shape a schedule rather than gate it.

Signed by convention: rules that reward good structure return positive totals,
rules that discourage bad structure return negative ones. The report reads the
same list from both ends to build "biggest upsides" and "biggest problems".

Weights come from the competition YAML, so tuning the character of a season —
more Sundays, or more back-to-back home weekends — is a data edit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..model.schema import WEEKDAYS, Competition, Match, RivalryFixture
from ..model.schema import TvTimeSpread as TvTimeSpreadConfig
from ..rounds.european_schedule import EuropeanCommitmentDate
from .base import ConstraintResult, EvalContext, Event, ScheduleIndex


@dataclass
class PreferredWeekday:
    """Reward matches landing on the league's preferred day.

    Eliteserien wants Sunday, Toppserien Saturday. Expressed as a reward for
    hitting the day rather than a penalty for missing it, so a competition with
    no strong preference simply earns fewer points instead of being punished
    for a calendar it did not choose.
    """

    competitions: list[Competition]
    id: str = "preferred_weekday"
    kind: str = "soft"
    weight: float = 1.0
    _by_competition: dict[str, tuple[int, float]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for competition in self.competitions:
            weight = competition.weights.get("preferred_weekday", self.weight)
            self._by_competition[competition.id] = (
                WEEKDAYS.index(competition.preferred_weekday),
                weight,
            )

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        total = 0.0
        count = 0
        events: list[Event] = []

        for competition_id, matches in index.by_competition.items():
            config = self._by_competition.get(competition_id)
            if config is None:
                continue
            weekday_index, weight = config
            on_day = sum(1 for m in matches if m.date.weekday() == weekday_index)
            total += weight * on_day
            count += on_day
            if ctx.detail and matches:
                competition = ctx.world.competition(competition_id)
                share = 100.0 * on_day / len(matches)
                events.append(
                    Event(
                        delta=weight * on_day,
                        detail=(
                            f"{competition.name}: {on_day}/{len(matches)} matches "
                            f"({share:.0f}%) on {competition.preferred_weekday.capitalize()}"
                        ),
                    )
                )

        return ConstraintResult(self.id, "soft", total, count, events)


@dataclass
class ConsecutiveHomeDays:
    """Reward a club's two teams playing at home on back-to-back days.

    The headline win of scheduling the leagues together: one trip into town
    buys a supporter two matches, and the club runs one match weekend instead
    of two.
    """

    competitions: list[Competition]
    id: str = "consecutive_home_days"
    kind: str = "soft"
    weight: float = 25.0
    _weight_by_team: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._weight_by_team = _weights_by_team(
            self.competitions, "consecutive_home_days", self.weight
        )

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        total = 0.0
        count = 0
        events: list[Event] = []

        for club in ctx.world.dual_clubs():
            for first, second in _team_pairs(club):
                first_days = index.home_dates_by_team.get(first, set())
                second_days = index.home_dates_by_team.get(second, set())
                if not first_days or not second_days:
                    continue
                weight = max(
                    self._weight_by_team.get(first, self.weight),
                    self._weight_by_team.get(second, self.weight),
                )
                # Both orders count: either team may take the Saturday. Each
                # adjacency is visited once because `day` only ranges over the
                # first team's home dates.
                for day in first_days:
                    for earlier, later, other_day in (
                        (first, second, day + _ONE_DAY),
                        (second, first, day - _ONE_DAY),
                    ):
                        if other_day not in second_days:
                            continue
                        total += weight
                        count += 1
                        if ctx.detail:
                            first_day = min(day, other_day)
                            events.append(
                                Event(
                                    delta=weight,
                                    detail=(
                                        f"{club.name}: {ctx.world.team_label(earlier)} home "
                                        f"{first_day}, {ctx.world.team_label(later)} home "
                                        f"{first_day + _ONE_DAY}"
                                    ),
                                    team_ids=(earlier, later),
                                )
                            )

        return ConstraintResult(self.id, "soft", total, count, events)


@dataclass
class ConsecutiveAwayDays:
    """Reward back-to-back away days when the two venues are close enough.

    Scaled by how easy the hop is: a two-hour transfer earns nearly full marks,
    one just inside the eight-hour limit earns very little, and anything beyond
    it — or unreachable by road, which in Norway means most things involving
    Tromsø — earns nothing at all.
    """

    competitions: list[Competition]
    id: str = "consecutive_away_days"
    kind: str = "soft"
    weight: float = 15.0
    _weight_by_team: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._weight_by_team = _weights_by_team(
            self.competitions, "consecutive_away_days", self.weight
        )

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        total = 0.0
        count = 0
        events: list[Event] = []
        max_hours = ctx.away_pairing_max_hours

        away_venue = index.away_venue_by_team_date

        for club in ctx.world.dual_clubs():
            for first, second in _team_pairs(club):
                first_days = index.away_dates_by_team.get(first, set())
                second_days = index.away_dates_by_team.get(second, set())
                if not first_days or not second_days:
                    continue
                weight = max(
                    self._weight_by_team.get(first, self.weight),
                    self._weight_by_team.get(second, self.weight),
                )
                for day in first_days:
                    for earlier, later, other_day in (
                        (first, second, day + _ONE_DAY),
                        (second, first, day - _ONE_DAY),
                    ):
                        if other_day not in second_days:
                            continue
                        first_day = min(day, other_day)
                        venue_a = away_venue.get((earlier, first_day))
                        venue_b = away_venue.get((later, first_day + _ONE_DAY))
                        if venue_a is None or venue_b is None:
                            continue
                        hours = ctx.travel.hours(venue_a, venue_b)  # type: ignore[attr-defined]
                        if not math.isfinite(hours) or hours > max_hours:
                            continue
                        # Linear falloff: a short hop is worth close to full
                        # marks, one just inside the limit almost nothing.
                        factor = 1.0 - (hours / max_hours)
                        total += weight * factor
                        count += 1
                        if ctx.detail:
                            events.append(
                                Event(
                                    delta=weight * factor,
                                    detail=(
                                        f"{club.name}: {ctx.world.team_label(earlier)} away at "
                                        f"{ctx.world.venue(venue_a).name} {first_day}, "
                                        f"{ctx.world.team_label(later)} away at "
                                        f"{ctx.world.venue(venue_b).name} "
                                        f"{first_day + _ONE_DAY} ({hours:.1f}h apart)"
                                    ),
                                    team_ids=(earlier, later),
                                )
                            )

        return ConstraintResult(self.id, "soft", total, count, events)


@dataclass
class HomeAwayBreaks:
    """Penalise runs of three or more consecutive home or away matches.

    A team playing five straight away matches has a wrecked season regardless
    of how the rest of the calendar looks; this is the classic measure of
    schedule quality in the round-robin literature.
    """

    competitions: list[Competition]
    id: str = "home_away_breaks"
    kind: str = "soft"
    weight: float = 8.0
    max_run: int = 2
    _weight_by_team: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._weight_by_team = _weights_by_team(
            self.competitions, "home_away_breaks", self.weight
        )

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        total = 0.0
        count = 0
        events: list[Event] = []

        for team_id, matches in index.by_team.items():
            weight = self._weight_by_team.get(team_id)
            if weight is None:
                continue
            run_length = 0
            run_is_home: bool | None = None
            run_start: date | None = None

            for match in matches:
                is_home = match.home_team == team_id
                if is_home == run_is_home:
                    run_length += 1
                else:
                    total, count = self._close_run(
                        run_length, run_is_home, run_start, weight, team_id, ctx, events, total, count
                    )
                    run_is_home, run_length, run_start = is_home, 1, match.date

            total, count = self._close_run(
                run_length, run_is_home, run_start, weight, team_id, ctx, events, total, count
            )

        return ConstraintResult(self.id, "soft", total, count, events)

    def _close_run(
        self,
        run_length: int,
        run_is_home: bool | None,
        run_start: date | None,
        weight: float,
        team_id: str,
        ctx: EvalContext,
        events: list[Event],
        total: float,
        count: int,
    ) -> tuple[float, int]:
        if run_is_home is None or run_length <= self.max_run:
            return total, count
        excess = run_length - self.max_run
        total -= weight * excess
        count += 1
        if ctx.detail:
            where = "home" if run_is_home else "away"
            events.append(
                Event(
                    delta=-weight * excess,
                    detail=(
                        f"{ctx.world.team_label(team_id)} plays {run_length} consecutive "
                        f"{where} matches from {run_start}"
                    ),
                    team_ids=(team_id,),
                )
            )
        return total, count


@dataclass
class HomeAwayBalance:
    """Penalise a home/away split that drifts within either half of the season.

    Over a full double league every team ends level by construction, so the
    only thing worth measuring is whether they got there smoothly or played
    fifteen home matches and then fifteen away. Both competitions this
    project schedules are double leagues today, but an odd `rounds_per_pairing`
    (a triple round-robin, say) can only ever end within 1 of level — see
    `round_robin.generate_fixtures` for why that's still guaranteed — which
    this rule doesn't need to know about: it only ever looks at the
    within-half drift, never the season-end total.
    """

    competitions: list[Competition]
    id: str = "home_away_balance"
    kind: str = "soft"
    weight: float = 6.0
    tolerance: int = 2
    _weight_by_team: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._weight_by_team = _weights_by_team(
            self.competitions, "home_away_balance", self.weight
        )

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        total = 0.0
        count = 0
        events: list[Event] = []

        for team_id, matches in index.by_team.items():
            weight = self._weight_by_team.get(team_id)
            if weight is None or not matches:
                continue
            midpoint = len(matches) // 2
            for label, half in (("first", matches[:midpoint]), ("second", matches[midpoint:])):
                if not half:
                    continue
                home = sum(1 for m in half if m.home_team == team_id)
                drift = abs(home - (len(half) - home))
                if drift <= self.tolerance:
                    continue
                excess = drift - self.tolerance
                total -= weight * excess
                count += 1
                if ctx.detail:
                    events.append(
                        Event(
                            delta=-weight * excess,
                            detail=(
                                f"{ctx.world.team_label(team_id)} plays {home} home and "
                                f"{len(half) - home} away in the {label} half"
                            ),
                            team_ids=(team_id,),
                        )
                    )

        return ConstraintResult(self.id, "soft", total, count, events)


@dataclass
class RestComfort:
    """Penalise rest counts that clear the hard minimum but are still short.

    Two rest days is legal; five is comfortable. This is what pushes the
    solver towards a humane calendar once feasibility is no longer in
    question.
    """

    competitions: list[Competition]
    id: str = "rest_comfort"
    kind: str = "soft"
    weight: float = 3.0
    _config_by_team: dict[str, tuple[int, int, float]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for competition in self.competitions:
            weight = competition.weights.get("rest_comfort", self.weight)
            for team_id in competition.teams:
                self._config_by_team[team_id] = (
                    competition.min_rest_days,
                    competition.comfortable_rest_days,
                    weight,
                )

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        total = 0.0
        count = 0
        events: list[Event] = []

        for team_id, matches in index.by_team.items():
            config = self._config_by_team.get(team_id)
            if config is None:
                continue
            minimum, comfortable, weight = config
            for earlier, later in zip(matches, matches[1:]):
                rest_days = (later.date - earlier.date).days - 1
                # Below the minimum is min_rest_days' business, not ours.
                if rest_days < minimum or rest_days >= comfortable:
                    continue
                shortfall = comfortable - rest_days
                total -= weight * shortfall
                count += 1
                if ctx.detail:
                    events.append(
                        Event(
                            delta=-weight * shortfall,
                            detail=(
                                f"{ctx.world.team_label(team_id)} gets {rest_days} days' rest "
                                f"before {later.date} (comfortable is {comfortable})"
                            ),
                            match_keys=(earlier.key, later.key),
                            team_ids=(team_id,),
                        )
                    )

        return ConstraintResult(self.id, "soft", total, count, events)


@dataclass
class SoftVenuePreference:
    """Penalise matches on dates that are legal but discouraged."""

    competitions: list[Competition]
    id: str = "soft_venue_preference"
    kind: str = "soft"
    weight: float = 5.0
    _weight_by_competition: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for competition in self.competitions:
            self._weight_by_competition[competition.id] = competition.weights.get(
                "soft_venue_preference", self.weight
            )

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        discouraged = {d.date: d.reason for d in ctx.season.discouraged_dates}
        if not discouraged:
            return ConstraintResult(self.id, "soft", 0.0, 0, [])

        total = 0.0
        count = 0
        events: list[Event] = []

        for match in index.matches:
            reason = discouraged.get(match.date)
            if reason is None:
                continue
            weight = self._weight_by_competition.get(match.competition_id, self.weight)
            total -= weight
            count += 1
            if ctx.detail:
                events.append(
                    Event(
                        delta=-weight,
                        detail=(
                            f"{ctx.world.team_label(match.home_team)} v "
                            f"{ctx.world.team_label(match.away_team)} on {match.date} — {reason}"
                        ),
                        match_keys=(match.key,),
                        team_ids=(match.home_team, match.away_team),
                    )
                )

        return ConstraintResult(self.id, "soft", total, count, events)


@dataclass
class GrassAwayRoundOne:
    """Reward a grass-pitch club playing away in round 1.

    Natural turf needs longer to establish than an artificial or hybrid
    surface, so a grass-venue side is often not match-ready the very first
    weekend of the season — an away trip is kinder than hosting on a pitch
    that isn't ready for it yet.
    """

    competitions: list[Competition]
    id: str = "grass_away_round_one"
    kind: str = "soft"
    weight: float = 4.0
    _weight_by_team: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._weight_by_team = _weights_by_team(self.competitions, "grass_away_round_one", self.weight)

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        total = 0.0
        count = 0
        events: list[Event] = []

        for match in index.matches:
            if match.round_index != 0:
                continue
            weight = self._weight_by_team.get(match.away_team)
            if weight is None:
                continue
            if ctx.world.home_venue_of(match.away_team).surface != "grass":
                continue
            total += weight
            count += 1
            if ctx.detail:
                events.append(
                    Event(
                        delta=weight,
                        detail=(
                            f"{ctx.world.team_label(match.away_team)} (grass pitch) starts the "
                            f"season away, at {ctx.world.venue(match.venue).name}"
                        ),
                        match_keys=(match.key,),
                        team_ids=(match.away_team,),
                    )
                )

        return ConstraintResult(self.id, "soft", total, count, events)


@dataclass
class LateKickoffLongTravel:
    """Penalise a late Sunday kickoff for an away team with a long trip home.

    A late kickoff already means a late finish; a long drive or flight home
    on top of that makes for a genuinely bad night for the travelling side.
    An earlier kickoff, or a short trip, is never penalised regardless of the
    other factor — this only fires when both line up. `late_from` and
    `long_travel_hours` are the tunable thresholds; `late_kickoff_long_travel`
    in a competition's `weights` overrides `weight` the same way every other
    soft rule's weight can be tuned per competition.
    """

    competitions: list[Competition]
    id: str = "late_kickoff_long_travel"
    kind: str = "soft"
    weight: float = 6.0
    late_from: str = "19:00"
    long_travel_hours: float = 5.0
    _weight_by_competition: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for competition in self.competitions:
            self._weight_by_competition[competition.id] = competition.weights.get(
                "late_kickoff_long_travel", self.weight
            )

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        total = 0.0
        count = 0
        events: list[Event] = []

        for match in index.matches:
            if match.competition_id not in self._weight_by_competition:
                continue
            if match.date.weekday() != WEEKDAYS.index("sunday"):
                continue
            if match.kickoff_time is None or match.kickoff_time < self.late_from:
                continue
            away_venue = ctx.world.team(match.away_team).home_venue
            if away_venue == match.venue:
                continue
            hours = ctx.travel.hours(away_venue, match.venue)  # type: ignore[attr-defined]
            if not math.isfinite(hours) or hours < self.long_travel_hours:
                continue
            weight = self._weight_by_competition[match.competition_id]
            total -= weight
            count += 1
            if ctx.detail:
                events.append(
                    Event(
                        delta=-weight,
                        detail=(
                            f"{ctx.world.team_label(match.away_team)} kick off at "
                            f"{match.kickoff_time} at {ctx.world.venue(match.venue).name} on "
                            f"{match.date}, {hours:.1f}h from home"
                        ),
                        match_keys=(match.key,),
                        team_ids=(match.away_team,),
                    )
                )

        return ConstraintResult(self.id, "soft", total, count, events)


@dataclass
class RivalryFixtureOnDate:
    """Reward a fixed annual pairing landing on its date with the right home side.

    Bodø/Glimt vs Tromsø IL on May 16, home advantage alternating by year —
    the closest thing Norwegian football has to a natural derby at that
    latitude. Nothing pins the pairing to the date the way a hard requirement
    would; this only scores it when the schedule happens to land there.
    """

    fixtures: list[RivalryFixture]
    season_year: int
    id: str = "rivalry_fixture_on_date"
    kind: str = "soft"

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        total = 0.0
        count = 0
        events: list[Event] = []

        for fixture in self.fixtures:
            even_year = self.season_year % 2 == 0
            home_team = fixture.team_even_year_home if even_year else fixture.team_odd_year_home
            away_team = fixture.team_odd_year_home if even_year else fixture.team_even_year_home
            satisfied = any(
                m.competition_id == fixture.competition
                and m.home_team == home_team
                and m.away_team == away_team
                for m in index.by_date.get(fixture.date, ())
            )
            if not satisfied:
                continue
            total += fixture.weight
            count += 1
            if ctx.detail:
                events.append(
                    Event(
                        delta=fixture.weight,
                        detail=(
                            f"{ctx.world.team_label(home_team)} host "
                            f"{ctx.world.team_label(away_team)} on {fixture.date}"
                        ),
                        match_keys=(),
                        team_ids=(home_team, away_team),
                    )
                )

        return ConstraintResult(self.id, "soft", total, count, events)


@dataclass
class TvTimeSpread:
    """Reward a round following the TV-broadcast kickoff shape configured by
    `Competition.tv_time_spread` — most matches on the preferred weekday at
    the primary kickoff time, one early, one late — and penalise a match
    colliding, at the same date and kickoff time, with a match from a
    different competition.

    Kickoff time is assigned once per schedule, after dates are fixed
    (`rounds/kickoff.py::assign_kickoff_times`), rather than searched, so
    every match here with `kickoff_time is None` is skipped: that is every
    match during annealing, and this rule only has anything to say about the
    finished schedule a candidate's dates produce.
    """

    competitions: list[Competition]
    id: str = "tv_time_spread"
    kind: str = "soft"
    weight: float = 3.0
    collision_weight: float = 2.0
    _spread_by_competition: dict[str, TvTimeSpreadConfig] = field(default_factory=dict, init=False)
    _weight_by_competition: dict[str, float] = field(default_factory=dict, init=False)
    _collision_weight_by_competition: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for competition in self.competitions:
            if competition.tv_time_spread is None:
                continue
            self._spread_by_competition[competition.id] = competition.tv_time_spread
            self._weight_by_competition[competition.id] = competition.weights.get(
                "tv_time_spread", self.weight
            )
            self._collision_weight_by_competition[competition.id] = competition.weights.get(
                "tv_time_spread_collision", self.collision_weight
            )

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        if not self._spread_by_competition:
            return ConstraintResult(self.id, "soft", 0.0, 0, [])

        total = 0.0
        count = 0
        events: list[Event] = []

        rounds: dict[tuple[str, int], list[Match]] = {}
        for match in index.matches:
            if match.kickoff_time is None or match.competition_id not in self._spread_by_competition:
                continue
            rounds.setdefault((match.competition_id, match.round_index), []).append(match)

        for (competition_id, round_index), round_matches in rounds.items():
            spread = self._spread_by_competition[competition_id]
            weight = self._weight_by_competition[competition_id]
            competition = ctx.world.competition(competition_id)
            preferred = WEEKDAYS.index(competition.preferred_weekday)
            on_day = [m for m in round_matches if m.date.weekday() == preferred]
            if not on_day:
                continue

            has_early = any(m.kickoff_time == spread.early_kickoff_time for m in on_day)
            has_late = any(m.kickoff_time == spread.late_kickoff_time for m in on_day)
            primary_count = sum(1 for m in on_day if m.kickoff_time == spread.primary_kickoff_time)
            expected_primary = len(on_day) if len(on_day) < 2 else len(on_day) - 2
            shaped = primary_count == expected_primary and (len(on_day) < 2 or (has_early and has_late))
            if not shaped:
                continue

            total += weight
            count += 1
            if ctx.detail:
                events.append(
                    Event(
                        delta=weight,
                        detail=(
                            f"{competition.name} round {round_index + 1}: {primary_count} at "
                            f"{spread.primary_kickoff_time}, 1 at {spread.early_kickoff_time}, "
                            f"1 at {spread.late_kickoff_time}"
                            if len(on_day) >= 2
                            else f"{competition.name} round {round_index + 1}: "
                            f"{len(on_day)} match on {competition.preferred_weekday.capitalize()}"
                        ),
                        match_keys=tuple(m.key for m in on_day),
                    )
                )

        for day, matches_on_date in index.by_date.items():
            by_slot: dict[str, list[Match]] = {}
            for m in matches_on_date:
                if m.kickoff_time is not None:
                    by_slot.setdefault(m.kickoff_time, []).append(m)
            for kickoff_time, slot_matches in by_slot.items():
                competitions_in_slot = {m.competition_id for m in slot_matches}
                if len(competitions_in_slot) < 2:
                    continue
                configured = [
                    cid for cid in competitions_in_slot if cid in self._collision_weight_by_competition
                ]
                if not configured:
                    continue
                weight = max(self._collision_weight_by_competition[cid] for cid in configured)
                total -= weight
                count += 1
                if ctx.detail:
                    names = sorted(ctx.world.competition(cid).name for cid in competitions_in_slot)
                    events.append(
                        Event(
                            delta=-weight,
                            detail=f"{' / '.join(names)} both kick off at {kickoff_time} on {day}",
                            match_keys=tuple(m.key for m in slot_matches),
                        )
                    )

        return ConstraintResult(self.id, "soft", total, count, events)


_ONE_DAY = timedelta(days=1)


def _team_pairs(club) -> list[tuple[str, str]]:
    """Ordered pairs of a club's senior teams, each unordered pair once."""
    seniors = [t.id for t in club.teams if t.level == "senior"]
    return [(a, b) for i, a in enumerate(seniors) for b in seniors[i + 1 :]]


def _weights_by_team(
    competitions: list[Competition], key: str, default: float
) -> dict[str, float]:
    weights: dict[str, float] = {}
    for competition in competitions:
        weight = competition.weights.get(key, default)
        for team_id in competition.teams:
            weights[team_id] = weight
    return weights


@dataclass
class EuropeanCommitmentSoftConflict:
    """Discourage a league match too close to a team's *conditional*
    European qualifying leg date — a commitment reachable only via one of
    several mutually-exclusive cascade branches (issue #93).

    `EuropeanCommitmentConflict`'s (`scoring/hard.py`) soft counterpart,
    same point-date-plus-rest arithmetic, but reading only the commitments
    that constraint skips (`commitment.certain is False`): a team either
    wins a round and advances, or loses it and drops — never both — so
    blocking a domestic date against every branch at once as a hard
    exclusion over-constrains the calendar for an outcome that's still a
    coin flip. Keeping it as a soft penalty still steers the solver away
    from the date when a cheaper alternative exists, without making an
    otherwise-feasible schedule infeasible over a branch that never
    materializes.
    """

    commitments_by_team: dict[str, list[EuropeanCommitmentDate]]
    id: str = "european_commitment_soft_conflict"
    kind: str = "soft"
    weight: float = 3.0

    def evaluate(self, index: ScheduleIndex, ctx: EvalContext) -> ConstraintResult:
        total = 0.0
        count = 0
        events: list[Event] = []

        for team_id, commitments in self.commitments_by_team.items():
            for match in index.by_team.get(team_id, ()):
                for commitment in commitments:
                    if commitment.certain:
                        continue
                    rest_days = abs((match.date - commitment.date).days) - 1
                    if rest_days >= commitment.min_rest_days:
                        continue
                    shortfall = commitment.min_rest_days - rest_days
                    total -= self.weight * shortfall
                    count += 1
                    if ctx.detail:
                        events.append(
                            Event(
                                delta=-self.weight * shortfall,
                                detail=(
                                    f"{ctx.world.team_label(team_id)} plays {match.date} in "
                                    f"{match.competition_id}, {rest_days} rest day(s) from "
                                    f"conditional commitment {commitment.label} on "
                                    f"{commitment.date} — needs {commitment.min_rest_days}"
                                ),
                                match_keys=(match.key,),
                                team_ids=(team_id,),
                            )
                        )

        return ConstraintResult(self.id, "soft", total, count, events)


__all__ = [
    "ConsecutiveAwayDays",
    "ConsecutiveHomeDays",
    "EuropeanCommitmentSoftConflict",
    "GrassAwayRoundOne",
    "HomeAwayBalance",
    "HomeAwayBreaks",
    "LateKickoffLongTravel",
    "PreferredWeekday",
    "RestComfort",
    "RivalryFixtureOnDate",
    "SoftVenuePreference",
    "TvTimeSpread",
]
