"""Template data for the contingency table, the "if they advance" calendar
markers and the robustness section (#10)."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from ..contingency import ContingencyPlan
from ..model.loader import World
from ..model.schema import FallbackKind, Match, Season
from ..rounds.commitments import Commitment, CommitmentSet
from ..scenarios import RobustnessReport
from ..scoring.registry import describe

KIND_LABELS: dict[FallbackKind, str] = {
    "same_slot": "same slot",
    "reserve_date": "reserve date",
    "next_free_slot": "next free slot",
}

_LEG_SUFFIX = re.compile(r" \((?:first|second) leg\)$")
_MATCHDAY = re.compile(r"^(.*): Matchday \d+$")


def day_label(day: date) -> str:
    return f"{day:%a} {day.day} {day:%b}"


def round_label(label: str) -> str:
    """A commitment label as a round name: no leg suffix, and a league phase's
    matchdays read as one."""
    label = _LEG_SUFFIX.sub("", label)
    matchday = _MATCHDAY.match(label)
    return f"{matchday.group(1)} league phase" if matchday else label


def _first_commitments(commitments: CommitmentSet) -> dict[str, Commitment]:
    first: dict[str, Commitment] = {}
    for c in commitments:
        known = first.get(c.node_id)
        if known is None or c.date < known.date:
            first[c.node_id] = c
    return first


def _fixture_label(world: World, match: Match) -> str:
    competition = world.competition(match.competition_id)
    return (
        f"{competition.short_name} R{match.round_index + 1} "
        f"{world.team_label(match.home_team)}–{world.team_label(match.away_team)}"
    )


def contingency_view(
    world: World, plan: ContingencyPlan | None, matches: list[Match], commitments: CommitmentSet
) -> dict | None:
    """The plan's moves grouped by trigger, each trigger worded from the
    commitment it is: "If Bodø/Glimt reach <round>"."""
    if plan is None:
        return None
    first = _first_commitments(commitments)
    by_key = {m.key: m for m in matches}
    grouped: dict[str, list] = defaultdict(list)
    for move in plan.moves(matches):
        grouped[move.trigger].append(move)

    groups = []
    for trigger, moves in grouped.items():
        anchor = first.get(trigger)
        rows = sorted(
            (
                {
                    "fixture": _fixture_label(world, by_key[move.match_key]),
                    "from": day_label(move.from_date),
                    "to": day_label(move.to_date),
                    "kind": KIND_LABELS[move.kind],
                    "sort": move.from_date,
                }
                for move in moves
            ),
            key=lambda r: (r["sort"], r["fixture"]),
        )
        groups.append(
            {
                "trigger": (
                    f"If {world.team_label(anchor.team_id)} reach {round_label(anchor.label)}"
                    if anchor
                    else f"If {trigger} is played"
                ),
                "probability": f"p={anchor.reached_probability:.2f}" if anchor else "",
                "rows": rows,
                "sort": (rows[0]["sort"], trigger),
            }
        )
    groups.sort(key=lambda g: g["sort"])
    return {
        "groups": groups,
        "moves": sum(len(g["rows"]) for g in groups),
        "uncovered": [
            f"{_fixture_label(world, by_key[k])}, {day_label(by_key[k].date)}"
            for k in plan.uncovered
            if k in by_key
        ],
    }


def maybe_by_day(world: World, season: Season, commitments: CommitmentSet) -> dict[str, list[str]]:
    """The dates a team plays only if it advances, as one line per team and
    round, keyed by ISO date."""
    days: dict[date, dict[tuple[str, str], str]] = defaultdict(dict)
    for c in commitments:
        if c.hard or not season.start <= c.date <= season.end:
            continue
        days[c.date][(c.team_id, c.node_id)] = (
            f"{world.team_label(c.team_id)}: {c.label}, if they advance (p={c.reached_probability:.2f})"
        )
    return {day.isoformat(): sorted(lines.values()) for day, lines in sorted(days.items())}


def robustness_view(report: RobustnessReport | None) -> dict | None:
    if report is None:
        return None
    worst = report.worst_case
    return {
        "report": report,
        "feasible_pct": round(100 * report.feasible_share),
        "mode": "enumerated" if report.exhaustive else "sampled",
        "failing": [
            {"id": rule, "description": describe(rule), "pct": round(100 * share), "mean": round(mean, 1)}
            for rule, share, mean in report.top_failing
        ],
        "worst": None
        if worst is None
        else {
            "moves": worst.moves,
            "feasible": worst.feasible,
            "violations": worst.hard_violations,
            "points": round(worst.points),
            "rules": [f"{rule} ×{n}" for rule, n in worst.failing_rules],
        },
    }
