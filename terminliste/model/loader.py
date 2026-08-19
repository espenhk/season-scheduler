"""Loads /data/*.yml into a validated, in-memory `World`.

The only place that reads the data files. Everything downstream — constraints,
solvers, the report — works off the typed result.

Referential integrity is checked here rather than at use sites, so a typo in a
venue id surfaces as one clear message from `cli.py validate` instead of a
`KeyError` twenty frames deep in the solver.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .schema import (
    NORWAY_LAT_RANGE,
    NORWAY_LON_RANGE,
    Club,
    Competition,
    FixedRequirement,
    Season,
    Team,
    TravelOverride,
    Venue,
)


class DataError(Exception):
    """Raised for referential-integrity failures in the data files."""


@dataclass
class World:
    """Everything the scheduler knows about, resolved and cross-linked."""

    data_root: Path
    venues: dict[str, Venue]
    clubs: dict[str, Club]
    teams: dict[str, Team]
    competitions: dict[str, Competition]
    seasons: dict[str, Season]
    travel_overrides: list[TravelOverride] = field(default_factory=list)

    # -- lookups -----------------------------------------------------------

    def team(self, team_id: str) -> Team:
        try:
            return self.teams[team_id]
        except KeyError:
            raise DataError(f"unknown team id: {team_id!r}") from None

    def club(self, club_id: str) -> Club:
        try:
            return self.clubs[club_id]
        except KeyError:
            raise DataError(f"unknown club id: {club_id!r}") from None

    def venue(self, venue_id: str) -> Venue:
        try:
            return self.venues[venue_id]
        except KeyError:
            raise DataError(f"unknown venue id: {venue_id!r}") from None

    def competition(self, competition_id: str) -> Competition:
        try:
            return self.competitions[competition_id]
        except KeyError:
            raise DataError(f"unknown competition id: {competition_id!r}") from None

    def season(self, season_id: str) -> Season:
        try:
            return self.seasons[str(season_id)]
        except KeyError:
            raise DataError(f"unknown season id: {season_id!r}") from None

    def club_of(self, team_id: str) -> Club:
        return self.club(self.team(team_id).club_id)

    def home_venue_of(self, team_id: str) -> Venue:
        return self.venue(self.team(team_id).home_venue)

    def team_label(self, team_id: str) -> str:
        """Display name: club name, plus a qualifier when it is not the men's senior side."""
        team = self.team(team_id)
        club = self.club(team.club_id)
        if team.level != "senior":
            return f"{club.name} ({team.level})"
        if team.gender == "women":
            # Only qualify if the club also fields a men's team, otherwise the
            # club name already unambiguously means the women's side.
            if any(t.gender == "men" for t in club.teams):
                return f"{club.name} Kvinner"
        return club.name

    def team_short_label(self, team_id: str) -> str:
        """Compact display name for dense views: short name, same qualifiers
        as `team_label` but abbreviated, so it fits a calendar cell."""
        team = self.team(team_id)
        club = self.club(team.club_id)
        if team.level != "senior":
            return f"{club.short_name} ({team.level[0].upper()})"
        if team.gender == "women" and any(t.gender == "men" for t in club.teams):
            return f"{club.short_name} K"
        return club.short_name

    def teams_sharing_venue(self, venue_id: str) -> list[Team]:
        return [t for t in self.teams.values() if t.home_venue == venue_id]

    def dual_clubs(self) -> list[Club]:
        """Clubs fielding more than one senior team — the coupled ones."""
        return [
            c
            for c in self.clubs.values()
            if len([t for t in c.teams if t.level == "senior"]) > 1
        ]


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise DataError(f"missing data file: {path}")
    with path.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    if not isinstance(loaded, dict):
        raise DataError(f"{path} did not parse to a mapping")
    return loaded


def load_world(data_root: Path) -> World:
    """Read every data file under `data_root` and validate the whole graph."""
    data_root = Path(data_root)

    venues = _load_venues(data_root / "venues.yml")
    clubs, teams = _load_clubs(data_root / "clubs.yml")
    competitions = _load_competitions(data_root / "competitions")
    seasons = _load_seasons(data_root / "seasons")
    overrides = _load_travel_overrides(data_root / "travel_overrides.yml")

    world = World(
        data_root=data_root,
        venues=venues,
        clubs=clubs,
        teams=teams,
        competitions=competitions,
        seasons=seasons,
        travel_overrides=overrides,
    )
    errors = validate_world(world)
    if errors:
        raise DataError("data validation failed:\n  - " + "\n  - ".join(errors))
    return world


