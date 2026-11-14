"""Template data for the contingency table and the calendar's move badges and
ghost fixtures, the "if they advance" calendar markers and the robustness
section (#10)."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from ..contingency import ContingencyPlan, combined_probability
from ..model.loader import World
from ..model.schema import FallbackKind, Match, Season
from ..replan import ReplanDiff
from ..rounds.commitments import Commitment, CommitmentSet
from ..scenarios import RobustnessReport
from ..scoring.registry import describe

KIND_ORDER: tuple[FallbackKind, ...] = ("same_slot", "reserve_date", "next_free_slot")

# Plain-words kinds, as the table and the badge's tooltip say them.
KIND_LABELS: dict[FallbackKind, str] = {
    "same_slot": "same round, another day",
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


def reach_phrase(label: str) -> str:
    """What reaching a round reads as in a sentence: "the UEFA Champions League
    league phase", "round 3 of NM Cupen"."""
    name = round_label(label)
    if name.endswith(" league phase"):
        return f"the {name}"
    competition, _, stage = name.partition(": ")
    if not stage:
        return f"the {name}"
    if stage.startswith("Round "):
        return f"{stage[0].lower()}{stage[1:]} of {competition}"
    return f"the {stage[0].lower()}{stage[1:]} of {competition}"


def shift_text(days: int) -> str:
    """"−1 day", "+2 days"."""
    sign = "+" if days > 0 else "\u2212"
    return f"{sign}{abs(days)} day{'' if abs(days) == 1 else 's'}"


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
        f"{world.team_label(match.home_team)}\u2013{world.team_label(match.away_team)}"
    )


def fixture_key(match: Match) -> str:
    """The id the page's script gives the fixture's card (`fx-` + this)."""
    return f"{match.competition_id}-{match.date.isoformat()}-{match.home_team}-{match.away_team}"


@dataclass
class MatchMoves:
    """Where one match goes, and under which triggers. `moves` has one entry per
    distinct destination; `uncovered` is the triggers of a match with none."""

    match: Match
    moves: list[Destination] = field(default_factory=list)
    uncovered: tuple[str, ...] | None = None


@dataclass
class Destination:
    to_date: date
    kind: FallbackKind
    venue: str
    kickoff_time: str | None
    triggers: tuple[str, ...]
    probability: float


class MoveIndex:
    """A plan's moves worded for the report: one `MatchMoves` per exposed match,
    and the trigger rounds they refer to.

    A match's destinations are grouped, so triggers that lead to the same date
    are one entry; each entry's probability is the chance at least one of its
    triggers is reached (the planner's rule for the expected-disruption score).
    """

    def __init__(
        self, world: World, plan: ContingencyPlan | None, matches: list[Match], commitments: CommitmentSet
    ) -> None:
        self.world = world
        self.by_match: list[MatchMoves] = []
        self.first = _first_commitments(commitments)
        self._by_node: dict[str, list[Commitment]] = defaultdict(list)
        for c in commitments:
            if not c.hard:
                self._by_node[c.node_id].append(c)
        if plan is None:
            return
        by_key = {m.key: m for m in matches}
        grouped: dict[str, dict[tuple, list[str]]] = defaultdict(lambda: defaultdict(list))
        for move in plan.moves(matches):
            grouped[move.match_key][(move.to_date, move.kind)].append(move.trigger)
        for key, destinations in grouped.items():
            entry = MatchMoves(by_key[key])
            fallback = plan.fallbacks[key]
            for (to_date, kind), triggers in destinations.items():
                entry.moves.append(
                    Destination(
                        to_date, kind, fallback.venue, fallback.kickoff_time, tuple(triggers),
                        self.probability(triggers),
                    )
                )
            entry.moves.sort(key=lambda d: (d.to_date, KIND_ORDER.index(d.kind)))
            self.by_match.append(entry)
        for key in plan.uncovered:
            if key in by_key:
                triggers = plan.uncovered_triggers.get(key, ())
                self.by_match.append(MatchMoves(by_key[key], uncovered=triggers))
        self.by_match.sort(key=lambda e: (e.match.date, e.match.key))

    def probability(self, triggers) -> float:
        return combined_probability(
            self.first[t].reached_probability for t in triggers if t in self.first
        )

    def trigger_team(self, node: str) -> str:
        anchor = self.first.get(node)
        return self.world.team_label(anchor.team_id) if anchor else node

    def trigger_round(self, node: str) -> str:
        anchor = self.first.get(node)
        return round_label(anchor.label) if anchor else ""

    def trigger_sentence(self, node: str) -> str:
        anchor = self.first.get(node)
        if anchor is None:
            return f"If {node} is played"
        return f"If {self.world.team_label(anchor.team_id)} reach {reach_phrase(anchor.label)}"

    def forcing_date(self, node: str, match_day: date) -> date | None:
        """The date of the node's commitment nearest the match, the one that
        pushes it off its day."""
        dates = [c.date for c in self._by_node.get(node, ())]
        return min(dates, key=lambda d: abs((d - match_day).days)) if dates else None


