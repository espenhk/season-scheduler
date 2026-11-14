"""Re-plan a season with what is already played or announced frozen (#13).

A fixture list is published in parts and revised as results come in, but a
revision must not move a match somebody has already played or planned a trip
for. A re-plan therefore takes an existing schedule as its reference: every
fixture keeps its round, leg and orientation, matches before the freeze date
(and rows marked `locked`) keep their date too, and only the rest is searched.
Known cup and UEFA results reshape the outcome trees before any of that
(`rounds/known_outcomes.py`), so a resolved branch stops producing
contingencies.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from .display import short_date
from .external_schedule import ExternalSchedule
from .frame import SeasonFrame
from .model.loader import World
from .model.schema import Competition, Fixture, Match
from .rounds.commitments import CommitmentSet


class ReplanError(ValueError):
    """The schedule to revise cannot be re-planned as given."""


@dataclass(frozen=True)
class ReplanSpec:
    """The schedule being revised and which of its matches stay as they are.

    `freeze_before` is the first date that may change: a match strictly before
    it is frozen, and so is any `locked` row. `reference` holds every fixture
    as scheduled, with the kickoff times it came with.
    """

    reference: tuple[Match, ...]
    frozen_keys: frozenset[str]
    freeze_before: date | None = None
    locked_keys: frozenset[str] = frozenset()
    reference_dates: dict[str, date] = field(default_factory=dict)
    # Kickoff of each frozen match, none included: what was announced stays.
    kickoffs: dict[str, str | None] = field(default_factory=dict)
    # With known results given, the only teams whose matches may move: those
    # whose commitments they changed. None leaves every open match free.
    movable_teams: frozenset[str] | None = None

    @classmethod
    def build(
        cls, reference: Iterable[Match], freeze_before: date | None, locked: frozenset[str]
    ) -> ReplanSpec:
        matches = tuple(reference)
        frozen = frozenset(
            m.key
            for m in matches
            if m.key in locked or (freeze_before is not None and m.date < freeze_before)
        )
        return cls(
            reference=matches,
            frozen_keys=frozen,
            freeze_before=freeze_before,
            locked_keys=locked,
            reference_dates={m.key: m.date for m in matches},
            kickoffs={m.key: m.kickoff_time for m in matches if m.key in frozen},
        )

    def fixtures(self, competition_id: str) -> list[Fixture]:
        return [
            Fixture(
                competition_id=m.competition_id,
                home_team=m.home_team,
                away_team=m.away_team,
                leg=m.leg,
                round_index=m.round_index,
            )
            for m in self.reference
            if m.competition_id == competition_id
        ]

    def round_dates(self) -> dict[str, dict[int, tuple[date, ...]]]:
        """Competition -> round -> the dates its matches were scheduled on."""
        rounds: dict[str, dict[int, set[date]]] = {}
        for m in self.reference:
            rounds.setdefault(m.competition_id, {}).setdefault(m.round_index, set()).add(m.date)
        return {
            competition_id: {r: tuple(sorted(days)) for r, days in by_round.items()}
            for competition_id, by_round in rounds.items()
        }

    def with_movable_teams(self, teams: frozenset[str] | None) -> ReplanSpec:
        return replace(self, movable_teams=teams)

    @property
    def pinned_keys(self) -> frozenset[str]:
        """Matches no solver moves: the frozen ones, and the open ones that
        involve no team in `movable_teams`."""
        if self.movable_teams is None:
            return self.frozen_keys
        return self.frozen_keys | {
            m.key
            for m in self.reference
            if self.movable_teams.isdisjoint((m.home_team, m.away_team))
        }


def build_replan_spec(
    competitions: list[Competition], schedule: ExternalSchedule, freeze_before: date | None
) -> ReplanSpec:
    """The spec for revising `schedule`, after checking it is a complete,
    round-numbered season of exactly `competitions`' leagues."""
    if not schedule.has_rounds:
        raise ReplanError(
            "every row of the schedule to revise needs a round number: use a JSON file written "
            "by `generate`, or add a `round` column to the CSV"
        )
    leagues = {c.id: c for c in competitions if c.format == "league"}
    for match in schedule.matches:
        if match.competition_id not in leagues:
            raise ReplanError(
                f"the schedule has a match in {match.competition_id!r}, which is not one of "
                f"the season's leagues ({', '.join(sorted(leagues))})"
            )
    for competition in leagues.values():
        _check_complete(competition, [m for m in schedule.matches if m.competition_id == competition.id])
    return ReplanSpec.build(schedule.matches, freeze_before, schedule.locked)