def _load_venues(path: Path) -> dict[str, Venue]:
    raw = _read_yaml(path)
    venues: dict[str, Venue] = {}
    for entry in raw.get("venues", []):
        venue = Venue.model_validate(entry)
        if venue.id in venues:
            raise DataError(f"duplicate venue id: {venue.id!r}")
        venues[venue.id] = venue
    if not venues:
        raise DataError(f"{path} declared no venues")
    return venues


def _load_clubs(path: Path) -> tuple[dict[str, Club], dict[str, Team]]:
    raw = _read_yaml(path)
    clubs: dict[str, Club] = {}
    teams: dict[str, Team] = {}
    for entry in raw.get("clubs", []):
        club = Club.model_validate(entry)
        if club.id in clubs:
            raise DataError(f"duplicate club id: {club.id!r}")
        for team in club.teams:
            team.club_id = club.id
            if team.id in teams:
                raise DataError(f"duplicate team id: {team.id!r}")
            teams[team.id] = team
        clubs[club.id] = club
    if not clubs:
        raise DataError(f"{path} declared no clubs")
    return clubs, teams


def _load_competitions(directory: Path) -> dict[str, Competition]:
    if not directory.is_dir():
        raise DataError(f"missing competitions directory: {directory}")
    competitions: dict[str, Competition] = {}
    for path in sorted(directory.glob("*.yml")):
        competition = Competition.model_validate(_read_yaml(path))
        if competition.id in competitions:
            raise DataError(f"duplicate competition id: {competition.id!r}")
        competitions[competition.id] = competition
    if not competitions:
        raise DataError(f"no competition files under {directory}")
    return competitions


def _load_seasons(directory: Path) -> dict[str, Season]:
    if not directory.is_dir():
        raise DataError(f"missing seasons directory: {directory}")
    seasons: dict[str, Season] = {}
    for path in sorted(directory.glob("*.yml")):
        season = Season.model_validate(_read_yaml(path))
        season.id = str(season.id)
        if season.id in seasons:
            raise DataError(f"duplicate season id: {season.id!r}")
        seasons[season.id] = season
    if not seasons:
        raise DataError(f"no season files under {directory}")
    return seasons


def _load_travel_overrides(path: Path) -> list[TravelOverride]:
    if not path.exists():
        return []
    raw = _read_yaml(path)
    return [TravelOverride.model_validate(e) for e in raw.get("overrides", [])]


