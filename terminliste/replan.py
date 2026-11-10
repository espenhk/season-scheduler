"""Re-plan a season from an existing schedule with its past and announced
rounds frozen (#13).

Real fixture lists are published in parts and revised as UEFA results, cup
draws and TV picks come in. A re-plan takes the schedule as it stands, keeps
every match before the freeze date (and any marked `locked`) exactly as it is,
and plans what remains around the results now known, moving as little as the
new facts allow.

`build_replan_spec` turns the schedule into a `ReplanSpec` the solvers read:
the fixtures to place, the matches that are frozen, and each league's round
slots (frozen rounds keep theirs, the rest are re-planned, `slot_planner.py`).
`diff_schedules` says afterwards what moved and which contingencies the new
results triggered or retired.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Literal

from .contingency import ContingencyPlan, ContingencyPlanner
from .external_schedule import ExternalSchedule
from .model.loader import World
from .model.schema import Competition, Fixture, Match, fixture_identity
from .model.slots import RoundSlot
from .rounds.commitments import CommitmentSet
from .slot_planner import SlotFreeze

if TYPE_CHECKING:
    from .solvers.base import Candidate, SolveRequest, SolverResult

FixtureId = tuple[str, int, str, str]

# A league round spans at most Friday to Monday.
_ROUND_SPAN_DAYS = 3
_WEEKEND_WEEKDAYS = frozenset({4, 5, 6, 0})
_CERTAIN = 1.0 - 1e-9


class ReplanError(ValueError):
    """The schedule being revised cannot be re-planned as it stands."""


@dataclass(frozen=True)
class ReplanSpec:
    """Everything a solver needs to revise an existing schedule."""

    earliest: date
    fixtures: dict[str, list[Fixture]]
    reference: dict[FixtureId, Match]
    frozen: dict[FixtureId, Match]
    slot_freezes: dict[str, SlotFreeze]
    locked: frozenset[FixtureId] = frozenset()

    def is_frozen(self, match: Match | Fixture) -> bool:
        return fixture_identity(match) in self.frozen

    def reference_date(self, match: Match | Fixture) -> date | None:
        """Where the schedule being revised had this fixture."""
        before = self.reference.get(fixture_identity(match))
        return before.date if before is not None else None

    def restore_frozen(self, matches: list[Match]) -> list[Match]:
        """`matches` with each frozen one replaced by its original, so what a
        solver derived afterwards (kickoff times) cannot change it."""
        return [self.frozen.get(fixture_identity(m), m) for m in matches]

    @property
    def stability_reference(self) -> dict[FixtureId, tuple[date, str]]:
        return {k: (m.date, m.home_team) for k, m in self.reference.items()}

    def frozen_requirement(self, competition_id: str, home_team: str, day: date) -> bool:
        """Whether a hard fixed requirement is already history or kept by a
        frozen match, so placing it again would double-book the team."""
        if day < self.earliest:
            return True
        return any(
            m.competition_id == competition_id and m.home_team == home_team and m.date == day
            for m in self.frozen.values()
        )


def build_replan_spec(
    competitions: list[Competition],
    schedule: ExternalSchedule,
    freeze_before: date | None,
) -> ReplanSpec:
    """The re-plan of `schedule`: matches before `freeze_before`, and any the
    file marks locked, stay put.

    Raises `ReplanError` when the schedule is not a complete league schedule,
    since there is nothing to revise otherwise.
    """
    earliest = freeze_before or date.min
    leagues = {c.id: c for c in competitions if c.format == "league"}
    unknown = sorted({m.competition_id for m in schedule.matches} - set(leagues))
    if unknown:
        raise ReplanError(f"the schedule has matches in {', '.join(unknown)}, not a league of this season")

    locked_ids = frozenset(
        fixture_identity(m) for m in schedule.matches if m.key in schedule.locked
    )
    by_competition: dict[str, list[Match]] = {c: [] for c in leagues}
    for match in schedule.matches:
        by_competition[match.competition_id].append(match)

    fixtures: dict[str, list[Fixture]] = {}
    reference: dict[FixtureId, Match] = {}
    frozen: dict[FixtureId, Match] = {}
    freezes: dict[str, SlotFreeze] = {}
    for competition in leagues.values():
        matches = by_competition[competition.id]
        if len(matches) != competition.total_matches:
            raise ReplanError(
                f"{competition.id}: the schedule has {len(matches)} matches, a full season "
                f"{competition.total_matches}; re-planning needs the whole schedule"
            )
        if not schedule.has_rounds:
            matches = _group_into_rounds(matches, competition)
        _check_rounds(matches, competition)

        pinned_rounds: set[int] = set()
        for match in matches:
            identity = fixture_identity(match)
            reference[identity] = match
            if match.date < earliest or identity in locked_ids:
                frozen[identity] = match
                pinned_rounds.add(match.round_index)
        fixtures[competition.id] = [
            Fixture(
                competition_id=m.competition_id,
                home_team=m.home_team,
                away_team=m.away_team,
                leg=m.leg,
                round_index=m.round_index,
            )
            for m in sorted(matches, key=lambda m: (m.round_index, m.home_team))
        ]
        slots = _input_slots(competition, matches)
        freezes[competition.id] = SlotFreeze(
            earliest=earliest,
            pinned={r: slot for r, slot in slots.items() if r in pinned_rounds},
            previous={r: slot for r, slot in slots.items() if r not in pinned_rounds},
        )
    return ReplanSpec(
        earliest=earliest,
        fixtures=fixtures,
        reference=reference,
        frozen=frozen,
        slot_freezes=freezes,
        locked=locked_ids,
    )


def _group_into_rounds(matches: list[Match], competition: Competition) -> list[Match]:
    """Round numbers for a schedule that does not carry them: dates within a
    Friday-to-Monday span form one round."""
    starts: list[date] = []
    round_of: dict[date, int] = {}
    for day in sorted({m.date for m in matches}):
        if not starts or (day - starts[-1]).days > _ROUND_SPAN_DAYS:
            starts.append(day)
        round_of[day] = len(starts) - 1
    if len(starts) != competition.rounds:
        raise ReplanError(
            f"{competition.id}: the dates fall in {len(starts)} rounds, not {competition.rounds}; "
            f"give each row a `round` column to say which round it belongs to"
        )
    return [m.model_copy(update={"round_index": round_of[m.date]}) for m in matches]


def _check_rounds(matches: list[Match], competition: Competition) -> None:
    per_round: dict[int, list[Match]] = {}
    for match in matches:
        per_round.setdefault(match.round_index, []).append(match)
    if sorted(per_round) != list(range(competition.rounds)):
        raise ReplanError(
            f"{competition.id}: rounds {sorted(per_round)[:3]}... do not number "
            f"1 to {competition.rounds}"
        )
    size = len(competition.teams) // 2
    for round_index, in_round in per_round.items():
        teams = [t for m in in_round for t in (m.home_team, m.away_team)]
        repeated = [t for t, n in Counter(teams).items() if n > 1]
        if len(in_round) != size or repeated:
            raise ReplanError(
                f"{competition.id}: round {round_index + 1} has {len(in_round)} matches "
                f"(expected {size}, one per team)"
                + (f", {repeated[0]} plays twice" if repeated else "")
            )


def _input_slots(competition: Competition, matches: list[Match]) -> dict[int, RoundSlot]:
    """Each round as the slot its matches occupy."""
    dates: dict[int, list[date]] = {}
    for match in matches:
        dates.setdefault(match.round_index, []).append(match.date)
    slots = {}
    for round_index, days in dates.items():
        distinct = tuple(sorted(set(days)))
        common = Counter(days)
        default = min(distinct, key=lambda d: (-common[d], d))
        weekend = len(distinct) > 1 or distinct[0].weekday() in _WEEKEND_WEEKDAYS
        slots[round_index] = RoundSlot(
            competition_id=competition.id,
            kind="weekend" if weekend else "midweek",
            start=distinct[0],
            end=distinct[-1],
            days=distinct,
            default_day=default,
            label="as scheduled",
        )
    return slots


def strip_past(commitments: CommitmentSet, earliest: date) -> CommitmentSet:
    """Commitments from `earliest` on: what is over no longer constrains."""
    return CommitmentSet(c for c in commitments if c.date >= earliest)


# -- the diff ----------------------------------------------------------------


@dataclass(frozen=True)
class MatchMove:
    """A fixture the revision placed differently."""

    competition_id: str
    round_number: int
    home_team: str
    away_team: str
    from_date: date
    to_date: date
    flipped: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "competition": self.competition_id,
            "round": self.round_number,
            "home": self.home_team,
            "away": self.away_team,
            "from": self.from_date.isoformat(),
            "to": self.to_date.isoformat(),
            "flipped": self.flipped,
        }


@dataclass(frozen=True)
class AffectedMatch:
    """A match that had a fallback under a trigger that is now settled."""

    match_key: str
    base_date: date
    fallback_date: date | None
    new_date: date | None

    def to_dict(self) -> dict[str, object]:
        return {
            "match": self.match_key,
            "was": self.base_date.isoformat(),
            "fallback_was": self.fallback_date.isoformat() if self.fallback_date else None,
            "now": self.new_date.isoformat() if self.new_date else None,
        }


@dataclass(frozen=True)
class ContingencyChange:
    """A cup or UEFA round the old plan hedged against, now settled: reached
    for certain (`triggered`) or out of the picture (`retired`)."""

    node_id: str
    status: Literal["triggered", "retired"]
    probability_before: float
    matches: tuple[AffectedMatch, ...] = ()

    @property
    def team_id(self) -> str:
        return self.node_id.split("/", 1)[0]

    def to_dict(self) -> dict[str, object]:
        return {
            "node": self.node_id,
            "status": self.status,
            "probability_before": round(self.probability_before, 4),
            "matches": [m.to_dict() for m in self.matches],
        }


@dataclass
class ScheduleDiff:
    """What a re-plan changed against the schedule it started from."""

    moved: list[MatchMove] = field(default_factory=list)
    frozen: int = 0
    unchanged: int = 0
    contingencies: list[ContingencyChange] = field(default_factory=list)
    fallbacks_before: int = 0
    fallbacks_after: int = 0

    @property
    def triggered(self) -> list[ContingencyChange]:
        return [c for c in self.contingencies if c.status == "triggered"]

    @property
    def retired(self) -> list[ContingencyChange]:
        return [c for c in self.contingencies if c.status == "retired"]

    def to_dict(self) -> dict[str, object]:
        return {
            "moved": [m.to_dict() for m in self.moved],
            "frozen": self.frozen,
            "unchanged": self.unchanged,
            "fallbacks_before": self.fallbacks_before,
            "fallbacks_after": self.fallbacks_after,
            "triggered": [c.to_dict() for c in self.triggered],
            "retired": [c.to_dict() for c in self.retired],
        }

    def summary_lines(self, world: World | None = None) -> list[str]:
        def team(team_id: str) -> str:
            return world.team_label(team_id) if world is not None else team_id

        lines = [
            f"{self.frozen} frozen, {len(self.moved)} moved, {self.unchanged} unchanged; "
            f"fallbacks {self.fallbacks_before} -> {self.fallbacks_after}"
        ]
        for move in self.moved:
            flip = " (home/away swapped)" if move.flipped else ""
            lines.append(
                f"  moved {move.competition_id} R{move.round_number} "
                f"{team(move.home_team)} v {team(move.away_team)}: "
                f"{move.from_date} -> {move.to_date}{flip}"
            )
        for change in self.contingencies:
            lines.append(
                f"  {change.status} {change.node_id} (was p={change.probability_before:.2f}), "
                f"{len(change.matches)} match(es) had a fallback"
            )
        return lines


def diff_schedules(
    before: list[Match],
    after: list[Match],
    spec: ReplanSpec,
    old_plan: ContingencyPlan | None,
    new_plan: ContingencyPlan | None,
    new_commitments: CommitmentSet,
) -> ScheduleDiff:
    """Compare a revised schedule with the one it started from, and say which
    of the old plan's contingencies the new results settled."""
    after_by_id = {fixture_identity(m): m for m in after}
    diff = ScheduleDiff(
        fallbacks_before=len(old_plan.fallbacks) if old_plan else 0,
        fallbacks_after=len(new_plan.fallbacks) if new_plan else 0,
    )
    for match in sorted(before, key=lambda m: (m.date, m.competition_id, m.home_team)):
        identity = fixture_identity(match)
        if identity in spec.frozen:
            diff.frozen += 1
            continue
        now = after_by_id.get(identity)
        if now is None:
            continue
        flipped = now.home_team != match.home_team
        if now.date == match.date and not flipped:
            diff.unchanged += 1
            continue
        diff.moved.append(
            MatchMove(
                competition_id=now.competition_id,
                round_number=now.round_index + 1,
                home_team=now.home_team,
                away_team=now.away_team,
                from_date=match.date,
                to_date=now.date,
                flipped=flipped,
            )
        )
    if old_plan is not None:
        diff.contingencies = _contingency_changes(before, after_by_id, old_plan, new_commitments)
    return diff