def _check_complete(competition: Competition, matches: list[Match]) -> None:
    """Every team plays once in each round and every pairing meets as often as
    the league says; anything else leaves fixtures a re-plan could not place."""
    problems: list[str] = []
    by_round: dict[int, list[Match]] = {}
    for m in matches:
        by_round.setdefault(m.round_index, []).append(m)
    if sorted(by_round) != list(range(competition.rounds)):
        problems.append(f"rounds {sorted(r + 1 for r in by_round)}, expected 1..{competition.rounds}")
    for round_index, round_matches in sorted(by_round.items()):
        teams = [t for m in round_matches for t in (m.home_team, m.away_team)]
        if sorted(teams) != sorted(competition.teams):
            problems.append(f"round {round_index + 1} does not have every team playing exactly once")
            break
    meetings: dict[tuple[str, str], int] = {}
    for m in matches:
        pair = tuple(sorted((m.home_team, m.away_team)))
        meetings[pair] = meetings.get(pair, 0) + 1
    if any(n != competition.rounds_per_pairing for n in meetings.values()) or len(meetings) != (
        len(competition.teams) * (len(competition.teams) - 1) // 2
    ):
        problems.append(f"pairings do not meet {competition.rounds_per_pairing} time(s) each")
    if problems:
        raise ReplanError(
            f"{competition.name}: the schedule to revise is not a complete season ("
            + "; ".join(problems)
            + ")"
        )


def settle_frame(frame: SeasonFrame, freeze_before: date | None) -> SeasonFrame:
    """The frame without the commitments that can no longer touch an open day.

    What is already over constrains nothing, and its contingent rounds are not
    something a past match needs a fallback for. A commitment stays while its
    rest days still reach `freeze_before`.
    """
    if freeze_before is None:
        return frame

    def over(day: date, rest: int) -> bool:
        return day + timedelta(days=rest) < freeze_before

    commitments = CommitmentSet(c for c in frame.commitments if not over(c.date, c.min_rest_days))
    return replace(
        frame,
        commitments=commitments,
        uefa_weeks=[w for w in frame.uefa_weeks if not over(w.date, w.min_rest_days)],
        cup_days=[d for d in frame.cup_days if not over(d.date, d.min_rest_days)],
    )


def affected_teams(before: CommitmentSet, after: CommitmentSet) -> frozenset[str]:
    """Teams whose commitments differ between the two sets: a round settled,
    ruled out, or made certain."""

    def signature(commitments: CommitmentSet, team: str) -> frozenset:
        return frozenset(
            (c.node_id, c.date, c.hard, round(c.reached_probability, 6))
            for c in commitments.for_team(team)
            if c.reached_probability > 0
        )

    teams = set(before.team_ids) | set(after.team_ids)
    return frozenset(t for t in teams if signature(before, t) != signature(after, t))


def check_frozen(spec: ReplanSpec, matches: Iterable[Match]) -> list[str]:
    """What differs between each frozen match and its place in the reference;
    empty when every one is exactly as it was."""
    by_key = {m.key: m for m in matches}
    problems = []
    for reference in spec.reference:
        if reference.key not in spec.frozen_keys:
            continue
        placed = by_key.get(reference.key)
        if placed is None:
            problems.append(f"{reference.key} is missing")
        elif placed.model_dump() != reference.model_dump():
            problems.append(f"{reference.key} was changed")
    return problems


@dataclass(frozen=True)
class MatchMove:
    competition_id: str
    round_index: int
    home_team: str
    away_team: str
    from_date: date
    to_date: date
    # The home side the reference had, when the revision turned it round.
    flipped: bool = False


