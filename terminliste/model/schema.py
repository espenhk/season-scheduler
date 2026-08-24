"""Pydantic models mirroring the YAML files under /data/.

Pure data containers — no scheduling logic, no I/O. `loader.py` reads the YAML
into these and cross-checks referential integrity; everything downstream works
off the validated result and never touches the raw YAML.

The shape is deliberately wider than needed: `Team.level` makes reserve sides a
data edit rather than a refactor, and `Competition.format` discriminates so cups
sit alongside leagues without disturbing the league path.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from functools import cached_property
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Gender = Literal["men", "women"]
TeamLevel = Literal["senior", "second", "youth"]
Surface = Literal["grass", "artificial", "hybrid"]
CompetitionFormat = Literal["league", "cup", "european"]

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Rough bounding box for mainland Norway, used by the data validator to catch
# swapped or mistyped coordinates.
NORWAY_LAT_RANGE = (57.0, 72.0)
NORWAY_LON_RANGE = (4.0, 32.0)


def _validate_hh_mm(v: str | None) -> str | None:
    """Shared 24h `HH:MM` check for every kickoff-time field."""
    if v is None:
        return v
    hour, sep, minute = v.partition(":")
    if sep != ":" or not (hour.isdigit() and minute.isdigit()):
        raise ValueError(f"kickoff_time {v!r} must be 24h HH:MM")
    if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
        raise ValueError(f"kickoff_time {v!r} must be 24h HH:MM")
    return v


class Venue(BaseModel):
    id: str
    name: str
    city: str
    lat: float
    lon: float
    capacity: int
    surface: Surface = "grass"


class Team(BaseModel):
    id: str
    gender: Gender
    level: TeamLevel = "senior"
    home_venue: str

    # Filled in by the loader once the owning club is known, so a Team can be
    # passed around on its own without dragging the club along.
    club_id: str = ""


class Club(BaseModel):
    id: str
    name: str
    short_name: str
    city: str
    # Fills the club's team markers in the report. One colour per club, not per
    # team, so a dual club's men's and women's markers always match.
    color: str
    teams: list[Team]

    @field_validator("color")
    @classmethod
    def _color_is_hex(cls, v: str) -> str:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            raise ValueError(f"club color {v!r} must be a 6-digit hex code, e.g. '#c0392b'")
        return v.lower()


RoundGranularity = Literal["week", "month", "quarter"]


class _DateSpec(BaseModel):
    """Shared mechanics for a date that may still be imprecise (#30).

    A near-term date may be confirmed (`forced_date`); a distant one is known
    only to the week, month or quarter (`window_start`/`window_end`, with
    `granularity` recording which). Exactly one of the two must be set, so
    narrowing a window once the real date is announced is a data edit.

    Carries no identity of its own: `_ScheduledRound` adds that for a whole
    round, while `EuropeanLeg` uses this bare — a leg is "first" or "second"
    within its `EuropeanRound`.
    """

    forced_date: date | None = None
    window_start: date | None = None
    window_end: date | None = None
    granularity: RoundGranularity | None = None

    @model_validator(mode="after")
    def _forced_xor_window(self) -> _DateSpec:
        has_forced = self.forced_date is not None
        has_window = self.window_start is not None or self.window_end is not None
        if has_forced and has_window:
            raise ValueError("set forced_date OR a window, not both")
        if not has_forced and not has_window:
            raise ValueError("needs either forced_date or a window")
        if has_window and (self.window_start is None or self.window_end is None):
            raise ValueError("a window needs both window_start and window_end")
        if has_window and self.window_end < self.window_start:
            raise ValueError(
                f"window_end ({self.window_end}) is before window_start ({self.window_start})"
            )
        return self

    @property
    def is_forced(self) -> bool:
        return self.forced_date is not None

    @property
    def earliest(self) -> date:
        """The earliest date this could conceivably land on."""
        return self.forced_date if self.forced_date is not None else self.window_start

    @property
    def latest(self) -> date:
        """The latest date this could conceivably land on."""
        return self.forced_date if self.forced_date is not None else self.window_end


class _ScheduledRound(_DateSpec):
    """`_DateSpec` plus the identity a whole round needs.

    One date (or window) for the round, unlike `EuropeanRound`, which needs
    one per leg of a two-legged tie.
    """

    id: str
    name: str
    note: str = ""


class CupRound(_ScheduledRound):
    """One round of a knockout cup: when it may be played, not who plays whom.

    Pairings are drawn round by round, so this only describes *when*;
    `rounds/cup_schedule.py` resolves it to a per-team date.
    """


class EuropeanLeg(_DateSpec):
    """One leg's date (or window) within a two-legged `EuropeanRound`.

    UEFA schedules first legs on one window and second legs on another — two
    separate dates, not one span covering both, so the domestic scheduler need
    only clear each leg date plus `min_rest_days`, leaving the days between
    free for a league match (a Thu-Sun-Thu week).
    """


EuropeanHomeLeg = Literal["first", "second"]


class EuropeanTie(BaseModel):
    """One entrant's specific two-legged tie within a `EuropeanRound`.

    More than one of a competition's teams can enter the same round, each
    against a different opponent, so opponent and home/away are per-tie.
    `opponent` stays `"TBD"` while it depends on another fixture in the draw
    (#32), and `home_leg` is `None` when even that isn't settled.
    """

    team: str
    opponent: str = "TBD"
    home_leg: EuropeanHomeLeg | None = None


class EuropeanRound(BaseModel):
    """One qualifying round of a UEFA competition, for the Norwegian team(s)
    entered in it (#29, #32).

    Unlike a cup round, not every team on the competition enters every round —
    a Champions League runner-up enters at the third qualifying round while
    the champion enters at the play-off — so `ties` names which of
    `Competition.teams` play this one.

    `drop_to_competition`/`drop_to_round` model the cascade: where an entrant
    losing here lands instead. Both are `None` when a loss has no further
    *qualifying* round to model — the team goes straight into a league phase
    (out of scope), or the hop simply isn't wired yet.

    Outcomes are unknown ahead of time, so `rounds/european_schedule.py` picks
    no branch: it blocks every reachable leg date at once. Resolving a
    conditional once results are in is a data edit — delete the rounds on the
    branch not taken, or narrow a surviving leg to a `forced_date`.
    """

    id: str
    name: str
    first_leg: EuropeanLeg
    second_leg: EuropeanLeg
    ties: list[EuropeanTie] = Field(default_factory=list)
    note: str = ""
    drop_to_competition: str | None = None
    drop_to_round: str | None = None

    @model_validator(mode="after")
    def _drop_to_is_paired(self) -> EuropeanRound:
        has_competition = self.drop_to_competition is not None
        has_round = self.drop_to_round is not None
        if has_competition != has_round:
            raise ValueError(
                f"european round {self.id!r}: drop_to_competition and drop_to_round must be "
                f"set together, or not at all"
            )
        return self

    @property
    def entrants(self) -> list[str]:
        return [tie.team for tie in self.ties]

    @property
    def earliest(self) -> date:
        """The earliest date this round (either leg) could conceivably land on."""
        return min(self.first_leg.earliest, self.second_leg.earliest)

    @property
    def latest(self) -> date:
        """The latest date this round (either leg) could conceivably land on."""
        return max(self.first_leg.latest, self.second_leg.latest)


class EuropeanMatchday(_ScheduledRound):
    """One league-phase matchday of a UEFA main tournament (#79) — a single
    date (or window), like a `CupRound`, since a matchday is one fixture per
    team rather than a two-legged tie."""


class MainTournamentRound(BaseModel):
    """One knockout round of a UEFA main tournament, following the league
    phase (#79).

    Two legs, like `EuropeanRound`, except the final: `second_leg` is `None`
    for a single-match round, and `venue_name` then names where it's played,
    since UEFA fixes a final's venue years ahead of either finalist. That
    venue is freeform text, not a `Venue.id` — it's almost always outside
    Norway, and there's no reason to carry foreign stadium coordinates just to
    print a name.
    """

    id: str
    name: str
    first_leg: EuropeanLeg
    second_leg: EuropeanLeg | None = None
    venue_name: str | None = None
    note: str = ""

    @model_validator(mode="after")
    def _venue_only_for_single_match(self) -> MainTournamentRound:
        if self.venue_name is not None and self.second_leg is not None:
            raise ValueError(
                f"main tournament round {self.id!r}: venue_name is only meaningful for a "
                f"single-match round (omit second_leg) — a two-legged tie has no one shared venue"
            )
        return self

    @property
    def earliest(self) -> date:
        """The earliest date this round could conceivably land on."""
        if self.second_leg is None:
            return self.first_leg.earliest
        return min(self.first_leg.earliest, self.second_leg.earliest)

    @property
    def latest(self) -> date:
        """The latest date this round could conceivably land on."""
        if self.second_leg is None:
            return self.first_leg.latest
        return max(self.first_leg.latest, self.second_leg.latest)


class TvTimeSpread(BaseModel):
    """Desired TV-broadcast kickoff shape for a competition's round: on the
    preferred weekday most matches sit at `primary_kickoff_time`, with one
    shifted early and one late.

    Opt-in via `Competition.tv_time_spread`; `None` there leaves kickoff times
    to `Competition.kickoff_slots`.
    """

    primary_kickoff_time: str = "17:00"
    early_kickoff_time: str = "14:30"
    late_kickoff_time: str = "19:15"

    @field_validator("primary_kickoff_time", "early_kickoff_time", "late_kickoff_time")
    @classmethod
    def _times_are_hh_mm(cls, v: str) -> str:
        _validate_hh_mm(v)
        return v


class Competition(BaseModel):
    """A league or a cup. `format` discriminates the two.

    A league's fixtures are generated and dated by the solver; a cup's rounds
    are real-world fixed dates the solver schedules around.
    """

    id: str
    name: str
    season: int
    gender: Gender
    format: CompetitionFormat = "league"

    # Short label used everywhere the report is cramped; headings and the
    # legend fall back to `name`. `None` until set, which the loader requires
    # of the shipped data.
    short_name: str | None = None

    # This competition's colour for the report's dots and tags (#77). Declared in
    # the data rather than cycled through a palette, so the mapping stays
    # stable across renders and the loader can catch two competitions sharing
    # one. `None` falls back to a neutral grey.
    color: str | None = None

    @field_validator("color")
    @classmethod
    def _color_is_hex(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            raise ValueError(f"competition color {v!r} must be a 6-digit hex code, e.g. '#c0392b'")
        return v.lower()

    # Whether the solver may place this competition's fixtures, or must
    # schedule *around* its given dates (#31). Explicit rather than inferred
    # from `format` so the flag is visible in the data itself.
    movable: bool = True

    rounds_per_pairing: int = 2
    team_count: int
    teams: list[str]

    # Narrower fixture window than the season's outer envelope, e.g. Toppserien
    # finishing well before Eliteserien. None uses the season's start/end.
    start: date | None = None
    end: date | None = None

    preferred_weekday: Weekday = "sunday"
    # Days strictly between two of a team's matchdays, counting neither:
    # Thursday to Sunday is two.
    min_rest_days: int = 2
    match_window_days: int = 3
    comfortable_rest_days: int = 5
    weights: dict[str, float] = Field(default_factory=dict)

    # The whole final round plays at this one time, so no team gains an edge
    # from kicking off after its rivals. Forced by `resolve_round_pins` and
    # checked by `FinalRoundSameSlot`.
    final_round_kickoff_time: str = "18:00"

    # Candidate kickoff times for a non-final-round match, earliest first.
    kickoff_slots: list[str] = Field(
        default_factory=lambda: ["14:00", "18:00", "20:00"]
    )

    @field_validator("final_round_kickoff_time")
    @classmethod
    def _final_round_kickoff_time_is_hh_mm(cls, v: str) -> str:
        _validate_hh_mm(v)
        return v

    @field_validator("kickoff_slots")
    @classmethod
    def _kickoff_slots_are_hh_mm(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("kickoff_slots must not be empty")
        for slot in v:
            _validate_hh_mm(slot)
        return v

    # Opt-in TV-broadcast kickoff pattern (#76); `None` leaves kickoff
    # assignment to `kickoff_slots`.
    tv_time_spread: TvTimeSpread | None = None

    # Cup-only: the real-world rounds this competition's teams are entered
    # into, in playing order.
    cup_rounds: list[CupRound] = Field(default_factory=list)

    # European-only: the UEFA qualifying rounds this competition's Norwegian
    # entrant(s) play, in playing order (#29). Empty for a main tournament,
    # which uses `league_phase_matchdays`/`knockout_rounds` instead.
    european_rounds: list[EuropeanRound] = Field(default_factory=list)

    # Separates a UEFA competition's qualifying rounds from its main
    # tournament (#79). Explicit rather than inferred from which field group
    # is populated, same reasoning as `movable`.
    is_main_tournament: bool = False

    # Main-tournament-only: every league-phase matchday reachable entrants
    # (see `reachable_from`) could be drawn into.
    league_phase_matchdays: list[EuropeanMatchday] = Field(default_factory=list)

    # Main-tournament-only: the knockout rounds following the league phase, in
    # playing order.
    knockout_rounds: list[MainTournamentRound] = Field(default_factory=list)

    # Main-tournament-only: which qualifying competitions' entrants can reach
    # this one (#79). Every entrant of every listed competition is assumed able to
    # reach the final — the same "assume it goes all the way" simplification
    # `rounds/cup_schedule.py` makes, since where a qualifying entrant lands
    # isn't known until results are in. Coarser than UEFA's round-by-round
    # drop rule; `docs/european_qualifiers_plan.md` has the follow-up.
    #
    # Only wire a hop the qualifying data itself wires via
    # `drop_to_competition`/`drop_to_round`. Champions League qualifying wires
    # none deliberately: listing it blocks an entrant's dates across every
    # reachable tournament's calendar at once, which put a feasible schedule
    # out of reach of `cli.py generate`'s time budget.
    reachable_from: list[str] = Field(default_factory=list)

    @property
    def min_gap_days(self) -> int:
        """Calendar days required between two matches: `min_rest_days` full
        rest days, plus the two matchdays themselves that bookend them."""
        return self.min_rest_days + 1

    @property
    def comfortable_gap_days(self) -> int:
        """Calendar-day equivalent of `comfortable_rest_days`, on the same
        matchday-inclusive footing as `min_gap_days`."""
        return self.comfortable_rest_days + 1

    @field_validator("rounds_per_pairing")
    @classmethod
    def _at_least_one_round(cls, v: int) -> int:
        if v < 1:
            raise ValueError("rounds_per_pairing must be at least 1")
        return v

    @model_validator(mode="after")
    def _window_is_ordered(self) -> Competition:
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError(f"{self.id}: end ({self.end}) is before start ({self.start})")
        return self

    @model_validator(mode="after")
    def _non_league_is_not_movable(self) -> Competition:
        """A cup or European competition claiming `movable: true` is a data
        error: the solver never dates its fixtures, so the flag would be
        lying. A league may still set `movable: false` deliberately."""
        if self.format != "league" and self.movable:
            raise ValueError(
                f"{self.id}: a {self.format} competition cannot be movable — the solver never "
                f"generates or dates its fixtures, so set movable: false"
            )
        return self

    @property
    def rounds(self) -> int:
        """Total rounds — (n-1) per leg for an even league, n for an odd one
        (bye rounds); for a cup or European competition, the number of
        real-world rounds tracked."""
        if self.format == "cup":
            return len(self.cup_rounds)
        if self.format == "european":
            return (
                len(self.european_rounds)
                + len(self.league_phase_matchdays)
                + len(self.knockout_rounds)
            )
        n = self.team_count
        per_leg = n - 1 if n % 2 == 0 else n
        return per_leg * self.rounds_per_pairing

    @property
    def matches_per_leg(self) -> int:
        return self.team_count * (self.team_count - 1) // 2

    @property
    def total_matches(self) -> int:
        """League only: a cup's or European competition's opponents are drawn
        rather than modelled as fixtures, so this is 0 for them."""
        if self.format != "league":
            return 0
        return self.matches_per_leg * self.rounds_per_pairing


class DatedNote(BaseModel):
    """A date carrying a human-readable reason, for blackouts and the like."""

    date: date
    reason: str = ""


class VenueBlackout(BaseModel):
    venue: str
    date: date
    reason: str = ""


class _DateRangeNote(BaseModel):
    """A closed span of blackout dates — the multi-day counterpart to
    `DatedNote`'s single date."""

    start: date
    end: date
    reason: str = ""

    @model_validator(mode="after")
    def _end_not_before_start(self) -> _DateRangeNote:
        if self.end < self.start:
            raise ValueError(f"date range end ({self.end}) is before start ({self.start})")
        return self

    @property
    def dates(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]


class GlobalBlackoutRange(_DateRangeNote):
    """A closed span of dates off-limits season-wide — holiday periods, FIFA
    international breaks. `Season.blacked_out_dates` expands it into the same
    date->reason mapping every global-blackout consumer reads."""


class VenueBlackoutRange(_DateRangeNote):
    """A closed span of dates one venue is unavailable — a concert run, ground
    works spanning a week. Only venue-scoped consumers see it; cup and
    European round resolution book no venues, so they never consult it."""

    venue: str


class FullRoundRequirement(BaseModel):
    """Every team in a competition must have a match on this date — May 16 in
    Eliteserien is a full round, not just the marquee fixtures a
    `FixedRequirement` can pin.

    `hard=True` makes it a solver constraint (`FullRoundOnDate`);
    `resolve_round_pins` forces the nearest round onto the date.
    """

    id: str
    date: date
    competition: str
    hard: bool = True
    reason: str = ""


class RivalryFixture(BaseModel):
    """A specific pairing that should land on a fixed date, home side
    alternating by year parity.

    Scored as a preference by `RivalryFixtureOnDate` — nothing pins the
    pairing to the date the way a hard requirement would.
    """

    id: str
    date: date
    competition: str
    team_even_year_home: str
    team_odd_year_home: str
    weight: float = 20.0
    reason: str = ""


class FixedRequirement(BaseModel):
    """A date that must carry a match, with a named team at home.

    `hard=True` makes it a constraint the solver must satisfy; `hard=False`
    demotes it to a scored preference.
    """

    id: str
    date: date
    home_team: str
    competition: str
    hard: bool = True
    reason: str = ""
    weight: float = 50.0

    # Copied onto the winning `Match` by `assign_kickoff_times`, for
    # requirements like Tromsø's Midnight Sun Match where the late kickoff is
    # the whole point.
    kickoff_time: str | None = None

    @field_validator("kickoff_time")
    @classmethod
    def _kickoff_time_is_hh_mm(cls, v: str | None) -> str | None:
        return _validate_hh_mm(v)


class Season(BaseModel):
    # Coerced to str so `id: 2026` in YAML — the natural way to write it —
    # loads without ceremony.
    id: str
    year: int

    @field_validator("id", mode="before")
    @classmethod
    def _id_to_str(cls, v: object) -> str:
        return str(v)
    start: date
    end: date
    competitions: list[str]
    # Cup competitions, kept out of `competitions` because their rounds are
    # fixed real-world dates rather than something to schedule. A cup round
    # may fall outside `start`..`end` — the 2027 Norwegian Cup starts in
    # August 2026 — which is expected, not a data error.
    cup_competitions: list[str] = Field(default_factory=list)
    # UEFA qualifying competitions, separate for the same reason: resolved
    # once up front, never fed to the solver pipeline.
    european_competitions: list[str] = Field(default_factory=list)
    global_blackouts: list[DatedNote] = Field(default_factory=list)
    discouraged_dates: list[DatedNote] = Field(default_factory=list)
    venue_blackouts: list[VenueBlackout] = Field(default_factory=list)
    global_blackout_ranges: list[GlobalBlackoutRange] = Field(default_factory=list)
    venue_blackout_ranges: list[VenueBlackoutRange] = Field(default_factory=list)
    fixed_requirements: list[FixedRequirement] = Field(default_factory=list)
    full_round_requirements: list[FullRoundRequirement] = Field(default_factory=list)
    rivalry_fixtures: list[RivalryFixture] = Field(default_factory=list)

    @cached_property
    def blacked_out_dates(self) -> dict[date, str]:
        """Every date blacked out season-wide, mapped to a reason: single
        dates plus every day inside a range. The one place every
        global-blackout consumer reads, so a multi-day exclusion behaves
        exactly like a run of single-day ones.

        Cached because `BlackoutDates.evaluate` runs in the annealer's inner
        loop and a season is never mutated after loading.
        """
        result: dict[date, str] = {}
        for excluded in self.global_blackout_ranges:
            for day in excluded.dates:
                result[day] = excluded.reason
        for blackout in self.global_blackouts:
            result[blackout.date] = blackout.reason
        return result

    @cached_property
    def venue_blacked_out_dates(self) -> dict[str, dict[date, str]]:
        """Every (venue, date) blacked out, keyed by venue then mapped to a
        reason. `blacked_out_dates`'s venue-scoped counterpart: cup and
        European round resolution book no venues and never read this."""
        result: dict[str, dict[date, str]] = {}
        for excluded in self.venue_blackout_ranges:
            for day in excluded.dates:
                result.setdefault(excluded.venue, {})[day] = excluded.reason
        for blackout in self.venue_blackouts:
            result.setdefault(blackout.venue, {})[blackout.date] = blackout.reason
        return result


class TravelOverride(BaseModel):
    a: str
    b: str
    # None means "no reasonable surface connection" — treated as infinite.
    hours: float | None = None
    note: str = ""


# -- scheduling artefacts ---------------------------------------------------


class Fixture(BaseModel):
    """An unscheduled pairing: who plays whom, in which leg and round."""

    competition_id: str
    home_team: str
    away_team: str
    leg: int
    round_index: int

    @property
    def key(self) -> str:
        return f"{self.competition_id}:{self.round_index}:{self.home_team}-{self.away_team}"


class Match(BaseModel):
    """A fixture placed on the calendar."""

    competition_id: str
    home_team: str
    away_team: str
    leg: int
    round_index: int
    date: date
    venue: str

    # Not searched — filled in by `assign_kickoff_times` once dates are fixed,
    # so it is `None` on a freshly built `Match`.
    kickoff_time: str | None = None

    @field_validator("kickoff_time")
    @classmethod
    def _kickoff_time_is_hh_mm(cls, v: str | None) -> str | None:
        return _validate_hh_mm(v)

    @property
    def key(self) -> str:
        return f"{self.competition_id}:{self.round_index}:{self.home_team}-{self.away_team}"