def validate_world(world: World) -> list[str]:
    """Return every referential-integrity problem found, as readable strings.

    Returns rather than raises so `cli.py validate` can print the full list
    instead of stopping at the first one.
    """
    errors: list[str] = []

    for team in world.teams.values():
        if team.home_venue not in world.venues:
            errors.append(
                f"team {team.id!r} has home venue {team.home_venue!r}, which is not in venues.yml"
            )

    for venue in world.venues.values():
        lo, hi = NORWAY_LAT_RANGE
        if not lo <= venue.lat <= hi:
            errors.append(f"venue {venue.id!r} latitude {venue.lat} is outside Norway ({lo}-{hi})")
        lo, hi = NORWAY_LON_RANGE
        if not lo <= venue.lon <= hi:
            errors.append(f"venue {venue.id!r} longitude {venue.lon} is outside Norway ({lo}-{hi})")
        if venue.capacity <= 0:
            errors.append(f"venue {venue.id!r} has non-positive capacity {venue.capacity}")

    for competition in world.competitions.values():
        seen: set[str] = set()
        for team_id in competition.teams:
            if team_id not in world.teams:
                errors.append(f"competition {competition.id!r} lists unknown team {team_id!r}")
            if team_id in seen:
                errors.append(f"competition {competition.id!r} lists team {team_id!r} twice")
            seen.add(team_id)
            team = world.teams.get(team_id)
            if team is not None and team.gender != competition.gender:
                errors.append(
                    f"competition {competition.id!r} is {competition.gender} but team "
                    f"{team_id!r} is {team.gender}"
                )
        if len(competition.teams) != competition.team_count:
            errors.append(
                f"competition {competition.id!r} declares team_count={competition.team_count} "
                f"but lists {len(competition.teams)} teams"
            )
        if competition.min_rest_days < 1:
            errors.append(f"competition {competition.id!r} has min_rest_days < 1")
        errors.extend(_validate_cup_rounds(competition))
        errors.extend(_validate_european_rounds(competition))

    errors.extend(_validate_european_cascade(world))
    errors.extend(_validate_competition_colors(world))
    errors.extend(_validate_competition_short_names(world))

    for season in world.seasons.values():
        if season.end <= season.start:
            errors.append(f"season {season.id!r} ends on or before it starts")
        for competition_id in season.competitions:
            competition = world.competitions.get(competition_id)
            if competition is None:
                errors.append(f"season {season.id!r} lists unknown competition {competition_id!r}")
                continue
            if competition.format != "league":
                errors.append(
                    f"season {season.id!r} lists {competition_id!r} under competitions, but it is "
                    f"a {competition.format} — cups belong under cup_competitions"
                )
            elif not competition.movable:
                errors.append(
                    f"season {season.id!r} lists {competition_id!r} under competitions (the "
                    f"solver-generated, movable list), but it is marked movable: false"
                )
            if competition.start is not None and competition.start < season.start:
                errors.append(
                    f"competition {competition_id!r} starts {competition.start}, before "
                    f"season {season.id!r} starts {season.start}"
                )
            if competition.end is not None and competition.end > season.end:
                errors.append(
                    f"competition {competition_id!r} ends {competition.end}, after "
                    f"season {season.id!r} ends {season.end}"
                )
        for competition_id in season.cup_competitions:
            competition = world.competitions.get(competition_id)
            if competition is None:
                errors.append(
                    f"season {season.id!r} lists unknown cup competition {competition_id!r}"
                )
            elif competition.format != "cup":
                errors.append(
                    f"season {season.id!r} lists {competition_id!r} under cup_competitions, but "
                    f"it is a {competition.format} — leagues belong under competitions"
                )
        for competition_id in season.european_competitions:
            competition = world.competitions.get(competition_id)
            if competition is None:
                errors.append(
                    f"season {season.id!r} lists unknown european competition {competition_id!r}"
                )
            elif competition.format != "european":
                errors.append(
                    f"season {season.id!r} lists {competition_id!r} under european_competitions, "
                    f"but it is a {competition.format}"
                )
        for blackout in season.venue_blackouts:
            if blackout.venue not in world.venues:
                errors.append(
                    f"season {season.id!r} blacks out unknown venue {blackout.venue!r}"
                )
        errors.extend(_validate_fixed_requirements(world, season))
        errors.extend(_validate_full_round_requirements(world, season))
        errors.extend(_validate_rivalry_fixtures(world, season))
        errors.extend(_validate_calendar_capacity(world, season))

    for override in world.travel_overrides:
        for venue_id in (override.a, override.b):
            if venue_id not in world.venues:
                errors.append(f"travel override references unknown venue {venue_id!r}")

    return errors


def _validate_cup_rounds(competition: Competition) -> list[str]:
    """A cup must declare at least one round, with unique ids in a sane order.

    This is the cheap, structural check: ids are unique, and no round's whole
    possible range (`earliest`..`latest` — a forced date collapses both to the
    same day) falls entirely before the previous round's. It cannot rule out
    every infeasible case — a round's window can still be too narrow once the
    previous round's *actual* placement and the required gap between rounds
    are known — that deeper check happens when `schedule_cup` resolves the
    rounds to real dates.
    """
    errors: list[str] = []
    if competition.format != "cup":
        return errors
    if not competition.cup_rounds:
        errors.append(f"cup competition {competition.id!r} declares no cup_rounds")
        return errors

    seen_ids: set[str] = set()
    previous_round = None
    for round_ in competition.cup_rounds:
        if round_.id in seen_ids:
            errors.append(
                f"competition {competition.id!r} has duplicate cup round id {round_.id!r}"
            )
        seen_ids.add(round_.id)
        if previous_round is not None and round_.latest < previous_round.earliest:
            errors.append(
                f"competition {competition.id!r} cup round {round_.id!r} "
                f"({round_.earliest}..{round_.latest}) falls entirely before the previous "
                f"round {previous_round.id!r} ({previous_round.earliest}..{previous_round.latest})"
            )
        previous_round = round_
    return errors


