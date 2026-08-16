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
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Gender = Literal["men", "women"]
TeamLevel = Literal["senior", "second", "youth"]
Surface = Literal["grass", "artificial", "hybrid"]
CompetitionFormat = Literal["league", "cup"]

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


class CupRound(BaseModel):
    """One round of a knockout cup: when it may be played, not who plays whom.

    Real-world cup rounds are announced at wildly different notice: a near-term
    round may already have a confirmed date (`forced_date`), while a distant
    one is only known to the week, month or quarter (`window_start`/
    `window_end`, with `granularity` recording which). Exactly one of the two
    must be set — never both, never neither.

    Pairings are drawn round by round and are not known ahead of time, so this
    only ever describes *when* a round falls; `terminliste/rounds/cup_schedule.py`
    resolves it to an actual per-team date, honouring `forced_date` exactly and
    picking a date inside the window otherwise.
    """

    id: str
    name: str
    forced_date: date | None = None
    window_start: date | None = None
    window_end: date | None = None
    granularity: RoundGranularity | None = None
    note: str = ""

    @model_validator(mode="after")
    def _forced_xor_window(self) -> "CupRound":
        has_forced = self.forced_date is not None
        has_window = self.window_start is not None or self.window_end is not None
        if has_forced and has_window:
            raise ValueError(f"cup round {self.id!r}: set forced_date OR a window, not both")
        if not has_forced and not has_window:
            raise ValueError(f"cup round {self.id!r}: needs either forced_date or a window")
        if has_window and (self.window_start is None or self.window_end is None):
            raise ValueError(f"cup round {self.id!r}: a window needs both window_start and window_end")
        if has_window and self.window_end < self.window_start:
            raise ValueError(
                f"cup round {self.id!r}: window_end ({self.window_end}) is before "
                f"window_start ({self.window_start})"
            )
        return self

    @property
    def is_forced(self) -> bool:
        return self.forced_date is not None

    @property
    def earliest(self) -> date:
        """The earliest date this round could conceivably land on."""
        return self.forced_date if self.forced_date is not None else self.window_start

    @property
    def latest(self) -> date:
        """The latest date this round could conceivably land on."""
        return self.forced_date if self.forced_date is not None else self.window_end


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
    min_rest_days: int = 3
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

    # Cup-only: the real-world rounds this competition's teams are entered
    # into, in the order they are played. Empty for a league.
    cup_rounds: list[CupRound] = Field(default_factory=list)

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

    @property
    def rounds(self) -> int:
        """Total rounds.

        League: (n-1) per leg for even n, n for odd n (bye rounds). Cup: the
        number of real-world rounds its teams are entered into.
        """
        if self.format == "cup":
            return len(self.cup_rounds)
        n = self.team_count
        per_leg = n - 1 if n % 2 == 0 else n
        return per_leg * self.rounds_per_pairing

    @property
    def matches_per_leg(self) -> int:
        return self.team_count * (self.team_count - 1) // 2

    @property
    def total_matches(self) -> int:
        """League only — a cup's pairings are drawn round by round and are
        not modelled as fixtures, so this is 0 for `format == "cup"`."""
        if self.format == "cup":
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
    global_blackouts: list[DatedNote] = Field(default_factory=list)
    discouraged_dates: list[DatedNote] = Field(default_factory=list)
    venue_blackouts: list[VenueBlackout] = Field(default_factory=list)
    fixed_requirements: list[FixedRequirement] = Field(default_factory=list)
    full_round_requirements: list[FullRoundRequirement] = Field(default_factory=list)
    rivalry_fixtures: list[RivalryFixture] = Field(default_factory=list)


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
