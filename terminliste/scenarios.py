"""Does a base plan plus its contingency moves stay feasible when cup and UEFA
rounds are really played? (#10)

An outcome is the set of outcome-tree nodes some elimination results make
real. For each one the evaluator moves every match whose fallback a played node
triggers, turns the played commitments into hard ones, and re-scores with the
full rule set. Teams' trees are independent: the joint outcomes are enumerated
exactly when there are few, else sampled with a seeded RNG. A deterministic
worst case plays every node that can be played alongside the others.
"""

from __future__ import annotations

import itertools
import random
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from math import prod

from .contingency import ContingencyPlan, Fallback
from .model.loader import World
from .model.schema import Competition, Match, Season
from .rounds.commitments import Commitment, CommitmentSet
from .rounds.outcome_tree import OutcomeTree
from .scoring.base import EvalContext, Score, evaluate
from .scoring.registry import build_constraints

DEFAULT_SEED = 42
TOP_FAILING_RULES = 5

# Base plans keep reserve dates empty; only a fired fallback may use one.
_RULES_NOT_IN_SCENARIOS = frozenset({"reserve_date_unused"})


@dataclass(frozen=True)
class ScenarioResult:
    """One outcome, re-scored."""

    moves: int
    hard_violations: int
    points: float
    failing_rules: tuple[tuple[str, int], ...] = ()

    @property
    def feasible(self) -> bool:
        return self.hard_violations == 0


@dataclass
class RobustnessReport:
    """How a plan holds up across outcomes. In an exhaustive run every outcome
    is weighted by its probability; in a sampled one each draw counts equally."""

    seed: int
    requested: int
    exhaustive: bool
    outcomes: int
    distinct: int
    feasible_share: float
    expected_moves: float
    max_moves: int
    mean_points: float
    min_points: float
    base_points: float
    # (rule, share of outcomes it fails in, mean violations when it does).
    top_failing: list[tuple[str, float, float]] = field(default_factory=list)
    worst_case: ScenarioResult | None = None

    def to_dict(self) -> dict[str, object]:
        worst = self.worst_case
        return {
            "seed": self.seed,
            "requested": self.requested,
            "exhaustive": self.exhaustive,
            "outcomes": self.outcomes,
            "distinct_outcomes": self.distinct,
            "feasible_share": round(self.feasible_share, 4),
            "expected_moves": round(self.expected_moves, 3),
            "max_moves": self.max_moves,
            "mean_points": round(self.mean_points, 1),
            "min_points": round(self.min_points, 1),
            "base_points": round(self.base_points, 1),
            "top_failing_rules": [
                {"rule": rule, "share": round(share, 4), "mean_violations": round(mean, 2)}
                for rule, share, mean in self.top_failing
            ],
            "worst_case": None
            if worst is None
            else {
                "moves": worst.moves,
                "feasible": worst.feasible,
                "hard_violations": worst.hard_violations,
                "points": round(worst.points, 1),
                "failing_rules": dict(worst.failing_rules),
            },
        }

    def summary_lines(self) -> list[str]:
        mode = "all" if self.exhaustive else "sampled"
        lines = [
            f"{self.feasible_share:.0%} of {self.outcomes} {mode} outcomes feasible; "
            f"moves expected {self.expected_moves:.1f}, max {self.max_moves}; "
            f"points mean {self.mean_points:.0f}, min {self.min_points:.0f} (base {self.base_points:.0f})"
        ]
        if self.worst_case is not None:
            worst = self.worst_case
            verdict = "feasible" if worst.feasible else f"{worst.hard_violations} hard violation(s)"
            lines.append(f"worst case ({worst.moves} moves): {verdict}, {worst.points:.0f}/100")
        for rule, share, _ in self.top_failing:
            lines.append(f"fails {rule} in {share:.0%} of outcomes")
        return lines