@dataclass
class ReplanDiff:
    """What a revision changed against the schedule it started from."""

    total: int
    frozen: int
    moved: list[MatchMove] = field(default_factory=list)
    # Contingent rounds the known results made certain, and ones they ruled out.
    triggered: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    fallbacks_before: int | None = None
    fallbacks_after: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "matches": self.total,
            "frozen": self.frozen,
            "moved": [
                {
                    "competition": m.competition_id,
                    "round": m.round_index + 1,
                    "home": m.home_team,
                    "away": m.away_team,
                    "from": m.from_date.isoformat(),
                    "to": m.to_date.isoformat(),
                    "home_away_swapped": m.flipped,
                }
                for m in self.moved
            ],
            "contingencies_triggered": list(self.triggered),
            "contingencies_retired": list(self.retired),
            "fallbacks_before": self.fallbacks_before,
            "fallbacks_after": self.fallbacks_after,
        }

    def summary_lines(self, world: World, year: int | None = None) -> list[str]:
        lines = [
            f"{self.frozen} of {self.total} matches frozen; {len(self.moved)} moved",
        ]
        for m in self.moved:
            label = f"{world.team_label(m.home_team)} v {world.team_label(m.away_team)}"
            swapped = " (home and away swapped)" if m.flipped else ""
            lines.append(
                f"  {label}: {short_date(m.from_date, year)} -> {short_date(m.to_date, year)}{swapped}"
            )
        if self.triggered:
            lines.append(f"contingencies triggered: {', '.join(self.triggered)}")
        if self.retired:
            lines.append(f"contingencies retired: {', '.join(self.retired)}")
        if self.fallbacks_before is not None and self.fallbacks_after is not None:
            lines.append(f"fallback dates planned: {self.fallbacks_before} -> {self.fallbacks_after}")
        return lines


def diff_schedules(
    spec: ReplanSpec,
    matches: Iterable[Match],
    before: CommitmentSet | None = None,
    after: CommitmentSet | None = None,
    fallbacks: tuple[int, int] | None = None,
) -> ReplanDiff:
    """The revision's changes against `spec.reference`.

    A fixture is the same one when competition, round and the two teams agree,
    whichever side is at home. `before` and `after` are the commitments with
    and without the known results, for the contingencies they settled.
    """
    reference = {_fixture_id(m): m for m in spec.reference}
    moved = []
    for match in matches:
        old = reference.get(_fixture_id(match))
        if old is None:
            continue
        flipped = old.home_team != match.home_team
        if old.date != match.date or flipped:
            moved.append(
                MatchMove(
                    match.competition_id, match.round_index, match.home_team, match.away_team,
                    old.date, match.date, flipped,
                )
            )
    moved.sort(key=lambda m: (m.from_date, m.competition_id, m.home_team))
    diff = ReplanDiff(total=len(spec.reference), frozen=len(spec.frozen_keys), moved=moved)
    if before is not None and after is not None:
        diff.triggered, diff.retired = settled_nodes(before, after)
    if fallbacks is not None:
        diff.fallbacks_before, diff.fallbacks_after = fallbacks
    return diff


def settled_nodes(before: CommitmentSet, after: CommitmentSet) -> tuple[list[str], list[str]]:
    """The rounds contingent in `before` that `after` makes certain
    (triggered) or no longer has at all (retired)."""
    maybe = {c.node_id for c in before if not c.hard}
    certain = {c.node_id for c in after if c.hard}
    # A branch a result rules out stays in the tree at probability zero.
    present = {c.node_id for c in after if c.reached_probability > 0}
    return sorted(maybe & certain), sorted(maybe - present)


def _fixture_id(match: Match) -> tuple[str, int, frozenset[str]]:
    return match.competition_id, match.round_index, frozenset((match.home_team, match.away_team))


__all__ = [
    "MatchMove",
    "ReplanDiff",
    "ReplanError",
    "ReplanSpec",
    "affected_teams",
    "build_replan_spec",
    "check_frozen",
    "diff_schedules",
    "settle_frame",
    "settled_nodes",
]