def _validate_european_rounds(competition: Competition) -> list[str]:
    """Structural check for a `format == "european"` competition's rounds:
    at least one, unique ids, sane order, and every `entrants` id actually
    belongs to the competition. Mirrors `_validate_cup_rounds`; the one
    thing that check doesn't need and this one does is `entrants`, since a
    European competition's rounds don't all share the same team list the
    way a cup's do (see `EuropeanRound`)."""
    errors: list[str] = []
    if competition.format != "european":
        return errors
    if not competition.european_rounds:
        errors.append(f"european competition {competition.id!r} declares no european_rounds")
        return errors

    seen_ids: set[str] = set()
    previous_round = None
    for round_ in competition.european_rounds:
        if round_.id in seen_ids:
            errors.append(
                f"competition {competition.id!r} has duplicate european round id {round_.id!r}"
            )
        seen_ids.add(round_.id)
        seen_teams: set[str] = set()
        for tie in round_.ties:
            if tie.team not in competition.teams:
                errors.append(
                    f"competition {competition.id!r} round {round_.id!r} lists entrant "
                    f"{tie.team!r}, which is not in the competition's teams"
                )
            if tie.team in seen_teams:
                errors.append(
                    f"competition {competition.id!r} round {round_.id!r} lists {tie.team!r} "
                    f"twice"
                )
            seen_teams.add(tie.team)
        if previous_round is not None and round_.latest < previous_round.earliest:
            errors.append(
                f"competition {competition.id!r} european round {round_.id!r} "
                f"({round_.earliest}..{round_.latest}) falls entirely before the previous "
                f"round {previous_round.id!r} ({previous_round.earliest}..{previous_round.latest})"
            )
        previous_round = round_
    return errors


def _validate_european_cascade(world: World) -> list[str]:
    """Cross-competition check: every `drop_to_competition`/`drop_to_round`
    pointer names a competition and round that actually exist, and the
    target is a different (lower) competition, not the same one — a
    cascade always crosses into another UEFA competition, never loops
    within its own rounds."""
    errors: list[str] = []
    for competition in world.competitions.values():
        if competition.format != "european":
            continue
        for round_ in competition.european_rounds:
            if round_.drop_to_competition is None:
                continue
            if round_.drop_to_competition == competition.id:
                errors.append(
                    f"competition {competition.id!r} round {round_.id!r} drops into itself — "
                    f"drop_to_competition must name a different competition"
                )
                continue
            target = world.competitions.get(round_.drop_to_competition)
            if target is None:
                errors.append(
                    f"competition {competition.id!r} round {round_.id!r} drops into unknown "
                    f"competition {round_.drop_to_competition!r}"
                )
                continue
            if target.format != "european":
                errors.append(
                    f"competition {competition.id!r} round {round_.id!r} drops into "
                    f"{round_.drop_to_competition!r}, which is a {target.format}, not european"
                )
                continue
            if round_.drop_to_round not in {r.id for r in target.european_rounds}:
                errors.append(
                    f"competition {competition.id!r} round {round_.id!r} drops into "
                    f"{round_.drop_to_competition!r} round {round_.drop_to_round!r}, which does "
                    f"not exist there"
                )
    return errors


def _validate_competition_colors(world: World) -> list[str]:
    """Issue #77: every competition must declare its own report colour, and
    no two competitions may share exactly the same one — two competitions
    with the same colour would be indistinguishable as dots on the season
    report's month calendar."""
    errors: list[str] = []
    by_color: dict[str, list[str]] = defaultdict(list)
    for competition in world.competitions.values():
        if competition.color is None:
            errors.append(f"competition {competition.id!r} has no color set")
            continue
        by_color[competition.color].append(competition.id)
    for color, competition_ids in sorted(by_color.items()):
        if len(competition_ids) > 1:
            errors.append(
                f"competitions {', '.join(sorted(competition_ids))} all use color {color!r} — "
                f"give each competition its own color"
            )
    return errors


def _validate_competition_short_names(world: World) -> list[str]:
    """Every competition must declare its own short label — the season
    report's month calendar and every other cramped spot fall back to it
    instead of the (often much longer) full name, so a missing one would
    leave a cramped spot with nothing to show."""
    errors: list[str] = []
    by_short_name: dict[str, list[str]] = defaultdict(list)
    for competition in world.competitions.values():
        if competition.short_name is None:
            errors.append(f"competition {competition.id!r} has no short_name set")
            continue
        by_short_name[competition.short_name].append(competition.id)
    for short_name, competition_ids in sorted(by_short_name.items()):
        if len(competition_ids) > 1:
            errors.append(
                f"competitions {', '.join(sorted(competition_ids))} all use short_name "
                f"{short_name!r} — give each competition its own short_name"
            )
    return errors


