"""Pydantic models mirroring the YAML files under /data/.

Pure data containers — no scheduling logic, no I/O. `loader.py` reads the YAML
into these and cross-checks referential integrity; everything downstream works
off the validated result and never touches the raw YAML.

The shape here is deliberately wider than the first version needs. `Team.level`
exists so reserve sides are a data edit rather than a refactor, and
`Competition.format` is a discriminator so cups can be added alongside leagues
without disturbing the league path.
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
    # Primary club colour, used to fill/stroke that club's team markers in the
    # report (square for a home fixture, diamond for an away one) so a reader
    # can recognise a club by colour the way they would on a real matchday
    # graphic. One colour per club, not per team, so a dual club's men's and
    # women's markers always match.
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
    """Shared forced-date-XOR-window mechanics — issue #30's "vague/imprecise
    game dates" mechanism.

    A near-term date may already be confirmed (`forced_date`); a distant one
    is only known to the week, month or quarter (`window_start`/
    `window_end`, with `granularity` recording which). Exactly one of the
    two must be set — never both, never neither — and narrowing a window
    down to a `forced_date` once the real date is announced is a data edit,
    not a re-model. Deliberately carries no identity of its own (no `id`,
    `name`) — `_ScheduledRound` adds that for a whole round; `EuropeanLeg`
    uses this bare, since a leg's identity is "first" or "second" within its
    `EuropeanRound`, not something it needs to name itself.
    """

    forced_date: date | None = None
    window_start: date | None = None
    window_end: date | None = None
    granularity: RoundGranularity | None = None

    @model_validator(mode="after")
    def _forced_xor_window(self) -> "_DateSpec":
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
    """`_DateSpec` plus the identity a whole round needs: `id`, `name`, and
    an optional human `note`. `CupRound`'s shape — a cup round has one date
    (or window) for the whole round, unlike `EuropeanRound`, which needs two
    (`EuropeanLeg`, one per leg of a two-legged tie). `_DateSpec`'s own
    forced-XOR-window validator is inherited as-is; it doesn't know about
    `id`, but its message is still clear enough without one, and pydantic
    reports which model failed regardless."""

    id: str
    name: str
    note: str = ""


class CupRound(_ScheduledRound):
    """One round of a knockout cup: when it may be played, not who plays whom.

    Pairings are drawn round by round and are not known ahead of time, so this
    only ever describes *when* a round falls; `terminliste/rounds/cup_schedule.py`
    resolves it to an actual per-team date, honouring `forced_date` exactly and
    picking a date inside the window otherwise.
    """


class EuropeanLeg(_DateSpec):
    """One leg's date (or window) within a two-legged `EuropeanRound`.

    UEFA schedules a qualifying round's first legs on one pairing window
    (e.g. "4/5 August") and its second legs on another ("11 August") — two
    genuinely separate dates, not one span covering both, which is what
    `EuropeanRound` used to model before this was split out. Resolving a
    `EuropeanLeg` to a single date is `terminliste/rounds/european_schedule.py`'s
    job (mirroring `rounds/cup_schedule.py`'s own forced/window resolution);
    the domestic scheduler then only needs to stay clear of that one date
    plus `min_rest_days`, not an entire span between the two legs — freeing
    up the days in between for a league match, e.g. a Thu-Sun-Thu week.
    """


EuropeanHomeLeg = Literal["first", "second"]


class EuropeanTie(BaseModel):
    """One entrant's specific two-legged tie within a `EuropeanRound`.

    More than one of a competition's teams can enter the same round (Norway
    had two Champions League play-off entrants in 2026-27, each against a
    different opponent), so opponent and home/away are per-tie, not
    per-round. `opponent` defaults to `"TBD"` for a tie whose own opponent
    is still conditional on another, unrelated fixture elsewhere in the
    draw — same spirit as issue #32's conditional fixtures, just describing
    who's on the other side of the tie rather than which round is played at
    all. `home_leg` is `None` when even that isn't settled yet.
    """

    team: str
    opponent: str = "TBD"
    home_leg: EuropeanHomeLeg | None = None


class EuropeanRound(BaseModel):
    """One qualifying round of a UEFA competition (Champions/Europa/Conference
    League), for the Norwegian team(s) entered in it.

    Built for issue #29 (the CL/EL/UECL qualifying cascade) and issue #32
    (conditional fixtures): unlike a domestic cup round, not every team
    listed on the competition necessarily enters every round — Norway's
    Champions League runner-up enters at the third qualifying round while
    the champion enters directly at the play-off round, for instance — so
    `ties` names exactly which of `Competition.teams` play this particular
    round (`entrants` is the plain team-id list derived from it).

    `drop_to_competition`/`drop_to_round` model the cascade: if an entrant
    loses this round, that pair names the round (in a different, lower
    competition) they drop into instead — e.g. a Champions League
    Champions-Path third-qualifying-round loss drops into that season's
    Europa League play-off round. Both are `None` when a loss here has no
    further *qualifying* round to model, either because the real rule sends
    the team straight into a competition's league phase (many matchdays,
    not a single round — out of scope for this project, which only tracks
    qualifying) or because this project simply hasn't sketched that hop yet
    (see `data/competitions/europa_league_2026.yml`'s header for exactly
    which hops are wired and which are left as a documented gap).

    Because the outcome of a round is unknown ahead of time,
    `terminliste/rounds/european_schedule.py` does not pick a branch: it
    walks every reachable round from a team's entry point and blocks every
    leg date reachable at once, so the domestic scheduler stays clear of a
    European commitment regardless of which branch of the cascade actually
    happens. Once a real result is known, resolving the conditional is a
    data edit — delete the round(s) on the branch not taken (or narrow a
    surviving leg's window to a `forced_date`) and regenerate; see the
    module docstring for the mechanics.
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
    def _drop_to_is_paired(self) -> "EuropeanRound":
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


class TvTimeSpread(BaseModel):
    """Desired TV-broadcast kickoff shape for a competition's round (issue
    #76): on the preferred weekday, most matches sit at `primary_kickoff_time`,
    with one shifted to `early_kickoff_time` and one to `late_kickoff_time`.

    Opt-in via `Competition.tv_time_spread` — `None` there leaves every match's
    kickoff time to `Competition.kickoff_slots` as before. Applied by
    `rounds/kickoff.py::assign_kickoff_times` and scored by
    `scoring/soft.py::TvTimeSpread`.
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

    A league's fixtures are generated and dated by the solver. A cup's rounds
    are real-world fixed dates (`cup_rounds`) that the solver treats as given
    rather than something to search over — see `cup_rounds` and
    `CupRoundConflict` in `scoring/hard.py`.
    """

    id: str
    name: str
    season: int
    gender: Gender
    format: CompetitionFormat = "league"

    # This competition's short label — the season report falls back to
    # `name` in the by-competition heading and the calendar legend, but
    # every cramped spot (the month calendar's day detail, a fixture card's
    # competition tag, the combined-list table) uses this instead. `None`
    # for a competition that hasn't set one yet; `_validate_competition_short_names`
    # in loader.py requires the shipped data to set one.
    short_name: str | None = None

    # Issue #77: this competition's own colour for the season report's dots
    # and tags — a report-relevant fact declared in the data, not inferred or
    # cycled through a fixed palette, so the mapping from colour to
    # competition is stable across renders and the loader can catch two
    # competitions accidentally sharing one (see `_validate_competition_colors`
    # in loader.py). `None` for a competition that hasn't set one yet; the
    # report falls back to a neutral grey until it does.
    color: str | None = None

    @field_validator("color")
    @classmethod
    def _color_is_hex(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            raise ValueError(f"competition color {v!r} must be a 6-digit hex code, e.g. '#c0392b'")
        return v.lower()

    # Issue #31: whether the solver is free to place this competition's
    # fixtures (`True`, the league default) or must treat its dates as a
    # given to schedule *around* (`False` — every cup and european
    # competition; see `README.md`'s "Data model" section). Explicit rather
    # than inferred from `format` so the flag is visible in the data itself,
    # even though in practice every `format != "league"` competition sets
    # it `False`.
    movable: bool = True

    rounds_per_pairing: int = 2
    team_count: int
    teams: list[str]

    # A competition's own fixture window, when it runs narrower than the
    # season's outer envelope (e.g. Toppserien finishing well before
    # Eliteserien). None means "use the season's start/end" — the common case
    # when every competition in a season shares one calendar.
    start: date | None = None
    end: date | None = None

    preferred_weekday: Weekday = "sunday"
    # Full days off between two of a team's matches — the days strictly
    # between the two matchdays, counting neither of them. Thursday to Sunday
    # is two (Friday, Saturday) and legal at the default setting.
    min_rest_days: int = 2
    match_window_days: int = 3
    comfortable_rest_days: int = 5
    weights: dict[str, float] = Field(default_factory=dict)

    # The whole final round — every match of the last round of the last leg —
    # is forced onto one date, at this one kickoff time, by `resolve_round_pins`
    # (see `rounds/greedy.py`) and checked by `FinalRoundSameSlot`
    # (`scoring/hard.py`). Real leagues play the last round simultaneously so
    # no team has a competitive edge from kicking off after its rivals.
    final_round_kickoff_time: str = "18:00"

    # The candidate kickoff times a non-final-round match may be assigned
    # (`rounds/kickoff.py::assign_kickoff_times`), earliest first. Only
    # meaningful together with `late_kickoff_long_travel` in `scoring/soft.py`.
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

    # Issue #76: opt-in TV-broadcast kickoff pattern for a round's matches on
    # the preferred weekday — see `TvTimeSpread` above. `None` (the default)
    # leaves kickoff assignment to `kickoff_slots`.
    tv_time_spread: TvTimeSpread | None = None

    # Cup-only: the real-world rounds this competition's teams are entered
    # into, in the order they are played. Empty for a league.
    cup_rounds: list[CupRound] = Field(default_factory=list)

    # European-only: the UEFA qualifying rounds this competition's Norwegian
    # entrant(s) play, in the order they are played — see `EuropeanRound`
    # and issue #29. Empty for a league or cup.
    european_rounds: list[EuropeanRound] = Field(default_factory=list)

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
    def _window_is_ordered(self) -> "Competition":
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError(f"{self.id}: end ({self.end}) is before start ({self.start})")
        return self

    @model_validator(mode="after")
    def _non_league_is_not_movable(self) -> "Competition":
        """Makes the `movable` comment above self-policing rather than just
        descriptive: a cup or European competition claiming `movable: true`
        is a data error, not a legitimate choice — the solver never
        generates or dates either one's fixtures, so the flag would be
        lying about what actually happens. A `league` may still set
        `movable: false` deliberately (e.g. to keep it out of
        `season.competitions` while modelling it) — only the reverse is
        forbidden here."""
        if self.format != "league" and self.movable:
            raise ValueError(
                f"{self.id}: a {self.format} competition cannot be movable — the solver never "
                f"generates or dates its fixtures, so set movable: false"
            )
        return self

    @property
    def rounds(self) -> int:
        """Total rounds.

        League: (n-1) per leg for even n, n for odd n (bye rounds). Cup: the
        number of real-world rounds its teams are entered into. European:
        the number of qualifying rounds tracked, same idea as a cup.
        """
        if self.format == "cup":
            return len(self.cup_rounds)
        if self.format == "european":
            return len(self.european_rounds)
        n = self.team_count
        per_leg = n - 1 if n % 2 == 0 else n
        return per_leg * self.rounds_per_pairing

    @property
    def matches_per_leg(self) -> int:
        return self.team_count * (self.team_count - 1) // 2

    @property
    def total_matches(self) -> int:
        """League only — a cup's or European competition's pairings are not
        modelled as fixtures (opponents are drawn, or simply not this
        project's concern), so this is 0 for anything but `format ==
        "league"`."""
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
    """Shared start/end/reason mechanics for a closed span of blackout dates
    — `GlobalBlackoutRange`'s and `VenueBlackoutRange`'s common shape (issue
    #33), the multi-day counterpart to `DatedNote`'s single date.
    """

    start: date
    end: date
    reason: str = ""

    @model_validator(mode="after")
    def _end_not_before_start(self) -> "_DateRangeNote":
        if self.end < self.start:
            raise ValueError(f"date range end ({self.end}) is before start ({self.start})")
        return self

    @property
    def dates(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]


class GlobalBlackoutRange(_DateRangeNote):
    """A closed span of dates off-limits for scheduling season-wide, with a
    reason label for the overview — holiday periods, FIFA international
    breaks. A labelled, multi-day generalisation of `global_blackouts`'s
    single dates: `Season.blacked_out_dates` expands it day by day and folds
    it into the same date->reason mapping every global-blackout consumer
    reads.
    """


class VenueBlackoutRange(_DateRangeNote):
    """A closed span of dates one venue is unavailable, with a reason label
    — a multi-day generalisation of `venue_blackouts`'s single dates (a
    multi-day concert run, ground works spanning a week). Unlike
    `GlobalBlackoutRange`, this only ever reaches venue-scoped consumers
    (the candidate calendar's per-venue blocks, `BlackoutDates`'s
    venue-blackout check) — cup and European round resolution don't book
    venues at all, so they never consult it, same as `venue_blackouts`.
    """

    venue: str


class FullRoundRequirement(BaseModel):
    """Every team in a competition must have a match on this date.

    May 16 in Eliteserien: the eve of the national day is a full round, not
    just the handful of marquee fixtures a plain `FixedRequirement` can pin.
    `hard=True` makes it a constraint the solver must satisfy (see
    `FullRoundOnDate` in `scoring/hard.py`); the round nearest this date is
    forced onto it entirely by `resolve_round_pins` in `rounds/greedy.py`.
    """

    id: str
    date: date
    competition: str
    hard: bool = True
    reason: str = ""


class RivalryFixture(BaseModel):
    """A specific pairing that should land on a fixed date, home side
    alternating by year parity.

    Bodø/Glimt vs Tromsø IL on May 16: the closest thing Norwegian football
    has to a natural derby at that latitude. Scored as a preference by
    `RivalryFixtureOnDate` in `scoring/soft.py` — nothing pins the pairing to
    the date the way a hard requirement would.
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

    # Informational at the requirement level: `rounds/kickoff.py::assign_kickoff_times`
    # copies this onto the winning `Match` once dates are fixed, for
    # requirements like Tromsø's Midnight Sun Match, where the late kickoff is
    # the whole point and needs to survive into the report.
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
    # Cup competitions tied to this season, kept separate from `competitions`
    # because they are not fed to the round-robin/solver pipeline: their
    # rounds are fixed real-world dates, not something to be scheduled. A cup
    # round can fall outside `start`..`end` (the 2027 Norwegian Cup starts in
    # August 2026 and runs into the following spring) — that is expected, not
    # a data error.
    cup_competitions: list[str] = Field(default_factory=list)
    # European (UEFA qualifying) competitions tied to this season — issue
    # #29 — kept separate for the same reason as `cup_competitions`: these
    # are resolved once, up front (`rounds/european_schedule.py`), not fed
    # to the round-robin/solver pipeline.
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
        """Every individual date this season blacks out season-wide, mapped
        to a reason — `global_blackouts`'s single dates plus every day
        inside `global_blackout_ranges`, expanded. The one place every
        global-blackout consumer (the candidate calendar, `BlackoutDates`
        scoring, cup/European round resolution) reads from, so a multi-day
        exclusion behaves exactly like a run of single-day ones everywhere
        blackouts matter.

        Cached rather than recomputed: `BlackoutDates.evaluate` runs inside
        the annealer's inner loop, once per candidate move, and a season is
        loaded once and never mutated, so expanding every range (potentially
        tens of dates each) on every call would be pure waste.
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
        """Every (venue, date) this season blacks out, keyed by venue then
        mapped to a reason — `venue_blackouts`'s single dates plus every day
        inside `venue_blackout_ranges`, expanded. Mirrors `blacked_out_dates`
        but stays venue-scoped throughout: unlike a global blackout, a venue
        one never reaches cup or European round resolution, which don't book
        venues at all. Cached for the same reason `blacked_out_dates` is.
        """
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

    # Not a search variable the way `date` and `venue` are — the solvers only
    # decide dates. Filled in by `rounds/kickoff.py::assign_kickoff_times`
    # once a schedule's dates are fixed, so it is always `None` on a
    # freshly-built `Match` and only meaningful on a solver's final output.
    kickoff_time: str | None = None

    @field_validator("kickoff_time")
    @classmethod
    def _kickoff_time_is_hh_mm(cls, v: str | None) -> str | None:
        return _validate_hh_mm(v)

    @property
    def key(self) -> str:
        return f"{self.competition_id}:{self.round_index}:{self.home_team}-{self.away_team}"