def contingency_view(
    world: World,
    plan: ContingencyPlan | None,
    matches: list[Match],
    commitments: CommitmentSet,
    index: MoveIndex | None = None,
) -> dict | None:
    """The plan's moves as one row per match and destination, in date order;
    each row names its triggers as chips with their probability."""
    if plan is None:
        return None
    index = index or MoveIndex(world, plan, matches, commitments)
    rows = []
    for entry in index.by_match:
        match = entry.match
        for move in entry.moves:
            rows.append(
                {
                    "fixture": _fixture_label(world, match),
                    "key": fixture_key(match),
                    "from": day_label(match.date),
                    "to": day_label(move.to_date),
                    "shift": shift_text((move.to_date - match.date).days),
                    "kind": KIND_LABELS[move.kind],
                    "triggers": [
                        {
                            "team": index.trigger_team(t),
                            "round": index.trigger_round(t),
                            "pct": _percent(index.probability([t])),
                        }
                        for t in move.triggers
                    ],
                }
            )
    return {
        "rows": rows,
        "moves": sum(1 for e in index.by_match if e.moves),
        "uncovered": [
            f"{_fixture_label(world, e.match)}, {day_label(e.match.date)}"
            for e in index.by_match
            if e.uncovered is not None
        ],
    }


def _percent(probability: float) -> int:
    """Whole percent, halves up; the page prints this figure as sent."""
    return math.floor(100 * probability + 0.5)


def moves_wire(index: MoveIndex) -> tuple[list[str], dict[str, dict]]:
    """The page's data for the move badges and ghost fixtures: the trigger
    sentences, and per fixture key its entry for the match wire. A destination
    is `{d, k, t, p, h?, v?}`: date, kind (an index into `KIND_ORDER`), triggers
    as `[trigger index, forcing date]`, whole percent chance one is reached,
    and the kickoff (empty when none is set) and venue when they differ from
    the match's own. An uncovered match is `{x: {t, p}}`."""
    triggers: list[str] = []
    slot: dict[str, int] = {}

    def trigger_index(node: str) -> int:
        if node not in slot:
            slot[node] = len(triggers)
            triggers.append(index.trigger_sentence(node))
        return slot[node]

    def forced_by(entry: MatchMoves, nodes) -> list[list]:
        out = []
        for node in nodes:
            forcing = index.forcing_date(node, entry.match.date)
            out.append([trigger_index(node), forcing.isoformat() if forcing else ""])
        return out

    wire: dict[str, dict] = {}
    for entry in index.by_match:
        match = entry.match
        if entry.uncovered is not None:
            wire[match.key] = {
                "x": {"t": forced_by(entry, entry.uncovered), "p": _percent(index.probability(entry.uncovered))}
            }
            continue
        destinations = []
        for move in entry.moves:
            item: dict = {
                "d": move.to_date.isoformat(),
                "k": KIND_ORDER.index(move.kind),
                "t": forced_by(entry, move.triggers),
                "p": _percent(move.probability),
            }
            if move.kickoff_time != match.kickoff_time:
                item["h"] = move.kickoff_time or ""
            if move.venue != match.venue:
                item["v"] = move.venue
            destinations.append(item)
        wire[match.key] = {"g": destinations}
    return triggers, wire


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


def replan_view(world: World, diff: ReplanDiff | None) -> dict | None:
    """Template data for the re-plan's "changes since the input schedule"
    section (#13)."""
    if diff is None:
        return None
    return {
        "frozen": diff.frozen,
        "total": diff.total,
        "rows": [
            {
                "fixture": f"{world.team_label(m.home_team)} v {world.team_label(m.away_team)}",
                "from": day_label(m.from_date),
                "to": day_label(m.to_date),
                "note": "home and away swapped" if m.flipped else "",
            }
            for m in diff.moved
        ],
        "triggered": diff.triggered,
        "retired": diff.retired,
        "fallbacks": (
            None
            if diff.fallbacks_before is None or diff.fallbacks_after is None
            else (diff.fallbacks_before, diff.fallbacks_after)
        ),
    }


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
