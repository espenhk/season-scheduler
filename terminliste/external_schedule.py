"""Load an externally supplied schedule and score it against the same rules.

The scoring machinery doesn't care where a `Match` list came from, so a real
fixture list — a league's published calendar, a hand-drafted spreadsheet —
can be scored exactly like a solver's own output. That is the fastest way to
answer "what's wrong with this schedule" without touching the solver.

The format is minimal — competition, date, home team, away team, venue
optional — because a real-world source rarely carries our `round_index`/`leg`
bookkeeping. Both are inferred: `leg` from the order each pair meets,
`round_index` from the rank of each date within its competition. Neither feeds
any constraint but `leg_ordering`, which is the property leg inference is
designed to preserve.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .model.loader import World
from .model.schema import Match


class ExternalScheduleError(Exception):
    """Raised for a schedule file that cannot be parsed or resolved."""


@dataclass
class RawEntry:
    competition_id: str
    date: date
    home_team: str
    away_team: str
    venue: str | None = None
    # Bookkeeping a previous `generate` writes, or a hand-made file may add;
    # inferred when absent.
    round_number: int | None = None
    leg: int | None = None
    kickoff_time: str | None = None
    locked: bool = False


@dataclass
class ExternalSchedule:
    """A loaded schedule plus what a re-plan needs beyond the matches (#13)."""

    matches: list[Match]
    warnings: list[str] = field(default_factory=list)
    # Keys of the rows marked `locked`.
    locked: frozenset[str] = frozenset()
    # Whether every row says which round it belongs to; the inferred rounds
    # are date ranks, which a re-plan cannot use.
    has_rounds: bool = False


def load_external_schedule(path: Path, world: World) -> tuple[list[Match], list[str]]:
    """Parse a CSV or JSON schedule file into `Match` objects.

    Returns `(matches, warnings)`. An unknown team id or bad date raises
    `ExternalScheduleError`, since the file can't be scored at all; softer
    problems like a pair meeting the wrong number of times come back as
    warnings so the rest of the schedule still scores.
    """
    loaded = load_schedule(path, world)
    return loaded.matches, loaded.warnings


def load_schedule(path: Path, world: World, option: str | None = None) -> ExternalSchedule:
    """`load_external_schedule` that also keeps the `locked` rows and says
    whether rounds were given. A JSON file may be a previous `generate`'s
    output; `option` names which of its options to read (the first by default).
    """
    path = Path(path)
    if not path.exists():
        raise ExternalScheduleError(f"no such file: {path}")

    if path.suffix.lower() == ".csv":
        entries = _parse_csv(path)
    elif path.suffix.lower() == ".json":
        entries = _parse_json(path, option)
    else:
        raise ExternalScheduleError(
            f"unrecognised schedule format {path.suffix!r} — use .csv or .json"
        )

    _validate_entries(world, entries, path)
    matches = _build_matches(entries, world)
    return ExternalSchedule(
        matches=matches,
        warnings=_coverage_warnings(world, entries),
        locked=frozenset(m.key for m, e in zip(matches, entries, strict=True) if e.locked),
        has_rounds=bool(entries) and all(e.round_number is not None for e in entries),
    )


def _parse_csv(path: Path) -> list[RawEntry]:
    entries: list[RawEntry] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        required = {"competition", "date", "home_team", "away_team"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ExternalScheduleError(
                f"{path}: missing required column(s) {sorted(missing)} — "
                f"expected competition,date,home_team,away_team[,venue]"
            )
        for line_number, row in enumerate(reader, start=2):
            entries.append(
                RawEntry(
                    competition_id=row["competition"].strip(),
                    date=_parse_date(row["date"].strip(), path, line_number),
                    home_team=row["home_team"].strip(),
                    away_team=row["away_team"].strip(),
                    venue=(row.get("venue") or "").strip() or None,
                    round_number=_optional_int(row.get("round"), path, line_number, "round"),
                    leg=_optional_int(row.get("leg"), path, line_number, "leg"),
                    kickoff_time=(row.get("kickoff_time") or "").strip() or None,
                    locked=_parse_locked(row.get("locked")),
                )
            )
    return entries


def _parse_json(path: Path, option: str | None = None) -> list[RawEntry]:
    with path.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    if isinstance(raw, dict) and "options" in raw:
        raw = _pick_option(raw["options"], option, path)
    rows = raw.get("matches", raw) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ExternalScheduleError(
            f"{path}: expected a JSON list of matches, or an object with a 'matches' list"
        )
    entries: list[RawEntry] = []
    # 1-based to match CSV line numbers and `_validate_entries`'s "entry N",
    # so a parse error and a validation error name the same row.
    for i, row in enumerate(rows, start=1):
        try:
            entries.append(
                RawEntry(
                    competition_id=str(row["competition"]).strip(),
                    date=_parse_date(str(row["date"]).strip(), path, i),
                    # `generate`'s own JSON says home/away.
                    home_team=str(row["home_team"] if "home_team" in row else row["home"]).strip(),
                    away_team=str(row["away_team"] if "away_team" in row else row["away"]).strip(),
                    venue=(str(row["venue"]).strip() if row.get("venue") else None),
                    round_number=_optional_int(row.get("round"), path, i, "round"),
                    leg=_optional_int(row.get("leg"), path, i, "leg"),
                    kickoff_time=(str(row["kickoff_time"]).strip() if row.get("kickoff_time") else None),
                    locked=_parse_locked(row.get("locked")),
                )
            )
        except KeyError as exc:
            raise ExternalScheduleError(f"{path}: entry {i} is missing field {exc}") from None
    return entries


def _pick_option(options: list, label: str | None, path: Path) -> dict:
    if not options:
        raise ExternalScheduleError(f"{path}: the file holds no options")
    if label is None:
        return options[0]
    for option in options:
        if option.get("label") == label:
            return option
    labels = ", ".join(str(o.get("label")) for o in options)
    raise ExternalScheduleError(f"{path}: no option {label!r} (has {labels})")


def _optional_int(raw: object, path: Path, where: object, name: str) -> int | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        raise ExternalScheduleError(f"{path}, entry {where}: {name} {raw!r} is not a number") from None


def _parse_locked(raw: object) -> bool:
    return raw is True or str(raw).strip().lower() in ("1", "true", "yes", "y")


def _parse_date(raw: str, path: Path, where: object) -> date:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ExternalScheduleError(f"{path}, entry {where}: unparseable date {raw!r}")


def _validate_entries(world: World, entries: list[RawEntry], path: Path) -> None:
    errors: list[str] = []
    for i, entry in enumerate(entries, start=1):
        if entry.competition_id not in world.competitions:
            errors.append(f"entry {i}: unknown competition {entry.competition_id!r}")
        if entry.home_team not in world.teams:
            errors.append(f"entry {i}: unknown home team {entry.home_team!r}")
        if entry.away_team not in world.teams:
            errors.append(f"entry {i}: unknown away team {entry.away_team!r}")
        if entry.home_team == entry.away_team:
            errors.append(f"entry {i}: {entry.home_team!r} scheduled to play itself")
        if entry.venue is not None and entry.venue not in world.venues:
            errors.append(f"entry {i}: unknown venue {entry.venue!r}")
    if errors:
        preview = "\n  - ".join(errors[:20])
        more = f"\n  ... and {len(errors) - 20} more" if len(errors) > 20 else ""
        raise ExternalScheduleError(f"{path}: {len(errors)} problem(s):\n  - {preview}{more}")


def _build_matches(entries: list[RawEntry], world: World) -> list[Match]:
    leg_by_index = {**_assign_legs(entries), **{i: e.leg for i, e in enumerate(entries) if e.leg}}
    round_by_index = {
        **_assign_rounds(entries),
        **{i: e.round_number - 1 for i, e in enumerate(entries) if e.round_number},
    }

    matches: list[Match] = []
    for i, entry in enumerate(entries):
        venue = entry.venue or world.team(entry.home_team).home_venue
        matches.append(
            Match(
                competition_id=entry.competition_id,
                home_team=entry.home_team,
                away_team=entry.away_team,
                leg=leg_by_index[i],
                round_index=round_by_index[i],
                date=entry.date,
                venue=venue,
                kickoff_time=entry.kickoff_time,
            )
        )
    return matches


def _assign_legs(entries: list[RawEntry]) -> dict[int, int]:
    """First meeting of a pair is leg 1, second leg 2, and so on.

    Orientation-independent — A-home-v-B and B-home-v-A are the same pair
    meeting — so this reads legs correctly whichever side was drawn at home.
    """
    order: dict[tuple[str, str, str], list[int]] = {}
    for i, entry in enumerate(entries):
        pair = tuple(sorted((entry.home_team, entry.away_team)))
        key = (entry.competition_id, pair[0], pair[1])
        order.setdefault(key, []).append(i)

    leg_by_index: dict[int, int] = {}
    for indexes in order.values():
        indexes_sorted = sorted(indexes, key=lambda i: entries[i].date)
        for leg, index in enumerate(indexes_sorted, start=1):
            leg_by_index[index] = leg
    return leg_by_index


def _assign_rounds(entries: list[RawEntry]) -> dict[int, int]:
    """Matchday number: the rank of each date among its competition's dates."""
    dates_by_competition: dict[str, set[date]] = {}
    for entry in entries:
        dates_by_competition.setdefault(entry.competition_id, set()).add(entry.date)

    rank_by_competition: dict[str, dict[date, int]] = {
        competition_id: {d: rank for rank, d in enumerate(sorted(dates))}
        for competition_id, dates in dates_by_competition.items()
    }

    return {
        i: rank_by_competition[entry.competition_id][entry.date]
        for i, entry in enumerate(entries)
    }


def _coverage_warnings(world: World, entries: list[RawEntry]) -> list[str]:
    """Flag pairs that met an unexpected number of times, or not at all.

    Advisory only: a partial schedule — a proposed change to a few rounds —
    is a reasonable thing to score. This just says what the coverage is
    before the reader trusts a "feasible" badge covering only part of it.
    """
    warnings: list[str] = []
    by_competition: dict[str, list[RawEntry]] = {}
    for entry in entries:
        by_competition.setdefault(entry.competition_id, []).append(entry)

    for competition_id, rows in by_competition.items():
        competition = world.competitions.get(competition_id)
        if competition is None:
            continue
        expected = competition.rounds_per_pairing
        counts: dict[tuple[str, str], int] = {}
        for entry in rows:
            pair = tuple(sorted((entry.home_team, entry.away_team)))
            counts[pair] = counts.get(pair, 0) + 1

        teams = competition.teams
        all_pairs = {tuple(sorted((a, b))) for i, a in enumerate(teams) for b in teams[i + 1 :]}
        missing = all_pairs - set(counts)
        wrong = {pair: n for pair, n in counts.items() if n != expected}

        # `min`, not `next(iter(...))`: `missing` is a set, so its iteration
        # order moves with the hash seed, and these strings land in committed
        # baseline reports where a sentence that rewrites itself every run is
        # an unreadable diff. `wrong` is already stable, sorted anyway so the
        # guarantee is stated rather than inherited.
        if missing:
            warnings.append(
                f"{competition.name}: {len(missing)} pair(s) never meet (expected {expected} "
                f"meetings each) — e.g. {_label(world, min(missing))}"
            )
        if wrong:
            example_pair, example_n = min(wrong.items())
            warnings.append(
                f"{competition.name}: {len(wrong)} pair(s) meet a different number of times "
                f"than {expected} — e.g. {_label(world, example_pair)} met {example_n} time(s)"
            )
    return warnings


def _label(world: World, pair: tuple[str, str]) -> str:
    try:
        return f"{world.team_label(pair[0])} v {world.team_label(pair[1])}"
    except Exception:
        return f"{pair[0]} v {pair[1]}"


__all__ = ["ExternalSchedule", "ExternalScheduleError", "load_external_schedule", "load_schedule"]