class ScenarioEvaluator:
    """Re-scores a base plan under outcomes of the given trees."""

    def __init__(
        self,
        world: World,
        season: Season,
        competitions: list[Competition],
        trees: Iterable[OutcomeTree],
        commitments: CommitmentSet,
        ctx: EvalContext,
    ) -> None:
        self.world = world
        self.season = season
        self.competitions = competitions
        self.ctx = replace(ctx, detail=False)
        self.trees = sorted(trees, key=lambda t: (t.team_id, t.roots))
        self._by_node: dict[str, list[Commitment]] = {}
        for commitment in commitments:
            self._by_node.setdefault(commitment.node_id, []).append(commitment)
        self._paths = [tree.paths() for tree in self.trees]
        self._cache: dict[tuple, Score] = {}

    # -- outcomes ----------------------------------------------------------

    def outcome_count(self) -> int:
        return prod(len(paths) for paths in self._paths)

    def worst_case_nodes(self, plan: ContingencyPlan) -> frozenset[str]:
        """Each tree's run that displaces the most matches (then the longest,
        then the likeliest, then by node id), so exclusive branches resolve the
        same way every run."""
        played: set[str] = set()
        for paths in self._paths:
            nodes, _ = max(
                paths,
                key=lambda item: (
                    sum(1 for f in plan.fallbacks.values() if item[0].intersection(f.trigger_node_ids)),
                    len(item[0]),
                    item[1],
                    sorted(item[0]),
                ),
            )
            played |= nodes
        return frozenset(played)

    def _draws(self, count: int, seed: int) -> tuple[bool, list[tuple[frozenset[str], float]]]:
        """Each draw is (nodes played, weight)."""
        if self.outcome_count() <= count:
            draws = [
                (frozenset().union(*(played for played, _ in combo)), prod(p for _, p in combo))
                for combo in itertools.product(*self._paths)
            ]
            return True, draws
        rng = random.Random(seed)
        draws = []
        for _ in range(count):
            played: set[str] = set()
            for paths in self._paths:
                nodes, _ = rng.choices(paths, weights=[p for _, p in paths])[0]
                played |= nodes
            draws.append((frozenset(played), 1.0 / count))
        return False, draws

    # -- scoring -----------------------------------------------------------

    def apply(
        self, matches: Sequence[Match], plan: ContingencyPlan, played: frozenset[str]
    ) -> tuple[list[Match], list[Fallback]]:
        """The schedule after the fallbacks `played` triggers."""
        fired = [
            f
            for _, f in sorted(plan.fallbacks.items())
            if any(node in played for node in f.trigger_node_ids)
        ]
        moved = {f.match_key: f for f in fired}
        result = [
            m.model_copy(
                update={
                    "date": moved[m.key].date,
                    "venue": moved[m.key].venue,
                    "kickoff_time": moved[m.key].kickoff,
                }
            )
            if m.key in moved
            else m
            for m in matches
        ]
        return result, fired

    def score(
        self, matches: Sequence[Match], plan: ContingencyPlan, played: frozenset[str]
    ) -> tuple[Score, int]:
        scenario, fired = self.apply(matches, plan, played)
        by_team: dict[str, list[Match]] = {}
        for m in scenario:
            by_team.setdefault(m.home_team, []).append(m)
            by_team.setdefault(m.away_team, []).append(m)
        # Only a played commitment close to one of its team's matches can move
        # a score, so outcomes differing elsewhere share one evaluation.
        relevant = sorted(
            {
                c.node_id
                for node in played
                for c in self._by_node.get(node, ())
                if any(c.clashes(m.date) for m in by_team.get(c.team_id, ()))
            }
        )
        key = (tuple(sorted(f.match_key for f in fired)), tuple(relevant))
        cached = self._cache.get(key)
        if cached is None:
            real = CommitmentSet(
                replace(c, hard=True, reached_probability=1.0, exclusive_with=frozenset())
                for node in relevant
                for c in self._by_node[node]
            )
            constraints = [
                c
                for c in build_constraints(self.world, self.season, self.competitions, real)
                if c.id not in _RULES_NOT_IN_SCENARIOS
            ]
            cached = evaluate(scenario, constraints, self.ctx)
            self._cache[key] = cached
        return cached, len(fired)

    def _result(self, score: Score, moves: int) -> ScenarioResult:
        failing = tuple(
            sorted((r.constraint_id, r.count) for r in score.hard_results() if r.count)
        )
        return ScenarioResult(
            moves=moves,
            hard_violations=score.hard_violations,
            points=score.points,
            failing_rules=failing,
        )

    def evaluate(
        self,
        matches: Sequence[Match],
        plan: ContingencyPlan,
        base: Score,
        count: int,
        seed: int = DEFAULT_SEED,
    ) -> RobustnessReport:
        exhaustive, draws = self._draws(max(1, count), seed)
        total = sum(weight for _, weight in draws) or 1.0
        results = [(self._result(*self.score(matches, plan, played)), w / total) for played, w in draws]

        failing_share: Counter[str] = Counter()
        failing_violations: Counter[str] = Counter()
        for result, weight in results:
            for rule, violations in result.failing_rules:
                failing_share[rule] += weight
                failing_violations[rule] += weight * violations
        top = [
            (rule, share, failing_violations[rule] / share)
            for rule, share in sorted(failing_share.items(), key=lambda kv: (-kv[1], kv[0]))[
                :TOP_FAILING_RULES
            ]
        ]
        worst = self._result(*self.score(matches, plan, self.worst_case_nodes(plan)))
        return RobustnessReport(
            seed=seed,
            requested=count,
            exhaustive=exhaustive,
            outcomes=len(draws),
            distinct=len({(r.moves, r.hard_violations, r.points, r.failing_rules) for r, _ in results}),
            feasible_share=sum(w for r, w in results if r.feasible),
            expected_moves=sum(r.moves * w for r, w in results),
            max_moves=max((r.moves for r, _ in results), default=0),
            mean_points=sum(r.points * w for r, w in results),
            min_points=min((r.points for r, _ in results), default=base.points),
            base_points=base.points,
            top_failing=top,
            worst_case=worst,
        )


def evaluate_robustness(
    world: World,
    season: Season,
    competitions: list[Competition],
    frame,
    matches: Sequence[Match],
    plan: ContingencyPlan,
    base: Score,
    ctx: EvalContext,
    count: int,
    seed: int = DEFAULT_SEED,
) -> RobustnessReport:
    """Robustness of one schedule against the frame's cup and UEFA trees."""
    evaluator = ScenarioEvaluator(
        world,
        season,
        competitions,
        [*frame.uefa_trees.values(), *frame.cup_trees.values()],
        frame.commitments,
        ctx,
    )
    return evaluator.evaluate(matches, plan, base, count, seed)


__all__ = [
    "DEFAULT_SEED",
    "RobustnessReport",
    "ScenarioEvaluator",
    "ScenarioResult",
    "evaluate_robustness",
]