def _contingency_changes(
    before: list[Match],
    after_by_id: dict[FixtureId, Match],
    old_plan: ContingencyPlan,
    new_commitments: CommitmentSet,
) -> list[ContingencyChange]:
    probability_now: dict[str, float] = {}
    for commitment in new_commitments:
        probability_now[commitment.node_id] = max(
            probability_now.get(commitment.node_id, 0.0), commitment.reached_probability
        )
    before_by_key = {m.key: m for m in before}

    by_node: dict[str, list[str]] = {}
    for key, nodes in old_plan.colliding.items():
        for node_id in nodes:
            by_node.setdefault(node_id, []).append(key)

    changes = []
    for node_id in sorted(by_node):
        now = probability_now.get(node_id, 0.0)
        if 0.0 < now < _CERTAIN:
            continue
        affected = []
        for key in sorted(by_node[node_id]):
            match = before_by_key.get(key)
            if match is None:
                continue
            fallback = old_plan.fallbacks.get(key)
            moved_to = after_by_id.get(fixture_identity(match))
            affected.append(
                AffectedMatch(
                    match_key=key,
                    base_date=match.date,
                    fallback_date=fallback.date if fallback else None,
                    new_date=moved_to.date if moved_to else None,
                )
            )
        changes.append(
            ContingencyChange(
                node_id=node_id,
                status="triggered" if now >= _CERTAIN else "retired",
                probability_before=old_plan.probabilities.get(node_id, 0.0),
                matches=tuple(affected),
            )
        )
    return changes


