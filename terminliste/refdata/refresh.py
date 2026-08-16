"""Compare the shipped venue/club data against a live API, without touching it.

This deliberately never writes to `data/*.yml`. A capacity or a coordinate
feeding into the scheduler is worth a human glance before it changes, so the
output here is a diff report — what the API says versus what's on file —
for `cli.py refresh-reference-data` to print. Folding an accepted diff into
the YAML is still a hand edit, same as any other data correction; this tool
only replaces "go search the web for it" with "here's what a structured
source says right now."

Caching (`cache.py`) is what makes that safe to run often: a cold cache or
an unreachable API degrades to "no diffs to show" rather than a crash, so
this can sit in a Makefile target or a periodic check without becoming a
liability the day the API is down — which, inside this project's own
sandboxed dev environment, it always is (the egress proxy only allows a
fixed host allowlist; see README).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..model.loader import World
from . import cache
from .client import FetchError, TeamRecord, fetch_teams

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / ".refdata_cache"

# Capacity is allowed to drift a little before it's worth flagging — grounds
# get temporary seating for one match, and different providers round
# differently. Anything named at all is worth a look; anything numeric only
# past this margin is.
CAPACITY_TOLERANCE = 50


@dataclass(frozen=True)
class FieldDiff:
    team_id: str
    field: str
    current: object
    fetched: object


@dataclass(frozen=True)
class RefreshReport:
    competition_id: str
    league_name: str
    source: str  # "api" | "cache" | "stale-cache" | "unavailable"
    diffs: list[FieldDiff] = field(default_factory=list)
    unmatched_api_teams: list[str] = field(default_factory=list)
    unmatched_local_teams: list[str] = field(default_factory=list)
    error: str | None = None


def refresh_competition(
    world: World,
    competition_id: str,
    league_name: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    ttl_s: float = cache.DEFAULT_TTL_S,
    force: bool = False,
) -> RefreshReport:
    """Fetch (or reuse a cached fetch of) a league's teams and diff them
    against `world`'s data for the named competition.

    Never raises for a network failure — an unreachable API is reported in
    `RefreshReport.source`/`.error`, not surfaced as an exception, so a
    caller can always show *something* rather than crash.
    """
    cache_path = cache_dir / f"{competition_id}.json"
    records, source, error = _load_records(league_name, cache_path, ttl_s, force)

    competition = world.competitions.get(competition_id)
    if competition is None or not records:
        return RefreshReport(
            competition_id=competition_id,
            league_name=league_name,
            source=source,
            error=error,
        )

    diffs, unmatched_api, unmatched_local = _diff(world, competition.teams, records)
    return RefreshReport(
        competition_id=competition_id,
        league_name=league_name,
        source=source,
        diffs=diffs,
        unmatched_api_teams=unmatched_api,
        unmatched_local_teams=unmatched_local,
        error=error,
    )


def _load_records(
    league_name: str, cache_path: Path, ttl_s: float, force: bool
) -> tuple[list[TeamRecord], str, str | None]:
    if not force:
        entry = cache.read(cache_path)
        if entry is not None and entry.is_fresh(ttl_s):
            records = _records_from_payload(entry.payload)
            if records is not None:
                return records, "cache", None

    try:
        fetched = fetch_teams(league_name)
    except FetchError as exc:
        stale = cache.read(cache_path)
        records = _records_from_payload(stale.payload) if stale is not None else None
        if records is not None:
            return records, "stale-cache", str(exc)
        return [], "unavailable", str(exc)

    cache.write(cache_path, [_record_to_dict(r) for r in fetched])
    return fetched, "api", None


def _records_from_payload(payload: object) -> list[TeamRecord] | None:
    """Deserialize a cached payload, or `None` if its shape isn't usable.

    A cache file that's valid JSON but the wrong shape — hand-edited, or
    left over from an older, incompatible version of this cache — must
    never crash the refresh path; it's exactly as unusable as a missing
    file, so it's treated the same way: fall through to the next fallback.
    """
    if not isinstance(payload, list):
        return None
    try:
        return [TeamRecord(**r) for r in payload]
    except (TypeError, ValueError):
        return None


def _record_to_dict(record: TeamRecord) -> dict:
    return {
        "api_team_id": record.api_team_id,
        "name": record.name,
        "stadium_name": record.stadium_name,
        "stadium_capacity": record.stadium_capacity,
    }


def _diff(
    world: World, team_ids: list[str], records: list[TeamRecord]
) -> tuple[list[FieldDiff], list[str], list[str]]:
    by_normalized_name: dict[str, TeamRecord] = {_normalize(r.name): r for r in records}
    matched_record_names: set[str] = set()
    diffs: list[FieldDiff] = []
    unmatched_local: list[str] = []

    for team_id in team_ids:
        club = world.club_of(team_id)
        record = by_normalized_name.get(_normalize(club.name))
        if record is None:
            unmatched_local.append(team_id)
            continue
        matched_record_names.add(_normalize(record.name))

        venue = world.home_venue_of(team_id)
        if record.stadium_name and _normalize(record.stadium_name) != _normalize(venue.name):
            diffs.append(FieldDiff(team_id, "venue.name", venue.name, record.stadium_name))
        if (
            record.stadium_capacity is not None
            and abs(record.stadium_capacity - venue.capacity) > CAPACITY_TOLERANCE
        ):
            diffs.append(
                FieldDiff(team_id, "venue.capacity", venue.capacity, record.stadium_capacity)
            )

    unmatched_api = [
        r.name for r in records if _normalize(r.name) not in matched_record_names
    ]
    return diffs, unmatched_api, unmatched_local


def _normalize(name: str) -> str:
    return "".join(ch for ch in name.casefold() if ch.isalnum())
