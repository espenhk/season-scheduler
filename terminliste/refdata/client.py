"""HTTP client for a free, no-signup sports-data API.

Uses TheSportsDB's public test key (`3`) — the same key their own docs
publish for anyone to try the API with, not a secret. It's a small, free
service, not the kind of provider you'd want backing a paying product, but
it covers the Norwegian Eliteserien and Toppserien and needs no account,
which matches this project's "everything runs offline by default, network
access is opt-in" posture: `validate`, `generate` and `score` never call
this; only `cli.py refresh-reference-data` does.

Deliberately a thin wrapper over `urllib.request` rather than a new
`requests`/`httpx` dependency — this is the only place in the project that
makes an HTTP call, and stdlib is enough for one GET-JSON endpoint.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

API_BASE = "https://www.thesportsdb.com/api/v1/json/3"
TIMEOUT_S = 10.0


class FetchError(Exception):
    """The API could not be reached, or returned something unparseable.

    Callers are expected to catch this and fall back to cached/static data —
    a refresh failing is never allowed to be worse than not refreshing.
    """


@dataclass(frozen=True)
class TeamRecord:
    """What the API told us about one team. `None` means "field not given" —
    distinct from a value we chose not to trust."""

    api_team_id: str
    name: str
    stadium_name: str | None
    stadium_capacity: int | None


def fetch_teams(league_name: str) -> list[TeamRecord]:
    """All teams the API lists for a league, e.g. "Norwegian Eliteserien".

    Raises `FetchError` on any network, HTTP, or parsing failure — never
    returns a partial or best-guess result silently.
    """
    url = f"{API_BASE}/search_all_teams.php?l={urllib.parse.quote(league_name)}"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:
            body = response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FetchError(f"could not reach {API_BASE}: {exc}") from exc

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise FetchError(f"response from {url} was not valid JSON: {exc}") from exc

    teams = payload.get("teams") or []
    return [_parse_team(t) for t in teams]


def _parse_team(raw: dict) -> TeamRecord:
    capacity_raw = raw.get("intStadiumCapacity")
    capacity = None
    if capacity_raw not in (None, "", "0"):
        try:
            capacity = int(capacity_raw)
        except (TypeError, ValueError):
            capacity = None
    team_id = raw.get("idTeam")
    return TeamRecord(
        api_team_id=str(team_id) if team_id is not None else "",
        name=raw.get("strTeam") or "",
        stadium_name=raw.get("strStadium") or None,
        stadium_capacity=capacity,
    )