def finish_replan(
    result: SolverResult,
    request: SolveRequest,
    input_matches: list[Match],
    old_commitments: CommitmentSet,
) -> None:
    """Put the frozen matches back exactly as they were, re-score, and attach
    the diff against `input_matches` to every candidate.

    Solvers derive kickoff times for the whole schedule, which must not touch
    a frozen match; restoring afterwards is what makes them byte-identical.
    """
    from .scoring.base import EvalContext

    spec = request.replan
    if spec is None:
        return
    ctx = EvalContext(
        world=request.world,
        season=request.season,
        travel=request.ctx.travel,
        detail=True,
        away_pairing_max_hours=request.ctx.away_pairing_max_hours,
    )
    old_plan = ContingencyPlanner(
        request.world, request.season, request.competitions, old_commitments
    ).plan(input_matches)
    for candidate in result.candidates:
        _finish_candidate(candidate, request, ctx, spec, input_matches, old_plan)
    result.candidates.sort(key=lambda c: c.score.sort_key)
    for position, candidate in enumerate(result.candidates, start=1):
        candidate.label = f"Option {position}"


def _finish_candidate(
    candidate: Candidate,
    request: SolveRequest,
    ctx,
    spec: ReplanSpec,
    input_matches: list[Match],
    old_plan: ContingencyPlan,
) -> None:
    from .scoring.base import evaluate
    from .solvers.base import attach_contingency_plan

    candidate.matches = spec.restore_frozen(candidate.matches)
    candidate.score = evaluate(candidate.matches, request.constraints, ctx)
    candidate.contingency_plan = None
    attach_contingency_plan(candidate, request)
    candidate.replan = diff_schedules(
        input_matches,
        candidate.matches,
        spec,
        old_plan,
        candidate.contingency_plan,
        request.commitments,
    )
    candidate.locked_keys = frozenset(m.key for m in candidate.matches if fixture_identity(m) in spec.locked)


__all__ = [
    "AffectedMatch",
    "ContingencyChange",
    "MatchMove",
    "ReplanError",
    "ReplanSpec",
    "ScheduleDiff",
    "build_replan_spec",
    "diff_schedules",
    "finish_replan",
    "strip_past",
]
