#!/usr/bin/env python3
"""Turn a season's worth of real fixtures into a baseline-ready CSV.

    python scripts/fetch_real_schedule.py \\
        --competition eliteserien_2026 \\
        --thesportsdb-league-id 4328 \\
        --season 2026 \\
        --out baselines/sources/eliteserien_toppserien_2026_full.csv

If this machine can't reach the API directly — this sandbox can't; see
`baselines/SOURCING_FIXTURES.md` — but curl or a browser on some other
machine can, fetch the JSON there and hand it to this script instead:

    curl "https://www.thesportsdb.com/api/v1/json/3/eventsseason.php?id=4328&s=2026" -o el.json
    python scripts/fetch_real_schedule.py --competition eliteserien_2026 \\
        --from-json el.json --out baselines/sources/eliteserien_toppserien_2026_full.csv

Either way the job is the same: match each fixture's team names against
`data/clubs.yml` for the target competition and write rows in the format
`external_schedule.py` expects, venue left blank so the loader falls back to
each home team's registered one. Matching is the part worth automating — an
API rarely spells a club the way `data/clubs.yml` does ("FK Bodø/Glimt" vs
"Bodø/Glimt") — and doing it by hand for ~450 fixtures is the tedium this
removes.

Two source schemas are understood, TheSportsDB (the default) and API-Football
/ api-sports.io v3; `baselines/SOURCING_FIXTURES.md` says which to use.

Unmatched or ambiguous names are never guessed at: they're skipped and listed
at the end, so a bad guess can't sneak into a committed baseline.
`--allow-fuzzy` accepts substring matches too, but read the summary first — a
fuzzy match goes stale silently when a club changes its API-listed name.

Run once per competition, passing `--append` on the second, so both leagues
land in one file: dual clubs' back-to-back-home-weekend scoring only fires
when they share a schedule.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.parse
from dataclasses import dataclass
from datetime import date
from datetime import datetime as _datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = PROJECT_ROOT / "data"

sys.path.insert(0, str(PROJECT_ROOT))

from terminliste.model.loader import DataError, World, load_world  # noqa: E402
from terminliste.refdata.client import API_BASE, FetchError, fetch_json  # noqa: E402

CSV_FIELDNAMES = ["competition", "date", "home_team", "away_team", "venue"]

# Normalized away on both sides so "Bodø/Glimt", "Bodo/Glimt" and
# "Bodø / Glimt" collapse to one key.
_DIACRITIC_MAP = str.maketrans("æøåÆØÅ", "aoaAOA")

# Club-type words an API is most likely to drop or abbreviate differently
# ("Kristiansund BK" vs "Kristiansund"). Stripped as an extra candidate rather
# than the only one, so a name that needs the qualifier still matches; no two
# clubs in `data/clubs.yml` differ only by one of these.
_CLUB_TOKENS = {"fk", "sk", "il", "bk", "ik", "kvinner", "damer", "fotball"}

# Below this length, "is it a substring of the API name" stops meaning "is it
# probably the same club" — "Åsane" contains "san", Sandefjord's short_name.
_MIN_FUZZY_CANDIDATE_LEN = 5


class ConversionError(Exception):
    """The input couldn't be turned into a schedule at all."""


@dataclass(frozen=True)
class RawFixture:
    event_date: date
    home_name: str
    away_name: str


@dataclass
class MatchResult:
    fixture: RawFixture
    home_team_id: str | None
    away_team_id: str | None
    home_fuzzy: bool
    away_fuzzy: bool

    @property
    def fully_matched(self) -> bool:
        return self.home_team_id is not None and self.away_team_id is not None

    @property
    def any_fuzzy(self) -> bool:
        return self.home_fuzzy or self.away_fuzzy


def _normalize(name: str) -> str:
    name = name.translate(_DIACRITIC_MAP).lower()
    name = name.replace("/", " ").replace("-", " ")
    name = "".join(ch for ch in name if ch.isalnum() or ch.isspace())
    return " ".join(name.split())


def _candidates(name: str) -> set[str]:
    """Every normalized spelling of `name` worth trying as a match key."""
    base = _normalize(name)
    candidates = {base}
    tokens = [t for t in base.split() if t not in _CLUB_TOKENS]
    if tokens:
        candidates.add(" ".join(tokens))
    return candidates


def _gender_variants(candidates: set[str], team) -> set[str]:
    if team.gender != "women":
        return candidates
    expanded = set(candidates)
    expanded |= {c + " kvinner" for c in candidates}
    expanded |= {c + " damer" for c in candidates}
    return expanded


def _team_candidates(world: World, team_id: str) -> set[str]:
    """Every normalized spelling worth trying for an *exact* match — full
    club name and short code both, since an exact hit can't collide."""
    team = world.team(team_id)
    club = world.club(team.club_id)
    candidates = _candidates(club.name) | _candidates(club.short_name)
    return _gender_variants(candidates, team)


