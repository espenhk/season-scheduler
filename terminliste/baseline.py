"""Score a committed, real-world fixture list and persist the verdict.

`external_schedule.py` answers "can this arbitrary CSV be scored?"; this
handles the durable version — the leagues' actually-published schedules, kept
in the repo with their provenance, re-scored on demand so the number never
drifts from the ruleset that produced it.

A generated schedule scoring 93/100 means nothing on its own; it is only
interesting next to what the real fixture list scores under the same rules.

Two deliberate choices:

**A schedule with no sidecar is not scoreable.** Every `<name>.csv` needs a
`<name>.yml` naming where it came from, when, and whether anyone verified it.
A fixture list claiming to be official but unable to say why is exactly what
this project wants to keep out.

**A partial baseline is legitimate.** Real sources are often incomplete. What
that changes is how the hard-violation count reads: rules keyed to dates the
file never reaches fail for want of data, not for want of a good schedule.
The sidecar's `expected_hard_violations` records those, and the report prints
them beside the raw count.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .external_schedule import ExternalScheduleError, load_external_schedule
from .model.loader import World
from .model.schema import Match
from .model.travel import ApiTravelModel
from .rounds.cup_schedule import CupSchedulingError, schedule_cups
from .scoring.base import EvalContext, Score, evaluate
from .scoring.registry import build_constraints


class BaselineError(Exception):
    """Raised for a baseline whose sidecar is missing, malformed, or inconsistent."""


# Sidecar keys the report and the refusal-to-guess policy depend on;
# everything else is optional prose.
REQUIRED_FIELDS = ("id", "name", "season", "schedule_file", "verified", "retrieved", "sources")


@dataclass(frozen=True)
class BaselineSource:
    """One committed real-world schedule plus the provenance that vouches for it."""

    id: str
    name: str
    season_id: str
    schedule_path: Path
    sidecar_path: Path
    verified: bool
    retrieved: str
    coverage: str
    sources: list[str]
    competitions: list[str] = field(default_factory=list)
    contains: str = ""
    expected_hard_violations: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass(frozen=True)
class BaselineEvaluation:
    """A baseline, scored."""

    source: BaselineSource
    matches: list[Match]
    warnings: list[str]
    score: Score


def discover_baselines(sources_dir: Path) -> list[BaselineSource]:
    """Every baseline in `sources_dir`, ordered by id.

    Driven by the sidecars rather than the CSVs, so a stray CSV without
    provenance is a loud error when someone scores it by name rather than
    silently joining the committed baselines on the next refresh.
    """
    sources_dir = Path(sources_dir)
    if not sources_dir.is_dir():
        raise BaselineError(f"no such baseline source directory: {sources_dir}")
    return sorted(
        (load_baseline(path) for path in sources_dir.glob("*.yml")),
        key=lambda s: s.id,
    )


def load_baseline(sidecar_path: Path) -> BaselineSource:
    """Parse one `<name>.yml` sidecar and locate the CSV it describes."""
    sidecar_path = Path(sidecar_path)
    if not sidecar_path.exists():
        raise BaselineError(f"no such baseline sidecar: {sidecar_path}")

    try:
        raw = yaml.safe_load(sidecar_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise BaselineError(f"{sidecar_path}: not valid YAML — {exc}") from None
    if not isinstance(raw, dict):
        raise BaselineError(f"{sidecar_path}: expected a YAML mapping")

    missing = [f for f in REQUIRED_FIELDS if raw.get(f) in (None, "", [])]
    if missing:
        raise BaselineError(f"{sidecar_path}: missing required field(s) {missing}")

    # Checked, not coerced: `bool("no")` is `True`, so a quoted "no" would
    # come through as "trusted" — the exact failure this field exists to stop.
    raw_verified = raw["verified"]
    if not isinstance(raw_verified, bool):
        raise BaselineError(
            f"{sidecar_path}: 'verified' must be a YAML boolean (true/false), "
            f"got {raw_verified!r}"
        )

    # A scalar string (a forgotten leading "- ") passes the presence check
    # above and then iterates character by character, turning one sentence
    # into dozens of one-letter bullets.
    for list_field in ("sources", "expected_hard_violations", "competitions"):
        value = raw.get(list_field)
        if value is not None and not isinstance(value, list):
            raise BaselineError(
                f"{sidecar_path}: {list_field!r} must be a list, got {type(value).__name__}"
            )

    schedule_path = sidecar_path.parent / str(raw["schedule_file"])
    if not schedule_path.exists():
        raise BaselineError(
            f"{sidecar_path}: schedule_file {raw['schedule_file']!r} does not exist "
            f"(looked in {sidecar_path.parent})"
        )

    return BaselineSource(
        id=str(raw["id"]),
        name=str(raw["name"]),
        season_id=str(raw["season"]),
        schedule_path=schedule_path,
        sidecar_path=sidecar_path,
        verified=raw_verified,
        retrieved=str(raw["retrieved"]),
        coverage=str(raw.get("coverage", "unknown")),
        sources=[str(s) for s in raw["sources"]],
        competitions=[str(c) for c in raw.get("competitions", [])],
        contains=str(raw.get("contains", "")),
        expected_hard_violations=[str(e) for e in raw.get("expected_hard_violations", [])],
        notes=str(raw.get("notes", "")),
    )


def evaluate_baseline(source: BaselineSource, world: World) -> BaselineEvaluation:
    """Score one baseline against the season's full hard + soft ruleset.

    Identical machinery to `cli.py score`, so a baseline's number is directly
    comparable to a generated schedule's — the entire point.
    """
    season = world.season(source.season_id)
    competitions = [world.competition(c) for c in season.competitions]
    try:
        cup_schedules, cup_warnings = schedule_cups(
            [world.competition(c) for c in season.cup_competitions], season
        )
    except CupSchedulingError as exc:
        raise BaselineError(f"{source.id}: cup scheduling failed — {exc}") from None
    constraints = build_constraints(world, season, competitions, cup_schedules)

    try:
        matches, warnings = load_external_schedule(source.schedule_path, world)
    except ExternalScheduleError as exc:
        raise BaselineError(f"{source.id}: {exc}") from None
    if not matches:
        raise BaselineError(f"{source.id}: {source.schedule_path} contained no matches")

    ctx = EvalContext(
        world=world,
        season=season,
        travel=ApiTravelModel(world),
        detail=True,
    )
    return BaselineEvaluation(
        source=source,
        matches=matches,
        warnings=[*warnings, *cup_warnings],
        score=evaluate(matches, constraints, ctx),
    )


def write_reports(evaluation: BaselineEvaluation, world: World, reports_dir: Path) -> list[Path]:
    """Write the committed `<id>.json` and `<id>.md` for one evaluation."""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)

    json_path = reports_dir / f"{evaluation.source.id}.json"
    # ensure_ascii=False: this file is committed and read in diffs, and
    # \u00e5-escaped Norwegian club names would make every review a decoding
    # exercise.
    json_path.write_text(
        json.dumps(_json_payload(evaluation), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    md_path = reports_dir / f"{evaluation.source.id}.md"
    md_path.write_text(_markdown(evaluation, world), encoding="utf-8")

    return [json_path, md_path]


def _json_payload(evaluation: BaselineEvaluation) -> dict:
    """Machine-readable twin of the markdown.

    Breakdown rows sort by constraint id rather than magnitude, so a rule
    whose score moved is a one-line diff, not a reordered table.
    """
    source, score = evaluation.source, evaluation.score
    return {
        "id": source.id,
        "name": source.name,
        "season": source.season_id,
        "verified": source.verified,
        "retrieved": source.retrieved,
        "coverage": source.coverage,
        "num_matches": len(evaluation.matches),
        "feasible": score.feasible,
        "hard_violations": score.hard_violations,
        "expected_hard_violations": source.expected_hard_violations,
        "soft_total": round(score.soft_total, 2),
        "points": round(score.points, 1),
        "coverage_warnings": evaluation.warnings,
        "breakdown": [
            {
                "constraint": r.constraint_id,
                "kind": r.kind,
                "total": round(r.total, 2),
                "count": r.count,
            }
            for r in sorted(score.results, key=lambda r: r.constraint_id)
        ],
        "matches": [
            {
                "date": m.date.isoformat(),
                "competition": m.competition_id,
                "round": m.round_index + 1,
                "leg": m.leg,
                "home": m.home_team,
                "away": m.away_team,
                "venue": m.venue,
            }
            for m in sorted(evaluation.matches, key=lambda m: (m.date, m.competition_id, m.home_team))
        ],
    }


def _markdown(evaluation: BaselineEvaluation, world: World) -> str:
    source, score = evaluation.source, evaluation.score
    lines: list[str] = []

    lines.append(f"# {source.name}")
    lines.append("")
    lines.append(
        "Generated by `python scripts/refresh_baselines.py` — do not edit by hand. "
        "Change the schedule or its sidecar in `baselines/sources/` and re-run."
    )
    lines.append("")

    lines.append("## Provenance")
    lines.append("")
    lines.append(f"- **Season:** {source.season_id}")
    lines.append(f"- **Schedule file:** `baselines/sources/{source.schedule_path.name}`")
    lines.append(f"- **Retrieved:** {source.retrieved}")
    lines.append(f"- **Verified:** {'yes' if source.verified else 'no — treat as approximate'}")
    lines.append(f"- **Coverage:** {source.coverage}")
    if source.contains:
        lines.append(f"- **Contains:** {source.contains}")
    lines.append("")
    lines.append("**Sources:**")
    lines.append("")
    for entry in source.sources:
        lines.append(f"- {entry}")
    lines.append("")

    lines.append("## Verdict")
    lines.append("")
    lines.append(f"- **Matches scored:** {len(evaluation.matches)}")
    lines.append(f"- **Points:** {score.points:.1f} / 100")
    lines.append(f"- **Hard violations:** {score.hard_violations}")
    lines.append(f"- **Soft score:** {score.soft_total:.1f}")
    lines.append("")

    if source.expected_hard_violations:
        lines.append(
            "Some of those hard violations are expected, and are artefacts of this "
            "baseline's coverage rather than defects in the published schedule:"
        )
        lines.append("")
        for entry in source.expected_hard_violations:
            lines.append(f"- {entry}")
        lines.append("")

    if evaluation.warnings:
        lines.append("## Coverage notes")
        lines.append("")
        for warning in evaluation.warnings:
            lines.append(f"- {warning}")
        lines.append("")

    hard = [r for r in score.hard_results() if r.count]
    if hard:
        lines.append("## Hard rules broken")
        lines.append("")
        for result in sorted(hard, key=lambda r: -r.count):
            lines.append(f"### `{result.constraint_id}` — {result.count}")
            lines.append("")
            for event in result.events[:10]:
                lines.append(f"- {event.detail}")
            if len(result.events) > 10:
                lines.append(f"- …and {len(result.events) - 10} more")
            lines.append("")

    lines.append("## Soft rules, worst first")
    lines.append("")
    lines.append("| Rule | Score | Events |")
    lines.append("| --- | ---: | ---: |")
    for result in sorted(score.soft_results(), key=lambda r: r.total):
        lines.append(f"| `{result.constraint_id}` | {result.total:.1f} | {result.count} |")
    lines.append("")

    lines.append("## Fixtures scored")
    lines.append("")
    lines.append("| Date | Competition | Home | Away | Venue |")
    lines.append("| --- | --- | --- | --- | --- |")
    for match in sorted(evaluation.matches, key=lambda m: (m.date, m.competition_id, m.home_team)):
        lines.append(
            f"| {match.date.isoformat()} "
            f"| {world.competition(match.competition_id).name} "
            f"| {world.team_label(match.home_team)} "
            f"| {world.team_label(match.away_team)} "
            f"| {world.venue(match.venue).name} |"
        )
    lines.append("")

    if source.notes:
        lines.append("## Notes")
        lines.append("")
        lines.append(source.notes.rstrip())
        lines.append("")

    return "\n".join(lines)


__all__ = [
    "BaselineError",
    "BaselineEvaluation",
    "BaselineSource",
    "discover_baselines",
    "evaluate_baseline",
    "load_baseline",
    "write_reports",
]
