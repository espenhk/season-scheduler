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
    teams: list[Team]


class Competition(BaseModel):
    """A league. `format` discriminates so cups can be added as a sibling."""

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
    comfortable_rest_days: int = 6
    weights: dict[str, float] = Field(default_factory=dict)

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
        """Total rounds: (n-1) per leg for even n, n for odd n (bye rounds)."""
        n = self.team_count
        per_leg = n - 1 if n % 2 == 0 else n
        return per_leg * self.rounds_per_pairing

    @property
    def matches_per_leg(self) -> int:
        return self.team_count * (self.team_count - 1) // 2

    @property
    def total_matches(self) -> int:
        return self.matches_per_leg * self.rounds_per_pairing


class DatedNote(BaseModel):
    """A date carrying a human-readable reason, for blackouts and the like."""

    date: date
    reason: str = ""


class VenueBlackout(BaseModel):
    venue: str
    date: date
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
    global_blackouts: list[DatedNote] = Field(default_factory=list)
    discouraged_dates: list[DatedNote] = Field(default_factory=list)
    venue_blackouts: list[VenueBlackout] = Field(default_factory=list)
    fixed_requirements: list[FixedRequirement] = Field(default_factory=list)


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

    @property
    def key(self) -> str:
        return f"{self.competition_id}:{self.round_index}:{self.home_team}-{self.away_team}"