def _fuzzy_team_candidates(world: World, team_id: str) -> set[str]:
    """Candidates worth trying as a *substring* match key.

    Narrower than `_team_candidates`: the short code is left out, and anything
    under `_MIN_FUZZY_CANDIDATE_LEN` is dropped even from the full club name,
    since a substring test on a short string finds unrelated clubs.
    """
    team = world.team(team_id)
    club = world.club(team.club_id)
    candidates = {c for c in _candidates(club.name) if len(c) >= _MIN_FUZZY_CANDIDATE_LEN}
    return _gender_variants(candidates, team)


def resolve_team(world: World, team_ids: list[str], api_name: str) -> tuple[str | None, bool]:
    """Match an API team name against one competition's roster.

    Restricted to the competition being converted rather than all of
    `data/clubs.yml`: "Brann" only means `brann_m` rather than `brann_w` once
    you know which league's fixture list this is.

    Returns `(team_id, was_fuzzy)`. An exact normalized match wins; a
    substring match is a flagged fallback for the caller to judge.
    """
    api_key = _normalize(api_name)
    if not api_key:
        return None, False

    exact_matches = {
        team_id for team_id in team_ids if api_key in _team_candidates(world, team_id)
    }
    if len(exact_matches) == 1:
        return next(iter(exact_matches)), False
    if len(exact_matches) > 1:
        # Two teams share a normalized name: refuse to guess rather than pick.
        return None, False

    fuzzy_matches = set()
    for team_id in team_ids:
        for candidate in _fuzzy_team_candidates(world, team_id):
            if candidate in api_key or api_key in candidate:
                fuzzy_matches.add(team_id)
                break
    if len(fuzzy_matches) == 1:
        return next(iter(fuzzy_matches)), True
    return None, False


def _parse_thesportsdb(payload: dict) -> list[RawFixture]:
    fixtures = []
    for raw in payload.get("events") or []:
        date_raw = raw.get("dateEvent")
        home = raw.get("strHomeTeam") or ""
        away = raw.get("strAwayTeam") or ""
        if not date_raw or not home or not away:
            continue
        try:
            event_date = _datetime.strptime(date_raw, "%Y-%m-%d").date()
        except ValueError:
            continue
        fixtures.append(RawFixture(event_date=event_date, home_name=home, away_name=away))
    return fixtures


def _parse_api_football(payload: dict) -> list[RawFixture]:
    fixtures = []
    for row in payload.get("response") or []:
        date_raw = (row.get("fixture") or {}).get("date")
        if not date_raw:
            continue
        try:
            # api-sports.io timestamps carry a full offset; only the date
            # half matters here.
            event_date = _datetime.fromisoformat(date_raw).date()
        except ValueError:
            continue
        teams = row.get("teams") or {}
        home = (teams.get("home") or {}).get("name") or ""
        away = (teams.get("away") or {}).get("name") or ""
        if not home or not away:
            continue
        fixtures.append(RawFixture(event_date=event_date, home_name=home, away_name=away))
    return fixtures


PARSERS = {
    "thesportsdb": _parse_thesportsdb,
    "api-football": _parse_api_football,
}


def _side_label(api_name: str, team_id: str, fuzzy: bool) -> str:
    """`'Åsane' -> sandefjord_m (fuzzy)` — a reviewer scanning the warnings
    needs the team id a name resolved to, since the API name alone reads as
    plausible whether the match was right or wrong."""
    return f"{api_name!r} -> {team_id} ({'fuzzy' if fuzzy else 'exact'})"


def convert(
    world: World,
    competition_id: str,
    fixtures: list[RawFixture],
    allow_fuzzy: bool,
) -> tuple[list[dict], list[str]]:
    """Match every fixture and split into (rows to write, problems to report)."""
    competition = world.competition(competition_id)
    team_ids = list(competition.teams)

    rows: list[dict] = []
    problems: list[str] = []
    for fixture in fixtures:
        home_id, home_fuzzy = resolve_team(world, team_ids, fixture.home_name)
        away_id, away_fuzzy = resolve_team(world, team_ids, fixture.away_name)
        result = MatchResult(fixture, home_id, away_id, home_fuzzy, away_fuzzy)

        if not result.fully_matched:
            unresolved = []
            if home_id is None:
                unresolved.append(f"home {fixture.home_name!r}")
            if away_id is None:
                unresolved.append(f"away {fixture.away_name!r}")
            problems.append(
                f"{fixture.event_date}: could not match {', '.join(unresolved)} "
                f"against {competition.name}'s roster — skipped"
            )
            continue

        if result.any_fuzzy:
            note = (
                f"{fixture.event_date}: "
                f"{_side_label(fixture.home_name, home_id, home_fuzzy)} v "
                f"{_side_label(fixture.away_name, away_id, away_fuzzy)} — "
                f"{'accepted (--allow-fuzzy)' if allow_fuzzy else 'skipped'}"
            )
            problems.append(note)
            if not allow_fuzzy:
                continue

        rows.append(
            {
                "competition": competition_id,
                "date": fixture.event_date.isoformat(),
                "home_team": home_id,
                "away_team": away_id,
                "venue": "",
            }
        )

    rows.sort(key=lambda r: (r["date"], r["home_team"]))
    return rows, problems