def _validate_fixed_requirements(world: World, season: Season) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for requirement in season.fixed_requirements:
        if requirement.id in seen:
            errors.append(f"season {season.id!r} has duplicate requirement id {requirement.id!r}")
        seen.add(requirement.id)
        if requirement.home_team not in world.teams:
            errors.append(
                f"requirement {requirement.id!r} names unknown team {requirement.home_team!r}"
            )
        if requirement.competition not in world.competitions:
            errors.append(
                f"requirement {requirement.id!r} names unknown competition "
                f"{requirement.competition!r}"
            )
        elif requirement.home_team not in world.competitions[requirement.competition].teams:
            errors.append(
                f"requirement {requirement.id!r} wants {requirement.home_team!r} at home in "
                f"{requirement.competition!r}, but that team does not play in it"
            )
        window_start, window_end = season.start, season.end
        competition = world.competitions.get(requirement.competition)
        if competition is not None:
            window_start = competition.start or season.start
            window_end = competition.end or season.end
        if not window_start <= requirement.date <= window_end:
            errors.append(
                f"requirement {requirement.id!r} falls on {requirement.date}, outside the "
                f"{requirement.competition!r} window {window_start}..{window_end}"
            )
        if any(b.date == requirement.date for b in season.global_blackouts):
            errors.append(
                f"requirement {requirement.id!r} demands a match on {requirement.date}, which is "
                f"also a global blackout — these cannot both hold"
            )
    return errors


def _validate_full_round_requirements(world: World, season: Season) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for requirement in season.full_round_requirements:
        if requirement.id in seen:
            errors.append(
                f"season {season.id!r} has duplicate full-round requirement id {requirement.id!r}"
            )
        seen.add(requirement.id)
        competition = world.competitions.get(requirement.competition)
        if competition is None:
            errors.append(
                f"full-round requirement {requirement.id!r} names unknown competition "
                f"{requirement.competition!r}"
            )
            continue
        if competition.format != "league":
            errors.append(
                f"full-round requirement {requirement.id!r} names {requirement.competition!r}, "
                f"which is a {competition.format}, not a league"
            )
        window_start = competition.start or season.start
        window_end = competition.end or season.end
        if not window_start <= requirement.date <= window_end:
            errors.append(
                f"full-round requirement {requirement.id!r} falls on {requirement.date}, outside "
                f"the {requirement.competition!r} window {window_start}..{window_end}"
            )
        if any(b.date == requirement.date for b in season.global_blackouts):
            errors.append(
                f"full-round requirement {requirement.id!r} demands a round on "
                f"{requirement.date}, which is also a global blackout — these cannot both hold"
            )
    return errors


def _validate_rivalry_fixtures(world: World, season: Season) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for fixture in season.rivalry_fixtures:
        if fixture.id in seen:
            errors.append(
                f"season {season.id!r} has duplicate rivalry fixture id {fixture.id!r}"
            )
        seen.add(fixture.id)
        competition = world.competitions.get(fixture.competition)
        if competition is None:
            errors.append(
                f"rivalry fixture {fixture.id!r} names unknown competition {fixture.competition!r}"
            )
            continue
        for team_id in (fixture.team_even_year_home, fixture.team_odd_year_home):
            if team_id not in world.teams:
                errors.append(f"rivalry fixture {fixture.id!r} names unknown team {team_id!r}")
            elif team_id not in competition.teams:
                errors.append(
                    f"rivalry fixture {fixture.id!r} names {team_id!r}, which does not play in "
                    f"{fixture.competition!r}"
                )
        window_start = competition.start or season.start
        window_end = competition.end or season.end
        if not window_start <= fixture.date <= window_end:
            errors.append(
                f"rivalry fixture {fixture.id!r} falls on {fixture.date}, outside the "
                f"{fixture.competition!r} window {window_start}..{window_end}"
            )
    return errors


def _validate_calendar_capacity(world: World, season: Season) -> list[str]:
    """Catch a season window too short for the rounds it has to hold.

    Cheap arithmetic, but it turns 'the solver ran for a minute and gave up'
    into 'this season is three weeks too short'.
    """
    errors: list[str] = []
    for competition_id in season.competitions:
        competition = world.competitions.get(competition_id)
        if competition is None:
            continue
        window_start = competition.start or season.start
        window_end = competition.end or season.end
        available_days = (window_end - window_start).days + 1
        # Tight lower bound: the first round can land on day 1, and each
        # subsequent round only needs to clear min_gap_days (min_rest_days
        # full rest days, plus the two matchdays either side of them) from
        # the one before it — (rounds - 1) gaps, not `rounds` of them. The
        # previous `rounds * min_rest_days` formula overcounted by one full
        # gap and could flag a genuinely feasible season as too short.
        needed = (competition.rounds - 1) * competition.min_gap_days + 1
        if needed > available_days:
            errors.append(
                f"{competition_id!r} spans {available_days} days ({window_start}..{window_end}) "
                f"but needs at least {needed} ({competition.rounds} rounds, "
                f"{competition.min_rest_days} days rest between each) — widen the window or "
                f"reduce rounds"
            )
    return errors


__all__ = [
    "DataError",
    "FixedRequirement",
    "World",
    "load_world",
    "validate_world",
]