def _write_csv(rows: list[dict], out_path: Path, append: bool) -> None:
    existing: list[dict] = []
    if append and out_path.exists():
        with out_path.open(newline="", encoding="utf-8") as fh:
            existing = list(csv.DictReader(fh))

    combined = existing + rows
    combined.sort(key=lambda r: (r["date"], r["competition"], r["home_team"]))

    # Checked before anything is opened: `csv.DictWriter.writerows` raises on
    # a key outside `fieldnames` only mid-write, by which point the header is
    # on disk and the previous contents are gone — which would let a failed
    # --append destroy the file it was meant to extend.
    unexpected = {key for row in combined for key in row} - set(CSV_FIELDNAMES)
    if unexpected:
        raise ConversionError(
            f"{out_path}: existing row(s) have column(s) {sorted(unexpected)} not in "
            f"{CSV_FIELDNAMES} — resolve that before appending"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Written to a sibling temp file and moved into place atomically, so a
    # crash mid-write leaves the original intact rather than truncated.
    tmp_path = out_path.with_name(f".{out_path.name}.tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(combined)
    tmp_path.replace(out_path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--competition", required=True, help="e.g. eliteserien_2026")
    parser.add_argument("--out", required=True, help="CSV path to write")
    parser.add_argument(
        "--schema", choices=sorted(PARSERS), default="thesportsdb", help="source JSON shape"
    )
    parser.add_argument("--from-json", help="parse a previously saved API response instead of fetching")
    parser.add_argument("--thesportsdb-league-id", help="numeric league id, e.g. 4328")
    parser.add_argument("--season", help="API season string, e.g. 2026 or 2025-2026")
    parser.add_argument(
        "--append", action="store_true", help="merge into an existing --out file rather than overwrite"
    )
    parser.add_argument(
        "--allow-fuzzy",
        action="store_true",
        help="accept substring-based team-name matches instead of skipping them",
    )
    args = parser.parse_args()

    try:
        world = load_world(DATA_ROOT)
    except DataError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    try:
        world.competition(args.competition)
    except DataError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    try:
        payload = _load_payload(args)
    except ConversionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    fixtures = PARSERS[args.schema](payload)
    if not fixtures:
        print(
            f"FAIL: no fixtures found in the {args.schema} payload — wrong league id/season, "
            "or the API returned no data for it.",
            file=sys.stderr,
        )
        return 1

    rows, problems = convert(world, args.competition, fixtures, args.allow_fuzzy)

    for problem in problems:
        print(f"  ⚠ {problem}")
    print(f"\nMatched {len(rows)} of {len(fixtures)} fixtures for {args.competition}.")

    if not rows:
        print("FAIL: nothing matched — check --schema and the team-name output above.", file=sys.stderr)
        return 1

    try:
        _write_csv(rows, Path(args.out), args.append)
    except ConversionError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {args.out}")

    if problems:
        print(
            "\nReview the lines above before trusting this file — unmatched or fuzzy "
            "fixtures were left out, so a partial write here means a partial baseline."
        )
    return 0


def _load_payload(args: argparse.Namespace) -> dict:
    if args.from_json:
        path = Path(args.from_json)
        if not path.exists():
            raise ConversionError(f"no such file: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConversionError(f"{path} is not valid JSON: {exc}") from None
        if not isinstance(payload, dict):
            raise ConversionError(f"{path}: expected a JSON object at the top level")
        return payload

    if args.schema != "thesportsdb":
        raise ConversionError(
            f"--schema {args.schema} has no built-in fetch path — use --from-json "
            "with a file fetched by curl or a browser instead"
        )
    if not args.thesportsdb_league_id or not args.season:
        raise ConversionError("--thesportsdb-league-id and --season are required without --from-json")

    # Returned as the raw payload so it runs through the same
    # `_parse_thesportsdb` as --from-json: one conversion path, not two that
    # could drift apart.
    league_id = urllib.parse.quote(args.thesportsdb_league_id)
    season = urllib.parse.quote(args.season)
    url = f"{API_BASE}/eventsseason.php?id={league_id}&s={season}"
    try:
        return fetch_json(url)
    except FetchError as exc:
        raise ConversionError(str(exc)) from None


if __name__ == "__main__":
    raise SystemExit(main())
